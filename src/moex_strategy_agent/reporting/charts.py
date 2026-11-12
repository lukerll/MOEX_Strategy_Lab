"""SVG-графики для автономного HTML-отчёта: открывается без интернета и без JS."""
from __future__ import annotations

from html import escape


def _scale(vals: list[float], lo: float, hi: float, a: float, b: float) -> list[float]:
    span = (hi - lo) or 1.0
    return [b - (v - lo) / span * (b - a) for v in vals]


def line_chart(dates: list[str], series: list[dict], *, height: int = 280, width: int = 1100,
               fill_zero: bool = False, unit: str = "") -> str:
    """series: [{"name", "values", "dash": bool, "color"}]. Цвета только из палитры отчёта."""
    pad_l, pad_r, pad_t, pad_b = 64, 16, 16, 28
    all_vals = [v for s in series for v in s["values"] if v is not None]
    lo, hi = min(all_vals), max(all_vals)
    if fill_zero:
        hi = max(hi, 0.0)
    n = len(dates)
    xs = [pad_l + i * (width - pad_l - pad_r) / max(n - 1, 1) for i in range(n)]
    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
             f'font-family="IBM Plex Mono, monospace" font-size="11">']
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = _scale([v], lo, hi, pad_t, height - pad_b)[0]
        parts.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" stroke="#000" '
                     f'stroke-opacity="0.12" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end">{v:,.0f}{unit}</text>'
                     .replace(",", " "))
    step = max(n // 6, 1)
    for i in range(0, n, step):
        parts.append(f'<text x="{xs[i]:.1f}" y="{height - 8}" text-anchor="middle">{dates[i][:7]}</text>')
    for s in series:
        ys = _scale(s["values"], lo, hi, pad_t, height - pad_b)
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        if fill_zero:
            y0 = _scale([0.0], lo, hi, pad_t, height - pad_b)[0]
            parts.append(f'<polygon points="{xs[0]:.1f},{y0:.1f} {pts} {xs[-1]:.1f},{y0:.1f}" '
                         f'fill="{s.get("fill", "#a1fea0")}" stroke="none"/>')
        dash = ' stroke-dasharray="6 5"' if s.get("dash") else ""
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{s.get("color", "#000")}" '
                     f'stroke-width="{s.get("width", 1.6)}"{dash}><title>{escape(s["name"])}</title></polyline>')
    parts.append("</svg>")
    return "".join(parts)
