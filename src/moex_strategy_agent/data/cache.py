"""Parquet-кеш котировок: один файл на тикер и режим торгов + метаданные покрытия."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd


class CandleCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, ticker: str, board: str) -> tuple[Path, Path]:
        base = self.root / f"{ticker.upper()}_{board.upper()}"
        return base.with_suffix(".parquet"), base.with_suffix(".json")

    def coverage(self, ticker: str, board: str) -> tuple[date, date] | None:
        _, meta = self._paths(ticker, board)
        if not meta.exists():
            return None
        m = json.loads(meta.read_text(encoding="utf-8"))
        return date.fromisoformat(m["from"]), date.fromisoformat(m["till"])

    def covers(self, ticker: str, board: str, start: date, end: date) -> bool:
        cov = self.coverage(ticker, board)
        return bool(cov and cov[0] <= start and cov[1] >= end)

    def load(self, ticker: str, board: str, start: date | None = None,
             end: date | None = None) -> pd.DataFrame | None:
        data, _ = self._paths(ticker, board)
        if not data.exists():
            return None
        df = pd.read_parquet(data)
        if start:
            df = df[df.index >= pd.Timestamp(start)]
        if end:
            df = df[df.index <= pd.Timestamp(end)]
        return df

    def save(self, ticker: str, board: str, df: pd.DataFrame, start: date, end: date) -> None:
        data, meta = self._paths(ticker, board)
        cov = self.coverage(ticker, board)
        old = self.load(ticker, board)
        if old is not None and cov and start <= cov[1] and end >= cov[0]:
            # пересекающиеся периоды склеиваем, покрытие расширяем
            df = pd.concat([old, df])
            df = df[~df.index.duplicated(keep="last")].sort_index()
            start, end = min(start, cov[0]), max(end, cov[1])
        df.to_parquet(data)
        meta.write_text(json.dumps({"from": start.isoformat(), "till": end.isoformat(),
                                    "fetched_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")}),
                        encoding="utf-8")
