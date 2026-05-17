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
import sys, json, argparse, html
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import core  # noqa: E402
import viz as V  # noqa: E402  shared visual helpers (sparklines, pills, bars, heatmaps)
import branding as B  # noqa: E402  Hilmar logo + brand colors


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
    """
    if not d:
        return None
    iso = d.isocalendar()
    wk = iso.week
    # Week starts Monday, ends Friday for label purposes
    monday = d - timedelta(days=d.weekday())
    friday = monday + timedelta(days=4)
    if monday.month != friday.month:
        label = f"W{wk} ({_fmt_date(monday, '%b %-d')}–{_fmt_date(friday, '%b %-d')})"
    else:
        label = f"W{wk} ({_fmt_date(monday, '%b %-d')}–{_fmt_date(friday, '%-d')})"
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
    buckets = defaultdict(lambda: {
        "requests": 0, "won": 0, "ql": 0, "nq": 0, "pending": 0, "mdolx": []
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
        st = r.get("status") or ""
        lr = r.get("loss_reason") or ""
        if st == "WIN":
            b["won"] += 1
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
    carriers = defaultdict(lambda: {
        "quoted": 0, "won": 0, "ql": 0, "pending": 0, "teu_won": 0, "teu_lost": 0
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
        elif st == "LOSS" and (r.get("loss_reason") or "") != "NO_RESPONSE":
            b["ql"] += 1
            b["teu_lost"] += teu
    rows = []
    for name, b in carriers.items():
        wr = (b["won"] / b["quoted"] * 100) if b["quoted"] else 0.0
        rows.append((name, b, wr))
    rows.sort(key=lambda x: (-x[1]["quoted"], x[0]))
    return rows


def _losing_lane_rows(data):
    lanes = defaultdict(lambda: {"lost": 0, "teu_lost": 0, "total": 0})
    for r in data.get("requests", []):
        lane = r.get("lane") or f"{r.get('origin','?')} → {r.get('destination','?')}"
        lanes[lane]["total"] += 1
        if (r.get("status") == "LOSS") and (r.get("loss_reason") or "") != "NO_RESPONSE":
            lanes[lane]["lost"] += 1
            lanes[lane]["teu_lost"] += (r.get("teu_requested") or 0)
    rows = [(lane, b) for lane, b in lanes.items() if b["lost"] > 0]
    rows.sort(key=lambda kv: -kv[1]["teu_lost"])
    return rows[:10]


def _winning_lane_rows(data):
    lanes = defaultdict(lambda: {"won": 0, "teu_won": 0, "total": 0, "carriers": set()})
    for r in data.get("requests", []):
        lane = r.get("lane") or f"{r.get('origin','?')} → {r.get('destination','?')}"
        lanes[lane]["total"] += 1
        if r.get("status") == "WIN":
            lanes[lane]["won"] += 1
            lanes[lane]["teu_won"] += (r.get("teu_won") or r.get("teu_requested") or 0)
            if r.get("carrier_won"):
                lanes[lane]["carriers"].add(r["carrier_won"])
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
    from datetime import datetime, timezone, timedelta
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

HEADER_GRADIENT = f"linear-gradient(135deg,{B.HILMAR_NAVY} 0%,{B.HILMAR_BLUE} 100%)"


EMAIL_FONT_STACK = "'Inter','Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif"
EMAIL_TNUM = "font-variant-numeric:tabular-nums;font-feature-settings:'tnum' 1"

def _header_html(today_label, range_label, updated_label):
    # Use the CID variant — Outlook blocks data: URIs in HTML email bodies
    # but renders inline CID attachments reliably. outlook_send.py attaches
    # the logo PNG with contentId=hilmar-logo + isInline=true so this
    # <img src="cid:hilmar-logo"> reference resolves at delivery time.
    # Per Michael 2026-05-17 ("hilmar logo not showing up").
    logo_html = B.logo_html_cid(height=42, alt="Hilmar Ingredients")
    logo_block = (
        f'<div style="background:white;padding:8px 12px;border-radius:6px;display:inline-block;margin-bottom:10px">{logo_html}</div>'
        if logo_html else ""
    )
    return f"""
<!--[if !mso]><!-->
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<!--<![endif]-->
<div style="max-width:900px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;font-family:{EMAIL_FONT_STACK};{EMAIL_TNUM}">
  <div style="padding:24px 32px;background:{HEADER_GRADIENT};color:white;font-family:{EMAIL_FONT_STACK}">
    {logo_block}
    <h1 style="margin:0;font-size:22px;font-weight:700;letter-spacing:-0.3px;font-family:{EMAIL_FONT_STACK}">{'' if logo_html else '🚢 '}Hilmar Ingredients — Daily Shipment Tracker</h1>
    <p style="margin:6px 0 0;font-size:14px;opacity:0.9;font-family:{EMAIL_FONT_STACK}">{_esc(range_label)} | Updated: {_esc(updated_label)}</p>
  </div>
  <div style="padding:24px 32px;font-family:{EMAIL_FONT_STACK};{EMAIL_TNUM}">
"""


def _today_block_html(report_label, new_req, ol_resp, status_ch, pending):
    """Render the 'What Happened on <day>' block. The label `report_label` is
    the previous business day (see _report_date) — not literal 'today' —
    because at 10 AM ET fire time, today's data window is still empty
    (Lonny's PT office isn't open yet).
    """
    def _li(text, margin_color="#000"):
        return f'<p style="margin:2px 0 2px 16px;font-size:13px">{text}</p>'

    # New requests
    new_html = ""
    if new_req:
        for r in new_req:
            new_html += _li(_lonny_line(r))
    else:
        new_html = _li("• No new requests")

    # OL responses
    resp_html = ""
    if ol_resp:
        for r in ol_resp:
            resp_html += _li(_response_line(r))
    else:
        resp_html = _li("• No OL responses")

    # Status changes — include containers + TEU + reason for context
    sc_html = ""
    for r, h in status_ch:
        lane = r.get("lane") or f"{r.get('origin','?')} → {r.get('destination','?')}"
        cnt = r.get('container_count') or 0
        teu = r.get('teu_requested') or 0
        cont_label = r.get('containers') or f"{cnt}cnt"
        reason = h.get('reason') or ''
        # Original request date
        req_date = r.get('request_date') or ''
        sc_html += _li(
            f"• {_esc(lane)} ({_esc(str(cont_label))} / {teu} TEU, req {_esc(req_date)}) "
            f"{V.status_pill(h['from'])} → {V.status_pill(h['to'])}"
            + (f" — {_esc(reason)}" if reason else "")
        )
    if not sc_html:
        sc_html = _li("• No status changes")

    # Pending
    pend_html = ""
    if pending:
        for r in pending:
            lane = r.get("lane") or "—"
            carrier = r.get("carrier_quoted") or "—"
            resp_dt = core.parse_iso(r.get("response_timestamp"))
            hrs = ""
            if resp_dt:
                delta_h = (datetime.now(timezone.utc) - resp_dt).total_seconds() / 3600.0
                hrs = f"{delta_h:.1f}h since quote"
            pend_html += _li(f"• {_esc(lane)} | {_esc(carrier)} | {_esc(hrs)}")
    else:
        pend_html = _li("• No pending Hilmar responses")

    wins_in_day = sum(1 for (r, h) in status_ch if h.get("to") == "WIN")
    summary_line = (
        f"📊 {len(new_req)} new requests, {len(ol_resp)} new quotes received, "
        f"{wins_in_day} wins, {len(status_ch)} status changes, {len(pending)} total pending Hilmar response"
    )

    return f"""
<div style="background:#eff6ff;border:2px solid #3b82f6;border-radius:8px;padding:20px;margin-bottom:24px">
  <h2 style="margin:0 0 12px;color:#1e40af;font-size:18px">📋 What Happened — {_esc(report_label)}</h2>
  <p style="margin:0 0 12px;font-size:11px;color:#64748b">Previous business day. Daily email runs at 10 AM ET — Lonny's California office (PT) opens ~3 hours later, so 'today' has no data yet at send time.</p>
  <h3 style="margin:12px 0 6px;color:#1e40af;font-size:14px">📥 NEW REQUESTS FROM LONNY:</h3>
  {new_html}
  <h3 style="margin:12px 0 6px;color:#1e40af;font-size:14px">📤 OL-USA RESPONSES:</h3>
  {resp_html}
  <h3 style="margin:12px 0 6px;color:#7c3aed;font-size:14px">🔄 STATUS CHANGES:</h3>
  {sc_html}
  <h3 style="margin:12px 0 6px;color:#7c3aed;font-size:14px">⏳ PENDING HILMAR RESPONSE:</h3>
  {pend_html}
  <p style="margin:12px 0 0;font-size:13px;color:#374151;font-weight:bold">{_esc(summary_line)}</p>
</div>
"""


def _kpi_card(value, label, bg, width="25%"):
    return f"""
<td style="padding:4px;width:{width}">
  <div style="background:{bg};color:white;border-radius:8px;padding:12px;text-align:center">
    <div style="font-size:20px;font-weight:bold">{_esc(value)}</div>
    <div style="font-size:11px;opacity:0.9;margin-top:4px">{_esc(label)}</div>
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
    wr = summary.get("win_rate", 0.0) or 0.0
    qr = summary.get("quote_rate", 0.0) or 0.0
    biz = summary.get("turnaround_avg_biz_hours", 0.0) or 0.0

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
    spark_nq = V.sparkline_svg([s['not_quoted'] for s in trend_days], width=80, height=18, color="#f59e0b")
    spark_pend = V.sparkline_svg([s['pending'] for s in trend_days], width=80, height=18, color="#8b5cf6")

    return f"""
<h2 style="color:#1e3a5f;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📊 KPIs — {_esc(day_short)} (ET) <span style="font-size:11px;color:#64748b;font-weight:400;margin-left:8px">7-day trend ↓</span></h2>
<p style="margin:-8px 0 8px;font-size:11px;color:#64748b">Activity on the previous business day. Math reconciliation: Requests = Won + Quoted&Lost + Not Quoted + Pending.</p>
<table style="width:100%;border-collapse:collapse;margin-bottom:16px">
  <tr>
    {_kpi_card(day['total'], f"Requests — {day_short}", "#3b82f6", "20%")}
    {_kpi_card(f"{day['wins']} ({day['teu_won']} TEU)", f"Won — {day_short}", "#22c55e", "20%")}
    {_kpi_card(day['quoted_lost'], f"Quoted & Lost — {day_short}", "#ef4444", "20%")}
    {_kpi_card(day['not_quoted'], f"Not Quoted — {day_short}", "#f59e0b", "20%")}
    {_kpi_card(day['pending'], f"Pending — {day_short}", "#8b5cf6", "20%")}
  </tr>
  <tr>
    <td style="padding:0 4px;text-align:center">{spark_total}</td>
    <td style="padding:0 4px;text-align:center">{spark_wins}</td>
    <td style="padding:0 4px;text-align:center">{spark_ql}</td>
    <td style="padding:0 4px;text-align:center">{spark_nq}</td>
    <td style="padding:0 4px;text-align:center">{spark_pend}</td>
  </tr>
</table>
<h2 style="color:#1e3a5f;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📊 KPIs — Period to Date</h2>
<p style="margin:-8px 0 8px;font-size:11px;color:#64748b">Cumulative over the data range — used for win-rate negotiation depth, not "today".</p>
<table style="width:100%;border-collapse:collapse;margin-bottom:20px">
  <tr>
    {_kpi_card(total, "Total Requests", "#3b82f6")}
    {_kpi_card(f"{wins} ({teu_won} TEU)", "Won — PTD", "#22c55e")}
    {_kpi_card(f"{ql} ({teu_ql} TEU)", "Quoted & Lost — PTD", "#ef4444")}
    {_kpi_card(f"{nq} ({teu_nq} TEU)", "Not Quoted — PTD", "#f59e0b")}
  </tr>
  <tr>
    {_kpi_card(f"{pending} ({teu_pending} TEU)", "Pending ⏳", "#8b5cf6")}
    {_kpi_card(f"{wr:.1f}%", "Win Rate — PTD", "#22c55e")}
    {_kpi_card(f"{qr:.1f}%", "Quote Rate — PTD", "#3b82f6")}
    {_kpi_card(f"{biz:.1f}h", "Avg Biz-Hrs", "#6366f1")}
  </tr>
</table>
"""


def _week_block_html(rows):
    if not rows:
        return ""
    body = ""
    alt = True
    for label, b in rows:
        bg = "#ffffff" if alt else "#f0f4f8"
        alt = not alt
        mdolx = ", ".join(b["mdolx"]) if b["mdolx"] else "—"
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px">{_esc(label)}</td>
  <td style="padding:6px 8px;text-align:center">{b['requests']}</td>
  <td style="padding:6px 8px;text-align:center;color:#16a34a;font-weight:bold">{b['won']}</td>
  <td style="padding:6px 8px;text-align:center;color:#dc2626">{b['ql']}</td>
  <td style="padding:6px 8px;text-align:center;color:#d97706">{b['nq']}</td>
  <td style="padding:6px 8px;text-align:center;color:#7c3aed">{b['pending']}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(mdolx)}</td>
</tr>
"""
    return f"""
<h2 style="color:#1e3a5f;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📅 This Week vs Last Week</h2>
<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px">
  <tr style="background:#1e3a5f;color:white">
    <th style="padding:8px;text-align:left">Week</th>
    <th style="padding:8px;text-align:center">Requests</th>
    <th style="padding:8px;text-align:center">Won</th>
    <th style="padding:8px;text-align:center">Q&amp;L</th>
    <th style="padding:8px;text-align:center">NQ</th>
    <th style="padding:8px;text-align:center">Pending</th>
    <th style="padding:8px;text-align:left">Notable Wins</th>
  </tr>
  {body}
</table>
"""


def _carrier_block_html(rows):
    if not rows:
        return ""
    body = ""
    alt = True
    for name, b, wr in rows:
        bg = "#ffffff" if alt else "#f0f4f8"
        alt = not alt
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px;font-weight:bold">{_esc(name)}</td>
  <td style="padding:6px 8px;text-align:center">{b['quoted']}</td>
  <td style="padding:6px 8px;text-align:center;color:#16a34a;font-weight:bold">{b['won']}</td>
  <td style="padding:6px 8px;text-align:center;color:#dc2626">{b['ql']}</td>
  <td style="padding:6px 8px;text-align:center;color:#7c3aed">{b['pending']}</td>
  <td style="padding:6px 8px;text-align:center">{wr:.1f}%</td>
  <td style="padding:6px 8px;text-align:center">{b['teu_won']}</td>
  <td style="padding:6px 8px;text-align:center">{b['teu_lost']}</td>
</tr>
"""
    return f"""
<h2 style="color:#1e3a5f;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">🚢 Carrier Performance — Period to Date (per carrier × W/L/Pending)</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">Same losses also appear in Top Losing Lanes (sliced by lane) and Not Quoted (NO_RESPONSE only). This view = per carrier.</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:#1e3a5f;color:white">
    <th style="padding:8px;text-align:left">Carrier</th>
    <th style="padding:8px;text-align:center">Quoted</th>
    <th style="padding:8px;text-align:center">Won</th>
    <th style="padding:8px;text-align:center">Q&amp;L</th>
    <th style="padding:8px;text-align:center">Pending</th>
    <th style="padding:8px;text-align:center">Win Rate</th>
    <th style="padding:8px;text-align:center">TEU Won</th>
    <th style="padding:8px;text-align:center">TEU Lost</th>
  </tr>
  {body}
</table>
"""


def _winning_lanes_html(rows):
    if not rows:
        return ""
    max_teu = max((b['teu_won'] for _, b in rows), default=1) or 1
    body = ""
    alt = True
    for lane, b in rows:
        bg = "#ffffff" if alt else "#ecfdf5"
        alt = not alt
        carriers = ", ".join(sorted(b.get("carriers") or []))
        teu_bar = V.bar_cell(b['teu_won'], max_teu, color="#16a34a", label=str(b['teu_won']), width_px=80)
        win_rate = (b['won'] / b['total'] * 100) if b['total'] else 0
        wr_bg = V.heatmap_color(win_rate, vmin=0, vmax=100, mode="good_high")
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px;font-weight:600">{_esc(lane)}</td>
  <td style="padding:6px 8px;text-align:center">{b['won']}</td>
  <td style="padding:6px 8px;text-align:left">{teu_bar}</td>
  <td style="padding:6px 8px;text-align:center;background:{wr_bg};font-weight:600">{b['total']}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(carriers)}</td>
</tr>
"""
    return f"""
<h2 style="color:#1e3a5f;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📈 Top Winning Lanes — Period to Date (sliced by lane, sorted by TEU won)</h2>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:linear-gradient(135deg,#059669 0%,#10b981 100%);color:white">
    <th style="padding:8px;text-align:left">Lane</th>
    <th style="padding:8px;text-align:center">Times Won</th>
    <th style="padding:8px;text-align:left">TEU Won</th>
    <th style="padding:8px;text-align:center">Total Reqs</th>
    <th style="padding:8px;text-align:left">Winning Carriers</th>
  </tr>
  {body}
</table>
"""


def _losing_lanes_html(rows):
    if not rows:
        return ""
    max_teu = max((b['teu_lost'] for _, b in rows), default=1) or 1
    body = ""
    alt = True
    for lane, b in rows:
        bg = "#ffffff" if alt else "#fef2f2"
        alt = not alt
        teu_bar = V.bar_cell(b['teu_lost'], max_teu, color="#dc2626", label=str(b['teu_lost']), width_px=80)
        loss_pct = (b['lost'] / b['total'] * 100) if b['total'] else 0
        loss_bg = V.heatmap_color(loss_pct, vmin=0, vmax=100, mode="good_low")
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px;font-weight:600">{_esc(lane)}</td>
  <td style="padding:6px 8px;text-align:center">{b['lost']}</td>
  <td style="padding:6px 8px;text-align:left">{teu_bar}</td>
  <td style="padding:6px 8px;text-align:center;background:{loss_bg};font-weight:600">{b['total']}</td>
</tr>
"""
    return f"""
<h2 style="color:#1e3a5f;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📉 Top Losing Lanes — Period to Date (sliced by lane, sorted by TEU lost)</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">Excludes NO_RESPONSE losses (those are in the "Not Quoted" section below).</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:linear-gradient(135deg,#dc2626 0%,#ef4444 100%);color:white">
    <th style="padding:8px;text-align:left">Lane</th>
    <th style="padding:8px;text-align:center">Times Lost</th>
    <th style="padding:8px;text-align:left">TEU Lost</th>
    <th style="padding:8px;text-align:center">Total Reqs</th>
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
        bg = "#ffffff" if alt else "#fffbeb"
        alt = not alt
        request_ts = r.get('request_timestamp') or ''
        imid_short = (r.get('source_imids') or ['—'])[0]
        if imid_short and imid_short != '—':
            imid_short = imid_short[:24] + '…' if len(imid_short) > 24 else imid_short
        body += f"""
<tr style="background:{bg};border-bottom:1px solid #fde68a">
  <td style="padding:6px 8px;white-space:nowrap">{_esc(r.get('request_date') or '—')}</td>
  <td style="padding:6px 8px;font-size:11px;color:#64748b">{_esc(r.get('lonny_time_pt') or '—')}</td>
  <td style="padding:6px 8px">{_esc(r.get('origin') or '—')}</td>
  <td style="padding:6px 8px">{_esc(r.get('destination') or '—')}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('containers') or '—')}</td>
  <td style="padding:6px 8px;text-align:center">{_esc(r.get('teu_requested') or '—')}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('eta_requested') or 'no ETA on request')}</td>
  <td style="padding:6px 8px;font-size:11px">{_esc(r.get('ol_responder') or '—')}</td>
  <td style="padding:6px 8px;text-align:center;font-weight:bold;color:#b45309">{_age_days(request_ts)}</td>
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
<h2 style="color:#d97706;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #fde68a;padding-bottom:8px">⚠️ Not Quoted — Last {NQ_DISPLAY_WINDOW_DAYS} Days ({len(rows)} listed • {_esc(total_label)}{_esc(teu_label)})</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">Full request audit — every field needed to root-cause why OL did not respond.{_esc(older_note)}</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:#d97706;color:white">
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


def _pending_html(rows):
    if not rows:
        return ""
    body = ""
    alt = True
    for r in rows:
        bg = "#ffffff" if alt else "#f5f3ff"
        alt = not alt
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px">{_esc(r.get('request_date') or '—')}</td>
  <td style="padding:6px 8px">{_esc(r.get('lane') or '—')}</td>
  <td style="padding:6px 8px">{_esc(r.get('carrier_quoted') or '—')}</td>
  <td style="padding:6px 8px">{_esc(r.get('ol_rate') or '—')}</td>
</tr>
"""
    return f"""
<h2 style="color:#7c3aed;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #c4b5fd;padding-bottom:8px">⏳ Pending Hilmar Response ({len(rows)})</h2>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:#7c3aed;color:white">
    <th style="padding:8px;text-align:left">Date</th>
    <th style="padding:8px;text-align:left">Lane</th>
    <th style="padding:8px;text-align:left">Carrier</th>
    <th style="padding:8px;text-align:left">Rate</th>
  </tr>
  {body}
</table>
"""


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
        '<tr style="background:#e2e8f0;font-weight:bold;border-top:2px solid #1e3a5f">'
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
    return f"""
<h2 style="color:#1e3a5f;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #cbd5e1;padding-bottom:8px">🌐 Volume by Trade Region</h2>
<p style="margin:0 0 4px;font-size:11px;color:#64748b">Destinations grouped by trade region. Totals reconcile to summary KPIs.</p>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">{_esc(recon)}</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:#1e3a5f;color:white">
    <th style="padding:8px;text-align:left">Region</th>
    <th style="padding:8px;text-align:center">Reqs</th>
    <th style="padding:8px;text-align:center">W</th>
    <th style="padding:8px;text-align:center">Q&amp;L</th>
    <th style="padding:8px;text-align:center">NQ</th>
    <th style="padding:8px;text-align:center">Pend</th>
    <th style="padding:8px;text-align:center">TEU Req</th>
    <th style="padding:8px;text-align:center">TEU Won</th>
    <th style="padding:8px;text-align:center">Win %</th>
  </tr>
  {rows_html}
</table>
"""


FOOTER_HTML = """
<div style="background:#f0f4f8;border:1px solid #cbd5e1;border-radius:8px;padding:16px;margin-bottom:20px">
  <h3 style="margin:0 0 8px;color:#1e3a5f;font-size:14px">📎 ATTACHED FILES:</h3>
  <p style="margin:4px 0;font-size:12px">• <b>hilmar-dashboard.html</b> — Open in any browser (works mobile + desktop, no software needed)</p>
  <p style="margin:4px 0;font-size:12px">• <b>hilmar-report.pdf</b> — Printable report</p>
  <h3 style="margin:12px 0 8px;color:#1e3a5f;font-size:14px">📖 DASHBOARD TAB GUIDE:</h3>
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

    html_body = _header_html(report_label, date_range, updated_label)
    html_body += _today_block_html(report_label, new_req, ol_resp, status_ch, pending)
    html_body += _kpi_block_html(data.get("summary", {}) or {}, requests=data.get("requests", []) or [], report_date=report_date)
    html_body += _week_block_html(week_rows)
    html_body += _carrier_block_html(carrier_rows)
    html_body += _trade_region_html(data, data.get("summary", {}) or {})
    html_body += _winning_lanes_html(winning_lanes)
    html_body += _losing_lanes_html(losing_lanes)
    html_body += _nq_html(nq_rows, total_nq=nq_total_count, teu_total=nq_total_teu)
    html_body += _pending_html(pend_rows)
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
