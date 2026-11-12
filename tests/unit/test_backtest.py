"""Корректность вычислений: эталон против движка, отсутствие заглядывания в будущее, исполнение."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from moex_strategy_agent.backtest import reference_engine as ref
from moex_strategy_agent.backtest.engine import run_engine
from moex_strategy_agent.backtest.metrics import compute_metrics
from moex_strategy_agent.backtest.reconciliation import reconcile
from moex_strategy_agent.backtest.signals import ema, rsi, sma
from moex_strategy_agent.data.demo_source import demo_ohlcv
from tests.conftest import make_spec

STRATS = [("SMA_CROSSOVER", {"fast_period": 20, "slow_period": 50}),
          ("EMA_CROSSOVER", {"fast_period": 12, "slow_period": 26}),
          ("RSI", {"period": 14, "lower": 30, "upper": 70}),
          ("BREAKOUT", {"entry_lookback": 20, "exit_lookback": 10}),
          ("BUY_AND_HOLD", {})]


def _df(ticker: str, end: date) -> pd.DataFrame:
    df = demo_ohlcv(ticker)
    return df[df.index <= pd.Timestamp(end)]


@pytest.mark.parametrize("ticker", ["SBER", "GAZP", "LKOH", "MTSS"])
@pytest.mark.parametrize("stype,params", STRATS)
def test_engine_matches_reference(ticker, stype, params):
    spec = make_spec(ticker=ticker, strategy_type=stype, parameters=params)
    df = _df(ticker, spec.end_date)
    main = run_engine(df, spec)
    r = ref.run_reference(ref.rows_from_df(df), spec)
    rec = reconcile(main, r)
    assert rec.status == "OK", [c for c in rec.checks if not c.ok]
    assert all(t.exit_reason == ("end" if t.is_open else "signal") for t in main.trades)   # без стопов выход только по сигналу


def test_indicators_match_loop_implementations():
    close = demo_ohlcv("SBER")["Close"].iloc[:400]
    x = close.tolist()
    for n in (5, 20):
        np.testing.assert_allclose(sma(close, n).to_numpy(), np.array(ref._sma(x, n), dtype=float), rtol=1e-10)
        np.testing.assert_allclose(ema(close, n).to_numpy(), np.array(ref._ema(x, n), dtype=float), rtol=1e-10)
        np.testing.assert_allclose(rsi(close, n).to_numpy(), np.array(ref._rsi(x, n), dtype=float), rtol=1e-8)


def test_no_lookahead_future_prices_do_not_change_past_decisions():
    spec = make_spec(end_date=date(2025, 12, 31))
    df = _df("SBER", spec.end_date)
    cut = pd.Timestamp("2024-06-28")
    base = run_engine(df, spec)
    shocked = df.copy()
    future = shocked.index > cut
    shocked.loc[future, ["Open", "High", "Low", "Close"]] *= np.linspace(0.3, 3.0, future.sum())[:, None]
    alt = run_engine(shocked, spec)
    pd.testing.assert_series_equal(base.equity[base.equity.index <= cut], alt.equity[alt.equity.index <= cut])
    past = lambda tr: [(t.entry_date, t.exit_date) for t in tr if t.exit_date and pd.Timestamp(t.exit_date) <= cut]
    assert past(base.trades) == past(alt.trades)


def test_execution_next_open_with_slippage_and_commission():
    spec = make_spec(strategy_type="BUY_AND_HOLD", parameters={}, commission=0.001, slippage=0.002)
    df = _df("SBER", spec.end_date)
    out = run_engine(df, spec)
    t = out.trades[0]
    first_bar = df.index[df.index >= pd.Timestamp(spec.start_date)][0]
    fill_bar = df.index[df.index > first_bar][0]
    assert t.entry_date == fill_bar.date()                      # решение на закрытии, исполнение на открытии
    assert t.entry_price == pytest.approx(df.loc[fill_bar, "Open"] * 1.002)
    expected_size = int(spec.initial_cash * (1 - 2.220446049250313e-16) //
                        (df.loc[fill_bar, "Open"] * 1.002 + df.loc[fill_bar, "Open"] * 0.001))
    assert t.size == expected_size
    assert t.is_open and out.equity.iloc[0] == pytest.approx(spec.initial_cash)


def test_metrics_basic_properties():
    spec = make_spec()
    df = _df("SBER", spec.end_date)
    out = run_engine(df, spec)
    m = compute_metrics(out.equity, out.trades, spec.initial_cash, out.position)
    assert m.final_equity == pytest.approx(out.equity.iloc[-1])
    assert m.max_drawdown_pct <= 0
    assert 0 <= m.win_rate_pct <= 100 and 0 <= m.exposure_pct <= 100
    assert m.trades_count == len([t for t in out.trades if not t.is_open])


def test_zero_trades_strategy_reports_flat_equity():
    # RSI с недостижимым нижним уровнем: входов нет, капитал не меняется
    spec = make_spec(strategy_type="RSI", parameters={"period": 14, "lower": 1, "upper": 99})
    df = _df("SBER", spec.end_date)
    out = run_engine(df, spec)
    assert out.trades == []
    assert (out.equity == spec.initial_cash).all()


def test_reconciliation_detects_injected_bug():
    spec = make_spec()
    df = _df("SBER", spec.end_date)
    main = run_engine(df, spec)
    bad = ref.run_reference(ref.rows_from_df(df), spec, commission=0.01)
    assert reconcile(main, bad).status == "FAIL"
