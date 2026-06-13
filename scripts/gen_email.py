"""
gen_email.py — Tasklet-style HTML email body for daily Hilmar distro.

Replicates the reference format: header gradient, "What Happened Today" blue box,
8-card KPI grid, Week-over-Week table, Carrier Performance, Top Winning Lanes,
Top Losing Lanes, Not Quoted list, Pending Hilmar Response list, and Attached
Files guide.

Produces: reports/email-body.html + reports/email-subject.txt
Usage:
  python3 scripts/gen_email.py
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import branding as B  # noqa: E402  Hilmar logo + brand colors
import core  # noqa: E402
import viz as V  # noqa: E402  shared visual helpers (sparklines, pills, bars, heatmaps)


def _esc(v):
    if v is None:
        return ""
    return html.escape(str(v))


def _fmt_date(dt, fmt: str) -> str:
    """Cross-platform strftime that supports '%-d' / '%-I' (Linux/macOS) by
    transparently mapping to '%#d' / '%#I' on Windows (which is what cpython's
    msvcrt strftime expects)."""
    if dt is None:
        return ""
    import sys as _sys
    if _sys.platform == "win32":
        fmt = fmt.replace("%-d", "%#d").replace("%-I", "%#I").replace("%-m", "%#m").replace("%-H", "%#H")
    return dt.strftime(fmt)


def _pct(n):
    return f"{n:.1f}%" if isinstance(n, (int, float)) else "—"


def _fmt_int(v):
    try:
        return f"{int(v):,}"
    except Exception:
        return "—"


def _iso_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _week_bucket(d):
    """Return ISO-ish week key like 'W15 (Apr 6–10)' — Monday through Friday.

    Weekend activity (rare, after-hours) folds into the prior Mon–Fri week
    via d.weekday() arithmetic: Saturday weekday=5 → monday is 5 days back.
    Label intentionally shows Mon–Fri only (not Mon–Sun) per Michael
    2026-05-07: 'the dating on the weekly should be based on weekdays'.

    2026-05-19 PM 3rd pass (Michael "how do we fix this formatting of the
    week" — date column was wrapping at the en-dash in narrow viewports
    even with white-space:nowrap because the column still couldn't fit
    "W18 (Apr 27–May 1)" as one line at narrow widths). Label now uses
    a literal "\n" separator between the week code and the date range,
    and the renderer (_week_block_html) substitutes "\n" with a
    "<br><span ...>" so the date range goes on its OWN line in muted
    grey — same vertical-stack pattern used by the "Requests (# · TEU)"
    cell on the same row. Eliminates unpredictable wrap on narrow
    screens and matches the established visual rhythm.
    """
    if not d:
        return None
    iso = d.isocalendar()
    wk = iso.week
    # Week starts Monday, ends Friday for label purposes
    monday = d - timedelta(days=d.weekday())
    friday = monday + timedelta(days=4)
    if monday.month != friday.month:
        date_range = f"{_fmt_date(monday, '%b %-d')} – {_fmt_date(friday, '%b %-d')}"
    else:
        date_range = f"{_fmt_date(monday, '%b %-d')} – {_fmt_date(friday, '%-d')}"
    # Use literal "\n" separator — _week_block_html splits and re-renders
    # as a 2-line cell. Stays a single string so existing dict keys + sort
    # behavior are unchanged.
    label = f"W{wk}\n{date_range}"
    return label, monday


def _report_date(now_et=None):
    """Return the date this email REPORTS ON — the most recent COMPLETE
    business day (Mon–Fri) before `now_et`.

    Why: the pipeline runs at 10 AM ET each weekday morning. At that time
    today's business day has barely begun and Lonny (in California, PT) is
    still asleep — Hilmar HQ doesn't open for ~3 more hours. So the email
    reports on yesterday's activity, not today's empty window.
    Per Michael 2026-05-07: 'there should be a yesterday kpi run as we
    send this in the morning, there would be absolutely no new data for
    today since lonny is in california and doens't open for hours'.

    Logic (today.weekday(): Mon=0..Sun=6):
      Tue–Fri (1..4): report = today − 1 day  (yesterday)
      Mon (0):        report = today − 3 days (last Friday)
      Sat (5):        report = today − 1 day  (Friday)
      Sun (6):        report = today − 2 days (Friday)
    """
    if now_et is None:
        now_et = datetime.now(timezone.utc).astimezone(core.ET)
    today = now_et.date()
    wd = today.weekday()
    if wd == 0:
        delta = 3
    elif wd == 5:
        delta = 1
    elif wd == 6:
        delta = 2
    else:
        delta = 1
    return today - timedelta(days=delta)


def _report_label(report_date):
    """Human-readable label e.g. 'Wednesday May 6, 2026'."""
    return _fmt_date(datetime.combine(report_date, datetime.min.time()), "%A %B %-d, %Y")


def build_subject(data, cfg):
    report = _report_date()
    label = _fmt_date(datetime.combine(report, datetime.min.time()), "%b %-d, %Y")
    return f"Hilmar Ingredients — Daily Shipment Tracker Update ({label})"


# ─────────────────────────────────────────────────────────────────────
# "What Happened Today" computation
# ─────────────────────────────────────────────────────────────────────

def _today_events(data, today_date):
    """Buckets the activity for the 'What Happened Today' block."""
    new_requests = []
    ol_responses = []
    status_changes = []
    pending_today = []

    for r in data.get("requests", []):
        req_d = _iso_date(r.get("request_date") or r.get("request_timestamp"))
        resp_d = _iso_date(r.get("response_timestamp"))
        if req_d == today_date:
            new_requests.append(r)
        if resp_d == today_date:
            ol_responses.append(r)
        # status changes today
        for h in (r.get("status_history") or []):
            at = h.get("at")
            if at and _iso_date(at) == today_date and h.get("from") and h.get("to") and h["from"] != h["to"]:
                status_changes.append((r, h))
        if r.get("status") == "PENDING":
            pending_today.append(r)

    return new_requests, ol_responses, status_changes, pending_today


def _lonny_line(r):
    lane = r.get("lane") or f"{r.get('origin','?')} → {r.get('destination','?')}"
    cont = r.get("containers") or "—"
    teu = r.get("teu_requested") or "—"
    time_et = r.get("olusa_time_et") or (r.get("request_timestamp") and _fmt_et_time(r.get("request_timestamp"))) or "—"
    return f"• {_esc(lane)} | {_esc(cont)} | {_esc(teu)} TEU | {_esc(time_et)}"


def _response_line(r):
    lane = r.get("lane") or f"{r.get('origin','?')} → {r.get('destination','?')}"
    carrier = r.get("carrier_quoted") or "—"
    rate = r.get("ol_rate") or "—"
    time_et = r.get("olusa_time_et") or "—"
    tb = r.get("turnaround_biz_hours")
    tb_str = f"⏱ {tb:.1f}h" if isinstance(tb, (int, float)) else ""
    if r.get("after_hours_request"):
        tb_str = f"⏱ {tb:.1f}h (after hours)" if isinstance(tb, (int, float)) else "⏱ after hours"
    return f"• {_esc(lane)} | {_esc(carrier)} | {_esc(rate)} | {_esc(time_et)} | {_esc(tb_str)}"


def _fmt_et_time(iso_ts):
    dt = core.parse_iso(iso_ts)
    if not dt:
        return "—"
    return _fmt_date(dt.astimezone(core.ET), "%-I:%M %p ET")


# ─────────────────────────────────────────────────────────────────────
# Weekly aggregation
# ─────────────────────────────────────────────────────────────────────

def _week_rows(data):
    # 2026-05-19 PM 2nd pass: accumulate teu_req + teu_won per week so the
    # "Requests (# · TEU)" + "Won (# · TEU)" cells in _week_block_html can
    # show both the count AND the volume the count represents.
    buckets = defaultdict(lambda: {
        "requests": 0, "won": 0, "ql": 0, "nq": 0, "pending": 0,
        "mdolx": [], "teu_req": 0, "teu_won": 0,
    })
    monday_by_label = {}
    for r in data.get("requests", []):
        d = _iso_date(r.get("request_date") or r.get("request_timestamp"))
        if not d:
            continue
        wk = _week_bucket(d)
        if not wk:
            continue
        label, monday = wk
        monday_by_label[label] = monday
        b = buckets[label]
        b["requests"] += 1
        b["teu_req"] += int(r.get("teu_requested") or 0)
        st = r.get("status") or ""
        lr = r.get("loss_reason") or ""
        if st == "WIN":
            b["won"] += 1
            b["teu_won"] += int(r.get("teu_won") or r.get("teu_requested") or 0)
            mref = r.get("mdolx_ref")
            if mref:
                b["mdolx"].append(mref)
        elif st == "PENDING":
            b["pending"] += 1
        elif st == "LOSS" and lr == "NO_RESPONSE":
            b["nq"] += 1
        elif st == "LOSS":
            b["ql"] += 1

    rows = sorted(buckets.items(), key=lambda kv: monday_by_label[kv[0]])
    return rows


def _carrier_rows(data):
    # 2026-05-19 PM (Michael "carrier performance should have totals teu
    # offered"): also accumulate teu_pending so the "TEU Offered" total in
    # _carrier_block_html (= Won + Lost + Pending) reconciles correctly.
    carriers = defaultdict(lambda: {
        "quoted": 0, "won": 0, "ql": 0, "pending": 0,
        "teu_won": 0, "teu_lost": 0, "teu_pending": 0,
    })
    for r in data.get("requests", []):
        c = r.get("carrier_quoted") or r.get("carrier_won")
        if not c:
            continue
        st = r.get("status") or ""
        teu = r.get("teu_requested") or 0
        b = carriers[c]
        if r.get("quoted"):
            b["quoted"] += 1
        if st == "WIN":
            b["won"] += 1
            b["teu_won"] += (r.get("teu_won") or teu or 0)
        elif st == "PENDING":
            b["pending"] += 1
            b["teu_pending"] += teu
        elif st == "LOSS" and (r.get("loss_reason") or "") != "NO_RESPONSE":
            b["ql"] += 1
            b["teu_lost"] += teu
    rows = []
    for name, b in carriers.items():
        wr = (b["won"] / b["quoted"] * 100) if b["quoted"] else 0.0
        rows.append((name, b, wr))
    rows.sort(key=lambda x: (-x[1]["quoted"], x[0]))
    return rows


def _build_lane_buckets(data):
    """Shared per-lane aggregation. 2026-05-19 PM: every lane bucket now
    carries the full status mix (won / ql / nq / pending) so the Winning
    and Losing tables can both show the per-status numbers behind the
    rollup. Also captures `carriers` (set of winners) so Losing Lanes
    can display "who beat us"."""
    lanes = defaultdict(lambda: {
        "won": 0, "ql": 0, "nq": 0, "lost": 0, "pending": 0,
        "total": 0, "teu_won": 0, "teu_lost": 0, "carriers": set(),
    })
    for r in data.get("requests", []):
        lane = r.get("lane") or f"{r.get('origin','?')} → {r.get('destination','?')}"
        b = lanes[lane]
        b["total"] += 1
        st = r.get("status")
        if st == "WIN":
            b["won"] += 1
            b["teu_won"] += (r.get("teu_won") or r.get("teu_requested") or 0)
            if r.get("carrier_won"):
                b["carriers"].add(r["carrier_won"])
        elif st == "PENDING":
            b["pending"] += 1
        elif st == "LOSS":
            reason = (r.get("loss_reason") or "")
            if reason == "NO_RESPONSE":
                b["nq"] += 1
            else:
                b["ql"] += 1
                b["lost"] += 1   # back-compat alias used by old callers
                b["teu_lost"] += (r.get("teu_requested") or 0)
    return lanes


def _losing_lane_rows(data):
    lanes = _build_lane_buckets(data)
    rows = [(lane, b) for lane, b in lanes.items() if b["lost"] > 0]
    rows.sort(key=lambda kv: -kv[1]["teu_lost"])
    return rows[:10]


def _winning_lane_rows(data):
    lanes = _build_lane_buckets(data)
    rows = [(lane, b) for lane, b in lanes.items() if b["won"] > 0]
    rows.sort(key=lambda kv: (-kv[1]["teu_won"], -kv[1]["won"]))
    return rows[:10]


NQ_DISPLAY_WINDOW_DAYS = 14  # Hide stale NQ rows from the listing (still counted in aggregates)


def _not_quoted_rows(data, cutoff_days: int = NQ_DISPLAY_WINDOW_DAYS):
    """Return ALL Not-Quoted rows, filtered to the recent display window.

    DISPLAY-ONLY filter — older NQ rows STILL count in summary totals,
    lane TEU tallies, and trade-region aggregates. They just don't show
    up in the per-row listing in the daily email since Lonny isn't going
    to reply 2+ weeks later and the noise crowds out actionable rows.

    Per Michael 2026-05-13: 'after 2 weeks with items that have no reply..
    just remove them from system that says not quoted but keep it on the
    talley of volumes that hilmar moves for rate negotiation'.

    `cutoff_days=None` returns all rows (used by aggregates / QC).
    """
    from datetime import datetime, timedelta, timezone
    cutoff_iso = None
    if cutoff_days is not None:
        cutoff_iso = (datetime.now(timezone.utc).date() - timedelta(days=cutoff_days)).isoformat()
    rows = []
    for r in data.get("requests", []):
        if r.get("status") == "LOSS" and (r.get("loss_reason") or "") == "NO_RESPONSE":
            if cutoff_iso is not None:
                req_date = r.get("request_date") or r.get("date") or ""
                if req_date < cutoff_iso:
                    continue
            rows.append(r)
    rows.sort(key=lambda r: (r.get("request_date") or ""))
    return rows


def _not_quoted_aggregate(data):
    """ALL Not-Quoted rows regardless of age — for tally / TEU / lane stats
    that should reflect total Hilmar volume for rate negotiation depth.
    """
    return [r for r in data.get("requests", [])
            if r.get("status") == "LOSS" and (r.get("loss_reason") or "") == "NO_RESPONSE"]


def _pending_rows(data):
    rows = [r for r in data.get("requests", []) if r.get("status") == "PENDING"]
    rows.sort(key=lambda r: (r.get("request_date") or ""))
    return rows


# ─────────────────────────────────────────────────────────────────────
# HTML builders
# ─────────────────────────────────────────────────────────────────────

# 2026-05-19 PM 4th pass: Outlook strips CSS linear-gradient — leaves text
# rendered without the background, which collapses the visual hierarchy
# (and on header rows in tables, makes white text invisible on white
# background). Keep the gradient string for clients that support it but
# pair every use with a solid-color background-color fallback. Inline
# styles ALWAYS list `background-color:` before `background:` so Outlook
# sees the solid color when it strips the gradient line.
HEADER_GRADIENT = f"linear-gradient(135deg,{B.HILMAR_NAVY} 0%,{B.HILMAR_BLUE} 100%)"
HEADER_BG_SOLID = B.HILMAR_NAVY  # solid fallback for Outlook


EMAIL_FONT_STACK = "'Inter','Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif"
EMAIL_TNUM = "font-variant-numeric:tabular-nums;font-feature-settings:'tnum' 1"

def _header_html(today_label, range_label, updated_label):
    # Use the CID variant — Outlook blocks data: URIs in HTML email bodies
    # but renders inline CID attachments reliably. outlook_send.py attaches
    # the logo PNG with contentId=hilmar-logo + isInline=true so this
    # <img src="cid:hilmar-logo"> reference resolves at delivery time.
    # Per Michael 2026-05-17 ("hilmar logo not showing up").
    # Sizing per Michael 2026-05-17 ("make the logo bigger.. it has too
    # much white space around it.. then also reduce white space"):
    # logo height 42 → 72, container padding 8/12 → 2/6, margins tightened.
    logo_html = B.logo_html_cid(height=72, alt="Hilmar Ingredients")
    logo_block = (
        f'<div style="background:white;padding:2px 6px;border-radius:4px;display:inline-block;margin-bottom:4px">{logo_html}</div>'
        if logo_html else ""
    )
    return f"""
<!--[if !mso]><!-->
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<!--<![endif]-->
<div style="max-width:900px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;font-family:{EMAIL_FONT_STACK};{EMAIL_TNUM}">
  <div style="padding:14px 28px;background-color:{HEADER_BG_SOLID};background:{HEADER_GRADIENT};color:white;font-family:{EMAIL_FONT_STACK}">
    {logo_block}
    <h1 style="margin:0;font-size:22px;font-weight:700;letter-spacing:-0.3px;font-family:{EMAIL_FONT_STACK}">{'' if logo_html else '🚢 '}Hilmar Ingredients — Daily Shipment Tracker</h1>
    <p style="margin:4px 0 0;font-size:14px;opacity:0.9;font-family:{EMAIL_FONT_STACK}">{_esc(range_label)} | Updated: {_esc(updated_label)}</p>
  </div>
  <div style="padding:20px 28px;font-family:{EMAIL_FONT_STACK};{EMAIL_TNUM}">
"""


def _today_block_html(report_label, new_req, ol_resp, status_ch, pending):
    """Render the 'What Happened on <day>' block. The label `report_label` is
    the previous business day (see _report_date) — not literal 'today' —
    because at 10 AM ET fire time, today's data window is still empty
    (Lonny's PT office isn't open yet).

    2026-05-19 PM (Michael "the current report is a mess. formatted poorly...
    the email should have clear tables with proper formatting and names of
    whom responded"): replaced bullet-list rendering with proper HTML tables.
    Every subsection has explicit column headers; OL-USA responses surface
    `ol_responder_signer` (the human at MBD who composed the rate response).
    """
    # Shared table CSS — Outlook-safe inline styles, no flexbox or grid.
    _TABLE_OPEN = (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="width:100%;border-collapse:collapse;font-size:12px;'
        'margin:4px 0 14px 0;border:1px solid #d1d5db">'
    )
    _TH_STYLE = (
        'style="padding:6px 8px;background:#0a2350;color:#ffffff;'
        'font-size:11px;font-weight:600;text-align:left;'
        'border-bottom:1px solid #0a2350"'
    )
    _TD_STYLE = (
        'style="padding:5px 8px;font-size:12px;color:#1f2937;'
        'border-bottom:1px solid #e5e7eb"'
    )
    _EMPTY_ROW = (
        f'<tr><td colspan="99" {_TD_STYLE.replace("text-align:left", "text-align:center")}>'
        f'<em style="color:#64748b">No activity</em></td></tr>'
    )

    # 2026-05-19 PM (Michael "the timing should also be on the cover email.
    # lots of data missing"): cover-email tables fully bulked. New Requests
    # gets product/temp/requested ETA; OL Responses gets equipment/TEU +
    # full timing (Lonny sent PT → OL quoted ET → Time to Quote biz-hrs)
    # + ETD/ETA offered; Pending gets equipment/TEU/timing trio. Status
    # Changes gets carrier + rate. Every row tells the whole story now.

    def _fmt_teu(r):
        return f"{r.get('teu_requested') or 0}"

    def _fmt_time_et(iso):
        try:
            dt = core.parse_iso(iso)
            return dt.astimezone(core.ET).strftime("%I:%M %p ET").lstrip("0") if dt else "—"
        except Exception:
            return "—"

    def _fmt_short_pt(iso):
        try:
            dt = core.parse_iso(iso)
            if not dt:
                return "—"
            s = dt.astimezone(core.PT).strftime("%b %d %I:%M %p")
            s = s.replace(" 0", " ", 1).replace(" 0", " ", 1)
            return s + " PT"
        except Exception:
            return "—"

    def _fmt_short_et(iso):
        try:
            dt = core.parse_iso(iso)
            if not dt:
                return "—"
            s = dt.astimezone(core.ET).strftime("%b %d %I:%M %p")
            s = s.replace(" 0", " ", 1).replace(" 0", " ", 1)
            return s + " ET"
        except Exception:
            return "—"

    def _ttq_cell(r):
        """Time to Quote — biz hours from Lonny RFQ to OL response. Color
        coded green ≤4h, amber 4–24h, red >24h."""
        tb = r.get("turnaround_biz_hours")
        if isinstance(tb, (int, float)):
            color = "#16a34a" if tb <= 4 else ("#d97706" if tb <= 24 else "#dc2626")
            suffix = " (AH)" if r.get("after_hours_request") else ""
            return f"{tb:.1f}h{suffix}", color
        return "—", "#64748b"

    # ── 1. NEW REQUESTS FROM LONNY (6 columns) ───────────────────────
    new_rows = ""
    if new_req:
        for r in new_req:
            lane = r.get("lane") or f"{r.get('origin','?')} → {r.get('destination','?')}"
            cont = r.get("containers") or "—"
            teu = _fmt_teu(r)
            product = r.get("product") or "—"
            temp = r.get("temperature") or "—"
            time_pt = r.get("lonny_time_pt") or _fmt_short_pt(r.get("request_timestamp"))
            requested_eta = r.get("eta_requested") or r.get("etd_requested") or "—"
            new_rows += (
                f'<tr><td {_TD_STYLE}><strong>{_esc(lane)}</strong></td>'
                f'<td {_TD_STYLE}>{_esc(cont)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")}>{_esc(teu)}</td>'
                f'<td {_TD_STYLE}>{_esc(product)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")}>{_esc(temp)}</td>'
                f'<td {_TD_STYLE}>{_esc(requested_eta)}</td>'
                f'<td {_TD_STYLE};white-space:nowrap>{_esc(time_pt)}</td></tr>'
            )
    else:
        new_rows = _EMPTY_ROW
    new_table = (
        f'{_TABLE_OPEN}<thead><tr>'
        f'<th {_TH_STYLE}>Lane (Origin → Destination)</th>'
        f'<th {_TH_STYLE}>Equipment</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:center")}>TEU</th>'
        f'<th {_TH_STYLE} title="Commodity from Lonny RFQ body">Product</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:center")} title="Reefer temperature when present (Hilmar dairy)">Temp</th>'
        f'<th {_TH_STYLE} title="Lonny\'s requested ETA / ETD if stated in RFQ">Lonny Asked For (ETA)</th>'
        f'<th {_TH_STYLE}>Lonny Sent (PT)</th>'
        f'</tr></thead><tbody>{new_rows}</tbody></table>'
    )

    # ── 2. OL-USA RESPONSES (11 columns, full timing trio + offered dates) ───
    resp_rows = ""
    if ol_resp:
        for r in ol_resp:
            lane = r.get("lane") or f"{r.get('origin','?')} → {r.get('destination','?')}"
            cont = r.get("containers") or "—"
            teu = _fmt_teu(r)
            carrier = r.get("carrier_quoted") or "—"
            rate = r.get("ol_rate")
            rate_s = f"${rate:,.0f}" if isinstance(rate, (int, float)) else "—"
            signer = r.get("ol_responder_signer") or "—"
            lonny_t = _fmt_short_pt(r.get("request_timestamp"))
            ol_t = _fmt_short_et(r.get("response_timestamp"))
            ttq_s, ttq_color = _ttq_cell(r)
            etd_off = r.get("etd_offered") or "—"
            eta_off = r.get("eta_offered") or "—"
            resp_rows += (
                f'<tr><td {_TD_STYLE}><strong>{_esc(lane)}</strong></td>'
                f'<td {_TD_STYLE};font-size:11px>{_esc(cont)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")}>{_esc(teu)}</td>'
                f'<td {_TD_STYLE}>{_esc(carrier)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:right")};font-weight:600>{_esc(rate_s)}</td>'
                f'<td {_TD_STYLE}>{_esc(signer)}</td>'
                f'<td {_TD_STYLE};white-space:nowrap;font-size:11px>{_esc(lonny_t)}</td>'
                f'<td {_TD_STYLE};white-space:nowrap;font-size:11px>{_esc(ol_t)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")};font-weight:600;color:{ttq_color}>{_esc(ttq_s)}</td>'
                f'<td {_TD_STYLE};font-size:11px>{_esc(etd_off)}</td>'
                f'<td {_TD_STYLE};font-size:11px>{_esc(eta_off)}</td></tr>'
            )
    else:
        resp_rows = _EMPTY_ROW
    resp_table = (
        f'{_TABLE_OPEN}<thead><tr>'
        f'<th {_TH_STYLE}>Lane</th>'
        f'<th {_TH_STYLE}>Equipment</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:center")}>TEU</th>'
        f'<th {_TH_STYLE}>Carrier Quoted</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:right")}>Rate ($/container)</th>'
        f'<th {_TH_STYLE}>Who Responded (OL signer)</th>'
        f'<th {_TH_STYLE} title="When Lonny sent the RFQ (Pacific Time)">Lonny Sent (PT)</th>'
        f'<th {_TH_STYLE} title="When OL responded with the rate (Eastern Time)">OL Quoted (ET)</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:center")} title="Biz-hours from Lonny RFQ to OL response. Green ≤4h, amber 4-24h, red >24h.">Time to Quote</th>'
        f'<th {_TH_STYLE} title="OL\'s offered ETD (sailing date)">ETD Offered</th>'
        f'<th {_TH_STYLE} title="OL\'s offered ETA (arrival date)">ETA Offered</th>'
        f'</tr></thead><tbody>{resp_rows}</tbody></table>'
    )

    # ── 3. STATUS CHANGES (5 columns) ────────────────────────────────
    # 2026-05-19 PM 3rd pass (Michael "why is rate not in $ format?"):
    # the reason strings from ingest.py are stored as "rate=3450.0" —
    # reformat at render time to "$3,450". Same regex used for any "rate=N"
    # substring across all status-change reasons.
    import re as _re_sc
    def _fmt_rate_in_reason(reason):
        def _sub(m):
            try:
                val = float(m.group(1))
                return f"${val:,.0f}"
            except ValueError:
                return m.group(0)
        # "carrier=CMA CGM, rate=3450.0" → "carrier=CMA CGM, rate=$3,450"
        # Also handle "rate=$3450.0" (defensive) and trailing decimals
        return _re_sc.sub(r"rate=\$?(\d+(?:\.\d+)?)", lambda m: f"rate={_sub(m)}", reason)

    sc_rows = ""
    if status_ch:
        for r, h in status_ch:
            lane = r.get("lane") or f"{r.get('origin','?')} → {r.get('destination','?')}"
            cnt = r.get('container_count') or 0
            teu = r.get('teu_requested') or 0
            cont_label = r.get('containers') or f"{cnt}cnt"
            reason = _fmt_rate_in_reason(h.get('reason') or '')
            req_date = r.get('request_date') or '—'
            carrier = r.get("carrier_won") or r.get("carrier_quoted") or "—"
            rate = r.get("ol_rate")
            rate_s = f"${rate:,.0f}" if isinstance(rate, (int, float)) else "—"
            sc_rows += (
                f'<tr><td {_TD_STYLE}><strong>{_esc(lane)}</strong></td>'
                f'<td {_TD_STYLE};font-size:11px>{_esc(str(cont_label))} / {teu} TEU</td>'
                f'<td {_TD_STYLE}>{_esc(req_date)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")}>'
                f'{V.status_pill(h["from"])} → {V.status_pill(h["to"])}</td>'
                f'<td {_TD_STYLE}>{_esc(carrier)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:right")};font-weight:600>{_esc(rate_s)}</td>'
                f'<td {_TD_STYLE};font-size:11px>{_esc(reason)}</td></tr>'
            )
    else:
        sc_rows = _EMPTY_ROW
    sc_table = (
        f'{_TABLE_OPEN}<thead><tr>'
        f'<th {_TH_STYLE}>Lane</th>'
        f'<th {_TH_STYLE}>Equipment / TEU</th>'
        f'<th {_TH_STYLE}>Requested Date</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:center")}>Status Change</th>'
        f'<th {_TH_STYLE}>Carrier</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:right")}>Rate</th>'
        f'<th {_TH_STYLE}>Reason</th>'
        f'</tr></thead><tbody>{sc_rows}</tbody></table>'
    )

    # ── 4. PENDING — split into its two materially different waits (per
    # Michael 2026-06-12 "several pending statuses to be clear"):
    # PENDING OL QUOTE (chase OL) vs PENDING HILMAR RESPONSE (chase Lonny).
    pending_ol = [r for r in pending if core.pending_substate(r) == "PENDING_OL"]
    pending_hil = [r for r in pending if core.pending_substate(r) == "PENDING_HILMAR"]

    pol_rows = ""
    if pending_ol:
        for r in pending_ol:
            lane = r.get("lane") or "—"
            cont = r.get("containers") or "—"
            teu = _fmt_teu(r)
            lonny_t = _fmt_short_pt(r.get("request_timestamp"))
            req_dt = core.parse_iso(r.get("request_timestamp"))
            wait_s = "—"
            if req_dt:
                wait_s = f"{(datetime.now(timezone.utc) - req_dt).total_seconds() / 3600.0:.1f}h"
            pol_rows += (
                f'<tr><td {_TD_STYLE}><strong>{_esc(lane)}</strong></td>'
                f'<td {_TD_STYLE};font-size:11px>{_esc(cont)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")}>{_esc(teu)}</td>'
                f'<td {_TD_STYLE};white-space:nowrap;font-size:11px>{_esc(lonny_t)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")};font-weight:600>{_esc(wait_s)}</td></tr>'
            )
    else:
        pol_rows = _EMPTY_ROW
    pol_table = (
        f'{_TABLE_OPEN}<thead><tr>'
        f'<th {_TH_STYLE}>Lane</th>'
        f'<th {_TH_STYLE}>Equipment</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:center")}>TEU</th>'
        f'<th {_TH_STYLE} title="When Lonny sent the RFQ (Pacific Time)">Lonny Sent (PT)</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:center")} title="Hours since the RFQ with no OL quote yet — chase OL">Waiting on OL</th>'
        f'</tr></thead><tbody>{pol_rows}</tbody></table>'
    )

    pend_rows = ""
    if pending_hil:
        for r in pending_hil:
            lane = r.get("lane") or "—"
            cont = r.get("containers") or "—"
            teu = _fmt_teu(r)
            carrier = r.get("carrier_quoted") or "—"
            rate = r.get("ol_rate")
            rate_s = f"${rate:,.0f}" if isinstance(rate, (int, float)) else "—"
            signer = r.get("ol_responder_signer") or "—"
            lonny_t = _fmt_short_pt(r.get("request_timestamp"))
            ol_t = _fmt_short_et(r.get("response_timestamp"))
            ttq_s, ttq_color = _ttq_cell(r)
            resp_dt = core.parse_iso(r.get("response_timestamp"))
            hrs_s = "—"
            if resp_dt:
                delta_h = (datetime.now(timezone.utc) - resp_dt).total_seconds() / 3600.0
                hrs_s = f"{delta_h:.1f}h"
            pend_rows += (
                f'<tr><td {_TD_STYLE}><strong>{_esc(lane)}</strong></td>'
                f'<td {_TD_STYLE};font-size:11px>{_esc(cont)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")}>{_esc(teu)}</td>'
                f'<td {_TD_STYLE}>{_esc(carrier)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:right")};font-weight:600>{_esc(rate_s)}</td>'
                f'<td {_TD_STYLE}>{_esc(signer)}</td>'
                f'<td {_TD_STYLE};white-space:nowrap;font-size:11px>{_esc(lonny_t)}</td>'
                f'<td {_TD_STYLE};white-space:nowrap;font-size:11px>{_esc(ol_t)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")};font-weight:600;color:{ttq_color}>{_esc(ttq_s)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")};font-weight:600>{_esc(hrs_s)}</td></tr>'
            )
    else:
        pend_rows = _EMPTY_ROW
    pend_table = (
        f'{_TABLE_OPEN}<thead><tr>'
        f'<th {_TH_STYLE}>Lane</th>'
        f'<th {_TH_STYLE}>Equipment</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:center")}>TEU</th>'
        f'<th {_TH_STYLE}>Carrier Quoted</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:right")}>Rate</th>'
        f'<th {_TH_STYLE}>Who Quoted (OL signer)</th>'
        f'<th {_TH_STYLE} title="When Lonny sent the RFQ (Pacific Time)">Lonny Sent (PT)</th>'
        f'<th {_TH_STYLE} title="When OL responded with the rate (Eastern Time)">OL Quoted (ET)</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:center")} title="Biz-hours OL took to respond on this row. Green ≤4h, amber 4-24h, red >24h.">Time to Quote</th>'
        f'<th {_TH_STYLE.replace("text-align:left","text-align:center")} title="Hours since OL quoted — chase if >24h">Waiting</th>'
        f'</tr></thead><tbody>{pend_rows}</tbody></table>'
    )

    wins_in_day = sum(1 for (r, h) in status_ch if h.get("to") == "WIN")
    summary_line = (
        f"📊 {len(new_req)} new requests · {len(ol_resp)} new quotes received · "
        f"{wins_in_day} wins · {len(status_ch)} status changes · "
        f"{len(pending_ol)} pending OL quote · {len(pending_hil)} pending Hilmar response"
    )

    return f"""
<div style="background:#eff6ff;border:2px solid #3b82f6;border-radius:8px;padding:20px;margin-bottom:24px">
  <h2 style="margin:0 0 8px;color:#1e40af;font-size:18px">📋 What Happened — {_esc(report_label)}</h2>
  <p style="margin:0 0 14px;font-size:11px;color:#64748b">Previous business day. Daily email runs at 10 AM ET — Lonny's California office (PT) opens ~3 hours later, so 'today' has no data yet at send time.</p>

  <h3 style="margin:14px 0 4px;color:#1e40af;font-size:13px">📥 NEW REQUESTS FROM LONNY ({len(new_req)})</h3>
  {new_table}

  <h3 style="margin:14px 0 4px;color:#1e40af;font-size:13px">📤 OL-USA RESPONSES ({len(ol_resp)})</h3>
  {resp_table}

  <h3 style="margin:14px 0 4px;color:#7c3aed;font-size:13px">🔄 STATUS CHANGES ({len(status_ch)})</h3>
  {sc_table}

  <h3 style="margin:14px 0 4px;color:#d97706;font-size:13px">⏳ PENDING OL QUOTE ({len(pending_ol)})</h3>
  {pol_table}

  <h3 style="margin:14px 0 4px;color:#7c3aed;font-size:13px">⏳ PENDING HILMAR RESPONSE ({len(pending_hil)})</h3>
  {pend_table}

  <p style="margin:14px 0 0;font-size:13px;color:#374151;font-weight:bold">{_esc(summary_line)}</p>
</div>
"""


def _kpi_card(value, label, bg, width="25%", sublabel=""):
    """Render a KPI tile.

    2026-05-19 PM 2nd pass (Michael "in the boxes.. you can see formatting
    is bad as bottoms of words get cut off"): height bumped 64px → 88px so
    3-line content (value + label + sublabel) doesn't get clipped on the
    bottom. `min-height` + `height` set together so Outlook (which honors
    height) and rich clients (which honor min-height) both render right.

    IMPORTANT: callers must pass RAW strings (e.g. "Quoted & Lost"), not
    pre-escaped HTML — the &-escape happens once here via _esc(). Passing
    "Quoted &amp; Lost" double-escapes to "&amp;amp;" which Outlook renders
    literally. Caught 2026-05-19 PM screenshot.
    """
    sub_html = (
        f'<div style="font-size:10px;opacity:0.88;margin-top:3px;line-height:1.25">{_esc(sublabel)}</div>'
        if sublabel else
        '<div style="font-size:10px;opacity:0.88;margin-top:3px;line-height:1.25">&nbsp;</div>'
    )
    return f"""
<td style="padding:4px;width:{width};vertical-align:top">
  <div style="background:{bg};color:white;border-radius:8px;padding:14px 10px 16px;text-align:center;min-height:88px;height:88px;box-sizing:border-box">
    <div style="font-size:22px;font-weight:bold;line-height:1.1">{_esc(value)}</div>
    <div style="font-size:11px;opacity:0.94;margin-top:4px;line-height:1.25">{_esc(label)}</div>
    {sub_html}
  </div>
</td>
"""


def _today_summary(requests, report_date=None):
    """Compute wins/losses/etc for the report date (= previous business day).
    Function name kept as `_today_summary` for backward compatibility, but it
    no longer reports 'today' — see _report_date for rationale.
    """
    if report_date is None:
        report_date = _report_date()
    rd_iso = report_date.isoformat()
    day_reqs = [r for r in requests
                if (r.get("request_date") == rd_iso) or (r.get("date") == rd_iso)]
    return {
        "wins":         sum(1 for r in day_reqs if r.get("status") == "WIN"),
        "teu_won":      sum(int(r.get("teu_won") or r.get("teu_requested") or 0)
                            for r in day_reqs if r.get("status") == "WIN"),
        "quoted_lost":  sum(1 for r in day_reqs if r.get("status") == "LOSS" and r.get("quoted")),
        "not_quoted":   sum(1 for r in day_reqs if r.get("status") == "LOSS" and not r.get("quoted")),
        "pending":      sum(1 for r in day_reqs if r.get("status") == "PENDING"),
        "total":        len(day_reqs),
        "as_of_label":  f"{rd_iso} (ET)",
        "report_date":  rd_iso,
    }


def _kpi_block_html(summary, requests=None, report_date=None):
    """Two KPI rows:
      Row 1 (REPORT DAY) — what happened on the previous business day in ET.
        Often low or zero on quiet days — that's the truth.
      Row 2 (PERIOD TO DATE) — cumulative over the data range. Used for negotiation depth.

    Michael 2026-04-30: "we didn't win that today" — fix is that the daily email's
    headline KPI was cumulative but unlabeled. Now both views are explicit.
    Michael 2026-05-07: "yesterday kpi run" — Row 1 reports yesterday (or
    last Friday on Mon), not literal today.
    """
    total = summary.get("total_entries", 0)
    wins = summary.get("wins", 0)
    teu_won = summary.get("teu_won", 0)
    ql = summary.get("quoted_lost", 0)
    teu_ql = summary.get("teu_quoted_lost", 0)
    nq = summary.get("not_quoted", 0)
    teu_nq = summary.get("teu_not_quoted", 0)
    pending = summary.get("pending_hilmar", 0)
    teu_pending = summary.get("teu_pending", 0)
    biz = summary.get("turnaround_avg_biz_hours", 0.0) or 0.0

    # 2026-05-19 PM 6th pass (Michael "i don't think your win rate is accurate
    # how is it including w +q&l + nq.. q&l and nq are losses"):
    #
    # Old formula: Win Rate = Wins / (Wins + Q&L + NQ) → 41.0% on current data.
    # The problem: NQ rows are "OL never responded" — we didn't lose on
    # price, we never even competed. Lumping them with Q&L (which IS a
    # head-to-head competitive loss) muddies the win-vs-competitor signal.
    #
    # New formula: Win Rate = Wins / (Wins + Q&L) — pure competitive
    # conversion. NQ now shows as its own KPI ("No-Response Rate") so the
    # parser-side failure mode (OL silence) is visible but doesn't drag the
    # competitive win rate down.
    decided_competitive = wins + ql
    wr = (wins / decided_competitive * 100.0) if decided_competitive else 0.0
    decided_all = wins + ql + nq
    no_resp_rate = (nq / decided_all * 100.0) if decided_all else 0.0

    # Period string for the KPI section header — per Michael 2026-05-18
    # ("two terrible audits..."): KPIs labeled "PTD" without a date range
    # made it ambiguous whether "32 wins / 137 TEU" was today or cumulative.
    # Render the explicit date range so there's no doubt.
    reqs_with_dates = [r for r in (requests or []) if r.get("request_date")]
    if reqs_with_dates:
        dates = sorted(r["request_date"] for r in reqs_with_dates)
        period_str_kpi = f"from {dates[0]} through {dates[-1]}"
    else:
        period_str_kpi = "(no date range available)"

    if report_date is None:
        report_date = _report_date()
    day_short = _fmt_date(datetime.combine(report_date, datetime.min.time()), "%a %b %-d")
    day = _today_summary(requests or [], report_date=report_date)

    # 7-day trend per metric for sparklines under the day-row cards
    from datetime import timedelta as _td
    trend_days = []
    for i in range(6, -1, -1):
        d = report_date - _td(days=i)
        s = _today_summary(requests or [], report_date=d)
        trend_days.append(s)
    spark_total = V.sparkline_svg([s['total'] for s in trend_days], width=80, height=18, color="#3b82f6")
    spark_wins = V.sparkline_svg([s['wins'] for s in trend_days], width=80, height=18, color="#22c55e")
    spark_ql = V.sparkline_svg([s['quoted_lost'] for s in trend_days], width=80, height=18, color="#ef4444")
    spark_nq = V.sparkline_svg([s['not_quoted'] for s in trend_days], width=80, height=18, color="#64748b")
    spark_pend = V.sparkline_svg([s['pending'] for s in trend_days], width=80, height=18, color="#8b5cf6")

    return f"""
<h2 style="color:#0a2350;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📊 KPIs — {_esc(day_short)} (ET) <span style="font-size:11px;color:#64748b;font-weight:400;margin-left:8px">7-day trend ↓</span></h2>
<p style="margin:-8px 0 8px;font-size:11px;color:#64748b">Activity on the previous business day. Math reconciliation: Requests = Won + Quoted&Lost + Not Quoted + Pending.</p>
<table style="width:100%;border-collapse:collapse;margin-bottom:16px">
  <tr>
    {_kpi_card(day['total'], f"Requests — {day_short}", "#3b82f6", "20%", sublabel=f"{day.get('total',0)} entries")}
    {_kpi_card(day['wins'], f"Won — {day_short}", "#22c55e", "20%", sublabel=f"{day['teu_won']} TEU won")}
    {_kpi_card(day['quoted_lost'], f"Quoted & Lost — {day_short}", "#ef4444", "20%", sublabel="OL quoted; not booked")}
    {_kpi_card(day['not_quoted'], f"Not Quoted — {day_short}", "#64748b", "20%", sublabel="OL did not respond")}
    {_kpi_card(day['pending'], f"Pending — {day_short}", "#8b5cf6", "20%", sublabel="awaiting Hilmar")}
  </tr>
  <tr>
    <td style="padding:0 4px;text-align:center">{spark_total}</td>
    <td style="padding:0 4px;text-align:center">{spark_wins}</td>
    <td style="padding:0 4px;text-align:center">{spark_ql}</td>
    <td style="padding:0 4px;text-align:center">{spark_nq}</td>
    <td style="padding:0 4px;text-align:center">{spark_pend}</td>
  </tr>
</table>
<h2 style="color:#0a2350;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📊 KPIs — All requests {_esc(period_str_kpi)}</h2>
<p style="margin:-8px 0 8px;font-size:11px;color:#64748b">Cumulative over the period shown above — used for win-rate negotiation depth, NOT "today". 'Won' = WIN rows. 'Quoted & Lost' = OL quoted but Lonny chose elsewhere. 'Not Quoted' = OL never responded or had no rate. 'Pending' = awaiting Lonny's decision.</p>
<table style="width:100%;border-collapse:collapse;margin-bottom:20px">
  <tr>
    {_kpi_card(total, "Total Requests", "#3b82f6", sublabel="(all statuses)")}
    {_kpi_card(wins, "Won · this period", "#22c55e", sublabel=f"{teu_won} TEU won")}
    {_kpi_card(ql, "Quoted & Lost", "#ef4444", sublabel=f"{teu_ql} TEU · lost on price")}
    {_kpi_card(nq, "Not Quoted", "#64748b", sublabel=f"{teu_nq} TEU · OL silent")}
  </tr>
  <tr>
    {_kpi_card(pending, "Pending Lonny", "#8b5cf6", sublabel=f"{teu_pending} TEU")}
    {_kpi_card(f"{wr:.1f}%", "Win Rate", "#22c55e", sublabel=f"{wins} wins ÷ {decided_competitive} decided")}
    {_kpi_card(f"{no_resp_rate:.1f}%", "No-Response Rate", "#64748b", sublabel=f"{nq} NQ ÷ {decided_all} total")}
    {_kpi_card(f"{biz:.1f}h", "Avg Biz-Hrs", "#6366f1", sublabel="Lonny → OL quote")}
  </tr>
</table>

<!-- 2026-05-19 PM 7th pass (Michael "i'm lost win rate 45.4 percent what
     is wins / wins + q&l"): plain-English explainer of Win Rate. -->
<div style="background:#f0fdf4;border-left:4px solid #16a34a;padding:10px 14px;margin:-8px 0 16px;border-radius:4px;font-size:12px;color:#166534;line-height:1.5">
  <strong>How Win Rate is computed:</strong> Win Rate = wins ÷ decided contests.
  A "decided contest" is every time OL gave Lonny a rate AND Lonny chose
  (so it's <strong>Wins + Q&amp;L</strong> — wins are the times Lonny picked us,
  Q&amp;L are the times Lonny picked a competitor). NQ (OL never gave a rate)
  is NOT in the denominator — we never actually competed on those, so they
  show as a separate "No-Response Rate" KPI. Pending (not decided yet) is
  also excluded. Today: <strong>{wins} wins ÷ {decided_competitive} decided = {wr:.1f}%</strong>.
</div>
"""


def _week_block_html(rows):
    """Render the 8-week rollup.

    2026-05-19 PM 2nd pass (Michael "in this week vs last week, the
    formatting is poor for the dates as it rolls to second lines. also
    formatting on notable wins shows numbers and sometimes has the mdolx
    in front of the number.. should be uniform"):
      - Widen Week column (white-space:nowrap) so "W18 (Apr 27–May 1)"
        stays on one line
      - Compute TEU totals per week from the row data (was: counts only)
        and show alongside the request count so it's clear what each
        number represents
      - Strip "MDOLX" prefix uniformly from Notable Wins — they're all
        MDOLX numbers, the prefix is redundant clutter
    """
    if not rows:
        return ""
    import re as _re
    body = ""
    alt = True
    for label, b in rows:
        bg = "#ffffff" if alt else "#f0f4f8"
        alt = not alt
        # Strip MDOLX prefix from every booking-ref so the column is uniform
        mdolx_clean = [_re.sub(r"^MDOLX", "", m) for m in (b.get("mdolx") or [])]
        mdolx = ", ".join(mdolx_clean) if mdolx_clean else "—"
        teu_req = b.get("teu_req") or 0
        teu_won = b.get("teu_won") or 0
        # 2026-05-19 PM 3rd pass: _week_bucket now returns label with a
        # literal "\n" between week code + date range. Split and render
        # the date on its own line in muted grey so narrow viewports
        # don't wrap mid-date.
        if "\n" in label:
            wk_code, date_range = label.split("\n", 1)
        else:
            wk_code, date_range = label, ""
        week_cell = (
            f'<strong>{_esc(wk_code)}</strong>'
            + (f'<br><span style="font-size:10px;color:#64748b;font-weight:400">{_esc(date_range)}</span>' if date_range else '')
        )
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px;white-space:nowrap">{week_cell}</td>
  <td style="padding:6px 8px;text-align:center">{b['requests']}<br><span style="font-size:10px;color:#64748b">{teu_req} TEU</span></td>
  <td style="padding:6px 8px;text-align:center;color:#16a34a;font-weight:bold">{b['won']}<br><span style="font-size:10px;color:#16a34a;opacity:0.75">{teu_won} TEU</span></td>
  <td style="padding:6px 8px;text-align:center;color:#dc2626">{b['ql']}</td>
  <td style="padding:6px 8px;text-align:center;color:#64748b">{b['nq']}</td>
  <td style="padding:6px 8px;text-align:center;color:#7c3aed">{b['pending']}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(mdolx)}</td>
</tr>
"""
    return f"""
<h2 style="color:#0a2350;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📅 This Week vs Last Week</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">Counts are number of REQUESTS / WINS / etc. — TEU is shown below each count in muted grey. All weeks are ISO weeks (Mon-Sun) in ET.</p>
<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px">
  <tr style="background:#0a2350;color:white">
    <th style="padding:8px;text-align:left" title="ISO week range (Mon-Sun) in ET">Week</th>
    <th style="padding:8px;text-align:center" title="Number of requests, with TEU asked-for beneath">Requests (# · TEU)</th>
    <th style="padding:8px;text-align:center" title="Bookings won, with TEU won beneath">Won (# · TEU)</th>
    <th style="padding:8px;text-align:center" title="Quoted &amp; Lost requests">Q&amp;L (#)</th>
    <th style="padding:8px;text-align:center" title="Not Quoted requests (OL never responded)">NQ (#)</th>
    <th style="padding:8px;text-align:center" title="Pending — awaiting Lonny's decision">Pending (#)</th>
    <th style="padding:8px;text-align:left" title="Booking refs for wins this week (MDOLX prefix stripped — all values are MDOLX numbers)">Notable Wins (MDOLX#)</th>
  </tr>
  {body}
</table>
"""


def _carrier_block_html(rows):
    if not rows:
        return ""
    body = ""
    alt = True
    # 2026-05-19 PM (Michael "carrier performance should have totals teu
    # offered"): new column. "TEU Offered" = sum of teu_requested across
    # every row this carrier quoted = Won + Q&L + Pending TEU. This is the
    # total capacity OL gave us via this carrier in the period.
    teu_offered_total = 0
    teu_won_total = 0
    teu_lost_total = 0
    quoted_total = 0
    wins_total = 0
    ql_total = 0
    pending_total = 0
    # 2026-05-19 PM 2nd pass (Michael "cma you show 434 teu offered but 94
    # won and 290 lost.. those don't add up"): the missing 50 TEU was Pending
    # TEU — correct math but invisible to the reader. Add an explicit
    # "TEU Pending" column so Offered = Won + Lost + Pending reconciles on
    # every row.
    teu_pending_total = 0
    for name, b, wr in rows:
        bg = "#ffffff" if alt else "#f0f4f8"
        alt = not alt
        teu_won = b.get('teu_won', 0) or 0
        teu_lost = b.get('teu_lost', 0) or 0
        teu_pend = b.get('teu_pending', 0) or 0
        teu_offered = teu_won + teu_lost + teu_pend
        teu_offered_total += teu_offered
        teu_won_total += teu_won
        teu_lost_total += teu_lost
        teu_pending_total += teu_pend
        quoted_total += b.get('quoted', 0) or 0
        wins_total += b.get('won', 0) or 0
        ql_total += b.get('ql', 0) or 0
        pending_total += b.get('pending', 0) or 0
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px;font-weight:bold">{_esc(name)}</td>
  <td style="padding:6px 8px;text-align:center">{b['quoted']}</td>
  <td style="padding:6px 8px;text-align:center;color:#16a34a;font-weight:bold">{b['won']}</td>
  <td style="padding:6px 8px;text-align:center;color:#dc2626">{b['ql']}</td>
  <td style="padding:6px 8px;text-align:center;color:#7c3aed">{b['pending']}</td>
  <td style="padding:6px 8px;text-align:center">{wr:.1f}%</td>
  <td style="padding:6px 8px;text-align:right;font-weight:600">{teu_offered}</td>
  <td style="padding:6px 8px;text-align:right;color:#16a34a;font-weight:bold">{teu_won}</td>
  <td style="padding:6px 8px;text-align:right;color:#dc2626">{teu_lost}</td>
  <td style="padding:6px 8px;text-align:right;color:#7c3aed">{teu_pend}</td>
</tr>
"""
    # Totals footer — reconciles to the data range
    body += f"""
<tr style="background:#e5e7eb;font-weight:bold;border-top:2px solid #0a2350">
  <td style="padding:8px">TOTAL (all carriers)</td>
  <td style="padding:8px;text-align:center">{quoted_total}</td>
  <td style="padding:8px;text-align:center;color:#16a34a">{wins_total}</td>
  <td style="padding:8px;text-align:center;color:#dc2626">{ql_total}</td>
  <td style="padding:8px;text-align:center;color:#7c3aed">{pending_total}</td>
  <td style="padding:8px;text-align:center;color:#64748b">—</td>
  <td style="padding:8px;text-align:right">{teu_offered_total}</td>
  <td style="padding:8px;text-align:right;color:#16a34a">{teu_won_total}</td>
  <td style="padding:8px;text-align:right;color:#dc2626">{teu_lost_total}</td>
  <td style="padding:8px;text-align:right;color:#7c3aed">{teu_pending_total}</td>
</tr>
"""
    return f"""
<h2 style="color:#0a2350;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">🚢 Carrier Performance — All requests in current dataset</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">Per-carrier rollup across EVERY request currently in tracking-data-v2.json. Counts (#) are number of request rows. TEU columns sum the containers on those rows. <strong>Math reconciliation:</strong> TEU Offered = TEU Won + TEU Lost + TEU Pending (on every row).</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:#0a2350;color:white">
    <th style="padding:8px;text-align:left">Carrier</th>
    <th style="padding:8px;text-align:center" title="Distinct requests where this carrier was quoted">Times<br>Quoted (#)</th>
    <th style="padding:8px;text-align:center" title="Wins booked">Wins (#)</th>
    <th style="padding:8px;text-align:center" title="Quoted &amp; Lost — gave a rate but lost the booking">Q&L (#)</th>
    <th style="padding:8px;text-align:center" title="Awaiting Lonny's decision">Pending (#)</th>
    <th style="padding:8px;text-align:center" title="Wins / Quotes. Per-carrier metric. NOT a parser metric.">Win<br>Rate</th>
    <th style="padding:8px;text-align:right" title="TEU Won + TEU Lost + TEU Pending. Total capacity OL gave us through this carrier.">TEU<br>Offered</th>
    <th style="padding:8px;text-align:right" title="TEU on this carrier's WIN rows">TEU<br>Won</th>
    <th style="padding:8px;text-align:right" title="TEU on this carrier's Q&L rows">TEU<br>Lost</th>
    <th style="padding:8px;text-align:right" title="TEU on this carrier's Pending rows (sums to make Offered reconcile)">TEU<br>Pending</th>
  </tr>
  {body}
</table>
"""


def _winning_lanes_html(rows):
    """Top Winning Lanes table.

    2026-05-19 PM 5th pass (Michael "you only have to title each row.. i
    love the idea but i have no clue what the data is"): every value in
    every row now carries an INLINE micro-label below it (e.g. "7" with
    "Wins" beneath in tiny grey). So even if the column header isn't
    visible (Outlook gradient bug, narrow viewport, screen reader, color-
    blind), each cell self-describes. Same vertical-stack pattern used in
    the Week table.
    """
    if not rows:
        return ""
    max_teu = max((b['teu_won'] for _, b in rows), default=1) or 1
    body = ""
    alt = True
    _MICRO = 'style="font-size:9px;color:#94a3b8;font-weight:400;letter-spacing:0.3px;text-transform:uppercase"'
    for lane, b in rows:
        bg = "#ffffff" if alt else "#ecfdf5"
        alt = not alt
        carriers = ", ".join(sorted(b.get("carriers") or [])) or "—"
        teu_bar = V.bar_cell(b['teu_won'], max_teu, color="#16a34a", label=str(b['teu_won']), width_px=80)
        # 2026-05-19 PM 6th pass: per-lane Win Rate now matches global
        # definition — Wins / (Wins + Q&L). NQ excluded (different failure
        # mode). Pending excluded (not decided yet).
        ql_count = b.get('ql', 0)
        _decided_comp = b['won'] + ql_count
        win_rate = (b['won'] / _decided_comp * 100) if _decided_comp else 0
        wr_bg = V.heatmap_color(win_rate, vmin=0, vmax=100, mode="good_high")
        nq_count = b.get('nq', 0)
        pend_count = b.get('pending', 0)
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px;font-weight:600;vertical-align:top">{_esc(lane)}<div {_MICRO}>Lane</div></td>
  <td style="padding:6px 8px;text-align:center;color:#16a34a;font-weight:bold;vertical-align:top">{b['won']}<div {_MICRO}>Wins</div></td>
  <td style="padding:6px 8px;text-align:center;color:#dc2626;vertical-align:top">{ql_count}<div {_MICRO}>Q&amp;L</div></td>
  <td style="padding:6px 8px;text-align:center;color:#64748b;vertical-align:top">{nq_count}<div {_MICRO}>NQ</div></td>
  <td style="padding:6px 8px;text-align:center;color:#7c3aed;vertical-align:top">{pend_count}<div {_MICRO}>Pending</div></td>
  <td style="padding:6px 8px;text-align:left;vertical-align:top">{teu_bar}<div {_MICRO}>TEU Won</div></td>
  <td style="padding:6px 8px;text-align:center;background:{wr_bg};font-weight:600;vertical-align:top">{win_rate:.1f}%<div {_MICRO}>Win Rate</div></td>
  <td style="padding:6px 8px;font-size:11px;vertical-align:top">{_esc(carriers)}<div {_MICRO}>Winning Carriers</div></td>
</tr>
"""
    # 2026-05-19 PM 4th pass (Michael "still no column headers for these
    # sections"): Outlook does not render CSS linear-gradient. The previous
    # header used background:linear-gradient → Outlook stripped the
    # background → white text on white = invisible header. Switched to
    # solid #059669 (winning) / #7f1d1d (losing) which Outlook honors.
    return f"""
<h2 style="color:#0a2350;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📈 Top Winning Lanes — All requests in current dataset</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">Sorted by TEU won (descending). "Wins" = WIN rows on this lane. "Q&amp;L" / "NQ" / "Pending" break out the other statuses so you see the full mix. "Win Rate" = Wins / (Wins + Q&amp;L + NQ), Pending excluded. A lane can ALSO appear in Top Losing Lanes when high-volume on both sides.</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background-color:#059669;color:#ffffff">
    <th style="padding:8px;text-align:left;background-color:#059669;color:#ffffff">Lane (origin → destination)</th>
    <th style="padding:8px;text-align:center;background-color:#059669;color:#ffffff" title="Bookings won on this lane">Wins (#)</th>
    <th style="padding:8px;text-align:center;background-color:#059669;color:#ffffff" title="Quoted & Lost — OL responded but Lonny chose elsewhere">Q&amp;L (#)</th>
    <th style="padding:8px;text-align:center;background-color:#059669;color:#ffffff" title="Not Quoted — OL didn't respond with a rate">NQ (#)</th>
    <th style="padding:8px;text-align:center;background-color:#059669;color:#ffffff" title="Awaiting Lonny's decision">Pending (#)</th>
    <th style="padding:8px;text-align:left;background-color:#059669;color:#ffffff" title="Sum of TEU on this lane's WIN rows">TEU Won</th>
    <th style="padding:8px;text-align:center;background-color:#059669;color:#ffffff" title="Wins / (Wins + Q&L + NQ). Per-lane. NOT a parser metric.">Win Rate</th>
    <th style="padding:8px;text-align:left;background-color:#059669;color:#ffffff">Winning Carriers</th>
  </tr>
  {body}
</table>
"""


def _losing_lanes_html(rows):
    """Top Losing Lanes table — same inline-row-label treatment as Winning."""
    if not rows:
        return ""
    max_teu = max((b['teu_lost'] for _, b in rows), default=1) or 1
    body = ""
    alt = True
    _MICRO = 'style="font-size:9px;color:#94a3b8;font-weight:400;letter-spacing:0.3px;text-transform:uppercase"'
    for lane, b in rows:
        bg = "#ffffff" if alt else "#fef2f2"
        alt = not alt
        teu_bar = V.bar_cell(b['teu_lost'], max_teu, color="#dc2626", label=str(b['teu_lost']), width_px=80)
        # 2026-05-19 PM 6th pass: same Win Rate redefinition as winning side
        won_count = b.get('won', 0)
        _decided_comp = won_count + b['lost']
        win_rate = (won_count / _decided_comp * 100) if _decided_comp else 0
        wr_bg = V.heatmap_color(win_rate, vmin=0, vmax=100, mode="good_high")
        nq_count = b.get('nq', 0)
        pend_count = b.get('pending', 0)
        winning_carriers = ", ".join(sorted(b.get("carriers") or [])) or "—"
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px;font-weight:600;vertical-align:top">{_esc(lane)}<div {_MICRO}>Lane</div></td>
  <td style="padding:6px 8px;text-align:center;color:#dc2626;font-weight:bold;vertical-align:top">{b['lost']}<div {_MICRO}>Q&amp;L</div></td>
  <td style="padding:6px 8px;text-align:center;color:#64748b;vertical-align:top">{nq_count}<div {_MICRO}>NQ</div></td>
  <td style="padding:6px 8px;text-align:center;color:#7c3aed;vertical-align:top">{pend_count}<div {_MICRO}>Pending</div></td>
  <td style="padding:6px 8px;text-align:center;color:#16a34a;vertical-align:top">{won_count}<div {_MICRO}>Wins</div></td>
  <td style="padding:6px 8px;text-align:left;vertical-align:top">{teu_bar}<div {_MICRO}>TEU Lost</div></td>
  <td style="padding:6px 8px;text-align:center;background:{wr_bg};font-weight:600;vertical-align:top">{win_rate:.1f}%<div {_MICRO}>Win Rate</div></td>
  <td style="padding:6px 8px;font-size:11px;vertical-align:top">{_esc(winning_carriers)}<div {_MICRO}>Winning Carriers</div></td>
</tr>
"""
    # 2026-05-19 PM 4th pass: solid bg for Outlook (was gradient → invisible).
    return f"""
<h2 style="color:#0a2350;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📉 Top Losing Lanes — All requests in current dataset</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">Sorted by TEU lost (descending). "Q&amp;L" = OL quoted but Lonny chose elsewhere. "NQ" = OL didn't respond. "Wins" shown for context (a lane often has BOTH wins and losses). "Win Rate" same definition as Winning Lanes — same number on the same lane.</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background-color:#7f1d1d;color:#ffffff">
    <th style="padding:8px;text-align:left;background-color:#7f1d1d;color:#ffffff">Lane (origin → destination)</th>
    <th style="padding:8px;text-align:center;background-color:#7f1d1d;color:#ffffff" title="Q&L rows on this lane (quoted but lost)">Q&amp;L (#)</th>
    <th style="padding:8px;text-align:center;background-color:#7f1d1d;color:#ffffff" title="Not Quoted rows on this lane (OL didn't respond)">NQ (#)</th>
    <th style="padding:8px;text-align:center;background-color:#7f1d1d;color:#ffffff" title="Awaiting Lonny decision">Pending (#)</th>
    <th style="padding:8px;text-align:center;background-color:#7f1d1d;color:#ffffff" title="Bookings WON on this lane (context)">Wins (#)</th>
    <th style="padding:8px;text-align:left;background-color:#7f1d1d;color:#ffffff" title="Sum of TEU on Q&L rows">TEU Lost</th>
    <th style="padding:8px;text-align:center;background-color:#7f1d1d;color:#ffffff" title="Wins / (Wins + Q&L + NQ). Same number that appears in Winning Lanes for this lane.">Win Rate</th>
    <th style="padding:8px;text-align:left;background-color:#7f1d1d;color:#ffffff">Winning Carriers (on the wins)</th>
  </tr>
  {body}
</table>
"""


def _nq_html(rows, total_nq=None, teu_total=None):
    """Full-detail NQ table — every column needed to root-cause WHY OL did not quote.

    `rows` is the recent display window only (14 days). `total_nq` and
    `teu_total` cover ALL no-response losses across the data range and are
    surfaced in the header so the volume tally for rate negotiation depth
    is still visible — Michael 2026-05-13: 'keep it on the talley of volumes
    that hilmar moves for rate negotiation'.
    """
    if not rows and not total_nq:
        return ""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    def _age_days(ts):
        if not ts:
            return "—"
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return f"{(now - dt).days}d"
        except Exception:
            return "—"

    body = ""
    alt = True
    for r in rows:
        bg = "#ffffff" if alt else "#f8fafc"
        alt = not alt
        request_ts = r.get('request_timestamp') or ''
        imid_short = (r.get('source_imids') or ['—'])[0]
        if imid_short and imid_short != '—':
            imid_short = imid_short[:24] + '…' if len(imid_short) > 24 else imid_short
        body += f"""
<tr style="background:{bg};border-bottom:1px solid #e2e8f0">
  <td style="padding:6px 8px;white-space:nowrap">{_esc(r.get('request_date') or '—')}</td>
  <td style="padding:6px 8px;font-size:11px;color:#64748b">{_esc(r.get('lonny_time_pt') or '—')}</td>
  <td style="padding:6px 8px">{_esc(r.get('origin') or '—')}</td>
  <td style="padding:6px 8px">{_esc(r.get('destination') or '—')}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('containers') or '—')}</td>
  <td style="padding:6px 8px;text-align:center">{_esc(r.get('teu_requested') or '—')}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('eta_requested') or 'no ETA on request')}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('ol_responder') or '—')}</td>
  <td style="padding:6px 8px;text-align:center;font-weight:bold;color:#475569">{_age_days(request_ts)}</td>
  <td style="padding:6px 8px;font-size:10px;color:#64748b;font-family:monospace">{_esc(imid_short)}</td>
</tr>
"""
    # Build header with both visible-window count + total tally for rate-negotiation context
    total_label = f"{total_nq} total" if total_nq is not None and total_nq != len(rows) else f"{len(rows)} total"
    teu_label = f" • {teu_total} TEU" if teu_total else ""
    older_count = (total_nq or len(rows)) - len(rows)
    older_note = (f" • {older_count} older than {NQ_DISPLAY_WINDOW_DAYS}d hidden from listing "
                  f"but counted in volume tally for rate negotiation") if older_count > 0 else ""
    return f"""
<h2 style="color:#64748b;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #cbd5e1;padding-bottom:8px">⚠️ Not Quoted — Last {NQ_DISPLAY_WINDOW_DAYS} Days ({len(rows)} listed • {_esc(total_label)}{_esc(teu_label)})</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">Full request audit — every field needed to root-cause why OL did not respond.{_esc(older_note)}</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:#64748b;color:white">
    <th style="padding:8px;text-align:left">Date</th>
    <th style="padding:8px;text-align:left">Lonny Sent (PT)</th>
    <th style="padding:8px;text-align:left">Origin</th>
    <th style="padding:8px;text-align:left">Destination</th>
    <th style="padding:8px;text-align:left">Equipment</th>
    <th style="padding:8px;text-align:center">TEU</th>
    <th style="padding:8px;text-align:left">ETA Asked</th>
    <th style="padding:8px;text-align:left">OL Mailbox</th>
    <th style="padding:8px;text-align:center">Aging</th>
    <th style="padding:8px;text-align:left">Source IMID</th>
  </tr>
  {body}
</table>
"""


def _pending_ol_html(rows):
    """Pending OL Quote — RFQs Lonny sent that OL hasn't answered yet.

    Split out of the old single Pending section per Michael 2026-06-12
    ("several pending statuses to be clear"): a row with no quote is
    waiting on OL, not on Hilmar — chasing Lonny about it would be
    nonsense. Renders nothing when every pending row is quoted.
    """
    if not rows:
        return ""
    from datetime import datetime as _dt2
    from datetime import timezone as _tz2
    now = _dt2.now(_tz2.utc)
    total_teu = sum(int(r.get("teu_requested") or 0) for r in rows)

    def _fmt_pt(iso):
        try:
            dt = core.parse_iso(iso)
            if not dt:
                return "—"
            s = dt.astimezone(core.PT).strftime("%b %d %I:%M %p")
            s = s.replace(" 0", " ", 1).replace(" 0", " ", 1)
            return s + " PT"
        except Exception:
            return (iso[:16] if iso else "—")

    body = ""
    for r in rows:
        req_dt = core.parse_iso(r.get("request_timestamp"))
        wait_s = "—"
        wait_color = "#374151"
        if req_dt:
            wait_h = (now - req_dt).total_seconds() / 3600.0
            wait_s = f"{wait_h:.1f}h"
            wait_color = "#16a34a" if wait_h <= 8 else ("#d97706" if wait_h <= 24 else "#dc2626")
        body += f"""
<tr style="border-bottom:1px solid #e5e7eb">
  <td style="padding:6px 8px"><strong>{_esc(r.get('lane') or '—')}</strong></td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('containers') or '—')}</td>
  <td style="padding:6px 8px;text-align:center">{_esc(str(r.get('teu_requested') or '—'))}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('product') or '—')}</td>
  <td style="padding:6px 8px;white-space:nowrap;font-size:11px">{_esc(_fmt_pt(r.get('request_timestamp')))}</td>
  <td style="padding:6px 8px;text-align:center;font-weight:600;color:{wait_color}">{_esc(wait_s)}</td>
</tr>
"""
    body += f"""
<tr style="background:#e5e7eb;font-weight:bold;border-top:2px solid #d97706">
  <td style="padding:8px" colspan="2">TOTAL ({len(rows)} awaiting OL)</td>
  <td style="padding:8px;text-align:center">{total_teu}</td>
  <td style="padding:8px" colspan="3">&nbsp;</td>
</tr>
"""
    return f"""
<h2 style="color:#d97706;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #fcd34d;padding-bottom:8px">⏳ Pending OL Quote ({len(rows)} requests · {total_teu} TEU)</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">RFQs Lonny has sent that OL has NOT yet quoted — the wait is on OL, not Hilmar. "Waiting on OL" is wall-clock since the RFQ (green ≤8h, amber ≤24h, red &gt;24h — red rows are chase candidates with the OL desk).</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:#d97706;color:white">
    <th style="padding:8px;text-align:left;background-color:#d97706;color:#ffffff">Lane</th>
    <th style="padding:8px;text-align:left;background-color:#d97706;color:#ffffff">Equipment</th>
    <th style="padding:8px;text-align:center;background-color:#d97706;color:#ffffff">TEU</th>
    <th style="padding:8px;text-align:left;background-color:#d97706;color:#ffffff">Product</th>
    <th style="padding:8px;text-align:left;background-color:#d97706;color:#ffffff" title="When Lonny sent the RFQ (Pacific Time)">Lonny Sent (PT)</th>
    <th style="padding:8px;text-align:center;background-color:#d97706;color:#ffffff" title="Wall-clock since the RFQ with no OL quote — chase OL when red">Waiting on OL</th>
  </tr>
  {body}
</table>
"""


def _pending_html(rows):
    """Pending Hilmar Response — full per-row detail.

    2026-05-19 PM 3rd pass (Michael "pending lonny response should also
    indicate the number of containers/teus etc etc and time quoted and
    time quote request came in"):
      - Added Equipment + TEU columns (2nd pass)
      - Rate formatted as $X,XXX (2nd pass)
      - OL signer column (2nd pass)
      - Hours since OL quote (2nd pass)
      - 3rd pass: explicit Lonny request timestamp (PT) AND OL response
        timestamp (ET) — both absolute. The "Hours since OL quote" stays
        as the chase metric. Now the operator sees the full timeline at
        a glance: when Lonny asked, when OL answered, how long ago.
    """
    if not rows:
        return ""
    from datetime import datetime as _dt2
    from datetime import timezone as _tz2
    now = _dt2.now(_tz2.utc)

    def _hours_since(iso):
        try:
            dt = core.parse_iso(iso)
            return f"{(now - dt).total_seconds()/3600.0:.1f}h" if dt else "—"
        except Exception:
            return "—"

    # 2026-05-19 PM 6th pass (Michael "pending hilmar is still missing the
    # data i said with number of teus also time of receipt of request and
    # time quote went out"): the timestamps WERE in the data, but the
    # strftime format strings used Unix-only `%-d` and `%-I` which raise
    # ValueError on Windows (where the pipeline actually runs on the Cloud
    # PC). The try/except swallowed the error and returned "—" for every
    # row. Fix: use portable `%d` / `%I` (zero-padded) then strip the
    # leading-space + zero pair with .replace(" 0", " ") so "Apr 03 04:50"
    # renders "Apr 3 4:50".
    def _fmt_local_full(iso, tz, tz_label):
        try:
            dt = core.parse_iso(iso)
            if not dt:
                return "—"
            s = dt.astimezone(tz).strftime("%b %d %I:%M %p")
            # Strip leading zeros on day and hour (Windows-safe)
            s = s.replace(" 0", " ", 1)        # day:  "Apr 03" → "Apr 3"
            s = s.replace(" 0", " ", 1)        # hour: " 04:50" → " 4:50"
            return s + f" {tz_label}"
        except Exception as _e:
            # Defensive — emit traceback only in debug; production silent
            # but at least show the raw ISO so we know the data exists.
            return (iso[:16] if iso else "—")

    def _fmt_pt_full(iso): return _fmt_local_full(iso, core.PT, "PT")
    def _fmt_et_full(iso): return _fmt_local_full(iso, core.ET, "ET")

    body = ""
    alt = True
    total_teu = 0
    for r in rows:
        bg = "#ffffff" if alt else "#f5f3ff"
        alt = not alt
        teu = r.get('teu_requested') or 0
        total_teu += teu
        rate = r.get('ol_rate')
        rate_s = f"${rate:,.0f}" if isinstance(rate, (int, float)) else "—"
        signer = r.get('ol_responder_signer') or "—"
        lonny_t = _fmt_pt_full(r.get('request_timestamp'))
        ol_t = _fmt_et_full(r.get('response_timestamp'))
        # 2026-05-19 PM (Michael "add a column after ol quoted time with
        # how long it took"): Time to Quote = OL response biz-hours from
        # Lonny's RFQ. Pulled from turnaround_biz_hours (set by
        # apply_rate_responses when the rate-response email is the source).
        # Falls back to clock-hours if biz-hours wasn't computed.
        ttq_biz = r.get('turnaround_biz_hours')
        ttq_clock = r.get('turnaround_hours')
        if isinstance(ttq_biz, (int, float)):
            ttq_s = f"{ttq_biz:.1f}h"
            ttq_color = "#16a34a" if ttq_biz <= 4 else ("#d97706" if ttq_biz <= 24 else "#dc2626")
        elif isinstance(ttq_clock, (int, float)):
            ttq_s = f"{ttq_clock:.1f}h (clock)"
            ttq_color = "#64748b"
        else:
            ttq_s = "—"
            ttq_color = "#64748b"
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px"><strong>{_esc(r.get('lane') or '—')}</strong></td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('containers') or '—')}</td>
  <td style="padding:6px 8px;text-align:center">{teu}</td>
  <td style="padding:6px 8px">{_esc(r.get('carrier_quoted') or '—')}</td>
  <td style="padding:6px 8px;text-align:right;font-weight:600">{_esc(rate_s)}</td>
  <td style="padding:6px 8px">{_esc(signer)}</td>
  <td style="padding:6px 8px;white-space:nowrap;font-size:11px">{_esc(lonny_t)}</td>
  <td style="padding:6px 8px;white-space:nowrap;font-size:11px">{_esc(ol_t)}</td>
  <td style="padding:6px 8px;text-align:center;font-weight:600;color:{ttq_color}">{_esc(ttq_s)}</td>
  <td style="padding:6px 8px;text-align:center;font-weight:600">{_esc(_hours_since(r.get('response_timestamp')))}</td>
</tr>
"""
    # Totals row for TEU reconciliation
    body += f"""
<tr style="background:#e5e7eb;font-weight:bold;border-top:2px solid #7c3aed">
  <td style="padding:8px" colspan="2">TOTAL ({len(rows)} pending)</td>
  <td style="padding:8px;text-align:center">{total_teu}</td>
  <td style="padding:8px" colspan="7">&nbsp;</td>
</tr>
"""
    return f"""
<h2 style="color:#7c3aed;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #c4b5fd;padding-bottom:8px">⏳ Pending Hilmar Response ({len(rows)} requests · {total_teu} TEU)</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">Rows where OL has quoted but Lonny hasn't yet decided. Columns show the full clock: when Lonny asked → when OL quoted → how long that took (biz-hours) → how long it's been waiting. "Time to Quote" is OL's response speed on this row (sub-4h green / 4–24h amber / &gt;24h red). "Hours since OL quote" is the chase metric — candidates &gt;24h need follow-up.</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:#7c3aed;color:white">
    <th style="padding:8px;text-align:left;background-color:#7c3aed;color:#ffffff">Lane</th>
    <th style="padding:8px;text-align:left;background-color:#7c3aed;color:#ffffff">Equipment</th>
    <th style="padding:8px;text-align:center;background-color:#7c3aed;color:#ffffff">TEU</th>
    <th style="padding:8px;text-align:left;background-color:#7c3aed;color:#ffffff">Carrier Quoted</th>
    <th style="padding:8px;text-align:right;background-color:#7c3aed;color:#ffffff">Rate ($/container)</th>
    <th style="padding:8px;text-align:left;background-color:#7c3aed;color:#ffffff">Who Quoted (OL signer)</th>
    <th style="padding:8px;text-align:left;background-color:#7c3aed;color:#ffffff" title="When Lonny sent the RFQ (Pacific Time, Lonny's office)">Lonny Requested (PT)</th>
    <th style="padding:8px;text-align:left;background-color:#7c3aed;color:#ffffff" title="When OL responded with the rate (Eastern Time, OL's office)">OL Quoted (ET)</th>
    <th style="padding:8px;text-align:center;background-color:#7c3aed;color:#ffffff" title="Biz-hours from Lonny's RFQ to OL's rate response. Green ≤4h, amber 4–24h, red &gt;24h. Counts only OL business hours (8:30 AM – 5:30 PM ET weekdays).">Time to Quote</th>
    <th style="padding:8px;text-align:center;background-color:#7c3aed;color:#ffffff" title="Time elapsed since OL's quote arrived — chase candidates &gt;24h">Hours since OL quote</th>
  </tr>
  {body}
</table>
"""


def _loss_reason_mix_html(data) -> str:
    """Book-wide "Why we lost" mix chart — 30/60-day windows + actionable
    bucketing (rate_driven / etd_driven / ol_silent / other).

    Renders nothing when there are no losses in the window — prevents an
    empty section in newly-deployed environments. Outlook-safe HTML:
    plain ``<table>`` with width % per bar; no SVG, no flexbox, no
    linear-gradient (QC-045's lesson).

    Added 2026-05-31 per the audit's "loss-reason mix chart is the single
    most actionable client-facing piece you don't ship" finding.
    """
    requests = data.get("requests", []) or []
    if not requests:
        return ""

    mix_30 = core.aggregate_loss_reasons(requests, window_days=30)
    mix_60 = core.aggregate_loss_reasons(requests, window_days=60)
    if mix_30["total"] == 0 and mix_60["total"] == 0:
        return ""

    # Reason → display label + category color (Hilmar palette).
    # UNDIFFERENTIATED gets a neutral slate color (NOT Hilmar navy) — by
    # design those losses don't have a concrete signal to blame, so the
    # bar shouldn't look "actionable" in the same visual register as
    # PRICE or ETD_MISS. The label explicitly names this as the gap
    # ("needs investigation") so Michael's eye lands on it as a research
    # signal rather than a tag to push carriers on.
    _REASON_META = {
        "PRICE":             ("Price (rate-driven)",      "#0a2350"),   # Hilmar navy
        "ETD_MISS":          ("ETD missed",               "#1e40af"),   # blue
        "NO_RESPONSE":       ("OL didn't respond",        "#7c2d12"),   # rust
        "RESPONSE_NO_RATE":  ("OL acked but no rate",     "#9a3412"),
        "SEND_NO_BOOKING":   ("Send w/o MDOLX booking",   "#d97706"),   # amber-deep
        "UNDIFFERENTIATED":  ("Undifferentiated — needs investigation", "#64748b"),  # slate-500
        "QUOTED_NOT_BOOKED": ("Quoted, generic no-fit",   "#475569"),   # slate-600
        "COVERED":           ("Lonny covered w/ competitor", "#5b21b6"),
        "DRAFT_ONLY":        ("Booking draft-only",       "#475569"),
        "OTHER":             ("Other",                    "#94a3b8"),
    }
    _ACTIONABLE_META = {
        "rate_driven": ("Push carriers — rate-driven",  "#0a2350"),
        "etd_driven":  ("Push ops — ETD-driven",        "#1e40af"),
        "ol_silent":   ("Push OL — silent or late",     "#d97706"),
        "other":       ("Other",                         "#94a3b8"),
    }

    def _bar_row(label, count, total, color):
        pct = (count * 100.0 / total) if total else 0
        # Inline-styled table cell with width:N% as the bar fill, plus the
        # label/count to the right. Outlook respects this; flexbox does not.
        return (
            '<tr>'
            f'<td style="padding:6px 12px 6px 0;color:#0f172a;font-size:13px;'
            f'white-space:nowrap;font-weight:500">{_esc(label)}</td>'
            f'<td style="padding:6px 0;width:60%">'
            f'<table cellspacing="0" cellpadding="0" border="0" '
            f'style="width:100%;border-collapse:collapse">'
            f'<tr><td style="background:{color};color:#fff;padding:3px 8px;'
            f'border-radius:3px;font-size:12px;width:{max(pct, 4):.0f}%;'
            f'white-space:nowrap;font-variant-numeric:tabular-nums">'
            f'{count} &middot; {pct:.0f}%</td>'
            f'<td style="padding-left:6px"></td></tr></table>'
            f'</td></tr>'
        )

    def _window_block(label, mix):
        if mix["total"] == 0:
            return ""
        out = (
            f'<div style="margin:14px 0 6px 0;font-size:13px;color:#475569;'
            f'font-weight:600;letter-spacing:0.02em">'
            f'{label} &nbsp;&middot;&nbsp; <span style="color:#0f172a;'
            f'font-variant-numeric:tabular-nums">{mix["total"]} losses</span>'
            f'</div>'
            f'<table cellspacing="0" cellpadding="0" border="0" '
            f'style="width:100%;border-collapse:collapse">'
        )
        # By-reason bars (top 6 by count).
        for reason, count in mix["ranked"][:6]:
            label_str, color = _REASON_META.get(reason, (reason, "#94a3b8"))
            out += _bar_row(label_str, count, mix["total"], color)
        out += '</table>'
        # Actionable summary line.
        am = mix["actionable_mix"]
        chunks = []
        for key in ("rate_driven", "etd_driven", "ol_silent", "other"):
            if am[key] <= 0:
                continue
            tag_label, color = _ACTIONABLE_META[key]
            pct = am[key] * 100.0 / mix["total"]
            chunks.append(
                f'<span style="display:inline-block;margin:4px 8px 0 0;'
                f'padding:2px 8px;background:{color};color:#fff;border-radius:3px;'
                f'font-size:11px;font-variant-numeric:tabular-nums">'
                f'{_esc(tag_label)}: {am[key]} ({pct:.0f}%)</span>'
            )
        if chunks:
            out += (
                f'<div style="margin:8px 0 0 0;font-size:11px;color:#64748b;'
                f'line-height:1.8">{ "".join(chunks) }</div>'
            )
        return out

    html = (
        '<div style="margin:28px 0 8px 0">'
        '<h2 style="font-size:15px;color:#0a2350;margin:0 0 4px 0;'
        'font-weight:700;letter-spacing:-0.01em">Why We Lost &mdash; '
        'Loss-Reason Mix</h2>'
        '<div style="font-size:12px;color:#64748b;margin-bottom:4px">'
        'Where the deals went. Buckets tag the action that follows: '
        '<em>Push carriers</em> when rate-driven dominates, '
        '<em>Push ops</em> on ETD misses, '
        '<em>Push OL</em> when OL is silent.'
        '</div>'
    )
    html += _window_block("Last 30 days", mix_30)
    html += _window_block("Last 60 days", mix_60)
    html += "</div>"
    return html


def _trade_region_html(data, summary):
    """Volume by Trade Region — must reconcile to summary totals."""
    try:
        regions = core.aggregate_trade_regions(data.get("requests", []) or [])
    except Exception:
        return ""
    if not regions:
        return ""
    ordered = sorted(regions.values(), key=lambda m: m.get("teu_requested", 0), reverse=True)
    rows_html = ""
    alt = True
    for m in ordered:
        bg = "#ffffff" if alt else "#f8fafc"
        if m["region"] == "Unmapped":
            bg = "#fef2f2"
        alt = not alt
        rows_html += (
            f'<tr style="background:{bg}">'
            f'<td style="padding:6px 8px"><strong>{_esc(m["region"])}</strong></td>'
            f'<td style="padding:6px 8px;text-align:center">{m["requests"]}</td>'
            f'<td style="padding:6px 8px;text-align:center">{m["wins"]}</td>'
            f'<td style="padding:6px 8px;text-align:center">{m["quoted_lost"]}</td>'
            f'<td style="padding:6px 8px;text-align:center">{m["not_quoted"]}</td>'
            f'<td style="padding:6px 8px;text-align:center">{m["pending"]}</td>'
            f'<td style="padding:6px 8px;text-align:center">{m["teu_requested"]}</td>'
            f'<td style="padding:6px 8px;text-align:center">{m["teu_won"]}</td>'
            f'<td style="padding:6px 8px;text-align:center">{m["win_rate"]}%</td>'
            '</tr>'
        )
    sum_req = sum(m["requests"] for m in ordered)
    sum_w   = sum(m["wins"] for m in ordered)
    sum_ql  = sum(m["quoted_lost"] for m in ordered)
    sum_nq  = sum(m["not_quoted"] for m in ordered)
    sum_pen = sum(m["pending"] for m in ordered)
    sum_teu_req = sum(m["teu_requested"] for m in ordered)
    sum_teu_won = sum(m["teu_won"] for m in ordered)
    rows_html += (
        '<tr style="background:#e2e8f0;font-weight:bold;border-top:2px solid #0a2350">'
        '<td style="padding:6px 8px">TOTAL</td>'
        f'<td style="padding:6px 8px;text-align:center">{sum_req}</td>'
        f'<td style="padding:6px 8px;text-align:center">{sum_w}</td>'
        f'<td style="padding:6px 8px;text-align:center">{sum_ql}</td>'
        f'<td style="padding:6px 8px;text-align:center">{sum_nq}</td>'
        f'<td style="padding:6px 8px;text-align:center">{sum_pen}</td>'
        f'<td style="padding:6px 8px;text-align:center">{sum_teu_req}</td>'
        f'<td style="padding:6px 8px;text-align:center">{sum_teu_won}</td>'
        '<td style="padding:6px 8px;text-align:center">—</td>'
        '</tr>'
    )
    # Reconciliation note
    recon = (f'reconciles to summary: {summary.get("total_entries",0)} reqs / '
             f'{summary.get("wins",0)} W / {summary.get("quoted_lost",0)} Q&L / '
             f'{summary.get("not_quoted",0)} NQ / {summary.get("pending_hilmar",0)} P')
    # Period scope — pull from the data's first/last request_date.
    # Per Michael 2026-05-18 "what does total mean? number of bookings? TEUs?"
    # — every column needs explicit units + period.
    reqs = data.get("requests", []) or []
    dates = sorted(r.get("request_date", "") for r in reqs if r.get("request_date"))
    period_str = f"{dates[0]} – {dates[-1]}" if dates else "all time"
    return f"""
<h2 style="color:#0a2350;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #cbd5e1;padding-bottom:8px">🌐 Volume by Trade Region — {_esc(period_str)}</h2>
<p style="margin:0 0 4px;font-size:11px;color:#64748b">Destinations grouped by trade region. All counts and TEU are cumulative across the period shown above. Totals reconcile to summary KPIs.</p>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">{_esc(recon)}</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:#0a2350;color:white">
    <th style="padding:8px;text-align:left">Region</th>
    <th style="padding:8px;text-align:center" title="Number of distinct requests from Lonny">Requests<br><span style="font-weight:400;font-size:10px;opacity:0.85">(#)</span></th>
    <th style="padding:8px;text-align:center" title="Wins — booked + confirmed via MDOLX">Wins<br><span style="font-weight:400;font-size:10px;opacity:0.85">(#)</span></th>
    <th style="padding:8px;text-align:center" title="Quoted &amp; Lost — OL gave a rate, Lonny went elsewhere">Q&amp;L<br><span style="font-weight:400;font-size:10px;opacity:0.85">(#)</span></th>
    <th style="padding:8px;text-align:center" title="Not Quoted — OL never responded or had no rate">NQ<br><span style="font-weight:400;font-size:10px;opacity:0.85">(#)</span></th>
    <th style="padding:8px;text-align:center" title="Pending — awaiting Lonny's decision">Pending<br><span style="font-weight:400;font-size:10px;opacity:0.85">(#)</span></th>
    <th style="padding:8px;text-align:center" title="Sum of TEU requested by Lonny (all statuses)">TEU<br>Requested</th>
    <th style="padding:8px;text-align:center" title="Sum of TEU on confirmed WIN rows only">TEU<br>Won</th>
    <th style="padding:8px;text-align:center" title="Win Rate = Wins / Requests">Win<br>Rate</th>
  </tr>
  {rows_html}
</table>
"""


FOOTER_HTML = """
<div style="background:#f0f4f8;border:1px solid #cbd5e1;border-radius:8px;padding:16px;margin-bottom:20px">
  <h3 style="margin:0 0 8px;color:#0a2350;font-size:14px">📎 ATTACHED FILES:</h3>
  <p style="margin:4px 0;font-size:12px">• <b>hilmar-dashboard.html</b> — Open in any browser (works mobile + desktop, no software needed)</p>
  <p style="margin:4px 0;font-size:12px">• <b>hilmar-report.pdf</b> — Printable report</p>
  <h3 style="margin:12px 0 8px;color:#0a2350;font-size:14px">📖 DASHBOARD TAB GUIDE:</h3>
  <p style="margin:4px 0;font-size:12px">• 📊 <b>Summary</b> — KPIs, confirmed wins with MDOLX, not-quoted requests</p>
  <p style="margin:4px 0;font-size:12px">• ⏱️ <b>Turnaround Timeline</b> — Lonny request time (PT) vs OL response (ET), business-hours adjusted</p>
  <p style="margin:4px 0;font-size:12px">• 📅 <b>Dates: Requested vs Offered</b> — Lonny's cutoff/ETD/ETA vs OL's offer side-by-side</p>
  <p style="margin:4px 0;font-size:12px">• 🚢 <b>Carriers &amp; Lanes</b> — Win/loss rates with lane-level breakdowns, TEU &amp; equipment for rate negotiations</p>
  <p style="margin:4px 0;font-size:12px">• 🔍 <b>QC</b> — Data quality checks and warnings</p>
</div>
<div style="border-top:2px solid #e5e7eb;padding-top:12px;margin-top:20px;text-align:center">
  <p style="font-size:11px;color:#6b7280">Auto-generated from the Hilmar Shipment Tracker</p>
  <p style="font-size:11px;color:#6b7280">Files also on OneDrive: IdealX → Hilmar folder</p>
</div>
"""


def _preheader_html(new_req, ol_resp, wins_today, pending_ol, pending_hil):
    """Hidden inbox-preview text — the line the recipient sees in the inbox
    list BEFORE opening (added 2026-06-13). Without it, mail clients scrape
    the first visible text (the logo alt / range label) — uninformative.
    This puts the day's verdict in the preview itself.

    The trailing run of zero-width-non-joiners + nbsp is the standard
    preheader-stuffing trick: it stops the client from spilling body text
    ('2026-04-01 – ...') into the preview after our line."""
    bits = []
    if new_req:
        bits.append(f"{new_req} new RFQ{'s' if new_req != 1 else ''}")
    if ol_resp:
        bits.append(f"{ol_resp} quote{'s' if ol_resp != 1 else ''}")
    if wins_today:
        bits.append(f"{wins_today} win{'s' if wins_today != 1 else ''}")
    waiting = pending_ol + pending_hil
    if waiting:
        bits.append(f"{waiting} pending")
    summary = " · ".join(bits) or "No new activity on the previous business day"
    return (
        f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
        f'font-size:1px;line-height:1px;color:#f5f7fa;opacity:0">{_esc(summary)}'
        + ("‌ " * 60) + "</div>"
    )


def _verdict_strip_html(report_label, new_req, ol_resp, wins_today,
                        pending_ol, pending_hil, summary):
    """Top-of-email stat band — the 5-second glance (added 2026-06-13 per
    Michael "the 5-second phone glance fails ... put the verdict at the
    top"). The old layout buried the day's numbers at the bottom of the
    cover block. This echoes the dashboard's KPI tiles so the two surfaces
    rhyme. Built as an Outlook-safe presentation table (no grid/flex);
    cells wrap on narrow screens because the table is width:100% with
    min-width cells."""
    T = B.THEME
    win_rate = summary.get("win_rate")
    wr_s = f"{win_rate:.0f}%" if isinstance(win_rate, (int, float)) else "—"
    cells = [
        ("New RFQs", str(new_req), T["brand_blue"]),
        ("Quotes", str(ol_resp), T["ink"]),
        ("Wins", str(wins_today), T["win"]),
        ("Pending OL", str(pending_ol), T["pending_ol"]),
        ("Pending Hilmar", str(pending_hil), T["pending"]),
        ("Win Rate · PTD", wr_s, T["brand_blue"]),
    ]
    tds = ""
    for label, value, color in cells:
        tds += (
            f'<td style="padding:10px 6px;text-align:center;vertical-align:top;'
            f'border-right:1px solid {T["rule_soft"]}">'
            f'<div style="font-size:24px;font-weight:800;line-height:1;'
            f'letter-spacing:-0.5px;color:{color};{EMAIL_TNUM}">{_esc(value)}</div>'
            f'<div style="font-size:9.5px;font-weight:600;text-transform:uppercase;'
            f'letter-spacing:0.4px;color:{T["muted_soft"]};margin-top:5px">{_esc(label)}</div>'
            f'</td>'
        )
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" '
        f'style="width:100%;border-collapse:collapse;background:{T["surface"]};'
        f'border:1px solid {T["rule"]};border-radius:10px;overflow:hidden;'
        f'margin:0 0 18px 0;{EMAIL_TNUM}">'
        f'<tr><td colspan="6" style="padding:7px 12px;background-color:{T["rule_soft"]};'
        f'font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.6px;'
        f'color:{T["muted"]};border-bottom:1px solid {T["rule"]}">'
        f'At a glance — {_esc(report_label)}</td></tr>'
        f'<tr>{tds}</tr></table>'
    )


def build_body(data, cfg):
    now_et = datetime.now(timezone.utc).astimezone(core.ET)
    # Email REPORTS on the previous business day, not "now". See _report_date
    # docstring for rationale (10 AM ET fire = before Lonny's PT office opens).
    report_date = _report_date(now_et)
    report_label = _report_label(report_date)             # 'Wednesday May 6, 2026'
    report_short = _fmt_date(datetime.combine(report_date, datetime.min.time()), "%b %-d, %Y")
    date_range = data.get("date_range") or f"{cfg.get('data_range', {}).get('start', 'start')} – {report_short}"
    updated_label = _fmt_date(now_et, "%B %-d, %Y at %-I:%M %p ET")

    new_req, ol_resp, status_ch, pending = _today_events(data, report_date)
    week_rows = _week_rows(data)
    carrier_rows = _carrier_rows(data)
    winning_lanes = _winning_lane_rows(data)
    losing_lanes = _losing_lane_rows(data)
    nq_rows = _not_quoted_rows(data)  # 14-day display window only
    nq_all = _not_quoted_aggregate(data)  # full tally for rate-negotiation context
    nq_total_count = len(nq_all)
    nq_total_teu = sum(int(r.get("teu_requested") or 0) for r in nq_all)
    pend_rows = _pending_rows(data)

    _summary = data.get("summary", {}) or {}
    _pol = [r for r in pend_rows if core.pending_substate(r) == "PENDING_OL"]
    _phil = [r for r in pend_rows if core.pending_substate(r) == "PENDING_HILMAR"]
    _wins_today = sum(1 for (_r, _h) in status_ch if _h.get("to") == "WIN")

    html_body = _preheader_html(len(new_req), len(ol_resp), _wins_today,
                                len(_pol), len(_phil))
    html_body += _header_html(report_label, date_range, updated_label)
    html_body += _verdict_strip_html(report_label, len(new_req), len(ol_resp),
                                     _wins_today, len(_pol), len(_phil), _summary)
    html_body += _today_block_html(report_label, new_req, ol_resp, status_ch, pending)
    html_body += _kpi_block_html(data.get("summary", {}) or {}, requests=data.get("requests", []) or [], report_date=report_date)
    # Loss-reason mix — the "why we lost" lens. Renders nothing when
    # there are no losses in the 30/60-day windows.
    html_body += _loss_reason_mix_html(data)
    html_body += _week_block_html(week_rows)
    html_body += _carrier_block_html(carrier_rows)
    html_body += _trade_region_html(data, data.get("summary", {}) or {})
    html_body += _winning_lanes_html(winning_lanes)
    html_body += _losing_lanes_html(losing_lanes)
    html_body += _nq_html(nq_rows, total_nq=nq_total_count, teu_total=nq_total_teu)
    html_body += _pending_ol_html(_pol)
    html_body += _pending_html(_phil)
    html_body += FOOTER_HTML
    html_body += "</div></div>"
    return html_body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args()
    cfg = core.load_config(args.config)
    data = json.loads(Path(cfg["paths"]["data"]).read_text())
    body = build_body(data, cfg)
    subject = build_subject(data, cfg)
    body_path = Path(cfg["paths"]["email_body"])
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(body, encoding="utf-8")
    subject_path = body_path.parent / "email-subject.txt"
    subject_path.write_text(subject, encoding="utf-8")
    print(f"✅ Email body: {len(body):,} bytes -> {body_path}")
    print(f"✅ Email subject: {subject!r} -> {subject_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
