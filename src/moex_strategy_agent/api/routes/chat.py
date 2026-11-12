"""Диалог с агентом и подтверждение опасных действий человеком."""
from __future__ import annotations

import json
import queue
import threading
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...agent.scope import scope_hint
from ..deps import Services, services

router = APIRouter(prefix="/api", tags=["chat"])


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str | None = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    initial_cash: float | None = Field(None, ge=1000, le=1_000_000_000)   # капитал по умолчанию из настроек


@router.get("/scope")
def scope(text: str = Query("", max_length=4000)) -> dict:
    """Подсказка при наборе: то, что агент точно не поддерживает, видно до отправки."""
    return {"hint": scope_hint(text)}


@router.post("/chat")
def chat(body: ChatIn, svc: Services = Depends(services)) -> dict:
    return svc.agent.handle(body.session_id, body.message, default_cash=body.initial_cash).to_dict()


@router.post("/chat/stream")
def chat_stream(body: ChatIn, svc: Services = Depends(services)) -> StreamingResponse:
    """Тот же агент, но этапы приходят по мере реального начала: {"type":"stage"}... затем {"type":"reply"}.

    Интерфейс показывает «Получаю котировки», когда агент действительно за ними пошёл,
    а не по таймеру. Формат: NDJSON, одна строка на событие.
    """
    q: queue.Queue = queue.Queue()

    def listener(kind: str, name: str, payload: dict) -> None:
        if kind == "stage":
            q.put({"type": "stage", "stage": name})

    def work() -> None:
        try:
            q.put({"type": "reply", "reply": svc.agent.handle(body.session_id, body.message, on_event=listener, default_cash=body.initial_cash).to_dict()})
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "error": f"{e.__class__.__name__}: {e}"})
        q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def gen() -> Iterator[bytes]:
        while True:
            item = q.get()
            if item is None:
                return
            yield (json.dumps(item, ensure_ascii=False, default=str) + "\n").encode("utf-8")

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/sessions/{session_id}/messages")
def messages(session_id: str, svc: Services = Depends(services)) -> dict:
    return {"session_id": session_id, "messages": svc.repo.messages(session_id),
            "pending": svc.repo.open_pending(session_id), "state": svc.repo.get_state(session_id)}


@router.post("/actions/{action_id}/confirm")
def confirm(action_id: str, svc: Services = Depends(services)) -> dict:
    out = svc.agent.confirm(action_id, approve=True)
    if not out["ok"] and "нет" in out["text"]:
        raise HTTPException(404, out["text"])
    return out


@router.post("/actions/{action_id}/reject")
def reject(action_id: str, svc: Services = Depends(services)) -> dict:
    return svc.agent.confirm(action_id, approve=False)


@router.get("/traces/{trace_id}")
def trace(trace_id: str, svc: Services = Depends(services)) -> dict:
    events = svc.repo.get_trace(trace_id)
    if not events:
        raise HTTPException(404, "трасса не найдена")
    return {"trace_id": trace_id, "events": events,
            "tokens": sum(e["tokens"] for e in events), "cost_rub": round(sum(e["cost_rub"] for e in events), 4),
            "latency_ms": sum(e["latency_ms"] for e in events if e["kind"] in ("llm", "tool"))}
