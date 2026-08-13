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
from collections import Counter, defaultdict
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


def _et_date(s):
    """The ET calendar date of an ISO timestamp (date-only strings pass
    through as-is — they carry no timezone to convert).

    Post-#112 review: _iso_date slices the UTC calendar date, but the report
    day is an ET business day — an event at 9:30 PM EDT is already the NEXT
    day in UTC, so UTC-sliced comparisons pushed evening events (win
    confirmations, requests, quotes) into the wrong day bucket. Every
    timestamp-vs-report-day comparison in this module goes through here so
    the Won tile, won_later, and the What Happened sections all shift (or
    don't) together — fixing one comparison alone would just relocate the
    self-contradiction between sections.
    """
    if not s:
        return None
    if len(s) <= 10 or "T" not in s:
        return _iso_date(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(core.ET).date()
    except Exception:
        return _iso_date(s)


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
    """Return the date this email REPORTS ON — the PRIOR business day.

    Michael 2026-07-21: "get rid of the recaps and just do daily at 8am est for
    the day before." The fire runs ~8 AM ET each weekday and reports the
    business day that just finished, so its quotes/bookings are complete. The
    production fire sets HILMAR_REPORT_WINDOW=previous, so
    core.report_business_day yields:
      Tue–Fri: report = the prior weekday (Mon..Thu)
      Mon:     report = Friday (today − 3)
    core.py's default window stays "current"; the fire's env flips it to
    "previous". The email header + section labels say "the prior business day".
    """
    return core.report_business_day(now_et)


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

def _win_landed(r, h) -> bool:
    """Did this →WIN transition actually STICK?

    THE single definition of "a win happened here", shared by the KPI tile
    (`_today_summary`) and the What-Happened block (`_today_block_html`).
    They used to disagree: the block counted any →WIN transition dated today,
    the tile also required the row to still BE a WIN. A row promoted on a
    send-signal and later re-decided away (aged to SEND_NO_BOOKING, or held as
    MDOLX_NO_SEND) satisfied one and not the other, so a single email reported
    both "0 wins" and "1 wins" for the same day.

    A transition that was subsequently reversed is not a win. Rendering it as
    one is how the report ends up arguing with itself.
    """
    return h.get("to") == "WIN" and (r.get("status") or "").upper() == "WIN"


def _today_events(data, today_date):
    """Buckets the activity for the 'What Happened Today' block."""
    new_requests = []
    ol_responses = []
    status_changes = []
    pending_today = []

    for r in data.get("requests", []):
        req_d = _et_date(r.get("request_date") or r.get("request_timestamp"))
        resp_d = _et_date(r.get("response_timestamp"))
        # Standalone bookings (stand_*) are neither a Lonny ask nor a rate
        # quote — rendering them in New Requests / OL Responses produced the
        # 2026-07-09 "Lane unresolved" junk rows (no Lonny timestamp, no rate,
        # 0 TEU). They surface in STATUS CHANGES instead (the builder writes a
        # PENDING→WIN history entry), which is the honest event: "a booking
        # confirmed today".
        _is_standalone = core.has_no_rfq_chain(r)
        if req_d == today_date and not _is_standalone:
            new_requests.append(r)
        if resp_d == today_date and not _is_standalone:
            ol_responses.append(r)
        # status changes today
        for h in (r.get("status_history") or []):
            at = h.get("at")
            if at and _et_date(at) == today_date and h.get("from") and h.get("to") and h["from"] != h["to"]:
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
        if st == "WIN":
            b["won"] += 1
            b["teu_won"] += int(r.get("teu_won") or r.get("teu_requested") or 0)
            mref = r.get("mdolx_ref")
            if mref:
                b["mdolx"].append(mref)
        elif st == "PENDING":
            b["pending"] += 1
        # core.is_not_quoted, NOT loss_reason. This is the 8-week rollup — the
        # "NQ 0 / Q&L 1" line in the finding-#17 evidence — and it was the one
        # NQ site in this file the first pass missed. A RESPONSE_NO_RATE row
        # (OL acknowledged the RFQ but sent no rate, so quoted=False) is NQ to
        # aggregate_summary and was Q&L here, in the same email.
        elif core.is_not_quoted(r):
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
        elif st == "LOSS" and not core.is_not_quoted(r):
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
        "total": 0, "teu_won": 0, "teu_lost": 0, "teu_req": 0, "carriers": set(),
    })
    for r in data.get("requests", []):
        lane = r.get("lane") or f"{r.get('origin','?')} → {r.get('destination','?')}"
        b = lanes[lane]
        b["total"] += 1
        # Total TEU up for offer on the lane (all statuses) — without it the
        # win-rate % floats with no denominator (Michael 2026-07-09: "shows
        # percentages but doesn't show total teus and shipments up for offer").
        b["teu_req"] += int(r.get("teu_requested") or 0)
        st = r.get("status")
        if st == "WIN":
            b["won"] += 1
            b["teu_won"] += (r.get("teu_won") or r.get("teu_requested") or 0)
            if r.get("carrier_won"):
                b["carriers"].add(r["carrier_won"])
        elif st == "PENDING":
            b["pending"] += 1
        elif st == "LOSS":
            # Same shared predicate as every other NQ site — this one feeds
            # the Winning/Losing lane tables, so a RESPONSE_NO_RATE row used
            # to be reported as a competitive loss on a lane it was never
            # quoted on, and counted into that lane's teu_lost.
            if core.is_not_quoted(r):
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


# ONE definition of "Not Quoted", shared with core.aggregate_summary and
# core.aggregate_trade_regions: core.is_not_quoted(r) — a LOSS that was never
# quoted, whatever the reason.
#
# This file used to test `loss_reason == "NO_RESPONSE"` instead, which is a
# DIFFERENT set: RESPONSE_NO_RATE (OL acknowledged the RFQ but sent no rate)
# is quoted=False, so core counted it NQ while these three call sites counted
# it Q&L. One row then split across five contradicting numbers in the SAME
# email — "Not Quoted: 1" in the KPI tile beside "NQ 0 / Q&L 1" in the 8-week
# rollup and in Volume by Trade Region, an NQ detail section rendering zero
# rows under a tile claiming one, and a carrier charged a Q&L loss while
# showing 0 quotes (win-rate denominator 0). loss_reason is now purely the
# WHY column; it never decides the bucket.
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
        if core.is_not_quoted(r):
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
    return [r for r in data.get("requests", []) if core.is_not_quoted(r)]


def _pending_rows(data):
    rows = [r for r in data.get("requests", []) if r.get("status") == "PENDING"]
    rows.sort(key=lambda r: (r.get("request_date") or ""))
    return rows


# ─────────────────────────────────────────────────────────────────────
# HTML builders
# ─────────────────────────────────────────────────────────────────────

# 2026-05-19 PM 4th pass: Outlook strips CSS linear-gradient — it left text
# rendered without its background, which collapsed the visual hierarchy (and
# on header rows made white text invisible on white). The workaround was a
# solid `background-color:` listed BEFORE the gradient so Outlook saw the
# solid one. 2026-08-04: the gradient is gone entirely — the restyle wants a
# flat header anyway, so the client that renders it and the client that
# strips it now agree by construction rather than by fallback. QC-045 still
# guards the rule for any header that comes later.
HEADER_BG_SOLID = B.HILMAR_NAVY

# ── Document restyle ──────────────────────────────────────────────────────
# Michael 2026-07-22 on an internal OL comparison doc: "gorgeous" → "i like
# it all" (dashboard, PDF, email). #138 did the dashboard; these are the same
# branding.DOC_* tokens so the email body cannot drift from it.
#
# EMAIL IS NOT THE WEB. Desktop Outlook renders HTML with Word's engine: no
# CSS custom properties, no flex, no grid, and <style> blocks are ignored
# outside @media. So the tokens are interpolated into INLINE styles as
# literal hex — one source in Python, resolved before it ever reaches a mail
# client. Do not "simplify" these to var().
DOC_PAPER, DOC_CARD = B.DOC_PAPER, B.DOC_CARD
DOC_INK, DOC_MUTED, DOC_LINE = B.DOC_INK, B.DOC_MUTED, B.DOC_LINE
DOC_TH_BG = B.DOC_TH_BG

# Solid navy bar, white text — REVERTED 2026-08-07 to what shipped before the
# 08-04 restyle (#1e3a5f, kept verbatim rather than remapped to a token).
#
# The restyle made this a muted uppercase label on a near-white ground with a
# hairline under it. That was a real improvement in the abstract — the three
# different saturated bars did shout over the data — but it removed the thing
# a reader actually navigates by. Michael asked for the old format back twice:
# 2026-08-06 ("go back to the older format that shows pending hilmar pending
# ol then what changed") and again on 08-07 ("still using the new formatting
# which i told you to go back to"). The first time I restored the SECTION rule
# and left the table headers flat, which is why it took a second ask.
#
# The document palette stays everywhere else — status pills, KPI accents,
# section rules. This is the one element where the loud version was doing a
# job the quiet version does not.
TH_STYLE = ("padding:6px 8px;background-color:#1e3a5f;background:#1e3a5f;"
            "color:#ffffff;font-size:11px;font-weight:600;text-align:left;"
            "border-bottom:1px solid #1e3a5f")
# Section rule: ink text over an INK rule — the same 2px ink rule TH_STYLE
# uses, so a section head and a table head are the same landmark at two
# scales.
#
# It was a 1px DOC_LINE hairline for two days, and that was a mistake in the
# 08-04/08-05 restyle. Replacing the solid navy bars was right — they shouted
# over the data — but a hairline the colour of the card ground is not a
# quieter landmark, it is an absent one. Michael 2026-08-06: "bad format.. go
# back to the older format that shows pending hilmar pending ol then what
# changed". Section ORDER and heading TEXT never changed (verified byte-for-
# byte across the repo's whole history); what he lost were the anchors he
# scanned for. Restoring weight is the fix; reverting the palette is not.
H2_STYLE = (f"color:{DOC_INK};font-size:15px;margin:22px 0 10px;"
            f"border-bottom:2px solid {DOC_INK};padding-bottom:7px;font-weight:700")

EMAIL_FONT_STACK = B.DOC_SANS_STACK
EMAIL_MONO_STACK = B.DOC_MONO_STACK
EMAIL_TNUM = B.DOC_TNUM

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
    # Mobile overrides (Michael 2026-07-01 "reading email on phone is poorly
    # formatted"): the layout is sized for desktop Outlook (1040px container,
    # 7-11 column fixed tables), so a phone either crushes the columns or
    # scales the whole email to unreadable. Phones' clients (iOS Mail, Gmail
    # app) honor <style> @media; desktop Outlook's Word engine ignores it
    # entirely — so these rules ONLY change the phone rendering:
    #   .hx-wrap  full-bleed container (no wasted margin at 390px)
    #   .hx-pad   tighter padding + ONE horizontal scroll surface for anything
    #             wider than the screen (-webkit-overflow-scrolling for momentum)
    #   .hx-kpi   KPI tiles stack FULL-WIDTH, one per row. display:block on the
    #             td is the only stacking that renders reliably in iOS Mail —
    #             the first ship used inline-block/50% for a 2-up grid and iOS
    #             collapsed the cells to narrow strips (tds pulled out of table
    #             layout shrink-wrap unpredictably; Michael's 2026-07-02
    #             screenshot). Pair with .hx-kpi-card height:auto so the
    #             desktop-locked 88px can't clip/overlap the wrapped text.
    #   .hx-data  data tables KEEP readable column geometry (min-width) and
    #             scroll sideways inside .hx-pad instead of squishing to
    #             3-char columns. KPI-tile tables are deliberately NOT .hx-data
    #             (stacking handles them; a min-width would fight it).
    mobile_style = """
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
    # NO CDN FONT LINK. The old header pulled Inter from fonts.googleapis.com
    # behind an mso conditional. This is email: remote content trips Outlook's
    # "download pictures?" bar and OL's proxy, and the same report should look
    # the same on every desk whether or not the fetch succeeds. The local
    # stack in branding.DOC_SANS_STACK renders deterministically.
    return f"""
{mobile_style}
<div style="background-color:{DOC_PAPER};padding:18px 0">
<div class="hx-wrap" style="max-width:1040px;margin:0 auto;background-color:{DOC_CARD};border:1px solid {DOC_LINE};border-radius:8px;overflow:hidden;font-family:{EMAIL_FONT_STACK};{EMAIL_TNUM}">
  <div style="padding:14px 28px;background-color:{HEADER_BG_SOLID};color:{B.DOC_CARD};font-family:{EMAIL_FONT_STACK}">
    {logo_block}
    <h1 style="margin:0;font-size:22px;font-weight:700;letter-spacing:-0.3px;font-family:{EMAIL_FONT_STACK}">{'' if logo_html else '🚢 '}Hilmar Ingredients — Daily Shipment Tracker</h1>
    <p style="margin:4px 0 0;font-size:14px;opacity:0.9;font-family:{EMAIL_FONT_STACK}">Reporting {_esc(today_label)} — the prior business day · {_esc(range_label)} | Updated: {_esc(updated_label)}</p>
  </div>
  <div class="hx-pad" style="padding:20px 28px;font-family:{EMAIL_FONT_STACK};{EMAIL_TNUM}">
"""


def _status_change_pill(status, r, other_status=None):
    """Status-change pill in operational 'who do we chase' terms, so a
    transition reads as the real-world wait — not the internal enum.

    Michael 2026-07-15: "status is waiting ol quote, then after quote is
    pending hilmar response." A rate response must therefore read
    'PENDING OL → PENDING HILMAR', never 'PENDING HILMAR → QUOTED'.

    Each end is resolved from the TRANSITION (its direction is unambiguous),
    NOT the row's CURRENT substate — the current substate describes where the
    row sits NOW, which mislabels the BEFORE end of the move (the old bug: a
    just-quoted row is now PENDING_HILMAR, so the pre-quote 'from' end wrongly
    showed PENDING HILMAR):
      • QUOTED end            → PENDING_HILMAR. OL delivered a rate; the ball is
        now in Hilmar's court. QUOTED is only ever a transition TARGET, so it is
        always this post-quote 'pending Hilmar' state.
      • PENDING → into QUOTED → PENDING_OL. OL had not quoted yet → chase OL.
      • PENDING → into an outcome (WIN/LOSS/…) → the wait that was resolved:
        PENDING_HILMAR if the row was quoted, else PENDING_OL (an NQ that OL
        never answered)."""
    s = (status or "").upper()
    o = (other_status or "").upper()
    if s == "QUOTED":
        return V.pending_pill("PENDING_HILMAR")
    if s != "PENDING":
        return V.status_pill(status)
    if o == "QUOTED":
        sub = "PENDING_OL"
    elif o in ("WIN", "LOSS", "BOOKING_CANCELED"):
        sub = "PENDING_HILMAR" if r.get("quoted") else "PENDING_OL"
    else:
        cur = (r.get("status") or "").upper()
        sub = core.pending_substate(r) if cur == "PENDING" else "PENDING_HILMAR"
    return V.pending_pill(sub)


def undated_quotes(data) -> list:
    """Rows carrying a real quote that no daily report can ever date.

    OL-USA RESPONSES buckets on response_timestamp (_today_events). A row with
    an ol_rate or a carrier_quoted but no response_timestamp has `resp_d is
    None`, so it matches no day and is invisible to that section forever —
    while PENDING HILMAR, which is current state and not windowed, keeps
    showing its quote. That is what made the 2026-07-29 report look
    self-contradictory rather than plainly broken.

    STANDALONE BOOKINGS ARE EXCLUDED, and that exclusion is the whole
    difference between a useful check and one that cries wolf. A `stand_*` row
    is a booking seen without any rate-response email; ingest.py:887 sets
    response_timestamp to None DELIBERATELY there, to signal "we never saw a
    rate response" rather than polluting it with the booking time. Counting
    those as defects would flag correct behaviour — 5 of the 29 rows found on
    2026-07-30 were exactly that.

    Kept as a module-level function, separate from _today_events, so the
    latter keeps its four-tuple contract; widening that broke every caller and
    test that unpacks it, for no benefit.
    """
    out = []
    for r in data.get("requests", []):
        if core.has_no_rfq_chain(r):
            continue
        if r.get("response_timestamp"):
            continue
        # core.has_quote_evidence, NOT `ol_rate is not None`. qc_selfheal's
        # NQ-contamination heal writes the STRING "Not Quoted" into ol_rate,
        # which is not None, so the old spelling counted rows with no quote at
        # all — inflating this note while QC-077, fixed in #148, excluded them.
        # Two numbers off one dataset, and the docstring of
        # test_undated_quotes_excludes_standalones_like_the_check_does had
        # already written down that they must agree.
        if core.has_quote_evidence(r):
            out.append(r)
    return out


def _undated_quotes_note(undated) -> str:
    """Say, in the report, how many real quotes it cannot date — and so is not
    showing above.

    OL-USA RESPONSES is bucketed on response_timestamp (see _today_events). A
    row with an ol_rate or a carrier_quoted but no response_timestamp is
    invisible to that section on EVERY day, while still rendering its quote
    under PENDING HILMAR. On 2026-07-30 that was 29 of 315 rows, and the
    newest response_timestamp in the whole dataset was 2026-07-23 — the
    section had been silently empty since Jul 24 and the report never said so.

    It is NOT the report's job to invent a date for these. Synthesising one
    would fabricate turnaround timing and corrupt the time-to-quote metrics.
    An empty section that is honest about being incomplete is worth far more
    than one that looks complete and isn't. QC-077 errors on the same
    condition so it gets fixed at the ingest end.

    WHERE THEY SIT IS COMPUTED, NOT ASSERTED (2026-08-13). This banner used to
    end "They appear under PENDING HILMAR" as a flat statement, which was true
    only while an undated quote could never age: decide_status had no clock on
    such a row, so it held PENDING at any age. Now that those rows age off
    Lonny's request, most are Quoted & Lost and the sentence had become a
    confident pointer to the wrong section — the failure mode this banner
    exists to prevent, committed by the banner itself. Read the statuses.

    Through core.display_status, because this repo stores status in two forms
    (LEGACY LOSS+quoted vs STRICT Q&L) and a raw r["status"] read would bucket
    every legacy row as "elsewhere" — a sentence that is vague in exactly the
    case it most needs to be specific.
    """
    if not undated:
        return ""
    n = len(undated)
    by_status = Counter(core.display_status(r) for r in undated)
    _SECTIONS = [("PENDING", "PENDING HILMAR"), ("Q&L", "Quoted &amp; Lost"),
                 ("WIN", "the wins"), ("NQ", "Not Quoted")]
    parts = [f"{by_status[k]} under {label}" for k, label in _SECTIONS
             if by_status[k]]
    other = n - sum(by_status[k] for k, _ in _SECTIONS)
    if other:
        parts.append(f"{other} elsewhere")
    where = (f'{"It appears" if n == 1 else "They appear"} '
             + ", ".join(parts) + ". ") if parts else ""
    return (
        f'<p style="margin:4px 0 0;font-size:11px;color:{B.DOC_WARN};'
        f'background:{B.DOC_WARN_BG};border-left:3px solid {B.DOC_WARN};padding:6px 9px">'
        f'⚠️ {n} further quote{"" if n == 1 else "s"} '
        f'{"is" if n == 1 else "are"} recorded with a rate or carrier but no '
        f'response time, so {"it" if n == 1 else "they"} cannot be dated and '
        f'{"is" if n == 1 else "are"} not counted above. '
        f'{where}'
        f'See QC-077 in the audit.</p>'
    )


def _today_block_html(report_label, new_req, ol_resp, status_ch, pending,
                      undated_quotes=()):
    """Render the 'What Happened on <day>' block. The label `report_label` is
    TODAY's now-complete business day (see _report_date) — the ~6 PM ET evening
    fire runs AFTER Lonny's PT office has closed for the day, so today's data
    window is fully populated by send time.

    2026-05-19 PM (Michael "the current report is a mess. formatted poorly...
    the email should have clear tables with proper formatting and names of
    whom responded"): replaced bullet-list rendering with proper HTML tables.
    Every subsection has explicit column headers; OL-USA responses surface
    `ol_responder_signer` (the human at MBD who composed the rate response).
    """
    # Shared table CSS — Outlook-safe inline styles, no flexbox or grid.
    _TABLE_OPEN = (
        '<table role="presentation" cellpadding="0" cellspacing="0" class="hx-data" '
        'style="width:100%;border-collapse:collapse;font-size:12px;'
        f'margin:4px 0 14px 0;border:1px solid {B.DOC_LINE}">'
    )
    # Fixed-layout variant for the WIDE tables (OL Responses = 11 cols, Status
    # Changes = 7 cols). Without table-layout:fixed + an explicit <colgroup>,
    # content-driven sizing crams the columns and truncates long cells (the
    # "MBD rate re…" squish). The Word/Outlook engine honors table-layout:fixed
    # and <col width>, so columns get controlled widths and long text WRAPS
    # (cells default to white-space:normal) instead of overflowing.
    _TABLE_OPEN_FIXED = (
        '<table role="presentation" cellpadding="0" cellspacing="0" class="hx-data" '
        'style="width:100%;border-collapse:collapse;font-size:12px;'
        f'table-layout:fixed;margin:4px 0 14px 0;border:1px solid {B.DOC_LINE}">'
    )

    def _colgroup(*widths):
        return "<colgroup>" + "".join(
            f'<col style="width:{w}%">' for w in widths) + "</colgroup>"
    _TH_STYLE = (
        f'style="{TH_STYLE};text-align:left"'
    )
    _TD_STYLE = (
        f'style="padding:5px 8px;font-size:12px;color:{DOC_INK};'
        f'border-bottom:1px solid {DOC_LINE}"'
    )
    _EMPTY_ROW = (
        f'<tr><td colspan="99" {_TD_STYLE.replace("text-align:left", "text-align:center")}>'
        f'<em style="color:{B.DOC_MUTED}">No activity</em></td></tr>'
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
            color = B.DOC_GOOD if tb <= 4 else (B.DOC_WARN if tb <= 24 else B.DOC_BAD)
            suffix = " (AH)" if r.get("after_hours_request") else ""
            return f"{tb:.1f}h{suffix}", color
        return "—", B.DOC_MUTED

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
            _ft_req = r.get("free_time_requested")
            if _ft_req:
                requested_eta = f"{requested_eta} · {_ft_req}" if requested_eta != "—" else _ft_req
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
        f'<th {_TH_STYLE} title="Lonny\'s requested ETA / ETD + any free-time ask (e.g. 14d demurrage) stated in the RFQ">Lonny Asked For</th>'
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
                f'<td {_TD_STYLE};font-size:11px>{_esc(lonny_t)}</td>'
                f'<td {_TD_STYLE};font-size:11px>{_esc(ol_t)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")};font-weight:600;color:{ttq_color}>{_esc(ttq_s)}</td>'
                f'<td {_TD_STYLE};font-size:11px>{_esc(etd_off)}</td>'
                f'<td {_TD_STYLE};font-size:11px>{_esc(eta_off)}</td></tr>'
            )
    else:
        resp_rows = _EMPTY_ROW
    resp_table = (
        # 11 cols: Lane Equip TEU Carrier Rate Signer LonnyPT OLet TTQ ETD ETA
        f'{_TABLE_OPEN_FIXED}'
        f'{_colgroup(15, 8, 5, 11, 9, 11, 11, 11, 6, 6.5, 6.5)}'
        f'<thead><tr>'
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
    _sc_pill = _status_change_pill

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
            # A →WIN that did NOT stick is still a real event worth showing —
            # but it must not read as a win, or the table contradicts the win
            # count directly above it. Say what actually happened instead.
            if h.get("to") == "WIN" and not _win_landed(r, h):
                _now_is = (r.get("status") or "?").upper()
                _why = r.get("loss_reason") or r.get("reason_detail") or ""
                reason = (f"{reason} — REVERSED, now {_now_is}"
                          f"{(' (' + str(_why)[:60] + ')') if _why else ''}").strip(" —")
            req_date = r.get('request_date') or '—'
            carrier = r.get("carrier_won") or r.get("carrier_quoted") or "—"
            rate = r.get("ol_rate")
            rate_s = f"${rate:,.0f}" if isinstance(rate, (int, float)) else "—"
            sc_rows += (
                f'<tr><td {_TD_STYLE}><strong>{_esc(lane)}</strong></td>'
                f'<td {_TD_STYLE};font-size:11px>{_esc(str(cont_label))} / {teu} TEU</td>'
                f'<td {_TD_STYLE}>{_esc(req_date)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:center")}>'
                f'{_sc_pill(h["from"], r, h["to"])} → {_sc_pill(h["to"], r, h["from"])}</td>'
                f'<td {_TD_STYLE}>{_esc(carrier)}</td>'
                f'<td {_TD_STYLE.replace("text-align:left","text-align:right")};font-weight:600>{_esc(rate_s)}</td>'
                f'<td {_TD_STYLE};font-size:11px;white-space:normal;word-break:break-word>{_esc(reason)}</td></tr>'
            )
    else:
        sc_rows = _EMPTY_ROW
    sc_table = (
        # 7 cols: Lane Equip/TEU ReqDate StatusChange Carrier Rate Reason.
        # Reason gets the widest share so it wraps instead of truncating.
        f'{_TABLE_OPEN_FIXED}'
        f'{_colgroup(15, 13, 10, 16, 12, 8, 26)}'
        f'<thead><tr>'
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

    # ONE definition of "a win today", shared with the KPI tile.
    #
    # This line used to count every →WIN transition dated today REGARDLESS of
    # where the row ended up, while `_today_summary`'s tile additionally
    # required `status == "WIN"`. A row that flipped to WIN on a send-signal
    # and was then re-decided away (aged to SEND_NO_BOOKING, or held as
    # MDOLX_NO_SEND) therefore counted HERE and not THERE — so the same email
    # said "Won — Wed Jul 22: 0" in the KPI strip and "· 1 wins ·" eight
    # inches below, with a green PENDING → WIN pill under it. Michael has
    # flagged this shape repeatedly ("CHECK YOUR REPORT"); it is the report
    # disagreeing with itself about whether the day had a win.
    #
    # `_win_landed` is the single rule both surfaces now use, and the STATUS
    # CHANGES table below applies it too, so a transition the KPI refuses to
    # count is never rendered as a win.
    wins_in_day = sum(1 for (r, h) in status_ch if _win_landed(r, h))
    summary_line = (
        f"📊 {len(new_req)} new requests · {len(ol_resp)} new quotes received · "
        f"{wins_in_day} wins · {len(status_ch)} status changes · "
        f"{len(pending_ol)} pending OL · {len(pending_hil)} pending Hilmar"
    )

    return f"""
<div style="background:{B.DOC_INFO_BG};border:2px solid {B.DOC_INFO};border-radius:8px;padding:20px;margin-bottom:24px">
  <h2 style="margin:0 0 8px;color:{B.DOC_INFO};font-size:18px">📋 What Happened — {_esc(report_label)}</h2>
  <p style="margin:0 0 14px;font-size:11px;color:{B.DOC_MUTED}">Activity for {_esc(report_label)} — the prior business day. The tracker runs ~8 AM ET the next business morning, after that day's California (PT) business is complete, so its quotes and bookings are final.</p>

  <h3 style="margin:14px 0 4px;color:{B.DOC_INFO};font-size:13px">📥 NEW REQUESTS FROM LONNY ({len(new_req)})</h3>
  {new_table}

  <h3 style="margin:14px 0 4px;color:{B.DOC_INFO};font-size:13px">📤 OL-USA RESPONSES ({len(ol_resp)})</h3>
  {resp_table}
  {_undated_quotes_note(undated_quotes)}

  <h3 style="margin:14px 0 4px;color:{B.DOC_PENDING};font-size:13px">🔄 STATUS CHANGES ({len(status_ch)})</h3>
  {sc_table}

  <h3 style="margin:14px 0 4px;color:{B.DOC_WARN};font-size:13px">⏳ PENDING OL ({len(pending_ol)}) — awaiting OL quote</h3>
  {pol_table}

  <h3 style="margin:14px 0 4px;color:{B.DOC_PENDING};font-size:13px">⏳ PENDING HILMAR ({len(pending_hil)}) — awaiting Lonny decision</h3>
  {pend_table}

  <p style="margin:14px 0 0;font-size:13px;color:{B.DOC_INK};font-weight:bold">{_esc(summary_line)}</p>
</div>
"""


def _timing_reset_banner(measured: int, excluded: int) -> str:
    """Say on the report that the response clock was restarted, and why.

    Michael 2026-08-13: "JUST INDICATE THE TURN AROUND CLOCK AND SUCH IS OFF
    AND START RUNNING IT AGAIN STARTING TODAY AND INDICATE THAT ON THE
    REPORTS." A suppressed metric with no explanation is worse than a wrong
    one — the reader assumes it is zero, or assumes nothing broke. This
    states the floor, the count it is now measuring, and the count it threw
    away, so nobody has to ask which.

    Returns "" once TIMING_VALID_FROM is retired, so removing the constant
    removes the banner with it and no dead notice can survive on the report.
    """
    if not core.TIMING_VALID_FROM:
        return ""
    if measured:
        head = (f"Response-time metrics restarted {core.TIMING_VALID_FROM} — "
                f"now measuring {measured} quote"
                f"{'' if measured == 1 else 's'}.")
    else:
        head = (f"Response-time metrics are OFF and restart from "
                f"{core.TIMING_VALID_FROM}.")
    tail = (f" {excluded} earlier sample{'' if excluded == 1 else 's'} "
            "excluded." if excluded else "")
    return f"""
<div style="background:{B.DOC_WARN_BG};border-left:4px solid {B.DOC_WARN};padding:10px 14px;margin:-8px 0 16px;border-radius:4px;font-size:12px;color:{B.DOC_WARN};line-height:1.5">
  <strong>⏱ Turnaround clock reset.</strong> {_esc(head)}{_esc(tail)}
  OL's replies were not reaching this mailbox beforehand — they went To:
  Lonny, Cc: the group — so a turnaround measured over that period is a clock
  that was started and never stopped, not a measure of how fast OL answered.
  <strong>Win, loss and TEU figures are unaffected</strong>: those are
  reconciled against OL's own booking export, not against mail timing.
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
    # RESTYLE 2026-08-04: the tile was a solid saturated block with white
    # text — six of them in a row is the loudest thing on the page, and it
    # made the FRAME the subject instead of the number. Now it is a white
    # card on the paper ground, held by a hairline, with `bg` demoted to a
    # 3px top rule that still colour-codes the tile. Same signature, same
    # call sites, same colour argument: only what it paints changed.
    #
    # The figure goes mono so the six values line up with each other and
    # with the table columns underneath.
    sub_html = (
        f'<div style="font-size:10px;color:{DOC_MUTED};margin-top:3px;line-height:1.25">{_esc(sublabel)}</div>'
        if sublabel else
        f'<div style="font-size:10px;color:{DOC_MUTED};margin-top:3px;line-height:1.25">&nbsp;</div>'
    )
    return f"""
<td class="hx-kpi" style="padding:4px;width:{width};vertical-align:top">
  <div class="hx-kpi-card" style="background-color:{DOC_CARD};border:1px solid {DOC_LINE};border-top:3px solid {bg};border-radius:8px;padding:13px 10px 15px;text-align:center;min-height:88px;height:88px;box-sizing:border-box">
    <div style="font-size:22px;font-weight:bold;line-height:1.1;color:{bg};font-family:{EMAIL_MONO_STACK}">{_esc(value)}</div>
    <div style="font-size:11px;color:{DOC_MUTED};margin-top:4px;line-height:1.25;text-transform:uppercase;letter-spacing:0.03em">{_esc(label)}</div>
    {sub_html}
  </div>
</td>
"""


def _today_summary(requests, report_date=None):
    """Compute the report day's KPI buckets.

    WINS COUNT BY EVENT DATE, not request date (Michael 2026-07-21 "firstly
    data missing … CHECK YOUR REPORT"): the Jul-20 report showed 2 wins in
    "What Happened" (a Jul-16 request booking-confirmed on Jul 20, plus a
    same-day win) while the day KPI said "0 Won" — because this bucketed by
    request_date == report day, so a win that HAPPENED on the report day for
    an older request was invisible. A win belongs to the day Lonny booked it:
    count →WIN status_history transitions dated the report day, falling back
    to request_date for WIN rows with no dated →WIN transition (legacy rows),
    deduped. This matches the "What Happened — STATUS CHANGES" section, so the
    email can no longer contradict itself.

    Requests / Q&L / NQ / Pending stay request-date-bucketed: "Requests" means
    RECEIVED that day, and pending/loss states are the CURRENT status of that
    day's intake. The reconciliation identity therefore covers those four; the
    Won tile is event-dated and labeled as such by the caller.
    """
    if report_date is None:
        report_date = _report_date()
    rd_iso = report_date.isoformat()
    day_reqs = [r for r in requests
                if (r.get("request_date") == rd_iso) or (r.get("date") == rd_iso)]

    def _won_on(r):
        """True if this row's win EVENT lands on the report day.

        `core.win_event_date` is the shared definition — the weekly summary
        buckets by the same call, so the two reports cannot credit one
        booking to different periods (audit finding #19). It already folds in
        the legacy fallback to request_date for WIN rows recorded before
        transitions were kept, so no separate fallback pass is needed here.
        """
        return core.win_event_date(r) == rd_iso

    def _has_dated_win(r):
        return any(h.get("to") == "WIN" and _et_date(h.get("at"))
                   for h in (r.get("status_history") or []))

    day_wins = [r for r in requests if _won_on(r)]

    # Rows REQUESTED this day whose win landed on a DIFFERENT day (e.g. asked
    # late Monday, booking confirmed Tuesday morning before the fire). The win
    # itself is credited to ITS day's tile — counting it here too would double
    # count it across day tiles and inflate the 7-day sparkline — but the row
    # still belongs to this day's intake, so it's surfaced as "booked a later
    # day" rather than silently vanishing from every bucket (post-#111 review
    # finding: total exceeded the bucket sum for exactly this shape).
    won_later = [r for r in day_reqs
                 if r.get("status") == "WIN" and _has_dated_win(r) and not _won_on(r)]

    return {
        "wins":         len(day_wins),
        "teu_won":      sum(int(r.get("teu_won") or r.get("teu_requested") or 0)
                            for r in day_wins),
        "quoted_lost":  sum(1 for r in day_reqs if core.is_quoted_and_lost(r)),
        "not_quoted":   sum(1 for r in day_reqs if core.is_not_quoted(r)),
        "pending":      sum(1 for r in day_reqs if core.is_pending(r)),
        "won_later":    len(won_later),
        "total":        len(day_reqs),
        "as_of_label":  f"{rd_iso} (ET)",
        "report_date":  rd_iso,
    }


def _kpi_block_html(summary, requests=None, report_date=None):
    """Two KPI rows:
      Row 1 (REPORT DAY) — what happened TODAY (the now-complete business day)
        in ET. Often low or zero on quiet days — that's the truth.
      Row 2 (PERIOD TO DATE) — cumulative over the data range. Used for negotiation depth.

    Michael 2026-04-30: "we didn't win that today" — fix is that the daily email's
    headline KPI was cumulative but unlabeled. Now both views are explicit.
    Michael 2026-06-16 ("move this to end of every day"): the fire moved to
    ~6 PM ET, so Row 1 now reports TODAY's complete day (or last Friday on a
    weekend), not the previous business day.
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
    # TIMING RESET (Michael 2026-08-13). core.summarize returns None, not 0.0,
    # while no post-floor sample exists — "0.0h" on this card would read as an
    # instant reply. The card says OFF and the banner below says why.
    biz = summary.get("turnaround_avg_biz_hours")
    ta_n = summary.get("turnaround_entries", 0) or 0
    ta_excluded = summary.get("turnaround_excluded", 0) or 0
    biz_value = f"{biz:.1f}h" if biz is not None else "OFF"
    biz_sub = ("Lonny → OL quote" if biz is not None
               else f"restarted {core.TIMING_VALID_FROM}")

    # Pending is two materially different waits — surface WHO to chase as a
    # clear marker instead of one lumped "Pending". PENDING_OL = chase OL for a
    # quote; PENDING_HILMAR = OL quoted, chase Lonny to decide.
    _pend_all = [r for r in (requests or []) if (r.get("status") or "").upper() == "PENDING"]
    pend_ol = sum(1 for r in _pend_all if core.pending_substate(r) == "PENDING_OL")
    pend_hil = sum(1 for r in _pend_all if core.pending_substate(r) == "PENDING_HILMAR")

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

    # Today's pending split (same date filter as _today_summary) for the day tile.
    _rd_iso = report_date.isoformat()
    _day_pend = [r for r in (requests or [])
                 if ((r.get("request_date") == _rd_iso) or (r.get("date") == _rd_iso))
                 and (r.get("status") or "").upper() == "PENDING"]
    day_pend_ol = sum(1 for r in _day_pend if core.pending_substate(r) == "PENDING_OL")
    day_pend_hil = sum(1 for r in _day_pend if core.pending_substate(r) == "PENDING_HILMAR")

    # 7-day trend per metric for sparklines under the day-row cards
    from datetime import timedelta as _td
    trend_days = []
    for i in range(6, -1, -1):
        d = report_date - _td(days=i)
        s = _today_summary(requests or [], report_date=d)
        trend_days.append(s)
    spark_total = V.sparkline_svg([s['total'] for s in trend_days], width=80, height=18, color=B.DOC_INFO)
    spark_wins = V.sparkline_svg([s['wins'] for s in trend_days], width=80, height=18, color=B.DOC_GOOD)
    spark_ql = V.sparkline_svg([s['quoted_lost'] for s in trend_days], width=80, height=18, color=B.DOC_BAD)
    spark_nq = V.sparkline_svg([s['not_quoted'] for s in trend_days], width=80, height=18, color=B.DOC_WARN)
    spark_pend = V.sparkline_svg([s['pending'] for s in trend_days], width=80, height=18, color=B.DOC_PENDING)

    return f"""
<h2 style="{H2_STYLE}">📊 KPIs — {_esc(day_short)} (ET) <span style="font-size:11px;color:{B.DOC_MUTED};font-weight:400;margin-left:8px">7-day trend ↓</span></h2>
<p style="margin:-8px 0 8px;font-size:11px;color:{B.DOC_MUTED}">Activity for the prior business day. "Won" counts bookings CONFIRMED that day (any request date, matching Status Changes); the other tiles bucket that day's incoming requests by current status.</p>
<table style="width:100%;border-collapse:collapse;margin-bottom:16px">
  <tr>
    {_kpi_card(day['total'], f"Requests — {day_short}", B.DOC_INFO, "20%", sublabel=(f"{day.get('total',0)} entries" + (f" · {day['won_later']} booked a later day" if day.get('won_later') else "")))}
    {_kpi_card(day['wins'], f"Won — {day_short}", B.DOC_GOOD, "20%", sublabel=f"{day['teu_won']} TEU · booked that day")}
    {_kpi_card(day['quoted_lost'], f"Quoted & Lost — {day_short}", B.DOC_BAD, "20%", sublabel="OL quoted; not booked")}
    {_kpi_card(day['not_quoted'], f"Not Quoted — {day_short}", B.DOC_WARN, "20%", sublabel="OL did not respond")}
    {_kpi_card(day['pending'], f"Pending — {day_short}", B.DOC_PENDING, "20%", sublabel=f"{day_pend_ol} Pending OL · {day_pend_hil} Pending Hilmar")}
  </tr>
  <tr>
    <td style="padding:0 4px;text-align:center">{spark_total}</td>
    <td style="padding:0 4px;text-align:center">{spark_wins}</td>
    <td style="padding:0 4px;text-align:center">{spark_ql}</td>
    <td style="padding:0 4px;text-align:center">{spark_nq}</td>
    <td style="padding:0 4px;text-align:center">{spark_pend}</td>
  </tr>
</table>
<h2 style="{H2_STYLE}">📊 KPIs — All requests {_esc(period_str_kpi)}</h2>
<p style="margin:-8px 0 8px;font-size:11px;color:{B.DOC_MUTED}">Cumulative over the period shown above — used for win-rate negotiation depth, NOT "today". 'Won' = WIN rows. 'Quoted & Lost' = OL quoted but Lonny chose elsewhere. 'Not Quoted' = OL never responded or had no rate. 'Pending' = awaiting Lonny's decision.</p>
<table style="width:100%;border-collapse:collapse;margin-bottom:20px">
  <tr>
    {_kpi_card(total, "Total Requests", B.DOC_INFO, sublabel="(all statuses)")}
    {_kpi_card(wins, "Won · this period", B.DOC_GOOD, sublabel=f"{teu_won} TEU won")}
    {_kpi_card(ql, "Quoted & Lost", B.DOC_BAD, sublabel=f"{teu_ql} TEU · lost on price")}
    {_kpi_card(nq, "Not Quoted", B.DOC_WARN, sublabel=f"{teu_nq} TEU · OL silent")}
  </tr>
  <tr>
    {_kpi_card(pending, "Pending", B.DOC_PENDING, sublabel=f"{pend_ol} Pending OL · {pend_hil} Pending Hilmar · {teu_pending} TEU")}
    {_kpi_card(f"{wr:.1f}%", "Win Rate", B.DOC_GOOD, sublabel=f"{wins} wins ÷ {decided_competitive} decided")}
    {_kpi_card(f"{no_resp_rate:.1f}%", "No-Response Rate", B.DOC_WARN, sublabel=f"{nq} NQ ÷ {decided_all} total")}
    {_kpi_card(biz_value, "Avg Biz-Hrs", B.DOC_PENDING, sublabel=biz_sub)}
  </tr>
</table>
{_timing_reset_banner(ta_n, ta_excluded)}

<!-- 2026-05-19 PM 7th pass (Michael "i'm lost win rate 45.4 percent what
     is wins / wins + q&l"): plain-English explainer of Win Rate. -->
<div style="background:{B.DOC_GOOD_BG};border-left:4px solid {B.DOC_GOOD};padding:10px 14px;margin:-8px 0 16px;border-radius:4px;font-size:12px;color:{B.DOC_GOOD};line-height:1.5">
  <strong>How Win Rate is computed:</strong> Win Rate = wins ÷ decided contests.
  A "decided contest" is every time OL gave Lonny a rate AND Lonny chose
  (so it's <strong>Wins + Q&amp;L</strong> — wins are the times Lonny picked us,
  Q&amp;L are the times Lonny picked a competitor). NQ (OL never gave a rate)
  is NOT in the denominator — we never actually competed on those, so they
  show as a separate "No-Response Rate" KPI. Pending (not decided yet) is
  also excluded. This period: <strong>{wins} wins ÷ {decided_competitive} decided = {wr:.1f}%</strong>.
</div>
"""


def _current_week_day_rows(data, report_date):
    """Mon–Fri of the CURRENT week, one row per day, oldest first.

    Michael 2026-08-07: "you aren't doing the current week in review as well
    for each day... there should be a weekly tally as well for current week."
    The multi-week rollup below this shows W32 as a single line — useful for
    trend, useless for "what happened Tuesday". Days that have not arrived yet
    are omitted rather than shown as zeros, because a zero for Friday on a
    Wednesday is not information, it is noise that looks like a bad week.

    Bucketing goes through the core accessors, so a STRICT-form row counts the
    same as a LEGACY one — the rollup directly below still uses raw `status ==`
    comparisons for its loss split and would drop them.
    """
    monday = report_date - timedelta(days=report_date.weekday())
    by_day = {}
    for r in data.get("requests", []):
        d = _iso_date(r.get("request_date") or r.get("request_timestamp"))
        if not d or not (monday <= d <= report_date):
            continue
        b = by_day.setdefault(d, {"requests": 0, "won": 0, "ql": 0, "nq": 0,
                                  "pending": 0, "teu_req": 0, "teu_won": 0})
        b["requests"] += 1
        b["teu_req"] += int(r.get("teu_requested") or 0)
        if core.is_win(r):
            b["won"] += 1
            b["teu_won"] += int(r.get("teu_won") or r.get("teu_requested") or 0)
        elif core.is_pending(r):
            b["pending"] += 1
        elif core.is_quoted_and_lost(r):
            b["ql"] += 1
        elif core.is_not_quoted(r):
            b["nq"] += 1
    return [(d, by_day[d]) for d in sorted(by_day)]


def _current_week_block_html(rows, report_date):
    """Day-by-day table for the current week, with a TOTAL row.

    The total is what makes this a "tally": it answers "how are we doing this
    week" without the reader adding five numbers in their head, and it is the
    number to compare against the same week last year.
    """
    monday = report_date - timedelta(days=report_date.weekday())
    label = (_fmt_date(datetime.combine(monday, datetime.min.time()), "%b %-d")
             + " – "
             + _fmt_date(datetime.combine(report_date, datetime.min.time()), "%b %-d, %Y"))
    if not rows:
        return f"""
<h2 style="{H2_STYLE}">🗓️ This Week, Day by Day — {_esc(label)}</h2>
<p style="margin:0 0 14px;font-size:12px;color:{B.DOC_MUTED}">No requests recorded yet this week.</p>
"""
    body = ""
    tot = {"requests": 0, "won": 0, "ql": 0, "nq": 0, "pending": 0,
           "teu_req": 0, "teu_won": 0}
    for n, (d, b) in enumerate(rows):
        for k in tot:
            tot[k] += b[k]
        bg = B.DOC_CARD if n % 2 else B.DOC_TH_BG
        day = _fmt_date(datetime.combine(d, datetime.min.time()), "%a %b %-d")
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px;font-weight:600;white-space:nowrap">{_esc(day)}</td>
  <td style="padding:6px 8px;text-align:center">{b['requests']}<div style="font-size:10px;color:{B.DOC_MUTED}">{b['teu_req']} TEU</div></td>
  <td style="padding:6px 8px;text-align:center">{b['won']}<div style="font-size:10px;color:{B.DOC_MUTED}">{b['teu_won']} TEU</div></td>
  <td style="padding:6px 8px;text-align:center">{b['ql']}</td>
  <td style="padding:6px 8px;text-align:center">{b['nq']}</td>
  <td style="padding:6px 8px;text-align:center">{b['pending']}</td>
</tr>
"""
    body += f"""
<tr style="background:{B.DOC_LINE};font-weight:bold;border-top:2px solid {DOC_INK}">
  <td style="padding:6px 8px">WEEK TO DATE</td>
  <td style="padding:6px 8px;text-align:center">{tot['requests']}<div style="font-size:10px;font-weight:normal">{tot['teu_req']} TEU</div></td>
  <td style="padding:6px 8px;text-align:center">{tot['won']}<div style="font-size:10px;font-weight:normal">{tot['teu_won']} TEU</div></td>
  <td style="padding:6px 8px;text-align:center">{tot['ql']}</td>
  <td style="padding:6px 8px;text-align:center">{tot['nq']}</td>
  <td style="padding:6px 8px;text-align:center">{tot['pending']}</td>
</tr>
"""
    return f"""
<h2 style="{H2_STYLE}">🗓️ This Week, Day by Day — {_esc(label)}</h2>
<p style="margin:0 0 8px;font-size:11px;color:{B.DOC_MUTED}">Every weekday of the current week so far, bucketed by the day Lonny sent the request. Days not yet reached are omitted. WEEK TO DATE is the running tally.</p>
<table class="hx-data" style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px">
  <tr>
    <th style="{TH_STYLE}">Day</th>
    <th style="{TH_STYLE};text-align:center">Requests (# · TEU)</th>
    <th style="{TH_STYLE};text-align:center">Won (# · TEU)</th>
    <th style="{TH_STYLE};text-align:center">Q&amp;L (#)</th>
    <th style="{TH_STYLE};text-align:center">NQ (#)</th>
    <th style="{TH_STYLE};text-align:center">Pending (#)</th>
  </tr>
  {body}
</table>
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
        bg = B.DOC_CARD if alt else B.DOC_TH_BG
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
            + (f'<br><span style="font-size:10px;color:{B.DOC_MUTED};font-weight:400">{_esc(date_range)}</span>' if date_range else '')
        )
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px;white-space:nowrap">{week_cell}</td>
  <td style="padding:6px 8px;text-align:center">{b['requests']}<br><span style="font-size:10px;color:{B.DOC_MUTED}">{teu_req} TEU</span></td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_GOOD};font-weight:bold">{b['won']}<br><span style="font-size:10px;color:{B.DOC_GOOD};opacity:0.75">{teu_won} TEU</span></td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_BAD}">{b['ql']}</td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_WARN}">{b['nq']}</td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_PENDING}">{b['pending']}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(mdolx)}</td>
</tr>
"""
    return f"""
<h2 style="{H2_STYLE}">📅 This Week vs Last Week</h2>
<p style="margin:0 0 8px;font-size:11px;color:{B.DOC_MUTED}">Counts are number of REQUESTS / WINS / etc. — TEU is shown below each count in muted grey. All weeks are ISO weeks (Mon-Sun) in ET.</p>
<table class="hx-data" style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px">
  <tr>
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
        bg = B.DOC_CARD if alt else B.DOC_TH_BG
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
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_GOOD};font-weight:bold">{b['won']}</td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_BAD}">{b['ql']}</td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_PENDING}">{b['pending']}</td>
  <td style="padding:6px 8px;text-align:center">{wr:.1f}%</td>
  <td style="padding:6px 8px;text-align:right;font-weight:600">{teu_offered}</td>
  <td style="padding:6px 8px;text-align:right;color:{B.DOC_GOOD};font-weight:bold">{teu_won}</td>
  <td style="padding:6px 8px;text-align:right;color:{B.DOC_BAD}">{teu_lost}</td>
  <td style="padding:6px 8px;text-align:right;color:{B.DOC_PENDING}">{teu_pend}</td>
</tr>
"""
    # Totals footer — reconciles to the data range
    body += f"""
<tr style="background:{B.DOC_LINE};font-weight:bold;border-top:2px solid {DOC_INK}">
  <td style="padding:8px">TOTAL (all carriers)</td>
  <td style="padding:8px;text-align:center">{quoted_total}</td>
  <td style="padding:8px;text-align:center;color:{B.DOC_GOOD}">{wins_total}</td>
  <td style="padding:8px;text-align:center;color:{B.DOC_BAD}">{ql_total}</td>
  <td style="padding:8px;text-align:center;color:{B.DOC_PENDING}">{pending_total}</td>
  <td style="padding:8px;text-align:center;color:{B.DOC_MUTED}">—</td>
  <td style="padding:8px;text-align:right">{teu_offered_total}</td>
  <td style="padding:8px;text-align:right;color:{B.DOC_GOOD}">{teu_won_total}</td>
  <td style="padding:8px;text-align:right;color:{B.DOC_BAD}">{teu_lost_total}</td>
  <td style="padding:8px;text-align:right;color:{B.DOC_PENDING}">{teu_pending_total}</td>
</tr>
"""
    return f"""
<h2 style="{H2_STYLE}">🚢 Carrier Performance — All requests in current dataset</h2>
<p style="margin:0 0 8px;font-size:11px;color:{B.DOC_MUTED}">Per-carrier rollup across EVERY request currently in tracking-data-v2.json. Counts (#) are number of request rows. TEU columns sum the containers on those rows. <strong>Math reconciliation:</strong> TEU Offered = TEU Won + TEU Lost + TEU Pending (on every row).</p>
<table class="hx-data" style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr>
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
    _MICRO = f'style="font-size:9px;color:{B.DOC_MUTED};font-weight:400;letter-spacing:0.3px;text-transform:uppercase"'
    for lane, b in rows:
        bg = B.DOC_CARD if alt else B.DOC_GOOD_BG
        alt = not alt
        carriers = ", ".join(sorted(b.get("carriers") or [])) or "—"
        teu_bar = V.bar_cell(b['teu_won'], max_teu, color=B.DOC_GOOD, label=str(b['teu_won']), width_px=80)
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
  <td style="padding:6px 8px;text-align:center;font-weight:600;vertical-align:top">{b['total']} · {b.get('teu_req', 0)}<div {_MICRO}>Offered (# · TEU)</div></td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_GOOD};font-weight:bold;vertical-align:top">{b['won']}<div {_MICRO}>Wins</div></td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_BAD};vertical-align:top">{ql_count}<div {_MICRO}>Q&amp;L</div></td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_WARN};vertical-align:top">{nq_count}<div {_MICRO}>NQ</div></td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_PENDING};vertical-align:top">{pend_count}<div {_MICRO}>Pending</div></td>
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
<h2 style="{H2_STYLE}">📈 Top Winning Lanes — All requests in current dataset</h2>
<p style="margin:0 0 8px;font-size:11px;color:{B.DOC_MUTED}">Sorted by TEU won (descending). "Wins" = WIN rows on this lane. "Q&amp;L" / "NQ" / "Pending" break out the other statuses so you see the full mix. "Win Rate" = Wins / (Wins + Q&amp;L + NQ), Pending excluded. A lane can ALSO appear in Top Losing Lanes when high-volume on both sides.</p>
<table class="hx-data" style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr>
    <th style="{TH_STYLE};text-align:left">Lane (origin → destination)</th>
    <th style="{TH_STYLE};text-align:center" title="ALL shipments up for offer on this lane (every status) · their total TEU — the denominator behind the percentages">Offered (# · TEU)</th>
    <th style="{TH_STYLE};text-align:center" title="Bookings won on this lane">Wins (#)</th>
    <th style="{TH_STYLE};text-align:center" title="Quoted & Lost — OL responded but Lonny chose elsewhere">Q&amp;L (#)</th>
    <th style="{TH_STYLE};text-align:center" title="Not Quoted — OL didn't respond with a rate">NQ (#)</th>
    <th style="{TH_STYLE};text-align:center" title="Awaiting Lonny's decision">Pending (#)</th>
    <th style="{TH_STYLE};text-align:left" title="Sum of TEU on this lane's WIN rows">TEU Won</th>
    <th style="{TH_STYLE};text-align:center" title="Wins / (Wins + Q&L + NQ). Per-lane. NOT a parser metric.">Win Rate</th>
    <th style="{TH_STYLE};text-align:left">Winning Carriers</th>
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
    _MICRO = f'style="font-size:9px;color:{B.DOC_MUTED};font-weight:400;letter-spacing:0.3px;text-transform:uppercase"'
    for lane, b in rows:
        bg = B.DOC_CARD if alt else B.DOC_BAD_BG
        alt = not alt
        teu_bar = V.bar_cell(b['teu_lost'], max_teu, color=B.DOC_BAD, label=str(b['teu_lost']), width_px=80)
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
  <td style="padding:6px 8px;text-align:center;font-weight:600;vertical-align:top">{b['total']} · {b.get('teu_req', 0)}<div {_MICRO}>Offered (# · TEU)</div></td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_BAD};font-weight:bold;vertical-align:top">{b['lost']}<div {_MICRO}>Q&amp;L</div></td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_WARN};vertical-align:top">{nq_count}<div {_MICRO}>NQ</div></td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_PENDING};vertical-align:top">{pend_count}<div {_MICRO}>Pending</div></td>
  <td style="padding:6px 8px;text-align:center;color:{B.DOC_GOOD};vertical-align:top">{won_count}<div {_MICRO}>Wins</div></td>
  <td style="padding:6px 8px;text-align:left;vertical-align:top">{teu_bar}<div {_MICRO}>TEU Lost</div></td>
  <td style="padding:6px 8px;text-align:center;background:{wr_bg};font-weight:600;vertical-align:top">{win_rate:.1f}%<div {_MICRO}>Win Rate</div></td>
  <td style="padding:6px 8px;font-size:11px;vertical-align:top">{_esc(winning_carriers)}<div {_MICRO}>Winning Carriers</div></td>
</tr>
"""
    # 2026-05-19 PM 4th pass: solid bg for Outlook (was gradient → invisible).
    return f"""
<h2 style="{H2_STYLE}">📉 Top Losing Lanes — All requests in current dataset</h2>
<p style="margin:0 0 8px;font-size:11px;color:{B.DOC_MUTED}">Sorted by TEU lost (descending). "Q&amp;L" = OL quoted but Lonny chose elsewhere. "NQ" = OL didn't respond. "Wins" shown for context (a lane often has BOTH wins and losses). "Win Rate" same definition as Winning Lanes — same number on the same lane.</p>
<table class="hx-data" style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr>
    <th style="{TH_STYLE};text-align:left">Lane (origin → destination)</th>
    <th style="{TH_STYLE};text-align:center" title="ALL shipments up for offer on this lane (every status) · their total TEU — the denominator behind the percentages">Offered (# · TEU)</th>
    <th style="{TH_STYLE};text-align:center" title="Q&L rows on this lane (quoted but lost)">Q&amp;L (#)</th>
    <th style="{TH_STYLE};text-align:center" title="Not Quoted rows on this lane (OL didn't respond)">NQ (#)</th>
    <th style="{TH_STYLE};text-align:center" title="Awaiting Lonny decision">Pending (#)</th>
    <th style="{TH_STYLE};text-align:center" title="Bookings WON on this lane (context)">Wins (#)</th>
    <th style="{TH_STYLE};text-align:left" title="Sum of TEU on Q&L rows">TEU Lost</th>
    <th style="{TH_STYLE};text-align:center" title="Wins / (Wins + Q&L + NQ). Same number that appears in Winning Lanes for this lane.">Win Rate</th>
    <th style="{TH_STYLE};text-align:left">Winning Carriers (on the wins)</th>
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
        bg = B.DOC_CARD if alt else B.DOC_WARN_BG
        alt = not alt
        request_ts = r.get('request_timestamp') or ''
        imid_short = (r.get('source_imids') or ['—'])[0]
        if imid_short and imid_short != '—':
            imid_short = imid_short[:24] + '…' if len(imid_short) > 24 else imid_short
        body += f"""
<tr style="background:{bg};border-bottom:1px solid {B.DOC_WARN_BG}">
  <td style="padding:6px 8px;white-space:nowrap">{_esc(r.get('request_date') or '—')}</td>
  <td style="padding:6px 8px;font-size:11px;color:{B.DOC_MUTED}">{_esc(r.get('lonny_time_pt') or '—')}</td>
  <td style="padding:6px 8px">{_esc(r.get('origin') or '—')}</td>
  <td style="padding:6px 8px">{_esc(r.get('destination') or '—')}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('containers') or '—')}</td>
  <td style="padding:6px 8px;text-align:center">{_esc(r.get('teu_requested') or '—')}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('eta_requested') or 'no ETA on request')}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('ol_responder') or '—')}</td>
  <td style="padding:6px 8px;text-align:center;font-weight:bold;color:{B.DOC_WARN}">{_age_days(request_ts)}</td>
  <td style="padding:6px 8px;font-size:10px;color:{B.DOC_MUTED};font-family:monospace">{_esc(imid_short)}</td>
</tr>
"""
    # Build header with both visible-window count + total tally for rate-negotiation context
    total_label = f"{total_nq} total" if total_nq is not None and total_nq != len(rows) else f"{len(rows)} total"
    teu_label = f" • {teu_total} TEU" if teu_total else ""
    older_count = (total_nq or len(rows)) - len(rows)
    older_note = (f" • {older_count} older than {NQ_DISPLAY_WINDOW_DAYS}d hidden from listing "
                  f"but counted in volume tally for rate negotiation") if older_count > 0 else ""
    return f"""
<h2 style="color:{B.DOC_WARN};font-size:16px;margin:20px 0 12px;border-bottom:2px solid {B.DOC_WARN};padding-bottom:8px">⚠️ Not Quoted — Last {NQ_DISPLAY_WINDOW_DAYS} Days ({len(rows)} listed • {_esc(total_label)}{_esc(teu_label)})</h2>
<p style="margin:0 0 8px;font-size:11px;color:{B.DOC_MUTED}">Full request audit — every field needed to root-cause why OL did not respond.{_esc(older_note)}</p>
<table class="hx-data" style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:{B.DOC_WARN};color:white">
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
        wait_color = B.DOC_INK
        overdue = False
        if req_dt:
            # BUSINESS hours (ET 8:30-17:30 Mon-Fri) — the same clock as the
            # "Time to Quote" column, so the SLA and the displayed wait can
            # never disagree. Michael 2026-07-26: OL's response SLA is 3 hours.
            wait_h = core.biz_hours_between(req_dt, now)
            if wait_h is None:
                wait_h = 0.0
            wait_s = f"{wait_h:.1f}h"
            overdue = core.pending_ol_overdue(req_dt, now)
            wait_color = B.DOC_BAD if overdue else B.DOC_GOOD
            if overdue:
                wait_s += " ⚠"
        body += f"""
<tr style="border-bottom:1px solid {B.DOC_LINE}">
  <td style="padding:6px 8px"><strong>{_esc(r.get('lane') or '—')}</strong></td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('containers') or '—')}</td>
  <td style="padding:6px 8px;text-align:center">{_esc(str(r.get('teu_requested') or '—'))}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('product') or '—')}</td>
  <td style="padding:6px 8px;white-space:nowrap;font-size:11px">{_esc(_fmt_pt(r.get('request_timestamp')))}</td>
  <td style="padding:6px 8px;text-align:center;font-weight:600;color:{wait_color}">{_esc(wait_s)}</td>
</tr>
"""
    body += f"""
<tr style="background:{B.DOC_LINE};font-weight:bold;border-top:2px solid {B.DOC_WARN}">
  <td style="padding:8px" colspan="2">TOTAL ({len(rows)} awaiting OL)</td>
  <td style="padding:8px;text-align:center">{total_teu}</td>
  <td style="padding:8px" colspan="3">&nbsp;</td>
</tr>
"""
    return f"""
<h2 style="color:{B.DOC_WARN};font-size:16px;margin:20px 0 12px;border-bottom:2px solid {B.DOC_WARN};padding-bottom:8px">⏳ Pending OL Quote ({len(rows)} requests · {total_teu} TEU)</h2>
<p style="margin:0 0 8px;font-size:11px;color:{B.DOC_MUTED}">RFQs Lonny has sent that OL has NOT yet quoted — the wait is on OL, not Hilmar. "Waiting on OL" is BUSINESS hours since the RFQ (ET 8:30–5:30 Mon–Fri, the same clock as Time to Quote). OL's response SLA is {core.PENDING_OL_SLA_BIZ_HOURS}h: red ⚠ rows have BLOWN the SLA and are chase candidates with the OL desk. These stay open until the {core.PENDING_OL_LOSS_HOURS}h win/loss timer resolves them.</p>
<table class="hx-data" style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:{B.DOC_WARN};color:white">
    <th style="padding:8px;text-align:left;background-color:{B.DOC_WARN};color:{B.DOC_CARD}">Lane</th>
    <th style="padding:8px;text-align:left;background-color:{B.DOC_WARN};color:{B.DOC_CARD}">Equipment</th>
    <th style="padding:8px;text-align:center;background-color:{B.DOC_WARN};color:{B.DOC_CARD}">TEU</th>
    <th style="padding:8px;text-align:left;background-color:{B.DOC_WARN};color:{B.DOC_CARD}">Product</th>
    <th style="padding:8px;text-align:left;background-color:{B.DOC_WARN};color:{B.DOC_CARD}" title="When Lonny sent the RFQ (Pacific Time)">Lonny Sent (PT)</th>
    <th style="padding:8px;text-align:center;background-color:{B.DOC_WARN};color:{B.DOC_CARD}" title="Business hours since the RFQ with no OL quote — red means OL has blown the response SLA">Waiting on OL</th>
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
        bg = B.DOC_CARD if alt else B.DOC_PENDING_BG
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
            ttq_color = B.DOC_GOOD if ttq_biz <= 4 else (B.DOC_WARN if ttq_biz <= 24 else B.DOC_BAD)
        elif isinstance(ttq_clock, (int, float)):
            ttq_s = f"{ttq_clock:.1f}h (clock)"
            ttq_color = B.DOC_MUTED
        else:
            ttq_s = "—"
            ttq_color = B.DOC_MUTED
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
<tr style="background:{B.DOC_LINE};font-weight:bold;border-top:2px solid {B.DOC_PENDING}">
  <td style="padding:8px" colspan="2">TOTAL ({len(rows)} pending)</td>
  <td style="padding:8px;text-align:center">{total_teu}</td>
  <td style="padding:8px" colspan="7">&nbsp;</td>
</tr>
"""
    return f"""
<h2 style="color:{B.DOC_PENDING};font-size:16px;margin:20px 0 12px;border-bottom:2px solid {B.DOC_PENDING};padding-bottom:8px">⏳ Pending Hilmar Response ({len(rows)} requests · {total_teu} TEU)</h2>
<p style="margin:0 0 8px;font-size:11px;color:{B.DOC_MUTED}">Rows where OL has quoted but Lonny hasn't yet decided. Columns show the full clock: when Lonny asked → when OL quoted → how long that took (biz-hours) → how long it's been waiting. "Time to Quote" is OL's response speed on this row (sub-4h green / 4–24h amber / &gt;24h red). "Hours since OL quote" is the chase metric — candidates &gt;24h need follow-up.</p>
<table class="hx-data" style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:{B.DOC_PENDING};color:white">
    <th style="padding:8px;text-align:left;background-color:{B.DOC_PENDING};color:{B.DOC_CARD}">Lane</th>
    <th style="padding:8px;text-align:left;background-color:{B.DOC_PENDING};color:{B.DOC_CARD}">Equipment</th>
    <th style="padding:8px;text-align:center;background-color:{B.DOC_PENDING};color:{B.DOC_CARD}">TEU</th>
    <th style="padding:8px;text-align:left;background-color:{B.DOC_PENDING};color:{B.DOC_CARD}">Carrier Quoted</th>
    <th style="padding:8px;text-align:right;background-color:{B.DOC_PENDING};color:{B.DOC_CARD}">Rate ($/container)</th>
    <th style="padding:8px;text-align:left;background-color:{B.DOC_PENDING};color:{B.DOC_CARD}">Who Quoted (OL signer)</th>
    <th style="padding:8px;text-align:left;background-color:{B.DOC_PENDING};color:{B.DOC_CARD}" title="When Lonny sent the RFQ (Pacific Time, Lonny's office)">Lonny Requested (PT)</th>
    <th style="padding:8px;text-align:left;background-color:{B.DOC_PENDING};color:{B.DOC_CARD}" title="When OL responded with the rate (Eastern Time, OL's office)">OL Quoted (ET)</th>
    <th style="padding:8px;text-align:center;background-color:{B.DOC_PENDING};color:{B.DOC_CARD}" title="Biz-hours from Lonny's RFQ to OL's rate response. Green ≤4h, amber 4–24h, red &gt;24h. Counts only OL business hours (8:30 AM – 5:30 PM ET weekdays).">Time to Quote</th>
    <th style="padding:8px;text-align:center;background-color:{B.DOC_PENDING};color:{B.DOC_CARD}" title="Time elapsed since OL's quote arrived — chase candidates &gt;24h">Hours since OL quote</th>
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
        "PRICE":             ("Price (rate-driven)",      B.DOC_INK),   # Hilmar navy
        "ETD_MISS":          ("ETD missed",               B.DOC_INFO),   # blue
        "NO_RESPONSE":       ("OL didn't respond",        B.DOC_WARN),   # rust
        "RESPONSE_NO_RATE":  ("OL acked but no rate",     B.DOC_WARN),
        "SEND_NO_BOOKING":   ("Send w/o MDOLX booking",   B.DOC_WARN),   # amber-deep
        "UNDIFFERENTIATED":  ("Undifferentiated — needs investigation", B.DOC_MUTED),  # slate-500
        "QUOTED_NOT_BOOKED": ("Quoted, generic no-fit",   B.DOC_MUTED),   # slate-600
        "COVERED":           ("Lonny covered w/ competitor", B.DOC_PENDING),
        "DRAFT_ONLY":        ("Booking draft-only",       B.DOC_MUTED),
        "OTHER":             ("Other",                    B.DOC_MUTED),
    }
    _ACTIONABLE_META = {
        "rate_driven": ("Push carriers — rate-driven",  B.DOC_INK),
        "etd_driven":  ("Push ops — ETD-driven",        B.DOC_INFO),
        "ol_silent":   ("Push OL — silent or late",     B.DOC_WARN),
        "other":       ("Other",                         B.DOC_MUTED),
    }

    def _bar_row(label, count, total, color):
        pct = (count * 100.0 / total) if total else 0
        # Two inner cells: a PURE colored fill (width:N%, no text) and the
        # "count · pct%" value to its RIGHT. The value never sits inside the
        # bar, so a narrow bar (e.g. 7%) can't squeeze its label onto two
        # lines — the 2026-06-25 "terrible formatting" wrap. Min 4% so a tiny
        # bar still shows a sliver. Outlook-safe: nested <table> + inline
        # width %, no flexbox/SVG/gradient (QC-045's lesson).
        return (
            '<tr>'
            f'<td style="padding:6px 12px 6px 0;color:{B.DOC_INK};font-size:13px;'
            f'white-space:nowrap;font-weight:500;vertical-align:middle">{_esc(label)}</td>'
            f'<td style="padding:6px 0;width:60%;vertical-align:middle">'
            f'<table cellspacing="0" cellpadding="0" border="0" '
            f'style="width:100%;border-collapse:collapse"><tr>'
            f'<td style="width:{max(pct, 4):.0f}%;background:{color};'
            f'border-radius:3px;line-height:16px;font-size:12px">&nbsp;</td>'
            f'<td style="padding-left:8px;white-space:nowrap;color:{B.DOC_INK};'
            f'font-size:12px;font-weight:600;font-variant-numeric:tabular-nums">'
            f'{count} &middot; {pct:.0f}%</td>'
            f'</tr></table>'
            f'</td></tr>'
        )

    def _window_block(label, mix):
        if mix["total"] == 0:
            return ""
        out = (
            f'<div style="margin:14px 0 6px 0;font-size:13px;color:{B.DOC_MUTED};'
            f'font-weight:600;letter-spacing:0.02em">'
            f'{label} &nbsp;&middot;&nbsp; <span style="color:{B.DOC_INK};'
            f'font-variant-numeric:tabular-nums">{mix["total"]} losses</span>'
            f'</div>'
            f'<table cellspacing="0" cellpadding="0" border="0" '
            f'style="width:100%;border-collapse:collapse">'
        )
        # By-reason bars (top 6 by count).
        for reason, count in mix["ranked"][:6]:
            label_str, color = _REASON_META.get(reason, (reason, B.DOC_MUTED))
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
                f'<div style="margin:8px 0 0 0;font-size:11px;color:{B.DOC_MUTED};'
                f'line-height:1.8">{ "".join(chunks) }</div>'
            )
        return out

    html = (
        '<div style="margin:28px 0 8px 0">'
        f'<h2 style="font-size:15px;color:{B.DOC_INK};margin:0 0 4px 0;'
        'font-weight:700;letter-spacing:-0.01em">Why We Lost &mdash; '
        'Loss-Reason Mix</h2>'
        f'<div style="font-size:12px;color:{B.DOC_MUTED};margin-bottom:4px">'
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
        bg = B.DOC_CARD if alt else B.DOC_TH_BG
        if m["region"] == "Unmapped":
            bg = B.DOC_BAD_BG
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
        f'<tr style="background-color:{DOC_TH_BG};font-weight:bold;border-top:2px solid {DOC_INK}">'
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
<h2 style="{H2_STYLE}">🌐 Volume by Trade Region — {_esc(period_str)}</h2>
<p style="margin:0 0 4px;font-size:11px;color:{B.DOC_MUTED}">Destinations grouped by trade region. All counts and TEU are cumulative across the period shown above. Totals reconcile to summary KPIs.</p>
<p style="margin:0 0 8px;font-size:11px;color:{B.DOC_MUTED}">{_esc(recon)}</p>
<table class="hx-data" style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr>
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


#: Size ceiling for the embedded AI-insights snippet. The Business section is
#: 2-5 bullets (~2-4KB rendered); anything past 40KB means the upstream file
#: is malformed/runaway and must not bloat the staff email.
INSIGHTS_SNIPPET_MAX_BYTES = 40 * 1024


def _ai_insights_business_html(cfg=None):
    """Inline reports/insights-business.html (written by gen_insights.py,
    which runs immediately before this script in run_pipeline).

    2026-07-11 wiring of docs/INSIGHTS-DESIGN.md M3.11 + Michael's 2026-04-28
    directive: the staff daily carries ONLY the Business section — System /
    Design / Data stay in the private idealx.us audit.

    Guards (all failure modes render nothing — insights can never break the
    client email):
      * file absent → skip
      * mtime not from today → skip (a stale yesterday narrative presented
        as today's is worse than no narrative)
      * empty or > INSIGHTS_SNIPPET_MAX_BYTES → skip (trusted internal
        output, but a runaway file must not ship)
    """
    try:
        paths = (cfg or {}).get("paths") or {}
        reports_dir = Path(paths["reports"]) if paths.get("reports") else ROOT / "reports"
        snippet_path = reports_dir / "insights-business.html"
        if not snippet_path.exists():
            return ""
        if datetime.fromtimestamp(snippet_path.stat().st_mtime).date() != datetime.now().date():
            return ""
        snippet = snippet_path.read_text(encoding="utf-8").strip()
        if not snippet or len(snippet.encode("utf-8")) > INSIGHTS_SNIPPET_MAX_BYTES:
            return ""
        return f"""
<h2 style="{H2_STYLE}">🤖 AI Insights — Business</h2>
<div style="font-size:13px;line-height:1.6;color:{B.DOC_INK}">{snippet}</div>
"""
    except Exception:
        return ""


FOOTER_HTML = f"""
<div style="background-color:{DOC_TH_BG};border:1px solid {DOC_LINE};border-radius:8px;padding:16px;margin-bottom:20px">
  <h3 style="margin:0 0 8px;color:{DOC_INK};font-size:14px">📎 ATTACHED FILES:</h3>
  <p style="margin:4px 0;font-size:12px">• <b>hilmar-dashboard.html</b> — Open in any browser (works mobile + desktop, no software needed)</p>
  <p style="margin:4px 0;font-size:12px">• <b>hilmar-report.pdf</b> — Printable report</p>
  <p style="margin:4px 0;font-size:12px">• <b>user-manual.html</b> — How to read every section, status and metric (rebuilt with each run, always current)</p>
  <h3 style="margin:12px 0 8px;color:{DOC_INK};font-size:14px">📖 DASHBOARD TAB GUIDE:</h3>
  <p style="margin:4px 0;font-size:12px">• 📊 <b>Summary</b> — KPIs, confirmed wins with MDOLX, not-quoted requests</p>
  <p style="margin:4px 0;font-size:12px">• ⏱️ <b>Turnaround Timeline</b> — Lonny request time (PT) vs OL response (ET), business-hours adjusted</p>
  <p style="margin:4px 0;font-size:12px">• 📅 <b>Dates: Requested vs Offered</b> — Lonny's cutoff/ETD/ETA vs OL's offer side-by-side</p>
  <p style="margin:4px 0;font-size:12px">• 🚢 <b>Carriers &amp; Lanes</b> — Win/loss rates with lane-level breakdowns, TEU &amp; equipment for rate negotiations</p>
  <p style="margin:4px 0;font-size:12px">• 🔍 <b>QC</b> — Data quality checks and warnings</p>
</div>
<div style="border-top:1px solid {DOC_LINE};padding-top:12px;margin-top:20px;text-align:center">
  <p style="font-size:11px;color:{DOC_MUTED}">Auto-generated from the Hilmar Shipment Tracker</p>
  <p style="font-size:11px;color:{DOC_MUTED}">Files also on OneDrive: IdealX → Hilmar folder</p>
</div>
"""


def build_body(data, cfg):
    now_et = datetime.now(timezone.utc).astimezone(core.ET)
    # Email REPORTS on TODAY's now-complete business day (weekends roll back to
    # Friday). See _report_date docstring for rationale (~6 PM ET fire = after
    # Lonny's PT office has closed for the day).
    report_date = _report_date(now_et)
    report_label = _report_label(report_date)             # 'Wednesday May 6, 2026'
    # core.format_date_range, NOT `or <fallback>`. ingest writes this key as a
    # DICT and a dict is truthy, so the fallback was unreachable in production
    # and the header printed the repr. See the docstring there.
    date_range = core.format_date_range(
        data.get("date_range"),
        fallback_start=cfg.get("data_range", {}).get("start"),
        fallback_end=report_date.isoformat(),
    )
    updated_label = _fmt_date(now_et, "%B %-d, %Y at %-I:%M %p ET")

    new_req, ol_resp, status_ch, pending = _today_events(data, report_date)
    undated_q = undated_quotes(data)
    week_rows = _week_rows(data)
    carrier_rows = _carrier_rows(data)
    winning_lanes = _winning_lane_rows(data)
    losing_lanes = _losing_lane_rows(data)
    nq_rows = _not_quoted_rows(data)  # 14-day display window only
    nq_all = _not_quoted_aggregate(data)  # full tally for rate-negotiation context
    nq_total_count = len(nq_all)
    nq_total_teu = sum(int(r.get("teu_requested") or 0) for r in nq_all)
    pend_rows = _pending_rows(data)

    html_body = _header_html(report_label, date_range, updated_label)
    html_body += _today_block_html(report_label, new_req, ol_resp, status_ch,
                                   pending, undated_q)
    html_body += _kpi_block_html(data.get("summary", {}) or {}, requests=data.get("requests", []) or [], report_date=report_date)
    # Loss-reason mix — the "why we lost" lens. Renders nothing when
    # there are no losses in the 30/60-day windows.
    html_body += _loss_reason_mix_html(data)
    html_body += _current_week_block_html(
        _current_week_day_rows(data, report_date), report_date)
    html_body += _week_block_html(week_rows)
    html_body += _carrier_block_html(carrier_rows)
    html_body += _trade_region_html(data, data.get("summary", {}) or {})
    html_body += _winning_lanes_html(winning_lanes)
    html_body += _losing_lanes_html(losing_lanes)
    html_body += _nq_html(nq_rows, total_nq=nq_total_count, teu_total=nq_total_teu)
    html_body += _pending_ol_html(
        [r for r in pend_rows if core.pending_substate(r) == "PENDING_OL"])
    html_body += _pending_html(
        [r for r in pend_rows if core.pending_substate(r) == "PENDING_HILMAR"])
    html_body += _ai_insights_business_html(cfg)
    html_body += FOOTER_HTML
    # Three closes: .hx-pad, .hx-wrap, and the paper-ground div the header
    # opens around the whole card.
    html_body += "</div></div></div>"
    return html_body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args()
    cfg = core.load_config(args.config)
    data = json.loads(Path(cfg["paths"]["data"]).read_text(encoding="utf-8"))
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
