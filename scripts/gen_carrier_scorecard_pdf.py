"""
gen_carrier_scorecard_pdf.py — One-page PDF per carrier for steamship-line meetings.

Each carrier gets its own PDF with:
  • Hero stats (quotes, wins, TEU won/lost, win rate, avg turnaround, avg ETD fit)
  • Lanes won (where we're competitive)
  • Lanes lost to other carriers (what the line should chase)
  • Rate posture (movers up/down)
  • Pending quotes (still live)

Usage:
  python3 scripts/gen_carrier_scorecard_pdf.py
  python3 scripts/gen_carrier_scorecard_pdf.py --carrier "MSC"     # just one
  python3 scripts/gen_carrier_scorecard_pdf.py --config config.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sys as _sys2
from pathlib import Path as _Path2

import core  # noqa: E402
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch

_sys2.path.insert(0, str(_Path2(__file__).resolve().parent))
import branding as B  # noqa: E402  Hilmar logo
import viz_pdf as VP  # noqa: E402  shared reportlab visual helpers
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# Register Inter — falls back to Helvetica if assets missing
_FONTS_DIR = ROOT / "assets" / "fonts"
BODY_FONT = "Helvetica"
BODY_FONT_BOLD = "Helvetica-Bold"
BODY_FONT_MED = "Helvetica"
try:
    if (_FONTS_DIR / "Inter-Regular.ttf").exists():
        if "Inter" not in pdfmetrics.getRegisteredFontNames():
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
    print(f"[scorecard] Inter registration failed ({_e})", file=sys.stderr)


NAVY = colors.HexColor("#0f172a")
SLATE = colors.HexColor("#475569")
LIGHT = colors.HexColor("#f8fafc")
BORDER = colors.HexColor("#e2e8f0")
GREEN = colors.HexColor("#059669")
RED = colors.HexColor("#dc2626")
AMBER = colors.HexColor("#d97706")
BLUE = colors.HexColor("#2563eb")
PURPLE = colors.HexColor("#7c3aed")


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("H1", parent=ss["Heading1"], fontName=BODY_FONT_BOLD, fontSize=18, textColor=NAVY, spaceAfter=4, leading=22))
    ss.add(ParagraphStyle("H2", parent=ss["Heading2"], fontName=BODY_FONT_BOLD, fontSize=12, textColor=NAVY, spaceBefore=8, spaceAfter=4, leading=16))
    ss.add(ParagraphStyle("Body", parent=ss["BodyText"], fontName=BODY_FONT, fontSize=9, textColor=NAVY, leading=12))
    ss.add(ParagraphStyle("Muted", parent=ss["BodyText"], fontName=BODY_FONT, fontSize=8, textColor=SLATE, leading=11))
    ss.add(ParagraphStyle("KPINum", parent=ss["BodyText"], fontName=BODY_FONT_BOLD, fontSize=16, textColor=NAVY, alignment=TA_CENTER, leading=18))
    ss.add(ParagraphStyle("KPILbl", parent=ss["BodyText"], fontName=BODY_FONT_MED, fontSize=7, textColor=SLATE, alignment=TA_CENTER, leading=9))
    return ss


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "carrier"


def _fmt_int(v):
    try:
        return f"{int(v):,}"
    except Exception:
        return "—"


def _pct(n):
    return f"{n:.1f}%" if isinstance(n, (int, float)) else "—"


def _kpi(label, value, color=NAVY):
    styles = _styles()
    big = ParagraphStyle("K", parent=styles["KPINum"], textColor=color)
    t = Table(
        [[Paragraph(f"<b>{value}</b>", big)], [Paragraph(label, styles["KPILbl"])]],
        colWidths=[1.15 * inch], rowHeights=[0.36 * inch, 0.16 * inch],
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _header_footer(canvas, doc, carrier, client, provider, generated):
    canvas.saveState()
    w, h = LETTER
    canvas.setFont(BODY_FONT, 7)
    canvas.setFillColor(SLATE)
    canvas.drawString(0.5 * inch, 0.4 * inch,
                      f"{carrier} Scorecard  •  {client} × {provider}  •  Confidential  •  Generated {generated}")
    canvas.drawRightString(w - 0.5 * inch, 0.4 * inch, f"Page {doc.page}")
    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(0.5 * inch, h - 0.55 * inch, w - 0.5 * inch, h - 0.55 * inch)
    canvas.restoreState()


def _aggregate_lanes(carrier_reqs, won=True):
    """Aggregate lane stats for a carrier."""
    agg = defaultdict(lambda: {"count": 0, "teu": 0, "equip": set(), "rates": []})
    for r in carrier_reqs:
        if won and r.get("status") != "WIN":
            continue
        if not won and (r.get("status") != "LOSS" or not r.get("quoted")):
            continue
        lane = r.get("lane") or r.get("destination") or "Unknown"
        a = agg[lane]
        a["count"] += 1
        a["teu"] += r.get("teu_requested", 0) or 0
        a["equip"].add(r.get("containers", ""))
        rate = r.get("ol_rate")
        if rate is not None:
            a["rates"].append(f"${rate:,.0f}" if isinstance(rate, (int, float)) else str(rate))
    return agg


def build_scorecard(carrier, data, cfg):
    """Return reportlab story for one carrier's 1-pager."""
    styles = _styles()
    reqs = data.get("requests", []) or []
    carriers = data.get("carrier_summary", {}) or {}
    cm = carriers.get(carrier, {}) or {}
    carrier_reqs = [r for r in reqs
                    if r.get("carrier_quoted") == carrier or r.get("carrier_won") == carrier]

    story = []
    # Logo at top of each carrier scorecard (no-op if file missing)
    logo_img = B.logo_reportlab_image(width=120)
    if logo_img:
        story.append(logo_img)
        story.append(Spacer(1, 8))
    story.append(Paragraph(f"{carrier} — Steamship Line Scorecard", styles["H1"]))
    story.append(Paragraph(
        f"For line-level negotiations with {carrier}.  "
        f"Data window: {core.format_date_range(data.get('date_range'))}  •  "
        f"Hilmar / {cfg['client']['name']} × {cfg['provider']['name']}",
        styles["Muted"]))
    story.append(Spacer(1, 10))

    # KPI strip
    ef = cm.get("avg_etd_fit_days")
    if ef is None:
        ef_str = "no ETA on req"
    elif ef > 0:
        ef_str = f"+{ef}d (late)"
    elif ef == 0:
        ef_str = "on date"
    else:
        ef_str = f"{ef}d (early)"
    ta = cm.get("avg_turnaround_biz_hours")
    kpis = [
        _kpi("Quotes", _fmt_int(cm.get("quotes", 0))),
        _kpi("Wins", _fmt_int(cm.get("wins", 0)), GREEN),
        _kpi("Losses", _fmt_int(cm.get("losses", 0)), RED),
        _kpi("Pending", _fmt_int(cm.get("pending", 0)), PURPLE),
        _kpi("Win rate", _pct(cm.get("win_rate", 0)), BLUE),
        _kpi("TEU won", _fmt_int(cm.get("teu_won", 0)), GREEN),
    ]
    kpi_row1 = Table([kpis], colWidths=[1.18 * inch] * 6, rowHeights=[0.56 * inch])
    story.append(kpi_row1)
    story.append(Spacer(1, 6))

    kpis2 = [
        _kpi("TEU lost", _fmt_int(cm.get("teu_lost", 0)), RED),
        _kpi("Lanes quoted", _fmt_int(cm.get("lanes_quoted", 0))),
        _kpi("Avg biz-hrs", f"{ta:.1f}h" if ta else "—"),
        _kpi("Avg ETD fit", ef_str),
    ]
    kpi_row2 = Table([kpis2], colWidths=[1.18 * inch] * 4, rowHeights=[0.56 * inch])
    story.append(kpi_row2)
    story.append(Spacer(1, 12))

    # Lanes won
    won_agg = _aggregate_lanes(carrier_reqs, won=True)
    story.append(Paragraph("Lanes won — where we're competitive", styles["H2"]))
    if won_agg:
        rows = [["Lane", "Wins", "TEU Won", "Equipment", "Rates"]]
        for lane, a in sorted(won_agg.items(), key=lambda x: x[1]["teu"], reverse=True):
            rows.append([
                lane, _fmt_int(a["count"]), _fmt_int(a["teu"]),
                ", ".join(sorted(e for e in a["equip"] if e)) or "—",
                ", ".join(a["rates"][:3]) or "—",
            ])
        won_teu_bar_cmds = VP.bar_style_cmds(
            rows, col_idx=2,
            value_extractor=lambda s: float(str(s).replace(",","")) if str(s).replace(",","").isdigit() else 0,
            color="#059669",
        )
        t = Table(rows, colWidths=[1.8*inch, 0.55*inch, 0.85*inch, 1.4*inch, 2.05*inch])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTNAME",(0,0),(-1,0),BODY_FONT_BOLD),("FONTSIZE",(0,0),(-1,-1),8),
            ("ALIGN",(1,0),(2,-1),"CENTER"),("GRID",(0,0),(-1,-1),0.3,BORDER),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT]),
            ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
        ] + won_teu_bar_cmds))
        story.append(t)
    else:
        story.append(Paragraph("No wins yet.", styles["Muted"]))
    story.append(Spacer(1, 8))

    # Lanes lost
    lost_agg = _aggregate_lanes(carrier_reqs, won=False)
    story.append(Paragraph("Lanes lost — what the line should chase", styles["H2"]))
    if lost_agg:
        rows = [["Lane", "Losses", "TEU Lost", "Equipment", "Rates quoted"]]
        for lane, a in sorted(lost_agg.items(), key=lambda x: x[1]["teu"], reverse=True):
            rows.append([
                lane, _fmt_int(a["count"]), _fmt_int(a["teu"]),
                ", ".join(sorted(e for e in a["equip"] if e)) or "—",
                ", ".join(a["rates"][:3]) or "—",
            ])
        lost_teu_bar_cmds = VP.bar_style_cmds(
            rows, col_idx=2,
            value_extractor=lambda s: float(str(s).replace(",","")) if str(s).replace(",","").isdigit() else 0,
            color="#dc2626",
        )
        t = Table(rows, colWidths=[1.8*inch, 0.55*inch, 0.85*inch, 1.4*inch, 2.05*inch])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),RED),("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTNAME",(0,0),(-1,0),BODY_FONT_BOLD),("FONTSIZE",(0,0),(-1,-1),8),
            ("ALIGN",(1,0),(2,-1),"CENTER"),("GRID",(0,0),(-1,-1),0.3,BORDER),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT]),
            ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
        ] + lost_teu_bar_cmds))
        story.append(t)
    else:
        story.append(Paragraph("No lost lanes.", styles["Muted"]))
    story.append(Spacer(1, 8))

    # Rate posture (trends filtered to this carrier)
    try:
        # `core` is imported at module scope (line 32). A redundant local
        # `import core` used to sit here, which made the name FUNCTION-LOCAL
        # for all of build_scorecard — so any earlier use in the same function
        # raised UnboundLocalError, which is exactly what happened the moment
        # the header started calling core.format_date_range.
        trends = [t for t in core.rate_trends(reqs) if t.get("carrier") == carrier]
    except Exception:
        trends = []
    story.append(Paragraph("Rate posture — movers", styles["H2"]))
    if trends:
        rows = [["Lane", "Samples", "First", "Latest", "Δ %"]]
        for tr in trends[:10]:
            pct = tr.get("change_pct", 0)
            rows.append([
                tr.get("lane", "—"), _fmt_int(tr.get("samples", 0)),
                tr.get("first_rate", "—") or "—",
                tr.get("latest_rate", "—") or "—",
                f"{'+' if pct > 0 else ''}{pct:.1f}%",
            ])
        t = Table(rows, colWidths=[2.0*inch, 0.8*inch, 1.3*inch, 1.3*inch, 1.0*inch])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTNAME",(0,0),(-1,0),BODY_FONT_BOLD),("FONTSIZE",(0,0),(-1,-1),8),
            ("ALIGN",(1,0),(-1,-1),"CENTER"),("GRID",(0,0),(-1,-1),0.3,BORDER),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT]),
            ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No rate-trend signals (need 2+ quotes per lane).", styles["Muted"]))
    story.append(Spacer(1, 8))

    # Pending
    pending = [r for r in carrier_reqs if r.get("status") == "PENDING"]
    story.append(Paragraph(f"Open quotes pending Hilmar ({len(pending)})", styles["H2"]))
    if pending:
        rows = [["Request", "Lane", "TEU", "Rate", "Aging (biz-hrs)"]]
        for r in pending[:10]:
            ta = r.get("turnaround_biz_hours")
            rows.append([
                r.get("request_id", "")[:10],
                r.get("lane", "—"),
                _fmt_int(r.get("teu_requested", 0)),
                r.get("ol_rate", "—") or "—",
                f"{ta:.1f}" if ta else "—",
            ])
        t = Table(rows, colWidths=[1.0*inch, 2.0*inch, 0.6*inch, 1.6*inch, 1.2*inch])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),PURPLE),("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTNAME",(0,0),(-1,0),BODY_FONT_BOLD),("FONTSIZE",(0,0),(-1,-1),8),
            ("GRID",(0,0),(-1,-1),0.3,BORDER),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, LIGHT]),
            ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No open pending quotes.", styles["Muted"]))

    return story


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--carrier", help="Only generate scorecard for this carrier")
    args = ap.parse_args()

    cfg = core.load_config(args.config)
    data_path = Path(cfg["paths"]["data"])
    out_dir = Path(cfg["paths"]["carrier_scorecards_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    if not data_path.exists():
        print(f"❌ Data not found: {data_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(data_path.read_text(encoding="utf-8"))
    carriers = data.get("carrier_summary", {}) or {}

    if args.carrier:
        targets = [args.carrier] if args.carrier in carriers else []
        if not targets:
            print(f"⚠️  Carrier '{args.carrier}' not found. Known: {list(carriers.keys()) or '(none)'}")
            sys.exit(1)
    else:
        targets = sorted(carriers.keys())

    if not targets:
        print("⚠️  No carriers in data yet — skipping scorecards.")
        return

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    client = cfg["client"]["name"]
    provider = cfg["provider"]["name"]

    count = 0
    for carrier in targets:
        story = build_scorecard(carrier, data, cfg)
        out = out_dir / f"{_slug(carrier)}-scorecard.pdf"
        doc = SimpleDocTemplate(
            str(out), pagesize=LETTER,
            leftMargin=0.5*inch, rightMargin=0.5*inch,
            topMargin=0.7*inch, bottomMargin=0.55*inch,
            title=f"{carrier} — Steamship Line Scorecard",
            author=provider,
        )
        def on_page(c, d, car=carrier):
            return _header_footer(c, d, car, client, provider, generated)
        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
        print(f"  ✅ {carrier}: {out.stat().st_size:,} bytes → {out}")
        count += 1
    print(f"Generated {count} carrier scorecard(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
