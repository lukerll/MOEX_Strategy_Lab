"""Язык правил: стратегия как дерево из проверяемых кубиков.

Модель собирает из описания на естественном языке структуру RuleSet. Код её проверяет
(validate_rules), описывает по-русски (describe_rules) и исполняет двумя независимыми движками.
Исполняемого кода от модели нет: только данные этой схемы.

Семантика (одинакова для обоих движков, зафиксирована в README):
  * все условия считаются по закрытию дня t, сделка исполняется по открытию дня t+1;
  * highest/lowest берут предыдущие N дней, без текущего (иначе пробой невозможен);
  * сравнение с пропуском (индикатор ещё не прогрелся) ложно;
  * стопы проверяются по закрытию: если close пробил уровень, продажа на следующем открытии;
  * выход через N дней: решение на закрытии N-го дня после дня покупки.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

Source = Literal["close", "open", "high", "low", "volume"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- выражения: числовые ряды ----------

class Price(_M):
    type: Literal["price"] = "price"
    field: Source = "close"


class Const(_M):
    type: Literal["const"] = "const"
    value: float


class SMA(_M):
    type: Literal["sma"] = "sma"
    n: int = Field(..., ge=1, le=400)
    source: Source = "close"


class EMA(_M):
    type: Literal["ema"] = "ema"
    n: int = Field(..., ge=1, le=400)
    source: Source = "close"


class RSI(_M):
    type: Literal["rsi"] = "rsi"
    n: int = Field(14, ge=2, le=100)


class Highest(_M):
    """Максимум за предыдущие n дней (без текущего)."""
    type: Literal["highest"] = "highest"
    n: int = Field(..., ge=1, le=400)
    source: Source = "high"


class Lowest(_M):
    """Минимум за предыдущие n дней (без текущего)."""
    type: Literal["lowest"] = "lowest"
    n: int = Field(..., ge=1, le=400)
    source: Source = "low"


class Change(_M):
    """Изменение цены закрытия за n дней, в процентах."""
    type: Literal["change"] = "change"
    n: int = Field(..., ge=1, le=400)


class AvgVolume(_M):
    type: Literal["avg_volume"] = "avg_volume"
    n: int = Field(..., ge=1, le=400)


class MACD(_M):
    type: Literal["macd"] = "macd"
    fast: int = Field(12, ge=2, le=100)
    slow: int = Field(26, ge=3, le=200)
    signal: int = Field(9, ge=2, le=100)
    line: Literal["macd", "signal", "hist"] = "macd"


class Bollinger(_M):
    type: Literal["bbands"] = "bbands"
    n: int = Field(20, ge=2, le=200)
    k: float = Field(2.0, gt=0, le=5)
    band: Literal["upper", "middle", "lower"] = "middle"


class Stochastic(_M):
    type: Literal["stoch"] = "stoch"
    k: int = Field(14, ge=2, le=100)
    d: int = Field(3, ge=1, le=50)
    line: Literal["k", "d"] = "k"


class ATR(_M):
    type: Literal["atr"] = "atr"
    n: int = Field(14, ge=2, le=100)


Expr = Annotated[Union[Price, Const, SMA, EMA, RSI, Highest, Lowest, Change, AvgVolume, MACD, Bollinger,
                       Stochastic, ATR], Field(discriminator="type")]


# ---------- условия ----------

class Compare(_M):
    type: Literal["compare"] = "compare"
    left: Expr
    op: Literal[">", "<", ">=", "<="]
    right: Expr


class Cross(_M):
    """left пересекает right: above = снизу вверх, below = сверху вниз."""
    type: Literal["cross"] = "cross"
    left: Expr
    direction: Literal["above", "below"]
    right: Expr


class Always(_M):
    """Всегда истинно: вход при первой возможности («купить и держать»)."""
    type: Literal["always"] = "always"


class All(_M):
    type: Literal["all"] = "all"
    conditions: list["Condition"] = Field(..., min_length=1, max_length=6)


class Any_(_M):
    type: Literal["any"] = "any"
    conditions: list["Condition"] = Field(..., min_length=1, max_length=6)


Condition = Annotated[Union[Compare, Cross, Always, All, Any_], Field(discriminator="type")]
All.model_rebuild()
Any_.model_rebuild()


class AtrStop(_M):
    n: int = Field(14, ge=2, le=100)
    mult: float = Field(2.0, gt=0, le=10)


class RuleSet(_M):
    entry: Condition
    exit: Condition | None = None
    stop_loss_pct: float | None = Field(None, gt=0, le=90)
    take_profit_pct: float | None = Field(None, gt=0, le=1000)
    trailing_stop_pct: float | None = Field(None, gt=0, le=90)
    max_hold_days: int | None = Field(None, ge=1, le=2000)
    atr_stop: AtrStop | None = None

    def has_exit(self) -> bool:
        return any(x is not None for x in (self.exit, self.stop_loss_pct, self.take_profit_pct,
                                           self.trailing_stop_pct, self.max_hold_days, self.atr_stop))


# ---------- обход дерева ----------

def walk_conditions(c: Any):
    yield c
    if isinstance(c, (All, Any_)):
        for x in c.conditions:
            yield from walk_conditions(x)


def walk_exprs(rs: RuleSet):
    for root in (rs.entry, rs.exit):
        if root is None:
            continue
        for c in walk_conditions(root):
            if isinstance(c, (Compare, Cross)):
                yield c.left
                yield c.right


def depth(c: Any) -> int:
    if isinstance(c, (All, Any_)):
        return 1 + max(depth(x) for x in c.conditions)
    return 1


def lookback(e: Any) -> int:
    """Сколько баров нужно индикатору, чтобы устояться."""
    if isinstance(e, (SMA, Highest, Lowest, AvgVolume, Change)):
        return e.n + 1
    if isinstance(e, Bollinger):
        return e.n + 1
    if isinstance(e, (EMA, RSI, ATR)):
        return e.n * 4 + 2
    if isinstance(e, MACD):
        return (e.slow + e.signal) * 3
    if isinstance(e, Stochastic):
        return e.k + e.d + 1
    return 1


def warmup(rs: RuleSet) -> int:
    n = max([lookback(e) for e in walk_exprs(rs)] + [2])
    if rs.atr_stop:
        n = max(n, rs.atr_stop.n * 4 + 2)
    return n


def validate_rules(rs: RuleSet) -> list[str]:
    """Ограничения в коде: модель может собрать формально верное, но бессмысленное дерево."""
    errors: list[str] = []
    for root, name in ((rs.entry, "входа"), (rs.exit, "выхода")):
        if root is None:
            continue
        if depth(root) > 4:
            errors.append(f"условие {name} слишком глубоко вложено")
        nodes = list(walk_conditions(root))
        if len(nodes) > 24:
            errors.append(f"условие {name} слишком сложное: {len(nodes)} узлов")
        for c in nodes:
            if isinstance(c, (Compare, Cross)) and isinstance(c.left, Const) and isinstance(c.right, Const):
                errors.append("сравнение двух чисел не зависит от рынка")
            if isinstance(c, Cross) and (isinstance(c.left, Const) and isinstance(c.right, Const)):
                errors.append("пересечение двух чисел невозможно")
    for e in walk_exprs(rs):
        if isinstance(e, MACD) and e.fast >= e.slow:
            errors.append("у MACD быстрый период должен быть меньше медленного")
    if isinstance(rs.exit, Always) or (rs.exit is not None and any(isinstance(c, Always) for c in walk_conditions(rs.exit))):
        errors.append("always в условии выхода продаёт на следующий день после покупки; выход через N дней "
                      "записывается полем max_hold_days, а «держать до конца» значит exit: null")
    for fld, label in (("stop_loss_pct", "стоп-лосс"), ("take_profit_pct", "тейк-профит"),
                       ("trailing_stop_pct", "трейлинг-стоп")):
        v = getattr(rs, fld)
        if v is not None and 0 < v < 0.5:
            errors.append(f"{label} {v:g}% слишком мал для дневных свечей: проценты пишутся числом, 7 значит 7%")
    return errors


# ---------- шаблоны как готовые деревья ----------

def template_rules(stype: str, p: dict[str, float]) -> RuleSet:
    if stype in ("SMA_CROSSOVER", "EMA_CROSSOVER"):
        f, s = int(p["fast_period"]), int(p["slow_period"])
        Ind = SMA if stype == "SMA_CROSSOVER" else EMA
        fast = Price() if f == 1 else Ind(n=f)
        slow = Ind(n=s)
        return RuleSet(entry=Cross(left=fast, direction="above", right=slow),
                       exit=Cross(left=fast, direction="below", right=slow))
    if stype == "RSI":
        r = RSI(n=int(p["period"]))
        return RuleSet(entry=Compare(left=r, op="<", right=Const(value=p["lower"])),
                       exit=Compare(left=r, op=">", right=Const(value=p["upper"])))
    if stype == "BREAKOUT":
        return RuleSet(entry=Compare(left=Price(), op=">", right=Highest(n=int(p["entry_lookback"]))),
                       exit=Compare(left=Price(), op="<", right=Lowest(n=int(p["exit_lookback"]))))
    return RuleSet(entry=Always())


def match_template(rs: RuleSet) -> tuple[str, dict[str, float]] | None:
    """Дерево, которое в точности совпадает с шаблоном, становится шаблоном.

    Модель вправе записать «SMA 20/50» деревом. Тогда агент теряет понятное название,
    подсказки «а если 10 и 30?» и шаблонные проверки. Сравнение идёт по построенному
    шаблонному дереву, поэтому смысл не меняется ни на байт.
    """
    if any(getattr(rs, f) for f in ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct", "max_hold_days", "atr_stop")):
        return None
    e, x = rs.entry, rs.exit
    cands: list[tuple[str, dict[str, float]]] = []
    if isinstance(e, Always) and x is None:
        cands.append(("BUY_AND_HOLD", {}))
    if isinstance(e, Cross) and isinstance(e.right, (SMA, EMA)):
        stype = "SMA_CROSSOVER" if isinstance(e.right, SMA) else "EMA_CROSSOVER"
        f = 1 if isinstance(e.left, Price) else getattr(e.left, "n", None)
        if f is not None:
            cands.append((stype, {"fast_period": f, "slow_period": e.right.n}))
    if isinstance(e, Compare) and isinstance(e.left, RSI) and isinstance(e.right, Const) \
            and isinstance(x, Compare) and isinstance(x.right, Const):
        cands.append(("RSI", {"period": e.left.n, "lower": e.right.value, "upper": x.right.value}))
    if isinstance(e, Compare) and isinstance(e.right, Highest) and isinstance(x, Compare) and isinstance(x.right, Lowest):
        cands.append(("BREAKOUT", {"entry_lookback": e.right.n, "exit_lookback": x.right.n}))
    dump = rs.model_dump(mode="json", exclude_none=True)
    for stype, params in cands:
        try:
            if template_rules(stype, params).model_dump(mode="json", exclude_none=True) == dump:
                return stype, params
        except (KeyError, ValueError):
            continue
    return None


# ---------- описание по-русски ----------

def _days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} дня"
    return f"{n} дней"


SRC = {"close": "цены закрытия", "open": "цены открытия", "high": "максимумов", "low": "минимумов",
       "volume": "объёма"}


# формы: именительный (что), родительный (выше чего), винительный (пересекает что)
def _f(nom: str, gen: str, acc: str, case: str) -> str:
    return {"nom": nom, "gen": gen, "acc": acc}[case]


def describe_expr(e: Any, case: str = "nom") -> str:
    if isinstance(e, Price):
        return {"close": _f("цена", "цены", "цену", case), "open": _f("цена открытия", "цены открытия", "цену открытия", case),
                "high": _f("дневной максимум", "дневного максимума", "дневной максимум", case),
                "low": _f("дневной минимум", "дневного минимума", "дневной минимум", case),
                "volume": _f("объём", "объёма", "объём", case)}[e.field]
    if isinstance(e, Const):
        return f"{e.value:g}".replace(".", ",")
    if isinstance(e, SMA):
        src = "" if e.source == "close" else f" {SRC[e.source]}"
        return _f("средняя", "средней", "среднюю", case) + src + f" за {_days(e.n)}"
    if isinstance(e, EMA):
        src = "" if e.source == "close" else f" {SRC[e.source]}"
        return _f("экспоненциальная средняя", "экспоненциальной средней", "экспоненциальную среднюю", case) + src + \
            f" за {_days(e.n)}"
    if isinstance(e, RSI):
        return f"RSI({e.n})"
    if isinstance(e, Highest):
        return _f("максимум", "максимума", "максимум", case) + f" за предыдущие {_days(e.n)}"
    if isinstance(e, Lowest):
        return _f("минимум", "минимума", "минимум", case) + f" за предыдущие {_days(e.n)}"
    if isinstance(e, Change):
        return _f("изменение", "изменения", "изменение", case) + f" цены за {_days(e.n)} в %"
    if isinstance(e, AvgVolume):
        return _f("средний объём", "среднего объёма", "средний объём", case) + f" за {_days(e.n)}"
    if isinstance(e, MACD):
        base = f"MACD({e.fast}, {e.slow}, {e.signal})"
        return {"macd": _f("линия", "линии", "линию", case), "signal": _f("сигнальная линия", "сигнальной линии", "сигнальную линию", case),
                "hist": _f("гистограмма", "гистограммы", "гистограмму", case)}[e.line] + " " + base
    if isinstance(e, Bollinger):
        k = f"{e.k:g}".replace(".", ",")
        band = {"upper": _f("верхняя", "верхней", "верхнюю", case), "middle": _f("средняя", "средней", "среднюю", case),
                "lower": _f("нижняя", "нижней", "нижнюю", case)}[e.band]
        return f"{band} " + _f("полоса", "полосы", "полосу", case) + f" Боллинджера ({e.n}, {k})"
    if isinstance(e, Stochastic):
        return _f("стохастик", "стохастика", "стохастик", case) + f" %{e.line.upper()}({e.k}, {e.d})"
    if isinstance(e, ATR):
        return f"ATR({e.n})"
    return "?"


OPS = {">": "выше", "<": "ниже", ">=": "не ниже", "<=": "не выше"}


def _describe_change(c: Any) -> str | None:
    """«Изменение цены за 20 дней ниже -10» по-человечески: «цена за 20 дней упала больше чем на 10%»."""
    if not (isinstance(c.left, Change) and isinstance(c.right, Const)):
        return None
    v, span = c.right.value, f"за {_days(c.left.n)}"
    pct = f"{abs(v):g}%".replace(".", ",")
    if c.op in ("<", "<=") and v < 0:
        return f"цена {span} упала больше чем на {pct}"
    if c.op in (">", ">=") and v > 0:
        return f"цена {span} выросла больше чем на {pct}"
    if c.op in ("<", "<=") and v > 0:
        return f"цена {span} выросла меньше чем на {pct} или упала"
    if c.op in (">", ">=") and v < 0:
        return f"цена {span} упала меньше чем на {pct} или выросла"
    return f"цена {span} {'выросла' if c.op in ('>', '>=') else 'упала'}"


def describe_condition(c: Any, nested: bool = False) -> str:
    if isinstance(c, Always):
        return "сразу, при первой возможности"
    if isinstance(c, Compare):
        ch = _describe_change(c)
        if ch:
            return ch
        return f"{describe_expr(c.left)} {OPS[c.op]} {describe_expr(c.right, 'gen')}"
    if isinstance(c, Cross):
        d = "снизу вверх" if c.direction == "above" else "сверху вниз"
        return f"{describe_expr(c.left)} пересекает {describe_expr(c.right, 'acc')} {d}"
    word = " и " if isinstance(c, All) else " или "
    inner = word.join(describe_condition(x, True) for x in c.conditions)
    return f"({inner})" if nested and len(c.conditions) > 1 else inner


def describe_rules(rs: RuleSet) -> dict[str, str]:
    if isinstance(rs.entry, Always):
        entry = "Покупаю сразу" + (" и держу до конца периода." if not rs.has_exit() else ".")
    else:
        entry = f"Покупаю, когда {describe_condition(rs.entry)}."
    parts = []
    if rs.exit is not None:
        parts.append(describe_condition(rs.exit))
    if rs.stop_loss_pct:
        parts.append(f"цена упала на {rs.stop_loss_pct:g}% от цены покупки".replace(".", ","))
    if rs.atr_stop:
        parts.append(f"цена ушла ниже цены покупки на {rs.atr_stop.mult:g} ATR({rs.atr_stop.n})".replace(".", ","))
    if rs.take_profit_pct:
        parts.append(f"цена выросла на {rs.take_profit_pct:g}% от цены покупки".replace(".", ","))
    if rs.trailing_stop_pct:
        parts.append(f"цена опустилась на {rs.trailing_stop_pct:g}% от максимума после покупки".replace(".", ","))
    if rs.max_hold_days:
        parts.append(f"прошло {_days(rs.max_hold_days)} после покупки")
    if parts:
        exit_ = "Продаю, когда " + ", или когда ".join(parts) + "."
    else:
        exit_ = "" if isinstance(rs.entry, Always) else "Не продаю до конца периода: условие выхода не задано."
    return {"entry": entry, "exit": exit_,
            "execution": "Решение по закрытию дня, сделка по открытию следующего, только покупка."}


# ---------- параметры для проверки устойчивости ----------

def _all_nodes(rs: RuleSet):
    for e in walk_exprs(rs):
        yield e


def numeric_params(rs: RuleSet) -> list[tuple[str, type, str, float]]:
    """Два главных числа стратегии: (подпись, тип узла, поле, значение). Периоды раньше порогов.

    Параметр определяется значением, а не объектом: одна и та же средняя во входе и в выходе
    считается одним параметром и меняется везде сразу (set_param).
    """
    periods, levels, seen = [], [], set()
    for e in _all_nodes(rs):
        for fld in ("n", "fast", "slow", "k"):
            v = getattr(e, fld, None)
            if not isinstance(v, int) or v <= 1:
                continue
            key = (type(e), fld, v)
            if key in seen:
                continue
            seen.add(key)
            periods.append((describe_expr(e), type(e), fld, float(v)))
    for root in (rs.entry, rs.exit):
        if root is None:
            continue
        for c in walk_conditions(root):
            if isinstance(c, Compare):
                for side in (c.left, c.right):
                    if isinstance(side, Const) and side.value != 0 and (Const, side.value) not in seen:
                        seen.add((Const, side.value))
                        levels.append((f"порог {describe_expr(side)}", Const, "value", float(side.value)))
    stops = [(f, RuleSet, f, float(getattr(rs, f))) for f in ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct")
             if getattr(rs, f)]
    return (periods + levels + stops)[:2]


def set_param(rs: RuleSet, kind: type, fld: str, old: float, new: float) -> None:
    """Заменяет значение параметра во всех узлах дерева, где оно встречается."""
    if kind is RuleSet:
        setattr(rs, fld, new)
        return
    for e in _all_nodes(rs):
        if isinstance(e, kind) and abs(float(getattr(e, fld, float("nan"))) - old) < 1e-9:
            cur = getattr(e, fld)
            setattr(e, fld, int(round(new)) if isinstance(cur, int) else float(new))
