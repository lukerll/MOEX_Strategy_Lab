"""Заранее скачать заголовки СМИ (Google Новости) в кеш, чтобы на показе они были и без VPN.

    python scripts/prefetch_news.py                          # SBER GAZP LKOH YDEX VTBR с 2021 года
    python scripts/prefetch_news.py --tickers SBER --from 2023-01-01

Запускайте с включённым VPN: в России Google Новости без него закрыты. Котировки берутся из кеша
(сначала, без VPN: python scripts/prefetch_moex.py). Скрипт ищет все дни, когда цена сдвинулась
больше чем на порог из configs/settings.yaml (news.move_threshold_pct), и для каждого такого дня
сохраняет заголовки. Агент потом выбирает из этих дней самые резкие для своего периода.
Повторный запуск не качает то, что уже в кеше.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from moex_strategy_agent.config import load_settings  # noqa: E402
from moex_strategy_agent.data.cache import CandleCache  # noqa: E402
from moex_strategy_agent.data.news_client import NewsService, company_name  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2021-01-01")
    ap.add_argument("--tickers", nargs="*", default=["SBER", "GAZP", "LKOH", "YDEX", "VTBR"])
    ap.add_argument("--board", default="TQBR")
    args = ap.parse_args()

    settings = load_settings()
    settings.data.setdefault("news", {})["max_events"] = 10_000      # здесь нужны все дни, а не восемь
    cache = CandleCache(settings.path("data.cache_dir"))
    thr = float(settings.get("news.move_threshold_pct", 4.0)) / 100
    start = date.fromisoformat(args.start)
    svc = NewsService(settings)
    total = 0
    for t in args.tickers:
        df = cache.load(t, args.board, start)
        if df is None or df.empty:
            print(f"  skip {t:<6} котировок нет в кеше: сначала без VPN python scripts/prefetch_moex.py --tickers {t}")
            continue
        ret = df["Close"].pct_change()
        days = [d.date() for d in ret[ret.abs() > thr].index]
        svc._notes.clear()
        try:
            got = svc._google_news(t, days)
        except (httpx.HTTPError, OSError) as e:
            print(f"  FAIL {t:<6} Google Новости не ответили ({e.__class__.__name__}). Включите VPN и запустите снова.")
            return 1
        if "недоступны" in svc._notes.get("google_news", ""):
            print(f"  part {t:<6} часть дней не скачалась: соединение оборвалось, запустите скрипт ещё раз")
        total += len(got)
        print(f"  ok   {t:<6} {company_name(t)}: дней с движением больше {thr:.0%}: {len(days)}, заголовков в кеше: {len(got)}")
    print(f"\nГотово, заголовков: {total}. Теперь агент покажет их и без VPN, с пометкой «из кеша».")
    return 0


if __name__ == "__main__":
    sys.exit(main())
