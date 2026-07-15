"""
gen_combined_pdf.py — ONE consolidated daily PDF.

Michael 2026-07-15: "the 3 reports should be made into one and emailed to
everyone as pdfs." The three daily reports become parts of a single PDF
(reports/hilmar-combined.pdf) attached to ONE staff email:

  Part 1 — Rate Desk Report        (the 6-page tracker PDF — gen_pdf builders)
  Part 2 — Client Service Update   (PDF rendition of what Hilmar receives)
  Part 3 — Systems Audit           (red flags / observations / suggestions)

DISTRIBUTION: the INTERNAL 10-recipient staff list (distribution.full_list)
ONLY. Lonny's separate client email is unchanged; the client never receives
Parts 1 or 3 (QC-065 — internal analytics never reach the client).

Usage:
  python3 scripts/gen_combined_pdf.py
  python3 scripts/gen_combined_pdf.py --config config.json --out reports/hilmar-combined.pdf
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as _xesc

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import gen_client_email as GC  # noqa: E402
import gen_improvements_report as GIR  # noqa: E402
import gen_pdf as GP  # noqa: E402
from reportlab.lib.pagesizes import LETTER  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

REPORTS = ROOT / "reports"
DEFAULT_OUT = REPORTS / "hilmar-combined.pdf"


def _p(text, style):
    """Paragraph with reportlab mini-HTML special chars escaped."""
    return Paragraph(_xesc(str(text)), style)


def _part_header(story, styles, part, title, blurb):
    story.append(PageBreak())
    story.append(Paragraph(f"Part {part} — {_xesc(title)}", styles["H1"]))
    story.append(_p(blurb, styles["BodyMuted"]))
    story.append(Spacer(1, 10))


def _simple_table(styles, headers, rows, col_widths=None):
    """Uniform data table in the gen_pdf visual language."""
    data = [[Paragraph(f"<b>{_xesc(str(h))}</b>", styles["Tiny"]) for h in headers]]
    for row in rows:
        data.append([Paragraph(_xesc(str(v)), styles["Tiny"]) for v in row])
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), GP.NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), GP.LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.4, GP.BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [None, GP.LIGHT]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _client_section(story, styles, title, note, headers, row_values, col_widths=None):
    story.append(Paragraph(_xesc(title) + f" ({len(row_values)})", styles["H2"]))
    story.append(_p(note, styles["BodyMuted"]))
    if row_values:
        story.append(_simple_table(styles, headers, row_values, col_widths))
    else:
        story.append(_p("None today.", styles["Body"]))
    story.append(Spacer(1, 8))


def build_client_part(story, styles, data, now=None):
    """Part 2 — PDF rendition of the client service update. SAME buckets and
    resolved-lane filters as gen_client_email (imported, not reimplemented),
    so this can never show more than the client email does."""
    report_date = GC._report_date(GC._now_et(now))
    s = GC._client_sections(data, report_date)
    active = GC._active_shipments(data, report_date)
    cutoffs = GC._upcoming_cutoffs(active, report_date)

    _part_header(
        story, styles, 2, "Client Service Update",
        "Copy of the daily service update Hilmar (Lonny Upfold) receives by "
        "email — client-safe content only, reproduced here so staff see "
        "exactly what the client sees.",
    )

    # KPI strip — the client email's four hero tiles.
    kpis = Table([[
        GP.kpi_cell("REQUESTS TODAY", str(len(s["requests"])), f"{GC._teu_sum(s['requests'])} TEU", GP.BLUE),
        GP.kpi_cell("QUOTES TODAY", str(len(s["quotes"])), f"{GC._teu_sum(s['quotes'])} TEU", GP.PURPLE),
        GP.kpi_cell("BOOKINGS TODAY", str(len(s["bookings"])), f"{GC._teu_sum(s['bookings'], won=True)} TEU", GP.GREEN),
        GP.kpi_cell("AWAITING DECISION", str(len(s["awaiting"])), f"{GC._teu_sum(s['awaiting'])} TEU", GP.AMBER),
    ]], colWidths=[1.875 * inch] * 4)
    kpis.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(kpis)
    story.append(Spacer(1, 6))
    story.append(_p(GC._narrative(s), styles["Body"]))
    story.append(Spacer(1, 8))

    if cutoffs:
        story.append(Paragraph("Time-sensitive dates", styles["H2"]))
        for line in cutoffs:
            story.append(_p("• " + line, styles["Body"]))
        story.append(Spacer(1, 8))

    _client_section(
        story, styles, "Active shipments",
        "Confirmed bookings from the last 14 days, sorted by departure.",
        ["Lane", "Carrier", "Booking ref", "ETD", "ETA", "Doc cutoff"],
        [[GC._lane(r),
          r.get("carrier_won") or r.get("carrier_quoted") or "—",
          r.get("mdolx_ref") or "Confirmation to follow",
          r.get("etd_offered") or "—",
          r.get("eta_offered") or "—",
          r.get("doc_cutoff") or "—"] for r in active],
    )
    _client_section(
        story, styles, "Rates delivered today",
        "Quotes returned on today's requests.",
        ["Lane", "Carrier", "Rate", "ETD"],
        [[GC._lane(r),
          r.get("carrier_quoted") or "—",
          GC._rate(r),
          r.get("etd_offered") or "—"] for r in s["quotes"]],
    )
    _client_section(
        story, styles, "Awaiting client decision",
        "Rates delivered, booking decision open.",
        ["Lane", "Carrier", "Rate", "Quoted"],
        [[GC._lane(r),
          r.get("carrier_quoted") or "—",
          GC._rate(r),
          GC._short_et(r.get("response_timestamp"))] for r in s["awaiting"]],
    )
    _client_section(
        story, styles, "In progress with OL",
        "New requests where OL-USA is sourcing rates.",
        ["Lane", "Equipment", "Requested"],
        [[GC._lane(r),
          r.get("containers") or "—",
          GC._short_pt(r.get("request_timestamp"))] for r in s["in_progress"]],
    )


def _audit_items(story, styles, title, items, empty_msg):
    story.append(Paragraph(f"{_xesc(title)} ({len(items)})", styles["H2"]))
    if not items:
        story.append(_p(empty_msg, styles["Body"]))
    for it in items:
        story.append(_p(f"{it.get('level', '')} {it.get('title', '')}".strip(), styles["Body"]))
        if it.get("detail"):
            story.append(_p(it["detail"], styles["BodyMuted"]))
        story.append(Spacer(1, 4))
    story.append(Spacer(1, 8))


def build_audit_part(story, styles, data, qc, drift):
    """Part 3 — the private systems audit, from the SAME collectors that
    build improvements-report.html (imported, not duplicated)."""
    red = GIR.collect_red_flags(data, qc, drift)
    yellow = GIR.collect_observations(data, qc, drift)
    suggestions = GIR.collect_suggestions(data, qc, drift)

    _part_header(
        story, styles, 3, "Systems Audit",
        "Daily pipeline self-audit: data-quality red flags, observations, and "
        "improvement suggestions. Internal only — never client-facing.",
    )

    qc_status = qc.get("status", "UNKNOWN")
    counts = qc.get("counts") or {}
    story.append(_p(
        f"QC status: {qc_status} — {qc.get('errors', 0)} errors, "
        f"{qc.get('warnings', 0)} warnings, {qc.get('fixes', 0)} self-heals "
        f"across {counts.get('total', '?')} tracked requests.",
        styles["Body"],
    ))
    story.append(Spacer(1, 8))

    _audit_items(story, styles, "Red flags", red, "None — no hard issues today.")
    _audit_items(story, styles, "Observations", yellow, "None today.")
    _audit_items(story, styles, "Suggestions", suggestions, "None today.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)

    cfg = core.load_config(args.config)
    data_path = Path(cfg["paths"]["data"])
    if not data_path.exists():
        print(f"Data not found: {data_path}", file=sys.stderr)
        return 1
    data = json.loads(data_path.read_text(encoding="utf-8"))
    qc = GIR._read_json(REPORTS / "qc-result.json") or {}
    drift = GIR._read_json(REPORTS / "drift-result.json") or {}

    client = cfg["client"]["name"]
    provider = cfg["provider"]["name"]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    styles = GP.make_styles()
    story = []
    # Part 1 — the full tracker report, exactly gen_pdf.main()'s page flow.
    GP.build_cover(story, styles, data, cfg)
    GP.build_dod(story, styles, data)
    GP.build_turnaround(story, styles, data)
    GP.build_carriers(story, styles, data)
    GP.build_trade_regions(story, styles, data)
    GP.build_lanes(story, styles, data)
    GP.build_pending_trends_qc(story, styles, data)
    # Parts 2 + 3.
    build_client_part(story, styles, data)
    build_audit_part(story, styles, data, qc, drift)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.7 * inch, bottomMargin=0.55 * inch,
        title=f"{client} x {provider} - Daily Combined Report",
        author=provider,
    )

    def on_page(c, d):
        return GP._header_footer(c, d, client, provider, generated)

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    print(f"Combined PDF: {out_path.stat().st_size:,} bytes -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
