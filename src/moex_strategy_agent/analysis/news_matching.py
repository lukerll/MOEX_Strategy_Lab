"""Аномальные движения цены и новости рядом с ними.

Совпадение по времени не доказывает причину. Это формулировка по умолчанию в отчёте.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from ..agent.guardrails import sanitize_untrusted
from ..data.market_data import KNOWN_NAMES
from ..data.news_client import RawNews
from ..domain.schemas import NewsItem, PriceEvent

MACRO = ["ключев", "ставк", "банк росси", "санкц", "дивиденд", "мсфо", "рсбу", "отчёт", "отчет",
         "приостанов", "индекс", "размещен", "выкуп", "сплит", "делист", "рубл", "нефть"]


def find_price_events(close: pd.Series, threshold_pct: float = 4.0, sigma: float = 3.0,
                      max_events: int = 8) -> list[PriceEvent]:
    ret = close.pct_change()
    vol = ret.rolling(60, min_periods=20).std()
    thr = pd.concat([pd.Series(threshold_pct / 100, index=ret.index), sigma * vol], axis=1).max(axis=1)
    hits = ret[ret.abs() > thr].dropna()
    hits = hits.reindex(hits.abs().sort_values(ascending=False).index)[:max_events]
    events = [PriceEvent(date=d.date(), change_pct=round(float(v) * 100, 2), close=round(float(close[d]), 4))
              for d, v in hits.items()]
    return sorted(events, key=lambda e: e.date)


def _aliases(ticker: str) -> list[str]:
    names = [k for k, v in KNOWN_NAMES.items() if v == ticker.upper()]
    return [ticker.lower()] + names


def match_news(events: list[PriceEvent], news: list[RawNews], ticker: str,
               before: int = 2, after: int = 1, per_event: int = 3) -> list[PriceEvent]:
    aliases = _aliases(ticker)
    for ev in events:
        lo, hi = ev.date - timedelta(days=before), ev.date + timedelta(days=after)
        scored = []
        for n in news:
            d = n.published_at.date()
            if not (lo <= d <= hi):
                continue
            title, flagged = sanitize_untrusted(n.title)
            low = title.lower()
            score = 0.1
            if any(a in low for a in aliases):
                score += 0.6
            if any(m in low for m in MACRO):
                score += 0.3
            score -= 0.05 * abs((d - ev.date).days)
            scored.append(NewsItem(published_at=n.published_at, title=title, url=n.url, source=n.source,
                                   relevance=round(max(score, 0.0), 2), flagged_injection=flagged))
        scored.sort(key=lambda x: x.relevance, reverse=True)
        ev.news = [s for s in scored if s.relevance >= 0.3 or s.flagged_injection][:per_event]
        if not ev.news:
            ev.note = "подтверждённых новостей рядом с этой датой не найдено"
        else:
            ev.note = "новости найдены рядом по времени; причинная связь не установлена"
    return events
