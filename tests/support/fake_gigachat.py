"""Имитация GigaChat API для тестов и офлайн-прогона eval.

Отвечает на те же адреса, что и настоящий сервис: OAuth, /models, /chat/completions.
На запрос формализации возвращает JSON по контракту из prompts.UNDERSTAND:
сначала смотрит в сценарий (свои стратегии, уточнения), иначе разбирает текст
регулярками из scripted_parser. На объяснение и ReAct отвечает пустой строкой:
harness обязан сам перейти на шаблонный текст и прямые вызовы инструментов.

Это не модель и не замена модели. Живую проверку формализации делает eval на GigaChat.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from typing import Any, Callable

import httpx

from . import scripted_parser

P = {"type": "price"}


def C(v: float) -> dict:
    return {"type": "const", "value": v}


def _rules(entry: dict, exit: dict | None = None, **stops: Any) -> dict:
    out: dict[str, Any] = {"entry": entry}
    if exit:
        out["exit"] = exit
    out.update(stops)
    return out


# фраза (в нижнем регистре) -> ответ модели; ticker и период добавляются из текста регулярками
SCRIPT: dict[str, dict] = {
    "выше средней за 200 дней и rsi ниже 40": {"strategy_type": "RULES", "rules": _rules(
        {"type": "all", "conditions": [
            {"type": "compare", "left": P, "op": ">", "right": {"type": "sma", "n": 200}},
            {"type": "compare", "left": {"type": "rsi", "n": 14}, "op": "<", "right": C(40)}]},
        {"type": "compare", "left": {"type": "rsi", "n": 14}, "op": ">", "right": C(70)},
        stop_loss_pct=7), "assumptions": ["период RSI не назван, взят 14"]},
    "macd пересекает сигнальную": {"strategy_type": "RULES", "rules": _rules(
        {"type": "cross", "left": {"type": "macd"}, "direction": "above", "right": {"type": "macd", "line": "signal"}},
        {"type": "cross", "left": {"type": "macd"}, "direction": "below", "right": {"type": "macd", "line": "signal"}},
        max_hold_days=30), "assumptions": ["MACD 12, 26, 9"]},
    "нижней полосы боллинджера": {"strategy_type": "RULES", "rules": _rules(
        {"type": "compare", "left": P, "op": "<", "right": {"type": "bbands", "band": "lower"}},
        {"type": "compare", "left": P, "op": ">", "right": {"type": "bbands", "band": "middle"}},
        take_profit_pct=10)},
    "стохастик": {"strategy_type": "RULES", "rules": _rules(
        {"type": "all", "conditions": [
            {"type": "cross", "left": {"type": "stoch"}, "direction": "above", "right": {"type": "stoch", "line": "d"}},
            {"type": "compare", "left": {"type": "stoch"}, "op": "<", "right": C(20)}]},
        {"type": "compare", "left": {"type": "stoch"}, "op": ">", "right": C(80)},
        atr_stop={"n": 14, "mult": 2})},
    # неоднозначное условие: модель обязана спросить, а не выдумать
    "на просадках": {"question": "Как считать просадку: цена упала на 10% за 20 дней? И когда продавать?",
                     "options": ["да, упала на 10% за 20 дней, продаю через 15 дней"]},
    # пример стратегии без терминов из словаря
    "цена за 20 дней упала на 10%, продаю через 20 дней": {"strategy_type": "RULES", "rules": _rules(
        {"type": "compare", "left": {"type": "change", "n": 20}, "op": "<", "right": C(-10)}, max_hold_days=20)},
    # согласие на замену, которую агент предложил вместо неподдерживаемой части
    "да, только покупка": {"strategy_type": "RULES", "rules": _rules(
        {"type": "compare", "left": {"type": "rsi", "n": 14}, "op": "<", "right": C(30)},
        {"type": "compare", "left": {"type": "rsi", "n": 14}, "op": ">", "right": C(70)}, stop_loss_pct=5)},
    "да, на дневных свечах": {"strategy_type": "RULES", "rules": _rules(
        {"type": "cross", "left": {"type": "ema", "n": 12}, "direction": "above", "right": {"type": "ema", "n": 26}},
        {"type": "cross", "left": {"type": "ema", "n": 12}, "direction": "below", "right": {"type": "ema", "n": 26}},
        stop_loss_pct=3)},
    "да, упала на 10% за 20 дней, продаю через 15 дней": {"strategy_type": "RULES", "rules": _rules(
        {"type": "compare", "left": {"type": "change", "n": 20}, "op": "<", "right": C(-10)},
        max_hold_days=15)},
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace("ё", "е")).strip()


class FakeGigaChat:
    def __init__(self, script: dict[str, Any] | None = None,
                 narrate: Callable[[dict], str] | None = None):
        self.script = {**SCRIPT, **(script or {})}
        self.narrate = narrate
        self.requests: list[dict] = []
        self.oauth_calls = 0

    # ---------- HTTP ----------
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/v2/oauth"):
            self.oauth_calls += 1
            if not request.headers.get("Authorization", "").startswith("Basic ") or not request.headers.get("RqUID"):
                return httpx.Response(401, json={"message": "bad auth"})
            return httpx.Response(200, json={"access_token": "fake-token",
                                              "expires_at": int((time.time() + 1800) * 1000)})
        if request.headers.get("Authorization") != "Bearer fake-token":
            return httpx.Response(401, json={"message": "no token"})
        if url.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "GigaChat-2-Max"}, {"id": "GigaChat-2"}]})
        body = json.loads(request.content)
        self.requests.append(body)
        system = body["messages"][0]["content"]
        if "модуль формализации" in system:
            content = json.dumps(self.understand(body), ensure_ascii=False)
        elif self.narrate and "объясняет результат" in system:
            content = self.narrate(body)
        else:
            content = ""
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": len(json.dumps(body)) // 4, "completion_tokens": len(content) // 3}})

    # ---------- «формализация» ----------
    def understand(self, body: dict) -> dict:
        system = body["messages"][0]["content"]
        text = body["messages"][-1]["content"]
        m = re.search(r"Последняя спецификация в этой сессии: (.*)", system)
        last = json.loads(m.group(1)) if m and m.group(1).strip().startswith("{") else None
        low = _norm(text)

        # свои стратегии и уточнения по сценарию
        for key, ans in self.script.items():
            if key in low:
                base = scripted_parser.understand(text, today=date.today(), last_spec=None)
                patch = {k: v for k, v in base.spec_patch.items()
                         if k in ("ticker", "start_date", "end_date", "initial_cash", "commission", "slippage")}
                if ans.get("strategy_type"):
                    patch.update(strategy_type=ans["strategy_type"], rules=ans.get("rules"))
                return {"intent": "backtest", "spec_patch": patch, "assumptions": ans.get("assumptions", []),
                        "question": ans.get("question"), "options": ans.get("options", []),
                        "target_id": None, "reasoning": "по сценарию"}

        # «только SBER» после вопроса про несколько бумаг: стратегия из прошлой реплики, бумага из этой
        users = [m["content"] for m in body["messages"] if m["role"] == "user"]
        if low.startswith("только ") and len(users) >= 2:
            u = scripted_parser.understand(users[-2], today=date.today(), last_spec=None)
            patch = {**u.spec_patch, "ticker": scripted_parser.parse_ticker(text)}
            return {"intent": "backtest", "spec_patch": patch, "assumptions": [], "question": None,
                    "target_id": None, "reasoning": "одна бумага из нескольких"}

        # «а если RSI(14) ниже 25?»: ослабленный порог входа для стратегии RSI
        rm = re.search(r"а если rsi\(\d+\) ниже (\d+(?:[.,]\d+)?)", low)
        if rm and last and last.get("strategy_type") == "RSI":
            return {"intent": "modify", "spec_patch": {"strategy_type": "RSI", "parameters": {
                        "lower": float(rm.group(1).replace(",", "."))}},
                    "assumptions": [], "question": None, "target_id": None, "reasoning": "меняю порог"}

        # «а со стоп-лоссом 5%?» / «а без стоп-лосса?» к стратегии на правилах
        if last and last.get("strategy_type") == "RULES" and last.get("rules"):
            sm = re.search(r"стоп.?лосс\w*\s*(\d+(?:[.,]\d+)?)\s*%", low)
            if sm or "без стоп" in low:
                rules = dict(last["rules"])
                if sm:
                    rules["stop_loss_pct"] = float(sm.group(1).replace(",", "."))
                else:
                    rules.pop("stop_loss_pct", None)
                return {"intent": "modify", "spec_patch": {"strategy_type": "RULES", "rules": rules},
                        "assumptions": [], "question": None, "target_id": None, "reasoning": "меняю стоп"}

        u = scripted_parser.understand(text, today=date.today(), last_spec=last)
        return {"intent": u.intent, "spec_patch": u.spec_patch, "assumptions": u.assumptions,
                "question": u.question, "target_id": u.target_id, "reasoning": u.reasoning}
