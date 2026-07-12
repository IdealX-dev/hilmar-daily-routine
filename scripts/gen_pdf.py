"""
gen_pdf.py — Hilmar / OL-USA client-facing PDF report.

6 pages, built for:
  • Client reviews with Hilmar (Lonny Upfold)
  • Internal steering with OL-USA leadership
  • Carrier/steamship line negotiations (supported by per-carrier scorecards)

Page flow:
  1. Cover + executive summary
  2. What changed today (DOD)
  3. Turnaround performance (biz-hours)
  4. Carrier scoreboard (sorted by win rate)
  5. Lane performance (sorted by TEU requested)
  6. Pending watchlist + rate trends + QC footnote

Usage:
  python3 scripts/gen_pdf.py
  python3 scripts/gen_pdf.py --config config.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import branding as B  # noqa: E402  Hilmar logo
import viz as V  # noqa: E402  shared pending-substate label ("Pending OL"/"Pending Hilmar")
import viz_pdf as VP  # noqa: E402  shared reportlab visual helpers

# Register Inter — falls back to Helvetica if assets missing
_FONTS_DIR = ROOT / "assets" / "fonts"
BODY_FONT = "Helvetica"
BODY_FONT_BOLD = "Helvetica-Bold"
BODY_FONT_MED = "Helvetica"
try:
    if (_FONTS_DIR / "Inter-Regular.ttf").exists():
        pdfmetrics.registerFont(TTFont("Inter", str(_FONTS_DIR / "Inter-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("Inter-Bold", str(_FONTS_DIR / "Inter-Bold.ttf")))
        pdfmetrics.registerFont(TTFont("Inter-Medium", str(_FONTS_DIR / "Inter-Medium.ttf")))
        pdfmetrics.registerFontFamily(
            "Inter", normal="Inter", bold="Inter-Bold",
            italic="Inter", boldItalic="Inter-Bold",
        )
        BODY_FONT = "Inter"
        BODY_FONT_BOLD = "Inter-Bold"
        BODY_FONT_MED = "Inter-Medium"
except Exception as _e:
    print(f"[gen_pdf] Inter registration failed ({_e}); falling back to Helvetica", file=sys.stderr)

# ── Brand palette ─────────────────────────────────────────────────────────
NAVY = colors.HexColor("#0f172a")
SLATE = colors.HexColor("#475569")
LIGHT = colors.HexColor("#f8fafc")
BORDER = colors.HexColor("#e2e8f0")
GREEN = colors.HexColor("#059669")
RED = colors.HexColor("#dc2626")
AMBER = colors.HexColor("#d97706")
BLUE = colors.HexColor("#2563eb")
PURPLE = colors.HexColor("#7c3aed")

# ── Styles ────────────────────────────────────────────────────────────────
def make_styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("H1", parent=ss["Heading1"], fontName=BODY_FONT_BOLD, fontSize=22, textColor=NAVY, spaceAfter=6, leading=26))
    ss.add(ParagraphStyle("H2", parent=ss["Heading2"], fontName=BODY_FONT_BOLD, fontSize=14, textColor=NAVY, spaceBefore=10, spaceAfter=6, leading=18))
    ss.add(ParagraphStyle("H3", parent=ss["Heading3"], fontName=BODY_FONT_MED, fontSize=11, textColor=SLATE, spaceBefore=6, spaceAfter=4))
    ss.add(ParagraphStyle("Body", parent=ss["BodyText"], fontName=BODY_FONT, fontSize=9.5, textColor=NAVY, leading=13))
    ss.add(ParagraphStyle("BodyMuted", parent=ss["BodyText"], fontName=BODY_FONT, fontSize=8.5, textColor=SLATE, leading=12))
    ss.add(ParagraphStyle("Tiny", parent=ss["BodyText"], fontName=BODY_FONT, fontSize=7.5, textColor=SLATE, leading=10))
    ss.add(ParagraphStyle("Callout", parent=ss["BodyText"], fontName=BODY_FONT, fontSize=9, textColor=NAVY, leading=13,
                          leftIndent=8, rightIndent=8, spaceBefore=4, spaceAfter=4))
    ss.add(ParagraphStyle("KPINum", parent=ss["BodyText"], fontName=BODY_FONT_BOLD, fontSize=20, textColor=NAVY, alignment=TA_CENTER, leading=22))
    ss.add(ParagraphStyle("KPILabel", parent=ss["BodyText"], fontName=BODY_FONT_MED, fontSize=7.5, textColor=SLATE, alignment=TA_CENTER, leading=10))
    return ss

# ── Helpers ───────────────────────────────────────────────────────────────
def _pct(n):
    return f"{n:.1f}%" if isinstance(n, (int, float)) else "—"

def _dash(v):
    if v is None or v == "":
        return "—"
    return str(v)

def _fmt_int(v):
    try:
        return f"{int(v):,}"
    except Exception:
        return "—"

def _header_footer(canvas, doc, client, provider, generated):
    canvas.saveState()
    w, h = LETTER
    # Footer
    canvas.setFont(BODY_FONT, 7.5)
    canvas.setFillColor(SLATE)
    canvas.drawString(0.5 * inch, 0.4 * inch, f"{client} × {provider}  •  Confidential  •  Generated {generated}")
    canvas.drawRightString(w - 0.5 * inch, 0.4 * inch, f"Page {doc.page}")
    # Header rule
    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(0.5 * inch, h - 0.55 * inch, w - 0.5 * inch, h - 0.55 * inch)
    canvas.restoreState()

def kpi_cell(label, value, subline="", color=NAVY):
    """Render a KPI as a compact inner table."""
    styles = make_styles()
    big = ParagraphStyle("BigK", parent=styles["KPINum"], textColor=color)
    t = Table(
        [[Paragraph(f"<b>{value}</b>", big)],
         [Paragraph(label, styles["KPILabel"])],
         [Paragraph(subline, styles["Tiny"])] if subline else [Paragraph("", styles["Tiny"])]],
        colWidths=[1.65 * inch],
        rowHeights=[0.42 * inch, 0.22 * inch, 0.18 * inch],
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t

# ── Page builders ─────────────────────────────────────────────────────────
def build_cover(story, styles, data, cfg):
    s = data.get("summary", {}) or {}
    client = data.get("client") or cfg["client"]["name"]
    provider = data.get("provider") or cfg["provider"]["name"]
    last_updated = data.get("last_updated", "—")
    date_range = data.get("date_range", cfg.get("data_range", {}).get("start", "—") + " → today")

    # Logo cover banner (top of page 1) — no-op if logo file missing
    logo_img = B.logo_reportlab_image(width=170)
    if logo_img:
        story.append(logo_img)
        story.append(Spacer(1, 12))
    story.append(Paragraph(f"{client} × {provider}", styles["H1"]))
    story.append(Paragraph("Rate Desk Performance Report", styles["H2"]))
    story.append(Paragraph(f"Period: {date_range}  •  Last updated: {last_updated}", styles["BodyMuted"]))
    story.append(Spacer(1, 14))

    # KPI grid — 2 rows × 4 cols
    total = s.get("total_entries", 0)
    wins = s.get("wins", 0)
    lost = s.get("quoted_lost", 0)
    nq = s.get("not_quoted", 0)
    pend = s.get("pending_hilmar", 0)
    win_rate = s.get("win_rate", 0)
    quote_rate = s.get("quote_rate", 0)
    teu_req = s.get("teu_requested", 0)
    teu_won = s.get("teu_won", 0)
    ta_biz = s.get("turnaround_avg_biz_hours")

    row1 = [
        kpi_cell("Total requests", _fmt_int(total)),
        kpi_cell("Wins", _fmt_int(wins), color=GREEN),
        kpi_cell("Quoted & Lost", _fmt_int(lost), color=RED),
        kpi_cell("Not Quoted", _fmt_int(nq), color=AMBER),
    ]
    row2 = [
        kpi_cell("Pending", _fmt_int(pend), color=PURPLE),
        kpi_cell("Win rate", _pct(win_rate), subline="wins / decided"),
        kpi_cell("Quote rate", _pct(quote_rate), subline="quoted / total"),
        kpi_cell("TEU won", f"{_fmt_int(teu_won)} / {_fmt_int(teu_req)}"),
    ]
    grid = Table([row1, row2], colWidths=[1.75 * inch] * 4, rowHeights=[0.95 * inch, 0.95 * inch])
    grid.setStyle(TableStyle([("LEFTPADDING",(0,0),(-1,-1),3),("RIGHTPADDING",(0,0),(-1,-1),3),
                              ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4)]))
    story.append(grid)
    story.append(Spacer(1, 14))

    # Narrative callout
    narrative = []
    if total == 0:
        narrative.append("No requests ingested yet for this period. Pipeline is in dry-run mode — once Outlook ingestion runs, this section populates automatically.")
    else:
        ta_str = f"{ta_biz:.1f}h biz-hrs avg response" if ta_biz else "turnaround data pending"
        narrative.append(
            f"Across <b>{total}</b> rate requests from Hilmar, OL-USA quoted <b>{wins+lost+pend}</b> "
            f"({_pct(quote_rate)}) and won <b>{wins}</b> ({_pct(win_rate)} win rate among decided). "
            f"TEU captured: <b>{_fmt_int(teu_won)}</b> of <b>{_fmt_int(teu_req)}</b>. "
            f"{ta_str}."
        )
        if pend > 0:
            narrative.append(f"<b>{pend}</b> request(s) currently pending (waiting on OL to quote, or on Hilmar to decide) — see page 6 watchlist for who to chase per row.")
        if nq > 0:
            narrative.append(f"<b>{nq}</b> request(s) went unanswered. Investigate root cause (capacity, lane gap, or missed inbox).")

    for p in narrative:
        story.append(Paragraph(p, styles["Body"]))
        story.append(Spacer(1, 4))

def build_dod(story, styles, data):
    s = data.get("summary", {}) or {}
    dod = s.get("dod", {}) or {}
    story.append(PageBreak())
    story.append(Paragraph("What Changed Today", styles["H1"]))
    story.append(Paragraph("Day-over-day movement vs. yesterday's report.", styles["BodyMuted"]))
    story.append(Spacer(1, 10))

    if not dod:
        story.append(Paragraph("No prior-day snapshot yet — today's data is the new baseline.", styles["Body"]))
        return

    rows = [["Metric", "Yesterday", "Today", "Δ"]]
    metrics = [
        ("New requests", dod.get("new_requests", 0)),
        ("New wins", dod.get("new_wins", 0)),
        ("New losses", dod.get("new_losses", 0)),
        ("New pending", dod.get("new_pending", 0)),
        ("Status flips", dod.get("status_flips", 0)),
    ]
    for label, delta in metrics:
        rows.append([label, "—", "—", f"+{delta}" if delta else "0"])

    t = Table(rows, colWidths=[2.0*inch, 1.3*inch, 1.3*inch, 1.3*inch])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),BODY_FONT_BOLD),("FONTSIZE",(0,0),(-1,-1),9),
        ("ALIGN",(1,0),(-1,-1),"CENTER"),("GRID",(0,0),(-1,-1),0.3,BORDER),
        ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
    ]))
    story.append(t)
    story.append(Spacer(1, 10))

    notes = dod.get("notes", [])
    if notes:
        story.append(Paragraph("Notes:", styles["H3"]))
        for n in notes:
            story.append(Paragraph(f"• {n}", styles["Body"]))

def build_turnaround(story, styles, data):
    s = data.get("summary", {}) or {}
    story.append(PageBreak())
    story.append(Paragraph("Turnaround Performance", styles["H1"]))
    story.append(Paragraph("Business hours from Hilmar's request (PT) to OL-USA's quote (ET).  Window: 8:30 AM – 5:30 PM ET, weekdays.", styles["BodyMuted"]))
    story.append(Spacer(1, 10))

    ta_biz = s.get("turnaround_avg_biz_hours")
    ta_clock = s.get("turnaround_avg_clock_hours")
    n_ta = s.get("turnaround_entries", 0)

    if not n_ta:
        story.append(Paragraph("No turnaround samples yet. Populated once quoted requests accumulate.", styles["Body"]))
        return

    kpi_row = Table(
        [[kpi_cell("Quotes measured", _fmt_int(n_ta)),
          kpi_cell("Avg biz-hrs", f"{ta_biz:.1f}h" if ta_biz is not None else "—", color=BLUE),
          kpi_cell("Avg clock-hrs", f"{ta_clock:.1f}h" if ta_clock is not None else "—")]],
        colWidths=[2.3 * inch] * 3,
    )
    story.append(kpi_row)
    story.append(Spacer(1, 10))

    # Per-carrier turnaround
    carriers = data.get("carrier_summary", {}) or {}
    if carriers:
        story.append(Paragraph("Turnaround by carrier", styles["H2"]))
        rows = [["Carrier", "Quotes", "Avg biz-hrs", "Wins", "Win rate"]]
        for c, cm in sorted(carriers.items(), key=lambda x: (x[1].get("avg_turnaround_biz_hours") or 9999)):
            ta = cm.get("avg_turnaround_biz_hours")
            rows.append([
                c,
                _fmt_int(cm.get("quotes", 0)),
                f"{ta:.1f}h" if ta else "—",
                _fmt_int(cm.get("wins", 0)),
                _pct(cm.get("win_rate", 0)),
            ])
        t = Table(rows, colWidths=[2.1*inch, 0.8*inch, 1.1*inch, 0.8*inch, 1.0*inch])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTNAME",(0,0),(-1,0),BODY_FONT_BOLD),("FONTSIZE",(0,0),(-1,-1),8.5),
            ("ALIGN",(1,0),(-1,-1),"CENTER"),("GRID",(0,0),(-1,-1),0.3,BORDER),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT]),
            ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
        ]))
        story.append(t)

def build_carriers(story, styles, data):
    carriers = data.get("carrier_summary", {}) or {}
    story.append(PageBreak())
    story.append(Paragraph("Carrier Scoreboard", styles["H1"]))
    story.append(Paragraph("Ranked by win rate (descending). Use per-carrier scorecards for line-level negotiation detail.", styles["BodyMuted"]))
    story.append(Spacer(1, 10))

    if not carriers:
        story.append(Paragraph("No carrier data yet.", styles["Body"]))
        return

    # Per Michael 2026-05-18 (audit feedback): columns were too terse —
    # he misread "W" / "TEU Won" / "Win %" in a similar table as today's
    # wins / dollars / today's rate. Spell out + units + (#) markers.
    rows = [["Carrier", "Times\nQuoted (#)", "Wins (#)", "Lost (#)", "Pending (#)",
             "Win\nRate", "TEU\nWon", "TEU\nLost", "Lanes (#)", "Avg ETD fit"]]
    for c, cm in sorted(carriers.items(), key=lambda x: x[1].get("win_rate", 0), reverse=True):
        ef = cm.get("avg_etd_fit_days")
        if ef is None:
            ef_str = "no ETA on req"
        elif ef > 0:
            ef_str = f"+{ef}d (late)"
        elif ef == 0:
            ef_str = "on date"
        else:
            ef_str = f"{ef}d (early)"
        rows.append([
            c,
            _fmt_int(cm.get("quotes", 0)),
            _fmt_int(cm.get("wins", 0)),
            _fmt_int(cm.get("losses", 0)),
            _fmt_int(cm.get("pending", 0)),
            _pct(cm.get("win_rate", 0)),
            _fmt_int(cm.get("teu_won", 0)),
            _fmt_int(cm.get("teu_lost", 0)),
            _fmt_int(cm.get("lanes_quoted", 0)),
            ef_str,
        ])
    t = Table(rows, colWidths=[1.5*inch, 0.55*inch, 0.4*inch, 0.4*inch, 0.5*inch,
                               0.6*inch, 0.7*inch, 0.7*inch, 0.55*inch, 0.75*inch])
    # Heatmap on Win% column (index 5) — green=high win rate, red=low
    win_pct_cmds = VP.heatmap_style_cmds(
        rows, col_idx=5,
        value_extractor=lambda s: float(str(s).rstrip("%")) if s and "%" in str(s) else None,
        vmin=0, vmax=100, mode="good_high",
    )
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),BODY_FONT_BOLD),("FONTSIZE",(0,0),(-1,-1),8),
        ("ALIGN",(1,0),(-1,-1),"CENTER"),("GRID",(0,0),(-1,-1),0.3,BORDER),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT]),
        ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
    ] + win_pct_cmds))
    story.append(t)

def build_trade_regions(story, styles, data):
    """Volume by Trade Region — must reconcile to summary totals."""
    regions = core.aggregate_trade_regions(data.get("requests", []) or [])
    if not regions:
        return
    summary = data.get("summary", {}) or {}
    story.append(PageBreak())
    story.append(Paragraph("Volume by Trade Region", styles["H1"]))
    story.append(Paragraph(
        "Destinations grouped by trade region. Totals reconcile to summary KPIs. "
        "&quot;Unmapped&quot; rows = destinations not in core._TRADE_REGION_MAP.",
        styles["BodyMuted"]))
    story.append(Spacer(1, 10))

    ordered = sorted(regions.values(), key=lambda m: m.get("teu_requested", 0), reverse=True)
    rows = [["Region", "Requests\n(#)", "Wins\n(#)", "Q&L\n(#)", "NQ\n(#)", "Pending\n(#)", "TEU\nRequested", "TEU\nWon", "Win\nRate"]]
    for m in ordered:
        rows.append([
            m["region"],
            _fmt_int(m["requests"]),
            _fmt_int(m["wins"]),
            _fmt_int(m["quoted_lost"]),
            _fmt_int(m["not_quoted"]),
            _fmt_int(m["pending"]),
            _fmt_int(m["teu_requested"]),
            _fmt_int(m["teu_won"]),
            f"{m['win_rate']}%",
        ])
    # Totals row that proves reconciliation
    rows.append([
        "TOTAL",
        _fmt_int(sum(m["requests"] for m in ordered)),
        _fmt_int(sum(m["wins"] for m in ordered)),
        _fmt_int(sum(m["quoted_lost"] for m in ordered)),
        _fmt_int(sum(m["not_quoted"] for m in ordered)),
        _fmt_int(sum(m["pending"] for m in ordered)),
        _fmt_int(sum(m["teu_requested"] for m in ordered)),
        _fmt_int(sum(m["teu_won"] for m in ordered)),
        "—",
    ])
    t = Table(rows, colWidths=[1.4*inch, 0.55*inch, 0.4*inch, 0.45*inch, 0.4*inch,
                               0.5*inch, 0.75*inch, 0.75*inch, 0.6*inch])
    # Heatmap on Win % column (index 8) — green=high win rate, red=low. Skip
    # totals row (last) which has "—" in this column.
    win_pct_cmds = VP.heatmap_style_cmds(
        rows[:-1], col_idx=8,
        value_extractor=lambda s: float(str(s).rstrip("%")) if s and "%" in str(s) else None,
        vmin=0, vmax=100, mode="good_high",
    )
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),BODY_FONT_BOLD),("FONTSIZE",(0,0),(-1,-1),9),
        ("ALIGN",(1,0),(-1,-1),"CENTER"),("GRID",(0,0),(-1,-1),0.3,BORDER),
        ("ROWBACKGROUNDS",(0,1),(-1,-2),[colors.white, LIGHT]),
        # Highlight Unmapped row in red, totals row in bold gray
        ("FONTNAME",(0,-1),(-1,-1),BODY_FONT_BOLD),
        ("BACKGROUND",(0,-1),(-1,-1),colors.HexColor("#e2e8f0")),
        ("LINEABOVE",(0,-1),(-1,-1),1.5,NAVY),
        ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
    ] + win_pct_cmds))
    story.append(t)
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"Reconciliation check: summary reports "
        f"{summary.get('total_entries',0)} reqs / "
        f"{summary.get('wins',0)} W / {summary.get('quoted_lost',0)} Q&amp;L / "
        f"{summary.get('not_quoted',0)} NQ / {summary.get('pending_hilmar',0)} P. "
        f"Region totals must match.",
        styles["BodyMuted"]))


def build_lanes(story, styles, data):
    lanes = data.get("lane_summary", {}) or {}
    story.append(PageBreak())
    story.append(Paragraph("Lane Performance", styles["H1"]))
    story.append(Paragraph("Ranked by TEU requested (largest opportunity first).", styles["BodyMuted"]))
    story.append(Spacer(1, 10))

    if not lanes:
        story.append(Paragraph("No lane data yet.", styles["Body"]))
        return

    # 2026-07-12 (Michael "poor formatting"): the Winning Carriers cells are
    # multi-carrier lists ("CMA CGM, HMM, Hapag-Lloyd") — as raw strings
    # reportlab does NOT wrap them, so they overflowed the narrow column into
    # the page margin and collided with the TEU-Won bars. Render them as a
    # wrapping Paragraph (left-aligned, escaped) so the text flows inside the
    # column; the column is also widened below.
    def _carriers_cell(txt):
        safe = (str(txt).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
        return Paragraph(safe, styles["Tiny"])

    rows = [["Lane", "Requests\n(#)", "Wins\n(#)", "Q&L\n(#)", "NQ\n(#)", "Pending\n(#)", "TEU\nRequested", "TEU\nWon", "Winning Carriers"]]
    for lane, lm in sorted(lanes.items(), key=lambda x: x[1].get("teu_requested", 0), reverse=True)[:20]:
        rows.append([
            lane,
            _fmt_int(lm.get("requests", 0)),
            _fmt_int(lm.get("wins", 0)),
            _fmt_int(lm.get("quoted_lost", 0)),
            _fmt_int(lm.get("not_quoted", 0)),
            _fmt_int(lm.get("pending", 0)),
            _fmt_int(lm.get("teu_requested", 0)),
            _fmt_int(lm.get("teu_won", 0)),
            _carriers_cell(lm.get("winning_carriers", "") or "—"),
        ])
    # Compute win % per row for heatmap and replace TEU Won (col 7) with bars
    max_teu_won = max((int(str(r[7]).replace(",","")) for r in rows[1:] if str(r[7]).replace(",","").isdigit()), default=1) or 1
    # Insert a virtual "Win %" column for heatmap calculation
    # Actually we don't have win% in this table — compute on the fly per row
    win_pct_cmds = []
    teu_bar_cmds = VP.bar_style_cmds(rows, col_idx=7,
                                       value_extractor=lambda s: float(str(s).replace(",","")) if str(s).replace(",","").isdigit() else 0,
                                       max_value=max_teu_won, color="#059669")
    for r_idx, row in enumerate(rows):
        if r_idx == 0: continue
        try:
            reqs = int(str(row[1]).replace(",",""))
            wins = int(str(row[2]).replace(",",""))
            if reqs > 0:
                wp = wins / reqs * 100
                c = VP.heatmap_color(wp, vmin=0, vmax=100, mode="good_high")
                # Color the wins column (idx 2) by win rate
                win_pct_cmds.append(("BACKGROUND", (2, r_idx), (2, r_idx), c))
        except (ValueError, TypeError):
            pass
    # Usable width = 7.5" (LETTER − 0.5" margins). Widths sum to 7.4": the
    # numeric columns stay tight and centered; Winning Carriers gets 1.75" so
    # a 3-carrier list wraps to two lines instead of overflowing.
    t = Table(rows, colWidths=[1.8*inch, 0.5*inch, 0.45*inch, 0.45*inch, 0.4*inch,
                               0.55*inch, 0.7*inch, 0.8*inch, 1.75*inch])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),BODY_FONT_BOLD),("FONTSIZE",(0,0),(-1,-1),8),
        # Numeric columns (1..7) centered; the carriers column (last) is a
        # left-aligned wrapping Paragraph. VALIGN MIDDLE keeps the numbers and
        # bars centered against a carriers cell that may run to two lines.
        ("ALIGN",(1,0),(7,-1),"CENTER"),("ALIGN",(8,1),(8,-1),"LEFT"),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("GRID",(0,0),(-1,-1),0.3,BORDER),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT]),
        ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
    ] + win_pct_cmds + teu_bar_cmds))
    story.append(t)

def build_pending_trends_qc(story, styles, data):
    reqs = data.get("requests", []) or []
    qc = data.get("qc", {}) or {}
    story.append(PageBreak())
    story.append(Paragraph("Pending Watchlist  •  Rate Trends  •  Data Quality", styles["H1"]))
    story.append(Spacer(1, 8))

    # Pending — split into the two materially different waits so the watchlist
    # says WHO to chase per row, not one lumped "Pending Hilmar decision":
    #   Pending OL     = RFQ sent, OL hasn't quoted yet (chase the OL desk)
    #   Pending Hilmar = OL quoted, Lonny hasn't decided yet (chase Hilmar)
    # (Michael 2026-06-27: "one should be 'pending OL' and one is 'pending
    # Hilmar' for how you show pending".) "Waiting On" leads the table and the
    # rows group OL-first so the two buckets read cleanly.
    pending = [r for r in reqs if r.get("status") == "PENDING"]
    n_ol = sum(1 for r in pending if core.pending_substate(r) == "PENDING_OL")
    n_hil = sum(1 for r in pending if core.pending_substate(r) == "PENDING_HILMAR")
    story.append(Paragraph(
        f"Pending Watchlist ({len(pending)})  —  {n_ol} Pending OL  ·  {n_hil} Pending Hilmar",
        styles["H2"]))
    if pending:
        pend_sorted = sorted(
            pending,
            key=lambda r: (core.pending_substate(r) != "PENDING_OL",
                           r.get("request_date") or ""))
        rows = [["Waiting On", "Lane", "TEU", "Carrier quoted", "Rate", "Aging (biz-hrs)"]]
        for r in pend_sorted[:15]:
            ta = r.get("turnaround_biz_hours")
            rows.append([
                V.pending_label(core.pending_substate(r)),
                r.get("lane", "—"),
                _fmt_int(r.get("teu_requested", 0)),
                r.get("carrier_quoted", "—") or "—",
                r.get("ol_rate", "—") or "—",
                f"{ta:.1f}" if ta else "—",
            ])
        t = Table(rows, colWidths=[1.1*inch, 1.5*inch, 0.5*inch, 1.3*inch, 1.0*inch, 1.1*inch])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTNAME",(0,0),(-1,0),BODY_FONT_BOLD),("FONTSIZE",(0,0),(-1,-1),8),
            ("GRID",(0,0),(-1,-1),0.3,BORDER),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT]),
            ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No pending requests right now.", styles["Body"]))
    story.append(Spacer(1, 10))

    # Rate trends
    try:
        trends = core.rate_trends(reqs)
    except Exception:
        trends = []
    story.append(Paragraph(f"Rate movers ({len(trends)})", styles["H2"]))
    if trends:
        rows = [["Carrier", "Lane", "Samples", "First", "Latest", "Δ %"]]
        for t_ in trends[:12]:
            pct = t_.get("change_pct", 0)
            rows.append([
                t_.get("carrier", "—"),
                t_.get("lane", "—"),
                _fmt_int(t_.get("samples", 0)),
                t_.get("first_rate", "-") or "-",
                t_.get("latest_rate", "-") or "-",
                f"{'+' if pct>0 else ''}{pct:.1f}%",
            ])
        tt = Table(rows, colWidths=[1.2*inch, 1.8*inch, 0.7*inch, 1.1*inch, 1.1*inch, 0.7*inch])
        tt.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTNAME",(0,0),(-1,0),BODY_FONT_BOLD),("FONTSIZE",(0,0),(-1,-1),8),
            ("ALIGN",(2,0),(-1,-1),"CENTER"),("GRID",(0,0),(-1,-1),0.3,BORDER),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT]),
            ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
        ]))
        story.append(tt)
    else:
        story.append(Paragraph("No rate-trend signals yet (need 2+ quotes per carrier/lane).", styles["Body"]))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Data quality", styles["H2"]))
    qc_status = qc.get("status", "unknown")
    qc_issues = qc.get("issues", []) or []
    qc_fixed = qc.get("healed", []) or []
    story.append(Paragraph(
        f"QC status: <b>{qc_status}</b>  -  {len(qc_fixed)} auto-healed  -  {len(qc_issues)} open issue(s)",
        styles["Body"]))
    if qc_issues:
        for i in qc_issues[:6]:
            story.append(Paragraph(f"- {i}", styles["BodyMuted"]))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args()
    cfg = core.load_config(args.config)
    data_path = Path(cfg["paths"]["data"])
    out_path = Path(cfg["paths"]["pdf"])
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not data_path.exists():
        print(f"Data not found: {data_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(data_path.read_text())
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    client = cfg["client"]["name"]
    provider = cfg["provider"]["name"]

    styles = make_styles()
    story = []
    build_cover(story, styles, data, cfg)
    build_dod(story, styles, data)
    build_turnaround(story, styles, data)
    build_carriers(story, styles, data)
    build_trade_regions(story, styles, data)
    build_lanes(story, styles, data)
    build_pending_trends_qc(story, styles, data)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=0.5*inch, rightMargin=0.5*inch,
        topMargin=0.7*inch, bottomMargin=0.55*inch,
        title=f"{client} x {provider} - Rate Desk Report",
        author=provider,
    )
    def on_page(c, d):
        return _header_footer(c, d, client, provider, generated)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    size = out_path.stat().st_size
    print(f"PDF: {size:,} bytes -> {out_path}")


if __name__ == "__main__":
    main()
