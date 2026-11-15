"""Выгрузка диалога в Markdown: реплики, что понял агент, какие шаги прошёл и чем закончились запуски.

Файл рассчитан на чтение человеком и другой языковой моделью при разборе диалога.
Ключей, промптов целиком и содержимого .env в выгрузке нет: в трассе хранятся только хвост
реплики и первые символы ответа модели.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..persistence.repository import Repository

# шаги трассы, которые помогают понять ход диалога; остальные (stage, start) только шумят
_KEEP = {"node", "llm", "tool", "guardrail", "error", "budget", "data"}
_SKIP_NODES = {"start"}


def _dt(s: str | None) -> str:
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s).strftime("%d.%m.%Y %H:%M:%S")
    except ValueError:
        return s


def _js(v: Any, limit: int = 1500) -> str:
    text = json.dumps(v, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " …"


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in (text or "").splitlines()) or ">"


def _steps(events: list[dict[str, Any]]) -> list[str]:
    out = []
    for e in events:
        kind, name, p = e["kind"], e["name"], e.get("payload") or {}
        if kind not in _KEEP or (kind == "node" and name in _SKIP_NODES):
            continue
        cost = []
        if e.get("tokens"):
            cost.append(f"{e['tokens']} токенов")
        if e.get("latency_ms"):
            cost.append(f"{e['latency_ms'] / 1000:.1f} с")
        head = f"- `{kind}` {name}" + (f" ({', '.join(cost)})" if cost else "")
        if kind == "llm":
            body = {k: p[k] for k in ("model", "response", "tool_calls") if p.get(k)}
        elif kind == "node" and name == "understand":
            body = {k: p.get(k) for k in ("intent", "source", "question", "reasoning", "patch") if p.get(k)}
        else:
            body = p
        out.append(head + (f": `{_js(body)}`" if body else ""))
    return out


def _run_block(repo: Repository, run_id: str) -> list[str]:
    res = repo.get_run(run_id)
    if not res:
        return [f"Запуск `{run_id}` удалён из истории."]
    h, m = res.headline(), res.metrics
    lines = [
        f"Запуск `{run_id}`: {h['ticker']}, {h['strategy']}, {h['period']}",
        "",
        "| Показатель | Значение |",
        "|---|---|",
        f"| Доходность стратегии | {h['total_return_pct']}% |",
        f"| Купить и держать | {h['buy_hold_return_pct']}% |",
        f"| Макс. просадка | {h['max_drawdown_pct']}% |",
        f"| Сделок | {h['trades']} |",
        f"| Итоговый капитал | {round(m.final_equity, 2)} ₽ |",
        f"| Сверка вторым движком | {h['verification']} |",
        f"| Демо-котировки | {'да' if h['synthetic_data'] else 'нет'} |",
        "",
        f"Спецификация: `{_js(res.spec.model_dump(mode='json'), 3000)}`",
    ]
    return lines


def build_chat_markdown(repo: Repository, session_id: str) -> str:
    msgs = repo.messages(session_id, limit=1000)
    model = ""
    tokens = 0
    blocks: list[str] = []
    for i, msg in enumerate(msgs, 1):
        meta = msg.get("meta") or {}
        who = "Пользователь" if msg["role"] == "user" else "Signal"
        blocks.append(f"## {i}. {who}, {_dt(msg.get('created_at'))}")
        blocks.append("")
        blocks.append(_quote(msg["content"]))
        if msg["role"] != "user":
            info = []
            if meta.get("intent"):
                info.append(f"намерение `{meta['intent']}`")
            if meta.get("flags"):
                info.append(f"флаги `{_js(meta['flags'], 300)}`")
            if info:
                blocks += ["", "Агент: " + ", ".join(info)]
            if meta.get("suggestions"):
                blocks += ["", "Кнопки ответа: " + "; ".join(str(s) for s in meta["suggestions"])]
            if meta.get("run_id"):
                blocks += [""] + _run_block(repo, meta["run_id"])
            if meta.get("trace_id"):
                events = repo.get_trace(meta["trace_id"])
                tokens += sum(e.get("tokens") or 0 for e in events)
                for e in events:
                    if e["kind"] == "llm" and (e.get("payload") or {}).get("model"):
                        model = e["payload"]["model"]
                steps = _steps(events)
                if steps:
                    blocks += ["", "<details><summary>Шаги агента</summary>", ""] + steps + ["", "</details>"]
        blocks.append("")

    first = msgs[0]["created_at"] if msgs else None
    last = msgs[-1]["created_at"] if msgs else None
    head = [
        "# Выгрузка диалога MOEX Strategy Agent",
        "",
        "Агент проверяет торговые стратегии на истории акций Мосбиржи. Ниже реплики по порядку.",
        "Под ответами агента: намерение, которое он распознал, кнопки ответа, результаты запусков",
        "и шаги из трассы (`node` решения графа, `llm` ответы модели, `tool` инструменты,",
        "`guardrail` срабатывания защиты, `error` сбои).",
        "",
        "| Поле | Значение |",
        "|---|---|",
        f"| Сессия | `{session_id}` |",
        f"| Начало | {_dt(first)} |",
        f"| Конец | {_dt(last)} |",
        f"| Реплик | {len(msgs)} |",
        f"| Модель | {model or 'не вызывалась'} |",
        f"| Токенов за диалог | {tokens} |",
        f"| Выгружено | {datetime.now().strftime('%d.%m.%Y %H:%M:%S')} |",
        "",
    ]
    if not msgs:
        head.append("В этой сессии ещё нет реплик.")
    return "\n".join(head + blocks).rstrip() + "\n"
