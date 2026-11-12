"""Сверка основного и контрольного расчётов.

FAIL блокирует публикацию метрик как подтверждённых: агент обязан об этом сказать.
"""
from __future__ import annotations

import math

from ..domain.schemas import Reconciliation, ReconciliationCheck
from .engine import EngineOutput
from .reference_engine import RefOutput


def _close(a: float, b: float, rel: float, abs_tol: float = 0.01) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_tol)


def reconcile(main: EngineOutput, ref: RefOutput, rel_tol: float = 1e-6) -> Reconciliation:
    checks: list[ReconciliationCheck] = []

    m_entries = [d.date() for d, v in main.entries.items() if v]
    m_exits = [d.date() for d, v in main.exits.items() if v]
    checks.append(ReconciliationCheck(
        name="Сигналы входа", main=len(m_entries), reference=len(ref.entry_dates),
        ok=m_entries == ref.entry_dates,
        note="" if m_entries == ref.entry_dates else _first_diff(m_entries, ref.entry_dates)))
    checks.append(ReconciliationCheck(
        name="Сигналы выхода", main=len(m_exits), reference=len(ref.exit_dates),
        ok=m_exits == ref.exit_dates,
        note="" if m_exits == ref.exit_dates else _first_diff(m_exits, ref.exit_dates)))

    m_tr = [(t.entry_date, t.exit_date, t.size) for t in main.trades]
    r_tr = [(t.entry_date, t.exit_date, t.size) for t in ref.trades]
    checks.append(ReconciliationCheck(name="Сделки (даты и объёмы)", main=len(m_tr), reference=len(r_tr),
                                      ok=m_tr == r_tr,
                                      note="" if m_tr == r_tr else _first_diff(m_tr, r_tr)))

    pnl_ok = len(main.trades) == len(ref.trades) and all(
        _close(a.pnl, b.pnl, 1e-6, 0.05) for a, b in zip(main.trades, ref.trades))
    checks.append(ReconciliationCheck(
        name="PnL сделок", main=round(sum(t.pnl for t in main.trades), 2),
        reference=round(sum(t.pnl for t in ref.trades), 2), ok=pnl_ok))

    m_final, r_final = float(main.equity.iloc[-1]), ref.equity[-1]
    checks.append(ReconciliationCheck(name="Итоговый капитал", main=round(m_final, 2),
                                      reference=round(r_final, 2), ok=_close(m_final, r_final, rel_tol)))

    worst = 0.0
    if len(ref.equity) == len(main.equity):
        worst = max(abs(a - b) / max(abs(b), 1) for a, b in zip(main.equity.tolist(), ref.equity))
        curve_ok = worst <= max(rel_tol, 1e-9) * 10
    else:
        curve_ok = False
    checks.append(ReconciliationCheck(name="Кривая капитала (макс. отклонение)",
                                      main=len(main.equity), reference=len(ref.equity), ok=curve_ok,
                                      note=f"{worst:.2e}"))

    m_dd = float((main.equity / main.equity.cummax() - 1).min() * 100)
    checks.append(ReconciliationCheck(name="Макс. просадка, %", main=round(m_dd, 4),
                                      reference=round(ref.max_drawdown_pct, 4),
                                      ok=_close(m_dd, ref.max_drawdown_pct, 1e-6, 1e-4)))

    lib_final = main.raw_stats.get("equity_final")
    if lib_final is not None:
        # третья точка: итог из штатного отчёта библиотеки, а не из нашей обработки кривой
        checks.append(ReconciliationCheck(
            name="Итог по отчёту backtesting.py", main=round(lib_final, 2),
            reference=round(r_final, 2), ok=_close(lib_final, r_final, rel_tol)))

    failed = [c for c in checks if not c.ok]
    critical = {"Итоговый капитал", "Сделки (даты и объёмы)", "Макс. просадка, %"}
    if not failed:
        status, summary = "OK", "Основной и контрольный расчёты совпали по всем проверкам."
    elif any(c.name in critical for c in failed):
        status = "FAIL"
        summary = "Расчёты разошлись: " + ", ".join(c.name for c in failed) + \
                  ". Метрики не подтверждены."
    else:
        status = "WARN"
        summary = "Есть некритичные расхождения: " + ", ".join(c.name for c in failed) + "."
    return Reconciliation(status=status, checks=checks, summary=summary)


def _first_diff(a: list, b: list) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"первое расхождение #{i + 1}: {x} vs {y}"
    return f"разная длина: {len(a)} vs {len(b)}"
