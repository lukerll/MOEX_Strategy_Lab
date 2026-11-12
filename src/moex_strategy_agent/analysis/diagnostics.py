"""Диагностика: где и почему стратегия теряла деньги.

Каждый вывод здесь посчитан кодом. LLM потом может только пересказать эти факты.
"""
from __future__ import annotations

import itertools
from typing import Any

import numpy as np
import pandas as pd

from ..backtest.reference_engine import run_reference
from ..domain.schemas import StrategySpec, Trade

REGIME_WINDOW = 60
REGIME_THRESHOLD = 0.08
REGIME_NAMES = {"up": "рост", "flat": "боковик", "down": "падение"}


def _ru(text: str) -> str:
    """Русская запятая в дробях: 7.77 -> 7,77."""
    import re
    return re.sub(r"(?<=\d)\.(?=\d)", ",", text)


def _signed(v: float) -> str:
    """Знак в сравнениях, где слова нет: «+14.26%», «−3.54%» (типографский минус, как в карточках)."""
    return ("+" if v > 0 else "−" if v < 0 else "") + f"{abs(v):.2f}%"


def drawdown_episodes(equity: pd.Series, top: int = 3) -> list[dict[str, Any]]:
    peak = equity.cummax()
    dd = equity / peak - 1
    episodes, in_dd, start = [], False, None
    for d, v in dd.items():
        if v < 0 and not in_dd:
            in_dd, start = True, d
        elif v == 0 and in_dd:
            seg = dd[start:d]
            episodes.append((seg.min(), start, seg.idxmin(), d))
            in_dd = False
    if in_dd:
        seg = dd[start:]
        episodes.append((seg.min(), start, seg.idxmin(), None))
    episodes.sort(key=lambda e: e[0])
    out = []
    for depth, s, trough, rec in episodes[:top]:
        out.append({"depth_pct": round(float(depth) * 100, 2), "start": s.date().isoformat(),
                    "trough": trough.date().isoformat(),
                    "recovery": rec.date().isoformat() if rec is not None else None,
                    "days": int(((rec or equity.index[-1]) - s).days)})
    return out


def yearly_returns(equity: pd.Series, bench: pd.Series) -> list[dict[str, Any]]:
    rows = []
    for year in sorted(set(equity.index.year)):
        e, b = equity[equity.index.year == year], bench[bench.index.year == year]
        prev_e = equity[equity.index.year < year]
        prev_b = bench[bench.index.year < year]
        e0 = prev_e.iloc[-1] if len(prev_e) else e.iloc[0]
        b0 = prev_b.iloc[-1] if len(prev_b) else b.iloc[0]
        rows.append({"year": int(year), "strategy_pct": round(float(e.iloc[-1] / e0 - 1) * 100, 2),
                     "buy_hold_pct": round(float(b.iloc[-1] / b0 - 1) * 100, 2)})
    return rows


def market_regimes(close: pd.Series, equity: pd.Series) -> list[dict[str, Any]]:
    """Режим рынка по доходности цены за 60 баров; доходность стратегии внутри каждого режима."""
    trend = close / close.shift(REGIME_WINDOW) - 1
    regime = pd.Series("flat", index=close.index)
    regime[trend > REGIME_THRESHOLD] = "up"
    regime[trend < -REGIME_THRESHOLD] = "down"
    regime = regime.reindex(equity.index).fillna("flat")
    r = np.log(equity / equity.shift(1)).fillna(0)
    out = []
    for key in ("up", "flat", "down"):
        mask = regime == key
        if mask.sum() == 0:
            continue
        out.append({"regime": REGIME_NAMES[key], "days": int(mask.sum()),
                    "strategy_pct": round(float(np.expm1(r[mask].sum())) * 100, 2)})
    return out


def sensitivity(rows: list, spec: StrategySpec) -> list[dict[str, Any]]:
    """Сетка соседних значений двух главных чисел стратегии (±25%) на контрольном движке."""
    from ..domain.rules import numeric_params, set_param

    base = numeric_params(spec.rules)
    if not base:
        return []
    variants = []
    for (label, _, fld, v) in base:
        is_int = fld in ("n", "fast", "slow", "k")
        step = max(1.0, round(v * 0.25)) if is_int else v * 0.25
        variants.append([max(1.0, v - step) if is_int else v - step, v, v + step])
    out = []
    for combo in itertools.product(*variants):
        rs = spec.rules.model_copy(deep=True)
        for (_, kind, fld, old), val in zip(base, combo):
            set_param(rs, kind, fld, old, val)
        try:
            rs = type(rs).model_validate(rs.model_dump())
        except Exception:  # noqa: BLE001, сосед вне допустимых значений пропускается
            continue
        r = run_reference(rows, spec, rules=rs)
        out.append({"params": {f"{t[0]}": v for t, v in zip(base, combo)},
                    "return_pct": round((r.equity[-1] / spec.initial_cash - 1) * 100, 2),
                    "max_drawdown_pct": round(r.max_drawdown_pct, 2), "trades": len(r.trades),
                    "is_current": all(abs(a - t[3]) < 1e-9 for a, t in zip(combo, base))})
    return out


def diagnose(*, spec: StrategySpec, equity: pd.Series, bench: pd.Series, close: pd.Series,
             trades: list[Trade], rows: list, total_return_pct: float,
             with_sensitivity: bool = True) -> dict[str, Any]:
    closed = [t for t in trades if not t.is_open]
    losing = [t for t in closed if t.pnl <= 0]
    quick_losses = [t for t in losing if t.holding_days <= 10]
    worst = sorted(closed, key=lambda t: t.pnl)[:3]

    no_cost = run_reference(rows, spec, commission=0.0, slippage=0.0)
    no_cost_ret = (no_cost.equity[-1] / spec.initial_cash - 1) * 100

    half = len(equity) // 2
    halves = []
    if half > 20:
        for label, seg in (("первая половина", equity.iloc[:half + 1]), ("вторая половина", equity.iloc[half:])):
            halves.append({"label": label, "from": seg.index[0].date().isoformat(),
                           "to": seg.index[-1].date().isoformat(),
                           "strategy_pct": round(float(seg.iloc[-1] / seg.iloc[0] - 1) * 100, 2)})

    diag: dict[str, Any] = {
        "drawdowns": drawdown_episodes(equity),
        "yearly": yearly_returns(equity, bench),
        "regimes": market_regimes(close, equity),
        "false_signals": {"losing_trades": len(losing), "closed_trades": len(closed),
                          "quick_losses": len(quick_losses),
                          "quick_loss_share_pct": round(len(quick_losses) / len(closed) * 100, 1) if closed else 0.0},
        "costs": {"return_without_costs_pct": round(no_cost_ret, 2),
                  "cost_impact_pp": round(no_cost_ret - total_return_pct, 2)},
        "worst_trades": [{"entry": t.entry_date.isoformat(),
                          "exit": t.exit_date.isoformat() if t.exit_date else None,
                          "pnl": round(t.pnl, 2), "return_pct": round(t.return_pct, 2),
                          "holding_days": t.holding_days} for t in worst],
        "halves": halves,
    }
    if with_sensitivity and spec.rules is not None and spec.rules.entry.type != "always":
        diag["sensitivity"] = sensitivity(rows, spec)
    diag["buy_hold_return_pct"] = round(float(bench.iloc[-1] / spec.initial_cash - 1) * 100, 2)
    diag["findings"] = findings(diag, total_return_pct)
    return diag


def findings(d: dict[str, Any], total_return_pct: float) -> list[str]:
    """Детерминированные выводы. Это опора для текста агента."""
    out: list[str] = []
    regimes = {r["regime"]: r for r in d["regimes"]}
    if regimes:
        worst = min(regimes.values(), key=lambda r: r["strategy_pct"])
        if worst["strategy_pct"] < 0:
            out.append(f"Хуже всего стратегия вела себя в режиме «{worst['regime']}»: "
                       f"потеряла {abs(worst['strategy_pct']):.2f}% за {worst['days']} торговых дней.")
    fs = d["false_signals"]
    if fs["closed_trades"] and fs["quick_loss_share_pct"] >= 30:
        out.append(f"{fs['quick_losses']} из {fs['closed_trades']} сделок закрылись в минус "
                   f"быстрее чем за 10 дней: похоже на ложные сигналы («пилу»).")
    c = d["costs"]
    if c["cost_impact_pp"] >= 1:
        out.append(f"Издержки съели {c['cost_impact_pp']:.2f} п.п. доходности "
                   f"(без них было бы {_signed(c['return_without_costs_pct'])}).")
    if d["drawdowns"]:
        dd = d["drawdowns"][0]
        rec = f"восстановился {dd['recovery']}" if dd["recovery"] else "к концу периода капитал не восстановился"
        out.append(f"Самое глубокое падение {abs(dd['depth_pct']):.2f}%: с {dd['start']} до дна {dd['trough']}, {rec}.")
    losers = [y for y in d["yearly"] if y["strategy_pct"] < y["buy_hold_pct"] - 5]
    if losers:
        y = min(losers, key=lambda y: y["strategy_pct"] - y["buy_hold_pct"])
        out.append(f"Сильнее всего отставание от простого держания акции в {y['year']} году: "
                   f"{_signed(y['strategy_pct'])} против {_signed(y['buy_hold_pct'])}.")
    # Устойчивость: важен не разброс цифр, а сохраняется ли вывод «лучше держания» у соседних настроек.
    sens = [s for s in (d.get("sensitivity") or []) if not s.get("is_current")]
    bh = d.get("buy_hold_return_pct")
    if len(sens) >= 4 and bh is not None:
        rets = [s["return_pct"] for s in sens]
        beat = sum(r > bh for r in rets)
        if total_return_pct > bh and beat < len(rets) * 2 / 3:
            out.append(f"Результат неустойчив к параметрам: держание обгоняют только {beat} из {len(rets)} "
                       f"соседних настроек. Возможна подгонка под историю.")
        elif total_return_pct > bh:
            out.append(f"Выигрыш устойчив: {beat} из {len(rets)} соседних настроек тоже обгоняют держание.")
        elif all(r < 0 for r in rets):
            out.append("Все соседние настройки параметров тоже убыточны: дело не в конкретных числах.")
    return [_ru(x) for x in out]
