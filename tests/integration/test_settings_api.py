"""То, на чём стоят настройки: расход, проверка связи, стиль ответа, очистка истории."""
from __future__ import annotations

from fastapi.testclient import TestClient

from moex_strategy_agent.api.main import create_app

TZ = "SBER за 2023-2025, SMA 20/50, капитал 100000 руб"


def test_usage_counts_model_calls(settings):
    c = TestClient(create_app(settings))
    before = c.get("/api/usage").json()
    assert before["today"]["tokens"] == 0 and before["model"]
    c.post("/api/chat", json={"message": TZ})
    after = c.get("/api/usage").json()
    assert after["today"]["tokens"] > 0 and after["total"]["tokens"] == after["today"]["tokens"]
    assert after["today"]["requests"] == 1 and after["limits"]["max_llm_calls"] > 0


def test_llm_check_does_not_spend_tokens(settings, fake_llm):
    c = TestClient(create_app(settings))
    r = c.post("/api/llm/check").json()
    assert r["ok"] and r["model_listed"] and r["latency_ms"] >= 0
    assert r["balance_tokens"] is None                    # провайдер остаток не сообщил: число не выдумываем
    assert not fake_llm.requests and c.get("/api/usage").json()["total"]["tokens"] == 0


def test_llm_check_reports_missing_key(settings, monkeypatch):
    monkeypatch.delenv("GIGACHAT_AUTH_KEY")
    r = TestClient(create_app(settings)).post("/api/llm/check").json()
    assert not r["ok"] and r["kind"] == "no_key" and "GIGACHAT_AUTH_KEY" in r["text"]


def test_short_style_reaches_the_prompt(settings, fake_llm):
    c = TestClient(create_app(settings))
    c.post("/api/chat", json={"message": TZ, "style": "short"})
    c.post("/api/chat", json={"message": TZ})
    systems = [q["messages"][0]["content"] for q in fake_llm.requests if "объясняет результат" in q["messages"][0]["content"]]
    assert "2-3 предложения" in systems[0] and "5-8 предложений" in systems[1]
    assert c.post("/api/chat", json={"message": TZ, "style": "стихами"}).status_code == 422


def test_clear_history_needs_confirmation_and_keeps_favorites(settings):
    c = TestClient(create_app(settings))
    a = c.post("/api/chat", json={"message": TZ}).json()
    c.post("/api/chat", json={"message": "Газпром за 2024 год, RSI 14 30/70"})
    save = c.post(f"/api/runs/{a['run_id']}/save", json={"session_id": a["session_id"]}).json()
    c.post(f"/api/actions/{save['action_id']}/confirm")

    ask = c.post("/api/history/clear?session_id=ui1").json()
    assert "Избранное (1) останется" in ask["description"]
    assert len(c.get("/api/runs").json()["runs"]) == 2    # без подтверждения ничего не удалено
    done = c.post(f"/api/actions/{ask['action_id']}/confirm").json()
    assert done["ok"] and "удалено запусков: 1" in done["text"]
    left = c.get("/api/runs").json()["runs"]
    assert [r["run_id"] for r in left] == [a["run_id"]] and len(c.get("/api/research").json()["research"]) == 1
