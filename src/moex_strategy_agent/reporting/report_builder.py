"""Автономный HTML-отчёт по запуску: Обзор, Сделки, Методика.

Файл открывается без сети и без скриптов, его можно переслать и распечатать. Выводы
в обзоре считает код по результату расчёта, модель в отчёте не участвует.
"""
from __future__ import annotations

import re
from html import escape

from ..agent.guardrails import DISCLAIMER
from ..domain.schemas import STRATEGY_TITLES, BacktestResult
from .charts import line_chart

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f8f7;color:#0a1a14;font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1160px;margin:0 auto;padding:0 32px}
.band{background:#000a06;color:#fff;padding:30px 0 30px}
.eyebrow{font-size:12px;color:#8a938f;letter-spacing:.02em}
.band .eyebrow{color:#7d8582}
h1{font-weight:300;font-size:40px;line-height:1.1;letter-spacing:-.03em;margin:10px 0 6px}
.sub{color:#c9d1cd;font-size:14px;margin:0}
.method{display:inline-flex;gap:8px;flex-wrap:wrap;margin-top:14px;font-size:12.5px;color:#c9d1cd}
.method span{border:1px solid rgba(255,255,255,.18);border-radius:999px;padding:2px 10px}
.method a{color:#7affb4}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:22px}
.kpi{border:1px solid rgba(255,255,255,.16);border-radius:16px;padding:16px 18px}
.kpi .v{font-weight:300;font-size:36px;line-height:1;letter-spacing:-.03em;margin-top:8px;font-variant-numeric:tabular-nums}
.kpi .n{font-size:12.5px;color:#9aa39f;margin-top:6px}
.kpi.hl{background:#7affb4;color:#000a06;border-color:#7affb4}.kpi.hl .eyebrow,.kpi.hl .n{color:#0b5c37}
h2{font-weight:400;font-size:26px;letter-spacing:-.02em;margin:44px 0 14px}
h3{font-weight:500;font-size:14px;margin:26px 0 8px}
.card{background:#fff;border:1px solid rgba(0,10,6,.1);border-radius:16px;padding:22px 24px;margin-bottom:12px}
.obs{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.obs .card{margin:0}.obs b{font-weight:500}
.obs .k{font-size:12px;color:#6b7570;margin-bottom:4px}
.legend{display:flex;flex-wrap:wrap;gap:6px 20px;font-size:12.5px;color:#6b7570;margin:0 0 6px}.legend span{white-space:nowrap}
.chart{overflow-x:auto}
.legend i{display:inline-block;width:18px;height:0;border-top:2px solid #0b7a48;vertical-align:middle;margin-right:6px}
.legend i.bh{border-top:2px dashed #8a938f}.legend i.dd{border-top:6px solid rgba(200,56,44,.25)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid rgba(0,10,6,.08)}
th{font-weight:400;font-size:12px;color:#6b7570}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
.pos{color:#0b7a48}.neg{color:#c8382c}.muted{color:#6b7570}
.tag{font-size:11px;border-radius:999px;padding:1px 8px;margin-left:6px;white-space:nowrap}
.tag.best{background:#dff7ea;color:#0b5c37}.tag.worst{background:#fbe7e5;color:#9e2b21}
details{border-top:1px solid rgba(0,10,6,.08);padding:10px 0}details:first-of-type{border-top:0}
summary{cursor:pointer;font-weight:500;font-size:14px;list-style:none;display:flex;justify-content:space-between}
summary::-webkit-details-marker{display:none}summary::after{content:"+";color:#8a938f}details[open] summary::after{content:"−"}
details>div{padding-top:10px}
.badge{display:inline-block;border-radius:999px;padding:1px 10px;font-size:12px;background:#dff7ea;color:#0b5c37}
.badge.bad{background:#fbe7e5;color:#9e2b21}
.warn{background:#fff6dc;border-color:#f0dfa7}
ul{margin:6px 0;padding-left:20px}li{margin:3px 0}
p{margin:6px 0}
svg{display:block;width:100%;height:auto}
.foot{color:#6b7570;font-size:13px;padding:36px 0 44px}
@media (max-width:720px){.chart svg{min-width:680px}.wrap{padding:0 16px}h1{font-size:30px}.kpis{grid-template-columns:1fr 1fr}.kpi .v{font-size:28px}
.obs{grid-template-columns:1fr}.hide-m{display:none}h2{font-size:22px;margin-top:34px}}
@media print{body{background:#fff}.band{-webkit-print-color-adjust:exact;print-color-adjust:exact}.card{break-inside:avoid}}
"""

NAMES = {"SBER": "Сбербанк", "GAZP": "Газпром", "LKOH": "Лукойл", "YDEX": "Яндекс", "VTBR": "ВТБ", "ROSN": "Роснефть",
         "GMKN": "Норникель", "NVTK": "Новатэк", "MGNT": "Магнит", "TATN": "Татнефть", "MTSS": "МТС", "MOEX": "Мосбиржа"}
STRATEGY_GREEN, BH_GREY, DD_RED, INK_MARK = "#0b7a48", "#8a938f", "#c8382c", "#0a1a14"


_FONT_CSS: str | None = None
FONT_FILES = [("Inter", 300, "inter-{s}-300-normal.woff2"),
              ("Inter", 400, "inter-{s}-400-normal.woff2"),
              ("Inter", 500, "inter-{s}-500-normal.woff2"),
              ("JetBrains Mono", 400, "jetbrains-mono-{s}-400-normal.woff2")]
RANGES = {"latin": "U+0000-00FF,U+2000-206F,U+20BD,U+2190-21FF,U+2500-257F",
          "cyrillic": "U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116"}


_REASONS = {"signal": "сигнал", "stop_loss": "стоп-лосс", "take_profit": "тейк-профит", "trailing": "трейлинг-стоп",
            "max_hold": "срок удержания", "atr_stop": "стоп по ATR"}


def font_css() -> str:
    """Шрифты встраиваются в отчёт base64: файл открывается без сети и выглядит как интерфейс."""
    global _FONT_CSS
    if _FONT_CSS is None:
        import base64

        from ..config import ROOT
        rules = []
        for family, weight, pattern in FONT_FILES:
            for subset, rng in RANGES.items():
                p = ROOT / "ui" / "web" / "fonts" / pattern.format(s=subset)
                if p.exists():
                    data = base64.b64encode(p.read_bytes()).decode()
                    rules.append(f"@font-face{{font-family:'{family}';font-weight:{weight};font-display:swap;"
                                 f"src:url(data:font/woff2;base64,{data}) format('woff2');unicode-range:{rng}}}")
        _FONT_CSS = "".join(rules)
    return _FONT_CSS


def _fmt(v: float | None, suffix: str = "%", sign: bool = True, d: int = 2) -> str:
    if v is None:
        return "н/д"
    s = f"{v:+.{d}f}" if sign else f"{v:.{d}f}"
    return s.replace(".", ",").replace("-", "−") + suffix


def _money(v: float, sign: bool = False) -> str:
    s = f"{abs(v):,.0f}".replace(",", " ")
    pre = "−" if v < 0 else ("+" if sign and v > 0 else "")
    return f"{pre}{s} ₽"


def _dmy(d) -> str:
    s = str(d)[:10]
    return f"{s[8:10]}.{s[5:7]}.{s[:4]}"


def _price(v: float | None) -> str:
    return "" if v is None else f"{v:,.2f}".replace(",", "\u00a0").replace(".", ",")


def _tone(v: float | None) -> str:
    return "" if v is None else "pos" if v > 0.004 else "neg" if v < -0.004 else ""


def _days(n: int) -> str:
    k = n % 100
    w = "дней" if 11 <= k <= 14 else "день" if n % 10 == 1 else "дня" if n % 10 in (2, 3, 4) else "дней"
    return f"{n} {w}"


# ---------- наблюдения обзора: считаются по сделкам и кривой капитала ----------

def _concentration(res: BacktestResult) -> str | None:
    tr = res.trades
    if len(tr) < 2:
        return None
    total = sum(t.pnl for t in tr)
    if total > 0:
        best = max(tr, key=lambda t: t.pnl)
        share = best.pnl / total * 100
        when = f"с {_dmy(best.entry_date)} по {_dmy(best.exit_date) if best.exit_date else 'конец периода'}"
        if share > 100:
            return (f"Лучшая сделка ({when}) принесла {_money(best.pnl, True)}, больше всей итоговой прибыли "
                    f"{_money(total, True)}: остальные сделки в сумме убыточны.")
        return (f"Лучшая сделка ({when}) принесла <b>{_fmt(share, sign=False, d=1)}</b> всей прибыли: "
                f"{_money(best.pnl, True)} из {_money(total, True)}.")
    worst = min(tr, key=lambda t: t.pnl)
    share = worst.pnl / total * 100 if total else 0
    when = f"с {_dmy(worst.entry_date)} по {_dmy(worst.exit_date) if worst.exit_date else 'конец периода'}"
    return (f"Худшая сделка ({when}) дала <b>{_fmt(share, sign=False, d=1)}</b> всего убытка: "
            f"{_money(worst.pnl, True)} из {_money(total, True)}.")


def _recovery(res: BacktestResult) -> tuple[str | None, dict | None]:
    dds = (res.diagnostics or {}).get("drawdowns") or []
    if not dds:
        return None, None
    d = dds[0]
    depth = _fmt(d["depth_pct"], sign=False, d=1)
    if d.get("recovery"):
        text = (f"Самая глубокая просадка <b>{depth}</b>: от пика {_dmy(d['start'])} до дна {_dmy(d['trough'])}, "
                f"восстановление {_dmy(d['recovery'])}. Ждать пришлось <b>{_days(d['days'])}</b>.")
    else:
        text = (f"Самая глубокая просадка <b>{depth}</b>: от пика {_dmy(d['start'])} до дна {_dmy(d['trough'])}. "
                f"К концу периода капитал так и не вернулся к пику, прошло <b>{_days(d['days'])}</b>.")
    return text, d


def _sens_text(sens: list[dict], cur: float) -> str:
    others = [x["return_pct"] for x in sens if not x.get("is_current")]
    if len(others) < 2:
        return ""
    lo, hi = min(others), max(others)
    med = sorted(others)[len(others) // 2]
    same = all((x > 0) == (cur > 0) or abs(x) < 0.5 for x in others)
    verdict = ("результат сильно зависит от точных чисел: соседние настройки дают и прибыль, и убыток" if not same
               else "текущие числа заметно лучше соседних, похоже на подгонку под историю"
               if cur - med > max(10, abs(med)) else "результат держится не на удачно подобранных числах")
    return (f"Если сдвинуть настройки на четверть в обе стороны, доходность будет от {_fmt(lo, d=1)} "
            f"до {_fmt(hi, d=1)}, у текущих {_fmt(cur, d=1)}: {verdict}.")


def _regimes_text(rg: list[dict]) -> str:
    if not rg:
        return ""
    loc = {"рост": "в росте", "боковик": "в боковике", "падение": "в падении"}
    name = {"рост": "растущем", "боковик": "боковом", "падение": "падающем"}
    lst = ", ".join(f"{loc.get(x['regime'], x['regime'])} {_fmt(x['strategy_pct'], d=1)}" for x in rg)
    win = [x["regime"] for x in rg if x["strategy_pct"] > 0.5]
    lose = [x["regime"] for x in rg if x["strategy_pct"] < -0.5]
    out = ("стратегия теряла при любом рынке" if not win else "стратегия зарабатывала при любом рынке" if not lose
           else f"стратегия зарабатывает только на {name[win[0]]} рынке" if len(win) == 1
           else f"стратегия зарабатывает на {' и '.join(name[w] for w in win)} рынке, а на "
                f"{' и '.join(name[w] for w in lose)} теряет")
    return f"{lst[0].upper()}{lst[1:]}: {out}."


def build_html(res: BacktestResult) -> str:
    m, b, s = res.metrics, res.benchmark, res.spec
    u, diag, v = res.understood or {}, res.diagnostics or {}, res.verification
    company = NAMES.get(s.ticker, s.ticker)
    rule_name = s.title().split(": ", 1)[-1]
    dates = [r["date"] for r in res.equity]

    # ---------- графики: капитал с пиком, дном и восстановлением; под ним просадка на той же шкале ----------
    rec_text, dd = _recovery(res)
    marks = []
    if dd and dates:
        idx = {d: i for i, d in enumerate(dates)}
        for key, label, color, above in (("start", "пик", INK_MARK, True), ("trough", "дно", DD_RED, False),
                                         ("recovery", "восстановление", STRATEGY_GREEN, True)):
            if dd.get(key) in idx:
                marks.append({"i": idx[dd[key]], "label": label, "color": color, "above": above})
    eq_svg = line_chart(dates, [
        {"name": "Купить и держать", "values": [r["benchmark"] for r in res.equity], "color": BH_GREY, "dash": True, "width": 1.4},
        {"name": "Стратегия", "values": [r["equity"] for r in res.equity], "color": STRATEGY_GREEN, "width": 2}],
        height=320, marks=[{**mk, "series": 1} for mk in marks], x_labels=False) if dates else ""
    dd_svg = line_chart(dates, [{"name": "Просадка стратегии", "values": [r["drawdown"] for r in res.equity],
                                 "color": DD_RED, "width": 1.2, "fill": "rgba(200,56,44,0.14)"}],
                        height=150, fill_zero=True, unit="%") if dates else ""

    # ---------- наблюдения ----------
    conc = _concentration(res)
    obs = "".join(f'<div class="card"><div class="k">{k}</div><p>{txt}</p></div>'
                  for k, txt in (("Откуда результат", conc), ("Сколько ждать после просадки", rec_text)) if txt)

    years = "".join(f"<tr><td>{y['year']}</td><td class='r {_tone(y['strategy_pct'])}'>{_fmt(y['strategy_pct'], d=1)}</td>"
                    f"<td class='r {_tone(y['buy_hold_pct'])}'>{_fmt(y['buy_hold_pct'], d=1)}</td></tr>"
                    for y in diag.get("yearly", []))

    # ---------- сделки ----------
    closed = [t for t in res.trades if not t.is_open]
    best = max(closed, key=lambda t: t.pnl) if len(closed) > 1 else None
    worst = min(closed, key=lambda t: t.pnl) if len(closed) > 1 else None
    rows = []
    for i, t in enumerate(res.trades, 1):
        tag = ('<span class="tag best">лучшая</span>' if t is best and t.pnl > 0 else
               '<span class="tag worst">худшая</span>' if t is worst and t.pnl < 0 else "")
        why = "позиция открыта" if t.is_open else _REASONS.get(t.exit_reason or "", "")
        rows.append(f"<tr><td class='r muted'>{i}</td><td>{_dmy(t.entry_date)}</td><td>{_dmy(t.exit_date) if t.exit_date else '—'}</td>"
                    f"<td class='r'>{t.holding_days}</td><td class='r hide-m'>{_price(t.entry_price)}</td>"
                    f"<td class='r hide-m'>{_price(t.exit_price)}</td>"
                    f"<td class='r {_tone(t.pnl)}'>{_money(t.pnl, True)}{tag}</td><td class='r {_tone(t.return_pct)}'>{_fmt(t.return_pct, d=1)}</td>"
                    f"<td class='hide-m'>{why}</td></tr>")
    trades_html = ("<table><tr><th class='r'>№</th><th>Вход</th><th>Выход</th><th class='r'>Дней</th>"
                   "<th class='r hide-m'>Цена входа</th><th class='r hide-m'>Цена выхода</th><th class='r'>Результат</th>"
                   "<th class='r'>%</th><th class='hide-m'>Выход по</th></tr>" + "".join(rows) + "</table>"
                   if rows else "<p class='muted'>Сделок не было: условие входа ни разу не выполнилось.</p>")
    wins = sum(1 for t in closed if t.pnl > 0)

    # ---------- методика ----------
    sens = diag.get("sensitivity") or []
    sens_rows = "".join(f"<tr><td>{escape(', '.join(f'{k}: {val:g}'.replace('.', ',') for k, val in x['params'].items()))}"
                        f"{' <span class=muted>(текущие)</span>' if x.get('is_current') else ''}</td>"
                        f"<td class='r {_tone(x['return_pct'])}'>{_fmt(x['return_pct'], d=1)}</td>"
                        f"<td class='r'>{_fmt(x['max_drawdown_pct'], sign=False, d=1)}</td><td class='r'>{x['trades']}</td></tr>"
                        for x in sens)
    num = lambda x: re.sub(r"(?<=\d)\.(?=\d)", ",", str(x)).replace("-", "−")   # noqa: E731
    checks = "".join(f"<tr><td>{escape(c.name)}</td><td class='r'>{escape(num(c.main))}</td><td class='r'>{escape(num(c.reference))}</td>"
                     f"<td>{'совпало' if c.ok else '<span class=neg>расхождение</span>'}</td></tr>" for c in v.checks)
    news = ""
    for e in res.events:
        titles = [n for n in e.news if not n.flagged_injection][:3]
        if titles:
            items = "".join(f"<li>{escape(n.title)} <span class='muted'>{escape(n.source)}</span></li>" for n in titles)
            news += f"<tr><td>{_dmy(e.date)}</td><td class='r {_tone(e.change_pct)}'>{_fmt(e.change_pct, d=1)}</td><td><ul>{items}</ul></td></tr>"
    warns = "".join(f"<li>{escape(w)}</li>" for w in res.data.warnings)
    assumptions = "".join(f"<li>{escape(a)}</li>" for a in res.assumptions)
    metric_rows = [("Итоговый капитал", _money(m.final_equity), _money(b.final_equity)),
                   ("Доходность", _fmt(m.total_return_pct), _fmt(b.total_return_pct)),
                   ("CAGR", _fmt(m.cagr_pct), _fmt(b.cagr_pct)),
                   ("Макс. просадка", _fmt(m.max_drawdown_pct, sign=False), _fmt(b.max_drawdown_pct, sign=False)),
                   ("Волатильность, годовая", _fmt(m.volatility_pct, sign=False), _fmt(b.volatility_pct, sign=False)),
                   ("Шарп, ставка 0", _fmt(m.sharpe, "", sign=False), _fmt(b.sharpe, "", sign=False)),
                   ("Прибыльных сделок", _fmt(m.win_rate_pct, sign=False), "—"),
                   ("Profit factor", _fmt(m.profit_factor, "", sign=False), "—"),
                   ("Время в позиции", _fmt(m.exposure_pct, sign=False), _fmt(b.exposure_pct, sign=False)),
                   ("Комиссии", _money(m.commissions_paid), _money(b.commissions_paid))]
    metrics = "".join(f"<tr><td>{a}</td><td class='r'>{x}</td><td class='r'>{y}</td></tr>" for a, x, y in metric_rows)

    fail = "" if v.status == "OK" else ('<div class="card warn"><b>Два независимых расчёта не совпали.</b> '
                                        'Цифрам этого отчёта доверять нельзя, подробности в разделе «Методика».</div>')
    synth = '<span>демо: синтетические котировки</span>' if res.data.is_synthetic else ""

    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(company)}, {escape(rule_name)}: отчёт</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23000a06'/%3E%3Cg transform='translate%2832 33%29 scale%28.86%29 translate%28-32 -36%29'%3E%3Cpath d='M12 46V34a20 20 0 0 1 40 0v12' fill='none' stroke='%23ffffff' stroke-width='9' stroke-linecap='round'/%3E%3Cpath d='M25 31v14' stroke='%23ffffff' stroke-width='2' stroke-linecap='round'/%3E%3Crect x='21.5' y='34' width='7' height='8' rx='1.5' fill='%23ffffff'/%3E%3Cpath d='M39 26v21' stroke='%237affb4' stroke-width='2' stroke-linecap='round'/%3E%3Crect x='35.5' y='29' width='7' height='14' rx='1.5' fill='%237affb4'/%3E%3C/g%3E%3C/svg%3E">
<style>{font_css()}{CSS}</style></head><body>
<section class="band"><div class="wrap">
<div class="eyebrow">MOEX Strategy Agent, отчёт {res.run_id} от {res.created_at:%d.%m.%Y}</div>
<h1>{escape(company)}, {escape(rule_name)}</h1>
<p class="sub">{STRATEGY_TITLES[s.strategy_type]}, с {_dmy(s.start_date)} по {_dmy(s.end_date)}, капитал {_money(s.initial_cash)}</p>
<div class="method"><span>Мосбиржа, {res.data.rows} торговых дней</span><span>после комиссий и проскальзывания</span>
<span>без дивидендов</span>{synth}<a href="#method">как считали</a></div>
<div class="kpis">
<div class="kpi hl"><div class="eyebrow">Доходность стратегии</div><div class="v">{_fmt(m.total_return_pct, d=1)}</div>
<div class="n">{_money(m.final_equity - s.initial_cash, True)}</div></div>
<div class="kpi"><div class="eyebrow">Купить и держать</div><div class="v">{_fmt(b.total_return_pct, d=1)}</div>
<div class="n">{_money(b.final_equity - s.initial_cash, True)}</div></div>
<div class="kpi"><div class="eyebrow">Макс. просадка</div><div class="v">{_fmt(m.max_drawdown_pct, sign=False, d=1)}</div>
<div class="n">у держания {_fmt(b.max_drawdown_pct, sign=False, d=1)}</div></div>
<div class="kpi"><div class="eyebrow">Сделки</div><div class="v">{m.trades_count}</div>
<div class="n">прибыльных {wins} из {len(closed)}</div></div>
</div></div></section>
<div class="wrap">
{fail}
<h2>Обзор</h2>
{f'<div class="obs">{obs}</div>' if obs else ''}
<div class="card"><div class="legend"><span><i></i>стратегия</span><span><i class="bh"></i>купить и держать</span>
<span><i class="dd"></i>просадка стратегии</span></div><div class="chart">{eq_svg}{dd_svg}</div></div>
{f'<div class="card"><h3 style="margin-top:0">Доходность по годам</h3><table><tr><th>Год</th><th class="r">Стратегия</th><th class="r">Купить и держать</th></tr>{years}</table></div>' if years else ''}

<h2>Сделки</h2>
<div class="card">{trades_html}</div>

<h2 id="method">Методика</h2>
<div class="card">
<h3 style="margin-top:0">Правила</h3>
{f'<p>{escape(u.get("entry", ""))}</p>' if u.get("entry") else ''}{f'<p>{escape(u.get("exit", ""))}</p>' if u.get("exit") else ''}
<table>
<tr><td class="muted">Исполнение</td><td>сигнал по закрытию дня, сделка по открытию следующего, только покупка</td></tr>
<tr><td class="muted">Размер позиции</td><td>весь доступный капитал, целым числом акций</td></tr>
<tr><td class="muted">Издержки</td><td>комиссия {_fmt(s.commission * 100, sign=False, d=3)} с каждой стороны, проскальзывание {_fmt(s.slippage * 100, sign=False, d=3)} к цене</td></tr>
<tr><td class="muted">Дивиденды</td><td>не учитываются ни в стратегии, ни в «купить и держать»: это ценовая доходность</td></tr>
<tr><td class="muted">Данные</td><td>{escape(res.data.source)}{', синтетика' if res.data.is_synthetic else ''}, с {_dmy(res.data.first_date)} по {_dmy(res.data.last_date)}</td></tr>
</table>
<h3>Метрики</h3>
<table><tr><th>Показатель</th><th class="r">Стратегия</th><th class="r">Купить и держать</th></tr>{metrics}</table>
{f'<h3>Где стратегия зарабатывает</h3><p>{escape(_regimes_text(diag.get("regimes", [])))}</p>' if diag.get("regimes") else ''}
{f'<h3>Устойчивость к настройкам</h3><p>{escape(_sens_text(sens, m.total_return_pct))}</p>' if len(sens) > 2 else ''}
</div>
<div class="card">
{f'<details><summary>Соседние настройки, {len(sens)}</summary><div><table><tr><th>Настройки</th><th class="r">Доходность</th><th class="r">Просадка</th><th class="r">Сделок</th></tr>{sens_rows}</table></div></details>' if sens_rows else ''}
<details><summary><span>Сверка двух расчётов <span class="badge{'' if v.status == 'OK' else ' bad'}">{'совпали' if v.status == 'OK' else 'не совпали'}</span></span></summary><div>
<p class="muted">Тот же тест независимо посчитан библиотекой backtesting.py и собственным движком.</p>
<table><tr><th>Проверка</th><th class="r">Основной</th><th class="r">Контрольный</th><th>Итог</th></tr>{checks}</table></div></details>
{f'<details><summary>Новости рядом с резкими движениями</summary><div><p class="muted">Новость рядом по дате не означает, что цена изменилась из-за неё.</p><table><tr><th>Дата</th><th class="r">Цена</th><th>Новости</th></tr>{news}</table></div></details>' if news else ''}
{f'<details><summary>Допущения и качество данных</summary><div><ul>{assumptions}{warns}</ul></div></details>' if assumptions or warns else ''}
</div>
</div>
<footer class="foot"><div class="wrap">{DISCLAIMER} Результаты на истории не гарантируют результатов в будущем.
Сформировано {res.created_at:%d.%m.%Y %H:%M} UTC.</div></footer></body></html>"""
