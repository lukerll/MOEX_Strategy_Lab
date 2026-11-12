"""Скачать котировки в кеш заранее, чтобы агент работал без доступа к бирже (VPN, поезд, защита).

    python scripts/prefetch_moex.py                      # популярные акции с 2015 года
    python scripts/prefetch_moex.py --from 2010-01-01 --tickers SBER GAZP LKOH

Запускайте, когда биржа доступна (например, с выключенным VPN). Потом агент при недоступной
бирже берёт котировки из кеша и пишет в результате, до какого дня они есть.
Повторный запуск докачивает только новое.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from moex_strategy_agent.config import load_settings  # noqa: E402
from moex_strategy_agent.data.cache import CandleCache  # noqa: E402
from moex_strategy_agent.data.market_data import KNOWN_NAMES  # noqa: E402
from moex_strategy_agent.data.moex_client import DataSourceError, MoexClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2015-01-01")
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--board", default="TQBR")
    args = ap.parse_args()

    settings = load_settings()
    cache = CandleCache(settings.path("data.cache_dir"))
    client = MoexClient(timeout_s=30, max_retries=2)
    tickers = args.tickers or sorted(set(KNOWN_NAMES.values()))
    start, end = date.fromisoformat(args.start), date.today()
    print(f"Качаю {len(tickers)} бумаг с {start} по {end} в {cache.root}\n")
    ok = 0
    for t in tickers:
        cov = cache.coverage(t, args.board)
        frm = max(start, cov[1]) if cov and cov[0] <= start else start      # докачиваем только хвост
        t0 = time.monotonic()
        try:
            df = client.get_candles(t, args.board, frm, end)
            cache.save(t, args.board, df, frm, end)
            full = cache.coverage(t, args.board)
            print(f"  ok   {t:<6} {len(df):>5} дней за {time.monotonic() - t0:4.1f} с, в кеше с {full[0]} по {full[1]}")
            ok += 1
        except DataSourceError as e:
            print(f"  FAIL {t:<6} {e}")
            if "недоступен" in str(e):
                print("\nБиржа не отвечает. Выключите VPN или смените сеть и запустите скрипт снова.")
                print("Проверить связь по шагам: python scripts/check_moex.py")
                return 1
    print(f"\nГотово: {ok} из {len(tickers)}. Теперь агент посчитает эти бумаги и без доступа к бирже.")
    return 0 if ok == len(tickers) else 2


if __name__ == "__main__":
    raise SystemExit(main())
