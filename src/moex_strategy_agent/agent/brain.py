"""«Мозги» агента: понимание запроса, выбор инструментов, текст ответа.

LLMBrain: формализацию стратегии делает только модель (GigaChat, дальше цепочка из конфига).
Код проверяет каждый её ответ схемой и языком правил и возвращает ошибки на исправление.
FallbackBrain: шаблонный текст и прямые вызовы инструментов, когда модель сбилась на
объяснении или ответе на вопрос. Понимать стратегии он не умеет и не пытается.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from ..analysis.explanations import build_facts, contradicts_verdict, news_block, numbers_in, template_narrative
from ..config import Settings
from ..domain.schemas import BacktestResult
from ..llm.provider import LLMError, LLMResponse, OpenAICompatibleClient, build_client, parse_json_object
from ..telemetry.tracing import Tracer
from pydantic import ValidationError

from ..domain.rules import RuleSet, match_template, validate_rules
from . import prompts
from .guardrails import DISCLAIMER, Budget, leaks_prompt, safe_model_text, scrub_output, unsupported_numbers
from .state import INTENTS, Understanding
from .tools import REACT_TOOLS, TOOLS, ToolResult, ToolRunner

SPEC_KEYS = {"ticker", "board", "start_date", "end_date", "strategy_type", "parameters", "rules",
             "initial_cash", "commission", "slippage"}
TEMPLATES = {"SMA_CROSSOVER", "EMA_CROSSOVER", "RSI", "BREAKOUT", "BUY_AND_HOLD", "RULES"}


class ModelUnavailable(Exception):
    """Ни один провайдер из цепочки не ответил: формализовать стратегию нечем.

    kind берётся у первого провайдера цепочки (основного): именно его человек чинит.
    """

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


class LLMGateway:
    """Цепочка провайдеров из конфига. Возвращает None, если не ответил никто или кончился бюджет."""

    _clients: dict[str, OpenAICompatibleClient | None] = {}

    def __init__(self, settings: Settings, tracer: Tracer, budget: Budget):
        self.settings, self.tracer, self.budget = settings, tracer, budget
        self.used_provider = "none"
        self.failures: list[tuple[str, str]] = []

    def failure_kind(self) -> str:
        """Причина отказа основного провайдера; если он не пробовался, то первого упавшего."""
        if not self.failures:
            return "no_key" if not self.has_llm else "other"
        main = self.settings.fallback_chain[0] if self.settings.fallback_chain else None
        for name, kind in self.failures:
            if name == main:
                return kind
        return self.failures[0][1]

    def _client(self, name: str) -> OpenAICompatibleClient | None:
        if name not in self._clients:
            self._clients[name] = build_client(name, self.settings.provider_conf(name))
        return self._clients[name]

    @property
    def has_llm(self) -> bool:
        for name in self.settings.fallback_chain:
            try:
                if self._client(name) is not None:
                    return True
            except (LLMError, KeyError):
                continue
        return False

    def chat(self, messages: list[dict[str, Any]], *, purpose: str,
             tools: list[dict[str, Any]] | None = None) -> LLMResponse | None:
        if not self.budget.can_call_llm():
            self.tracer.event("budget", "llm_skipped", {"purpose": purpose, **self.budget.snapshot()})
            return None
        for name in self.settings.fallback_chain:
            try:
                client = self._client(name)
            except (LLMError, KeyError) as e:
                self.failures.append((name, getattr(e, "kind", "other")))
                self.tracer.event("error", "provider_unavailable", {"provider": name, "error": str(e)})
                continue
            if client is None:
                continue
            try:
                resp = client.chat(messages, tools=tools)
            except LLMError as e:
                self.failures.append((name, e.kind))
                self.tracer.event("error", "llm_failed", {"provider": name, "purpose": purpose, "error": str(e),
                                                          "kind": e.kind})
                continue
            cost = client.cost(resp.total_tokens)
            self.used_provider = name
            self.tracer.event("llm", purpose, {
                "provider": name, "model": resp.model,
                "prompt_tail": str(messages[-1].get("content", ""))[-300:],
                "response": resp.content[:600],
                "tool_calls": [{"name": t.name, "args": t.arguments} for t in resp.tool_calls]},
                tokens=resp.total_tokens, cost=cost, latency_ms=resp.latency_ms)
            self.budget.llm(resp.total_tokens, cost)
            return resp
        return None


# ---------------- правила ----------------


class FallbackBrain:
    """Запасной путь для объяснений и вопросов. Стратегии не понимает."""
    name = "fallback"

    def understand(self, text: str, *, today: date, last_spec: dict | None, summary: str,
                   dialog: list[dict[str, str]] | None = None) -> Understanding:
        raise ModelUnavailable("модель недоступна", kind="no_key")

    def narrate(self, res: BacktestResult, style: str = "full") -> str:
        return template_narrative(res)

    def followup(self, intent: str, text: str, run_id: str | None, state: dict[str, Any],
                 runner: ToolRunner, target_id: str | None) -> tuple[str, list[tuple[str, ToolResult]]]:
        calls: list[tuple[str, ToolResult]] = []

        def call(name: str, args: dict) -> ToolResult:
            r = runner.call(name, args, allowed=REACT_TOOLS)
            calls.append((name, r))
            return r

        if intent == "history":
            r = call("list_history", {"limit": 10})
            saved = len(r.data["saved"]) if r.ok else 0
            runs = len(r.data["recent_runs"]) if r.ok else 0
            return f"Сохранённых исследований: {saved}. Запусков в этой сессии: {runs}.", calls
        if intent == "delete":
            if not target_id or not target_id.startswith("r_"):
                r = call("list_history", {"limit": 10})
                return "Какое исследование удалить? Назовите его id (вида r_xxxxxxxx) из списка.", calls
            r = call("delete_research", {"research_id": target_id})
            if r.awaiting_confirmation:
                return "Удаление необратимо, поэтому жду вашего подтверждения.", calls
            call("list_history", {"limit": 10})
            return f"Не получилось: {r.error}. Вот что есть в сохранённом.", calls
        if not run_id:
            return "Сначала нужен хотя бы один бэктест: опишите стратегию, бумагу и период.", calls
        if intent == "save":
            res = runner.ctx.repo.get_run(run_id)
            title = res.spec.title() if res else run_id
            r = call("save_research", {"run_id": run_id, "title": title})
            return ("Сохраню исследование после вашего подтверждения." if r.awaiting_confirmation
                    else f"Не получилось: {r.error}"), calls
        if intent == "trades":
            order = "best" if "лучш" in text.lower() else "chronological" if "все" in text.lower() else "worst"
            r = call("get_trades", {"run_id": run_id, "order": order, "limit": 10})
            if not r.ok:
                return f"Не получилось достать сделки: {r.error}", calls
            if not r.data:
                return "Сделок не было.", calls
            w = r.data[0]
            label = {"worst": "Худшая", "best": "Лучшая", "chronological": "Первая"}[order]
            money = f"{w['pnl']:+,.0f}".replace(",", " ")
            return (f"{label} сделка: вход {w['entry']}, выход {w['exit']}, результат {money} ₽ "
                    f"({w['return_pct']:+.2f}%). Полный список в разделе «Сделки»."), calls
        if intent == "news":
            r = call("get_news_events", {"run_id": run_id})
            if not r.ok:
                return f"Новости не получены: {r.error}", calls
            ev = r.data["events"]
            with_news = [e for e in ev if e["news"]]
            text_out = (f"Резких движений цены: {len(ev)}, рядом с {len(with_news)} из них нашлись новости. "
                        "Совпадение по времени не означает, что новость стала причиной.")
            if len(with_news) < len(ev):
                text_out += " Для остальных дат подтверждённых новостей нет, объяснение я не придумываю."
            if "блокир" in r.data.get("status", "") or "заблокировано" in r.data.get("status", ""):
                text_out += " Одна из новостей содержала попытку управлять агентом и была скрыта."
            return text_out, calls
        if intent == "compare":
            prev = state.get("prev_run_id")
            if not prev:
                return "Сравнивать пока не с чем: в сессии один запуск.", calls
            r = call("compare_runs", {"run_id_a": run_id, "run_id_b": prev})
            if not r.ok:
                return f"Сравнение не удалось: {r.error}", calls
            m = r.data["metrics"]
            return (f"{r.data['a']} против {r.data['b']}: доходность {m['total_return_pct']['a']:+.2f}% "
                    f"против {m['total_return_pct']['b']:+.2f}%, просадка {m['max_drawdown_pct']['a']:.2f}% "
                    f"против {m['max_drawdown_pct']['b']:.2f}%."), calls
        # explain
        r = call("get_diagnostics", {"run_id": run_id, "section": "all"})
        if not r.ok:
            return f"Диагностика недоступна: {r.error}", calls
        findings = r.data.get("findings") or ["Явных слабых мест диагностика не нашла."]
        return " ".join(findings), calls


# ---------------- модель ----------------


class LLMBrain(FallbackBrain):
    name = "llm"

    def __init__(self, gateway: LLMGateway, settings: Settings):
        self.gw = gateway
        self.settings = settings
        self.rules = FallbackBrain()

    def understand(self, text: str, *, today: date, last_spec: dict | None, summary: str,
                   dialog: list[dict[str, str]] | None = None) -> Understanding:
        system = prompts.UNDERSTAND.format(today=today.isoformat(),
                                           last_spec=json.dumps(last_spec, ensure_ascii=False) if last_spec else "нет",
                                           summary=summary or "пусто")
        # незаконченное уточнение идёт настоящими репликами: модель видит свой вопрос и ответ человека
        messages = [{"role": "system", "content": system}, *(dialog or []), {"role": "user", "content": text}]
        repairs = int(self.settings.get("agent.formalize_repairs", 2))
        last_error = ""
        for attempt in range(repairs + 1):
            resp = self.gw.chat(messages, purpose="understand" if attempt == 0 else "understand_repair")
            if resp is None:
                raise ModelUnavailable("ни один провайдер не ответил", kind=self.gw.failure_kind())
            try:
                u = self._parse_understanding(resp.content)
                u.source = f"llm:{self.gw.used_provider}"
                if u.dropped:    # модель назвала поле, которого нет: молча терять его нельзя
                    self.gw.tracer.event("guardrail", "unknown_spec_fields", {"fields": u.dropped})
                if attempt:
                    u.reasoning = f"исправлено с попытки {attempt + 1}: {u.reasoning}"[:300]
                return u
            except (ValueError, ValidationError, json.JSONDecodeError) as e:
                last_error = _short_error(e)
                self.gw.tracer.event("guardrail", "formalize_rejected", {"attempt": attempt, "error": last_error})
                messages += [{"role": "assistant", "content": resp.content},
                             {"role": "user", "content": prompts.UNDERSTAND_REPAIR.format(error=last_error)}]
        # модель так и не дала корректных правил: не выдумываем, а спрашиваем о том месте, где застряли
        question, options = self._giveup_question(messages, last_error)
        return Understanding(intent="backtest", spec_patch=_salvage(resp.content), question=question,
                             options=options, reasoning=f"формализация не удалась: {last_error}"[:300],
                             source="llm:failed")

    GIVEUP_QUESTION = ("Не смог однозначно записать стратегию правилами. Опишите проще: когда покупать и когда "
                       "продавать, например «покупаю, когда цена выше средней за 50 дней, продаю при обратном "
                       "пересечении».")

    def _giveup_question(self, messages: list[dict[str, Any]], error: str) -> tuple[str, list[str]]:
        """Последний вызов: модель называет место, которое не смогла записать, и варианты ответа."""
        resp = self.gw.chat(messages[:-1] + [{"role": "user", "content": prompts.UNDERSTAND_GIVEUP.format(error=error)}],
                            purpose="understand_giveup")
        try:
            js = parse_json_object(resp.content) if resp else {}
        except (ValueError, json.JSONDecodeError):
            js = {}
        question = safe_model_text(js.get("question") or "")
        if not question:
            return self.GIVEUP_QUESTION, []
        return question, _options(js.get("options"))

    @staticmethod
    def _parse_understanding(content: str) -> Understanding:
        js = parse_json_object(content)
        intent = js.get("intent")
        if intent not in INTENTS:
            raise ValueError(f"intent должен быть одним из {', '.join(INTENTS)}, а не {intent!r}")
        raw = js.get("spec_patch") or {}
        patch = {k: v for k, v in raw.items() if k in SPEC_KEYS and v not in (None, "", {}, [])}
        dropped = sorted(k for k in raw if k not in SPEC_KEYS)
        question = js.get("question") or None
        if question and safe_model_text(question) is None:
            # вопрос с чужим текстом или куском промпта человеку не показываем
            question = "Уточните стратегию: когда покупать и когда продавать?"
            js["options"] = []
        elif question:
            question = safe_model_text(question)
        st = patch.get("strategy_type")
        if st is not None and st not in TEMPLATES:
            raise ValueError(f"strategy_type {st!r} не существует: используй шаблон или RULES")
        if "rules" in patch and st not in (None, "RULES"):
            patch.pop("rules")                     # шаблон сам построит свои правила
        if st == "RULES" or "rules" in patch:
            if "rules" not in patch:
                if not question:
                    raise ValueError("strategy_type RULES без поля rules: заполни rules или задай вопрос")
                patch.pop("strategy_type", None)
            else:
                rs = RuleSet.model_validate(_coerce_numbers(patch["rules"]))  # схема: лишнего и чужого не пропустит
                errs = validate_rules(rs)
                if errs:
                    raise ValueError("; ".join(errs))
                tpl = match_template(rs)
                if tpl:                            # то же дерево, что у шаблона: берём шаблон
                    patch["strategy_type"], patch["parameters"] = tpl[0], tpl[1]
                    patch.pop("rules")
                else:
                    patch["strategy_type"] = "RULES"
                    patch["rules"] = rs.model_dump(mode="json", exclude_none=True)
        if question and "rules" in patch:
            question = None                        # правила собраны: вопрос лишний, считаем сразу
        assumptions = [t for t in (safe_model_text(a, 200, strict=True) for a in (js.get("assumptions") or []) if a) if t][:5]
        return Understanding(intent=intent, spec_patch=patch, question=question, assumptions=assumptions,
                             options=_options(js.get("options")) if question else [], dropped=dropped,
                             target_id=js.get("target_id"), reasoning=str(js.get("reasoning", ""))[:300])

    def narrate(self, res: BacktestResult, style: str = "full") -> str:
        facts = build_facts(res)
        allowed = numbers_in(facts)
        content = prompts.NARRATE.format(length=prompts.NARRATE_LENGTH[style if style in prompts.NARRATE_LENGTH else "full"], facts=json.dumps(facts, ensure_ascii=False, indent=1),
                                         news=news_block(res))
        messages = [{"role": "system", "content": content},
                    {"role": "user", "content": "Объясни результат бэктеста."}]
        retries = int(self.settings.get("agent.narrative_retries", 1))
        for attempt in range(retries + 1):
            resp = self.gw.chat(messages, purpose="narrate" if attempt == 0 else "narrate_reflexion")
            if resp is None or not resp.content.strip():
                break
            text, _ = scrub_output(resp.content.strip())
            bad = unsupported_numbers(text, allowed)
            flipped = contradicts_verdict(text, res.metrics.total_return_pct - res.benchmark.total_return_pct)
            self.gw.tracer.event("guardrail", "fact_check", {"attempt": attempt, "unsupported": bad[:10],
                                                             "verdict_flipped": flipped})
            if not bad and not flipped:
                return text if DISCLAIMER in text else f"{text} {DISCLAIMER}"
            note = prompts.NARRATE_RETRY.format(bad=bad[:10], previous="(см. выше)") if bad else \
                f"В прошлом ответе перепутано сравнение за весь период. Верно так: {facts['verdict']}. Перепиши текст."
            messages += [{"role": "assistant", "content": resp.content}, {"role": "user", "content": note}]
        self.gw.tracer.event("guardrail", "narrative_fallback", {"reason": "модель недоступна или выдумывала числа"})
        return self.rules.narrate(res)

    def followup(self, intent: str, text: str, run_id: str | None, state: dict[str, Any],
                 runner: ToolRunner, target_id: str | None) -> tuple[str, list[tuple[str, ToolResult]]]:
        """ReAct: модель сама выбирает инструменты, harness ограничивает список и число шагов.

        Сохранение, удаление, история и сравнение идут напрямую: действие, которое ждёт
        подтверждения человека, не должно зависеть от того, решит ли модель вызвать инструмент.
        """
        if intent in ("save", "delete", "history", "compare"):
            return self.rules.followup(intent, text, run_id, state, runner, target_id)
        tools = [TOOLS[n].schema() for n in sorted(REACT_TOOLS)]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompts.REACT.format(run_id=run_id or "нет", today=date.today())},
            {"role": "user", "content": text}]
        calls: list[tuple[str, ToolResult]] = []
        evidence: list[Any] = []
        if run_id:
            res = runner.ctx.repo.get_run(run_id)
            if res:
                evidence.append(build_facts(res))
        for _ in range(4):
            resp = self.gw.chat(messages, purpose="react", tools=tools)
            if resp is None:
                return self.rules.followup(intent, text, run_id, state, runner, target_id)
            if not resp.tool_calls:
                answer, _ = scrub_output(resp.content.strip())
                bad = unsupported_numbers(answer, numbers_in(evidence))
                if bad or not answer or leaks_prompt(answer):
                    self.gw.tracer.event("guardrail", "react_fact_check_failed", {"unsupported": bad[:10]})
                    return self.rules.followup(intent, text, run_id, state, runner, target_id)
                return answer, calls
            messages.append({"role": "assistant", "content": resp.content, "tool_calls": resp.tool_calls})
            for tc in resp.tool_calls[:3]:
                r = runner.call(tc.name, tc.arguments, allowed=REACT_TOOLS)
                calls.append((tc.name, r))
                if r.ok:
                    evidence.append(r.data)
                payload = json.dumps(r.for_llm(), ensure_ascii=False, default=str)[:6000]
                if tc.name in ("get_news_events", "find_news", "list_history"):   # заголовки и названия: чужой текст
                    from .guardrails import wrap_untrusted
                    payload = wrap_untrusted(tc.name, payload)
                messages.append({"role": "tool", "name": tc.name, "tool_call_id": tc.id, "content": payload})
        return self.rules.followup(intent, text, run_id, state, runner, target_id)


def _options(raw: Any) -> list[str]:
    """Готовые ответы на уточнение: короткие строки, не больше трёх."""
    out: list[str] = []
    for o in raw if isinstance(raw, list) else []:
        text = safe_model_text(o, 90) if isinstance(o, str) else None
        if text and text not in out:
            out.append(text)
    return out[:3]


def _salvage(content: str) -> dict[str, Any]:
    """Из ответа с негодными правилами берём то, что к правилам не относится: бумагу, период, деньги."""
    try:
        raw = parse_json_object(content).get("spec_patch") or {}
    except (ValueError, json.JSONDecodeError, AttributeError):
        return {}
    keep = ("ticker", "board", "start_date", "end_date", "initial_cash", "commission", "slippage")
    return {k: raw[k] for k in keep if raw.get(k) not in (None, "", {}, [])}


def _coerce_numbers(node: Any) -> Any:
    """Модели пишут «right": 30» вместо константы. Это однозначно, поэтому чиним кодом, а не запросом."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("left", "right") and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = {"type": "const", "value": v}
            else:
                out[k] = _coerce_numbers(v)
        return out
    if isinstance(node, list):
        return [_coerce_numbers(x) for x in node]
    return node


def _short_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        parts = [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5]]
        return "схема правил: " + "; ".join(parts)
    return str(e)[:400]
