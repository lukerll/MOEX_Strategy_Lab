"""Проверка источников новостей с вашей машины.

    python scripts/check_news.py            # Сбербанк, 2023-2025
    python scripts/check_news.py GAZP 2024

Для каждого источника печатает, сколько событий он дал за период, и по три примера.
Ничего секретного не печатает, вывод можно пересылать.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("MSA_DATA_MODE", "moex")

from moex_strategy_agent.config import get_settings  # noqa: E402
from moex_strategy_agent.data.news_client import SOURCE_TITLES, NewsService  # noqa: E402


def main() -> None:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "SBER").upper()
    if len(sys.argv) > 2:
        y = int(sys.argv[2])
        start, end = date(y, 1, 1), date(y, 12, 31)
    else:
        start, end = date(2023, 1, 1), date(2025, 12, 31)
    settings = get_settings()
    # для Google Новостей берём середину каждого квартала: в агенте это дни резких движений цены
    probes = [date(y, m, 15) for y in range(start.year, end.year + 1) for m in (2, 5, 8, 11)
              if start <= date(y, m, 15) <= end][:8]
    print(f"Новости для {ticker} с {start} по {end}\n")
    for src in settings.get("news.sources", []):
        settings.data["news"]["sources"] = [src]
        svc = NewsService(settings)
        items = svc.fetch(start, end, ticker=ticker, event_dates=probes)
        title = SOURCE_TITLES.get(src, src)
        print(f"{title}: {svc.coverage.get(title, 'нет данных')}")
        for n in sorted(items, key=lambda x: x.published_at)[:3]:
            print(f"    {n.published_at:%d.%m.%Y}  {n.title[:110]}")
        print()


if __name__ == "__main__":
    main()
