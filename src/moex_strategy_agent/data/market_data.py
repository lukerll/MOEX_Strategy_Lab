"""Сервис рыночных данных: источник (ISS или демо) + кеш + проверка качества."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from ..config import Settings
from ..domain.schemas import DataInfo
from .cache import CandleCache
from .demo_source import DEMO_UNIVERSE, demo_name, demo_ohlcv
from .moex_client import DataSourceError, MoexClient, NoDataError
from .quality import check_ohlcv

# Популярные названия -> тикер. Используются и разборщиком, и поиском.
KNOWN_NAMES = {
    "сбер": "SBER", "сбербанк": "SBER", "газпром": "GAZP", "лукойл": "LKOH", "яндекс": "YDEX",
    "втб": "VTBR", "роснефть": "ROSN", "норникель": "GMKN", "норильский никель": "GMKN",
    "новатэк": "NVTK", "магнит": "MGNT", "татнефть": "TATN", "мтс": "MTSS",
    "мосбиржа": "MOEX", "московская биржа": "MOEX", "аэрофлот": "AFLT", "полюс": "PLZL",
    "алроса": "ALRS", "северсталь": "CHMF", "нлмк": "NLMK", "сургутнефтегаз": "SNGS",
    "интер рао": "IRAO", "русгидро": "HYDR", "фосагро": "PHOR", "x5": "X5", "озон": "OZON",
}


class MarketData:
    def __init__(self, settings: Settings, client: MoexClient | None = None):
        self.settings = settings
        self.mode = settings.get("data.mode", "moex")
        self.cache = CandleCache(settings.path("data.cache_dir"))
        self._client = client

    @property
    def client(self) -> MoexClient:
        if self._client is None:
            self._client = MoexClient(timeout_s=self.settings.get("data.request_timeout_s", 15),
                                      max_retries=self.settings.get("data.max_retries", 3))
        return self._client

    def _fault(self) -> None:
        """Инъекция сбоев для eval-сценариев устойчивости."""
        fault = self.settings.get("data.fault")
        if fault == "timeout":
            raise DataSourceError("ISS недоступен: ReadTimeout (инъекция сбоя)", retryable=True)
        if fault == "empty":
            raise NoDataError("ISS вернул пустой ответ (инъекция сбоя)")

    def history(self, ticker: str, board: str, start: date, end: date,
                warmup_days: int = 0) -> tuple[pd.DataFrame, DataInfo]:
        """Котировки за [start - warmup, end]. Возвращает ряд и описание источника."""
        fetch_start = start - timedelta(days=int(warmup_days * 1.6) + 10)  # торговые дни -> календарные
        end = min(end, date.today())
        source = self.mode
        warnings: list[str] = []
        self._fault()

        if self.mode == "demo":
            df = demo_ohlcv(ticker)
            df = df[(df.index >= pd.Timestamp(fetch_start)) & (df.index <= pd.Timestamp(end))]
        else:
            df = self._from_moex(ticker, board, fetch_start, end, warnings)
            if warnings and warnings[0].startswith("ISS недоступен"):
                source = "cache"

        # незавершённая дневная свеча текущего дня в расчёт не идёт
        df = df[df.index < pd.Timestamp(date.today())] if self.mode != "demo" else df
        if df.empty or df.index.max() < pd.Timestamp(start):
            raise NoDataError(f"Нет котировок {ticker} за выбранный период")
        df, q_warn = check_ohlcv(df)
        warnings += q_warn
        period = df[df.index >= pd.Timestamp(start)]
        if period.empty:
            raise NoDataError(f"Нет котировок {ticker} внутри периода {start}..{end}")
        if period.index.min() > pd.Timestamp(start) + timedelta(days=10):
            warnings.append(f"история начинается только с {period.index.min().date()}")
        info = DataInfo(source=source, ticker=ticker, board=board, rows=len(period),
                        first_date=period.index.min().date(), last_date=period.index.max().date(),
                        is_synthetic=self.mode == "demo", warnings=warnings)
        return df, info

    def _from_moex(self, ticker: str, board: str, start: date, end: date,
                   warnings: list[str]) -> pd.DataFrame:
        if self.cache.covers(ticker, board, start, end):
            return self.cache.load(ticker, board, start, end)
        try:
            df = self.client.get_candles(ticker, board, start, end)
            self.cache.save(ticker, board, df, start, end)
            return df
        except DataSourceError as e:
            cached = self.cache.load(ticker, board, start, end)
            if e.retryable and cached is not None and not cached.empty:
                warnings.insert(0, f"ISS недоступен ({e}); использован кеш до {cached.index.max().date()}")
                return cached
            raise

    def search(self, query: str) -> list[dict]:
        q = query.strip().lower()
        if q.upper() in DEMO_UNIVERSE or q in KNOWN_NAMES:
            t = KNOWN_NAMES.get(q, q.upper())
            return [{"secid": t, "shortname": demo_name(t) or t, "primary_boardid": "TQBR"}]
        if self.mode == "demo":
            return [{"secid": t, "shortname": v[3], "primary_boardid": "TQBR"}
                    for t, v in DEMO_UNIVERSE.items() if q in v[3].lower()]
        rows = self.client.search_securities(query)
        return [r for r in rows if r.get("is_traded") in (1, True, None)]
