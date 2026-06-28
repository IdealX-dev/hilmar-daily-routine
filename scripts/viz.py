"""
viz.py — Shared visualization helpers for email + dashboard + audit + summary.

Per Michael 2026-05-14: "all of it" (visual improvements).

All helpers produce HTML strings safe for email AND browser rendering:
  - Inline SVG for sparklines (works in modern Outlook + browsers; older
    Outlook desktop with Word renderer strips SVG — graceful fallback to
    text values in each helper)
  - Inline CSS only (no <style> blocks — many email clients strip them)
  - No JavaScript
  - No external image URLs (Outlook blocks by default)
  - Pure CSS color heatmaps + horizontal bars via width-% + background

Used by:
  gen_email.py          — daily email body
  gen_dashboard.py      — dashboard HTML
  gen_rate_intelligence — rate cheat sheet
  gen_weekly_summary    — Friday exec PDF
  gen_improvements_report — audit
"""
from __future__ import annotations

from collections.abc import Sequence


def _esc(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ─────────────────────────────────────────────────────────────────────
# Sparklines
# ─────────────────────────────────────────────────────────────────────

def sparkline_svg(values: Sequence[float], width: int = 80, height: int = 20,
                  color: str = "#3b82f6", show_dots: bool = True,
                  fill: str = "rgba(59,130,246,0.15)") -> str:
    """Inline SVG sparkline. Returns "" if values empty."""
    vals = [v for v in (values or []) if v is not None]
    if len(vals) < 2:
        return ""
    vmin, vmax = min(vals), max(vals)
    if vmin == vmax:
        vmin -= 1
        vmax += 1
    pad = 2
    w = width - 2 * pad
    h = height - 2 * pad

    def _xy(i, v):
        x = pad + (i / (len(vals) - 1)) * w
        y = pad + (1 - (v - vmin) / (vmax - vmin)) * h
        return x, y

    pts = [_xy(i, v) for i, v in enumerate(vals)]
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    # Filled area under the line
    area = f"{polyline} {pts[-1][0]:.1f},{height - pad} {pts[0][0]:.1f},{height - pad}"
    dots = ""
    if show_dots:
        for i, (x, y) in enumerate(pts):
            # Highlight the last point (current value)
            r = 2 if i == len(pts) - 1 else 1.3
            f = color if i == len(pts) - 1 else color
            dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{f}"/>'
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'style="vertical-align:middle" xmlns="http://www.w3.org/2000/svg">'
        f'<polygon points="{area}" fill="{fill}" stroke="none"/>'
        f'<polyline points="{polyline}" fill="none" stroke="{color}" '
        f'stroke-width="1.5" stroke-linejoin="round"/>'
        f'{dots}'
        f'</svg>'
    )


def trend_arrow(curr: float, prev: float, fmt: str = "d",
                good_direction: str = "up") -> str:
    """Returns a colored ▲/▼/▬ span. good_direction='up' → up is green
    (e.g. win rate); 'down' → down is green (e.g. rate cost).
    """
    if not isinstance(curr, (int, float)) or not isinstance(prev, (int, float)):
        return ""
    delta = curr - prev
    if abs(delta) < 0.005:
        return '<span style="color:#94a3b8;font-size:11px">▬ 0</span>'
    is_up = delta > 0
    is_good = (is_up and good_direction == "up") or ((not is_up) and good_direction == "down")
    color = "#16a34a" if is_good else "#dc2626"
    arrow = "▲" if is_up else "▼"
    label = f"{int(delta):+d}" if fmt == "d" else f"{delta:+.1f}"
    return f'<span style="color:{color};font-size:11px;margin-left:4px">{arrow} {label}</span>'


# ─────────────────────────────────────────────────────────────────────
# Heatmap cell colors
# ─────────────────────────────────────────────────────────────────────

def heatmap_color(value: float, vmin: float = 0, vmax: float = 100,
                  mode: str = "good_high") -> str:
    """Return a hex CSS background color for a heatmap cell.

    mode='good_high' → low=red, high=green   (e.g. win rate %)
    mode='good_low'  → low=green, high=red   (e.g. rate cost, days)
    mode='neutral'   → low=blue, high=red    (just intensity)
    """
    if value is None or not isinstance(value, (int, float)):
        return "transparent"
    t = (value - vmin) / max(vmax - vmin, 1e-9)
    t = max(0.0, min(1.0, t))
    if mode == "good_low":
        t = 1.0 - t
    # Light pastel gradient red→yellow→green so text stays readable
    if mode == "neutral":
        # Blue → red gradient for intensity
        r = int(220 * t + 219 * (1 - t))
        g = int(220 * (1 - t) + 234 * (1 - t))
        b = int(255 * (1 - t) + 252 * t)
        return f"rgba({r},{g},{b},0.5)"
    # good_high (default) and good_low (after inversion above)
    if t < 0.5:
        # red → yellow
        r = 254
        g = int(202 + (243 - 202) * (t * 2))
        b = int(202 + (199 - 202) * (t * 2))
    else:
        # yellow → green
        r = int(243 + (187 - 243) * ((t - 0.5) * 2))
        g = int(243 + (247 - 243) * ((t - 0.5) * 2))
        b = int(199 + (208 - 199) * ((t - 0.5) * 2))
    return f"rgb({r},{g},{b})"


# ─────────────────────────────────────────────────────────────────────
# Horizontal bar (in table cell)
# ─────────────────────────────────────────────────────────────────────

def bar_cell(value: float, max_value: float, color: str = "#3b82f6",
             label: str | None = None, width_px: int = 90) -> str:
    """Returns HTML for a horizontal bar with optional value label.
    Use inside a <td> to make a single-cell bar chart row."""
    if value is None or max_value is None or max_value <= 0:
        return _esc(label or "—")
    pct = max(0, min(100, value / max_value * 100))
    label_text = label if label is not None else f"{value:.0f}"
    return (
        f'<div style="display:inline-block;vertical-align:middle">'
        f'<div style="display:inline-block;background:#e5e7eb;width:{width_px}px;height:14px;'
        f'border-radius:3px;overflow:hidden;vertical-align:middle">'
        f'<div style="background:{color};width:{pct:.1f}%;height:100%"></div>'
        f'</div>'
        f'<span style="margin-left:6px;font-size:11px;color:#475569;vertical-align:middle">'
        f'{_esc(label_text)}</span>'
        f'</div>'
    )


# ─────────────────────────────────────────────────────────────────────
# Status pill badge
# ─────────────────────────────────────────────────────────────────────

_PILL_PALETTE = {
    "WIN":     ("#dcfce7", "#166534", "#16a34a"),  # bg, text, border
    "LOSS":    ("#fee2e2", "#991b1b", "#dc2626"),
    "PENDING": ("#fef3c7", "#92400e", "#f59e0b"),
    # Pending is two materially different waits — give each a DISTINCT marker so
    # the reader sees at a glance who to chase, not just one amber "PENDING".
    # Amber = waiting on OL to quote (chase OL); violet = OL quoted, waiting on
    # Lonny to decide (chase Lonny). Colors match the detail-section headers.
    "PENDING_OL":     ("#fef3c7", "#92400e", "#f59e0b"),  # amber — chase OL
    "PENDING_HILMAR": ("#ede9fe", "#5b21b6", "#7c3aed"),  # violet — chase Hilmar
    "QUOTED":  ("#dbeafe", "#1e40af", "#3b82f6"),
    "OK":      ("#dcfce7", "#166534", "#16a34a"),
    "WARN":    ("#fef3c7", "#92400e", "#f59e0b"),
    "ERROR":   ("#fee2e2", "#991b1b", "#dc2626"),
    "CLEAN":   ("#dcfce7", "#166534", "#16a34a"),
}

# Friendly labels so a pill reads as the ACTION, not the enum. Pending splits
# into who-you-chase; everything else shows its own name.
_PILL_LABELS = {
    "PENDING_OL":     "PENDING OL",
    "PENDING_HILMAR": "PENDING HILMAR",
}


def status_pill(status: str, icon: str = "", label: str | None = None) -> str:
    """Colored pill badge for status. Falls back to neutral grey if unknown.
    `label` overrides the displayed text (defaults to a friendly label for the
    pending substates, else the status name)."""
    s = (status or "").upper()
    bg, txt, border = _PILL_PALETTE.get(s, ("#f1f5f9", "#475569", "#cbd5e1"))
    text = label if label is not None else _PILL_LABELS.get(s, s)
    icon_part = f"{icon} " if icon else ""
    return (
        f'<span style="display:inline-block;background:{bg};color:{txt};'
        f'border:1px solid {border};border-radius:10px;padding:2px 8px;'
        f'font-size:10px;font-weight:600;letter-spacing:0.3px">{icon_part}{_esc(text)}</span>'
    )


def pending_pill(substate: str | None, icon: str = "") -> str:
    """Substate-aware PENDING marker: amber 'PENDING OL' (chase OL for a quote)
    vs violet 'PENDING HILMAR' (OL quoted, chase Hilmar to decide). Falls back
    to a plain amber 'PENDING' when the substate is unknown."""
    s = (substate or "").upper()
    if s in ("PENDING_OL", "PENDING_HILMAR"):
        return status_pill(s, icon=icon)
    return status_pill("PENDING", icon=icon)


def pending_label(substate: str | None) -> str:
    """Plain-text 'who to chase' label for a PENDING substate — the single
    source of truth shared by the PDF + dashboard tables so every surface reads
    the same wording as the email pills: 'Pending OL' (RFQ sent, waiting on OL
    to quote) vs 'Pending Hilmar' (OL quoted, waiting on Hilmar to decide).
    Falls back to plain 'Pending' when the substate is unknown."""
    s = (substate or "").upper()
    if s == "PENDING_OL":
        return "Pending OL"
    if s == "PENDING_HILMAR":
        return "Pending Hilmar"
    return "Pending"


# ─────────────────────────────────────────────────────────────────────
# Section header with gradient + icon
# ─────────────────────────────────────────────────────────────────────

def section_header(title: str, subtitle: str = "", icon: str = "📊",
                   gradient: str = "linear-gradient(135deg,#1e3a5f 0%,#3b82f6 100%)") -> str:
    """Polished section header — gradient bar, icon, optional subtitle."""
    sub_html = (
        f'<div style="font-size:12px;color:#cbd5e1;margin-top:4px">{_esc(subtitle)}</div>'
        if subtitle else ""
    )
    return (
        f'<div style="background:{gradient};color:white;padding:14px 20px;'
        f'border-radius:6px 6px 0 0;margin-bottom:0">'
        f'<div style="font-size:16px;font-weight:600">{icon} {_esc(title)}</div>'
        f'{sub_html}'
        f'</div>'
    )
