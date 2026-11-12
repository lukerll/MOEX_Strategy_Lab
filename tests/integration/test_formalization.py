"""Формализация через модель: свои правила, уточнения, починка ошибок, честный отказ без модели."""
from __future__ import annotations

import json

from moex_strategy_agent.agent.graph import Agent
from tests.conftest import ScriptedLLM, chat_response, install_mock

CUSTOM = ("Сбер за 2021–2025: покупаю, когда цена выше средней за 200 дней и RSI ниже 40, "
          "продаю при RSI выше 70, стоп 7%")


def test_custom_strategy_is_formalized_and_shown_back(agent, repo, fake_llm):
    r = agent.handle(None, CUSTOM)
    assert r.intent == "backtest" and r.run_id, r.text
    res = repo.get_run(r.run_id)
    assert res.spec.strategy_type.value == "RULES" and res.spec.rules.stop_loss_pct == 7
    assert res.understood["entry"] == "Покупаю, когда цена выше средней за 200 дней и RSI(14) ниже 40."
    assert "цена упала на 7%" in res.understood["exit"]
    assert res.verification.status == "OK"
    assert "период RSI не назван" in r.text                    # допущение модели показано человеку
    assert fake_llm.oauth_calls == 1 and r.provider == "gigachat"


def test_every_indicator_family_runs_through_chat(agent, repo):
    for text in ["GAZP за 2022–2025, MACD пересекает сигнальную снизу вверх, выход по обратному или через 30 дней",
                 "LKOH за 2023–2025, покупаю ниже нижней полосы Боллинджера, продаю выше средней, тейк 10%",
                 "SBER за 2022–2025, стохастик %K пересекает %D снизу ниже 20, продаю выше 80, стоп 2 ATR"]:
        r = agent.handle(None, text)
        assert r.run_id, (text, r.text)
        assert repo.get_run(r.run_id).verification.status == "OK"


def test_ambiguous_strategy_asks_then_completes(agent, repo, fake_llm):
    r = agent.handle(None, "Лукойл за 2022–2025, покупаю на просадках")
    assert r.intent == "clarify" and r.run_id is None and "просадку" in r.text
    r2 = agent.handle(r.session_id, "да, упала на 10% за 20 дней, продаю через 15 дней")
    assert r2.run_id, r2.text
    spec = repo.get_run(r2.run_id).spec
    assert spec.ticker == "LKOH" and spec.start_date.year == 2022 and spec.rules.max_hold_days == 15
    assert repo.get_run(r2.run_id).understood["entry"] == "Покупаю, когда цена за 20 дней упала больше чем на 10%."
    # модель видела свой вопрос и ответ человека как настоящие реплики
    req = next(q for q in fake_llm.requests if "да, упала" in q["messages"][-1]["content"])
    roles = [m["role"] for m in req["messages"]]
    assert roles[:4] == ["system", "user", "assistant", "user"]


def test_rules_strategy_can_be_modified_by_followup(agent, repo):
    r = agent.handle(None, CUSTOM)
    assert "а без стоп-лосса?" in r.suggestions
    r2 = agent.handle(r.session_id, "а без стоп-лосса?")
    spec = repo.get_run(r2.run_id).spec
    assert r2.intent == "modify" and spec.rules.stop_loss_pct is None
    assert spec.rules.entry == repo.get_run(r.run_id).spec.rules.entry and spec.start_date.year == 2021


def _formalizer(answers):
    calls = {"n": 0}

    def responder(body):
        system = body["messages"][0]["content"]
        if "модуль формализации" in system:
            a = answers[min(calls["n"], len(answers) - 1)]
            calls["n"] += 1
            return chat_response(a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))
        return chat_response("")
    return responder


GOOD = {"intent": "backtest", "spec_patch": {"ticker": "SBER", "start_date": "2023-01-01", "end_date": "2025-12-31",
        "strategy_type": "RULES", "rules": {"entry": {"type": "compare", "left": {"type": "price"}, "op": ">",
                                                      "right": {"type": "sma", "n": 50}}, "max_hold_days": 10}}}


def test_bad_rules_are_sent_back_for_repair(llm_settings, repo):
    bad_schema = json.loads(json.dumps(GOOD))
    bad_schema["spec_patch"]["rules"]["entry"] = {"type": "python", "code": "import os; os.system('rm -rf /')"}
    bad_logic = json.loads(json.dumps(GOOD))
    bad_logic["spec_patch"]["rules"]["entry"] = {"type": "compare", "left": {"type": "const", "value": 1},
                                                 "op": ">", "right": {"type": "const", "value": 2}}
    scripted = ScriptedLLM(_formalizer([bad_schema, bad_logic, GOOD]))
    install_mock(llm_settings, scripted)
    r = Agent(llm_settings, repo).handle(None, "сбер, цена выше SMA 50, держу 10 дней")
    assert r.run_id, r.text
    trace = repo.get_trace(r.trace_id)
    assert [e["name"] for e in trace if e["kind"] == "llm"][:3] == ["understand", "understand_repair", "understand_repair"]
    rejected = [e for e in trace if e["name"] == "formalize_rejected"]
    assert len(rejected) == 2 and "схема правил" in json.dumps(rejected[0], ensure_ascii=False)
    repair_msg = scripted.requests[1]["messages"][-1]["content"]
    assert repair_msg.startswith("Ответ не принят")


def test_hopeless_model_leads_to_question_not_invention(llm_settings, repo):
    scripted = ScriptedLLM(_formalizer(["это не JSON", "{\"intent\": \"backtest\", \"spec_patch\": {\"strategy_type\": \"MAGIC\"}}"]))
    install_mock(llm_settings, scripted)
    r = Agent(llm_settings, repo).handle(None, "сбер, какая-то хитрая стратегия")
    assert r.intent == "clarify" and r.run_id is None and "Опишите проще" in r.text
    # первая попытка, две починки и один запрос вопроса: дальше не тратим деньги
    assert len(scripted.requests) == 4
    assert "Больше не пытайся" in scripted.requests[3]["messages"][-1]["content"]


def test_no_model_means_honest_refusal(agent, monkeypatch):
    monkeypatch.delenv("GIGACHAT_AUTH_KEY")
    r = agent.handle(None, CUSTOM)
    assert r.intent == "model_unavailable" and r.run_id is None and r.tools_called == []
    assert "GIGACHAT_AUTH_KEY" in r.text and r.suggestions == []   # примеры без модели не сработают
    assert "llm_no_key" in r.flags
    r2 = agent.handle(None, "купи 100 акций сбера на моём брокерском счёте")
    assert "trading_refused" in r2.flags                # защита в коде работает и без модели


def test_gigachat_function_results_are_json_objects():
    from moex_strategy_agent.llm.provider import OpenAICompatibleClient
    c = OpenAICompatibleClient("g", {"model": "m", "base_url": "http://x", "tools_style": "functions", "api_key_env": ""})
    out = c._messages([{"role": "tool", "name": "find_news", "tool_call_id": "1", "content": "<untrusted_data>x</untrusted_data>"},
                       {"role": "tool", "name": "get_trades", "tool_call_id": "2", "content": "{\"a\": 1}"}])
    assert json.loads(out[0]["content"]) == {"result": "<untrusted_data>x</untrusted_data>"}
    assert out[1]["content"] == "{\"a\": 1}"


def test_dotenv_is_read_without_overriding_env(tmp_path, monkeypatch):
    from moex_strategy_agent.config import load_dotenv
    (tmp_path / ".env").write_text("# комментарий\nMSA_T1=from_file\nMSA_T2=\"quoted\"\nMSA_T3=file\n", encoding="utf-8")
    monkeypatch.setenv("MSA_T3", "env")
    load_dotenv(tmp_path / ".env")
    import os
    assert os.environ["MSA_T1"] == "from_file" and os.environ["MSA_T2"] == "quoted" and os.environ["MSA_T3"] == "env"
    for k in ("MSA_T1", "MSA_T2"):
        monkeypatch.delenv(k)


def test_config_reports_llm_readiness_without_leaking_key(settings, monkeypatch):
    from fastapi.testclient import TestClient
    from moex_strategy_agent.api.main import create_app
    c = TestClient(create_app(settings))
    body = c.get("/api/config").text
    assert '"llm_ready":true' in body and "dGVzdDp0ZXN0" not in body
    monkeypatch.delenv("GIGACHAT_AUTH_KEY")
    assert c.get("/api/config").json()["llm_ready"] is False


def test_check_gigachat_script_on_fake(monkeypatch, capsys):
    import runpy
    import sys
    monkeypatch.setattr(sys, "argv", ["check_gigachat.py"])
    try:
        runpy.run_path("scripts/check_gigachat.py", run_name="__main__")
    except SystemExit as e:
        assert e.code == 0
    out = capsys.readouterr().out
    assert "Итого: 3 из 3" in out and "dGVzdDp0ZXN0" not in out and "fake-token" not in out


def test_model_shortcuts_are_normalized_or_sent_back(llm_settings, repo):
    lazy = json.loads(json.dumps(GOOD))
    lazy["spec_patch"]["rules"] = {"entry": {"type": "compare", "left": {"type": "rsi", "n": 14}, "op": "<", "right": 30},
                                   "exit": {"type": "compare", "left": {"type": "rsi", "n": 14}, "op": ">", "right": 70},
                                   "stop_loss_pct": 0.07}
    fixed = json.loads(json.dumps(lazy))
    fixed["spec_patch"]["rules"]["stop_loss_pct"] = 7
    scripted = ScriptedLLM(_formalizer([lazy, fixed]))
    install_mock(llm_settings, scripted)
    r = Agent(llm_settings, repo).handle(None, "сбер, RSI ниже 30 покупаю, выше 70 продаю, стоп 7%")
    assert r.run_id, r.text
    rules = repo.get_run(r.run_id).spec.rules
    assert rules.stop_loss_pct == 7 and rules.entry.right.value == 30          # число стало константой без запроса
    assert "слишком мал" in scripted.requests[1]["messages"][-1]["content"]     # доля вместо процентов ушла на исправление


# ---------- регрессии живого прогона на GigaChat (eval 07.11.2026) ----------

def test_always_as_exit_is_rejected_and_repaired(llm_settings, repo):
    """F05: модель записала «продаю через 15 дней» как exit: always и наторговала 88 сделок."""
    bad = json.loads(json.dumps(GOOD))
    bad["spec_patch"]["rules"] = {"entry": {"type": "compare", "left": {"type": "change", "n": 20}, "op": "<=",
                                            "right": {"type": "const", "value": -10}}, "exit": {"type": "always"}}
    good = json.loads(json.dumps(bad))
    good["spec_patch"]["rules"].pop("exit")
    good["spec_patch"]["rules"]["max_hold_days"] = 15
    scripted = ScriptedLLM(_formalizer([bad, good]))
    install_mock(llm_settings, scripted)
    r = Agent(llm_settings, repo).handle(None, "лукойл, покупаю после падения на 10% за 20 дней, продаю через 15 дней")
    assert repo.get_run(r.run_id).spec.rules.max_hold_days == 15
    assert "max_hold_days" in scripted.requests[1]["messages"][-1]["content"]


def test_tree_equal_to_template_becomes_template(llm_settings, repo):
    """C06: модель вправе записать пробой деревом, агент узнаёт шаблон и сохраняет подсказки."""
    tree = {"intent": "backtest", "spec_patch": {"ticker": "LKOH", "start_date": "2022-01-01", "end_date": "2024-12-31",
            "strategy_type": "RULES", "rules": {
                "entry": {"type": "compare", "left": {"type": "price"}, "op": ">", "right": {"type": "highest", "n": 55}},
                "exit": {"type": "compare", "left": {"type": "price"}, "op": "<", "right": {"type": "lowest", "n": 20}}}}}
    install_mock(llm_settings, ScriptedLLM(_formalizer([tree])))
    r = Agent(llm_settings, repo).handle(None, "лукойл, пробой 55-дневного максимума, выход по минимуму за 20")
    spec = repo.get_run(r.run_id).spec
    assert spec.strategy_type.value == "BREAKOUT" and spec.parameters == {"entry_lookback": 55, "exit_lookback": 20}


def test_swapped_periods_are_fixed_by_code(llm_settings, repo):
    """R03: «быстрая 50, медленная 20» модель передаёт как есть, код переставляет и говорит об этом."""
    ans = {"intent": "backtest", "spec_patch": {"ticker": "SBER", "start_date": "2023-01-01", "end_date": "2023-12-31",
           "strategy_type": "SMA_CROSSOVER", "parameters": {"fast_period": 50, "slow_period": 20}}}
    install_mock(llm_settings, ScriptedLLM(_formalizer([ans])))
    r = Agent(llm_settings, repo).handle(None, "SBER за 2023 год, SMA быстрая 50, медленная 20")
    assert repo.get_run(r.run_id).spec.parameters == {"fast_period": 20, "slow_period": 50}
    assert "переставлены" in r.text


def test_save_does_not_depend_on_model_calling_tool(llm_settings, repo):
    """S04: GigaChat на «сохрани» ответил текстом без вызова инструмента, и сохранения не было."""
    def responder(body):
        system = body["messages"][0]["content"]
        if "модуль формализации" in system:
            last = body["messages"][-1]["content"]
            if "сохрани" in last:
                return chat_response(json.dumps({"intent": "save", "spec_patch": {}}))
            return chat_response(json.dumps(GOOD))
        return chat_response("Готово, сохранил.")                 # модель «сохранила» на словах
    scripted = ScriptedLLM(responder)
    install_mock(llm_settings, scripted)
    agent = Agent(llm_settings, repo)
    r = agent.handle(None, "сбер, цена выше SMA 50")
    r2 = agent.handle(r.session_id, "сохрани")
    assert r2.pending_action is not None and repo.list_research() == []
    assert agent.confirm(r2.pending_action["id"], approve=True)["ok"] and len(repo.list_research()) == 1


def _gigachat_failing(monkeypatch, handler):
    import httpx
    from moex_strategy_agent.agent.brain import LLMGateway
    from moex_strategy_agent.llm import provider as llm_provider
    monkeypatch.setattr(llm_provider, "TEST_TRANSPORT", httpx.MockTransport(handler))
    LLMGateway._clients.clear()


def test_network_timeout_explains_vpn_not_key(agent, monkeypatch):
    import httpx

    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)
    _gigachat_failing(monkeypatch, handler)
    r = agent.handle(None, CUSTOM)
    assert r.intent == "model_unavailable" and "llm_timeout" in r.flags
    assert "VPN" in r.text and "GIGACHAT_AUTH_KEY" not in r.text


def test_rejected_key_is_named_as_such(agent, monkeypatch):
    import httpx
    _gigachat_failing(monkeypatch, lambda request: httpx.Response(401, json={"message": "bad key"}))
    r = agent.handle(None, CUSTOM)
    assert "llm_auth" in r.flags and "отклонил GIGACHAT_AUTH_KEY" in r.text


def test_tls_failure_points_to_certificate(agent, monkeypatch):
    import httpx

    def handler(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=request)
    _gigachat_failing(monkeypatch, handler)
    r = agent.handle(None, CUSTOM)
    assert "llm_tls" in r.flags and "GIGACHAT_CA_BUNDLE" in r.text


def test_price_threshold_outside_range_asks_instead_of_empty_report(llm_settings, repo):
    """«Покупка ниже 30, продажа выше 70» записано как цена в рублях: агент переспрашивает про RSI."""
    price_30 = {"intent": "backtest", "spec_patch": {
        "ticker": "SBER", "start_date": "2023-01-01", "end_date": "2025-12-31", "strategy_type": "RULES",
        "rules": {"entry": {"type": "compare", "left": {"type": "price"}, "op": "<", "right": {"type": "const", "value": 30}},
                  "exit": {"type": "compare", "left": {"type": "price"}, "op": ">", "right": {"type": "const", "value": 70}}}}}
    install_mock(llm_settings, ScriptedLLM(_formalizer([price_30])))
    r = Agent(llm_settings, repo).handle(None, "Сбер покупка ниже 30 продажа выше 70")
    assert r.intent == "clarify" and "dead_entry_condition" in r.flags and r.run_id is None
    assert "не сработало ни разу" in r.text and "RSI ниже 30" in r.text and "RSI выше 70" in r.text
    assert repo.list_runs(10) == []                         # пустой запуск не засоряет историю


def test_yes_after_dead_entry_question_runs_rsi_on_same_ticker_and_period(llm_settings, repo):
    price_30 = {"intent": "backtest", "spec_patch": {
        "ticker": "SBER", "start_date": "2023-01-01", "end_date": "2025-12-31", "strategy_type": "RULES",
        "rules": {"entry": {"type": "compare", "left": {"type": "price"}, "op": "<", "right": {"type": "const", "value": 30}},
                  "exit": {"type": "compare", "left": {"type": "price"}, "op": ">", "right": {"type": "const", "value": 70}}}}}
    rsi = {"intent": "backtest", "spec_patch": {"strategy_type": "RSI", "parameters": {"period": 14, "lower": 30, "upper": 70}}}
    scripted = ScriptedLLM(_formalizer([price_30, rsi]))
    install_mock(llm_settings, scripted)
    agent = Agent(llm_settings, repo)
    r1 = agent.handle(None, "Сбер покупка ниже 30 продажа выше 70")
    r2 = agent.handle(r1.session_id, "да")
    assert r2.run_id and r2.intent != "clarify"
    spec = repo.get_run(r2.run_id).spec
    assert spec.ticker == "SBER" and str(spec.start_date) == "2023-01-01" and spec.strategy_type.value == "RSI"
    # модель видела свой вопрос и ответ человека
    und = [q for q in scripted.requests if "модуль формализации" in q["messages"][0]["content"]][-1]
    msgs = [m["content"] for m in und["messages"][1:]]
    assert any("RSI ниже 30" in m for m in msgs) and msgs[-1] == "да"
