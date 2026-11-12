"""Состояние одного запроса к агенту и результат понимания реплики."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Intent = Literal["backtest", "modify", "explain", "news", "trades", "history", "compare",
                 "save", "delete", "help", "out_of_scope"]

INTENTS: tuple[str, ...] = ("backtest", "modify", "explain", "news", "trades", "history", "compare",
                            "save", "delete", "help", "out_of_scope", "unsupported")


@dataclass
class Understanding:
    intent: str
    spec_patch: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    question: str | None = None          # уточнение, если без него нельзя продолжать
    options: list[str] = field(default_factory=list)   # готовые ответы на уточнение, станут кнопками
    dropped: list[str] = field(default_factory=list)   # поля от модели, которых нет в спецификации
    target_id: str | None = None         # id запуска/исследования, если назван
    reasoning: str = ""
    source: str = "rules"                # rules | llm | llm->rules


@dataclass
class AgentReply:
    session_id: str
    trace_id: str
    text: str
    intent: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    run_id: str | None = None
    pending_action: dict[str, Any] | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    tools_called: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)     # injection_blocked, trading_refused, ...
    suggestions: list[str] = field(default_factory=list)  # 2–3 следующих шага, понятных агенту
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()
