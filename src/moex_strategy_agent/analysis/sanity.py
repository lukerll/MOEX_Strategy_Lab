"""Проверка здравого смысла после расчёта: условие, которое не могло сработать ни разу.

Пример: «покупка ниже 30» без индикатора модель записала как «цена ниже 30 ₽», а Сбербанк
столько не стоил. Пустой отчёт в таком случае вводит в заблуждение, честнее переспросить.
"""
from __future__ import annotations

from typing import Any

from ..domain.rules import Compare, Const, Cross, Price, walk_conditions
from ..domain.schemas import BacktestResult

_OP_RU = {">": "выше", ">=": "не ниже", "<": "ниже", "<=": "не выше"}


def _fmt(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",").replace(",00", "")


def _impossible(c: Any, lo: float, hi: float) -> tuple[str, float] | None:
    """(«ниже»/«выше», порог), если сравнение цены с числом за период не выполнялось никогда."""
    if isinstance(c, Compare):
        left, right, op = c.left, c.right, c.op
        if isinstance(left, Const) and isinstance(right, Price):      # 30 > price == price < 30
            left, right, op = right, left, {">": "<", "<": ">", ">=": "<=", "<=": ">="}[op]
        if isinstance(left, Price) and left.field != "volume" and isinstance(right, Const):
            v = right.value
            if (op in ("<", "<=") and v < lo) or (op in (">", ">=") and v > hi):
                return _OP_RU[op], v
    if isinstance(c, Cross) and isinstance(c.left, Price) and isinstance(c.right, Const):
        v = c.right.value
        if v < lo or v > hi:
            return ("выше" if c.direction == "above" else "ниже"), v
    return None


def dead_entry_hint(res: BacktestResult) -> str | None:
    """Текст вопроса, если вход не мог сработать из-за порога цены вне диапазона за период."""
    if res.trades or not res.spec.rules or not res.equity:
        return None
    closes = [r["close"] for r in res.equity if r.get("close") is not None]
    if not closes:
        return None
    lo, hi = min(closes), max(closes)
    bad = None
    for c in walk_conditions(res.spec.rules.entry):
        bad = _impossible(c, lo, hi)
        if bad:
            break
    if not bad:
        return None
    word, v = bad
    text = (f"Условие входа «цена {word} {_fmt(v)} ₽» не сработало ни разу: за период цена была "
            f"от {_fmt(lo)} до {_fmt(hi)} ₽.")
    if 0 < v < 100:
        exit_v = None
        if res.spec.rules.exit is not None:
            for c in walk_conditions(res.spec.rules.exit):
                if isinstance(c, Compare) and isinstance(c.left, Price) and isinstance(c.right, Const) \
                        and 0 < c.right.value < 100:
                    exit_v = (_OP_RU[c.op], c.right.value)
                    break
        tail = f", а продажа при RSI {exit_v[0]} {_fmt(exit_v[1])}" if exit_v is not None else ""
        text += f" Вы имели в виду RSI {word} {_fmt(v)}{tail}?"
    else:
        text += " Уточните порог или индикатор, и я пересчитаю."
    return text


# ---------- ноль сделок: какое условие входа не сработало ----------

def _nice(v: float) -> float:
    """Порог для подсказки: круглое число того же порядка."""
    a = abs(v)
    step = 5 if a >= 20 else 1 if a >= 5 else 0.5 if a >= 1 else 0.1
    return round(round(v / step) * step, 2)


def entry_check(rows: list[tuple], spec: Any) -> dict[str, Any] | None:
    """Сколько дней за период выполнялось каждое условие входа.

    Считается контрольным вычислителем по тем же барам, что и бэктест. Возвращает None для
    «купить сразу». Поле blocker называет условие, из-за которого сделок не было, и порог,
    при котором оно срабатывало бы примерно каждый десятый день.
    """
    from ..backtest.reference_engine import RefEval
    from ..domain.rules import Always, describe_condition, describe_expr

    rs = spec.rules
    if rs is None or isinstance(rs.entry, Always):
        return None
    o = {"date": [r[0] for r in rows], "open": [r[1] for r in rows], "high": [r[2] for r in rows],
         "low": [r[3] for r in rows], "close": [r[4] for r in rows],
         "volume": [float(r[5]) if len(r) > 5 else 0.0 for r in rows]}
    idx = [i for i, d in enumerate(o["date"]) if d >= spec.start_date and i >= 1]
    if not idx:
        return None
    ev = RefEval(o)
    leaves = [c for c in walk_conditions(rs.entry) if isinstance(c, (Compare, Cross))]
    stat = [{"text": describe_condition(c), "days": sum(1 for i in idx if ev.cond(c, i))} for c in leaves]
    out: dict[str, Any] = {"days_total": len(idx), "entry_days": sum(1 for i in idx if ev.cond(rs.entry, i)),
                           "conditions": stat}
    if not stat:
        return out
    k = min(range(len(stat)), key=lambda j: stat[j]["days"])
    c, blocker = leaves[k], dict(stat[k])
    if isinstance(c, Compare) and isinstance(c.right, Const) and not isinstance(c.left, Const):
        vals = sorted(v for v in (ev.series(c.left)[i] for i in idx) if v is not None and v == v)
        if vals:
            below = c.op in ("<", "<=")
            reach = vals[0] if below else vals[-1]
            q = vals[int(len(vals) * 0.1)] if below else vals[min(len(vals) - 1, int(len(vals) * 0.9))]
            blocker.update(expr=describe_expr(c.left), word=_OP_RU[c.op], threshold=c.right.value,
                           reach=round(reach, 2), reach_word="минимум" if below else "максимум",
                           suggest=_nice(q))
    out["blocker"] = blocker
    return out


def _g(v: float) -> str:
    return f"{v:g}".replace(".", ",")


def no_trades_text(check: dict[str, Any] | None) -> str | None:
    """Объяснение для отчёта без сделок. None, если вход срабатывал или проверки нет."""
    if not check or check.get("entry_days") or not check.get("conditions"):
        return None
    b, total = check["blocker"], check["days_total"]
    if b["days"] == 0:
        text = f"Ни одной сделки: условие входа «{b['text']}» не выполнилось ни разу за {total} торговых дней."
        if "reach" in b:
            text += f" {b['reach_word'].capitalize()} {b['expr']} за период: {_g(b['reach'])}."
            if b["suggest"] != b["threshold"]:
                text += f" С порогом {_g(b['suggest'])} условие срабатывало бы примерно каждый десятый день."
        return text
    parts = ", ".join(f"«{c['text']}»: {c['days']}" for c in check["conditions"])
    return (f"Ни одной сделки: условия входа выполнялись по отдельности, но ни разу в один день. "
            f"Дней, когда условие было верно, из {total}: {parts}.")


def no_trades_suggestion(check: dict[str, Any] | None) -> str | None:
    """Следующий шаг кнопкой: ослабленный порог или вход без самого редкого условия."""
    if not check or check.get("entry_days") or not check.get("conditions"):
        return None
    b = check["blocker"]
    if b["days"] == 0 and "suggest" in b and b["suggest"] != b["threshold"]:
        return f"а если {b['expr']} {b['word']} {_g(b['suggest'])}?"
    if b["days"] > 0 and len(check["conditions"]) > 1:
        return f"а если убрать условие «{b['text']}»?"
    return None
