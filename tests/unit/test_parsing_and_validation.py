from __future__ import annotations

from datetime import date

import pytest

from tests.support.scripted_parser import understand  # разборщик имитации модели
from moex_strategy_agent.domain.schemas import StrategySpec
from moex_strategy_agent.domain.validators import validate_spec
from moex_strategy_agent.llm.provider import parse_json_object

TODAY = date(2026, 11, 7)
TZ_EXAMPLE = ("Протестируй стратегию для акций Сбербанка за 2023–2025 годы. Покупать, когда 20-дневная "
              "скользящая средняя пересекает 50-дневную снизу вверх, продавать при обратном пересечении. "
              "Начальный капитал 100 000 рублей. Сравни результат с обычным удержанием акций и объясни, "
              "где стратегия теряла деньги.")


def test_tz_example_is_formalized_exactly():
    u = understand(TZ_EXAMPLE, today=TODAY)
    assert u.intent == "backtest"
    assert u.spec_patch == {"ticker": "SBER", "start_date": "2023-01-01", "end_date": "2025-12-31",
                            "strategy_type": "SMA_CROSSOVER",
                            "parameters": {"fast_period": 20, "slow_period": 50}, "initial_cash": 100000.0}


@pytest.mark.parametrize("text,ticker,stype,params", [
    ("GAZP RSI(14) покупка ниже 25 продажа выше 75 с 01.03.2022 по 31.12.2024", "GAZP", "RSI",
     {"period": 14, "lower": 25, "upper": 75}),
    ("пробой 20-дневного максимума, выход по минимуму за 10 дней, лукойл", "LKOH", "BREAKOUT",
     {"entry_lookback": 20, "exit_lookback": 10}),
    ("EMA 12/26 по ВТБ в 2024 году", "VTBR", "EMA_CROSSOVER", {"fast_period": 12, "slow_period": 26}),
    ("купить и держать яндекс с 2020", "YDEX", "BUY_AND_HOLD", {}),
])
def test_strategy_variants(text, ticker, stype, params):
    u = understand(text, today=TODAY)
    assert u.spec_patch["ticker"] == ticker
    assert u.spec_patch["strategy_type"] == stype
    assert u.spec_patch.get("parameters", {}) == params


def test_modify_uses_last_spec():
    u = understand("а если 10 и 30?", today=TODAY, last_spec={"strategy_type": "SMA_CROSSOVER", "ticker": "SBER"})
    assert u.intent == "modify" and u.spec_patch["parameters"] == {"fast_period": 10, "slow_period": 30}


@pytest.mark.parametrize("text,intent", [
    ("покажи худшие сделки", "trades"), ("сохрани исследование", "save"), ("какая погода в москве", "out_of_scope"),
    ("что ты умеешь?", "help"), ("какие новости были?", "news"), ("удали r_1234abcd", "delete")])
def test_intents(text, intent):
    assert understand(text, today=TODAY, last_spec={"strategy_type": "SMA_CROSSOVER"}).intent == intent


def _spec(**kw):
    base = dict(ticker="SBER", start_date=date(2023, 1, 1), end_date=date(2025, 12, 31),
                strategy_type="SMA_CROSSOVER", parameters={"fast_period": 20, "slow_period": 50})
    base.update(kw)
    return StrategySpec(**base)


def test_validator_rules():
    assert validate_spec(_spec(), today=TODAY).ok
    assert not validate_spec(_spec(parameters={"fast_period": 50, "slow_period": 20}), today=TODAY).ok
    assert not validate_spec(_spec(start_date=date(2025, 12, 1)), today=TODAY).ok          # слишком коротко
    assert not validate_spec(_spec(strategy_type="RSI", parameters={"lower": 80, "upper": 20}), today=TODAY).ok
    rep = validate_spec(_spec(end_date=date(2030, 1, 1)), today=TODAY)
    assert rep.ok and rep.spec.end_date == TODAY and rep.assumptions


def test_validator_fills_defaults_and_drops_unknown_params():
    rep = validate_spec(_spec(strategy_type="RSI", parameters={"period": 10, "foo": 1}), today=TODAY)
    assert rep.ok and rep.spec.parameters == {"period": 10, "lower": 30, "upper": 70}
    assert any("foo" in a for a in rep.assumptions)


def test_spec_rejects_garbage():
    with pytest.raises(Exception):
        StrategySpec(ticker="SBER; DROP TABLE", start_date="2023-01-01", end_date="2024-01-01",
                     strategy_type="SMA_CROSSOVER")
    with pytest.raises(Exception):
        StrategySpec(ticker="SBER", start_date="2023-01-01", end_date="2024-01-01", strategy_type="MARTINGALE")


def test_parse_json_object_from_noisy_output():
    assert parse_json_object('Вот:\n```json\n{"intent": "help", "x": "a}b"}\n```') == {"intent": "help", "x": "a}b"}
