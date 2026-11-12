"""Тупики диалога: агент не отказывает общей фразой, а задаёт вопрос с готовыми ответами."""
from __future__ import annotations

import json

from moex_strategy_agent.agent.graph import Agent
from tests.conftest import ScriptedLLM, chat_response, install_mock


def test_question_comes_with_ready_answers(agent):
    r = agent.handle(None, "Лукойл за 2022–2025, покупаю на просадках")
    assert r.intent == "clarify"
    assert r.suggestions == ["да, упала на 10% за 20 дней, продаю через 15 дней"]
    r2 = agent.handle(r.session_id, r.suggestions[0])
    assert r2.run_id, r2.text


def test_failed_formalization_asks_about_the_exact_place(llm_settings, repo):
    bad = {"intent": "backtest", "spec_patch": {"ticker": "SBER", "start_date": "2024-01-01",
                                                "end_date": "2024-12-31", "strategy_type": "MAGIC"}}
    ask = {"question": "Что считать перегревом: RSI выше 70?", "options": ["да, RSI выше 70", "рост на 15% за 10 дней"]}
    calls = {"n": 0}

    def responder(body):
        calls["n"] += 1
        return chat_response(json.dumps(ask if calls["n"] == 4 else bad, ensure_ascii=False))

    scripted = ScriptedLLM(responder)
    install_mock(llm_settings, scripted)
    agent = Agent(llm_settings, repo)
    r = agent.handle(None, "сбер за 2024, продаю на перегреве")
    assert r.intent == "clarify" and r.text == "Что считать перегревом: RSI выше 70?"
    assert r.suggestions == ["да, RSI выше 70", "рост на 15% за 10 дней"]
    # бумага и период из негодного ответа не потеряны: второй раз их не спросят
    draft = repo.get_state(r.session_id)["draft"]
    assert draft["ticker"] == "SBER" and draft["start_date"] == "2024-01-01"


def test_unknown_fields_from_model_are_traced(llm_settings, repo):
    ans = {"intent": "backtest", "spec_patch": {"ticker": "SBER", "strategy_type": "SMA_CROSSOVER",
                                                "parameters": {"fast_period": 20, "slow_period": 50},
                                                "stop_loss": 5}}
    install_mock(llm_settings, ScriptedLLM(lambda body: chat_response(json.dumps(ans))))
    r = Agent(llm_settings, repo).handle(None, "сбер sma 20 50 стоп 5")
    events = [e for e in repo.get_trace(r.trace_id) if e["name"] == "unknown_spec_fields"]
    assert events and "stop_loss" in json.dumps(events[0])


def test_short_is_not_replaced_silently_but_offered(agent, repo):
    r = agent.handle(None, "газпром за 2024: шорт при RSI выше 70, стоп 5%")
    assert r.intent == "unsupported" and r.run_id is None
    assert "только на покупку" in r.text and r.suggestions == ["да, только покупка"]
    r2 = agent.handle(r.session_id, r.suggestions[0])
    assert r2.run_id, r2.text
    assert repo.get_run(r2.run_id).spec.ticker == "GAZP"


def test_unsupported_without_fair_replacement_gets_no_offer(agent):
    r = agent.handle(None, "сбер, прогноз цены нейросетью")
    assert r.intent == "unsupported" and "Считать так?" not in r.text


def test_several_tickers_are_resolved_by_one_click(agent, repo):
    r = agent.handle(None, "SBER и GAZP за 2024 год, SMA 20/50")
    assert r.intent == "clarify" and r.suggestions == ["только SBER", "только GAZP"]
    r2 = agent.handle(r.session_id, "только GAZP")
    assert r2.run_id, r2.text
    spec = repo.get_run(r2.run_id).spec
    assert spec.ticker == "GAZP" and spec.strategy_type.value == "SMA_CROSSOVER" and spec.start_date.year == 2024


def test_no_trades_names_the_condition_and_offers_a_threshold(agent, repo):
    r = agent.handle(None, "SBER RSI 14, покупка ниже 2, продажа выше 98, за 2024 год")
    assert r.run_id and "не выполнилось ни разу" in r.text and "RSI(14) ниже 2" in r.text
    check = repo.get_run(r.run_id).diagnostics["entry_check"]
    assert check["entry_days"] == 0 and check["blocker"]["reach"] > 2
    relax = r.suggestions[0]
    assert relax.startswith("а если RSI(14) ниже ")
    r2 = agent.handle(r.session_id, relax)
    m = repo.get_run(r2.run_id).metrics
    assert m.trades_count > 0 or m.open_position_at_end, r2.text       # с новым порогом вход сработал


def test_no_trades_text_is_written_by_code_without_model(agent, repo, fake_llm):
    agent.handle(None, "SBER RSI 14, покупка ниже 2, продажа выше 98, за 2024 год")
    assert not any("объясняет результат" in q["messages"][0]["content"] for q in fake_llm.requests)


def test_stop_wider_than_take_profit_is_reported(llm_settings, repo):
    ans = {"intent": "backtest", "spec_patch": {"ticker": "SBER", "strategy_type": "RULES", "rules": {
        "entry": {"type": "compare", "left": {"type": "price"}, "op": ">", "right": {"type": "sma", "n": 50}},
        "stop_loss_pct": 7, "take_profit_pct": 3}}}
    install_mock(llm_settings, ScriptedLLM(lambda body: chat_response(
        json.dumps(ans) if "модуль формализации" in body["messages"][0]["content"] else "")))
    r = Agent(llm_settings, repo).handle(None, "сбер выше sma 50, стоп 7, тейк 3")
    assert r.run_id and "стоп-лосс 7% больше тейк-профита 3%" in r.text


def test_save_and_delete_commands_do_not_need_the_model(agent, repo, fake_llm):
    r = agent.handle(None, "SBER SMA 20/50 за 2024 год")
    before = len(fake_llm.requests)
    r2 = agent.handle(r.session_id, "сохрани")
    assert r2.intent == "save" and r2.pending_action and len(fake_llm.requests) == before
    agent.confirm(r2.pending_action["id"], True)
    rid = repo.list_research(10)[0]["id"]
    r3 = agent.handle(r.session_id, f"удали {rid}")
    assert r3.intent == "delete" and r3.pending_action and len(fake_llm.requests) == before


def test_stop_is_removed_by_code_even_if_model_keeps_it(llm_settings, repo):
    rules = {"entry": {"type": "compare", "left": {"type": "price"}, "op": ">", "right": {"type": "sma", "n": 50}},
             "max_hold_days": 10, "stop_loss_pct": 7}
    first = {"intent": "backtest", "spec_patch": {"ticker": "SBER", "strategy_type": "RULES", "rules": rules}}
    stubborn = {"intent": "modify", "spec_patch": {"strategy_type": "RULES", "rules": rules}}

    def responder(body):
        if "модуль формализации" not in body["messages"][0]["content"]:
            return chat_response("")
        return chat_response(json.dumps(stubborn if "без стоп" in body["messages"][-1]["content"] else first))

    install_mock(llm_settings, ScriptedLLM(responder))
    agent = Agent(llm_settings, repo)
    r = agent.handle(None, "сбер выше sma 50, держу 10 дней, стоп 7%")
    r2 = agent.handle(r.session_id, "а без стоп-лосса?")
    assert repo.get_run(r.run_id).spec.rules.stop_loss_pct == 7
    assert repo.get_run(r2.run_id).spec.rules.stop_loss_pct is None


def test_flipped_verdict_is_caught():
    from moex_strategy_agent.analysis.explanations import contradicts_verdict
    flipped = "Стратегия уступила простому держанию в основном из-за убытков. В 2025 году она отстала от рынка."
    assert contradicts_verdict(flipped, 10.23)
    assert not contradicts_verdict("В 2025 году стратегия отстала от «купить и держать». В целом стратегия "
                                   "обогнала «купить и держать» на 10,23 п.п.", 10.23)
    assert contradicts_verdict("Стратегия обогнала «купить и держать».", -5.0)
    assert not contradicts_verdict("Стратегия отстала от «купить и держать» на 5 п.п.", -5.0)
