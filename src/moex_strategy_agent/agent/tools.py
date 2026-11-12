"""Инструменты агента: контракт (pydantic-схема), валидация, ошибки, права.

Три класса инструментов:
  * read    : только читают (котировки, сделки, история);
  * compute : считают и сохраняют запуск (бэктест, новости к запуску);
  * action  : меняют долговременное состояние. Без подтверждения человека не исполняются:
               это проверяется здесь, в коде, а не просьбой в промпте.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..backtest.service import LoadedData, attach_news, load_data, run_backtest
from ..config import Settings
from ..data.market_data import MarketData
from ..data.moex_client import DataSourceError, NoDataError
from ..data.news_client import NewsService
from ..data.quality import DataQualityError
from ..domain.schemas import BacktestResult, StrategySpec
from ..persistence.repository import Repository
from ..telemetry.tracing import Tracer
from .guardrails import Budget, sanitize_untrusted

# ---------- аргументы ----------


class SearchSecurityArgs(BaseModel):
    query: str = Field(..., min_length=2, max_length=60, description="Название компании или тикер")


class MarketDataArgs(BaseModel):
    ticker: str = Field(..., max_length=12)
    start_date: date
    end_date: date
    board: str = "TQBR"


class RunBacktestArgs(BaseModel):
    spec: StrategySpec
    assumptions: list[str] = Field(default_factory=list)


class RunIdArgs(BaseModel):
    run_id: str = Field(..., pattern=r"^[0-9a-f]{10}$")


class TradesArgs(RunIdArgs):
    order: Literal["worst", "best", "chronological"] = "worst"
    limit: int = Field(5, ge=1, le=30)


class DiagnosticsArgs(RunIdArgs):
    section: Literal["findings", "drawdowns", "yearly", "regimes", "false_signals", "costs",
                     "sensitivity", "worst_trades", "all"] = "findings"


class HistoryArgs(BaseModel):
    limit: int = Field(10, ge=1, le=30)


class CompareArgs(BaseModel):
    run_id_a: str = Field(..., pattern=r"^[0-9a-f]{10}$")
    run_id_b: str = Field(..., pattern=r"^[0-9a-f]{10}$")


class SaveResearchArgs(RunIdArgs):
    title: str = Field(..., min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def _plain_title(cls, v: str) -> str:
        # название потом читает модель в списке сохранённого: это внешний текст, а не команда
        clean, flagged = sanitize_untrusted(v, 120)
        if flagged or not clean:
            raise ValueError("название похоже на попытку управлять агентом, выберите другое")
        return clean


class DeleteResearchArgs(BaseModel):
    research_id: str = Field(..., pattern=r"^r_[0-9a-f]{8}$")


class DeleteRunArgs(BaseModel):
    run_id: str = Field(..., pattern=r"^[0-9a-f]{10}$")


class ClearHistoryArgs(BaseModel):
    model_config = {"extra": "forbid"}


# ---------- инфраструктура ----------


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str = ""
    retryable: bool = False
    awaiting_confirmation: bool = False
    action_id: str | None = None

    def for_llm(self) -> dict[str, Any]:
        if self.awaiting_confirmation:
            return {"status": "awaiting_confirmation", "action_id": self.action_id,
                    "message": "Действие ждёт подтверждения пользователя"}
        return {"ok": True, "data": self.data} if self.ok else {"ok": False, "error": self.error}


@dataclass
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    kind: Literal["read", "compute", "action"]
    handler: Callable[["ToolContext", Any], Any]

    def schema(self) -> dict[str, Any]:
        s = self.args.model_json_schema()
        s.pop("title", None)
        return {"name": self.name, "description": self.description, "parameters": s}


@dataclass
class ToolContext:
    settings: Settings
    market: MarketData
    news: NewsService
    repo: Repository
    session_id: str
    loaded: dict[str, LoadedData] = field(default_factory=dict)   # run_id -> данные для новостей


class ToolError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _get_run(ctx: ToolContext, run_id: str) -> BacktestResult:
    res = ctx.repo.get_run(run_id)
    if res is None:
        raise ToolError(f"запуск {run_id} не найден")
    return res


# ---------- обработчики ----------


def t_search_security(ctx: ToolContext, a: SearchSecurityArgs) -> Any:
    rows = ctx.market.search(a.query)
    return [{"ticker": r.get("secid"), "name": r.get("shortname"), "board": r.get("primary_boardid")}
            for r in rows[:8]]


def t_market_data(ctx: ToolContext, a: MarketDataArgs) -> Any:
    df, info = ctx.market.history(a.ticker.upper(), a.board, a.start_date, a.end_date)
    period = df[df.index >= str(a.start_date)]
    return {"source": info.source, "synthetic": info.is_synthetic, "rows": info.rows,
            "first_date": str(info.first_date), "last_date": str(info.last_date),
            "first_close": round(float(period["Close"].iloc[0]), 4),
            "last_close": round(float(period["Close"].iloc[-1]), 4), "warnings": info.warnings[:5]}


def t_run_backtest(ctx: ToolContext, a: RunBacktestArgs) -> Any:
    data = load_data(ctx.market, a.spec)
    res = run_backtest(a.spec, data, ctx.settings, a.assumptions)
    ctx.repo.save_run(res, ctx.session_id)
    ctx.loaded[res.run_id] = data
    return {**res.headline(), "data_warnings": res.data.warnings[:5]}


def t_find_news(ctx: ToolContext, a: RunIdArgs) -> Any:
    res = _get_run(ctx, a.run_id)
    data = ctx.loaded.get(a.run_id) or load_data(ctx.market, res.spec)
    res = attach_news(res, data, ctx.settings, ctx.news)
    ctx.repo.save_run(res, ctx.session_id)
    return {"status": res.news_status,
            "events": [{"date": str(e.date), "change_pct": e.change_pct, "note": e.note,
                        "news": [{"title": n.title if not n.flagged_injection else "[скрыто: prompt injection]",
                                  "source": n.source, "relevance": n.relevance} for n in e.news]}
                       for e in res.events]}


def t_run_summary(ctx: ToolContext, a: RunIdArgs) -> Any:
    res = _get_run(ctx, a.run_id)
    from ..analysis.explanations import build_facts
    f = build_facts(res)
    f.pop("yearly", None)
    return f


def t_trades(ctx: ToolContext, a: TradesArgs) -> Any:
    res = _get_run(ctx, a.run_id)
    trades = list(res.trades)
    if a.order == "worst":
        trades.sort(key=lambda t: t.pnl)
    elif a.order == "best":
        trades.sort(key=lambda t: -t.pnl)
    return [{"entry": str(t.entry_date), "exit": str(t.exit_date) if t.exit_date else "открыта",
             "size": t.size, "entry_price": round(t.entry_price, 4),
             "exit_price": round(t.exit_price, 4) if t.exit_price else None,
             "pnl": round(t.pnl, 2), "return_pct": round(t.return_pct, 2), "days": t.holding_days}
            for t in trades[:a.limit]]


def t_diagnostics(ctx: ToolContext, a: DiagnosticsArgs) -> Any:
    d = dict(_get_run(ctx, a.run_id).diagnostics)
    d.pop("indicators", None)
    return d if a.section == "all" else {a.section: d.get(a.section)}


def t_news_events(ctx: ToolContext, a: RunIdArgs) -> Any:
    res = _get_run(ctx, a.run_id)
    if not res.events and not res.news_status:
        return t_find_news(ctx, a)
    return {"status": res.news_status,
            "events": [{"date": str(e.date), "change_pct": e.change_pct, "note": e.note,
                        "news": [n.title if not n.flagged_injection else "[скрыто: prompt injection]"
                                 for n in e.news]} for e in res.events]}


def t_history(ctx: ToolContext, a: HistoryArgs) -> Any:
    return {"saved": ctx.repo.list_research(a.limit), "recent_runs": ctx.repo.list_runs(a.limit, ctx.session_id)}


def t_compare(ctx: ToolContext, a: CompareArgs) -> Any:
    ra, rb = _get_run(ctx, a.run_id_a), _get_run(ctx, a.run_id_b)
    keys = ["total_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe", "trades_count", "win_rate_pct"]
    return {"a": ra.spec.title(), "b": rb.spec.title(),
            "metrics": {k: {"a": round(getattr(ra.metrics, k), 2), "b": round(getattr(rb.metrics, k), 2),
                            "diff": round(getattr(ra.metrics, k) - getattr(rb.metrics, k), 2)} for k in keys}}


def t_save_research(ctx: ToolContext, a: SaveResearchArgs) -> Any:
    _get_run(ctx, a.run_id)
    rid = ctx.repo.save_research(a.run_id, a.title)
    return {"research_id": rid, "title": a.title}


def t_clear_history(ctx: ToolContext, a: ClearHistoryArgs) -> Any:
    return ctx.repo.clear_history()


def t_delete_run(ctx: ToolContext, a: DeleteRunArgs) -> Any:
    out = ctx.repo.delete_run(a.run_id)
    if not out["deleted"]:
        raise ToolError(f"запуск {a.run_id} не найден")
    return {"deleted": a.run_id, **out}


def t_delete_research(ctx: ToolContext, a: DeleteResearchArgs) -> Any:
    if not ctx.repo.delete_research(a.research_id):
        raise ToolError(f"исследование {a.research_id} не найдено")
    return {"deleted": a.research_id}


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("search_security", "Найти тикер MOEX по названию компании", SearchSecurityArgs, "read", t_search_security),
    ToolSpec("get_market_data", "Дневные котировки акции за период: источник, число дней, первая и последняя цена",
             MarketDataArgs, "read", t_market_data),
    ToolSpec("run_backtest", "Запустить бэктест по проверенной спецификации (с независимой сверкой)",
             RunBacktestArgs, "compute", t_run_backtest),
    ToolSpec("find_news", "Найти новости рядом с резкими движениями цены для запуска", RunIdArgs, "compute", t_find_news),
    ToolSpec("get_run_summary", "Метрики, сравнение с купить-и-держать, выводы и статус сверки запуска",
             RunIdArgs, "read", t_run_summary),
    ToolSpec("get_trades", "Сделки запуска: худшие, лучшие или по порядку", TradesArgs, "read", t_trades),
    ToolSpec("get_diagnostics", "Диагностика слабых мест: просадки, годы, режимы рынка, ложные сигналы, "
             "издержки, чувствительность к параметрам", DiagnosticsArgs, "read", t_diagnostics),
    ToolSpec("get_news_events", "Аномальные движения цены и новости рядом с ними", RunIdArgs, "read", t_news_events),
    ToolSpec("list_history", "Сохранённые исследования и последние запуски", HistoryArgs, "read", t_history),
    ToolSpec("compare_runs", "Сравнить метрики двух запусков", CompareArgs, "read", t_compare),
    ToolSpec("save_research", "Сохранить исследование в историю (нужно подтверждение пользователя)",
             SaveResearchArgs, "action", t_save_research),
    ToolSpec("delete_research", "Удалить сохранённое исследование (нужно подтверждение пользователя)",
             DeleteResearchArgs, "action", t_delete_research),
    ToolSpec("delete_run", "Удалить запуск из истории (только из интерфейса, нужно подтверждение пользователя)",
             DeleteRunArgs, "action", t_delete_run),
    ToolSpec("clear_history", "Очистить историю запусков, кроме избранного (только из настроек, нужно подтверждение пользователя)",
             ClearHistoryArgs, "action", t_clear_history),
]}

READ_TOOLS = {n for n, t in TOOLS.items() if t.kind == "read"}
REACT_TOOLS = READ_TOOLS | {"find_news", "save_research", "delete_research"}


def describe_action(tool: str, args: dict[str, Any], repo: Repository) -> str:
    if tool == "save_research":
        return f"Сохранить «{args.get('title')}» в раздел «Сохранённое»"
    if tool == "clear_history":
        favs = len(repo.list_research(500))
        return "Очистить историю запусков" + (f". Избранное ({favs}) останется" if favs else "")
    if tool == "delete_run":
        run = repo.get_run(args.get("run_id", ""))
        title = run.spec.title() if run else args.get("run_id")
        in_fav = any(r["run_id"] == args.get("run_id") for r in repo.list_research(500))
        return f"Удалить из истории «{title}»" + (". Он также пропадёт из избранного" if in_fav else "")
    if tool == "delete_research":
        res = {r["id"]: r["title"] for r in repo.list_research(500)}
        return f"Удалить из сохранённого «{res.get(args.get('research_id'), args.get('research_id'))}»"
    return f"{tool}({args})"


class ToolRunner:
    def __init__(self, ctx: ToolContext, tracer: Tracer, budget: Budget):
        self.ctx = ctx
        self.tracer = tracer
        self.budget = budget
        self.called: list[str] = []

    def _precheck(self, name: str, parsed: BaseModel) -> str:
        repo = self.ctx.repo
        if name == "delete_research":
            ids = {r["id"] for r in repo.list_research(500)}
            return "" if parsed.research_id in ids else f"исследования {parsed.research_id} нет в сохранённом"
        if name == "delete_run" and repo.get_run(parsed.run_id) is None:
            return f"запуск {parsed.run_id} не найден"
        if name == "save_research" and repo.get_run(parsed.run_id) is None:
            return f"запуск {parsed.run_id} не найден"
        return ""

    def call(self, name: str, args: dict[str, Any], *, allowed: set[str] | None = None,
             confirmed: bool = False) -> ToolResult:
        spec = TOOLS.get(name)
        if spec is None:
            self.tracer.event("guardrail", "unknown_tool", {"tool": name})
            return ToolResult(False, error=f"инструмента {name} нет")
        if allowed is not None and name not in allowed:
            self.tracer.event("guardrail", "tool_not_allowed", {"tool": name})
            return ToolResult(False, error=f"инструмент {name} недоступен на этом шаге")
        try:
            parsed = spec.args.model_validate(args)
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:4])
            self.tracer.event("tool", name, {"args": args, "error": f"invalid args: {msg}"})
            return ToolResult(False, error=f"некорректные аргументы: {msg}")

        if spec.kind == "action" and not confirmed:
            # нечего подтверждать, если объекта нет: проверяем до карточки, а не после
            missing = self._precheck(name, parsed)
            if missing:
                self.tracer.event("tool", name, {"args": parsed.model_dump(mode="json"), "error": missing})
                return ToolResult(False, error=missing)
            clean = parsed.model_dump(mode="json")
            aid = self.ctx.repo.create_pending(self.ctx.session_id, name, clean,
                                               describe_action(name, clean, self.ctx.repo))
            self.tracer.event("guardrail", "awaiting_confirmation", {"tool": name, "action_id": aid, "args": clean})
            return ToolResult(False, awaiting_confirmation=True, action_id=aid)

        self.budget.tool()
        self.called.append(name)
        t0 = time.monotonic()
        try:
            data = spec.handler(self.ctx, parsed)
            res = ToolResult(True, data=data)
        except NoDataError as e:
            res = ToolResult(False, error=str(e), retryable=False)
        except DataSourceError as e:
            res = ToolResult(False, error=str(e), retryable=e.retryable)
        except (ToolError, DataQualityError) as e:
            res = ToolResult(False, error=str(e), retryable=getattr(e, "retryable", False))
        except Exception as e:  # noqa: BLE001, инструмент не должен ронять агента
            res = ToolResult(False, error=f"внутренняя ошибка инструмента: {e.__class__.__name__}: {e}")
        self.tracer.event("tool", name, {
            "args": parsed.model_dump(mode="json") if spec.kind != "compute" or name != "run_backtest"
            else {"spec": parsed.spec.model_dump(mode="json")},
            "ok": res.ok, "error": res.error, "result_preview": str(res.data)[:400] if res.ok else None},
            latency_ms=int((time.monotonic() - t0) * 1000))
        return res
