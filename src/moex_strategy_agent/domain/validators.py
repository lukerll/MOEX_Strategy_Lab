"""Программная проверка спецификации. Правила живут в коде, а не в промпте."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .rules import template_rules, validate_rules
from .rules import warmup as rules_warmup
from .schemas import DEFAULT_PARAMS, StrategySpec, StrategyType

PARAM_RULES: dict[StrategyType, dict[str, tuple[float, float]]] = {
    StrategyType.SMA_CROSSOVER: {"fast_period": (1, 250), "slow_period": (3, 400)},
    StrategyType.EMA_CROSSOVER: {"fast_period": (1, 250), "slow_period": (3, 400)},
    StrategyType.RSI: {"period": (2, 100), "lower": (1, 99), "upper": (1, 99)},
    StrategyType.BREAKOUT: {"entry_lookback": (2, 250), "exit_lookback": (2, 250)},
    StrategyType.BUY_AND_HOLD: {},
    StrategyType.RULES: {},
}


@dataclass
class ValidationReport:
    spec: StrategySpec | None
    errors: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    bad_fields: set[str] = field(default_factory=set)     # что выкинуть из черновика

    @property
    def ok(self) -> bool:
        return self.spec is not None and not self.errors


def fill_defaults(spec: StrategySpec) -> tuple[StrategySpec, list[str]]:
    """Дополняет недостающие параметры значениями по умолчанию и фиксирует допущения."""
    assumptions: list[str] = []
    params = dict(spec.parameters)
    for k, v in DEFAULT_PARAMS[spec.strategy_type].items():
        if k not in params:
            params[k] = v
            assumptions.append(f"параметр {k} не указан, взято {v:g}")
    # лишние ключи отбрасываем: движок не исполняет то, чего не знает
    allowed = set(DEFAULT_PARAMS[spec.strategy_type])
    dropped = sorted(set(params) - allowed)
    for k in dropped:
        params.pop(k)
    if dropped:
        assumptions.append(f"параметры {', '.join(dropped)} не поддерживаются этим типом и проигнорированы")
    return spec.model_copy(update={"parameters": params}), assumptions


def validate_spec(spec: StrategySpec, *, today: date | None = None,
                  min_days: int = 60, max_years: int = 15) -> ValidationReport:
    today = today or date.today()
    spec, assumptions = fill_defaults(spec)
    errors: list[str] = []

    for name, (lo, hi) in PARAM_RULES[spec.strategy_type].items():
        v = spec.parameters[name]
        if not (lo <= v <= hi):
            errors.append(f"{name}={v:g} вне допустимого диапазона [{lo:g}; {hi:g}]")
        if name.endswith(("period", "lookback")) and v != int(v):
            errors.append(f"{name} должен быть целым числом дней")

    p = spec.parameters
    if spec.strategy_type in (StrategyType.SMA_CROSSOVER, StrategyType.EMA_CROSSOVER):
        if p["fast_period"] >= p["slow_period"]:
            errors.append("период быстрой средней должен быть меньше периода медленной")
    if spec.strategy_type == StrategyType.RSI and p["lower"] >= p["upper"]:
        errors.append("нижний уровень RSI должен быть меньше верхнего")

    # дерево правил: для шаблонов строит код, для своих правил проверяет код
    if spec.strategy_type == StrategyType.RULES:
        if spec.rules is None:
            errors.append("для своей стратегии нужны правила входа")
        else:
            errors += validate_rules(spec.rules)
            if not spec.rules.has_exit() and spec.rules.entry.type != "always":
                assumptions.append("условие выхода не задано: позиция держится до конца периода")
    elif not errors:
        spec = spec.model_copy(update={"rules": template_rules(spec.strategy_type.value, spec.parameters)})

    if spec.start_date >= spec.end_date:
        errors.append("дата начала должна быть раньше даты окончания")
    if spec.start_date > today:
        errors.append("период целиком в будущем: истории там пока нет")
    if spec.end_date > today:
        spec = spec.model_copy(update={"end_date": today})
        assumptions.append(f"конец периода в будущем, обрезан до {today.isoformat()}")
    if (spec.end_date - spec.start_date).days < min_days:
        errors.append(f"период короче {min_days} дней: статистики не будет")
    if (spec.end_date - spec.start_date).days > max_years * 366:
        errors.append(f"период длиннее {max_years} лет: сузьте его")
    if spec.start_date.year < 2000:
        errors.append("данные ISS по акциям доступны примерно с 2000 года")

    bad: set[str] = set()
    for e in errors:
        if any(w in e for w in ("период", "дата", "будущем", "года")):
            bad |= {"start_date", "end_date"}
        else:
            bad.add("parameters")
    return ValidationReport(spec=spec, errors=errors, assumptions=assumptions, bad_fields=bad)


def warmup_bars(spec: StrategySpec) -> int:
    """Сколько баров истории нужно до начала периода, чтобы индикаторы были готовы."""
    if spec.rules is None:
        spec = spec.model_copy(update={"rules": template_rules(spec.strategy_type.value, spec.parameters)})
    return rules_warmup(spec.rules)
