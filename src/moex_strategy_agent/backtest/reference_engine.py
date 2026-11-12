"""Контрольный расчёт: независимая реализация без pandas и без backtesting.py.

Всё считается явными циклами по барам: индикаторы, условия дерева правил, исполнение,
стопы, деньги. Общего кода с основным движком нет, кроме схемы правил и входных котировок.

Модель издержек (зафиксирована в README, совпадает с брокером backtesting.py):
  * покупка: цена = open * (1 + slippage), комиссия = commission * акции * цена покупки;
  * размер: floor(капитал / (open * (1 + slippage) + open * commission));
  * продажа: цена = open, комиссия = commission * акции * open;
  * открытая на конец позиция переоценивается по последнему close без комиссии выхода.
Стопы (по закрытию, решение исполняется на следующем открытии):
  * стоп-лосс: close <= цена покупки * (1 - x%);   тейк-профит: close >= цена покупки * (1 + x%);
  * трейлинг: close <= максимум закрытий с дня покупки * (1 - x%);
  * по времени: прошло N баров с дня покупки;   ATR-стоп: close <= цена покупки - k * ATR(день решения).
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..domain import rules as R
from ..domain.schemas import StrategySpec

FULL = 1 - sys.float_info.epsilon      # «весь капитал», как в брокере основного движка
NA = None


@dataclass
class RefTrade:
    entry_date: date
    exit_date: date | None
    size: int
    entry_price: float
    exit_price: float | None
    pnl: float


@dataclass
class RefOutput:
    dates: list[date]
    equity: list[float]
    trades: list[RefTrade]
    entry_dates: list[date] = field(default_factory=list)
    exit_dates: list[date] = field(default_factory=list)
    max_drawdown_pct: float = 0.0
    commissions: float = 0.0


# ---------- индикаторы циклами ----------

def _sma(x: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _ema_full(x: list[float | None], n: int) -> list[float | None]:
    """EMA с затравкой первым значением; пропуски в начале пропускаются."""
    a = 2 / (n + 1)
    out: list[float | None] = [None] * len(x)
    cur = None
    for i, v in enumerate(x):
        if v is None:
            continue
        cur = v if cur is None else a * v + (1 - a) * cur
        out[i] = cur
    return out


def _ema(x: list[float], n: int) -> list[float | None]:
    out = _ema_full(x, n)
    return [None if i < n - 1 else v for i, v in enumerate(out)]


def _rsi(x: list[float], n: int) -> list[float | None]:
    a = 1 / n
    out: list[float | None] = [None] * len(x)
    ag = al = 0.0
    for i in range(1, len(x)):
        d = x[i] - x[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        if i == 1:
            ag, al = g, l
        else:
            ag = (1 - a) * ag + a * g
            al = (1 - a) * al + a * l
        if i >= n:
            out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def _prev_extreme(x: list[float], n: int, fn) -> list[float | None]:
    return [fn(x[i - n:i]) if i >= n else None for i in range(len(x))]


def _atr(h: list[float], l: list[float], c: list[float], n: int) -> list[float | None]:
    a = 1 / n
    out: list[float | None] = [None] * len(c)
    cur = 0.0
    for i in range(len(c)):
        tr = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        cur = tr if i == 0 else (1 - a) * cur + a * tr
        if i >= n - 1:
            out[i] = cur
    return out


class RefEval:
    def __init__(self, o: dict[str, list]):
        self.o = o
        self.cache: dict[str, list] = {}

    def series(self, e: Any) -> list[float | None]:
        key = repr(e.model_dump())
        if key not in self.cache:
            self.cache[key] = self._series(e)
        return self.cache[key]

    def _series(self, e: Any) -> list[float | None]:
        o, N = self.o, len(self.o["close"])
        if isinstance(e, R.Price):
            return list(o[e.field])
        if isinstance(e, R.Const):
            return [float(e.value)] * N
        if isinstance(e, R.SMA):
            return _sma(o[e.source], e.n)
        if isinstance(e, R.EMA):
            return _ema(o[e.source], e.n)
        if isinstance(e, R.RSI):
            return _rsi(o["close"], e.n)
        if isinstance(e, R.Highest):
            return _prev_extreme(o[e.source], e.n, max)
        if isinstance(e, R.Lowest):
            return _prev_extreme(o[e.source], e.n, min)
        if isinstance(e, R.Change):
            c = o["close"]
            return [(c[i] / c[i - e.n] - 1) * 100 if i >= e.n else None for i in range(N)]
        if isinstance(e, R.AvgVolume):
            return _sma(o["volume"], e.n)
        if isinstance(e, R.MACD):
            ef, es = _ema_full(o["close"], e.fast), _ema_full(o["close"], e.slow)
            macd = [a - b for a, b in zip(ef, es)]
            sig = _ema_full(macd, e.signal)
            line = {"macd": macd, "signal": sig, "hist": [a - b for a, b in zip(macd, sig)]}[e.line]
            cut = e.slow + e.signal - 2
            return [None if i < cut else v for i, v in enumerate(line)]
        if isinstance(e, R.Bollinger):
            c, out = o["close"], [None] * N
            for i in range(e.n - 1, N):
                w = c[i - e.n + 1:i + 1]
                m = sum(w) / e.n
                sd = math.sqrt(sum((v - m) ** 2 for v in w) / e.n)
                out[i] = {"upper": m + e.k * sd, "middle": m, "lower": m - e.k * sd}[e.band]
            return out
        if isinstance(e, R.Stochastic):
            h, l, c = o["high"], o["low"], o["close"]
            k: list[float | None] = [None] * N
            for i in range(e.k - 1, N):
                hh, ll = max(h[i - e.k + 1:i + 1]), min(l[i - e.k + 1:i + 1])
                k[i] = 50.0 if hh == ll else 100 * (c[i] - ll) / (hh - ll)
            if e.line == "k":
                return k
            d: list[float | None] = [None] * N
            for i in range(N):
                w = k[i - e.d + 1:i + 1] if i >= e.d - 1 else []
                if len(w) == e.d and all(v is not None for v in w):
                    d[i] = sum(w) / e.d
            return d
        if isinstance(e, R.ATR):
            return _atr(o["high"], o["low"], o["close"], e.n)
        raise ValueError(e)

    def cond(self, c: Any, i: int) -> bool:
        if isinstance(c, R.Always):
            return True
        if isinstance(c, R.Compare):
            a, b = self.series(c.left)[i], self.series(c.right)[i]
            if a is None or b is None or (isinstance(a, float) and math.isnan(a)) or (isinstance(b, float) and math.isnan(b)):
                return False
            return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[c.op]
        if isinstance(c, R.Cross):
            if i < 1:
                return False
            L, Rr = self.series(c.left), self.series(c.right)
            vals = (L[i - 1], Rr[i - 1], L[i], Rr[i])
            if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in vals):
                return False
            if c.direction == "above":
                return L[i - 1] < Rr[i - 1] and L[i] > Rr[i]
            return L[i - 1] > Rr[i - 1] and L[i] < Rr[i]
        res = [self.cond(x, i) for x in c.conditions]
        return all(res) if isinstance(c, R.All) else any(res)


def run_reference(rows: list[tuple], spec: StrategySpec, *, commission: float | None = None,
                  slippage: float | None = None, rules: R.RuleSet | None = None) -> RefOutput:
    """rows: (date, open, high, low, close[, volume]) по возрастанию даты, включая прогрев."""
    c = spec.commission if commission is None else commission
    s = spec.slippage if slippage is None else slippage
    rs = rules or spec.rules
    o = {"date": [r[0] for r in rows], "open": [r[1] for r in rows], "high": [r[2] for r in rows],
         "low": [r[3] for r in rows], "close": [r[4] for r in rows],
         "volume": [float(r[5]) if len(r) > 5 else 0.0 for r in rows]}
    n = len(rows)
    p0 = next((i for i, d in enumerate(o["date"]) if d >= spec.start_date), n)
    ev = RefEval(o)
    first = max(p0, 1)
    entry = [False] * n
    exit_ = [False] * n
    if rs.entry.type == "always":
        if first < n:
            entry[first] = True
    else:
        for i in range(first, n):
            entry[i] = ev.cond(rs.entry, i)
    if rs.exit is not None:
        for i in range(first, n):
            exit_[i] = ev.cond(rs.exit, i)
    atr_ser = _atr(o["high"], o["low"], o["close"], rs.atr_stop.n) if rs.atr_stop else None

    cash = spec.initial_cash
    shares, entry_px, entry_d, entry_i, comm_open, peak = 0, 0.0, None, -1, 0.0, 0.0
    pending: str | None = None
    out = RefOutput(dates=[], equity=[], trades=[])
    top, max_dd = -math.inf, 0.0

    for i in range(max(p0 - 1, 0), n):
        op = o["open"][i]
        if pending == "buy":
            px = op * (1 + s)
            qty = int((cash * FULL) // (px + op * c))
            if qty > 0:
                comm_open = qty * px * c
                cash -= qty * px + comm_open
                shares, entry_px, entry_d, entry_i = qty, px, o["date"][i], i
                peak = o["close"][i]
                out.commissions += comm_open
        elif pending == "sell" and shares:
            comm_close = shares * op * c
            cash += shares * op - comm_close
            out.commissions += comm_close
            out.trades.append(RefTrade(entry_d, o["date"][i], shares, entry_px, op,
                                       shares * (op - entry_px) - comm_open - comm_close))
            shares = 0
        pending = None

        close = o["close"][i]
        if shares > 0:
            peak = max(peak, close)
            stop = False
            if rs.stop_loss_pct and close <= entry_px * (1 - rs.stop_loss_pct / 100):
                stop = True
            elif rs.take_profit_pct and close >= entry_px * (1 + rs.take_profit_pct / 100):
                stop = True
            elif rs.trailing_stop_pct and close <= peak * (1 - rs.trailing_stop_pct / 100):
                stop = True
            elif rs.max_hold_days and i - entry_i >= rs.max_hold_days:
                stop = True
            elif rs.atr_stop and entry_i >= 1 and atr_ser[entry_i - 1] is not None and \
                    close <= entry_px - rs.atr_stop.mult * atr_ser[entry_i - 1]:
                stop = True
            if exit_[i] or stop:
                pending = "sell"
        elif entry[i]:
            pending = "buy"

        if i >= p0:
            eq = cash + shares * close
            out.dates.append(o["date"][i])
            out.equity.append(eq)
            top = max(top, eq)
            max_dd = min(max_dd, eq / top - 1)
            if entry[i]:
                out.entry_dates.append(o["date"][i])
            if exit_[i]:
                out.exit_dates.append(o["date"][i])

    if shares:
        last = o["close"][-1]
        out.trades.append(RefTrade(entry_d, None, shares, entry_px, None,
                                   shares * (last - entry_px) - comm_open))
    out.max_drawdown_pct = max_dd * 100
    return out


def rows_from_df(df) -> list[tuple]:
    vol = df["Volume"] if "Volume" in df else None
    return [(ts.date(), float(r.Open), float(r.High), float(r.Low), float(r.Close),
             float(v) if vol is not None else 0.0)
            for (ts, r), v in zip(zip(df.index, df.itertuples(index=False)),
                                  (vol if vol is not None else [0.0] * len(df)))]
