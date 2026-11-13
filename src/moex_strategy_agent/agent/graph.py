"""Граф агента: явные узлы, переходы и лимиты. Цикл пишем сами, без фреймворка.

    guard_input ─► understand ─► route
       route=backtest|modify ─► build_spec ─► validate ─► collect_data ─► execute_backtest
                                   │clarify      │clarify     │search_security/clarify
                                   ▼             ▼            ▼
                                 ответ        ответ        ответ
                               ─► verify ─► investigate(news) ─► narrate(+fact-check, reflexion) ─► respond
       route=explain|news|trades|history|compare|save|delete ─► ReAct по read-инструментам ─► respond
       route=help|out_of_scope ─► respond

Критичные вычисления (данные, бэктест, сверка) идут в фиксированном порядке.
Модель решает: что хотел пользователь, какие read-инструменты вызвать, как объяснить.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError

from ..analysis.sanity import dead_entry_hint
from .typography import tidy_prose
from ..config import Settings
from ..data.market_data import MarketData
from ..data.news_client import NewsService
from ..domain.schemas import StrategySpec
from ..domain.validators import validate_spec
from ..persistence.repository import Repository
from ..telemetry.tracing import Tracer
from .brain import FallbackBrain, LLMBrain, LLMGateway, ModelUnavailable
from ..analysis.explanations import no_trades_narrative, ru_decimals
from ..analysis.sanity import no_trades_suggestion
from . import glossary
from . import scope as scope_guard
from .guardrails import DISCLAIMER, Budget, BudgetExceeded, check_user_input
from .state import AgentReply, Understanding
from .tools import TOOLS, ToolContext, ToolResult, ToolRunner

HELP = ("Я проверяю торговые стратегии на истории акций Мосбиржи. Опишите словами бумагу, период и "
        "правила, например: «Сбер за 2021-2025, покупаю, когда цена выше средней за 200 дней и RSI ниже 40, "
        "продаю при RSI выше 70, стоп 7%». Понимаю цену и объём, SMA, EMA, RSI, MACD, полосы Боллинджера, "
        "стохастик, ATR, максимумы и минимумы за N дней, условия через «и» и «или», стоп-лосс, тейк-профит, "
        "трейлинг-стоп и выход через N дней. Если что-то в описании неоднозначно, переспрошу. "
        "Считаю доходность, просадку, сделки, сравниваю с удержанием, проверяю расчёт второй независимой "
        "реализацией и ищу новости вокруг резких движений. Реальных сделок не совершаю.")

_MODEL_TAIL = " Сохранённые исследования и посчитанные результаты открываются и без модели."
MODEL_UNAVAILABLE_BY_KIND = {
    "no_key": ("В файле .env нет GIGACHAT_AUTH_KEY, а без модели я не перевожу описание стратегии в правила: "
               "угадывать по ключевым словам не стану. Добавьте ключ и перезапустите сервер."),
    "auth": ("Сервер авторизации отклонил GIGACHAT_AUTH_KEY. Ключ копируется из личного кабинета целиком, "
             "без кавычек и пробелов. Скрипт scripts/check_gigachat.py покажет, на каком шаге отказ."),
    "timeout": ("Ответа не дождался. Чаще всего так бывает с включённым VPN: серверы Сбера "
                "не любят зарубежные адреса. Выключите VPN или добавьте *.sberbank.ru в исключения "
                "и повторите запрос."),
    "network": ("Соединение не установилось. Проверьте интернет и VPN: с зарубежного адреса серверы Сбера "
                "часто недоступны. Скрипт scripts/check_gigachat.py покажет, где обрыв."),
    "tls": ("Сбер подписывает соединение сертификатом НУЦ Минцифры, а система его не знает. "
            "Укажите путь к сертификату в GIGACHAT_CA_BUNDLE, как описано в README."),
    "limit": "Подождите минуту и повторите. Если не помогает, проверьте остаток токенов в личном кабинете.",
    "server": "Сбой на стороне Сбера, а не у вас. Повторите запрос через пару минут.",
    "other": ("Языковая модель сейчас недоступна, а без неё я не перевожу описание стратегии в правила. "
              "Скрипт scripts/check_gigachat.py покажет, что не так."),
}
MODEL_UNAVAILABLE = MODEL_UNAVAILABLE_BY_KIND["other"] + _MODEL_TAIL


def model_unavailable_text(kind: str) -> str:
    return MODEL_UNAVAILABLE_BY_KIND.get(kind, MODEL_UNAVAILABLE_BY_KIND["other"]) + _MODEL_TAIL


OUT_OF_SCOPE = ("Это не ко мне: я проверяю торговые стратегии на исторических данных Мосбиржи. "
                "Опишите стратегию, и я её протестирую.")

TRADING_REFUSAL = ("Реальные сделки я не совершаю и доступа к брокерскому счёту не имею: это запрещено "
                   "на уровне кода, а не настроения. Могу проверить ту же идею на истории и показать, "
                   "как она бы сработала.")

INJECTION_REFUSAL = ("Похоже на попытку поменять мои правила. Они зашиты в код и из чата не меняются. "
                     "Если нужен бэктест, опишите стратегию обычными словами.")


def u_text(u: Understanding, state: dict[str, Any]) -> str:
    return state.get("_last_text", "")


class Agent:
    def __init__(self, settings: Settings, repo: Repository, market: MarketData | None = None,
                 news: NewsService | None = None):
        self.settings = settings
        self.repo = repo
        self.market = market or MarketData(settings)
        self.news = news or NewsService(settings)

    # ---------- вход ----------

    def handle(self, session_id: str | None, text: str, on_event: Any = None,
               default_cash: float | None = None, style: str = "full") -> AgentReply:
        sid = self.repo.ensure_session(session_id)
        state = self.repo.get_state(sid)
        if default_cash:
            state["_default_cash"] = float(default_cash)     # из настроек интерфейса, только на этот запрос
        state["_style"] = "short" if style == "short" else "full"
        tracer = Tracer(self.repo, sid, self.settings.path("app.logs_dir"), listener=on_event)
        budget = Budget(**{k: self.settings.get(f"agent.{v}") for k, v in {
            "max_steps": "max_steps", "max_tool_calls": "max_tool_calls", "max_llm_calls": "max_llm_calls",
            "max_tokens": "max_tokens_per_request", "max_cost_rub": "max_cost_rub_per_request"}.items()})
        gateway = LLMGateway(self.settings, tracer, budget)
        brain: FallbackBrain = LLMBrain(gateway, self.settings) if gateway.has_llm else FallbackBrain()
        ctx = ToolContext(self.settings, self.market, self.news, self.repo, sid)
        runner = ToolRunner(ctx, tracer, budget)
        reply = AgentReply(session_id=sid, trace_id=tracer.trace_id, text="", intent="")
        self.repo.add_message(sid, "user", text, {"trace_id": tracer.trace_id})
        tracer.event("node", "start", {"text": text[:500], "brain": brain.name,
                                       "provider_chain": self.settings.fallback_chain})
        try:
            self._run(text, state, brain, runner, tracer, budget, reply)
        except ModelUnavailable as e:
            tracer.event("error", "model_unavailable", {"error": str(e), "chain": self.settings.fallback_chain})
            reply.intent = "model_unavailable"
            reply.flags.append("model_unavailable")
            reply.flags.append(f"llm_{e.kind}")
            reply.text = model_unavailable_text(e.kind)
        except BudgetExceeded as e:
            tracer.event("budget", "exceeded", {"reason": str(e), **budget.snapshot()})
            reply.flags.append("budget_exceeded")
            reply.text = (reply.text + "\n\n" if reply.text else "") + \
                f"Остановился: исчерпан {e}. Сузьте запрос или повторите позже."
        except Exception as e:  # noqa: BLE001, агент отвечает честно, а не падает
            tracer.event("error", "unhandled", {"error": f"{e.__class__.__name__}: {e}"})
            reply.flags.append("internal_error")
            reply.text = f"Внутренняя ошибка ({e.__class__.__name__}). Запрос сохранён в трассе {tracer.trace_id}."
        try:
            reply.suggestions = self._suggestions(reply, state, text)
        except Exception:  # noqa: BLE001, подсказки не должны ломать ответ
            reply.suggestions = []
        reply.text = tidy_prose(reply.text)
        state["last_intent"] = reply.intent
        reply.budget = budget.snapshot()
        reply.tools_called = runner.called
        reply.provider = gateway.used_provider
        state.pop("_default_cash", None)
        state.pop("_style", None)
        self.repo.set_state(sid, state)
        self.repo.add_message(sid, "assistant", reply.text,
                              {"trace_id": tracer.trace_id, "run_id": reply.run_id, "blocks": reply.blocks,
                               "intent": reply.intent, "flags": reply.flags, "suggestions": reply.suggestions})
        tracer.event("summary", "finish", {"intent": reply.intent, "flags": reply.flags,
                                           "tools": runner.called, **budget.snapshot()},
                     tokens=0, cost=0.0)
        return reply

    # ---------- граф ----------

    def _run(self, text: str, state: dict[str, Any], brain: FallbackBrain, runner: ToolRunner,
             tracer: Tracer, budget: Budget, reply: AgentReply) -> None:
        budget.step()
        state["_last_text"] = text
        verdict = check_user_input(text)
        tracer.event("node", "guard_input", {"blocked": verdict.blocked, "reason": verdict.reason,
                                             "trading": verdict.trading, "injection": verdict.injection})
        if verdict.blocked:
            reply.intent = "rejected"
            reply.text = "Пустое сообщение." if verdict.reason == "empty" else \
                "Слишком длинное сообщение: опишите стратегию короче, до 2000 символов."
            return
        if verdict.trading:
            reply.intent, reply.text = "out_of_scope", TRADING_REFUSAL
            reply.flags.append("trading_refused")
            tracer.event("guardrail", "trading_refused", {})
            return
        if verdict.injection:
            reply.intent, reply.text = "out_of_scope", INJECTION_REFUSAL
            reply.flags.append("injection_blocked")
            tracer.event("guardrail", "user_injection_blocked", {})
            return

        # вопрос о терминах: объяснение из словаря, модель не нужна
        kind = glossary.detect(verdict.text)
        if kind:
            reply.intent = "glossary"
            reply.text, state["_options"] = glossary.answer(kind)
            tracer.event("node", "glossary", {"kind": kind})
            state.pop("dialog", None)
            return

        # границы возможностей проверяет код, до модели: агент не подменяет запрос похожим
        scope = scope_guard.scope_check(verdict.text)
        if scope:
            tracer.event("guardrail", "scope_" + scope["kind"], scope)
            state.pop("draft", None)
            state.pop("dialog", None)
            tickers = scope_guard.find_tickers(verdict.text)
            if scope["kind"] == "foreign":
                reply.intent = "unsupported"
                reply.flags.append("foreign_security")
                what = "Криптовалюты" if scope["name"] == "криптовалюта" else scope["name"]
                reply.text = (f"{what} на Мосбирже в основном режиме торгов нет, а котировки я беру только оттуда: "
                              "это российские акции. Иностранные бумаги, индексы и криптовалюту проверить не смогу. "
                              "Могу посчитать ту же идею на российской акции.")
                state["_options"] = list(scope_guard.FOREIGN_OPTIONS)
            elif scope["kind"] == "unsupported":
                reply.intent = "unsupported"
                reply.flags.append("unsupported_strategy")
                if scope.get("offer"):
                    # замену предлагаем вопросом; согласие придёт следующей репликой вместе с этим диалогом
                    question, answer = scope["offer"]
                    reply.text = f"Пока не умею: {scope['name']}. {question}"
                    state["_options"] = [answer]
                    if len(tickers) == 1:
                        state["draft"] = {"ticker": tickers[0]}
                    state["dialog"] = [{"role": "user", "content": verdict.text},
                                       {"role": "assistant", "content": reply.text}]
                else:
                    reply.text = (f"Пока не умею: {scope['name']}. Ближайшее, что могу посчитать: "
                                  f"{scope['nearest']}. Что умею: {scope_guard.SUPPORTED_TEXT}.")
            else:
                reply.intent = "clarify"
                reply.flags.append("multi_request")
                reply.text = (f"В одном сообщении несколько бумаг ({', '.join(tickers)}). Считаю по одной, "
                              "чтобы ничего не перепутать. С какой начнём?")
                state["_options"] = [f"только {t}" for t in tickers[:3]]
                state["dialog"] = [{"role": "user", "content": verdict.text},
                                   {"role": "assistant", "content": reply.text}]
            return

        budget.step()
        tracer.event("stage", "understand", {})
        u = self._command(verdict.text, state) or brain.understand(
            verdict.text, today=date.today(), last_spec=state.get("last_spec"),
            summary="; ".join(state.get("summary", [])[-5:]), dialog=state.get("dialog"))
        if u.options and u.question:
            state["_options"] = u.options
        # незавершённое уточнение: следующая реплика дополняет черновик
        if state.get("draft") and u.intent in ("backtest", "modify", "out_of_scope") and u.spec_patch:
            u.spec_patch = {**state["draft"], **u.spec_patch,
                            "parameters": {**state["draft"].get("parameters", {}), **u.spec_patch.get("parameters", {})}}
            u.intent, u.question = "backtest", None
        tracer.event("node", "understand", {"intent": u.intent, "patch": u.spec_patch, "source": u.source,
                                            "question": u.question, "reasoning": u.reasoning})
        reply.intent = u.intent

        if u.intent not in ("backtest", "modify"):
            state.pop("dialog", None)          # разговор ушёл от уточнения стратегии
        if u.intent in ("backtest", "modify"):
            self._flow_backtest(u, state, brain, runner, tracer, budget, reply)
            if reply.intent == "clarify":
                # вопрос и ответ живут в диалоге: следующую реплику модель прочтёт вместе с ними
                d = state.get("dialog") or []
                d += [{"role": "user", "content": verdict.text}, {"role": "assistant", "content": reply.text}]
                state["dialog"] = d[-6:]
            else:
                state.pop("dialog", None)
        elif u.intent == "unsupported":
            state.pop("draft", None)
            reply.flags.append("unsupported_strategy")
            if u.question and u.options:
                # модель предложила замену: бумага и вопрос остаются до ответа человека
                reply.text = u.question
                if u.spec_patch.get("ticker"):
                    state["draft"] = {"ticker": u.spec_patch["ticker"]}
                state["dialog"] = [{"role": "user", "content": verdict.text},
                                   {"role": "assistant", "content": reply.text}]
            else:
                reply.text = (u.question or "Такую стратегию я пока не умею записать правилами.") + \
                    f" Что умею: {scope_guard.SUPPORTED_TEXT}."
        elif u.intent == "help":
            if state.get("last_intent") == "help":
                # справка уже была: повторять её бесполезно, спрашиваем, что именно неясно
                reply.text = ("Что именно непонятно: как описать стратегию или что значат обозначения "
                              "вроде SMA и RSI?")
                state["_options"] = ["покажи пример стратегии простыми словами", "что значат SMA, RSI и стоп-лосс?"]
            else:
                reply.text = HELP
        elif u.intent == "out_of_scope":
            reply.text = OUT_OF_SCOPE
        else:
            budget.step()
            tracer.event("stage", "answer", {"intent": u.intent})
            answer, calls = brain.followup(u.intent, verdict.text, state.get("last_run_id"), state, runner,
                                           u.target_id)
            tracer.event("node", "react", {"intent": u.intent, "tools": [c[0] for c in calls]})
            reply.text = ru_decimals(answer)
            self._blocks_from_calls(calls, state, reply)

    _SAVE = re.compile(r"^\s*сохрани(ть)?(\s+(это|его|результат|исследование|запуск))?[\s.!]*$", re.IGNORECASE)
    _DELETE = re.compile(r"^\s*удали(ть)?\s+(?:исследование\s+)?(r_[0-9a-f]{8})[\s.!]*$", re.IGNORECASE)

    @classmethod
    def _command(cls, text: str, state: dict[str, Any]) -> Understanding | None:
        """Однозначные команды с подтверждением человека разбирает код: «сохрани», «удали r_...».

        Действие, которое меняет сохранённое, не должно зависеть от того, как модель поняла слово.
        """
        if cls._SAVE.match(text) and state.get("last_run_id"):
            return Understanding(intent="save", source="code", reasoning="команда сохранения")
        m = cls._DELETE.match(text)
        if m:
            return Understanding(intent="delete", target_id=m.group(2).lower(), source="code",
                                 reasoning="команда удаления")
        return None

    # ---------- ветка бэктеста ----------

    def _merge_spec(self, u: Understanding, state: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        base = dict(state.get("last_spec") or {}) if u.intent == "modify" else {}
        patch = dict(u.spec_patch)
        assumptions: list[str] = list(u.assumptions)
        last = state.get("last_spec")
        if u.intent == "backtest" and last and "ticker" not in patch and "strategy_type" in patch:
            # новая стратегия без бумаги: продолжаем разговор о прошлой бумаге и периоде
            for k in ("ticker", "board", "start_date", "end_date", "initial_cash", "commission", "slippage"):
                if k not in patch and k in last:
                    base[k] = last[k]
            assumptions.append(f"бумага и период взяты из прошлого запуска: {last['ticker']}, "
                               f"с {last['start_date']} по {last['end_date']}")
        merged = {**base, **{k: v for k, v in patch.items() if k != "parameters"}}
        if "strategy_type" in patch and patch["strategy_type"] != base.get("strategy_type"):
            merged["parameters"] = dict(patch.get("parameters") or {})
        else:
            merged["parameters"] = {**base.get("parameters", {}), **(patch.get("parameters") or {})}
        today = date.today()
        if "start_date" not in merged or "end_date" not in merged:
            merged.setdefault("end_date", today.isoformat())
            merged.setdefault("start_date", (today - timedelta(days=3 * 365)).isoformat())
            assumptions.append(f"период не указан, взяты последние 3 года: с {merged['start_date']} по {merged['end_date']}")
        if "initial_cash" not in merged:
            merged["initial_cash"] = state.get("_default_cash") or self.settings.get("backtest.default_cash", 100000)
            cash = f"{merged['initial_cash']:,.0f}".replace(",", " ")
            assumptions.append(f"капитал не указан, взято {cash} ₽")
        merged.setdefault("commission", self.settings.get("backtest.default_commission", 0.0005))
        merged.setdefault("slippage", self.settings.get("backtest.default_slippage", 0.0005))
        merged.setdefault("board", self.settings.get("data.default_board", "TQBR"))
        if u.intent == "modify":
            names = {"parameters": "параметры", "start_date": "период", "end_date": "период", "ticker": "бумага",
                     "strategy_type": "стратегия", "initial_cash": "капитал", "commission": "комиссия",
                     "slippage": "проскальзывание", "board": "режим торгов"}
            changed = list(dict.fromkeys(names.get(k, k) for k in sorted(patch)))
            assumptions.append("относительно прошлого запуска изменено: " + ", ".join(changed))
        return merged, assumptions

    def _flow_backtest(self, u: Understanding, state: dict[str, Any], brain: FallbackBrain, runner: ToolRunner,
                       tracer: Tracer, budget: Budget, reply: AgentReply) -> None:
        budget.step()
        merged, assumptions = self._merge_spec(u, state)
        # «а без стоп-лосса?»: просьба однозначна, поэтому поле убирает код, даже если модель его оставила
        low = u_text(u, state).lower()
        if u.intent == "modify" and isinstance(merged.get("rules"), dict):
            for pat, fld in ((r"без стоп", "stop_loss_pct"), (r"без тейк", "take_profit_pct"),
                             (r"без трейлинг", "trailing_stop_pct")):
                if re.search(pat, low) and merged["rules"].get(fld) is not None:
                    merged["rules"] = {k: v for k, v in merged["rules"].items() if k != fld}
        tracer.event("node", "build_spec", {"merged": merged, "assumptions": assumptions})

        # «давай торговать»: сразу честно говорим, что умеем только историю
        lead = ""
        if re.search(r"торгова|торгуй|торгуем", u_text(u, state).lower()):
            lead = "Реально торговать я не умею, но могу проверить идею на истории. "

        # период целиком в будущем: цен ещё нет, предлагаем такой же по длине прошлый период
        start_s, end_s = merged.get("start_date"), merged.get("end_date")
        min_days = int(self.settings.get("data.min_period_days", 60))
        if start_s and end_s and date.fromisoformat(str(end_s)) > date.today() and \
                (date.today() - date.fromisoformat(str(start_s))).days < min_days:
            years = max(1, round((date.fromisoformat(str(merged["end_date"])) - date.fromisoformat(str(start_s))).days / 365))
            state["draft"] = {k: v for k, v in merged.items()
                              if k in u.spec_patch and k not in ("start_date", "end_date")}
            reply.intent = "clarify"
            reply.flags.append("future_period")
            word = "год" if years == 1 else "года" if years < 5 else "лет"
            reply.text = (lead + f"Этот период ещё не наступил, а бэктест считает только на прошлых ценах. "
                          f"Могу проверить ту же идею за последние {years} {word}." +
                          ("" if merged.get("strategy_type") else " Какую стратегию взять: когда покупать и когда продавать?"))
            state["future_years"] = years
            return

        # validate & clarify: без бумаги и стратегии не придумываем
        missing = [k for k in ("ticker", "strategy_type") if not merged.get(k)]
        if missing or u.question:
            state["draft"] = {k: v for k, v in merged.items() if k in u.spec_patch or k == "parameters"}
            reply.intent = "clarify"
            if "ticker" in missing and not u.question:
                reply.text = lead + "Какую бумагу тестируем? Назовите тикер или компанию, например SBER или «Газпром»."
            else:
                reply.text = lead + (u.question or ("Когда покупать и когда продавать? Например: «покупаю, когда "
                                                    "цена выше средней за 50 дней, продаю при обратном пересечении», "
                                                    "или возьмите готовое: SMA 20/50, RSI 14 30/70."))
            reply.blocks.append({"type": "clarify", "missing": missing})
            return
        # «быстрая 50, медленная 20»: смысл однозначен, переставляем кодом и говорим об этом
        p = merged.get("parameters") or {}
        if merged.get("strategy_type") in ("SMA_CROSSOVER", "EMA_CROSSOVER") and \
                p.get("fast_period") and p.get("slow_period") and p["fast_period"] > p["slow_period"]:
            p["fast_period"], p["slow_period"] = p["slow_period"], p["fast_period"]
            assumptions.append(f"периоды переставлены: быстрая средняя {p['fast_period']:g}, медленная {p['slow_period']:g}")
        try:
            spec = StrategySpec.model_validate(merged)
        except ValidationError as e:
            errs = [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:4]]
            tracer.event("node", "validate", {"ok": False, "errors": errs})
            reply.intent = "clarify"
            reply.text = "Не получается собрать корректную спецификацию: " + "; ".join(errs) + "."
            return
        rep = validate_spec(spec, min_days=self.settings.get("data.min_period_days", 60),
                            max_years=self.settings.get("data.max_period_years", 15))
        tracer.event("node", "validate", {"ok": rep.ok, "errors": rep.errors, "assumptions": rep.assumptions})
        if not rep.ok:
            # в черновике остаётся только то, что было правильно: ошибка не должна залипать
            state["draft"] = {k: v for k, v in merged.items() if k not in rep.bad_fields}
            reply.intent = "clarify"
            reply.text = "Стратегию в таком виде проверить нельзя: " + "; ".join(rep.errors) + \
                ". Поправьте, и я сразу посчитаю."
            reply.blocks.append({"type": "clarify", "errors": rep.errors})
            return
        spec = rep.spec
        assumptions += rep.assumptions
        rs = spec.rules
        if rs and rs.stop_loss_pct and rs.take_profit_pct and rs.stop_loss_pct > rs.take_profit_pct:
            assumptions.append(f"стоп-лосс {rs.stop_loss_pct:g}% больше тейк-профита {rs.take_profit_pct:g}%: "
                               "убыточная сделка весит больше прибыльной, посчитано как задано")

        # collect_data: с повтором внутри клиента; при неизвестном тикере ищем варианты
        budget.step()
        tracer.event("stage", "data", {"ticker": spec.ticker})
        md = runner.call("get_market_data", {"ticker": spec.ticker, "board": spec.board,
                                             "start_date": spec.start_date.isoformat(),
                                             "end_date": spec.end_date.isoformat()})
        if not md.ok:
            state["draft"] = merged
            if md.retryable:
                reply.intent = "data_unavailable"
                reply.flags.append("data_unavailable")
                reply.text = (f"Не удалось получить котировки: {md.error}. Кеша за этот период нет, "
                              "а придумывать цены я не буду. Чаще всего мешает VPN или сеть, которая не пускает "
                              "к iss.moex.com: скрипт scripts/check_moex.py покажет, на каком шаге рвётся связь. "
                              "Посчитать на демо-котировках можно, перезапустив сервис с флагом --demo.")
                return
            found = runner.call("search_security", {"query": spec.ticker})
            options = [r for r in (found.data or []) if r.get("ticker") != spec.ticker] if found.ok else []
            reply.intent = "clarify"
            tracer.event("data", "no_data", {"error": md.error})          # техническая причина остаётся в трассе
            reply.text = (f"На Мосбирже нет котировок {spec.ticker} с {spec.start_date:%d.%m.%Y} по {spec.end_date:%d.%m.%Y}: "
                          "такой бумаги нет в основном режиме торгов или по ней не было сделок в эти даты.")
            if options:
                reply.text += " Возможно, вы имели в виду: " + ", ".join(
                    f"{o['ticker']} ({o['name']})" for o in options[:5]) + "?"
            reply.blocks.append({"type": "clarify", "options": options[:5]})
            return
        if md.data.get("synthetic"):
            assumptions.append("демо-режим: котировки синтетические, не биржевые")

        # execute_backtest + verify (сверка считается внутри инструмента)
        budget.step()
        tracer.event("stage", "backtest", {})
        bt = runner.call("run_backtest", {"spec": spec.model_dump(mode="json"), "assumptions": assumptions})
        if not bt.ok:
            reply.intent = "error"
            reply.text = f"Бэктест не выполнен: {bt.error}"
            return
        run_id = bt.data["run_id"]
        # sanity: порог цены вне её диапазона за период; пустой отчёт ввёл бы в заблуждение
        hint = dead_entry_hint(runner.ctx.repo.get_run(run_id))
        if hint:
            tracer.event("guardrail", "dead_entry_condition", {"run_id": run_id, "hint": hint})
            runner.ctx.repo.delete_run(run_id)
            # бумага, период и капитал остаются, правила уточняет следующая реплика
            state["draft"] = {k: v for k, v in merged.items() if k not in ("rules", "strategy_type", "parameters")}
            reply.intent = "clarify"
            reply.flags.append("dead_entry_condition")
            reply.text = hint
            reply.blocks.append({"type": "clarify", "dead_entry": True})
            return
        budget.step()
        tracer.event("node", "verify", {"status": bt.data["verification"]})
        if bt.data["verification"] == "FAIL":
            reply.flags.append("verification_failed")

        # investigate: новости ищутся по возможности, их сбой не ломает ответ
        budget.step()
        tracer.event("stage", "news", {})
        news = runner.call("find_news", {"run_id": run_id})
        if news.ok and any(n.get("title", "").startswith("[скрыто") for e in news.data["events"] for n in e["news"]):
            reply.flags.append("injection_blocked")
            tracer.event("guardrail", "news_injection_blocked", {"run_id": run_id})

        # narrate: модель объясняет, код проверяет числа
        budget.step()
        tracer.event("stage", "narrate", {})
        res = runner.ctx.repo.get_run(run_id)
        narrative = no_trades_narrative(res) or brain.narrate(res, state.get("_style", "full"))
        if res.verification.status == "FAIL" and "не подтвержден" not in narrative:
            narrative = "Внимание: независимая сверка не прошла, метрики не подтверждены. " + narrative
        narrative = tidy_prose(narrative)
        res.narrative = narrative
        runner.ctx.repo.save_run(res, runner.ctx.session_id)
        tracer.event("node", "narrate", {"chars": len(narrative)})

        if state.get("last_run_id"):
            state["prev_run_id"] = state["last_run_id"]
        state["last_run_id"] = run_id
        state["last_spec"] = spec.model_dump(mode="json")
        state.pop("draft", None)
        h = res.headline()
        state.setdefault("summary", []).append(
            f"{run_id}: {h['strategy']} {h['period']}, доходность {h['total_return_pct']}%, "
            f"просадка {h['max_drawdown_pct']}%, B&H {h['buy_hold_return_pct']}%")
        state["summary"] = state["summary"][-10:]   # сжатие: в контекст идут только последние итоги

        reply.run_id = run_id
        reply.text = narrative
        if assumptions:
            reply.text += "\n\nДопущения: " + "; ".join(dict.fromkeys(assumptions)) + "."
        reply.blocks.append({"type": "result", "run_id": run_id})

    # ---------- подсказки следующего шага ----------

    EXAMPLES = ["SBER за 2023-2025, SMA 20 и 50", "Газпром за 2024 год, RSI 14 30/70",
                "Лукойл, купить и держать"]
    FOLLOWUPS = {"trades": "покажи худшие сделки", "news": "какие были новости?",
                 "explain": "почему такая просадка?", "save": "сохрани"}

    def _suggestions(self, reply: AgentReply, state: dict[str, Any], text: str) -> list[str]:
        """Только фразы, которые агент гарантированно понимает (проверяется тестом)."""
        intent = reply.intent
        options = state.pop("_options", None)
        if "model_unavailable" in reply.flags:
            return []                      # без модели примеры всё равно не сработают
        if options and intent in ("clarify", "unsupported", "glossary", "help") and not reply.run_id:
            return options[:3]             # готовые ответы на заданный вопрос
        if reply.run_id:
            res = self.repo.get_run(reply.run_id)
            s = res.spec
            relax = no_trades_suggestion(res.diagnostics.get("entry_check"))
            if relax:                      # сделок нет: первым идёт способ их получить
                year = s.start_date.year - 1 if s.start_date.year > 2015 else 2024
                return [relax, "а если SMA 20/50?", f"а за {year} год?"]
            out = ["почему она проиграла удержанию?" if res.metrics.total_return_pct < res.benchmark.total_return_pct
                   else "где она теряла деньги?"]
            p, t = s.parameters, s.strategy_type.value
            if t in ("SMA_CROSSOVER", "EMA_CROSSOVER"):
                out.append("а если 50 и 200?" if (int(p["fast_period"]), int(p["slow_period"])) == (10, 30)
                           else "а если 10 и 30?")
            elif t == "RSI":
                out.append("а если 25/75?" if (p["lower"], p["upper"]) != (25, 75) else "а если 30/70?")
            elif t == "BREAKOUT":
                out.append("а если пробой 55-дневного максимума и минимум за 20?")
            elif t == "RULES":
                out.append("а со стоп-лоссом 5%?" if not (s.rules and s.rules.stop_loss_pct) else "а без стоп-лосса?")
            else:
                out.append("а если SMA 20/50?")
            year = s.start_date.year - 1 if s.start_date.year > 2015 else 2024
            out.append(f"а за {year} год?")
            return out
        if "future_period" in reply.flags:
            n = state.pop("future_years", 3)
            span = f"за последние {n} {'год' if n == 1 else 'года' if n < 5 else 'лет'}"
            if state.get("draft", {}).get("strategy_type"):
                return [span]
            return [f"SMA 20/50 {span}", f"RSI 14 30/70 {span}", f"купить и держать {span}"]
        if intent == "clarify" and state.get("draft") is not None:
            draft = state["draft"]
            if not draft.get("ticker"):
                return ["SBER", "GAZP", "LKOH"]
            if not draft.get("strategy_type"):
                return ["SMA 20/50", "RSI 14 30/70", "купить и держать"]
        if intent == "unsupported":
            t = (scope_guard.find_tickers(text) or ["SBER"])[0]
            return [f"{t}, EMA 12/26", f"{t}, RSI 14 30/70", f"{t}, пробой 20-дневного максимума"]
        if state.get("last_run_id") and intent in ("explain", "trades", "news", "compare", "history", "save",
                                                     "delete", "clarify"):
            return [v for k, v in self.FOLLOWUPS.items() if k != intent][:3]
        return list(self.EXAMPLES)

    # ---------- блоки для интерфейса ----------

    def _blocks_from_calls(self, calls: list[tuple[str, ToolResult]], state: dict[str, Any],
                           reply: AgentReply) -> None:
        for name, r in calls:
            if r.awaiting_confirmation:
                p = self.repo.get_pending(r.action_id)
                reply.pending_action = {"id": r.action_id, "tool": name, "description": p["description"]}
                reply.blocks.append({"type": "confirm", "action_id": r.action_id, "text": p["description"]})
            elif not r.ok:
                continue
            elif name == "get_trades":
                reply.blocks.append({"type": "trades", "rows": r.data})
            elif name == "list_history":
                reply.blocks.append({"type": "history", **r.data})
            elif name in ("get_news_events", "find_news"):
                reply.blocks.append({"type": "news", **r.data})
            elif name == "compare_runs":
                reply.blocks.append({"type": "compare", **r.data})
            elif name == "get_diagnostics" and state.get("last_run_id"):
                reply.blocks.append({"type": "result", "run_id": state["last_run_id"], "tab": "weak"})

    # ---------- подтверждение человеком ----------

    def confirm(self, action_id: str, approve: bool) -> dict[str, Any]:
        p = self.repo.get_pending(action_id)
        if p is None:
            return {"ok": False, "text": "Такого действия нет."}
        if p["status"] != "pending":
            return {"ok": False, "text": f"Действие уже обработано: {p['status']}."}
        tracer = Tracer(self.repo, p["session_id"], self.settings.path("app.logs_dir"))
        if not approve:
            self.repo.resolve_pending(action_id, "rejected")
            tracer.event("guardrail", "action_rejected", {"action_id": action_id, "tool": p["tool"]})
            text = "Отменено. Ничего не изменилось."
            self.repo.add_message(p["session_id"], "assistant", text, {"trace_id": tracer.trace_id})
            return {"ok": True, "text": text, "trace_id": tracer.trace_id}
        budget = Budget(max_steps=2, max_tool_calls=1, max_llm_calls=0, max_tokens=0, max_cost_rub=0)
        ctx = ToolContext(self.settings, self.market, self.news, self.repo, p["session_id"])
        r = ToolRunner(ctx, tracer, budget).call(p["tool"], p["args"], confirmed=True)
        self.repo.resolve_pending(action_id, "confirmed" if r.ok else "failed")
        tracer.event("guardrail", "action_confirmed", {"action_id": action_id, "tool": p["tool"], "ok": r.ok})
        if r.ok and p["tool"] == "save_research":
            text = f"Сохранено: «{r.data['title']}». Найдёте в разделе «Сохранённое»."
        elif r.ok and p["tool"] == "clear_history":
            text = f"История очищена, удалено запусков: {r.data['deleted']}."
        elif r.ok and p["tool"] == "delete_run":
            text = "Удалено из истории." + (" Из избранного тоже." if r.data.get("favorites_removed") else "")
        elif r.ok:
            text = "Удалено из сохранённого."
        else:
            text = f"Не выполнено: {r.error}"
        self.repo.add_message(p["session_id"], "assistant", text, {"trace_id": tracer.trace_id})
        return {"ok": r.ok, "text": text, "trace_id": tracer.trace_id, "data": r.data}


__all__ = ["Agent", "DISCLAIMER", "TOOLS"]
