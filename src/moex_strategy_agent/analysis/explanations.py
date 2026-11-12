"""Факты для объяснения и шаблонный текст.

Модель получает только этот набор фактов. Любое число в её ответе потом сверяется
с ним кодом (guardrails.unsupported_numbers).
"""
from __future__ import annotations

from typing import Any

from ..agent.guardrails import DISCLAIMER, wrap_untrusted
from ..domain.schemas import STRATEGY_TITLES, BacktestResult


def r2(x: float | None) -> float | None:
    return None if x is None else round(float(x), 2)


def build_facts(res: BacktestResult) -> dict[str, Any]:
    m, b, d = res.metrics, res.benchmark, res.diagnostics
    facts: dict[str, Any] = {
        "strategy": res.spec.title(),
        "strategy_type": STRATEGY_TITLES[res.spec.strategy_type],
        "ticker": res.spec.ticker,
        "period": {"from": res.spec.start_date.isoformat(), "to": res.spec.end_date.isoformat(),
                   "trading_days": res.data.rows},
        "initial_cash": m.initial_cash,
        "final_equity": r2(m.final_equity),
        "total_return_pct": r2(m.total_return_pct),
        "cagr_pct": r2(m.cagr_pct),
        "max_drawdown_pct": r2(m.max_drawdown_pct),
        "volatility_pct": r2(m.volatility_pct),
        "sharpe": r2(m.sharpe),
        "trades": m.trades_count,
        "exit_reasons": _exit_reasons(res),
        "win_rate_pct": r2(m.win_rate_pct),
        "profit_factor": r2(m.profit_factor),
        "exposure_pct": r2(m.exposure_pct),
        "commissions_paid": r2(m.commissions_paid),
        "open_position_at_end": m.open_position_at_end,
        "buy_hold_return_pct": r2(b.total_return_pct),
        "buy_hold_max_drawdown_pct": r2(b.max_drawdown_pct),
        "excess_return_pp": r2(m.total_return_pct - b.total_return_pct),
        "verification": {"status": res.verification.status, "summary": res.verification.summary},
        "findings": d.get("findings", []),
        "regimes": d.get("regimes", []),
        "yearly": d.get("yearly", []),
        "costs": d.get("costs", {}),
        "data_source": res.data.source,
        "synthetic_data": res.data.is_synthetic,
        "data_warnings": res.data.warnings[:5],
        "assumptions": res.assumptions,
        "commission_pct": r2(res.spec.commission * 100),
        "slippage_pct": r2(res.spec.slippage * 100),
        "news_status": res.news_status,
        "verdict": _verdict(m.total_return_pct - b.total_return_pct),
        "entry_check": d.get("entry_check"),
        "price_events": [{"date": e.date.isoformat(), "change_pct": e.change_pct,
                          "news_found": len([n for n in e.news if not n.flagged_injection])}
                         for e in res.events],
    }
    return facts


def _verdict(excess: float) -> str:
    if abs(excess) < 0.005:
        return "результат стратегии совпал с «купить и держать»"
    return f"стратегия {'обогнала' if excess > 0 else 'отстала от'} «купить и держать» на {abs(excess):.2f} п.п."


_WORSE = r"(уступил|проиграл|отстал|хуже)"
_BETTER = r"(обогнал|опередил|превзошл|выиграл|лучше)"
_BENCH = r"(купить и держать|купи и держи|держани|удержани|владени)"
_PART = r"(\b(19|20)\d\d\b|боковик|\bрост|падени|просадк|сделк|риск)"


def contradicts_verdict(text: str, excess: float) -> bool:
    """Модель перепутала, кто кого обогнал за весь период. Фразы про отдельный год или режим не считаются."""
    import re
    if abs(excess) < 0.005:
        return False
    wrong = _WORSE if excess > 0 else _BETTER
    for sent in re.split(r"(?<=[.!?])\s+", text):
        low = sent.lower()
        if re.search(r"стратеги\w+[^.]{0,40}" + wrong, low) and re.search(_BENCH, low) and not re.search(_PART, low):
            return True
    return False


_REASON_RU = {"signal": "сигнал на выход", "stop_loss": "стоп-лосс", "take_profit": "тейк-профит",
              "trailing": "трейлинг-стоп", "max_hold": "срок удержания", "atr_stop": "стоп по ATR"}


def _exit_reasons(res: BacktestResult) -> dict[str, int]:
    """Сколько закрытых сделок вышло по каждой причине: модель может сказать «две из пяти по стопу»."""
    out: dict[str, int] = {}
    for t in res.trades:
        if not t.is_open and t.exit_reason:
            k = _REASON_RU.get(t.exit_reason, t.exit_reason)
            out[k] = out.get(k, 0) + 1
    return out


def news_block(res: BacktestResult) -> str:
    """Заголовки новостей для модели: только в обёртке недоверенных данных."""
    lines = []
    for e in res.events:
        for n in e.news:
            title = "[скрыто: подозрение на prompt injection]" if n.flagged_injection else n.title
            lines.append(f"{e.date} ({e.change_pct:+.2f}%): {title} | {n.source}")
    return wrap_untrusted("news", "\n".join(lines) or "нет новостей") if lines else ""


def numbers_in(obj: Any) -> list[float]:
    """Все числа из фактов, плюс производные, которые модель вправе назвать."""
    out: list[float] = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out += [float(obj), round(float(obj), 1), round(float(obj)), abs(float(obj))]
    elif isinstance(obj, dict):
        for v in obj.values():
            out += numbers_in(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out += numbers_in(v)
    elif isinstance(obj, str):
        from ..agent.guardrails import _numbers
        out += _numbers(obj)
    return out


def ru_decimals(text: str) -> str:
    """7.77 -> 7,77: русская запятая в дробях. Даты и тикеры не трогает."""
    import re
    return re.sub(r"(?<=\d)\.(?=\d)", ",", text)


def no_trades_narrative(res: BacktestResult) -> str | None:
    """Отчёт без сделок объясняет код: называет условие, которое не сработало. Модель не нужна."""
    from ..agent.typography import tidy_prose
    from .sanity import no_trades_text
    lead = no_trades_text(res.diagnostics.get("entry_check"))
    if not lead or res.metrics.open_position_at_end:
        return None
    f = build_facts(res)
    bh = f["buy_hold_return_pct"]
    lines = [lead, f"Капитал всё время лежал в деньгах. «Купить и держать» за тот же срок дало "
                   f"{'прибыль' if bh > 0 else 'убыток'} {abs(bh):.2f}%."]
    if res.verification.status == "FAIL":
        lines.insert(0, "Основной и контрольный расчёты разошлись, цифры не подтверждены.")
    if res.data.is_synthetic:
        lines.append("Это демо-режим на синтетических котировках, а не данные биржи.")
    lines.append(DISCLAIMER)
    return tidy_prose(ru_decimals(" ".join(lines)))


def template_narrative(res: BacktestResult) -> str:
    """Текст без модели. Сухо, по фактам, в одном регистре."""
    f = build_facts(res)
    m = res.metrics
    lines = []
    if res.verification.status == "FAIL":
        lines.append("Основной и контрольный расчёты разошлись, поэтому цифры ниже не подтверждены. "
                     + res.verification.summary)
    lines.append(f"{f['strategy']}, период с {f['period']['from']} по {f['period']['to']} "
                 f"({f['period']['trading_days']} торговых дней).")
    ret_word = "заработала" if m.total_return_pct > 0 else "потеряла"
    lines.append(f"Стратегия {ret_word} {abs(f['total_return_pct']):.2f}%, "
                 f"максимальная просадка {abs(f['max_drawdown_pct']):.2f}%, "
                 f"сделок: {f['trades']}, прибыльных {f['win_rate_pct']:.2f}%.")
    bh = f["buy_hold_return_pct"]
    bh_word = "прибыль" if bh > 0 else "убыток"
    diff = f["excess_return_pp"]
    if abs(diff) < 0.005:
        cmp = "результат совпал"
    else:
        cmp = f"стратегия {'обогнала его' if diff > 0 else 'отстала от него'} на {abs(diff):.2f} п.п."
    lines.append(f"«Купить и держать» за тот же срок дало {bh_word} {abs(bh):.2f}% "
                 f"при просадке {abs(f['buy_hold_max_drawdown_pct']):.2f}%: {cmp}")
    if m.trades_count == 0 and not m.open_position_at_end:
        lines.append("Сигналов на входе за период не было: стратегия просидела в деньгах.")
    if m.open_position_at_end:
        lines.append("На конец периода позиция открыта, она оценена по последней цене закрытия.")
    paras = [" ".join(lines)]
    if f["findings"]:
        first = " ".join(f["findings"][:4])
        paras.append("Где теряла: " + first[0].lower() + first[1:])
    tail = []
    if res.verification.status == "WARN":
        tail.append(res.verification.summary)
    if res.data.is_synthetic:
        tail.append("Это демо-режим на синтетических котировках, а не данные биржи.")
    tail.append(DISCLAIMER)
    paras.append(" ".join(tail))
    from ..agent.typography import tidy_prose
    return tidy_prose(ru_decimals("\n\n".join(paras)))
