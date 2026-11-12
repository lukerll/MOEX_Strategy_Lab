"""Трассировка: каждый шаг (узел графа, вызов модели, инструмент, срабатывание защиты)
пишется в SQLite и в JSONL-лог с токенами, стоимостью и задержкой."""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..persistence.repository import Repository


class Tracer:
    def __init__(self, repo: Repository, session_id: str | None, log_dir: Path | None = None,
                 listener: Any = None):
        self.repo = repo
        self.listener = listener          # получает события по мере выполнения (потоковый ответ в UI)
        self.session_id = session_id
        self.trace_id = "t_" + uuid.uuid4().hex[:10]
        self.seq = 0
        self.log_path = (log_dir / "trace.jsonl") if log_dir else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, name: str, payload: dict[str, Any] | None = None, *,
              tokens: int = 0, cost: float = 0.0, latency_ms: int = 0) -> None:
        self.seq += 1
        payload = payload or {}
        self.repo.add_trace(self.trace_id, self.session_id, self.seq, kind, name, payload,
                            tokens, cost, latency_ms)
        if self.listener is not None:
            try:
                self.listener(kind, name, payload)
            except Exception:  # noqa: BLE001, слушатель не должен ломать агента
                pass
        if self.log_path:
            rec = {"ts": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="milliseconds"), "trace_id": self.trace_id,
                   "session_id": self.session_id, "seq": self.seq, "kind": kind, "name": name,
                   "tokens": tokens, "cost_rub": cost, "latency_ms": latency_ms, "payload": payload}
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    @contextmanager
    def span(self, kind: str, name: str, payload: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Замеряет длительность; в out можно дописать результат."""
        out: dict[str, Any] = {}
        t0 = time.monotonic()
        try:
            yield out
        except Exception as e:
            out["error"] = f"{e.__class__.__name__}: {e}"
            raise
        finally:
            self.event(kind, name, {**(payload or {}), **out}, latency_ms=int((time.monotonic() - t0) * 1000))
