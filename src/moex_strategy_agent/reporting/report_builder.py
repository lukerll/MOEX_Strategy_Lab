"""Автономный HTML-отчёт по запуску в стиле интерфейса (чёрный, белый и один зелёный)."""
from __future__ import annotations

from html import escape

from ..agent.guardrails import DISCLAIMER
from ..domain.schemas import STRATEGY_TITLES, BacktestResult
from .charts import line_chart

CSS = """
*{box-sizing:border-box}body{margin:0;background:#fff;color:#000;font-family:Inter,Arial,sans-serif;font-size:16px;line-height:1.6}
.wrap{max-width:1200px;margin:0 auto;padding:0 32px}
.band{background:#000a06;color:#fff;padding:64px 0 56px}
.eyebrow{font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.2em;text-transform:uppercase}
h1{font-family:Inter,Arial,sans-serif;font-weight:300;font-size:60px;line-height:1.05;
letter-spacing:-.05em;margin:16px 0 0}
h2{font-family:Inter,Arial,sans-serif;font-weight:300;font-size:44px;letter-spacing:-.03em;
line-height:1;margin:80px 0 24px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:40px}
.kpi{border:1px solid #000;border-radius:25px;padding:24px 28px}
.band .kpi{border-color:#fff}
h1,h2,.kpi .v{font-variant-numeric:lining-nums;font-feature-settings:'lnum' 1}
.kpi .v{font-family:Inter,Arial,sans-serif;font-weight:300;font-size:56px;line-height:1;
letter-spacing:-.04em}
.kpi.hl{background:#7affb4;color:#000;border-color:#7affb4}
.card{border:1px solid #000;border-radius:25px;padding:32px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px 10px;
border-bottom:1px solid #000}th{font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.1em;
text-transform:uppercase;font-weight:400}
.badge{display:inline-block;border:1px solid #000;border-radius:25px;padding:2px 14px;font-size:13px;
letter-spacing:.1em;text-transform:uppercase}
.badge.ok{background:#7affb4}
.legend span{margin-right:24px}.foot{background:#000;color:#fff;padding:56px 0;margin-top:80px;font-size:14px}
.muted{opacity:.7}
@media print{.band,.foot{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""


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


def _fmt(v: float | None, suffix: str = "%", sign: bool = True) -> str:
    if v is None:
        return "н/д"
    s = f"{v:+.2f}" if sign else f"{v:.2f}"
    return s.replace(".", ",") + suffix


def _money(v: float) -> str:
    return f"{v:,.0f} ₽".replace(",", " ")


def build_html(res: BacktestResult) -> str:
    u = res.understood or {}
    understood_html = (f'<h2>Как я понял стратегию</h2><div class="card"><p>{escape(u.get("entry", ""))}</p>'
                       f'<p>{escape(u.get("exit", ""))}</p><p class="muted">{escape(u.get("execution", ""))}</p></div>'
                       if u.get("entry") else "")
    m, b, s = res.metrics, res.benchmark, res.spec
    dates = [r["date"] for r in res.equity]
    eq_svg = line_chart(dates, [
        {"name": "Стратегия", "values": [r["equity"] for r in res.equity], "color": "#000", "width": 2},
        {"name": "Купить и держать", "values": [r["benchmark"] for r in res.equity], "color": "#000", "dash": True}])
    dd_svg = line_chart(dates, [{"name": "Просадка стратегии", "values": [r["drawdown"] for r in res.equity],
                                 "color": "#000", "width": 1.2}], height=200, fill_zero=True, unit="%")
    v = res.verification
    rows_checks = "".join(
        f"<tr><td>{escape(c.name)}</td><td>{escape(str(c.main))}</td><td>{escape(str(c.reference))}</td>"
        f"<td>{'совпало' if c.ok else 'расхождение'}</td><td class='muted'>{escape(c.note)}</td></tr>"
        for c in v.checks)
    rows_trades = "".join(
        f"<tr><td>{t.entry_date}</td><td>{t.exit_date or 'открыта'}</td><td>{t.size}</td>"
        f"<td>{t.entry_price:.2f}</td><td>{'' if t.exit_price is None else f'{t.exit_price:.2f}'}</td>"
        f"<td>{_money(t.pnl)}</td><td>{_fmt(t.return_pct)}</td><td>{t.holding_days}</td>"
        f"<td>{'' if t.is_open else _REASONS.get(t.exit_reason or '', '')}</td></tr>"
        for t in res.trades) or "<tr><td colspan='9'>Сделок не было.</td></tr>"
    findings = "".join(f"<li>{escape(x)}</li>" for x in res.diagnostics.get("findings", [])) or \
        "<li>Явных слабых мест не найдено.</li>"
    yearly = "".join(f"<tr><td>{y['year']}</td><td>{_fmt(y['strategy_pct'])}</td><td>{_fmt(y['buy_hold_pct'])}</td></tr>"
                     for y in res.diagnostics.get("yearly", []))
    regimes = "".join(f"<tr><td>{r['regime']}</td><td>{r['days']}</td><td>{_fmt(r['strategy_pct'])}</td></tr>"
                      for r in res.diagnostics.get("regimes", []))
    events = ""
    for e in res.events:
        items = "".join(
            f"<li>{'[скрыто: подозрение на prompt injection]' if n.flagged_injection else escape(n.title)} "
            f"<span class='muted'>({escape(n.source)}, {n.published_at:%Y-%m-%d})</span>"
            + (f" <a href='{escape(n.url)}'>ссылка</a>" if n.url and not n.flagged_injection else "") + "</li>"
            for n in e.news)
        events += (f"<tr><td>{e.date}</td><td>{_fmt(e.change_pct)}</td><td><ul style='margin:0;padding-left:18px'>"
                   f"{items or '<li>' + escape(e.note) + '</li>'}</ul></td></tr>")
    narrative_html = "".join(f"<p>{escape(p)}</p>" for p in (res.narrative or "").split("\n\n") if p.strip())
    synthetic = ("<p class='badge'>Демо: синтетические котировки, не данные биржи</p>"
                 if res.data.is_synthetic else "")
    assumptions = "".join(f"<li>{escape(a)}</li>" for a in res.assumptions) or "<li>нет</li>"
    warnings = "".join(f"<li>{escape(w)}</li>" for w in res.data.warnings) or "<li>замечаний нет</li>"

    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(s.title())}: отчёт</title>
<style>{font_css()}{CSS}</style></head><body>
<section class="band"><div class="wrap">
<div class="eyebrow">MOEX Strategy Agent / отчёт {res.run_id}</div>
<h1>{escape(s.title())}</h1>
<p class="eyebrow" style="margin-top:24px">{STRATEGY_TITLES[s.strategy_type]} / с {s.start_date} по {s.end_date} /
капитал {_money(s.initial_cash)}</p>
<div class="kpis">
<div class="kpi hl"><div class="eyebrow">Доходность</div><div class="v">{_fmt(m.total_return_pct)}</div></div>
<div class="kpi"><div class="eyebrow">Макс. просадка</div><div class="v">{_fmt(m.max_drawdown_pct, sign=False)}</div></div>
<div class="kpi"><div class="eyebrow">Сделки</div><div class="v">{m.trades_count}</div></div>
<div class="kpi"><div class="eyebrow">Купить и держать</div><div class="v">{_fmt(b.total_return_pct)}</div></div>
</div></div></section>
<div class="wrap">
{synthetic}
{understood_html}<h2>Что получилось</h2><div class="card">{narrative_html}</div>
<h2>Капитал против «купить и держать»</h2>
<div class="legend eyebrow"><span>━ стратегия</span><span>┅ купить и держать</span></div>{eq_svg}
<h2>Просадка</h2>{dd_svg}
<h2>Метрики</h2><div class="card"><table>
<tr><th>Показатель</th><th>Стратегия</th><th>Купить и держать</th></tr>
<tr><td>Итоговый капитал</td><td>{_money(m.final_equity)}</td><td>{_money(b.final_equity)}</td></tr>
<tr><td>Доходность</td><td>{_fmt(m.total_return_pct)}</td><td>{_fmt(b.total_return_pct)}</td></tr>
<tr><td>CAGR</td><td>{_fmt(m.cagr_pct)}</td><td>{_fmt(b.cagr_pct)}</td></tr>
<tr><td>Макс. просадка</td><td>{_fmt(m.max_drawdown_pct, sign=False)}</td><td>{_fmt(b.max_drawdown_pct, sign=False)}</td></tr>
<tr><td>Волатильность, годовая</td><td>{_fmt(m.volatility_pct, sign=False)}</td><td>{_fmt(b.volatility_pct, sign=False)}</td></tr>
<tr><td>Шарп (ставка 0)</td><td>{_fmt(m.sharpe, '', sign=False)}</td><td>{_fmt(b.sharpe, '', sign=False)}</td></tr>
<tr><td>Сделок / прибыльных</td><td>{m.trades_count} / {_fmt(m.win_rate_pct, sign=False)}</td><td>н/п</td></tr>
<tr><td>Profit factor</td><td>{_fmt(m.profit_factor, '', sign=False)}</td><td>н/п</td></tr>
<tr><td>Время в позиции</td><td>{_fmt(m.exposure_pct, sign=False)}</td><td>{_fmt(b.exposure_pct, sign=False)}</td></tr>
<tr><td>Комиссии</td><td>{_money(m.commissions_paid)}</td><td>{_money(b.commissions_paid)}</td></tr>
</table></div>
<h2>Где стратегия теряла</h2><div class="card"><ul>{findings}</ul>
<table><tr><th>Год</th><th>Стратегия</th><th>Купить и держать</th></tr>{yearly}</table><br>
<table><tr><th>Режим рынка</th><th>Дней</th><th>Стратегия</th></tr>{regimes}</table></div>
<h2>Сделки</h2><div class="card"><table><tr><th>Вход</th><th>Выход</th><th>Акций</th><th>Цена входа</th>
<th>Цена выхода</th><th>PnL</th><th>%</th><th>Дней</th><th>Причина выхода</th></tr>{rows_trades}</table></div>
<h2>Резкие движения и новости</h2><div class="card"><p class="muted">{escape(res.news_status)}.
Совпадение по времени не доказывает причину.</p><table><tr><th>Дата</th><th>Изменение</th><th>Новости рядом</th></tr>
{events or "<tr><td colspan='3'>Нет событий.</td></tr>"}</table></div>
<h2>Независимая проверка</h2><div class="card"><p><span class="badge {'ok' if v.status == 'OK' else ''}">{v.status}</span>
{escape(v.summary)}</p><table><tr><th>Проверка</th><th>Основной расчёт</th><th>Контрольный</th><th>Итог</th><th></th></tr>
{rows_checks}</table></div>
<h2>Параметры теста</h2><div class="card"><table>
<tr><td>Источник данных</td><td>{res.data.source}{' (синтетика)' if res.data.is_synthetic else ''}, {res.data.rows} торговых дней,
с {res.data.first_date} по {res.data.last_date}</td></tr>
<tr><td>Исполнение</td><td>сигнал по закрытию дня, сделка по открытию следующего; только длинные позиции</td></tr>
<tr><td>Издержки</td><td>комиссия {_fmt(s.commission * 100, sign=False)} с каждой стороны, проскальзывание
{_fmt(s.slippage * 100, sign=False)} к цене покупки</td></tr>
<tr><td>Дивиденды</td><td>не учитываются ни в стратегии, ни в «купить и держать»: это ценовая доходность</td></tr>
</table><p class="eyebrow">Допущения</p><ul>{assumptions}</ul><p class="eyebrow">Качество данных</p><ul>{warnings}</ul></div>
</div>
<footer class="foot"><div class="wrap"><div class="eyebrow">{DISCLAIMER}</div>
<p class="muted">Результаты на истории не гарантируют результатов в будущем. Сформировано {res.created_at:%Y-%m-%d %H:%M} UTC.</p>
</div></footer></body></html>"""
