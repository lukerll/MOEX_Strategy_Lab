"""SVG-графики для автономного HTML-отчёта: открывается без интернета и без JS.

Оба графика отчёта (капитал и просадка) строятся на одной геометрии, поэтому даты
под ними стоят строго друг под другом. Подсказки при наведении дают теги <title>.
"""
from __future__ import annotations

from html import escape

MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
PAD_L, PAD_R, PAD_T, PAD_B = 76, 18, 14, 28
INK, GRID, MUTED = "#0a1a14", "rgba(0,10,6,0.08)", "#6b7570"


def _scale(vals: list[float], lo: float, hi: float, a: float, b: float) -> list[float]:
    span = (hi - lo) or 1.0
    return [b - (v - lo) / span * (b - a) for v in vals]


def _num(v: float, unit: str) -> str:
    if unit == "%":
        return f"{v:.0f}%".replace("-", "−")
    return f"{v:,.0f}".replace(",", " ")


def _month(d: str) -> str:
    return f"{MONTHS[int(d[5:7]) - 1]} {d[:4]}"


def _dmy(d: str) -> str:
    return f"{d[8:10]}.{d[5:7]}.{d[:4]}"


def _nice(span: float) -> float:
    """Круглый шаг сетки: 1, 2, 2,5 или 5, умноженные на степень десяти."""
    import math
    raw = span / 4 or 1.0
    k = 10 ** math.floor(math.log10(raw))
    return next(k * m for m in (1, 2, 2.5, 5, 10) if k * m >= raw)


def line_chart(dates: list[str], series: list[dict], *, height: int = 300, width: int = 1100,
               fill_zero: bool = False, unit: str = "", marks: list[dict] | None = None,
               x_labels: bool = True) -> str:
    """series: [{"name", "values", "color", "width", "dash", "fill"}];
    marks: [{"i": индекс даты, "series": номер ряда, "label": подпись, "color"}]."""
    all_vals = [v for s in series for v in s["values"] if v is not None]
    lo, hi = min(all_vals), max(all_vals)
    if fill_zero:
        hi = max(hi, 0.0)
    pad = (hi - lo) * 0.06 or 1.0
    lo, hi = (lo - pad, hi) if fill_zero else (lo - pad, hi + pad)
    n = len(dates)
    xs = [PAD_L + i * (width - PAD_L - PAD_R) / max(n - 1, 1) for i in range(n)]
    y_of = lambda v: _scale([v], lo, hi, PAD_T, height - PAD_B)[0]   # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
             f'font-family="Inter, Arial, sans-serif" font-size="12" fill="{MUTED}">']
    step_y = _nice(hi - lo)
    v0 = -(-lo // step_y) * step_y
    ticks = [v0 + i * step_y for i in range(int((hi - v0) // step_y) + 1)]
    for v in ticks:
        y = y_of(v)
        parts.append(f'<line x1="{PAD_L}" x2="{width - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>')
        parts.append(f'<text x="{PAD_L - 10}" y="{y + 4:.1f}" text-anchor="end">{_num(v, unit)}</text>')
    # подписи дат: начало каждого шестого отрезка, по-русски
    step = max(n // 6, 1)
    for i in range(0, n, step) if x_labels else []:
        if xs[i] < width - PAD_R - 30:
            parts.append(f'<text x="{xs[i]:.1f}" y="{height - 8}" text-anchor="middle">{_month(dates[i])}</text>')
    for s in series:
        ys = _scale(s["values"], lo, hi, PAD_T, height - PAD_B)
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        if fill_zero:
            y0 = y_of(0.0)
            parts.append(f'<polygon points="{xs[0]:.1f},{y0:.1f} {pts} {xs[-1]:.1f},{y0:.1f}" '
                         f'fill="{s.get("fill", "rgba(200,56,44,0.12)")}" stroke="none"/>')
        dash = ' stroke-dasharray="5 5"' if s.get("dash") else ""
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{s.get("color", INK)}" stroke-linejoin="round" '
                     f'stroke-width="{s.get("width", 1.6)}"{dash}><title>{escape(s["name"])}</title></polyline>')
    for mk in marks or []:
        s = series[mk.get("series", 0)]
        x, y = xs[mk["i"]], y_of(s["values"][mk["i"]])
        color = mk.get("color", INK)
        anchor = "end" if x > width * 0.8 else "start" if x < width * 0.2 else "middle"
        ty = y - 12 if mk.get("above", True) else y + 20
        parts.append(f'<g><title>{escape(mk["label"])}, {_dmy(dates[mk["i"]])}</title>'
                     f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="#fff" stroke="{color}" stroke-width="2"/>'
                     f'<text x="{x:.1f}" y="{ty:.1f}" text-anchor="{anchor}" fill="{color}" font-size="11.5">'
                     f'{escape(mk["label"])} {_dmy(dates[mk["i"]])}</text></g>')
    parts.append("</svg>")
    return "".join(parts)
