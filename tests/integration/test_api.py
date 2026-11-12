"""HTTP-сервис: чат, отчёты, подтверждение, перезапуск, ключ API, лимит запросов."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from moex_strategy_agent.api.main import create_app

TZ = "SBER за 2023-2025, SMA 20/50, капитал 100000 руб"


def _client(settings, **over):
    return TestClient(create_app(settings.with_overrides(over) if over else settings))


def test_chat_report_and_confirm(settings):
    c = _client(settings)
    r = c.post("/api/chat", json={"message": TZ}).json()
    run_id, sid = r["run_id"], r["session_id"]
    assert r["intent"] == "backtest"

    run = c.get(f"/api/runs/{run_id}").json()
    assert run["verification"]["status"] == "OK" and len(run["equity"]) > 500

    html = c.get(f"/api/runs/{run_id}/report.html").text
    assert "<svg" in html and "Не является индивидуальной инвестиционной рекомендацией" in html
    assert "@font-face" in html                                      # отчёт автономный
    csv = c.get(f"/api/runs/{run_id}/trades.csv").text
    assert csv.lstrip("﻿").startswith("Дата покупки;Дата продажи")
    assert "." not in csv.splitlines()[1].split(";")[3]                # цена с запятой для русского Excel

    save = c.post(f"/api/runs/{run_id}/save", json={"session_id": sid}).json()
    assert c.get("/api/research").json()["research"] == []
    assert c.post(f"/api/actions/{save['action_id']}/confirm").json()["ok"]
    assert len(c.get("/api/research").json()["research"]) == 1

    trace = c.get(f"/api/traces/{r['trace_id']}").json()
    kinds = {e["kind"] for e in trace["events"]}
    assert {"node", "tool", "summary"} <= kinds


def test_direct_backtest_endpoint_validates(settings):
    c = _client(settings)
    bad = c.post("/api/backtests", json={"ticker": "SBER", "start_date": "2024-01-01", "end_date": "2024-12-31",
                                         "strategy_type": "SMA_CROSSOVER",
                                         "parameters": {"fast_period": 50, "slow_period": 10}})
    assert bad.status_code == 422
    ok = c.post("/api/backtests", json={"ticker": "GAZP", "start_date": "2024-01-01", "end_date": "2024-12-31",
                                        "strategy_type": "BUY_AND_HOLD"})
    assert ok.status_code == 200 and ok.json()["result"]["verification"]["status"] == "OK"


def test_restart_keeps_sessions_and_research(settings):
    c1 = _client(settings)
    r = c1.post("/api/chat", json={"message": TZ}).json()
    a = c1.post(f"/api/runs/{r['run_id']}/save", json={"session_id": r["session_id"]}).json()
    c1.post(f"/api/actions/{a['action_id']}/confirm")
    c2 = _client(settings)                                           # новый процесс, та же база
    msgs = c2.get(f"/api/sessions/{r['session_id']}/messages").json()
    assert len(msgs["messages"]) >= 2 and msgs["state"]["last_run_id"] == r["run_id"]
    assert len(c2.get("/api/research").json()["research"]) == 1


def test_api_key_and_rate_limit(settings):
    c = _client(settings, app={"api_key": "secret", "rate_limit_per_minute": 2})
    assert c.post("/api/chat", json={"message": "привет"}).status_code == 401
    h = {"X-API-Key": "secret"}
    assert c.post("/api/chat", json={"message": "привет"}, headers=h).status_code == 200
    assert c.post("/api/chat", json={"message": "привет"}, headers=h).status_code == 200
    assert c.post("/api/chat", json={"message": "привет"}, headers=h).status_code == 429
    assert c.get("/api/health").status_code == 200


def test_rules_strategy_via_api_keeps_understood_after_restart(settings):
    body = {"ticker": "SBER", "start_date": "2021-01-01", "end_date": "2025-12-31", "strategy_type": "RULES",
            "rules": {"entry": {"type": "all", "conditions": [
                {"type": "compare", "left": {"type": "price"}, "op": ">", "right": {"type": "sma", "n": 200}},
                {"type": "compare", "left": {"type": "rsi", "n": 14}, "op": "<", "right": {"type": "const", "value": 40}}]},
                "stop_loss_pct": 7, "max_hold_days": 20}}
    r = _client(settings).post("/api/backtests", json=body)
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["verification"]["status"] == "OK"
    assert res["understood"]["entry"].startswith("Покупаю, когда цена выше средней за 200 дней")
    run = _client(settings).get(f"/api/runs/{res['run_id']}").json()          # новый процесс
    assert run["understood"] == res["understood"] and run["spec"]["rules"]["stop_loss_pct"] == 7
    assert "Покупаю" in _client(settings).get(f"/api/runs/{res['run_id']}/report.html").text


def test_delete_run_from_history_needs_confirmation_and_cleans_up(settings):
    c = _client(settings)
    r = c.post("/api/chat", json={"message": TZ}).json()
    run_id, sid = r["run_id"], r["session_id"]
    a = c.post(f"/api/runs/{run_id}/save", json={"session_id": sid}).json()
    c.post(f"/api/actions/{a['action_id']}/confirm")
    assert len(c.get("/api/research").json()["research"]) == 1

    d = c.post(f"/api/runs/{run_id}/delete", params={"session_id": sid}).json()
    assert "Удалить из истории" in d["description"] and "из избранного" in d["description"]
    assert c.get(f"/api/runs/{run_id}").status_code == 200                 # без «Да» ничего не удалено
    c.post(f"/api/actions/{d['action_id']}/reject")
    assert c.get(f"/api/runs/{run_id}").status_code == 200                 # «Нет» тоже ничего не удаляет

    d = c.post(f"/api/runs/{run_id}/delete", params={"session_id": sid}).json()
    out = c.post(f"/api/actions/{d['action_id']}/confirm").json()
    assert out["ok"] and "Удалено из истории" in out["text"] and "избранного" in out["text"]
    assert c.get(f"/api/runs/{run_id}").status_code == 404
    assert c.get("/api/research").json()["research"] == []
    assert all(x["run_id"] != run_id for x in c.get("/api/runs").json()["runs"])
    state = c.get(f"/api/sessions/{sid}/messages").json()["state"]
    assert state.get("last_run_id") != run_id                               # агент не обратится к пустоте
    follow = c.post("/api/chat", json={"message": "покажи худшие сделки", "session_id": sid}).json()
    assert follow["intent"] != "error" and "internal_error" not in follow["flags"]


def test_delete_run_rejects_bad_input(settings):
    c = _client(settings)
    assert c.post("/api/runs/0000000000/delete", params={"session_id": "s1"}).status_code == 404
    assert c.post("/api/runs/0000000000/delete", params={"session_id": "x'; drop"}).status_code == 422


def _stream(c, message, sid=None):
    lines = c.post("/api/chat/stream", json={"message": message, "session_id": sid}).text.strip().split("\n")
    return [json.loads(x) for x in lines]


def test_stream_reports_real_stages_in_order(settings):
    c = _client(settings)
    ev = _stream(c, TZ)
    stages = [e["stage"] for e in ev if e["type"] == "stage"]
    assert stages == ["understand", "data", "backtest", "news", "narrate"]
    reply = ev[-1]
    assert reply["type"] == "reply" and reply["reply"]["run_id"]
    # история знает, в каком диалоге сделан анализ
    runs = c.get("/api/runs").json()["runs"]
    assert runs[0]["session_id"] == reply["reply"]["session_id"]


def test_stream_stops_at_real_stage_when_data_fails(settings):
    c = _client(settings.with_overrides({"data": {"fault": "timeout"}}))
    ev = _stream(c, TZ)
    stages = [e["stage"] for e in ev if e["type"] == "stage"]
    assert stages == ["understand", "data"]                          # дальше котировок не пошли и не делаем вид
    assert ev[-1]["reply"]["intent"] == "data_unavailable"


def test_stream_followup_stage(settings):
    c = _client(settings)
    sid = _stream(c, TZ)[-1]["reply"]["session_id"]
    stages = [e["stage"] for e in _stream(c, "покажи худшие сделки", sid) if e["type"] == "stage"]
    assert stages == ["understand", "answer"]


def test_scope_hint_while_typing(settings):
    client = _client(settings)
    r = client.get("/api/scope", params={"text": "шорт сбербанка на пробое минимума"}).json()
    assert r["hint"] and "короткие позиции" in r["hint"].lower()
    r = client.get("/api/scope", params={"text": "SBER и GAZP, SMA 20 и 50"}).json()
    assert r["hint"] and "SBER" in r["hint"] and "GAZP" in r["hint"]
    assert client.get("/api/scope", params={"text": "Сбербанк, SMA 20 и 50"}).json()["hint"] is None
    assert client.get("/api/scope", params={"text": "шорт"}).json()["hint"] is None   # слишком коротко


def test_default_capital_from_settings_is_used_when_not_named(settings):
    client = _client(settings)
    r = client.post("/api/chat", json={"message": "SBER за 2023-2024, SMA 20 и 50", "initial_cash": 500000}).json()
    run = client.get(f"/api/runs/{r['run_id']}").json()
    assert run["spec"]["initial_cash"] == 500000
    assert any(a.startswith("капитал не указан") for a in run["assumptions"])
    assert client.get("/api/config").json()["default_cash"] == 100000
