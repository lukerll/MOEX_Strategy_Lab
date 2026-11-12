"""Выгрузка результатов: CSV сделок и кривой капитала, JSON, файл отчёта на диск."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from ..domain.schemas import BacktestResult
from .report_builder import build_html


_REASON_RU = {"signal": "сигнал", "stop_loss": "стоп-лосс", "take_profit": "тейк-профит", "trailing": "трейлинг-стоп",
              "max_hold": "срок удержания", "atr_stop": "стоп по ATR", "end": "конец периода"}


def _n(v: float | None, digits: int = 2) -> str:
    """Число с запятой: русский Excel читает точку как дату или текст."""
    return "" if v is None else f"{v:.{digits}f}".replace(".", ",")


def trades_csv(res: BacktestResult) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата покупки", "Дата продажи", "Акций", "Цена покупки, ₽", "Цена продажи, ₽", "Результат, ₽",
                "Результат, %", "Комиссия, ₽", "Дней в позиции", "Причина выхода"])
    for t in res.trades:
        reason = "позиция открыта" if t.is_open else _REASON_RU.get(t.exit_reason or "", t.exit_reason or "")
        w.writerow([t.entry_date, t.exit_date or "", t.size, _n(t.entry_price, 4), _n(t.exit_price, 4), _n(t.pnl),
                    _n(t.return_pct), _n(t.commission), t.holding_days, reason])
    return buf.getvalue()


def equity_csv(res: BacktestResult) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Цена закрытия, ₽", "Акций", "Капитал стратегии, ₽", "Капитал «купить и держать», ₽",
                "Просадка стратегии, %", "Просадка «купить и держать», %"])
    for r in res.equity:
        w.writerow([r["date"], _n(r["close"], 4), r["position"], _n(r["equity"]), _n(r["benchmark"]),
                    _n(r["drawdown"], 3), _n(r["benchmark_drawdown"], 3)])
    return buf.getvalue()


def save_report(res: BacktestResult, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{res.run_id}_{res.spec.ticker}.html"
    path.write_text(build_html(res), encoding="utf-8")
    return path
