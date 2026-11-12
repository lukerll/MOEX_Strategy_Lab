"""Репозиторий: всё, что агент помнит между запросами и перезапусками."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ..domain.schemas import BacktestResult
from .database import Database


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # --- сессии ---
    def ensure_session(self, session_id: str | None) -> str:
        if session_id and self.db.query("SELECT 1 FROM sessions WHERE id=?", (session_id,)):
            return session_id
        sid = session_id or uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO sessions(id, created_at, updated_at) VALUES (?,?,?)", (sid, _now(), _now()))
        return sid

    def get_state(self, session_id: str) -> dict[str, Any]:
        rows = self.db.query("SELECT state_json FROM sessions WHERE id=?", (session_id,))
        return json.loads(rows[0]["state_json"]) if rows else {}

    def set_state(self, session_id: str, state: dict[str, Any]) -> None:
        self.db.execute("UPDATE sessions SET state_json=?, updated_at=? WHERE id=?",
                        (json.dumps(state, ensure_ascii=False, default=str), _now(), session_id))

    # --- сообщения ---
    def add_message(self, session_id: str, role: str, content: str, meta: dict | None = None) -> None:
        self.db.execute("INSERT INTO messages(session_id, role, content, meta_json, created_at) VALUES (?,?,?,?,?)",
                        (session_id, role, content, json.dumps(meta or {}, ensure_ascii=False, default=str), _now()))

    def messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT role, content, meta_json, created_at FROM messages WHERE session_id=? "
                             "ORDER BY id DESC LIMIT ?", (session_id, limit))
        return [{"role": r["role"], "content": r["content"], "meta": json.loads(r["meta_json"]),
                 "created_at": r["created_at"]} for r in reversed(rows)]

    # --- запуски ---
    def save_run(self, result: BacktestResult, session_id: str | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO runs(id, session_id, title, spec_json, result_json, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (result.run_id, session_id, result.spec.title(), result.spec.model_dump_json(),
                         result.model_dump_json(), result.verification.status, _now()))

    def get_run(self, run_id: str) -> BacktestResult | None:
        rows = self.db.query("SELECT result_json FROM runs WHERE id=?", (run_id,))
        return BacktestResult.model_validate_json(rows[0]["result_json"]) if rows else None

    def list_runs(self, limit: int = 20, session_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, session_id, title, status, created_at, result_json FROM runs"
        params: tuple = ()
        if session_id:
            sql += " WHERE session_id=?"
            params = (session_id,)
        rows = self.db.query(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", params + (limit,))
        out = []
        for r in rows:
            res = BacktestResult.model_validate_json(r["result_json"])
            out.append({**res.headline(), "created_at": r["created_at"], "session_id": r["session_id"]})
        return out

    # --- сохранённые исследования ---
    def save_research(self, run_id: str, title: str) -> str:
        rid = "r_" + uuid.uuid4().hex[:8]
        self.db.execute("INSERT INTO research(id, run_id, title, created_at) VALUES (?,?,?,?)",
                        (rid, run_id, title, _now()))
        return rid

    def delete_research(self, research_id: str) -> bool:
        return self.db.execute("DELETE FROM research WHERE id=?", (research_id,)).rowcount > 0

    def delete_run(self, run_id: str) -> dict[str, Any]:
        """Удаляет запуск из истории вместе с его записями в избранном.

        Сообщения диалога не трогаем: в них остаётся текст, а ссылка на результат честно
        показывает, что анализ удалён. Из состояния сессий убираем ссылку на запуск,
        чтобы «покажи сделки» не обратилось к пустоте.
        """
        favs = self.db.execute("DELETE FROM research WHERE run_id=?", (run_id,)).rowcount
        n = self.db.execute("DELETE FROM runs WHERE id=?", (run_id,)).rowcount
        for row in self.db.query("SELECT id, state_json FROM sessions WHERE state_json LIKE ?", (f"%{run_id}%",)):
            st = json.loads(row["state_json"])
            for k in ("last_run_id", "prev_run_id"):
                if st.get(k) == run_id:
                    st.pop(k)
            if st.get("last_run_id") is None:
                st.pop("last_spec", None)
            self.set_state(row["id"], st)
        return {"deleted": n > 0, "favorites_removed": favs}

    def list_research(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT r.id, r.run_id, r.title, r.created_at, runs.session_id, runs.result_json FROM research r "
                             "JOIN runs ON runs.id = r.run_id ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?",
                             (limit,))
        return [{"id": r["id"], "run_id": r["run_id"], "title": r["title"], "created_at": r["created_at"],
                 "session_id": r["session_id"],
                 **{k: v for k, v in BacktestResult.model_validate_json(r["result_json"]).headline().items()
                    if k != "run_id"}} for r in rows]

    # --- действия, ждущие человека ---
    def create_pending(self, session_id: str, tool: str, args: dict, description: str) -> str:
        aid = "a_" + uuid.uuid4().hex[:8]
        self.db.execute("INSERT INTO pending_actions(id, session_id, tool, args_json, description, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (aid, session_id, tool, json.dumps(args, ensure_ascii=False), description, "pending", _now()))
        return aid

    def get_pending(self, action_id: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM pending_actions WHERE id=?", (action_id,))
        if not rows:
            return None
        r = dict(rows[0])
        r["args"] = json.loads(r.pop("args_json"))
        return r

    def resolve_pending(self, action_id: str, status: str) -> None:
        self.db.execute("UPDATE pending_actions SET status=?, resolved_at=? WHERE id=?", (status, _now(), action_id))

    def open_pending(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id, tool, description FROM pending_actions WHERE session_id=? AND status='pending'",
                             (session_id,))
        return [dict(r) for r in rows]

    # --- трасса ---
    def add_trace(self, trace_id: str, session_id: str | None, seq: int, kind: str, name: str,
                  payload: dict, tokens: int = 0, cost: float = 0.0, latency_ms: int = 0) -> None:
        self.db.execute("INSERT INTO trace_events(trace_id, session_id, seq, kind, name, payload_json, tokens, cost_rub, "
                        "latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (trace_id, session_id, seq, kind, name, json.dumps(payload, ensure_ascii=False, default=str),
                         tokens, cost, latency_ms, _now()))

    def get_trace(self, trace_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT seq, kind, name, payload_json, tokens, cost_rub, latency_ms, created_at "
                             "FROM trace_events WHERE trace_id=? ORDER BY seq", (trace_id,))
        return [{**{k: r[k] for k in ("seq", "kind", "name", "tokens", "cost_rub", "latency_ms", "created_at")},
                 "payload": json.loads(r["payload_json"])} for r in rows]

    def usage(self) -> dict[str, Any]:
        """Расход по обращениям к модели: сегодня (по UTC) и за всё время. Источник: трасса."""
        def one(where: str, params: tuple = ()) -> dict[str, Any]:
            r = self.db.query("SELECT COUNT(DISTINCT trace_id) n, SUM(tokens) t, SUM(cost_rub) c FROM trace_events "
                              "WHERE kind='llm'" + where, params)[0]
            return {"requests": r["n"] or 0, "tokens": r["t"] or 0, "cost_rub": round(r["c"] or 0, 2)}
        return {"today": one(" AND created_at >= ?", (_now()[:10],)), "total": one("")}

    def clear_history(self) -> dict[str, Any]:
        """Удаляет запуски, которых нет в избранном. Избранное и переписка остаются."""
        ids = [r["id"] for r in self.db.query(
            "SELECT id FROM runs WHERE id NOT IN (SELECT run_id FROM research)")]
        for run_id in ids:
            self.delete_run(run_id)
        return {"deleted": len(ids), "kept_favorites": len(self.list_research(500))}

    def trace_stats(self) -> dict[str, Any]:
        r = self.db.query("SELECT COUNT(DISTINCT trace_id) n, SUM(tokens) t, SUM(cost_rub) c FROM trace_events")[0]
        return {"traces": r["n"] or 0, "tokens": r["t"] or 0, "cost_rub": round(r["c"] or 0, 4)}
