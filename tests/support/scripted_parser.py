"""Бывший офлайн-разборщик. Теперь живёт только в тестах: на нём стоит имитация GigaChat.

В рабочем коде формализацию делает модель. Здесь регулярки нужны, чтобы тесты и
офлайн-прогон eval проверяли весь путь агента (OAuth, JSON, проверка схемы, граф)
без сети и ключей.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from moex_strategy_agent.data.demo_source import DEMO_UNIVERSE
from moex_strategy_agent.data.market_data import KNOWN_NAMES
from moex_strategy_agent.agent.state import Understanding

NOT_TICKERS = {"SMA", "EMA", "RSI", "ISS", "API", "MACD", "USD", "RUB", "CAGR", "ETF", "IPO", "HTML",
               "CSV", "PDF", "JSON", "MSCI", "OK", "AND", "HOLD", "BUY", "SELL", "LLM", "EUR", "CNY", "МСФО"}
KNOWN_TICKERS = set(KNOWN_NAMES.values()) | set(DEMO_UNIVERSE)

MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8,
          "сен": 9, "окт": 10, "ноя": 11, "дек": 12}


def _norm(text: str) -> str:
    t = text.lower().replace("ё", "е")
    return re.sub(r"[–—−]", "-", t)


def _num(s: str) -> float:
    return float(s.replace(" ", "").replace(" ", "").replace(",", "."))


# ---------- тикер ----------

_VENUE = re.compile(r"(данн\w*|на|с|котировк\w*|истори\w*)\s+(мосбирж\w*|московск\w+ бирж\w*|moex)"
                    r"|moex\s+iss|мосбирж\w*\s+iss", re.IGNORECASE)


def find_tickers(text: str) -> list[str]:
    """Все упомянутые бумаги по порядку. «На данных Мосбиржи» — площадка, а не акция MOEX."""
    cleaned = _VENUE.sub(" ", text)
    found: list[tuple[int, str]] = []
    for m in re.finditer(r"\b[A-Z][A-Z0-9]{2,5}\b", cleaned):
        tok = m.group(0)
        if tok not in NOT_TICKERS and (tok in KNOWN_TICKERS or len(tok) >= 4):
            found.append((m.start(), tok))
    low = _norm(cleaned)
    for name, ticker in KNOWN_NAMES.items():
        for m in re.finditer(r"(?<![а-яa-z])" + re.escape(name) + r"[а-я]*", low):
            found.append((m.start(), ticker))
    out: list[str] = []
    for _, t in sorted(found):
        if t not in out:
            out.append(t)
    return out


def parse_ticker(text: str) -> str | None:
    tickers = find_tickers(text)
    return tickers[0] if tickers else None


# ---------- период ----------

def parse_period(text: str, today: date) -> tuple[date | None, date | None, str | None]:
    low = _norm(text)
    d = r"(\d{1,2})\.(\d{1,2})\.(\d{4})"
    m = re.search(rf"{d}\s*(?:-|по|до)\s*{d}", low)
    if m:
        g = list(map(int, m.groups()))
        return date(g[2], g[1], g[0]), date(g[5], g[4], g[3]), None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})\s*(?:-|по|до|\.\.)\s*(\d{4})-(\d{2})-(\d{2})", low)
    if m:
        g = list(map(int, m.groups()))
        return date(g[0], g[1], g[2]), date(g[3], g[4], g[5]), None
    m = re.search(r"(?:с\s+)?(20\d\d|19\d\d)\s*(?:-|по|до)\s*(20\d\d|19\d\d)", low)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b:
            return date(a, 1, 1), date(b, 12, 31), None
    m = re.search(r"с\s+(20\d\d)\s*(?:года|г\.?)?(?!\s*(?:-|по|до)\s*\d)", low)
    if m:
        return date(int(m.group(1)), 1, 1), today, None
    m = re.search(r"(?:за|в)\s+(20\d\d)\s*(?:год|г\b|г\.)", low)
    if m:
        y = int(m.group(1))
        return date(y, 1, 1), date(y, 12, 31), None
    m = re.search(r"последн\w*\s+(\d+)?\s*(год|года|лет|месяц\w*)", low) or \
        re.search(r"за\s+(\d+)\s*(год|года|лет|месяц\w*)", low)
    if m:
        n = int(m.group(1) or 1)
        days = n * 30 if m.group(2).startswith("месяц") else n * 365
        return today - timedelta(days=days), today, None
    if re.search(r"последний год|за год\b", low):
        return today - timedelta(days=365), today, None
    years = re.findall(r"(?<![\d.])(19\d\d|20\d\d)(?![\d.])", low)
    if len(years) == 1:
        y = int(years[0])
        return date(y, 1, 1), min(date(y, 12, 31), today), None
    return None, None, None


# ---------- стратегия ----------

def _ma_periods(low: str) -> list[int]:
    nums: list[int] = []
    pats = [r"(\d{1,3})\s*-?\s*(?:дневн|дн\.|days?|периодн)",
            r"(?:sma|ema|ma|средн\w*|скользящ\w*)\s*\(?\s*(\d{1,3})",
            r"(\d{1,3})\s*(?:/|и|x|×)\s*(\d{1,3})",
            r"период\w*\s*(\d{1,3})"]
    for p in pats:
        for m in re.finditer(p, low):
            for g in m.groups():
                v = int(g)
                if 2 <= v <= 400 and v not in nums:
                    nums.append(v)
    return nums


PRICE_VS_MA = re.compile(r"цен\w*[^.;]{0,30}(выше|ниже|над|под|пересека\w*|пробива\w*)[^.;]{0,30}"
                         r"(средн\w*|скользящ\w*|sma|ema|ma\b)")


def parse_strategy(text: str) -> tuple[str | None, dict[str, float]]:
    low = _norm(text)
    params: dict[str, float] = {}
    if re.search(r"\brsi\b|\bрси\b|относительной силы", low):
        m = re.search(r"rsi\s*\(?\s*(\d{1,3})\s*\)?", low) or re.search(r"период\w*\s*(\d{1,3})", low)
        if m and int(m.group(1)) > 1:
            params["period"] = int(m.group(1))
        lo = re.search(r"(?:ниже|меньше|под|опуска\w*\s+до|<)\s*(\d{1,2})", low)
        hi = re.search(r"(?:выше|больше|над|поднима\w*\s+до|>)\s*(\d{1,2})", low)
        pair = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})", low)
        if lo:
            params["lower"] = int(lo.group(1))
        if hi:
            params["upper"] = int(hi.group(1))
        if pair and "lower" not in params:
            params["lower"], params["upper"] = sorted((int(pair.group(1)), int(pair.group(2))))
        return "RSI", params
    if re.search(r"пробо|breakout|дончиан|donchian|канал\w*|максимум\w* за", low):
        m = re.search(r"максимум\w*\s*(?:за)?\s*(\d{1,3})", low) or re.search(r"(\d{1,3})\s*-?\s*дневн\w*\s*максимум", low)
        if m:
            params["entry_lookback"] = int(m.group(1))
        m = re.search(r"минимум\w*\s*(?:за)?\s*(\d{1,3})", low) or re.search(r"(\d{1,3})\s*-?\s*дневн\w*\s*минимум", low)
        if m:
            params["exit_lookback"] = int(m.group(1))
        return "BREAKOUT", params
    is_ema = bool(re.search(r"\bema\b|экспоненц", low))
    is_sma = bool(re.search(r"\bsma\b|скользящ|средн\w+|\bma\b", low))
    pv = PRICE_VS_MA.search(low)
    if pv and len(_ma_periods(low)) == 1:
        params["fast_period"], params["slow_period"] = 1, _ma_periods(low)[0]
        return ("EMA_CROSSOVER" if is_ema else "SMA_CROSSOVER"), params
    if is_ema or is_sma:
        periods = _ma_periods(low)
        if len(periods) >= 2:
            params["fast_period"], params["slow_period"] = sorted(periods[:2])
        return ("EMA_CROSSOVER" if is_ema else "SMA_CROSSOVER"), params
    if re.search(r"купи\w* и держ|buy\s*(?:&|and)\s*hold|просто держ", low):
        return "BUY_AND_HOLD", params
    return None, params


def parse_money(text: str) -> dict[str, float]:
    low = _norm(text)
    out: dict[str, float] = {}
    m = re.search(r"(\d[\d\s ]*(?:[.,]\d+)?)\s*(млн|миллион\w*|тыс\w*|к\b|k\b)?\s*(?:руб|₽|р\.)", low) or \
        re.search(r"(?:капитал\w*|сумм\w*|депозит\w*|деньг\w*)\D{0,15}(\d[\d\s ]*(?:[.,]\d+)?)\s*(млн|миллион\w*|тыс\w*|к\b|k\b)?", low)
    if m:
        v = _num(m.group(1).strip())
        mult = m.group(2) or ""
        v *= 1_000_000 if mult.startswith(("млн", "миллион")) else 1000 if mult.startswith(("тыс", "к", "k")) else 1
        if 1000 <= v <= 1e10:
            out["initial_cash"] = v
    m = re.search(r"комисси\w*\D{0,20}(\d+(?:[.,]\d+)?)\s*%", low)
    if m:
        out["commission"] = _num(m.group(1)) / 100
    elif re.search(r"без комисси", low):
        out["commission"] = 0.0
    m = re.search(r"проскальзыв\w*\D{0,20}(\d+(?:[.,]\d+)?)\s*%", low)
    if m:
        out["slippage"] = _num(m.group(1)) / 100
    elif re.search(r"без проскальзыв", low):
        out["slippage"] = 0.0
    return out


# ---------- границы возможностей ----------

UNSUPPORTED: list[tuple[str, str, str]] = [
    (r"\bmacd\b|макд", "MACD", "пересечение EMA 12/26: на этих средних и строится MACD"),
    (r"боллинджер|bollinger", "полосы Боллинджера", "RSI по уровням или пробой канала"),
    (r"стохастик|stochastic", "стохастик", "RSI по уровням"),
    (r"ишимоку|ichimoku", "Ишимоку", "пересечение средних"),
    (r"\bvwap\b", "VWAP", "пересечение средних"),
    (r"\batr\b|average true range", "ATR", "пробой канала"),
    (r"парабол|parabolic|\bsar\b", "Parabolic SAR", "пробой канала"),
    (r"\badx\b|\bcci\b|\bobv\b|фибоначч|fibonacci", "этот индикатор", "SMA, EMA, RSI или пробой канала"),
    (r"(?<!без )(стоп.?лосс|stop.?loss|тейк.?профит|take.?profit|трейлинг)", "стоп-лоссы и тейк-профиты",
     "та же стратегия с выходом по сигналу; напишите «без стопов», и я посчитаю"),
    (r"\bшорт|\bshort\b|коротк\w+ позици|продаж\w* без покрыт", "короткие позиции",
     "только длинные позиции"),
    (r"мартингейл|martingale|усреднен", "мартингейл и усреднение", "вход всем капиталом по сигналу"),
    (r"минутн|часов\w* свеч|\b\d+\s*(мин|m)\b|интрадей|intraday|внутридн", "внутридневные свечи",
     "дневные свечи"),
    (r"нейросет|машинн\w* обучен|\blstm\b|прогноз\w* цен", "прогнозирование ценой модели",
     "проверка правил на истории"),
]

SUPPORTED_TEXT = ("пересечение SMA или EMA (в том числе цена против одной средней), RSI по уровням, "
                  "пробой канала, «купить и держать»")


def _strategy_kinds(low: str) -> set[str]:
    kinds = set()
    if re.search(r"\brsi\b|\bрси\b|относительной силы", low):
        kinds.add("RSI")
    if re.search(r"пробо|breakout|дончиан|donchian", low):
        kinds.add("BREAKOUT")
    if re.search(r"\bema\b|экспоненц", low):
        kinds.add("EMA")
    if re.search(r"\bsma\b|простая скользящ", low) or (
            re.search(r"скользящ|средн\w+", low) and "EMA" not in kinds and "RSI" not in kinds):
        kinds.add("SMA")
    return kinds


def scope_check(text: str) -> dict[str, Any]:
    """Код решает, что агент не будет подменять: неподдерживаемое и несколько задач сразу."""
    low = _norm(text)
    for pat, name, nearest in UNSUPPORTED:
        if re.search(pat, low):
            return {"kind": "unsupported", "name": name, "nearest": nearest}
    kinds = _strategy_kinds(low)
    tickers = find_tickers(text)
    if len(kinds) > 1 and re.search(r"\b(и|а потом|ещё|еще|плюс|также|затем)\b|,", low):
        return {"kind": "multi", "what": "стратегии: " + ", ".join(sorted(kinds))}
    if len(tickers) > 1 and kinds:
        return {"kind": "multi", "what": "бумаги: " + ", ".join(tickers)}
    return {}


# ---------- намерение ----------

def classify(text: str, has_strategy: bool, has_changes: bool, has_last: bool) -> str:
    low = _norm(text)
    if has_strategy:
        return "backtest"
    if re.search(r"\bудал(и|ить)\b", low):
        return "delete"
    if re.search(r"\bсохран", low):
        return "save"
    if re.search(r"сравни\w*\s+(с\s+)?(предыдущ|прошл|прежн)", low):
        return "compare"
    if re.search(r"истори|прошл\w+ (запуск|исследован|тест)|что (я|мы) тестировал|список (исследован|запуск)", low):
        return "history"
    if re.search(r"новост|событи", low):
        return "news"
    if re.search(r"сделк", low) and re.search(r"покаж|список|худш|лучш|журнал|какие|выведи", low):
        return "trades"
    if has_changes and has_last:
        return "modify"
    if re.search(r"почему|объясни|где .*(терял|проигр|слаб)|слаб\w+ мест|просадк|в ч[её]м причин|плох", low):
        return "explain" if has_last else "help"
    if re.search(r"что (ты )?умеешь|помощ|\bhelp\b|привет|здравствуй|как пользоваться|что делать", low):
        return "help"
    if re.search(r"протестир|бэктест|backtest|проверь стратег|прогони|тестир|торгова|торгуй|торгуем", low):
        return "backtest"
    if has_changes:
        return "backtest"          # бумага или период без стратегии: это незаконченный запрос, а не «не по теме»
    return "out_of_scope"


def understand(text: str, *, today: date, last_spec: dict[str, Any] | None = None) -> Understanding:
    ticker = parse_ticker(text)
    start, end, _ = parse_period(text, today)
    stype, params = parse_strategy(text)
    money = parse_money(text)
    low = _norm(text)

    patch: dict[str, Any] = {}
    if ticker:
        patch["ticker"] = ticker
    if start:
        patch["start_date"], patch["end_date"] = start.isoformat(), end.isoformat()
    if stype:
        patch["strategy_type"] = stype
    if params:
        patch["parameters"] = params
    patch.update(money)

    # «а если 10 и 30» без названия стратегии: периоды для прошлой стратегии
    if not stype and last_spec and re.search(r"\bа\s+(если|теперь|для|за|с)\b|поменя|измени|то же|тот же|те же", low):
        nums = _ma_periods(low)
        lt = last_spec.get("strategy_type")
        if lt in ("SMA_CROSSOVER", "EMA_CROSSOVER") and len(nums) >= 2:
            a, b = sorted(nums[:2])
            patch["parameters"] = {"fast_period": a, "slow_period": b}
        elif lt == "RSI":
            _, rp = parse_strategy("rsi " + text)
            rp.pop("period", None) if "period" not in low else None
            if rp:
                patch["parameters"] = rp

    assumptions: list[str] = []
    if stype in ("SMA_CROSSOVER", "EMA_CROSSOVER") and params.get("fast_period") == 1:
        n = int(params["slow_period"])
        assumptions.append(f"«цена против {n}-дневной средней» посчитана как пересечение средних 1/{n}: "
                           "покупка, когда цена пересекает среднюю снизу вверх, продажа при обратном")
    elif stype in ("SMA_CROSSOVER", "EMA_CROSSOVER"):
        raw = _ma_periods(low)
        if len(raw) >= 2 and raw[0] > raw[1]:
            assumptions.append(f"периоды переставлены: быстрая средняя {raw[1]}, медленная {raw[0]}")

    has_changes = bool(patch)
    intent = classify(text, bool(stype), has_changes, bool(last_spec))
    u = Understanding(intent=intent, spec_patch=patch, assumptions=assumptions,
                      source="rules", reasoning=f"правила: intent={intent}, поля={sorted(patch)}")
    m = re.search(r"\b(r_[0-9a-f]{8}|[0-9a-f]{10})\b", text)
    if m:
        u.target_id = m.group(1)
    if intent == "backtest" and not stype and not (last_spec and ticker):
        u.question = ("Какую стратегию проверить? Умею: пересечение SMA или EMA, RSI по уровням, "
                      "пробой канала, «купить и держать».")
    return u
