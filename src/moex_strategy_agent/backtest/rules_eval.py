"""Основной вычислитель дерева правил: векторно, pandas.

Контрольный вычислитель (reference_engine) повторяет ту же семантику циклами по барам,
без общего кода с этим модулем.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from ..domain import rules as R

COL = {"close": "Close", "open": "Open", "high": "High", "low": "Low", "volume": "Volume"}


def _ema_raw(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _mask(s: pd.Series, k: int) -> pd.Series:
    s = s.copy()
    if k > 0:
        s.iloc[:k] = np.nan
    return s


class Evaluator:
    def __init__(self, df: pd.DataFrame):
        self.df = df
        self._cache: dict[str, pd.Series] = {}

    def expr(self, e) -> pd.Series:
        key = json.dumps(e.model_dump(), sort_keys=True)
        if key not in self._cache:
            self._cache[key] = self._expr(e)
        return self._cache[key]

    def _expr(self, e) -> pd.Series:
        df = self.df
        if isinstance(e, R.Price):
            return df[COL[e.field]].astype(float)
        if isinstance(e, R.Const):
            return pd.Series(float(e.value), index=df.index)
        if isinstance(e, R.SMA):
            return df[COL[e.source]].astype(float).rolling(e.n, min_periods=e.n).mean()
        if isinstance(e, R.EMA):
            return _mask(_ema_raw(df[COL[e.source]].astype(float), e.n), e.n - 1)
        if isinstance(e, R.RSI):
            c = df["Close"].astype(float)
            d = c.diff()
            g, l = d.clip(lower=0), -d.clip(upper=0)
            ag, al = g.ewm(alpha=1 / e.n, adjust=False).mean(), l.ewm(alpha=1 / e.n, adjust=False).mean()
            out = 100 - 100 / (1 + ag / al)
            out = out.where(al != 0, 100.0)
            return _mask(out, e.n)
        if isinstance(e, R.Highest):
            return df[COL[e.source]].astype(float).rolling(e.n, min_periods=e.n).max().shift(1)
        if isinstance(e, R.Lowest):
            return df[COL[e.source]].astype(float).rolling(e.n, min_periods=e.n).min().shift(1)
        if isinstance(e, R.Change):
            c = df["Close"].astype(float)
            return (c / c.shift(e.n) - 1) * 100
        if isinstance(e, R.AvgVolume):
            return df["Volume"].astype(float).rolling(e.n, min_periods=e.n).mean()
        if isinstance(e, R.MACD):
            c = df["Close"].astype(float)
            macd = _ema_raw(c, e.fast) - _ema_raw(c, e.slow)
            sig = _ema_raw(macd, e.signal)
            line = {"macd": macd, "signal": sig, "hist": macd - sig}[e.line]
            return _mask(line, e.slow + e.signal - 2)
        if isinstance(e, R.Bollinger):
            c = df["Close"].astype(float)
            mid = c.rolling(e.n, min_periods=e.n).mean()
            sd = c.rolling(e.n, min_periods=e.n).std(ddof=0)
            return {"upper": mid + e.k * sd, "middle": mid, "lower": mid - e.k * sd}[e.band]
        if isinstance(e, R.Stochastic):
            lo = df["Low"].astype(float).rolling(e.k, min_periods=e.k).min()
            hi = df["High"].astype(float).rolling(e.k, min_periods=e.k).max()
            k = 100 * (df["Close"].astype(float) - lo) / (hi - lo)
            k = k.where(hi != lo, 50.0).where(hi.notna())
            return k if e.line == "k" else k.rolling(e.d, min_periods=e.d).mean()
        if isinstance(e, R.ATR):
            return atr(df, e.n)
        raise ValueError(f"неизвестное выражение {e}")

    def cond(self, c) -> pd.Series:
        if isinstance(c, R.Always):
            return pd.Series(True, index=self.df.index)
        if isinstance(c, R.Compare):
            l, r = self.expr(c.left), self.expr(c.right)
            out = {">": l > r, "<": l < r, ">=": l >= r, "<=": l <= r}[c.op]
            return out & l.notna() & r.notna()
        if isinstance(c, R.Cross):
            l, r = self.expr(c.left), self.expr(c.right)
            if c.direction == "above":
                return (l.shift(1) < r.shift(1)) & (l > r)
            return (l.shift(1) > r.shift(1)) & (l < r)
        parts = [self.cond(x) for x in c.conditions]
        out = parts[0]
        for p in parts[1:]:
            out = (out & p) if isinstance(c, R.All) else (out | p)
        return out


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["High"].astype(float), df["Low"].astype(float), df["Close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = h.iloc[0] - l.iloc[0]
    return _mask(tr.ewm(alpha=1 / n, adjust=False).mean(), n - 1)


def chart_indicators(ev: Evaluator, rs: R.RuleSet) -> dict[str, pd.Series]:
    """Индикаторы для графика: до трёх линий в ценовой шкале и один осциллятор."""
    overlays, osc = {}, {}
    for e in R.walk_exprs(rs):
        label = R.describe_expr(e)
        if isinstance(e, (R.SMA, R.EMA, R.Highest, R.Lowest, R.Bollinger)) and getattr(e, "source", "close") != "volume":
            if len(overlays) < 3:
                overlays[label] = ev.expr(e)
        elif isinstance(e, (R.RSI, R.Stochastic, R.MACD)) and not osc:
            osc["osc:" + label] = ev.expr(e)
    return {**overlays, **osc}
