"""Метрики по кривой капитала и журналу сделок. Безрисковая ставка = 0 (см. README)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..domain.schemas import Metrics, Trade

TRADING_DAYS = 252


def drawdown_series(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1


def compute_metrics(equity: pd.Series, trades: list[Trade], initial_cash: float,
                    position: pd.Series | None = None) -> Metrics:
    eq = equity.astype(float)
    final = float(eq.iloc[-1])
    total = final / initial_cash - 1
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    cagr = (final / initial_cash) ** (365.25 / days) - 1 if final > 0 else -1.0
    rets = eq.pct_change().dropna()
    vol = float(rets.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(rets) > 1 else 0.0
    sharpe = float(rets.mean() / rets.std(ddof=1) * np.sqrt(TRADING_DAYS)) \
        if len(rets) > 1 and rets.std(ddof=1) > 0 else 0.0

    closed = [t for t in trades if not t.is_open]
    pnls = np.array([t.pnl for t in closed]) if closed else np.array([])
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    pf = float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None
    rets_pct = [t.return_pct for t in closed]
    exposure = float((position > 0).mean() * 100) if position is not None and len(position) else 0.0

    return Metrics(
        initial_cash=initial_cash, final_equity=final,
        total_return_pct=total * 100, cagr_pct=cagr * 100,
        max_drawdown_pct=float(drawdown_series(eq).min() * 100),
        volatility_pct=vol * 100, sharpe=sharpe,
        trades_count=len(closed),
        win_rate_pct=float(len(wins) / len(closed) * 100) if closed else 0.0,
        avg_trade_pnl=float(pnls.mean()) if closed else 0.0,
        avg_trade_return_pct=float(np.mean(rets_pct)) if closed else 0.0,
        profit_factor=pf,
        best_trade_pct=float(max(rets_pct)) if closed else None,
        worst_trade_pct=float(min(rets_pct)) if closed else None,
        avg_holding_days=float(np.mean([t.holding_days for t in closed])) if closed else 0.0,
        exposure_pct=exposure,
        commissions_paid=float(sum(t.commission for t in trades)),
        open_position_at_end=any(t.is_open for t in trades),
    )
