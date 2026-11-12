"""Язык правил: оба движка совпадают на произвольных деревьях, шаблоны не изменились."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from moex_strategy_agent.backtest.engine import run_engine
from moex_strategy_agent.backtest.reconciliation import reconcile
from moex_strategy_agent.backtest.reference_engine import RefEval, run_reference, rows_from_df
from moex_strategy_agent.backtest.rules_eval import Evaluator
from moex_strategy_agent.data.demo_source import demo_ohlcv
from moex_strategy_agent.domain import rules as R
from moex_strategy_agent.domain.schemas import StrategySpec
from moex_strategy_agent.domain.validators import validate_spec

P = {"type": "price"}
C = lambda v: {"type": "const", "value": v}  # noqa: E731

RULESETS = {
    "цена выше SMA200 и RSI ниже 40, стоп 7%, 20 дней": {
        "entry": {"type": "all", "conditions": [
            {"type": "compare", "left": P, "op": ">", "right": {"type": "sma", "n": 200}},
            {"type": "compare", "left": {"type": "rsi", "n": 14}, "op": "<", "right": C(40)}]},
        "stop_loss_pct": 7, "max_hold_days": 20},
    "MACD пересекает сигнальную, выход по обратному или трейлинг 10%": {
        "entry": {"type": "cross", "left": {"type": "macd"}, "direction": "above", "right": {"type": "macd", "line": "signal"}},
        "exit": {"type": "cross", "left": {"type": "macd"}, "direction": "below", "right": {"type": "macd", "line": "signal"}},
        "trailing_stop_pct": 10},
    "Боллинджер: ниже нижней полосы, продать выше средней, тейк 15%": {
        "entry": {"type": "compare", "left": P, "op": "<", "right": {"type": "bbands", "band": "lower"}},
        "exit": {"type": "compare", "left": P, "op": ">", "right": {"type": "bbands", "band": "middle"}},
        "take_profit_pct": 15},
    "стохастик %K пересекает %D снизу при %K < 20, ATR-стоп 2": {
        "entry": {"type": "all", "conditions": [
            {"type": "cross", "left": {"type": "stoch"}, "direction": "above", "right": {"type": "stoch", "line": "d"}},
            {"type": "compare", "left": {"type": "stoch"}, "op": "<", "right": C(20)}]},
        "exit": {"type": "compare", "left": {"type": "stoch"}, "op": ">", "right": C(80)},
        "atr_stop": {"n": 14, "mult": 2}},
    "рост за 10 дней > 5% или объём выше среднего вдвое (через any), выход EMA": {
        "entry": {"type": "any", "conditions": [
            {"type": "compare", "left": {"type": "change", "n": 10}, "op": ">", "right": C(5)},
            {"type": "all", "conditions": [
                {"type": "compare", "left": {"type": "price", "field": "volume"}, "op": ">", "right": {"type": "avg_volume", "n": 20}},
                {"type": "compare", "left": P, "op": ">", "right": {"type": "highest", "n": 10}}]}]},
        "exit": {"type": "cross", "left": P, "direction": "below", "right": {"type": "ema", "n": 21}}},
    "гистограмма MACD < 0 и цена < минимума 20 дней, без выхода": {
        "entry": {"type": "all", "conditions": [
            {"type": "compare", "left": {"type": "macd", "line": "hist"}, "op": "<", "right": C(0)},
            {"type": "compare", "left": P, "op": "<=", "right": {"type": "lowest", "n": 20}}]}},
}


def _spec(ticker, rules, start=date(2020, 1, 1), end=date(2025, 12, 31)):
    rep = validate_spec(StrategySpec(ticker=ticker, start_date=start, end_date=end, strategy_type="RULES",
                                     rules=R.RuleSet.model_validate(rules)))
    assert rep.ok, rep.errors
    return rep.spec


@pytest.mark.parametrize("ticker", ["SBER", "GAZP", "LKOH", "YDEX"])
@pytest.mark.parametrize("name", list(RULESETS))
def test_engines_agree_on_arbitrary_rules(ticker, name):
    spec = _spec(ticker, RULESETS[name])
    df = demo_ohlcv(ticker)
    df = df[df.index <= pd.Timestamp(spec.end_date)]
    main = run_engine(df, spec)
    ref = run_reference(rows_from_df(df), spec)
    rec = reconcile(main, ref)
    assert rec.status == "OK", (name, [c for c in rec.checks if not c.ok])


def test_every_indicator_main_equals_reference():
    df = demo_ohlcv("SBER").iloc[:600]
    ev, rv = Evaluator(df), RefEval({k: list(v) for k, v in {
        "open": df["Open"], "high": df["High"], "low": df["Low"], "close": df["Close"],
        "volume": df["Volume"].astype(float)}.items()})
    exprs = [R.SMA(n=20), R.EMA(n=12), R.RSI(n=14), R.Highest(n=20), R.Lowest(n=10), R.Change(n=5),
             R.AvgVolume(n=20), R.MACD(), R.MACD(line="signal"), R.MACD(line="hist"), R.Bollinger(band="upper"),
             R.Bollinger(band="lower"), R.Stochastic(), R.Stochastic(line="d"), R.ATR(n=14),
             R.SMA(n=10, source="volume"), R.EMA(n=5, source="high")]
    for e in exprs:
        a = ev.expr(e).to_numpy(dtype=float)
        b = pd.Series([float("nan") if v is None else v for v in rv.series(e)]).to_numpy(dtype=float)
        assert ((pd.isna(a)) == (pd.isna(b))).all(), e
        m = ~pd.isna(a)
        assert abs(a[m] - b[m]).max() < 1e-6 * max(1.0, abs(b[m]).max()), e


def test_stops_actually_trigger():
    spec = _spec("SBER", {"entry": {"type": "always"}, "stop_loss_pct": 5})
    df = demo_ohlcv("SBER")
    df = df[df.index <= pd.Timestamp(spec.end_date)]
    out = run_engine(df, spec)
    first = out.trades[0]
    assert first.exit_date is not None and first.return_pct < 0      # «купить и держать» со стопом вышел по стопу
    assert first.exit_reason == "stop_loss"
    assert all(t.exit_reason == ("end" if t.is_open else "stop_loss") for t in out.trades)
    spec2 = _spec("SBER", {"entry": {"type": "compare", "left": P, "op": ">", "right": {"type": "sma", "n": 50}},
                           "max_hold_days": 7})
    out2 = run_engine(df[df.index <= pd.Timestamp(spec2.end_date)], spec2)
    assert all(t.holding_days <= 12 for t in out2.trades if not t.is_open)   # 7 торговых дней ≈ 9–11 календарных
    assert {t.exit_reason for t in out2.trades if not t.is_open} == {"max_hold"}


def test_describe_rules_reads_like_russian():
    d = R.describe_rules(R.RuleSet.model_validate(RULESETS["цена выше SMA200 и RSI ниже 40, стоп 7%, 20 дней"]))
    assert d["entry"] == "Покупаю, когда цена выше средней за 200 дней и RSI(14) ниже 40."
    assert d["exit"] == "Продаю, когда цена упала на 7% от цены покупки, или когда прошло 20 дней после покупки."


def test_validation_rejects_nonsense():
    rs = R.RuleSet.model_validate({"entry": {"type": "compare", "left": C(1), "op": ">", "right": C(2)}})
    assert R.validate_rules(rs)
    with pytest.raises(Exception):
        R.RuleSet.model_validate({"entry": {"type": "python", "code": "import os"}})
    with pytest.raises(Exception):
        R.RuleSet.model_validate({"entry": {"type": "compare", "left": {"type": "sma", "n": 0}, "op": ">", "right": P}})


def test_sensitivity_changes_shared_params_together():
    from moex_strategy_agent.analysis.diagnostics import sensitivity
    spec = validate_spec(StrategySpec(ticker="SBER", start_date=date(2023, 1, 1), end_date=date(2025, 12, 31),
                                      strategy_type="SMA_CROSSOVER", parameters={"fast_period": 20, "slow_period": 50})).spec
    spec = StrategySpec.model_validate_json(spec.model_dump_json())      # как после хранения в базе
    df = demo_ohlcv("SBER")
    rows = rows_from_df(df[df.index <= pd.Timestamp(spec.end_date)])
    grid = sensitivity(rows, spec)
    assert len(grid) == 9 and sum(g["is_current"] for g in grid) == 1
    cur = [g for g in grid if g["is_current"]][0]
    ref = run_reference(rows, spec)
    assert cur["return_pct"] == round((ref.equity[-1] / spec.initial_cash - 1) * 100, 2)


def test_benchmark_is_buy_and_hold_not_strategy(settings):
    """Регрессия: бенчмарк унаследовал правила стратегии и совпадал с ней до копейки."""
    from moex_strategy_agent.backtest.service import load_data, run_backtest
    from moex_strategy_agent.data.market_data import MarketData
    spec = validate_spec(StrategySpec(ticker="SBER", start_date=date(2021, 1, 1), end_date=date(2025, 12, 31),
                                      strategy_type="SMA_CROSSOVER", parameters={"fast_period": 20, "slow_period": 50})).spec
    res = run_backtest(spec, load_data(MarketData(settings), spec), settings)
    assert res.benchmark.open_position_at_end and res.benchmark.exposure_pct > 99
    assert abs(res.metrics.final_equity - res.benchmark.final_equity) > 1
