"""
hilmar.render — Daily-output rendering.

Merges what was four separate Cowork-mode scripts:

    scripts/gen_dashboard.py            → render_dashboard()
    scripts/gen_pdf.py                  → render_pdf()
    scripts/gen_email.py                → render_email()
    scripts/gen_carrier_scorecard_pdf.py → render_scorecards()

HTML is rendered via Jinja templates in :mod:`hilmar.templates`. PDFs are
rendered via reportlab (pure-Python, no GTK system deps — works on the
Windows dev box AND on the C3 Linux VM without extra installation).

Insights integration (M3.11.c): :func:`render_email` accepts an optional
``insights_html`` kwarg; if provided, it's appended to the email body as
a ``<details>`` block (collapsed by default).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(("html", "xml", "j2")),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _usd(value) -> str:  # noqa: ANN001
    """Format a number as ``$x,xxx.xx`` with thousands separators and
    two decimal places. Strings get a one-shot parse-attempt (so a
    free-form ``ol_rate`` like "$2400/40HC" still renders cleanly).
    None / unparseable → "—" so the cell never shows "$0.00" by accident.
    """
    if value is None:
        return "—"
    if isinstance(value, str):
        from . import core as _c
        parsed = _c.parse_rate(value)
        if parsed is None:
            return value or "—"
        value = parsed
    try:
        return f"${value:,.2f}"
    except (TypeError, ValueError):
        return "—"


_jinja_env.filters["usd"] = _usd


# ─────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────

def _load(data_path: Path) -> dict[str, Any]:
    return json.loads(Path(data_path).read_text(encoding="utf-8"))


def _safe_pct(num: float | int | None, denom: float | int | None) -> float:
    if not denom:
        return 0.0
    return round(100.0 * (num or 0) / denom, 1)


def _today_label(generated_at: str | None) -> str:
    try:
        if generated_at:
            return datetime.fromisoformat(generated_at.rstrip("Z")).strftime("%b %d, %Y")
    except ValueError:
        pass
    return datetime.now().strftime("%b %d, %Y")


def _date_range_dict(data: dict[str, Any]) -> dict[str, str]:
    """Normalize ``date_range`` to a {start, end} dict regardless of
    whether the persisted form is a string label (legacy / golden
    fixture) or a dict (current ingest output). Schema declares both
    shapes via ``oneOf``."""
    rng = data.get("date_range")
    if isinstance(rng, dict):
        return {"start": rng.get("start", "?"), "end": rng.get("end", "?")}
    if isinstance(rng, str) and rng:
        # Try "<start> to <end>" or "<start> -> <end>" pattern
        for sep in (" to ", " -> ", " → ", " - "):
            if sep in rng:
                parts = rng.split(sep, 1)
                return {"start": parts[0].strip(), "end": parts[1].strip()}
        return {"start": rng, "end": rng}
    return {"start": "?", "end": "?"}


def _date_range_label(data: dict[str, Any]) -> str:
    rng = _date_range_dict(data)
    return f"{rng['start']} → {rng['end']}"


# ─────────────────────────────────────────────────────────────────────
# render_dashboard — full HTML report
# ─────────────────────────────────────────────────────────────────────

def render_dashboard(
    *,
    data_path: Path,
    out_path: Path,
    period_trends: dict[str, Any] | None = None,
    pricing_levels: dict[str, Any] | None = None,
) -> Path:
    """Render the full HTML dashboard for a daily snapshot.

    ``period_trends`` and ``pricing_levels`` are optional — orchestrator
    computes them; tests render without to keep them simple.
    """
    from . import core as core_mod
    data = _load(data_path)
    template = _jinja_env.get_template("dashboard.html.j2")
    movers = core_mod.rate_trends(data.get("requests") or [])[:10]
    html = template.render(
        generated_label=_today_label(data.get("generated_at")),
        window_start=_date_range_dict(data)["start"],
        window_end=_date_range_dict(data)["end"],
        summary=data.get("summary") or {},
        requests=data.get("requests") or [],
        lanes=data.get("lanes") or [],
        carriers=data.get("carriers") or [],
        rate_trends=movers,
        period_trends=period_trends,
        pricing_levels=pricing_levels,
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


# ─────────────────────────────────────────────────────────────────────
# render_email — collapsible email body
# ─────────────────────────────────────────────────────────────────────

def _winning_lanes(data: dict[str, Any], top: int = 5) -> list[dict[str, Any]]:
    lanes = data.get("lanes") or []
    return sorted(
        [ln for ln in lanes if (ln.get("wins") or 0) > 0],
        key=lambda x: (-(x.get("wins") or 0), -(x.get("win_rate") or 0)),
    )[:top]


def _losing_lanes(data: dict[str, Any], top: int = 5) -> list[dict[str, Any]]:
    # core.aggregate_lanes uses `requests` for the count, not `total` —
    # pre-fix this filter required `total > 0`, which was always 0, so the
    # losing-lanes block was always empty in the email.
    lanes = data.get("lanes") or []
    return sorted(
        [ln for ln in lanes if (ln.get("wins") or 0) == 0 and (ln.get("requests") or 0) > 0],
        key=lambda x: -(x.get("requests") or 0),
    )[:top]


def _pending_rows(data: dict[str, Any], top: int = 10) -> list[dict[str, Any]]:
    return [r for r in (data.get("requests") or []) if r.get("status") == "PENDING"][:top]


def _request_log_rows(data: dict[str, Any], top: int = 50) -> list[dict[str, Any]]:
    """Per-row request log, sorted most-recent-first. Inlined into the
    email body as the one-click replacement for the previously-attached
    HTML dashboard (per Michael 2026-04-28). Capped at ``top`` so the
    email body stays under typical client display limits.
    """
    rows = list(data.get("requests") or [])
    rows.sort(
        key=lambda r: (r.get("request_timestamp") or r.get("request_date") or ""),
        reverse=True,
    )
    return rows[:top]


def _total_value_won(data: dict[str, Any]) -> float:
    """Total dollar value of WIN bookings today: sum of
    ``rate_per_feu × FEU-equivalent count``. Used for the "Value won
    today" KPI cell + the subject-line headline.
    """
    total = 0.0
    for r in (data.get("requests") or []):
        if r.get("status") != "WIN":
            continue
        rpf = r.get("rate_per_feu")
        if not rpf:
            continue
        teu = r.get("teu_won") or r.get("teu_requested") or 0
        feu = (teu or 0) / 2.0
        try:
            total += float(rpf) * feu
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def _loss_reasons_aggregate(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Aggregate loss_reason counts across all Q&L rows. The
    carrier-scoreboard already shows per-carrier breakdown; this gives
    the bird's-eye "where are we leaking" view at the top.

    Returns a list of ``{reason, count, teu, share_pct}`` sorted by
    count desc. Used for the new "Why are we losing?" table.
    """
    reasons: dict[str, dict[str, Any]] = {}
    total_loss = 0
    for r in (data.get("requests") or []):
        if r.get("status") != "Q&L":
            continue
        reason = r.get("loss_reason") or "OTHER"
        b = reasons.setdefault(reason, {"reason": reason, "count": 0, "teu": 0})
        b["count"] += 1
        b["teu"] += int(r.get("teu_requested") or 0)
        total_loss += 1
    out = []
    for b in reasons.values():
        b["share_pct"] = round(100.0 * b["count"] / total_loss, 1) if total_loss else 0.0
        out.append(b)
    return sorted(out, key=lambda x: -x["count"])


def _trade_region_segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Aggregate today's requests by ``trade_region`` (added in PR #28).

    Returns a list of ``{region, total, wins, q_and_l, win_rate, teu}``
    dicts sorted by total volume descending. Used in the email's
    "Volume by trade region" segment table — answers the "where is
    this business going?" question at a glance.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for r in (data.get("requests") or []):
        region = r.get("trade_region") or "Other"
        b = buckets.setdefault(region, {
            "region": region, "total": 0, "wins": 0, "q_and_l": 0,
            "teu": 0, "value_won": 0.0, "wins_no_rate": 0,
        })
        b["total"] += 1
        b["teu"] += int(r.get("teu_requested") or 0)
        if r.get("status") == "WIN":
            b["wins"] += 1
            rpf = r.get("rate_per_feu") or 0
            teu_won = r.get("teu_won") or r.get("teu_requested") or 0
            try:
                value_added = float(rpf) * (teu_won / 2.0)
            except (TypeError, ValueError):
                value_added = 0
            b["value_won"] += value_added
            # Pure-MDOLX WINs (booking confirmed without a parseable
            # rate-quote email) land here with rpf=0. Track them so the
            # template can disambiguate "no wins" (—) from "wins but no
            # captured rate" (n/a) — the latter is data we couldn't
            # parse, not absence of business.
            if value_added == 0:
                b["wins_no_rate"] += 1
        elif r.get("status") == "Q&L":
            b["q_and_l"] += 1
    out = []
    for b in buckets.values():
        decided = b["wins"] + b["q_and_l"]
        b["win_rate"] = round(100.0 * b["wins"] / decided, 1) if decided else 0.0
        b["value_won"] = round(b["value_won"], 2)
        out.append(b)
    return sorted(out, key=lambda x: -x["total"])


def _today_events(data: dict[str, Any], today_iso: str | None) -> list[dict[str, Any]]:
    if not today_iso:
        return []
    today_str = today_iso[:10]
    out: list[dict[str, Any]] = []
    for r in data.get("requests") or []:
        if (r.get("request_date") or "")[:10] == today_str:
            out.append({
                "label": f"{r.get('lane','?')} — {r.get('status','?')}",
                "detail": r.get("reason_detail") or "",
            })
    return out[:8]


def _carrier_rows_with_rollup(data: dict[str, Any], min_quotes: int = 5) -> list[dict[str, Any]]:
    """Top-carrier-first scoreboard. Carriers with < ``min_quotes``
    quotes get rolled into a single 'Others (N)' row in lighter text.

    Per the LLM-narrative critique 2026-04-28: when MSC is 75% of
    volume, giving CMA CGM (2) and ONE (1) full rows with badges is
    noise — it dilutes the actionable signal. Top carrier gets the
    primary row; small carriers collapse so the table is 2 rows max.
    """
    carriers = data.get("carriers") or []
    if not carriers:
        return []
    sorted_carriers = sorted(carriers, key=lambda c: -(c.get("quotes") or 0))
    primary = [c for c in sorted_carriers if (c.get("quotes") or 0) >= min_quotes]
    small = [c for c in sorted_carriers if (c.get("quotes") or 0) < min_quotes]
    out = list(primary)
    if small:
        out.append({
            "carrier": f"Others ({len(small)})",
            "wins": sum(c.get("wins") or 0 for c in small),
            "losses": sum(c.get("losses") or 0 for c in small),
            "win_rate": "—",
            "loss_reason_summary": ", ".join(c.get("carrier") for c in small),
            "_is_rollup": True,
        })
    return out


def _has_meaningful_dod(dod: dict[str, Any] | None) -> bool:
    """True if the DOD diff has at least one populated sub-list. Pre-fix
    a quiet-day dod rendered just the 'What happened since last run'
    header followed by the summary text and four empty sub-blocks —
    visually broken. Now: render only when there's something to say."""
    if not dod:
        return False
    return bool(
        dod.get("new_requests") or dod.get("new_responses") or
        dod.get("status_changes") or dod.get("new_wins") or
        dod.get("new_pending") or dod.get("newly_lost")
    )


def _alert_anomalies(insights_ctx: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return anomalies with severity='alert' from the insights context
    dict (already serialised). Drives the red banner above the KPI grid.

    Per the LLM-narrative design critique: alert-severity anomalies are
    currently buried in the collapsible insights block at the bottom —
    Michael could glance at the KPI grid, see 25% win rate, and miss a
    "today's ingest is 0" flag. Banner up top fixes the trust caveat
    being the LAST thing read."""
    if not insights_ctx:
        return []
    return [
        a for a in (insights_ctx.get("anomalies") or [])
        if a.get("severity") == "alert"
    ]


def render_email(
    *,
    data_path: Path,
    out_path: Path,
    insights_html: str | None = None,
    dod: dict[str, Any] | None = None,
    insights_ctx: dict[str, Any] | None = None,
    period_trends: dict[str, Any] | None = None,
    pricing_levels: dict[str, Any] | None = None,
    lane_sparklines: dict[str, dict] | None = None,
) -> Path:
    """Render the daily email body. If ``insights_html`` is provided, it's
    appended as a collapsible ``<details>`` block (M3.11.c). If ``dod`` is
    provided (from ``core.compute_dod`` against yesterday's snapshot), a
    rich "What happened since last run" section replaces the fallback
    today_events list. ``insights_ctx`` (post-`context_to_dict`) drives
    the anomaly banner up top."""
    data = _load(data_path)
    template = _jinja_env.get_template("email.html.j2")
    today = _today_label(data.get("generated_at"))
    html = template.render(
        today_label=today,
        range_label=_date_range_label(data),
        updated_label=data.get("generated_at") or "",
        summary=data.get("summary") or {},
        today_events=_today_events(data, data.get("generated_at")),
        dod=dod if _has_meaningful_dod(dod) else None,
        winning_lanes=_winning_lanes(data),
        losing_lanes=_losing_lanes(data),
        carrier_rows=_carrier_rows_with_rollup(data),
        pending_rows=_pending_rows(data),
        request_log_rows=_request_log_rows(data),
        total_value_won=_total_value_won(data),
        trade_region_segments=_trade_region_segments(data),
        loss_reasons=_loss_reasons_aggregate(data),
        alert_anomalies=_alert_anomalies(insights_ctx),
        period_trends=period_trends,
        pricing_levels=pricing_levels,
        lane_sparklines=lane_sparklines or {},
        insights_html=insights_html,
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


# ─────────────────────────────────────────────────────────────────────
# PDF rendering via reportlab (no GTK deps)
# ─────────────────────────────────────────────────────────────────────

def _build_pdf_story(data: dict[str, Any], *, scope_label: str) -> list[Any]:
    """Build the reportlab "story" (flowable list) for the daily PDF.

    Kept inline (not a separate module) because the gen_pdf.py original was
    monolithic; if/when we need richer layout we'll factor it out.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import (
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )

    _ = LETTER  # imported to surface any reportlab issues at story-build time

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18,
                        textColor=colors.HexColor("#0b3d91"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13,
                        textColor=colors.HexColor("#0b3d91"))
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10)

    summary = data.get("summary") or {}
    today = _today_label(data.get("generated_at"))

    story: list[Any] = []
    story.append(Paragraph("Hilmar Rate-Desk Daily", h1))
    story.append(Paragraph(
        f"<b>Window:</b> {scope_label}<br/>"
        f"<b>Generated:</b> {today}<br/>"
        f"<b>Distribution:</b> OL-USA internal &mdash; not for Hilmar Ingredients.<br/>"
        f"<b>Source mailbox:</b> michael.deitchman@ol-usa.com (delegated Microsoft Graph).",
        body,
    ))
    story.append(Spacer(1, 12))

    intro = (
        "This report summarises Hilmar Ingredients rate-desk activity for the "
        "window above. <b>WIN</b> means Lonny replied with a Send/Book on a "
        "thread where OL-USA already had a quote out, AND an MDOLX booking "
        "confirmation was extracted. <b>Q&amp;L</b> (Quoted &amp; Lost) means "
        "OL-USA quoted but Lonny moved the load with another carrier. <b>NQ</b> "
        "(Not Quoted) means OL-USA never responded to the rate request inside "
        "the SLA window. Counts include standalone bookings whose original Lonny "
        "ask landed outside the search window."
    )
    story.append(Paragraph(intro, body))
    story.append(Spacer(1, 14))

    # KPI table
    kpi_rows = [
        ["Total entries", "Wins", "Q&L", "Not quoted", "Pending", "Win rate", "Quote rate"],
        [
            str(summary.get("total_entries", 0)),
            str(summary.get("wins", 0)),
            str(summary.get("quoted_lost", 0)),
            str(summary.get("not_quoted", 0)),
            str(summary.get("pending_hilmar", 0)),
            f"{summary.get('win_rate', 0)}%",
            f"{summary.get('quote_rate', 0)}%",
        ],
    ]
    kpi_tbl = Table(kpi_rows, hAlign="LEFT")
    kpi_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
    ]))
    story.append(kpi_tbl)
    story.append(Spacer(1, 18))

    # Lanes
    lanes = data.get("lanes") or []
    if lanes:
        story.append(Paragraph("Lanes", h2))
        lane_rows = [["Lane", "Total", "Wins", "Win rate", "TEU won"]]
        # core.aggregate_lanes outputs `requests` (count) per lane and does
        # NOT compute `win_rate` — render computes it inline. Pre-fix this
        # read `ln.get("total")` (wrong field name) and `ln.get("win_rate")`
        # (no such field), so every row showed Total=0 and Win rate=0%
        # regardless of actual data.
        for ln in lanes[:30]:
            total = ln.get("requests", 0) or 0
            wins = ln.get("wins", 0) or 0
            win_rate = round(100.0 * wins / total, 1) if total else 0.0
            lane_rows.append([
                str(ln.get("lane", ""))[:38],
                str(total),
                str(wins),
                f"{win_rate}%",
                str(ln.get("teu_won", 0)),
            ])
        lane_tbl = Table(lane_rows, hAlign="LEFT")
        lane_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(lane_tbl)
        story.append(Spacer(1, 18))

    # Carriers
    carriers = data.get("carriers") or []
    if carriers:
        story.append(Paragraph("Carriers", h2))
        carrier_rows = [["Carrier", "Wins", "Q&L", "Win rate", "TEU won"]]
        for c in carriers[:30]:
            carrier_rows.append([
                str(c.get("carrier", ""))[:38],
                str(c.get("wins", 0)),
                str(c.get("quoted_lost", 0)),
                f"{c.get('win_rate', 0)}%",
                str(c.get("teu_won", 0)),
            ])
        c_tbl = Table(carrier_rows, hAlign="LEFT")
        c_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(c_tbl)
        story.append(Spacer(1, 18))

    # Recent requests preview — visual sanity check for the reader.
    # Sort by request_date DESC so "most recent 12" actually shows the
    # latest 12. Pre-fix this took `requests[:12]` with no sort, so the
    # PDF showed the OLDEST 12 in the window — header lied.
    requests = data.get("requests") or []
    if requests:
        story.append(Paragraph("Recent requests (most recent 12)", h2))
        rows = [["Date", "Lane", "Status", "Carrier", "Rate", "MDOLX"]]
        recent = sorted(
            requests,
            key=lambda r: (r.get("request_date") or "", r.get("request_timestamp") or ""),
            reverse=True,
        )[:12]
        for r in recent:
            rows.append([
                str(r.get("request_date") or "-")[:10],
                str(r.get("lane") or "-")[:32],
                str(r.get("status") or "-"),
                str(r.get("carrier_won") or r.get("carrier_quoted") or "-")[:18],
                str(r.get("ol_rate") or "-")[:10],
                str(r.get("mdolx_ref") or "-")[:10],
            ])
        rec = Table(rows, hAlign="LEFT")
        rec.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(rec)
        story.append(Spacer(1, 16))

    # Footer notes — bulk + transparency about what went into this report.
    notes = (data.get("notes") or {})
    note_blob = (
        f"<b>Ingest model:</b> {notes.get('ingest_model','(legacy)')}<br/>"
        f"<b>OL responder rule:</b> {notes.get('ol_responder_rule','(legacy)')}<br/>"
        f"<b>Auth:</b> {notes.get('auth','(legacy)')}<br/>"
        "<b>Dedup:</b> idempotent merge on request_id; existing populated "
        "fields preserved; status / lane / turnaround recomputed each run via "
        "<i>core.decide_status</i>."
    )
    story.append(Paragraph(note_blob, body))

    return story


def render_pdf(*, data_path: Path, out_path: Path) -> Path:
    """Render the daily PDF report. Reportlab-only, no system libs."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.platypus import SimpleDocTemplate

    data = _load(data_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36,
    )
    story = _build_pdf_story(data, scope_label=_date_range_label(data))
    doc.build(story)
    return out_path


# ─────────────────────────────────────────────────────────────────────
# Carrier scorecards — one PDF per carrier
# ─────────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in (name or "unknown")).strip("-").lower() or "unknown"


def _aggregate_for_carrier(requests: list[dict[str, Any]], carrier: str) -> dict[str, Any]:
    won = [r for r in requests if (r.get("carrier_won") or "").lower() == carrier.lower()]
    quoted = [r for r in requests if (r.get("carrier_quoted") or "").lower() == carrier.lower()]
    # "Decided" = WIN or any LOSS variant. Pre 2026-04-27 single LOSS
    # status; post-cutover the LOSS family is split into Q&L + NQ.
    decided = [r for r in quoted if r.get("status") in ("WIN", "Q&L", "NQ")]
    win_rate = _safe_pct(len(won), len(decided))
    teu_won = sum(int(r.get("teu_won") or 0) for r in won)
    lane_counter: Counter[str] = Counter()
    for r in won:
        lane = (r.get("lane") or "Unknown").strip()
        lane_counter[lane] += 1
    return {
        "carrier": carrier,
        "wins": len(won),
        "quoted": len(quoted),
        "decided": len(decided),
        "win_rate": win_rate,
        "teu_won": teu_won,
        "top_lanes": lane_counter.most_common(5),
    }


def _build_scorecard_story(agg: dict[str, Any], data: dict[str, Any]) -> list[Any]:
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18,
                        textColor=colors.HexColor("#0b3d91"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13,
                        textColor=colors.HexColor("#0b3d91"))
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10)

    today = _today_label(data.get("generated_at"))
    story: list[Any] = []
    story.append(Paragraph(f"Carrier Scorecard — {agg['carrier']}", h1))
    story.append(Paragraph(f"Window {_date_range_label(data)} &middot; Generated {today}", body))
    story.append(Spacer(1, 14))

    kpi = [
        ["Wins", "Quoted", "Decided", "Win rate", "TEU won"],
        [
            str(agg["wins"]),
            str(agg["quoted"]),
            str(agg["decided"]),
            f"{agg['win_rate']}%",
            str(agg["teu_won"]),
        ],
    ]
    tbl = Table(kpi, hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 18))

    if agg["top_lanes"]:
        story.append(Paragraph("Top winning lanes", h2))
        rows = [["Lane", "Wins"]]
        for lane, n in agg["top_lanes"]:
            rows.append([str(lane)[:48], str(n)])
        tbl_l = Table(rows, hAlign="LEFT")
        tbl_l.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(tbl_l)

    return story


def render_scorecards(*, data_path: Path, out_dir: Path) -> list[Path]:
    """Render one scorecard PDF per carrier present in the data.

    Returns the list of paths actually written. ``out_dir`` is created if
    it doesn't exist. Carriers with zero wins AND zero quotes are skipped.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.platypus import SimpleDocTemplate

    data = _load(data_path)
    requests = data.get("requests") or []

    carriers_seen: set[str] = set()
    for r in requests:
        for key in ("carrier_won", "carrier_quoted"):
            v = r.get(key)
            if v and v.strip():
                carriers_seen.add(v.strip())

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for carrier in sorted(carriers_seen):
        agg = _aggregate_for_carrier(requests, carrier)
        if agg["wins"] == 0 and agg["quoted"] == 0:
            continue
        out_path = out_dir / f"scorecard-{_slug(carrier)}.pdf"
        doc = SimpleDocTemplate(
            str(out_path), pagesize=LETTER,
            leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36,
        )
        doc.build(_build_scorecard_story(agg, data))
        written.append(out_path)
    return written


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    """Entrypoint for ``hilmar-render``. Renders all four artifacts."""
    import argparse

    ap = argparse.ArgumentParser(description="Hilmar daily render — all 4 artifacts.")
    ap.add_argument("--data", type=Path, required=True, help="tracking-data-v2.json path")
    ap.add_argument("--reports", type=Path, required=True, help="output directory")
    args = ap.parse_args()

    reports = Path(args.reports)
    reports.mkdir(parents=True, exist_ok=True)

    render_dashboard(data_path=args.data, out_path=reports / "hilmar-dashboard.html")
    render_pdf(       data_path=args.data, out_path=reports / "hilmar-report.pdf")
    render_email(     data_path=args.data, out_path=reports / "email-body.html")
    render_scorecards(data_path=args.data, out_dir=reports / "carrier-scorecards")
    print(f"Wrote artifacts to {reports}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
