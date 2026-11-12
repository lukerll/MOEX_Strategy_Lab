"""Сигналы основного движка: дерево правил, вычисленное векторно (rules_eval).

Правила исполнения, общие для обоих движков (зафиксированы в README):
  * сигнал считается по цене закрытия бара t, сделка исполняется по открытию бара t+1;
  * сигнал на баре t допустим, только если в данных есть бар t-1;
  * только длинные позиции, весь доступный капитал, целое число акций;
  * стопы, тейк-профит и выход по времени проверяются по закрытию (см. engine.py).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..domain.schemas import StrategySpec
from .rules_eval import Evaluator, atr, chart_indicators

# Совместимость: старые функции индикаторов (используются тестами и графиками)
from .rules_eval import _ema_raw  # noqa: F401


def sma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n, min_periods=n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    out = close.ewm(span=n, adjust=False).mean()
    out.iloc[: n - 1] = np.nan
    return out


def rsi(close: pd.Series, n: int) -> pd.Series:
    from ..domain.rules import RSI
    return Evaluator(pd.DataFrame({"Close": close}))._expr(RSI(n=n))


def compute_signals(df: pd.DataFrame, spec: StrategySpec, period_start: pd.Timestamp
                    ) -> tuple[pd.Series, pd.Series, dict[str, pd.Series], pd.Series | None]:
    """Сигналы входа и выхода по условиям (без стопов). Вне периода False."""
    rs = spec.rules
    ev = Evaluator(df)
    entry = ev.cond(rs.entry)
    exit_ = ev.cond(rs.exit) if rs.exit is not None else pd.Series(False, index=df.index)
    if rs.entry.type == "always":
        # «купить и держать»: вход ровно на первом допустимом баре периода
        entry = pd.Series(False, index=df.index)
        eligible = [i for i in np.flatnonzero(df.index >= period_start) if i >= 1]
        if eligible:
            entry.iloc[eligible[0]] = True
    mask = pd.Series(df.index >= period_start, index=df.index)
    mask.iloc[0] = False
    entry = entry.fillna(False).astype(bool) & mask
    exit_ = exit_.fillna(False).astype(bool) & mask
    atr_series = atr(df, rs.atr_stop.n) if rs.atr_stop else None
    return entry, exit_, chart_indicators(ev, rs), atr_series
