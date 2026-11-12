"""Контракты данных: StrategySpec на входе движка и BacktestResult на выходе.

LLM никогда не пишет исполняемый код. Она заполняет StrategySpec, а движок исполняет
только поддерживаемые здесь типы стратегий.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .rules import RuleSet


class StrategyType(str, Enum):
    SMA_CROSSOVER = "SMA_CROSSOVER"
    EMA_CROSSOVER = "EMA_CROSSOVER"
    RSI = "RSI"
    BREAKOUT = "BREAKOUT"
    BUY_AND_HOLD = "BUY_AND_HOLD"
    RULES = "RULES"            # произвольная стратегия на языке правил


# Параметры по умолчанию для каждого типа: используются, если пользователь их не назвал.
DEFAULT_PARAMS: dict[StrategyType, dict[str, float]] = {
    StrategyType.SMA_CROSSOVER: {"fast_period": 20, "slow_period": 50},
    StrategyType.EMA_CROSSOVER: {"fast_period": 12, "slow_period": 26},
    StrategyType.RSI: {"period": 14, "lower": 30, "upper": 70},
    StrategyType.BREAKOUT: {"entry_lookback": 20, "exit_lookback": 10},
    StrategyType.BUY_AND_HOLD: {},
    StrategyType.RULES: {},
}

STRATEGY_TITLES = {
    StrategyType.SMA_CROSSOVER: "Пересечение SMA",
    StrategyType.EMA_CROSSOVER: "Пересечение EMA",
    StrategyType.RSI: "RSI по уровням",
    StrategyType.BREAKOUT: "Пробой канала",
    StrategyType.BUY_AND_HOLD: "Купить и держать",
    StrategyType.RULES: "Свои правила",
}


class StrategySpec(BaseModel):
    """Формальная спецификация стратегии. Единственное, что LLM передаёт в вычисления."""

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(..., description="Тикер MOEX, например SBER")
    board: str = Field("TQBR", description="Режим торгов")
    timeframe: Literal["1d"] = "1d"
    start_date: date
    end_date: date
    initial_cash: float = Field(100_000, gt=0, le=1e10)
    strategy_type: StrategyType
    parameters: dict[str, float] = Field(default_factory=dict)
    position: Literal["long_only"] = "long_only"
    signal_time: Literal["daily_close"] = "daily_close"
    execution_time: Literal["next_open"] = "next_open"
    commission: float = Field(0.0005, ge=0, le=0.01, description="Доля оборота с каждой стороны")
    slippage: float = Field(0.0005, ge=0, le=0.01, description="Доля к цене покупки")
    rules: RuleSet | None = Field(None, description="Дерево правил; для шаблонов заполняется кодом")

    @field_validator("ticker", "board")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.replace("_", "").isalnum() or not (1 <= len(v) <= 12):
            raise ValueError("тикер/режим: только латиница и цифры, до 12 символов")
        return v

    def title(self) -> str:
        p = self.parameters
        t = self.strategy_type
        if t in (StrategyType.SMA_CROSSOVER, StrategyType.EMA_CROSSOVER):
            name = "SMA" if t == StrategyType.SMA_CROSSOVER else "EMA"
            if int(p["fast_period"]) == 1:
                return f"{self.ticker}: цена против {name} {int(p['slow_period'])}"
            return f"{self.ticker}: {name} {int(p['fast_period'])}/{int(p['slow_period'])}"
        if t == StrategyType.RSI:
            return f"{self.ticker}: RSI {int(p['period'])}, {int(p['lower'])}/{int(p['upper'])}"
        if t == StrategyType.BREAKOUT:
            return f"{self.ticker}: пробой {int(p['entry_lookback'])}/{int(p['exit_lookback'])}"
        if t == StrategyType.RULES:
            return f"{self.ticker}: свои правила"
        return f"{self.ticker}: купить и держать"


class Trade(BaseModel):
    entry_date: date
    exit_date: date | None
    size: int
    entry_price: float          # с учётом проскальзывания
    exit_price: float | None
    pnl: float                  # после комиссий; для открытой позиции: переоценка по закрытию
    return_pct: float
    commission: float
    holding_days: int
    is_open: bool = False
    # почему закрыли: signal | stop_loss | take_profit | trailing | max_hold | atr_stop | end (позиция открыта)
    exit_reason: str | None = None


class Metrics(BaseModel):
    initial_cash: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    volatility_pct: float
    sharpe: float
    trades_count: int
    win_rate_pct: float
    avg_trade_pnl: float
    avg_trade_return_pct: float
    profit_factor: float | None
    best_trade_pct: float | None
    worst_trade_pct: float | None
    avg_holding_days: float
    exposure_pct: float
    commissions_paid: float
    open_position_at_end: bool


class DataInfo(BaseModel):
    source: Literal["moex", "demo", "cache"]
    ticker: str
    board: str
    rows: int
    first_date: date
    last_date: date
    is_synthetic: bool
    warnings: list[str] = Field(default_factory=list)


class ReconciliationCheck(BaseModel):
    name: str
    main: Any
    reference: Any
    ok: bool
    note: str = ""


class Reconciliation(BaseModel):
    status: Literal["OK", "WARN", "FAIL"]
    checks: list[ReconciliationCheck]
    summary: str


class NewsItem(BaseModel):
    published_at: datetime
    title: str
    url: str | None
    source: str
    relevance: float
    flagged_injection: bool = False


class PriceEvent(BaseModel):
    date: date
    change_pct: float
    close: float
    news: list[NewsItem] = Field(default_factory=list)
    note: str = ""


class BacktestResult(BaseModel):
    run_id: str
    spec: StrategySpec
    data: DataInfo
    metrics: Metrics
    benchmark: Metrics
    trades: list[Trade]
    equity: list[dict[str, Any]]    # date, equity, benchmark, drawdown, close, position
    signals: dict[str, list[str]]   # entries/exits: даты сигналов
    verification: Reconciliation
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    events: list[PriceEvent] = Field(default_factory=list)
    news_status: str = ""
    understood: dict[str, str] = Field(default_factory=dict)   # «Как я понял стратегию» по-русски
    narrative: str = ""
    assumptions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    def headline(self) -> dict[str, Any]:
        """Короткая сводка для контекста LLM и истории."""
        m, b = self.metrics, self.benchmark
        return {
            "run_id": self.run_id,
            "ticker": self.spec.ticker,
            "strategy_type": self.spec.strategy_type.value,
            "start_date": self.spec.start_date.isoformat(),
            "end_date": self.spec.end_date.isoformat(),
            "strategy": self.spec.title(),
            "period": f"с {self.spec.start_date} по {self.spec.end_date}",
            "total_return_pct": round(m.total_return_pct, 2),
            "max_drawdown_pct": round(m.max_drawdown_pct, 2),
            "trades": m.trades_count,
            "buy_hold_return_pct": round(b.total_return_pct, 2),
            "verification": self.verification.status,
            "synthetic_data": self.data.is_synthetic,
        }
