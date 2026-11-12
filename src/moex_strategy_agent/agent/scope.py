"""Границы возможностей, которые проверяет код до модели.

Модель формализует стратегию, но не решает, что агенту разрешено. Здесь только то,
чего нет в языке правил: короткие позиции, внутридневные свечи, прогнозы нейросетью,
несколько бумаг в одном запросе. Агент не подменяет такой запрос похожим.
"""
from __future__ import annotations

import re
from typing import Any

from ..data.demo_source import DEMO_UNIVERSE
from ..data.market_data import KNOWN_NAMES

NOT_TICKERS = {"SMA", "EMA", "RSI", "ISS", "API", "MACD", "USD", "RUB", "CAGR", "ETF", "IPO", "HTML",
               "CSV", "PDF", "JSON", "MSCI", "OK", "AND", "HOLD", "BUY", "SELL", "LLM", "EUR", "CNY", "МСФО",
               "ATR", "BB", "SAR", "ADX", "CCI", "OBV", "VWAP", "STOP", "LOSS", "TAKE"}
KNOWN_TICKERS = set(KNOWN_NAMES.values()) | set(DEMO_UNIVERSE)

SUPPORTED_TEXT = ("условия на цене, объёме, SMA, EMA, RSI, MACD, полосах Боллинджера, стохастике, ATR, "
                  "максимумах и минимумах за N дней, связанные через «и» и «или»; стоп-лосс, тейк-профит, "
                  "трейлинг-стоп, выход через N дней")

UNSUPPORTED: list[tuple[str, str, str]] = [
    (r"\bшорт|\bshort\b|коротк\w+ позици|продаж\w* без покрыт|игра\w* на пониж", "короткие позиции",
     "только покупка и продажа уже купленного"),
    (r"мартингейл|martingale|усреднен|докупа\w*|пирамидинг", "докупку и усреднение",
     "вход всем капиталом по сигналу и выход целиком"),
    (r"минутн|часов\w* свеч|\b\d+\s*(мин|m)\b|интрадей|intraday|внутридн|скальп", "внутридневные свечи",
     "дневные свечи"),
    (r"нейросет|машинн\w* обучен|\blstm\b|прогноз\w* цен", "прогнозирование цены моделью",
     "проверка правил на истории"),
    (r"ишимоку|ichimoku|\bvwap\b|парабол|parabolic|\bsar\b|\badx\b|\bcci\b|\bobv\b|фибоначч|fibonacci",
     "этот индикатор", "SMA, EMA, RSI, MACD, Боллинджер, стохастик или ATR"),
    (r"дивиденд\w* (отсеч|гэп)|отчетност|по новост\w* покуп|покупа\w* на новост", "сигналы по новостям и календарю",
     "сигналы по цене и объёму; новости я показываю рядом с результатом"),
]

# Замена, которую агент предлагает вопросом: (вопрос, готовый ответ). Сам он запрос не меняет.
# Где честной замены нет (другой индикатор, прогноз, новости), предложения нет.
OFFERS: dict[str, tuple[str, str]] = {
    "короткие позиции": ("Могу посчитать ту же идею только на покупку: сигналы на шорт пропущу. Считать так?",
                         "да, только покупка"),
    "докупку и усреднение": ("Могу посчитать одним входом: всем капиталом по первому сигналу, выход целиком. "
                             "Считать так?", "да, одним входом"),
    "внутридневные свечи": ("Могу посчитать те же правила на дневных свечах. Считать так?",
                            "да, на дневных свечах"),
}

_VENUE = re.compile(r"(данн\w*|на|с|котировк\w*|истори\w*)\s+(мосбирж\w*|московск\w+ бирж\w*|moex)"
                    r"|moex\s+iss|мосбирж\w*\s+iss", re.IGNORECASE)


def _norm(text: str) -> str:
    t = text.lower().replace("ё", "е")
    return re.sub(r"[–—−]", "-", t)


def find_tickers(text: str) -> list[str]:
    """Все упомянутые бумаги по порядку. «На данных Мосбиржи»: площадка, а не акция MOEX."""
    cleaned = _VENUE.sub(" ", text)
    found: list[tuple[int, str]] = []
    for m in re.finditer(r"\b[A-Z][A-Z0-9]{2,5}\b", cleaned):
        tok = m.group(0)
        if tok not in NOT_TICKERS and (tok in KNOWN_TICKERS or len(tok) >= 4):
            found.append((m.start(), tok))
    low = _norm(cleaned)
    for name, ticker in KNOWN_NAMES.items():
        for m in re.finditer(r"(?<![а-яa-z])" + re.escape(name) + r"[а-я]*", low):
            found.append((m.start(), ticker))
    out: list[str] = []
    for _, t in sorted(found):
        if t not in out:
            out.append(t)
    return out


def scope_check(text: str) -> dict[str, Any]:
    low = _norm(text)
    for pat, name, nearest in UNSUPPORTED:
        if re.search(pat, low):
            return {"kind": "unsupported", "name": name, "nearest": nearest, "offer": OFFERS.get(name)}
    tickers = find_tickers(text)
    if len(tickers) > 1:
        return {"kind": "multi", "what": "бумаги: " + ", ".join(tickers), "tickers": tickers}
    return {}


def scope_hint(text: str) -> str | None:
    """Короткая подсказка под полем ввода, пока человек ещё печатает. Модель не вызывается."""
    if len((text or "").strip()) < 8:
        return None
    sc = scope_check(text)
    if sc.get("kind") == "unsupported":
        name = sc["name"][0].upper() + sc["name"][1:]
        return f"{name} не поддерживаю. Вместо этого: {sc['nearest']}."
    if sc.get("kind") == "multi":
        return f"По одной бумаге за запрос, а здесь {sc['what'].removeprefix('бумаги: ')}."
    return None
