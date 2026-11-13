"""Диалог с агентом и подтверждение опасных действий человеком."""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Iterator, Literal

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...agent.brain import LLMGateway
from ...agent.graph import MODEL_UNAVAILABLE_BY_KIND
from ...agent.scope import scope_hint
from ...llm.provider import LLMError, _net_kind, build_client
from ..deps import Services, services

router = APIRouter(prefix="/api", tags=["Диалог с агентом"])


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000, examples=["Сбербанк за 2024 год, SMA 20/50"],
                         description="Стратегия или вопрос обычным языком")
    session_id: str | None = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_-]+$", examples=["demo"],
                                   description="Любая строка, общая для реплик одного диалога")
    initial_cash: float | None = Field(None, ge=1000, le=1_000_000_000, examples=[100000],
                                       description="Капитал в рублях, если не назван в сообщении")
    style: Literal["full", "short"] = Field("full", description="Длина объяснения: full подробно, short коротко")


@router.get("/scope", summary="Подсказка при наборе",
             description="Проверяет текст до отправки, без модели. Если в запросе то, чего агент не умеет (шорт, внутридневные свечи, иностранные акции), вернёт короткую подсказку, иначе `hint: null`.")
def scope(text: str = Query("", max_length=4000)) -> dict:
    """Подсказка при наборе: то, что агент точно не поддерживает, видно до отправки."""
    return {"hint": scope_hint(text)}


@router.post("/chat", summary="Сообщение агенту",
             description="Главный вход. Текст на русском, ответ целиком: что агент понял, результат бэктеста, объяснение и готовые подсказки. `session_id` связывает реплики в один диалог, без него начнётся новый.")
def chat(body: ChatIn, svc: Services = Depends(services)) -> dict:
    return svc.agent.handle(body.session_id, body.message, default_cash=body.initial_cash, style=body.style).to_dict()


@router.post("/chat/stream", summary="Сообщение агенту по шагам",
             description="То же, что «Сообщение агенту», но ответ приходит строками NDJSON: сначала этапы по мере их реального начала (`stage`), в конце весь ответ (`reply`). Так интерфейс показывает ход анализа.")
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
            q.put({"type": "reply", "reply": svc.agent.handle(body.session_id, body.message, on_event=listener, default_cash=body.initial_cash, style=body.style).to_dict()})
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


@router.get("/sessions/{session_id}/messages", summary="История диалога",
             description="Все реплики сессии, действия, которые ждут подтверждения, и черновик стратегии, если уточнение не закончено.")
def messages(session_id: str, svc: Services = Depends(services)) -> dict:
    return {"session_id": session_id, "messages": svc.repo.messages(session_id),
            "pending": svc.repo.open_pending(session_id), "state": svc.repo.get_state(session_id)}


@router.post("/actions/{action_id}/confirm", summary="Подтвердить действие",
             description="Выполняет сохранение или удаление, которое агент подготовил и отложил до «Да» человека. Без этого вызова агент ничего не сохраняет и не удаляет.")
def confirm(action_id: str, svc: Services = Depends(services)) -> dict:
    out = svc.agent.confirm(action_id, approve=True)
    if not out["ok"] and "нет" in out["text"]:
        raise HTTPException(404, out["text"])
    return out


@router.post("/actions/{action_id}/reject", summary="Отклонить действие",
             description="Отменяет отложенное сохранение или удаление. Ничего не меняется.")
def reject(action_id: str, svc: Services = Depends(services)) -> dict:
    return svc.agent.confirm(action_id, approve=False)


@router.get("/traces/{trace_id}", summary="Трасса запроса",
             description="Каждый шаг агента: что понял, какие инструменты вызвал, сколько токенов, рублей и миллисекунд потратил. По ней видно, где модель, а где код.")
def trace(trace_id: str, svc: Services = Depends(services)) -> dict:
    events = svc.repo.get_trace(trace_id)
    if not events:
        raise HTTPException(404, "трасса не найдена")
    return {"trace_id": trace_id, "events": events,
            "tokens": sum(e["tokens"] for e in events), "cost_rub": round(sum(e["cost_rub"] for e in events), 4),
            "latency_ms": sum(e["latency_ms"] for e in events if e["kind"] in ("llm", "tool"))}


@router.get("/usage", summary="Расход",
             description="Токены и рубли сегодня и за всё время, модель и лимиты на один запрос. Остаток у GigaChat отдаёт «Проверка связи с моделью».")
def usage(svc: Services = Depends(services)) -> dict:
    """Расход токенов и рублей по трассе: сегодня и за всё время. Остаток у провайдера отдаёт /llm/check."""
    s = svc.settings
    conf = s.provider_conf(s.active_provider)
    return {**svc.repo.usage(), "provider": s.active_provider, "model": conf.get("model"),
            "price_rub_per_1k": conf.get("price_rub_per_1k_tokens", 0.0), "limits": s.get("agent")}


@router.post("/llm/check", summary="Проверка связи с моделью",
             description="Авторизуется у провайдера и запрашивает список моделей, токены не тратит. Вернёт время ответа и остаток, если провайдер его отдаёт.")
def llm_check(svc: Services = Depends(services)) -> dict:
    """Проверка связи с моделью без траты токенов: авторизация и список моделей, затем остаток, если он есть."""
    s = svc.settings
    name = s.active_provider
    conf = s.provider_conf(name)
    started = time.monotonic()

    def fail(kind: str) -> dict:
        return {"ok": False, "kind": kind, "model": conf.get("model"),
                "text": MODEL_UNAVAILABLE_BY_KIND.get(kind, MODEL_UNAVAILABLE_BY_KIND["other"])}

    try:
        client = LLMGateway._clients.get(name) or build_client(name, conf)
        if client is None:
            return fail("no_key")
        LLMGateway._clients[name] = client
        models = client.list_models()
    except LLMError as e:
        return fail(e.kind)
    except httpx.HTTPError as e:
        return fail(_net_kind(e))
    latency = int((time.monotonic() - started) * 1000)
    try:
        balance = client.balance()
    except (LLMError, httpx.HTTPError, ValueError):
        balance = None
    mine = next((b["tokens"] for b in balance or [] if b["model"].lower() == str(conf.get("model", "")).lower()), None)
    return {"ok": True, "latency_ms": latency, "model": conf.get("model"),
            "model_listed": conf.get("model") in models, "balance": balance, "balance_tokens": mine}


class ModelIn(BaseModel):
    model: str = Field(..., min_length=1, max_length=80, examples=["GigaChat-2-Max"])


@router.get("/account", summary="Аккаунт GigaChat",
            description="Тип доступа, текущая модель, доступные модели с ценой за 1 млн токенов и остаток по группам Lite, Pro, Max. "
                        "Для физлиц к остатку приложена квота Freemium. Ответ кешируется на минуту, `fresh=true` спросит заново. Токены не тратит.")
def account(request: Request, fresh: bool = False) -> dict:
    return request.app.state.account.info(fresh=fresh)


@router.post("/account/model", summary="Сменить модель",
             description="Модель для всего сервиса: запоминается на сервере и действует, пока её не сменят. "
                         "Подходит только модель из списка доступных у ключа.")
def account_model(body: ModelIn, request: Request) -> dict:
    try:
        return request.app.state.account.set_model(body.model)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
