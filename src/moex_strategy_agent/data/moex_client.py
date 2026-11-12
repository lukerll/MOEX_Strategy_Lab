"""HTTP-клиент MOEX ISS: свечи, поиск инструментов, новости биржи.

Документация: https://iss.moex.com/iss/reference/
Ответ ISS: блоки вида {"candles": {"columns": [...], "data": [[...], ...]}}.
Страницы по 500 строк, листаем параметром start.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any

import httpx
import pandas as pd

ISS_BASE = "https://iss.moex.com/iss"
PAGE = 500

# Предохранитель: если ISS не отвечает по сети, следующие запросы несколько минут не ждут
# таймаутов, а сразу идут в кеш. Иначе каждый вопрос к агенту стоил бы ~20 секунд ожидания.
BREAKER_SECONDS = 300
_down_until: dict[str, float] = {}


def reset_breaker() -> None:
    _down_until.clear()


class DataSourceError(Exception):
    """Ошибка источника данных. retryable=True: сеть, таймаут, 5xx."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class NoDataError(DataSourceError):
    """Источник ответил, но данных нет: неизвестный тикер, пустой период."""


def _block(payload: dict[str, Any], name: str) -> pd.DataFrame:
    blk = payload.get(name) or {}
    return pd.DataFrame(blk.get("data", []), columns=blk.get("columns", []))


class MoexClient:
    def __init__(self, timeout_s: float = 15, max_retries: int = 3,
                 transport: httpx.BaseTransport | None = None, base_url: str = ISS_BASE):
        self.max_retries = max_retries
        self.base_url = base_url
        # соединение ждём недолго: если биржа не отвечает, три попытки по 15 секунд только тянут время
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=min(7.0, timeout_s)), transport=transport,
                                    headers={"User-Agent": "moex-strategy-agent/0.1"})

    # --- транспорт с повторами ---
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {"iss.meta": "off", **params}
        wait = _down_until.get(self.base_url, 0) - time.monotonic()
        if wait > 0:
            raise DataSourceError(f"ISS недоступен (сеть не пускала к бирже, повторю попытку через {int(wait // 60) + 1} мин)",
                                  retryable=True)
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self._client.get(f"{self.base_url}{path}", params=params)
                if r.status_code >= 500:
                    raise DataSourceError(f"ISS ответил {r.status_code}", retryable=True)
                if r.status_code >= 400:
                    raise DataSourceError(f"ISS ответил {r.status_code} на {path}", retryable=False)
                data = r.json()
                _down_until.pop(self.base_url, None)
                return data
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = DataSourceError(f"ISS недоступен: {e.__class__.__name__}", retryable=True)
                last_err.network = True  # type: ignore[attr-defined]
            except DataSourceError as e:
                if not e.retryable:
                    raise
                last_err = e
            except ValueError as e:  # битый JSON
                last_err = DataSourceError(f"ISS вернул некорректный JSON: {e}", retryable=True)
            time.sleep(min(0.5 * 2 ** (attempt - 1), 4))
        assert last_err is not None
        if getattr(last_err, "network", False):
            _down_until[self.base_url] = time.monotonic() + BREAKER_SECONDS
        raise last_err

    # --- свечи ---
    def get_candles(self, ticker: str, board: str, start: date, end: date) -> pd.DataFrame:
        path = f"/engines/stock/markets/shares/boards/{board}/securities/{ticker}/candles.json"
        frames, offset = [], 0
        while True:
            payload = self._get(path, {"from": start.isoformat(), "till": end.isoformat(),
                                       "interval": 24, "start": offset})
            df = _block(payload, "candles")
            if df.empty:
                break
            frames.append(df)
            offset += len(df)
            if len(df) < PAGE:
                break
        if not frames:
            raise NoDataError(f"ISS не вернул свечей {ticker}@{board} за {start}..{end}")
        return candles_to_ohlcv(pd.concat(frames, ignore_index=True))

    # --- поиск инструментов ---
    def search_securities(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        payload = self._get("/securities.json", {
            "q": query, "engine": "stock", "market": "shares", "limit": limit,
            "securities.columns": "secid,shortname,name,isin,is_traded,primary_boardid"})
        df = _block(payload, "securities")
        return df.to_dict("records")

    def security_boards(self, ticker: str) -> list[str]:
        payload = self._get(f"/securities/{ticker}.json", {"iss.only": "boards"})
        df = _block(payload, "boards")
        if df.empty:
            raise NoDataError(f"Инструмент {ticker} не найден на MOEX")
        df = df[(df.get("engine") == "stock") & (df.get("market") == "shares")] if "engine" in df else df
        return df["boardid"].tolist() if "boardid" in df else []

    # --- новости биржи (архив с пагинацией) ---
    def site_news(self, start: int = 0) -> pd.DataFrame:
        payload = self._get("/sitenews.json", {"start": start})
        return _block(payload, "sitenews")

    def close(self) -> None:
        self._client.close()


def candles_to_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Приводит свечи ISS к Open/High/Low/Close/Volume с индексом-датой."""
    need = {"open", "close", "high", "low", "volume", "begin"}
    missing = need - set(df.columns)
    if missing:
        raise DataSourceError(f"В ответе ISS нет колонок: {sorted(missing)}")
    out = pd.DataFrame({
        "Open": pd.to_numeric(df["open"], errors="coerce"),
        "High": pd.to_numeric(df["high"], errors="coerce"),
        "Low": pd.to_numeric(df["low"], errors="coerce"),
        "Close": pd.to_numeric(df["close"], errors="coerce"),
        "Volume": pd.to_numeric(df["volume"], errors="coerce"),
    })
    out.index = pd.to_datetime(df["begin"]).dt.normalize()
    out.index.name = "Date"
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out
