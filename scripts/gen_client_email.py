"""
gen_client_email.py — CLIENT-facing daily service update email.

A SEPARATE artifact from gen_email.py (the staff email). This one goes to
THE CLIENT — Lonny Upfold at Hilmar Ingredients — so it is a service update
from OL-USA to its customer and carries ZERO internal analytics: no win/loss
rates, no "Quoted & Lost"/NQ framing, no carrier scoreboards, no rate-
negotiation intel, no QC/parser/system language. QC-065 enforces both the
recipient invariants and this content rule on the rendered body.

LAYOUT (2026-07-11 redesign — Michael: the first client sample was
"terrible"; rebuilt as a premium service update):
  1. Branded header — "Prepared by OL-USA · <day> · Updated <time> ET".
  2. Hero KPI strip — 4 tiles (requests / quotes / bookings / awaiting),
     reusing gen_email._kpi_card so mobile stacking is identical.
  3. One-line service narrative (PT-window reply speed + same-business-day
     share when today's quotes carry timestamps — Lonny's desk is Pacific,
     Michael 2026-07-11 "lonny is uswc and we are usec").
  4. Amber "Upcoming cutoffs" callout — doc cutoffs / departures ≤ 7 days out.
  5. Active shipments — confirmed bookings from the last 14 days with
     booking ref, vessel, ETD/ETA, doc cutoff (the table a client checks
     daily), sorted by ETD.
  6. The five daily sections. Empty sections COLLAPSE to one friendly line;
     non-empty sections render full striped tables ("Awaiting your decision"
     is the client's action list and always tables when it has rows).
  7. Contact footer.

SHIPS GATED OFF: config.json `client_report.enabled = false`. While disabled
the pipeline still builds this artifact every fire, and the send step mails a
SAMPLE to Michael only (`--force --no-flag`, never touching idempotency
state). Lonny receives nothing until the operator flips enabled=true.

Produces: reports/client-email-body.html + reports/client-email-subject.txt
Usage:
  python3 scripts/gen_client_email.py
  python3 scripts/gen_client_email.py --data tests/fixtures/golden_day.json --out-dir /tmp/out
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import branding as B  # noqa: E402
import core  # noqa: E402

# Shared helpers from the staff renderer — same escaping (raw strings in,
# single-escape out; QC-044), same Windows-portable strftime mapping (no
# bare %-d/%-I; QC-046), same report-day math, same today-bucketing, and the
# SAME KPI tile builder so the .hx-kpi mobile stacking behaves identically.
from gen_email import (  # noqa: E402
    EMAIL_FONT_STACK,
    EMAIL_TNUM,
    _esc,
    _fmt_date,
    _iso_date,
    _kpi_card,
    _report_date,
    _today_events,
)

# Navy palette used repo-wide. Table headers are a SOLID color only (QC-045:
# Outlook strips linear-gradient; the banner pairs the gradient with a solid
# background-color fallback, listed first so Outlook keeps the solid).
TH_BG = "#1e3a5f"
HEADER_BG_SOLID = B.HILMAR_NAVY
HEADER_GRADIENT = f"linear-gradient(135deg,{B.HILMAR_NAVY} 0%,{B.HILMAR_BLUE} 100%)"

#: Alternating-row stripe — inline bgcolor attribute (the form desktop
#: Outlook's Word engine honors most reliably).
STRIPE_BG = "#f8fafc"

#: "Active shipments" = WIN rows whose request/response date falls within
#: this many days of the report day.
ACTIVE_WINDOW_DAYS = 14
#: Cutoff callout horizon — doc cutoffs / departures within this many days.
CUTOFF_HORIZON_DAYS = 7

_TH_STYLE = (
    f'style="padding:7px 10px;background-color:{TH_BG};background:{TH_BG};'
    'color:#ffffff;font-size:11px;font-weight:600;text-align:left;'
    'white-space:nowrap;'
    f'border-bottom:1px solid {TH_BG}"'
)
_TD_STYLE = (
    'style="padding:7px 10px;font-size:12px;color:#1f2937;'
    'border-bottom:1px solid #e5e7eb"'
)
_TD_FIRST_STYLE = _TD_STYLE.replace('color:#1f2937', 'color:#1f2937;font-weight:600')

# Mobile overrides — same .hx-* conventions as gen_email._header_html so the
# client email renders on phones: .hx-wrap full-bleed, .hx-pad one horizontal
# scroll surface, .hx-data tables keep readable column geometry and scroll
# sideways instead of squishing, and td.hx-kpi stacks FULL-WIDTH via
# display:block (NEVER inline-block/50% — iOS Mail collapses those cells to
# narrow strips; Michael's 2026-07-02 screenshot). Desktop Outlook ignores
# <style> entirely.
MOBILE_STYLE = """
<style>
@media only screen and (max-width:640px) {
  .hx-wrap { width:100% !important; max-width:100% !important; border-radius:0 !important; }
  .hx-pad { padding:14px 8px !important; overflow-x:auto !important; -webkit-overflow-scrolling:touch; }
  td.hx-kpi { display:block !important; width:100% !important; box-sizing:border-box !important; }
  .hx-kpi-card { height:auto !important; min-height:0 !important; }
  table.hx-data { min-width:640px !important; }
}
</style>
"""


# ─────────────────────────────────────────────────────────────────────
# Small formatters (client-safe, Windows-portable)
# ─────────────────────────────────────────────────────────────────────

def _short_tz(iso, tz, label):
    """'Jul 9 2:05 PM PT' — portable strftime (no %-d/%-I; strip the leading
    zeros with .replace, per CLAUDE.md rule #8)."""
    dt = core.parse_iso(iso)
    if not dt:
        return "—"
    s = dt.astimezone(tz).strftime("%b %d %I:%M %p")
    s = s.replace(" 0", " ", 1).replace(" 0", " ", 1)
    return f"{s} {label}"


def _short_pt(iso):
    return _short_tz(iso, core.PT, "PT")


def _short_et(iso):
    return _short_tz(iso, core.ET, "ET")


def _rate(r):
    v = r.get("ol_rate")
    return f"${v:,.0f}" if isinstance(v, (int, float)) else (str(v) if v else "—")


def _lane(r):
    return r.get("lane") or f"{r.get('origin', '?')} → {r.get('destination', '?')}"


def _lane_resolved(r) -> bool:
    """True when the row carries a lane fit to show the CLIENT — the hard
    guarantee for Lonny (2026-07-14, run 29292014093): an unresolvable booking
    is an OL-internal cleanup, surfaced only in the staff audit (QC-015), never
    in the client email. A row is UNRESOLVED (returns False, excluded) when it
    has no displayable lane: destination is a placeholder (None/""/"Unknown",
    case-insensitive) AND there is no real `lane` string — or the lane is the
    literal "Lane unresolved" marker. A genuine lane string ("Oakland → Tokyo")
    is resolved even when the `destination` FIELD is unset, because the lane is
    exactly the value _lane(r) renders."""
    lane = (r.get("lane") or "").strip()
    if lane and lane.lower() != "lane unresolved":
        return True
    dest = (r.get("destination") or "").strip()
    return bool(dest) and dest.lower() != "unknown"


def _teu(r):
    return str(r.get("teu_requested") or 0)


def _teu_sum(rows, won=False):
    """Total TEU across rows — teu_won first for booked rows, defensively int."""
    total = 0
    for r in rows:
        v = (r.get("teu_won") if won else None) or r.get("teu_requested") or 0
        if isinstance(v, (int, float)):
            total += int(v)
    return total


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _day_label(d):
    """'Wed Apr 9' — portable via the shared _fmt_date mapping."""
    return _fmt_date(datetime.combine(d, datetime.min.time()), "%a %b %-d")


# ─────────────────────────────────────────────────────────────────────
# Day bucketing — reuses gen_email._today_events (stand_* rows are already
# excluded from the requests/quotes buckets there; they surface only through
# a →WIN status_history entry, i.e. the bookings section — the honest event).
# ─────────────────────────────────────────────────────────────────────

def _client_sections(data, report_date):
    """Return the five client-facing row buckets for the report day."""
    new_req, ol_resp, status_ch, pending = _today_events(data, report_date)
    quotes = [r for r in ol_resp if r.get("ol_rate")]
    bookings, seen = [], set()
    for r, h in status_ch:
        if h.get("to") == "WIN" and id(r) not in seen:
            seen.add(id(r))
            bookings.append(r)
    awaiting = [r for r in pending if core.pending_substate(r) == "PENDING_HILMAR"]
    in_progress = [r for r in pending if core.pending_substate(r) == "PENDING_OL"]
    # HARD GUARANTEE (2026-07-14): the client sees only resolved shipments.
    # Every bucket is filtered through _lane_resolved so a "Lane unresolved" /
    # placeholder-destination row can never render in any section. If this
    # empties a section, the existing empty-section collapse handles it.
    return {
        "requests": [r for r in new_req if _lane_resolved(r)],
        "quotes": [r for r in quotes if _lane_resolved(r)],
        "bookings": [r for r in bookings if _lane_resolved(r)],
        "awaiting": [r for r in awaiting if _lane_resolved(r)],
        "in_progress": [r for r in in_progress if _lane_resolved(r)],
    }


def _active_shipments(data, report_date):
    """Confirmed bookings (WIN) from the last ACTIVE_WINDOW_DAYS — dated by
    request_date, falling back to response_timestamp; rows with no parseable
    date are skipped (defensive). Sorted by ETD ascending, undated ETDs last.
    stand_* rows are INCLUDED here: a confirmed standalone booking is exactly
    what the client tracks daily."""
    rows = []
    for r in data.get("requests", []):
        if r.get("status") != "WIN":
            continue
        # Never surface an unresolved-lane booking to the client (2026-07-14).
        if not _lane_resolved(r):
            continue
        d = (_iso_date(r.get("request_date") or r.get("request_timestamp"))
             or _iso_date(r.get("response_timestamp")))
        if not d or (report_date - d).days > ACTIVE_WINDOW_DAYS:
            continue
        rows.append(r)
    rows.sort(key=lambda r: (_iso_date(r.get("etd_offered")) is None,
                             _iso_date(r.get("etd_offered")) or date.max))
    return rows


def _upcoming_cutoffs(active, report_date):
    """'Lane — doc cutoff Wed Apr 9' lines for active shipments whose doc
    cutoff (or, lacking one, vessel departure) lands within the next
    CUTOFF_HORIZON_DAYS. Dates parsed defensively; unparseable rows skipped."""
    horizon = report_date + timedelta(days=CUTOFF_HORIZON_DAYS)
    items = []
    for r in active:
        doc = _iso_date(r.get("doc_cutoff"))
        etd = _iso_date(r.get("etd_offered"))
        if doc and report_date <= doc <= horizon:
            items.append((doc, f"{_lane(r)} — doc cutoff {_day_label(doc)}"))
        elif etd and report_date <= etd <= horizon:
            items.append((etd, f"{_lane(r)} — vessel departs {_day_label(etd)}"))
    items.sort(key=lambda t: t[0])
    return [text for _, text in items]


# ─────────────────────────────────────────────────────────────────────
# HTML builders
# ─────────────────────────────────────────────────────────────────────

def _table(headers, rows):
    """Outlook-safe table. Every cell value is escaped HERE (pass RAW strings —
    QC-044). Even rows get an inline bgcolor stripe. Empty → a stable friendly
    one-liner (defensive; build_body collapses empty sections before this)."""
    head = "".join(f"<th {_TH_STYLE}>{_esc(h)}</th>" for h in headers)
    if rows:
        body = ""
        for n, row in enumerate(rows):
            stripe = f' bgcolor="{STRIPE_BG}"' if n % 2 else ""
            cells = "".join(
                f"<td {_TD_FIRST_STYLE if i == 0 else _TD_STYLE}>{_esc(v)}</td>"
                for i, v in enumerate(row)
            )
            body += f"<tr{stripe}>{cells}</tr>"
    else:
        body = (
            f'<tr><td colspan="{len(headers)}" '
            f'{_TD_STYLE.replace("text-align:left", "text-align:center")}>'
            f'<em style="color:#64748b">None today.</em></td></tr>'
        )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" class="hx-data" '
        'style="width:100%;border-collapse:collapse;font-size:12px;'
        'margin:6px 0 18px 0;border:1px solid #d1d5db">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def _section(title, count, note, table_html):
    return (
        f'<h2 style="margin:18px 0 2px;color:{TH_BG};font-size:15px;'
        f'font-weight:700;letter-spacing:-0.01em">{_esc(title)} ({count})</h2>'
        f'<p style="margin:0 0 6px;font-size:11px;color:#64748b">{_esc(note)}</p>'
        f"{table_html}"
    )


def _quiet_section(title, text):
    """A zero-row section collapses to ONE composed line instead of an empty
    table — bolded section name, muted friendly copy, subtle left rule."""
    return (
        f'<p style="margin:12px 0 0;padding:8px 12px;font-size:12px;color:#475569;'
        f'background:#f8fafc;border-left:3px solid #cbd5e1;border-radius:0 4px 4px 0">'
        f'<strong style="color:{TH_BG}">{_esc(title)}:</strong> {_esc(text)}</p>'
    )


def _section_or_line(title, note, quiet_text, headers, row_values):
    if row_values:
        return _section(title, len(row_values), note, _table(headers, row_values))
    return _quiet_section(title, quiet_text)


def _kpi_strip(s):
    """Hero KPI strip — 4 tiles via gen_email._kpi_card (same td.hx-kpi /
    .hx-kpi-card markup, so the mobile display:block stacking is shared).
    Deliberately NOT class hx-data — a min-width would fight the stacking."""
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;margin:2px 0 8px">
  <tr>
    {_kpi_card(len(s["requests"]), "Requests received today", "#3b82f6", "25%", sublabel=f"{_teu_sum(s['requests'])} TEU")}
    {_kpi_card(len(s["quotes"]), "Quotes delivered today", "#6366f1", "25%", sublabel=f"{_teu_sum(s['quotes'])} TEU")}
    {_kpi_card(len(s["bookings"]), "Bookings confirmed today", "#22c55e", "25%", sublabel=f"{_teu_sum(s['bookings'], won=True)} TEU")}
    {_kpi_card(len(s["awaiting"]), "Awaiting your decision", "#f59e0b", "25%", sublabel=f"{_teu_sum(s['awaiting'])} TEU")}
  </tr>
</table>
"""


def _pt_reply_stats(quotes):
    """(avg_pt_biz_hours, same_day_count, n) across today's quoted rows,
    computed request_timestamp→response_timestamp in the PACIFIC business
    window (core.biz_hours_between_pt). 2026-07-12 fix (Michael 2026-07-11
    "lonny is uswc and we are usec"): Lonny's desk is West-coast, so the
    client narrative reflects HIS experienced wait — NOT the stored
    turnaround_biz_hours, which is the ET staff-desk SLA and stays
    untouched everywhere else. A quote counts same-day when request and
    response fall on the same America/Los_Angeles calendar day. Returns
    None when no quote carries both timestamps (narrative omits the
    parenthetical rather than guessing)."""
    hours, same_day = [], 0
    for r in quotes:
        req = core.parse_iso(r.get("request_timestamp"))
        resp = core.parse_iso(r.get("response_timestamp"))
        if not req or not resp:
            continue
        h = core.biz_hours_between_pt(req, resp)
        if h is None:
            continue
        hours.append(h)
        if req.astimezone(core.PT).date() == resp.astimezone(core.PT).date():
            same_day += 1
    if not hours:
        return None
    return sum(hours) / len(hours), same_day, len(hours)


def _narrative(s):
    """One-line service narrative under the hero tiles. The reply-speed
    clause is the Pacific-window metric from _pt_reply_stats (see there);
    it is omitted rather than guessed when today's quotes carry no usable
    request/response timestamps."""
    n_req, n_quo, n_book = len(s["requests"]), len(s["quotes"]), len(s["bookings"])
    if not (n_req or n_quo or n_book):
        return ("A quiet day on new activity — no new requests, quotes, or "
                "bookings. Your active shipments are summarized below.")
    stats = _pt_reply_stats(s["quotes"])
    if stats:
        avg_h, same_day, n = stats
        share = "all" if same_day == n else f"{same_day} of {n}"
        speed = (f" — {share} the same business day "
                 f"(average {avg_h:.1f} business hours, Pacific)")
    else:
        speed = ""
    line = (f"We received {_plural(n_req, 'rate request')} and returned "
            f"{_plural(n_quo, 'quote')}{speed}; "
            f"{_plural(n_book, 'booking')} confirmed.")
    n_wait = len(s["awaiting"])
    if n_wait:
        verb = "awaits" if n_wait == 1 else "await"
        line += f" {_plural(n_wait, 'quote')} {verb} your decision below."
    return line


def _cutoff_callout(items):
    """Amber highlight box — the time-sensitive dates a client must not miss."""
    lines = "".join(
        f'<p style="margin:3px 0 0;font-size:12px;font-weight:600;color:#78350f">'
        f'{_esc(t)}</p>'
        for t in items
    )
    return (
        '<div style="background:#fffbeb;border:1px solid #fcd34d;'
        'border-left:4px solid #f59e0b;border-radius:6px;'
        'padding:10px 14px;margin:12px 0 6px">'
        '<p style="margin:0;font-size:12px;font-weight:700;color:#92400e;'
        'letter-spacing:0.01em">Upcoming cutoffs — next 7 days</p>'
        f"{lines}"
        '</div>'
    )


def _header_html(report_label, updated_label):
    # CID logo — Outlook blocks data: URIs in email bodies (QC-042);
    # outlook_send attaches the PNG with contentId=hilmar-logo + isInline.
    logo_html = B.logo_html_cid(height=56, alt="Hilmar Ingredients")
    logo_block = (
        f'<div style="background:white;padding:2px 6px;border-radius:4px;'
        f'display:inline-block;margin-bottom:6px">{logo_html}</div>'
        if logo_html else ""
    )
    return f"""
{MOBILE_STYLE}
<div class="hx-wrap" style="max-width:900px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;font-family:{EMAIL_FONT_STACK};{EMAIL_TNUM}">
  <div style="padding:16px 28px;background-color:{HEADER_BG_SOLID};background:{HEADER_GRADIENT};color:white;font-family:{EMAIL_FONT_STACK}">
    {logo_block}
    <h1 style="margin:0;font-size:21px;font-weight:700;letter-spacing:-0.3px;font-family:{EMAIL_FONT_STACK}">Daily Shipment Update — Hilmar Ingredients</h1>
    <p style="margin:4px 0 0;font-size:13px;opacity:0.9;font-family:{EMAIL_FONT_STACK}">Prepared by OL-USA · {_esc(report_label)} · Updated {_esc(updated_label)}</p>
  </div>
  <div class="hx-pad" style="padding:20px 28px;font-family:{EMAIL_FONT_STACK};{EMAIL_TNUM}">
"""


FOOTER_HTML = """
<div style="border-top:2px solid #e5e7eb;padding-top:14px;margin-top:24px">
  <p style="margin:0 0 4px;font-size:12px;color:#374151">Questions about a specific shipment? Reply with the booking reference and our team will follow up.</p>
  <p style="margin:0 0 4px;font-size:12px;color:#374151">For anything else, reply to this email or contact <a href="mailto:MBD_OceanExportBookingShared@ol-usa.com" style="color:#1e3a5f">MBD_OceanExportBookingShared@ol-usa.com</a>.</p>
  <p style="margin:0;font-size:11px;color:#6b7280">This daily update is generated automatically by OL-USA for Hilmar Ingredients.</p>
</div>
"""


def _now_et(now=None):
    """The single ET "now" behind a client-email render.

    2026-07-12 root fix (run 29174327034 — the Friday-evening fire whose
    sample subject carried the wrong report day): build_subject and
    build_body each read the wall clock SEPARATELY and neither accepted an
    injected instant, so the subject's report-day derivation could drift
    from the body's and could not be pinned by tests (a UTC-vs-ET shift
    around midnight was unverifiable). Both entry points now derive from
    ONE aware instant (any tz; tests inject UTC) converted to ET, then go
    through gen_email._report_date → core.report_business_day — the exact
    staff-email path (wee-hours rollback + weekend→Friday rules), never a
    naive/UTC date and never a data-derived date."""
    return (now or datetime.now(timezone.utc)).astimezone(core.ET)


def build_subject(data, cfg, now=None):
    report = _report_date(_now_et(now))
    label = _fmt_date(datetime.combine(report, datetime.min.time()), "%b %-d, %Y")
    return f"OL-USA — Daily Shipment Update for Hilmar Ingredients ({label})"


def build_body(data, cfg, now=None):
    now_et = _now_et(now)
    report_date = _report_date(now_et)
    report_label = _fmt_date(
        datetime.combine(report_date, datetime.min.time()), "%A, %B %-d, %Y")
    updated_label = _fmt_date(now_et, "%-I:%M %p") + " ET"
    s = _client_sections(data, report_date)
    active = _active_shipments(data, report_date)
    cutoffs = _upcoming_cutoffs(active, report_date)

    html = _header_html(report_label, updated_label)

    # 1. Hero KPI strip + one-line service narrative.
    html += _kpi_strip(s)
    html += (
        f'<p style="margin:6px 2px 14px;font-size:13px;color:#374151;'
        f'line-height:1.5">{_esc(_narrative(s))}</p>'
    )

    # 2. Time-sensitive dates first — amber callout, only when there are any.
    if cutoffs:
        html += _cutoff_callout(cutoffs)

    # 3. Active shipments — confirmed bookings from the last 14 days.
    html += _section_or_line(
        "Active shipments",
        "Your confirmed bookings from the last 14 days — booking references, "
        "vessel details, and cutoff dates, sorted by departure.",
        "No shipments currently in transit or awaiting departure.",
        ["Lane", "Carrier", "Booking ref", "Vessel", "ETD", "ETA", "Doc cutoff"],
        [[
            _lane(r),
            r.get("carrier_won") or r.get("carrier_quoted") or "—",
            r.get("mdolx_ref") or "Confirmation to follow",
            r.get("vessel_voyage") or "—",
            r.get("etd_offered") or "—",
            r.get("eta_offered") or "—",
            r.get("doc_cutoff") or "—",
        ] for r in active],
    )

    # 4. Requests received today — acknowledgment of TODAY's RFQs.
    html += _section_or_line(
        "Requests received today",
        "Rate requests we received from your team today — all acknowledged and being worked.",
        "No new rate requests today.",
        ["Lane", "Equipment", "TEU", "Product", "Temp", "Requested ETA", "Received (PT)"],
        [[
            _lane(r),
            r.get("containers") or "—",
            _teu(r),
            r.get("product") or "—",
            r.get("temperature") or "—",
            r.get("eta_requested") or r.get("etd_requested") or "—",
            r.get("lonny_time_pt") or _short_pt(r.get("request_timestamp")),
        ] for r in s["requests"]],
    )

    # 5. Quotes provided today — TODAY's rate responses.
    html += _section_or_line(
        "Quotes provided today",
        "Rates our booking team sent you today.",
        "No new quotes today.",
        ["Lane", "Equipment", "TEU", "Carrier", "Rate ($/container)",
         "Quoted at (ET)", "ETD offered", "ETA offered"],
        [[
            _lane(r),
            r.get("containers") or "—",
            _teu(r),
            r.get("carrier_quoted") or "—",
            _rate(r),
            _short_et(r.get("response_timestamp")),
            r.get("etd_offered") or "—",
            r.get("eta_offered") or "—",
        ] for r in s["quotes"]],
    )

    # 6. Bookings confirmed today — →WIN transitions on the report day.
    html += _section_or_line(
        "Bookings confirmed today",
        "Shipments confirmed today. Booking references are listed once carrier confirmation arrives.",
        "No new bookings confirmed today.",
        ["Lane", "Equipment / TEU", "Carrier", "Booking ref", "ETD", "ETA"],
        [[
            _lane(r),
            f"{r.get('containers') or '—'} / {_teu(r)} TEU",
            r.get("carrier_won") or r.get("carrier_quoted") or "—",
            r.get("mdolx_ref") or "Confirmation to follow",
            r.get("etd_offered") or "—",
            r.get("eta_offered") or "—",
        ] for r in s["bookings"]],
    )

    # 7. Awaiting your decision — quotes on the table (OL quoted, no booking
    # yet). This is the client's ACTION LIST — always a table when it has rows.
    html += _section_or_line(
        "Awaiting your decision",
        "Quotes on the table — reply to book, or let us know if the dates or rates need adjusting.",
        "Nothing awaiting your decision — all caught up.",
        ["Lane", "Carrier", "Rate ($/container)", "Quoted at (ET)", "ETD offered"],
        [[
            _lane(r),
            r.get("carrier_quoted") or "—",
            _rate(r),
            _short_et(r.get("response_timestamp")),
            r.get("etd_offered") or "—",
        ] for r in s["awaiting"]],
    )

    # 8. In progress — requests we're still pricing.
    html += _section_or_line(
        "In progress — quote coming",
        "We're working on these and will have rates to you shortly.",
        "Nothing in the pricing queue — every request has been quoted.",
        ["Lane", "Equipment", "TEU", "Received (PT)"],
        [[
            _lane(r),
            r.get("containers") or "—",
            _teu(r),
            r.get("lonny_time_pt") or _short_pt(r.get("request_timestamp")),
        ] for r in s["in_progress"]],
    )

    html += FOOTER_HTML
    html += "</div></div>"
    return html


# ─────────────────────────────────────────────────────────────────────
# Entry point — mirrors gen_email.main()'s config/path resolution, with
# --data/--out-dir overrides so tests + smoke runs work without production
# data (e.g. against tests/fixtures/golden_day.json).
# ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--data", default=None,
                    help="Override the tracking-data path (default: config paths.data)")
    ap.add_argument("--out-dir", default=None,
                    help="Override the output directory (default: the reports/ dir "
                         "holding config paths.email_body)")
    args = ap.parse_args(argv)
    cfg = core.load_config(args.config)
    data_path = Path(args.data) if args.data else Path(cfg["paths"]["data"])
    data = json.loads(data_path.read_text(encoding="utf-8"))
    # ONE instant for both artifacts — a render that straddles midnight ET
    # must never date the subject and the body differently (2026-07-12).
    now = datetime.now(timezone.utc)
    body = build_body(data, cfg, now=now)
    subject = build_subject(data, cfg, now=now)
    out_dir = Path(args.out_dir) if args.out_dir else Path(cfg["paths"]["email_body"]).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    body_path = out_dir / "client-email-body.html"
    subject_path = out_dir / "client-email-subject.txt"
    body_path.write_text(body, encoding="utf-8")
    subject_path.write_text(subject, encoding="utf-8")
    print(f"✅ Client email body: {len(body):,} bytes -> {body_path}")
    print(f"✅ Client email subject: {subject!r} -> {subject_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
