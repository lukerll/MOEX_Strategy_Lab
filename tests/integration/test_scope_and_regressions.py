"""Регрессии из UX-проверки: подмена стратегий, смешение задач, залипший черновик, удаление."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("text,name", [
    ("шорт сбера при пересечении средних", "короткие"),
    ("сбер на часовых свечах SMA 20/50", "внутридневные"),
    ("ишимоку по газпрому", "индикатор"),
    ("усреднение по сберу: докупаю каждые -5%", "усреднение"),
])
def test_unsupported_strategy_is_not_substituted(agent, fake_llm, text, name):
    r = agent.handle(None, text)
    assert r.intent == "unsupported" and "unsupported_strategy" in r.flags
    assert r.run_id is None and r.tools_called == [] and name in r.text
    assert fake_llm.requests == []                 # граница проверена кодом, модель даже не спрашивали


@pytest.mark.parametrize("text", ["SBER SMA 20/50 за 2024, а потом ещё GAZP RSI", "сбер и газпром SMA 20/50"])
def test_multiple_tasks_ask_instead_of_mixing(agent, text):
    r = agent.handle(None, text)
    assert r.intent == "clarify" and "multi_request" in r.flags and r.run_id is None


def test_price_vs_single_ma_maps_to_sma_1_n(agent, repo):
    r = agent.handle(None, "газпром за 2023 год: покупка, когда цена выше 200-дневной средней")
    spec = repo.get_run(r.run_id).spec
    assert spec.parameters == {"fast_period": 1, "slow_period": 200}
    assert "1/200" in r.text and repo.get_run(r.run_id).verification.status == "OK"


def test_validation_error_does_not_stick(agent):
    r = agent.handle(None, "SBER SMA 20/50 с 01.12.2024 по 20.12.2024")
    assert r.intent == "clarify"
    r2 = agent.handle(r.session_id, "протестируй сбер")
    assert r2.run_id, r2.text                       # черновик без ошибочных дат, период по умолчанию
    r3 = agent.handle(None, "SBER SMA 20/50 с 01.12.2024 по 20.12.2024")
    r4 = agent.handle(r3.session_id, "с 2022 по 2024")
    assert r4.run_id                                # исправленный период дополняет черновик


def test_delete_unknown_research_has_no_confirmation(agent):
    r = agent.handle(None, "SBER SMA 20/50 за 2024 год")
    r2 = agent.handle(r.session_id, "удали r_12345678")
    assert r2.pending_action is None and "нет в сохранённом" in r2.text


def test_venue_is_not_a_ticker(agent, repo):
    r = agent.handle(None, "На данных Мосбиржи протестируй SBER SMA 20/50 за 2024 год")
    assert repo.get_run(r.run_id).spec.ticker == "SBER"


def test_followup_numbers_use_russian_decimals(agent):
    r = agent.handle(None, "SBER SMA 20/50 за 2023 год")
    r2 = agent.handle(r.session_id, "покажи худшие сделки")
    assert "." not in r2.text.split("результат")[1].split("₽")[0]


def test_future_period_with_trading_wording(agent, repo):
    """Скриншот пользователя: «давай торговать», бумага есть, стратегии нет, период в будущем."""
    r = agent.handle(None, "Давай торговать только акциями Сбера с 07.11.2026 по 30.11.2030 год")
    assert r.intent == "clarify" and "future_period" in r.flags
    assert "не умею" in r.text and "ещё не наступил" in r.text
    assert r.suggestions
    r2 = agent.handle(r.session_id, r.suggestions[0])
    run = repo.get_run(r2.run_id)
    assert run.spec.ticker == "SBER" and run.spec.end_date <= __import__("datetime").date.today()


@pytest.mark.parametrize("text", ["Давай торговать сбером", "сбер за 2024 год", "газпром, капитал 500 000 рублей"])
def test_ticker_without_strategy_asks_for_strategy(agent, text):
    r = agent.handle(None, text)
    assert r.intent == "clarify" and r.suggestions == ["SMA 20/50", "RSI 14 30/70", "купить и держать"]
