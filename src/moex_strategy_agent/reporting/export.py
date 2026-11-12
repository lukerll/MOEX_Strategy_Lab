"""Выгрузка результатов: CSV сделок и кривой капитала, JSON, файл отчёта на диск."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from ..domain.schemas import BacktestResult
from .report_builder import build_html


def trades_csv(res: BacktestResult) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["entry_date", "exit_date", "size", "entry_price", "exit_price", "pnl", "return_pct",
                "commission", "holding_days", "is_open", "exit_reason"])
    for t in res.trades:
        w.writerow([t.entry_date, t.exit_date or "", t.size, f"{t.entry_price:.4f}",
                    "" if t.exit_price is None else f"{t.exit_price:.4f}", f"{t.pnl:.2f}",
                    f"{t.return_pct:.4f}", f"{t.commission:.2f}", t.holding_days, int(t.is_open), t.exit_reason or ""])
    return buf.getvalue()


def equity_csv(res: BacktestResult) -> str:
    buf = io.StringIO()
    keys = ["date", "close", "position", "equity", "benchmark", "drawdown", "benchmark_drawdown"]
    w = csv.DictWriter(buf, fieldnames=keys, delimiter=";", extrasaction="ignore")
    w.writeheader()
    w.writerows(res.equity)
    return buf.getvalue()


def save_report(res: BacktestResult, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{res.run_id}_{res.spec.ticker}.html"
    path.write_text(build_html(res), encoding="utf-8")
    return path
