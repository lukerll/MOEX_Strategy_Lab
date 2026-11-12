"""Каждая подсказка, которую показывает интерфейс, должна быть фразой, понятной агенту."""
from __future__ import annotations

import pytest

STARTS = ["SBER за 2023–2025, SMA 20 и 50", "Газпром за 2024 год, RSI 14: покупка ниже 30, продажа выше 70",
          "Лукойл за 2022–2024: пробой 20-дневного максимума, выход по минимуму за 10 дней",
          "Лукойл, купить и держать за последние 3 года"]
OK_INTENTS = {"backtest", "modify", "explain", "trades", "news", "save", "compare", "history"}


@pytest.mark.parametrize("start", STARTS)
def test_every_suggestion_after_backtest_is_executable(agent, start):
    r = agent.handle(None, start)
    assert r.run_id and 2 <= len(r.suggestions) <= 3
    for s in r.suggestions:
        r2 = agent.handle(r.session_id, s)
        assert r2.intent in OK_INTENTS, (s, r2.intent, r2.text)
        if r2.intent in ("backtest", "modify"):
            assert r2.run_id, (s, r2.text)


def test_examples_on_greeting_are_executable(agent):
    r = agent.handle(None, "привет")
    assert r.suggestions
    for s in r.suggestions:
        assert agent.handle(None, s).run_id, s


def test_clarify_suggestions_complete_the_request(agent):
    r = agent.handle(None, "SMA 20/50 за 2024 год")
    assert r.intent == "clarify" and r.suggestions == ["SBER", "GAZP", "LKOH"]
    r2 = agent.handle(r.session_id, r.suggestions[0])
    assert r2.run_id
    r3 = agent.handle(None, "протестируй газпром")
    assert r3.intent == "clarify"
    for s in r3.suggestions:
        r4 = agent.handle(r3.session_id, s)
        assert r4.run_id, (s, r4.text)
        r3 = agent.handle(None, "протестируй газпром")


def test_unsupported_suggests_supported_alternatives(agent):
    r = agent.handle(None, "газпром, ишимоку")           # честной замены нет: только готовые примеры
    assert r.suggestions and all("GAZP" in s for s in r.suggestions)
    for s in r.suggestions:
        assert agent.handle(None, s).run_id, s
