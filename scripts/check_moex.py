"""Проверка доступа к MOEX ISS с вашей машины.

    python scripts/check_moex.py

Идёт по шагам и останавливается на первом, где связь обрывается:
прокси в окружении → DNS → TCP-соединение → HTTPS-ответ ISS → настоящие свечи SBER.
Ничего секретного не печатает, вывод можно пересылать.
"""
from __future__ import annotations

import os
import socket
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from moex_strategy_agent.data.moex_client import DataSourceError, MoexClient  # noqa: E402

HOST = "iss.moex.com"


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL {msg}")


def ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


ADVICE_NET = """
  Что проверить:
    1. Выключите VPN и повторите: ISS бывает недоступен с зарубежных адресов.
    2. Если сеть рабочая или университетская, попробуйте другую (например, раздачу с телефона).
    3. Откройте в браузере https://iss.moex.com/iss/index.json: если и там не открывается,
       дело в сети, а не в проекте.
  Пока биржа недоступна, агент работает на демо-котировках:
    uv run --python 3.11 python -m moex_strategy_agent --demo"""


def main() -> int:
    print("Проверка доступа к MOEX ISS\n")

    print("1. Прокси в окружении")
    names = [k for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy") if os.environ.get(k)]
    if names:
        print(f"  info задан прокси через {', '.join(names)}: запросы к бирже идут через него")
    else:
        ok("прокси не задан, соединение прямое")

    if names:
        print("\n2–3. DNS и прямое соединение пропускаю: при прокси их проверяет сам прокси")
    else:
        rc = direct_checks()
        if rc:
            return rc
    return https_checks()


def direct_checks() -> int:
    print("\n2. DNS: адрес iss.moex.com")
    t0 = time.monotonic()
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)})
        ok(f"{', '.join(addrs[:3])} за {ms(t0)} мс")
    except OSError as e:
        fail(f"имя не разрешилось: {e}")
        print(ADVICE_NET)
        return 1

    print("\n3. TCP-соединение с портом 443")
    t0 = time.monotonic()
    try:
        with socket.create_connection((HOST, 443), timeout=10):
            ok(f"соединение установлено за {ms(t0)} мс")
    except OSError as e:
        fail(f"соединиться не удалось за {ms(t0)} мс: {e.__class__.__name__}")
        print("  Именно это агент показал как ConnectTimeout: сервер биржи не отвечает на вашей сети.")
        print(ADVICE_NET)
        return 1
    return 0


def https_checks() -> int:
    print("\n4. HTTPS-ответ ISS")
    t0 = time.monotonic()
    try:
        r = httpx.get(f"https://{HOST}/iss/index.json", params={"iss.meta": "off"}, timeout=15)
        if r.status_code != 200:
            fail(f"ISS ответил {r.status_code} за {ms(t0)} мс")
            print("  Биржа отвечает, но отказывает. Повторите через несколько минут.")
            return 1
        ok(f"200 за {ms(t0)} мс")
    except httpx.HTTPError as e:
        fail(f"{e.__class__.__name__} за {ms(t0)} мс")
        if "CERTIFICATE" in str(e).upper():
            print("  Проблема с сертификатом: обычно это антивирус или корпоративный прокси, подменяющий HTTPS.")
        print(ADVICE_NET)
        return 1

    print("\n5. Свечи SBER за последний месяц (тот же клиент, что у агента)")
    t0 = time.monotonic()
    try:
        df = MoexClient(timeout_s=15, max_retries=1).get_candles("SBER", "TQBR", date.today() - timedelta(days=35), date.today())
        if df.empty:
            fail("ISS вернул пустой ответ по SBER")
            return 1
        last = df.iloc[-1]
        ok(f"{len(df)} торговых дней за {ms(t0)} мс, последнее закрытие {float(last['Close']):.2f} ₽ ({df.index[-1].date()})")
    except DataSourceError as e:
        fail(f"{e} за {ms(t0)} мс")
        print(ADVICE_NET)
        return 1
    except Exception as e:  # noqa: BLE001
        fail(f"неожиданная ошибка {e.__class__.__name__}: {e}")
        return 1

    print("\nБиржа доступна. Перезапустите агента без --demo, и он будет считать на настоящих котировках.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
