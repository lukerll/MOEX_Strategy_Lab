"""Выгрузка диалога в Markdown для разбора другой моделью."""
from __future__ import annotations

import os

from fastapi.testclient import TestClient

from moex_strategy_agent.api.main import create_app

TZ = "SBER за 2023-2025, SMA 20/50, капитал 100000 руб"


def test_session_export_has_replies_steps_and_run(settings, fake_llm):
    c = TestClient(create_app(settings))
    a = c.post("/api/chat", json={"message": "покупаю на просадках", "session_id": "ui7"}).json()
    c.post("/api/chat", json={"message": TZ, "session_id": "ui7"})
    r = c.get("/api/sessions/ui7/export.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert 'filename="chat_ui7_' in r.headers["content-disposition"]
    md = r.text
    assert md.startswith("# Выгрузка диалога") and "| Реплик | 4 |" in md
    assert "## 1. Пользователь" in md and "> покупаю на просадках" in md
    assert "## 2. Signal" in md and a["text"].splitlines()[0] in md
    assert "Шаги агента" in md and "`node` understand" in md and "`llm`" in md
    assert "| Доходность стратегии |" in md and "Спецификация:" in md
    assert os.environ["GIGACHAT_AUTH_KEY"] not in md          # ключ в выгрузку не попадает


def test_run_menu_exports_dialog_of_that_run(settings, fake_llm):
    c = TestClient(create_app(settings))
    a = c.post("/api/chat", json={"message": TZ, "session_id": "ui8"}).json()
    c.post("/api/chat", json={"message": "Газпром за 2024 год, RSI 14 30/70", "session_id": "ui9"})
    md = c.get(f"/api/runs/{a['run_id']}/chat.md").text
    assert "`ui8`" in md and "Газпром" not in md
    assert c.get("/api/runs/nope/chat.md").status_code == 404


def test_empty_session_exports_without_error(settings):
    md = TestClient(create_app(settings)).get("/api/sessions/empty1/export.md").text
    assert "| Реплик | 0 |" in md and "ещё нет реплик" in md
