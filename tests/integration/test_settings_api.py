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


def test_account_shows_balance_against_freemium_quota(settings, fake_llm):
    fake_llm.balance_rows = [{"usage": "GigaChat", "value": 249_000_000}, {"usage": "GigaChat-Max", "value": 24_100_000},
                             {"usage": "embeddings", "value": 1_000_000}]
    a = TestClient(create_app(settings)).get("/api/account").json()
    assert a["ok"] and a["scope_label"] == "Физическое лицо" and a["model"] == "GigaChat-2-Max"
    assert [b["group"] for b in a["balance"]] == ["Lite", "Max"]               # эмбеддинги не показываем
    mx = a["balance"][1]
    assert mx == {"group": "Max", "tokens": 24_100_000, "quota": 25_000_000, "current": True}
    assert {m["id"] for m in a["models"]} == {"GigaChat-2", "GigaChat-2-Max"} and not fake_llm.requests
    assert [(c["group"], c["id"], c["current"]) for c in a["choices"]] == [("Lite", "GigaChat-2", False), ("Max", "GigaChat-2-Max", True)]
    assert all(c["about"] and "₽" not in c["about"] for c in a["choices"])


def test_account_without_balance_does_not_invent_numbers(settings, fake_llm):
    a = TestClient(create_app(settings)).get("/api/account").json()
    assert a["ok"] and a["balance"] is None


def test_model_switch_applies_to_requests_and_survives_restart(settings, fake_llm):
    c = TestClient(create_app(settings))
    assert c.post("/api/account/model", json={"model": "GigaChat-7-Mega"}).status_code == 422
    r = c.post("/api/account/model", json={"model": "GigaChat-2"}).json()
    assert r["model"] == "GigaChat-2" and r["price_rub_per_1m"] == 65
    c.post("/api/chat", json={"message": TZ})
    assert fake_llm.requests and all(q["model"] == "GigaChat-2" for q in fake_llm.requests)
    settings.provider_conf("gigachat")["model"] = "GigaChat-2-Max"            # как будто сервис перезапустили с конфигом по умолчанию
    assert TestClient(create_app(settings)).get("/api/account").json()["model"] == "GigaChat-2"


def test_model_choices_exist_even_without_gigachat(settings, monkeypatch):
    monkeypatch.delenv("GIGACHAT_AUTH_KEY")
    a = TestClient(create_app(settings)).get("/api/account").json()
    assert not a["ok"] and [c["group"] for c in a["choices"]] == ["Lite", "Pro", "Max"]
    assert next(c for c in a["choices"] if c["current"])["group"] == "Max"
