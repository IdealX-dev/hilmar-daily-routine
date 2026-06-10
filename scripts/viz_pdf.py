"""
viz_pdf.py — Reportlab visual helpers (PDF side).

Mirror of scripts/viz.py for the reportlab/PDF generators:
  - heatmap_color()      → reportlab.lib.colors.Color (for TableStyle BACKGROUND)
  - bar_drawing()        → Drawing with a horizontal bar
  - sparkline_drawing()  → Drawing with a polyline sparkline
  - heatmap_style_cmds() → list of ("BACKGROUND", (col,row), (col,row), color) tuples

Kept separate from viz.py so the HTML side doesn't have to import reportlab.

Per Michael 2026-05-14: "all of them as swell" — visual upgrade applies to
client PDF + carrier scorecards too.
"""
from __future__ import annotations

from reportlab.graphics.shapes import Drawing, PolyLine, Rect
from reportlab.lib import colors


def heatmap_color(value: float, vmin: float = 0, vmax: float = 100,
                  mode: str = "good_high") -> colors.Color:
    """Returns a reportlab Color for a heatmap cell. mode='good_high' →
    low=red, high=green. mode='good_low' → low=green, high=red."""
    if value is None:
        return colors.transparent
    t = (value - vmin) / max(vmax - vmin, 1e-9)
    t = max(0.0, min(1.0, t))
    if mode == "good_low":
        t = 1.0 - t
    if t < 0.5:
        # red → yellow
        r = 254 / 255
        g = (202 + (243 - 202) * (t * 2)) / 255
        b = (202 + (199 - 202) * (t * 2)) / 255
    else:
        # yellow → green
        r = (243 + (187 - 243) * ((t - 0.5) * 2)) / 255
        g = (243 + (247 - 243) * ((t - 0.5) * 2)) / 255
        b = (199 + (208 - 199) * ((t - 0.5) * 2)) / 255
    return colors.Color(r, g, b)


def bar_drawing(value: float, max_value: float, width: float = 80, height: float = 12,
                color: str = "#3b82f6") -> Drawing:
    """Returns a Drawing of a horizontal bar. Use as cell content in a Table.
    `color` is a hex string."""
    d = Drawing(width, height)
    # Background track
    d.add(Rect(0, 0, width, height, fillColor=colors.HexColor("#e5e7eb"),
               strokeColor=None))
    if max_value and max_value > 0 and value:
        w = max(0.5, min(width, (value / max_value) * width))
        d.add(Rect(0, 0, w, height, fillColor=colors.HexColor(color),
                   strokeColor=None))
    return d


def sparkline_drawing(values: list[float], width: float = 80, height: float = 18,
                      color: str = "#3b82f6") -> Drawing | None:
    """Returns a Drawing with a polyline sparkline. None if too few points."""
    vals = [v for v in (values or []) if v is not None]
    if len(vals) < 2:
        return None
    vmin, vmax = min(vals), max(vals)
    if vmin == vmax:
        vmin -= 1
        vmax += 1
    d = Drawing(width, height)
    pad = 2
    w = width - 2 * pad
    h = height - 2 * pad
    pts = []
    for i, v in enumerate(vals):
        x = pad + (i / (len(vals) - 1)) * w
        # reportlab y axis is bottom-up, so flip
        y = pad + ((v - vmin) / (vmax - vmin)) * h
        pts.extend([x, y])
    d.add(PolyLine(pts, strokeColor=colors.HexColor(color),
                   strokeWidth=1.2, strokeLineJoin=1))
    # Highlight last point
    last_x, last_y = pts[-2], pts[-1]
    d.add(Rect(last_x - 1.5, last_y - 1.5, 3, 3,
               fillColor=colors.HexColor(color), strokeColor=None))
    return d


def heatmap_style_cmds(table_data: list[list], col_idx: int,
                       value_extractor=None, vmin: float = 0,
                       vmax: float = 100, mode: str = "good_high",
                       skip_header: bool = True) -> list:
    """Walk a column in table_data, return a list of TableStyle BACKGROUND
    commands that color each cell by its heatmap value.

    value_extractor(cell_value) → numeric; defaults to float(cell_value).
    skip_header=True skips row 0.
    """
    cmds = []
    for r_idx, row in enumerate(table_data):
        if skip_header and r_idx == 0:
            continue
        if col_idx >= len(row):
            continue
        val = row[col_idx]
        try:
            num = float(value_extractor(val)) if value_extractor else float(val)
        except (TypeError, ValueError):
            continue
        c = heatmap_color(num, vmin, vmax, mode)
        cmds.append(("BACKGROUND", (col_idx, r_idx), (col_idx, r_idx), c))
    return cmds


def bar_style_cmds(table_data: list[list], col_idx: int,
                   value_extractor=None, max_value: float | None = None,
                   color: str = "#3b82f6", skip_header: bool = True) -> tuple:
    """Replace cell values in a column with bar Drawings + return TableStyle
    commands to align them. Mutates table_data in place.

    Returns the style commands needed (CENTER alignment, padding tweaks)
    for the column.
    """
    # First pass — determine max_value if not given
    if max_value is None:
        vals = []
        for r_idx, row in enumerate(table_data):
            if skip_header and r_idx == 0:
                continue
            if col_idx >= len(row):
                continue
            try:
                vals.append(float(value_extractor(row[col_idx]) if value_extractor else row[col_idx]))
            except (TypeError, ValueError):
                continue
        max_value = max(vals, default=1) or 1

    # Second pass — replace cells with Drawings
    for r_idx, row in enumerate(table_data):
        if skip_header and r_idx == 0:
            continue
        if col_idx >= len(row):
            continue
        original = row[col_idx]
        try:
            num = float(value_extractor(original) if value_extractor else original)
        except (TypeError, ValueError):
            continue
        row[col_idx] = bar_drawing(num, max_value, color=color)
    return [
        ("ALIGN", (col_idx, 0), (col_idx, -1), "CENTER"),
        ("VALIGN", (col_idx, 0), (col_idx, -1), "MIDDLE"),
    ]
