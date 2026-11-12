"""Основной расчёт: исполнение сигналов через backtesting.py."""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy

from ..domain.schemas import StrategySpec, StrategyType, Trade
from .signals import compute_signals


@dataclass
class EngineOutput:
    equity: pd.Series                 # стоимость портфеля на закрытии каждого бара периода
    trades: list[Trade]
    entries: pd.Series                # сигналы на периоде
    exits: pd.Series
    indicators: dict[str, pd.Series] = field(default_factory=dict)
    position: pd.Series | None = None  # число акций на закрытии бара
    raw_stats: dict = field(default_factory=dict)


class _SignalStrategy(Strategy):
    """Исполняет готовые массивы сигналов и стопы. Решение на закрытии, исполнение на открытии."""

    entry_arr: np.ndarray = np.array([])
    exit_arr: np.ndarray = np.array([])
    atr_arr: np.ndarray | None = None
    stop_loss: float | None = None       # доли, не проценты
    take_profit: float | None = None
    trailing: float | None = None
    max_hold: int | None = None
    atr_mult: float | None = None

    def init(self) -> None:
        self._peak = 0.0
        self._entry_bar = -1
        self.exit_reasons: dict[int, str] = {}     # бар входа -> причина выхода

    def _stop_hit(self, i: int, close: float) -> str | None:
        """Какой из защитных выходов сработал (первый по порядку проверки), либо None."""
        tr = self.trades[-1]
        if tr.entry_bar != self._entry_bar:          # новая позиция: сбрасываем максимум
            self._entry_bar, self._peak = tr.entry_bar, close
        self._peak = max(self._peak, close)
        p = tr.entry_price
        if self.stop_loss and close <= p * (1 - self.stop_loss):
            return "stop_loss"
        if self.take_profit and close >= p * (1 + self.take_profit):
            return "take_profit"
        if self.trailing and close <= self._peak * (1 - self.trailing):
            return "trailing"
        if self.max_hold and i - tr.entry_bar >= self.max_hold:
            return "max_hold"
        if self.atr_mult and self.atr_arr is not None and tr.entry_bar >= 1:
            a = self.atr_arr[tr.entry_bar - 1]          # ATR на дне решения о покупке
            if not np.isnan(a) and close <= p - self.atr_mult * a:
                return "atr_stop"
        return None

    def next(self) -> None:
        i = len(self.data) - 1
        close = float(self.data.Close[-1])
        if self.position:
            reason = "signal" if self.exit_arr[i] else self._stop_hit(i, close)
            if reason:
                self.exit_reasons[self.trades[-1].entry_bar] = reason
                self.position.close()
        elif self.entry_arr[i] and not self.orders:
            self.buy()


def run_engine(df: pd.DataFrame, spec: StrategySpec) -> EngineOutput:
    start = pd.Timestamp(spec.start_date)
    entries, exits, ind, atr_series = compute_signals(df, spec, start)
    rs = spec.rules

    p0 = int(np.searchsorted(df.index.values, start.to_datetime64()))
    lo = max(p0 - 1, 0)                     # один бар до периода: решение на первом баре периода
    data = df.iloc[lo:][["Open", "High", "Low", "Close", "Volume"]]
    pct = lambda v: v / 100 if v else None  # noqa: E731
    strat = type("SpecStrategy", (_SignalStrategy,), {
        "entry_arr": entries.iloc[lo:].to_numpy(),
        "exit_arr": exits.iloc[lo:].to_numpy(),
        "atr_arr": atr_series.iloc[lo:].to_numpy() if atr_series is not None else None,
        "stop_loss": pct(rs.stop_loss_pct), "take_profit": pct(rs.take_profit_pct),
        "trailing": pct(rs.trailing_stop_pct), "max_hold": rs.max_hold_days,
        "atr_mult": rs.atr_stop.mult if rs.atr_stop else None,
    })
    bt = Backtest(data, strat, cash=spec.initial_cash, commission=spec.commission,
                  spread=spec.slippage, trade_on_close=False, exclusive_orders=False,
                  finalize_trades=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        stats = bt.run()

    eq = stats["_equity_curve"]["Equity"]
    period_mask = data.index >= start
    equity = eq[period_mask].astype(float)
    idx = data.index

    strategy_obj = stats["_strategy"]
    reasons: dict[int, str] = getattr(strategy_obj, "exit_reasons", {})
    trades: list[Trade] = []
    for _, r in stats["_trades"].iterrows():
        entry_d, exit_d = idx[int(r["EntryBar"])], idx[int(r["ExitBar"])]
        trades.append(Trade(
            entry_date=entry_d.date(), exit_date=exit_d.date(), size=int(r["Size"]),
            entry_price=float(r["EntryPrice"]), exit_price=float(r["ExitPrice"]),
            pnl=float(r["PnL"]), return_pct=float(r["ReturnPct"]) * 100,
            commission=float(r.get("Commission", 0.0)),
            holding_days=int((exit_d - entry_d).days), is_open=False,
            exit_reason=reasons.get(int(r["EntryBar"]), "signal")))

    # открытая на конец периода позиция: переоценка по последнему закрытию, без комиссии выхода
    last_close = float(data["Close"].iloc[-1])
    for tr in strategy_obj.trades:
        entry_d = idx[tr.entry_bar]
        comm_open = tr.size * tr.entry_price * spec.commission
        pnl = tr.size * (last_close - tr.entry_price) - comm_open
        trades.append(Trade(
            entry_date=entry_d.date(), exit_date=None, size=int(tr.size),
            entry_price=float(tr.entry_price), exit_price=None, pnl=float(pnl),
            return_pct=float(pnl / (tr.size * tr.entry_price) * 100), commission=float(comm_open),
            holding_days=int((idx[-1] - entry_d).days), is_open=True, exit_reason="end"))

    position = _position_series(equity.index, trades)
    return EngineOutput(
        equity=equity, trades=trades,
        entries=entries[entries.index >= start], exits=exits[exits.index >= start],
        indicators={k: v[v.index >= start] for k, v in ind.items()}, position=position,
        raw_stats={"return_pct": float(stats["Return [%]"]),
                   "max_drawdown_pct": float(stats["Max. Drawdown [%]"]),
                   "trades": int(stats["# Trades"]), "equity_final": float(stats["Equity Final [$]"])},
    )


def _position_series(index: pd.DatetimeIndex, trades: list[Trade]) -> pd.Series:
    pos = pd.Series(0, index=index, dtype="int64")
    for t in trades:
        a = pd.Timestamp(t.entry_date)
        if t.exit_date is None:
            pos[index >= a] = t.size
        else:
            pos[(index >= a) & (index < pd.Timestamp(t.exit_date))] = t.size
    return pos


def is_buy_and_hold(spec: StrategySpec) -> bool:
    return spec.strategy_type == StrategyType.BUY_AND_HOLD
