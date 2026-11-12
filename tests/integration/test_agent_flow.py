"""Сквозные сценарии агента на имитации GigaChat и на мок-провайдере со сценарием в тесте."""
from __future__ import annotations

import json

import httpx

from moex_strategy_agent.agent.brain import LLMGateway
from moex_strategy_agent.agent.graph import Agent
from moex_strategy_agent.llm.provider import OpenAICompatibleClient
from tests.conftest import ScriptedLLM, chat_response, install_mock

TZ = ("Протестируй стратегию для акций Сбербанка за 2023–2025 годы. Покупать, когда 20-дневная скользящая "
      "средняя пересекает 50-дневную снизу вверх, продавать при обратном пересечении. Капитал 100 000 рублей.")


def test_end_to_end_dialog(agent, repo):
    r = agent.handle(None, TZ)
    sid = r.session_id
    assert r.intent == "backtest" and r.run_id
    assert r.tools_called == ["get_market_data", "run_backtest", "find_news"]
    res = repo.get_run(r.run_id)
    assert res.verification.status == "OK" and res.narrative
    assert "Не является индивидуальной инвестиционной рекомендацией" in r.text

    r2 = agent.handle(sid, "а если 10 и 30?")
    assert r2.intent == "modify" and repo.get_run(r2.run_id).spec.parameters["fast_period"] == 10
    assert repo.get_run(r2.run_id).spec.start_date.isoformat() == "2023-01-01"     # период унаследован

    r3 = agent.handle(sid, "сравни с предыдущим")
    assert r3.intent == "compare" and any(b["type"] == "compare" for b in r3.blocks)

    r4 = agent.handle(sid, "сохрани")
    assert r4.pending_action and repo.list_research() == []           # без подтверждения ничего не сохранено
    out = agent.confirm(r4.pending_action["id"], approve=True)
    assert out["ok"] and len(repo.list_research()) == 1
    assert agent.confirm(r4.pending_action["id"], approve=True)["ok"] is False   # повторно не исполняется


def test_clarification_then_completion(agent):
    r = agent.handle(None, "протестируй стратегию")
    assert r.intent == "clarify"
    r2 = agent.handle(r.session_id, "Газпром, RSI 14 ниже 30 выше 70 за 2023 год")
    assert r2.intent == "backtest" and r2.run_id


def test_unknown_ticker_asks_instead_of_inventing(agent):
    r = agent.handle(None, "XXXX SMA 20/50 за 2024 год")
    assert r.intent == "clarify" and r.run_id is None and "XXXX" in r.text


def test_data_outage_is_reported_honestly(settings, repo):
    a = Agent(settings.with_overrides({"data": {"fault": "timeout"}}), repo)
    r = a.handle(None, TZ)
    assert r.intent == "data_unavailable" and "data_unavailable" in r.flags and r.run_id is None


def test_verification_failure_is_not_hidden(settings, repo):
    a = Agent(settings.with_overrides({"debug": {"inject_reference_bug": True}}), repo)
    r = a.handle(None, TZ)
    assert "verification_failed" in r.flags
    assert "не подтвержден" in r.text


def test_budget_limit_stops_agent(settings, repo):
    a = Agent(settings.with_overrides({"agent": {"max_tool_calls": 1}}), repo)
    r = a.handle(None, TZ)
    assert "budget_exceeded" in r.flags and "исчерпан" in r.text


def test_state_survives_restart(settings, tmp_path):
    from moex_strategy_agent.persistence.database import Database
    from moex_strategy_agent.persistence.repository import Repository
    path = tmp_path / "persist.sqlite3"
    r = Agent(settings, Repository(Database(path))).handle(None, TZ)
    agent2 = Agent(settings, Repository(Database(path)))          # «перезапуск»
    r2 = agent2.handle(r.session_id, "а если 10 и 30?")
    assert r2.intent == "modify" and r2.run_id


# ---------------- LLM-мозг ----------------

def _llm_script(fake_number: bool = True):
    def responder(body):
        system = body["messages"][0]["content"]
        if "модуль формализации" in system:
            return chat_response(json.dumps({"intent": "backtest", "spec_patch": {
                "ticker": "SBER", "start_date": "2023-01-01", "end_date": "2025-12-31",
                "strategy_type": "SMA_CROSSOVER", "parameters": {"fast_period": 20, "slow_period": 50}},
                "reasoning": "пересечение средних"}, ensure_ascii=False))
        if "объясняет результат" in system:
            return chat_response("Стратегия заработала 512,34% за период. Это выдумка." if fake_number
                                 else "Стратегия показала результат, указанный в отчёте.")
        return chat_response("ok")
    return responder


def test_llm_understanding_and_reflexion_fallback(llm_settings, repo):
    scripted = ScriptedLLM(_llm_script(fake_number=True))
    install_mock(llm_settings, scripted)
    r = Agent(llm_settings, repo).handle(None, "SMA 20/50 по сберу 2023-2025")
    assert r.run_id and r.provider == "mock"
    assert "512" not in r.text                                       # выдуманное число не прошло
    purposes = [e["name"] for e in repo.get_trace(r.trace_id) if e["kind"] == "llm"]
    assert purposes == ["understand", "narrate", "narrate_reflexion"]
    events = repo.get_trace(r.trace_id)
    assert any(e["name"] == "narrative_fallback" for e in events)
    assert sum(e["tokens"] for e in events) > 0 and sum(e["cost_rub"] for e in events) > 0


def test_llm_react_uses_tools_and_hitl(llm_settings, repo):
    state = {"n": 0}
    ids = {"rid": "r_0000abcd"}

    def responder(body):
        system = body["messages"][0]["content"]
        if "модуль формализации" in system:
            text = body["messages"][-1]["content"]
            if "удали" in text:
                return chat_response(json.dumps({"intent": "delete", "spec_patch": {}, "target_id": ids["rid"]}))
            return chat_response(json.dumps({"intent": "trades", "spec_patch": {}}))
        if "объясняет результат" in system:
            return chat_response("Короткий текст без чисел. Не является индивидуальной инвестиционной рекомендацией.")
        state["n"] += 1
        last = body["messages"][-1]
        if last["role"] == "tool":
            return chat_response("Худшие сделки показаны в таблице.")
        if "удали" in body["messages"][1]["content"]:
            return chat_response("", tool_calls=[("delete_research", {"research_id": ids["rid"]})])
        return chat_response("", tool_calls=[("get_trades", {"run_id": run_id, "order": "worst", "limit": 3})])

    scripted = ScriptedLLM(responder)
    install_mock(llm_settings, scripted)
    agent = Agent(llm_settings, repo)
    first = Agent(llm_settings.with_overrides(provider="gigachat"), repo).handle(None, TZ)
    run_id = first.run_id
    r = agent.handle(first.session_id, "покажи худшие сделки")
    assert "get_trades" in r.tools_called and any(b["type"] == "trades" for b in r.blocks)
    tool_msgs = [m for req in scripted.requests for m in req["messages"] if m["role"] == "tool"]
    assert tool_msgs and "pnl" in tool_msgs[0]["content"]

    ids["rid"] = repo.save_research(run_id, "для удаления")
    r2 = agent.handle(first.session_id, f"удали {ids['rid']}")
    assert r2.pending_action is not None and "delete_research" not in r2.tools_called   # не исполнено
    assert len(repo.list_research()) == 1


def test_provider_fallback_chain(llm_settings, repo):
    """Первый провайдер отвечает 500, агент уходит на запасной без падения."""
    s = llm_settings
    s.providers["providers"]["broken"] = {"kind": "openai_compatible", "base_url": "http://broken/v1",
                                          "model": "b", "api_key_env": "", "tools_style": "tools"}
    s.providers["active"] = "broken"
    s.providers["fallback"] = ["mock"]
    LLMGateway._clients["broken"] = OpenAICompatibleClient(
        "broken", s.provider_conf("broken"), transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    install_mock(s, ScriptedLLM(_llm_script(fake_number=False)))
    r = Agent(s, repo).handle(None, "SMA 20/50 по сберу 2023-2025")
    assert r.run_id and r.provider == "mock"
    assert any(e["name"] == "llm_failed" for e in repo.get_trace(r.trace_id))
