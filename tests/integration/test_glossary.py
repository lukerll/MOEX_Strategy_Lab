"""Вопросы о терминах: объяснение из словаря, без модели и без повторной справки."""
from __future__ import annotations

import pytest

from moex_strategy_agent.agent import glossary
from moex_strategy_agent.agent.guardrails import FORBIDDEN_OUTPUT, check_user_input


def test_users_own_question_gets_overview_not_help(agent, fake_llm):
    r = agent.handle(None, "А что делать если я не понимаю все эти обозначения SMA EMA и так далее ?")
    assert r.intent == "glossary" and "необязательно" in r.text and "RSI" in r.text
    assert not fake_llm.requests                        # словарь отвечает без модели
    assert r.suggestions and len(r.suggestions) == 3


@pytest.mark.parametrize("text,key", [
    ("что такое RSI?", "rsi"), ("Что значит SMA", "sma"), ("не понимаю, что такое стоп-лосс", "stop"),
    ("что такое трейлинг-стоп?", "trailing"), ("что такое MACD", "macd"), ("что за полосы Боллинджера?", "bbands"),
    ("что значит просадка", "drawdown"), ("что такое бэктест", "backtest"), ("что такое ATR?", "atr"),
])
def test_single_term(text, key, agent):
    r = agent.handle(None, text)
    assert r.intent == "glossary" and r.text.startswith(glossary.TERMS[key][1])


@pytest.mark.parametrize("text", [
    "Протестируй стратегию для акций Сбербанка за 2023-2025 годы. Покупать, когда 20-дневная скользящая средняя "
    "пересекает 50-дневную снизу вверх, продавать при обратном пересечении. Объясни, где стратегия теряла деньги.",
    "Газпром за 2024 год, RSI 14 30/70",
    "почему такая просадка?",
    "покажи худшие сделки",
])
def test_strategies_and_followups_are_not_taken_for_questions(text):
    assert glossary.detect(text) is None


def test_every_button_works(agent):
    seen = set()
    for start in ("что значат SMA, RSI и стоп-лосс?", "покажи пример стратегии простыми словами", "что такое RSI?"):
        r = agent.handle(None, start)
        for s in r.suggestions:
            if s in seen:
                continue
            seen.add(s)
            r2 = agent.handle(None, s)
            assert r2.intent == "glossary" or r2.run_id, (s, r2.intent, r2.text)


def test_help_is_not_repeated(agent):
    r = agent.handle(None, "что ты умеешь?")
    assert r.intent == "help"
    r2 = agent.handle(r.session_id, "а что ты умеешь вообще?")
    assert r2.intent == "help" and r2.text != r.text and "Что именно непонятно" in r2.text
    assert r2.suggestions == ["покажи пример стратегии простыми словами", "что значат SMA, RSI и стоп-лосс?"]


def test_texts_are_safe():
    texts = [glossary.OVERVIEW] + [t[2] for t in glossary.TERMS.values()]
    import re
    for t in texts:
        assert "—" not in t and not check_user_input(t).injection
        assert not any(re.search(p, t, re.IGNORECASE) for p, _ in FORBIDDEN_OUTPUT), t
