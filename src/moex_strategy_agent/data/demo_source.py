"""Синтетические котировки для офлайн-демо и eval.

Это НЕ данные биржи. Ряд детерминирован (сид от тикера), со сменой режимов
(рост, боковик, падение), чтобы у стратегий были и удачные, и провальные периоды.
Каждый результат на этих данных помечается is_synthetic=True.
"""
from __future__ import annotations

import zlib
from datetime import date

import numpy as np
import pandas as pd

from .moex_client import NoDataError

DEMO_START = date(2014, 1, 6)
DEMO_END = date(2026, 11, 6)

# тикер -> (стартовая цена, годовой дрейф, годовая волатильность, название)
DEMO_UNIVERSE: dict[str, tuple[float, float, float, str]] = {
    "SBER": (100.0, 0.12, 0.32, "Сбербанк"),
    "GAZP": (140.0, -0.02, 0.30, "Газпром"),
    "LKOH": (2000.0, 0.14, 0.27, "Лукойл"),
    "YDEX": (1500.0, 0.10, 0.40, "Яндекс"),
    "VTBR": (60.0, -0.05, 0.38, "ВТБ"),
    "ROSN": (230.0, 0.08, 0.30, "Роснефть"),
    "GMKN": (150.0, 0.03, 0.29, "Норникель"),
    "NVTK": (400.0, 0.09, 0.31, "Новатэк"),
    "MGNT": (5000.0, 0.02, 0.30, "Магнит"),
    "TATN": (250.0, 0.13, 0.30, "Татнефть"),
    "MTSS": (300.0, 0.01, 0.24, "МТС"),
    "MOEX": (60.0, 0.11, 0.28, "Московская биржа"),
}

# Демонстрационная «приостановка торгов»: дыра в данных, на которой видна проверка качества
DEMO_GAPS = {"GAZP": (date(2022, 2, 28), date(2022, 3, 23))}


def _regimes(rng: np.random.Generator, n: int, drift: float) -> np.ndarray:
    """Дневной дрейф с переключением режимов каждые ~60–250 дней."""
    mu = np.empty(n)
    i = 0
    while i < n:
        length = int(rng.integers(60, 250))
        kind = rng.choice(["up", "flat", "down"], p=[0.4, 0.35, 0.25])
        annual = {"up": drift + 0.35, "flat": 0.0, "down": drift - 0.40}[kind]
        mu[i:i + length] = annual / 252
        i += length
    return mu


def demo_ohlcv(ticker: str) -> pd.DataFrame:
    ticker = ticker.upper()
    if ticker not in DEMO_UNIVERSE:
        raise NoDataError(f"Тикер {ticker} отсутствует в демо-наборе. Доступны: {', '.join(DEMO_UNIVERSE)}")
    p0, drift, vol, _ = DEMO_UNIVERSE[ticker]
    rng = np.random.default_rng(zlib.crc32(ticker.encode()))
    days = pd.bdate_range(DEMO_START, DEMO_END)
    n = len(days)
    sigma = vol / np.sqrt(252)
    mu = _regimes(rng, n, drift)
    # толстые хвосты: t-распределение и редкие шоки
    shocks = rng.standard_t(4, n) * sigma / np.sqrt(2)
    jumps = (rng.random(n) < 0.006) * rng.normal(0, 0.06, n)
    log_ret = mu - 0.5 * sigma ** 2 + shocks + jumps
    close = p0 * np.exp(np.cumsum(log_ret))
    gap_open = rng.normal(0, sigma * 0.35, n)
    open_ = np.empty(n)
    open_[0] = p0
    open_[1:] = close[:-1] * np.exp(gap_open[1:])
    spread = np.abs(rng.normal(0, sigma * 0.6, n))
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread * 0.9)
    volume = (rng.lognormal(16, 0.5, n) * (1 + 8 * np.abs(log_ret))).astype("int64")
    df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
                      index=pd.DatetimeIndex(days, name="Date")).round(4)
    if ticker in DEMO_GAPS:
        a, b = DEMO_GAPS[ticker]
        df = df[(df.index < pd.Timestamp(a)) | (df.index > pd.Timestamp(b))]
    return df


def demo_name(ticker: str) -> str | None:
    item = DEMO_UNIVERSE.get(ticker.upper())
    return item[3] if item else None
