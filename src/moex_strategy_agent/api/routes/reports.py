"""Отчёт по запуску: HTML, CSV сделок и кривой капитала, JSON."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response

from ...reporting.export import equity_csv, save_report, trades_csv
from ...reporting.report_builder import build_html
from .chat import _chat_file
from ..deps import Services, get_run_or_404, services

router = APIRouter(prefix="/api/runs", tags=["Отчёты"])


@router.get("/{run_id}/report.html", response_class=HTMLResponse, summary="Отчёт HTML",
             description="Отчёт по запуску одной страницей. С `download=true` скачивается файлом.")
def report_html(run_id: str, download: bool = False, svc: Services = Depends(services)) -> HTMLResponse:
    res = get_run_or_404(svc, run_id)
    save_report(res, svc.settings.path("app.reports_dir"))
    headers = {"Content-Disposition": f'attachment; filename="{run_id}_{res.spec.ticker}.html"'} if download else {}
    return HTMLResponse(build_html(res), headers=headers)


@router.get("/{run_id}/trades.csv", summary="Сделки CSV",
             description="Сделки запуска для Excel: русские заголовки, числа с запятой.")
def report_trades(run_id: str, svc: Services = Depends(services)) -> Response:
    res = get_run_or_404(svc, run_id)
    return Response("﻿" + trades_csv(res), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}_trades.csv"'})


@router.get("/{run_id}/equity.csv", summary="Капитал по дням CSV",
             description="Кривая капитала стратегии и держания по дням для Excel.")
def report_equity(run_id: str, svc: Services = Depends(services)) -> Response:
    res = get_run_or_404(svc, run_id)
    return Response("﻿" + equity_csv(res), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}_equity.csv"'})


@router.get("/{run_id}/chat.md", summary="Выгрузка диалога запуска",
            description="Диалог, в котором получен запуск, в Markdown: реплики, намерения, итоги запусков и шаги агента.")
def report_chat(run_id: str, svc: Services = Depends(services)) -> Response:
    get_run_or_404(svc, run_id)
    sid = svc.repo.run_session(run_id)
    if not sid:
        raise HTTPException(404, "у запуска нет диалога: он сделан напрямую через API")
    return _chat_file(svc, sid)
