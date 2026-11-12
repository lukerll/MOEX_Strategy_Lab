"""Типографика текста агента: слова вместо знаков, без длинного тире, диапазоны через дефис."""
from moex_strategy_agent.agent.typography import tidy_prose


def test_sign_removed_after_direction_word():
    assert tidy_prose("показала убыток −17,38%.") == "показала убыток 17,38%."
    assert tidy_prose("в 2025-м прибыль была +14,26%") == "в 2025-м прибыль была 14,26%"
    assert tidy_prose("снижение капитала составило −33,75%") == "снижение капитала составило 33,75%"


def test_em_dash_before_number_and_between_words():
    assert tidy_prose("значительный убыток — −25,03%.") == "значительный убыток 25,03%."
    assert tidy_prose("падения рынка — минус 46,82%") == "падения рынка: минус 46,82%"
    assert tidy_prose("Это важно — и вот почему.") == "Это важно - и вот почему."
    assert "—" not in tidy_prose("а — б — в")


def test_ranges_and_dates():
    assert tidy_prose("Период 2023–2026, февраль–июль.") == "Период 2023-2026, февраль-июль."
    assert tidy_prose("с 2023-01-01 по 2026-11-01") == "с 2023-01-01 по 2026-11-01"
    assert tidy_prose("в 2024-м") == "в 2024-м"


def test_bare_minus_becomes_typographic():
    assert tidy_prose("Доходность -5,10% против +3,00%.") == "Доходность −5,10% против +3,00%."
