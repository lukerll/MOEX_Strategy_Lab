"""Прямой запуск бэктеста по спецификации (без LLM) и работа с запусками."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...agent.guardrails import Budget
from ...agent.tools import ToolContext, ToolRunner
from ...domain.schemas import StrategySpec
from ...domain.validators import validate_spec
from ...telemetry.tracing import Tracer
from ..deps import Services, get_run_or_404, services

router = APIRouter(prefix="/api", tags=["Бэктесты и избранное"])


def _runner(svc: Services, session_id: str) -> tuple[ToolRunner, Tracer]:
    tracer = Tracer(svc.repo, session_id, svc.settings.path("app.logs_dir"))
    budget = Budget(max_steps=5, max_tool_calls=4, max_llm_calls=0, max_tokens=0, max_cost_rub=0)
    ctx = ToolContext(svc.settings, svc.agent.market, svc.agent.news, svc.repo, session_id)
    return ToolRunner(ctx, tracer, budget), tracer


@router.post("/backtests", summary="Бэктест без агента",
             description="Считает стратегию по готовому описанию в JSON, модель не участвует. Удобно для проверки движков и повторяемых расчётов. Ответ: результат, сверка двух движков и новости рядом с резкими движениями.")
def create_backtest(spec: StrategySpec, session_id: str | None = None, with_news: bool = True,
                    svc: Services = Depends(services)) -> dict:
    rep = validate_spec(spec, min_days=svc.settings.get("data.min_period_days", 60),
                        max_years=svc.settings.get("data.max_period_years", 15))
    if not rep.ok:
        raise HTTPException(422, {"errors": rep.errors})
    sid = svc.repo.ensure_session(session_id)
    runner, tracer = _runner(svc, sid)
    r = runner.call("run_backtest", {"spec": rep.spec.model_dump(mode="json"), "assumptions": rep.assumptions})
    if not r.ok:
        raise HTTPException(502 if r.retryable else 422, r.error)
    if with_news:
        runner.call("find_news", {"run_id": r.data["run_id"]})
    res = svc.repo.get_run(r.data["run_id"])
    from ...analysis.explanations import template_narrative
    res.narrative = template_narrative(res)
    svc.repo.save_run(res, sid)
    return {"session_id": sid, "trace_id": tracer.trace_id, "result": res.model_dump(mode="json")}


@router.get("/runs", summary="Последние запуски",
             description="Список запусков, новые сверху. С `session_id` только запуски этой сессии.")
def list_runs(limit: int = 20, session_id: str | None = None, svc: Services = Depends(services)) -> dict:
    return {"runs": svc.repo.list_runs(min(limit, 100), session_id)}


@router.get("/runs/{run_id}", summary="Запуск целиком",
             description="Всё по одному запуску: стратегия, метрики, сравнение с держанием, сделки, кривая капитала, сверка движков и диагностика.")
def get_run(run_id: str, svc: Services = Depends(services)) -> dict:
    return get_run_or_404(svc, run_id).model_dump(mode="json")


class SaveIn(BaseModel):
    session_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]+$")
    title: str | None = Field(None, max_length=120)


@router.post("/runs/{run_id}/save", summary="Сохранить в избранное",
             description="Ничего не сохраняет сразу: создаёт действие и возвращает `action_id`. Сохранится после «Подтвердить действие».")
def request_save(run_id: str, body: SaveIn, svc: Services = Depends(services)) -> dict:
    """Не сохраняет, а создаёт действие, ждущее подтверждения человека."""
    res = get_run_or_404(svc, run_id)
    svc.repo.ensure_session(body.session_id)        # кнопка «В избранное» работает и без начатого диалога
    runner, tracer = _runner(svc, body.session_id)
    r = runner.call("save_research", {"run_id": run_id, "title": body.title or res.spec.title()})
    if not r.awaiting_confirmation:
        raise HTTPException(422, r.error or "сохранить не получилось")
    p = svc.repo.get_pending(r.action_id)
    return {"action_id": r.action_id, "description": p["description"], "trace_id": tracer.trace_id}


@router.post("/runs/{run_id}/delete", summary="Удалить запуск",
             description="Создаёт действие на удаление. Удалится после «Подтвердить действие».")
def request_run_delete(run_id: str, session_id: str = Query(..., pattern=r"^[A-Za-z0-9_-]{1,40}$"), svc: Services = Depends(services)) -> dict:
    """Не удаляет, а создаёт действие, ждущее подтверждения человека."""
    get_run_or_404(svc, run_id)
    svc.repo.ensure_session(session_id)
    runner, tracer = _runner(svc, session_id)
    r = runner.call("delete_run", {"run_id": run_id})
    if not r.awaiting_confirmation:
        raise HTTPException(422, r.error)
    p = svc.repo.get_pending(r.action_id)
    return {"action_id": r.action_id, "description": p["description"], "trace_id": tracer.trace_id}


@router.get("/research", summary="Избранное",
             description="Сохранённые исследования с названиями.")
def research(svc: Services = Depends(services)) -> dict:
    return {"research": svc.repo.list_research()}


@router.post("/research/{research_id}/delete", summary="Удалить из избранного",
             description="Создаёт действие на удаление. Удалится после «Подтвердить действие».")
def request_delete(research_id: str, session_id: str = Query(..., pattern=r"^[A-Za-z0-9_-]{1,40}$"), svc: Services = Depends(services)) -> dict:
    svc.repo.ensure_session(session_id)
    runner, tracer = _runner(svc, session_id)
    r = runner.call("delete_research", {"research_id": research_id})
    if not r.awaiting_confirmation:
        raise HTTPException(422, r.error)
    p = svc.repo.get_pending(r.action_id)
    return {"action_id": r.action_id, "description": p["description"], "trace_id": tracer.trace_id}


@router.post("/history/clear", summary="Очистить историю",
             description="Создаёт действие на очистку истории запусков сессии, избранное остаётся. Очистится после «Подтвердить действие».")
def request_clear_history(session_id: str = Query(..., pattern=r"^[A-Za-z0-9_-]{1,40}$"), svc: Services = Depends(services)) -> dict:
    """Не очищает, а создаёт действие, ждущее подтверждения человека. Избранное остаётся."""
    svc.repo.ensure_session(session_id)
    runner, tracer = _runner(svc, session_id)
    r = runner.call("clear_history", {})
    if not r.awaiting_confirmation:
        raise HTTPException(422, r.error)
    p = svc.repo.get_pending(r.action_id)
    return {"action_id": r.action_id, "description": p["description"], "trace_id": tracer.trace_id}
