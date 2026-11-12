"""Проверка целостности временного ряда до бэктеста."""
from __future__ import annotations

import pandas as pd


class DataQualityError(Exception):
    pass


def check_ohlcv(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Возвращает очищенный ряд и список предупреждений. Критичные проблемы: исключение."""
    warnings: list[str] = []
    if df is None or df.empty:
        raise DataQualityError("пустой ряд котировок")

    need = ["Open", "High", "Low", "Close"]
    if df[need].isna().any().any():
        n = int(df[need].isna().any(axis=1).sum())
        df = df.dropna(subset=need)
        warnings.append(f"удалено {n} строк с пропусками цен")
    if (df[need] <= 0).any().any():
        n = int((df[need] <= 0).any(axis=1).sum())
        df = df[(df[need] > 0).all(axis=1)]
        warnings.append(f"удалено {n} строк с неположительными ценами")
    if df.index.duplicated().any():
        df = df[~df.index.duplicated(keep="last")]
        warnings.append("найдены дубли дат, оставлена последняя запись")
    df = df.sort_index()

    bad_ohlc = (df["High"] < df[["Open", "Close"]].max(axis=1) - 1e-9) | \
               (df["Low"] > df[["Open", "Close"]].min(axis=1) + 1e-9)
    if bad_ohlc.any():
        warnings.append(f"{int(bad_ohlc.sum())} свечей с High/Low вне Open/Close, исправлено")
        df = df.copy()
        df["High"] = df[["High", "Open", "Close"]].max(axis=1)
        df["Low"] = df[["Low", "Open", "Close"]].min(axis=1)

    if "Volume" in df and (df["Volume"] == 0).any():
        warnings.append(f"{int((df['Volume'] == 0).sum())} дней с нулевым объёмом")

    gaps = df.index.to_series().diff().dt.days
    big = gaps[gaps > 7]
    for d, g in big.items():
        warnings.append(f"разрыв {int(g)} календарных дней до {d.date()}: возможна приостановка торгов")

    jumps = df["Close"].pct_change().abs()
    for d, j in jumps[jumps > 0.25].items():
        warnings.append(f"скачок цены {j:.0%} на {d.date()}: проверьте сплит или корпоративное действие")

    if len(df) < 30:
        raise DataQualityError(f"слишком мало торговых дней: {len(df)}")
    return df, warnings
