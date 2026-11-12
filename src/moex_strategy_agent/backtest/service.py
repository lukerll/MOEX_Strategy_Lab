"""Детерминированный конвейер расчёта: данные -> бэктест -> эталон -> сверка -> диагностика.

Агент вызывает эти функции как инструменты, но сам их порядок внутри не меняет.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pandas as pd

from ..analysis.diagnostics import diagnose
from ..analysis.news_matching import find_price_events, match_news
from ..analysis.sanity import entry_check
from ..config import Settings
from ..data.market_data import MarketData
from ..data.news_client import NewsService
from ..domain.schemas import BacktestResult, DataInfo, StrategySpec, StrategyType
from ..domain.rules import describe_rules, template_rules
from ..domain.validators import warmup_bars
from .engine import run_engine
from .metrics import compute_metrics, drawdown_series
from .reconciliation import reconcile
from .reference_engine import run_reference, rows_from_df


@dataclass
class LoadedData:
    df: pd.DataFrame
    info: DataInfo


def load_data(market: MarketData, spec: StrategySpec) -> LoadedData:
    df, info = market.history(spec.ticker, spec.board, spec.start_date, spec.end_date,
                              warmup_days=warmup_bars(spec))
    df = df[df.index <= pd.Timestamp(spec.end_date)]
    return LoadedData(df, info)


def run_backtest(spec: StrategySpec, data: LoadedData, settings: Settings,
                 assumptions: list[str] | None = None) -> BacktestResult:
    df = data.df
    main = run_engine(df, spec)
    bh_spec = spec.model_copy(update={"strategy_type": StrategyType.BUY_AND_HOLD, "parameters": {},
                                     "rules": template_rules("BUY_AND_HOLD", {})})
    bench = run_engine(df, bh_spec)

    rows = rows_from_df(df)
    ref_kwargs = {}
    if settings.get("debug.inject_reference_bug"):
        ref_kwargs["commission"] = spec.commission * 3 + 0.001   # намеренная ошибка для eval
    ref = run_reference(rows, spec, **ref_kwargs)
    verification = reconcile(main, ref, settings.get("backtest.reconciliation_rel_tol", 1e-6))

    metrics = compute_metrics(main.equity, main.trades, spec.initial_cash, main.position)
    bmetrics = compute_metrics(bench.equity, bench.trades, spec.initial_cash, bench.position)

    pdf = df[df.index >= pd.Timestamp(spec.start_date)]
    period_close = pdf["Close"]
    diag = diagnose(spec=spec, equity=main.equity, bench=bench.equity, close=df["Close"],
                    trades=main.trades, rows=rows, total_return_pct=metrics.total_return_pct,
                    with_sensitivity=settings.get("backtest.sensitivity_grid", True))

    dd = drawdown_series(main.equity)
    bdd = drawdown_series(bench.equity)
    equity_rows = [{
        "date": d.date().isoformat(), "equity": round(float(e), 2), "benchmark": round(float(b), 2),
        "drawdown": round(float(x) * 100, 3), "benchmark_drawdown": round(float(y) * 100, 3),
        "open": round(float(o), 4), "high": round(float(h), 4), "low": round(float(lo), 4),
        "close": round(float(c), 4), "volume": int(v), "position": int(p)}
        for d, e, b, x, y, o, h, lo, c, v, p in zip(main.equity.index, main.equity, bench.equity, dd, bdd,
                                                     pdf["Open"], pdf["High"], pdf["Low"], period_close,
                                                     pdf["Volume"], main.position)]
    indicators = {k: [None if pd.isna(v) else round(float(v), 4) for v in s]
                  for k, s in main.indicators.items()}
    understood = describe_rules(spec.rules)
    diag["indicators"] = indicators
    if not main.trades:
        check = entry_check(rows, spec)
        if check:
            diag["entry_check"] = check

    return BacktestResult(
        run_id=uuid.uuid4().hex[:10], spec=spec, data=data.info, metrics=metrics, benchmark=bmetrics,
        trades=main.trades, equity=equity_rows,
        signals={"entries": [d.date().isoformat() for d, v in main.entries.items() if v],
                 "exits": [d.date().isoformat() for d, v in main.exits.items() if v]},
        verification=verification, diagnostics=diag, assumptions=list(assumptions or []),
        understood=understood,
    )


def attach_news(result: BacktestResult, data: LoadedData, settings: Settings,
                news: NewsService) -> BacktestResult:
    close = data.df["Close"]
    events = find_price_events(close[close.index >= pd.Timestamp(result.spec.start_date)],
                               settings.get("news.move_threshold_pct", 4.0),
                               settings.get("news.move_sigma", 3.0),
                               settings.get("news.max_events", 8))
    if not events:
        result.events, result.news_status = [], "аномальных движений цены не найдено"
        return result
    raw = news.fetch(min(e.date for e in events) - pd.Timedelta(days=5).to_pytimedelta(),
                     max(e.date for e in events) + pd.Timedelta(days=2).to_pytimedelta(),
                     ticker=result.spec.ticker, event_dates=[e.date for e in events])
    result.events = match_news(events, raw, result.spec.ticker,
                               settings.get("news.window_days_before", 2),
                               settings.get("news.window_days_after", 1))
    found = sum(1 for e in result.events if e.news)
    flagged = sum(1 for e in result.events for n in e.news if n.flagged_injection)
    parts = [f"аномальных дней: {len(events)}, с новостями рядом: {found}"]
    if flagged:
        parts.append(f"заблокировано подозрительных новостей: {flagged}")
    cov = getattr(news, "coverage", {}) or {}
    if cov:
        parts.append("источники: " + ", ".join(f"{k}: {v}" for k, v in cov.items()))
    else:
        parts += news.status
    result.news_status = "; ".join(parts)
    return result
