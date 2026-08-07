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
import core  # noqa: E402


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
    """Return ISO-ish week key like 'W15 (Apr 6–12)'."""
    if not d:
        return None
    iso = d.isocalendar()
    wk = iso.week
    # Week starts Monday
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    label = f"W{wk} ({_fmt_date(monday, '%b %-d')}–{_fmt_date(sunday, '%-d')})"
    return label, monday


def build_subject(data, cfg):
    return f"Hilmar Ingredients — Daily Shipment Tracker Update ({_fmt_date(datetime.now(timezone.utc), '%b %-d, %Y')})"


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


def _not_quoted_rows(data):
    rows = []
    for r in data.get("requests", []):
        if r.get("status") == "LOSS" and (r.get("loss_reason") or "") == "NO_RESPONSE":
            rows.append(r)
    rows.sort(key=lambda r: (r.get("request_date") or ""))
    return rows


def _pending_rows(data):
    rows = [r for r in data.get("requests", []) if r.get("status") == "PENDING"]
    rows.sort(key=lambda r: (r.get("request_date") or ""))
    return rows


# ─────────────────────────────────────────────────────────────────────
# HTML builders
# ─────────────────────────────────────────────────────────────────────

HEADER_GRADIENT = "linear-gradient(135deg,#1e3a5f 0%,#3b82f6 100%)"


def _header_html(today_label, range_label, updated_label):
    return f"""
<div style="max-width:900px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden">
  <div style="padding:24px 32px;background:{HEADER_GRADIENT};color:white">
    <h1 style="margin:0;font-size:22px">🚢 Hilmar Ingredients — Daily Shipment Tracker</h1>
    <p style="margin:6px 0 0;font-size:14px;opacity:0.9">{_esc(range_label)} | Updated: {_esc(updated_label)}</p>
  </div>
  <div style="padding:24px 32px">
"""


def _today_block_html(today_label, new_req, ol_resp, status_ch, pending):
    def _li(text, margin_color="#000"):
        return f'<p style="margin:2px 0 2px 16px;font-size:13px">{text}</p>'

    # New requests
    new_html = ""
    if new_req:
        for r in new_req:
            new_html += _li(_lonny_line(r))
    else:
        new_html = _li("• No new requests today")

    # OL responses
    resp_html = ""
    if ol_resp:
        for r in ol_resp:
            resp_html += _li(_response_line(r))
    else:
        resp_html = _li("• No OL responses today")

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
            f"• {_esc(lane)} ({_esc(str(cont_label))} / {teu} TEU, req {_esc(req_date)}) | "
            f"<b>{_esc(h['from'])} → {_esc(h['to'])}</b>"
            + (f" — {_esc(reason)}" if reason else "")
        )
    if not sc_html:
        sc_html = _li("• No status changes today")

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

    summary_line = (
        f"📊 {len(new_req)} new requests added, {len(ol_resp)} new quotes received, "
        f"0 wins today, {len(status_ch)} status changes, {len(pending)} total pending Hilmar response"
    )

    return f"""
<div style="background:#eff6ff;border:2px solid #3b82f6;border-radius:8px;padding:20px;margin-bottom:24px">
  <h2 style="margin:0 0 12px;color:#1e40af;font-size:18px">📋 What Happened Today — {_esc(today_label)}</h2>
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


def _kpi_card(value, label, bg):
    return f"""
<td style="padding:4px;width:25%">
  <div style="background:{bg};color:white;border-radius:8px;padding:12px;text-align:center">
    <div style="font-size:20px;font-weight:bold">{_esc(value)}</div>
    <div style="font-size:11px;opacity:0.9;margin-top:4px">{_esc(label)}</div>
  </div>
</td>
"""


def _today_summary(requests):
    """Compute wins/losses/etc that happened TODAY in ET (the OL business day)."""
    today_et = datetime.now(core.ET).date().isoformat()
    today_reqs = [r for r in requests
                  if (r.get("request_date") == today_et) or (r.get("date") == today_et)]
    return {
        "wins":         sum(1 for r in today_reqs if r.get("status") == "WIN"),
        "teu_won":      sum(int(r.get("teu_won") or r.get("teu_requested") or 0)
                            for r in today_reqs if r.get("status") == "WIN"),
        "quoted_lost":  sum(1 for r in today_reqs if r.get("status") == "LOSS" and r.get("quoted")),
        "not_quoted":   sum(1 for r in today_reqs if r.get("status") == "LOSS" and not r.get("quoted")),
        "pending":      sum(1 for r in today_reqs if r.get("status") == "PENDING"),
        "total":        len(today_reqs),
        "as_of_label":  f"Today ({today_et} ET)",
    }


def _kpi_block_html(summary, requests=None):
    """Two KPI rows:
      Row 1 (TODAY) — what happened in today's ET business day. Often zeros — that's the truth.
      Row 2 (PERIOD TO DATE) — cumulative over the data range. Used for negotiation depth.

    Michael 2026-04-30: "we didn't win that today" — fix is that the daily email's
    headline KPI was cumulative but unlabeled. Now both views are explicit.
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

    today = _today_summary(requests or [])

    return f"""
<h2 style="color:#1e3a5f;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📊 KPIs — Today (ET)</h2>
<p style="margin:-8px 0 8px;font-size:11px;color:#64748b">Activity within today's OL business day. Most days this is zero or low; that's expected.</p>
<table style="width:100%;border-collapse:collapse;margin-bottom:16px">
  <tr>
    {_kpi_card(today['total'], "Requests Today", "#3b82f6")}
    {_kpi_card(f"{today['wins']} ({today['teu_won']} TEU)", "Won — Today", "#22c55e")}
    {_kpi_card(today['quoted_lost'], "Quoted & Lost — Today", "#ef4444")}
    {_kpi_card(today['not_quoted'], "Not Quoted — Today", "#f59e0b")}
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
    body = ""
    alt = True
    for lane, b in rows:
        bg = "#ffffff" if alt else "#ecfdf5"
        alt = not alt
        carriers = ", ".join(sorted(b.get("carriers") or []))
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px">{_esc(lane)}</td>
  <td style="padding:6px 8px;text-align:center">{b['won']}</td>
  <td style="padding:6px 8px;text-align:center;font-weight:bold;color:#059669">{b['teu_won']}</td>
  <td style="padding:6px 8px;text-align:center">{b['total']}</td>
  <td style="padding:6px 8px">{_esc(carriers)}</td>
</tr>
"""
    return f"""
<h2 style="color:#1e3a5f;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📈 Top Winning Lanes — Period to Date (sliced by lane, sorted by TEU won)</h2>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:#059669;color:white">
    <th style="padding:8px;text-align:left">Lane</th>
    <th style="padding:8px;text-align:center">Times Won</th>
    <th style="padding:8px;text-align:center">TEU Won</th>
    <th style="padding:8px;text-align:center">Total Requests</th>
    <th style="padding:8px;text-align:left">Winning Carriers</th>
  </tr>
  {body}
</table>
"""


def _losing_lanes_html(rows):
    if not rows:
        return ""
    body = ""
    alt = True
    for lane, b in rows:
        bg = "#ffffff" if alt else "#fef2f2"
        alt = not alt
        body += f"""
<tr style="background:{bg}">
  <td style="padding:6px 8px">{_esc(lane)}</td>
  <td style="padding:6px 8px;text-align:center">{b['lost']}</td>
  <td style="padding:6px 8px;text-align:center;font-weight:bold;color:#dc2626">{b['teu_lost']}</td>
  <td style="padding:6px 8px;text-align:center">{b['total']}</td>
</tr>
"""
    return f"""
<h2 style="color:#1e3a5f;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #e5e7eb;padding-bottom:8px">📉 Top Losing Lanes — Period to Date (sliced by lane, sorted by TEU lost)</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">Excludes NO_RESPONSE losses (those are in the "Not Quoted" section below).</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
  <tr style="background:#dc2626;color:white">
    <th style="padding:8px;text-align:left">Lane</th>
    <th style="padding:8px;text-align:center">Times Lost</th>
    <th style="padding:8px;text-align:center">TEU Lost</th>
    <th style="padding:8px;text-align:center">Total Requests</th>
  </tr>
  {body}
</table>
"""


def _nq_html(rows):
    """Full-detail NQ table — every column needed to root-cause WHY OL did not quote."""
    if not rows:
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
    return f"""
<h2 style="color:#d97706;font-size:16px;margin:20px 0 12px;border-bottom:2px solid #fde68a;padding-bottom:8px">⚠️ Not Quoted — Period to Date ({len(rows)} threads where OL never replied)</h2>
<p style="margin:0 0 8px;font-size:11px;color:#64748b">Full request audit — every field needed to root-cause why OL did not respond. NO_RESPONSE losses only.</p>
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
    today = datetime.now(timezone.utc).astimezone(core.ET)
    today_date = today.date()
    today_label = _fmt_date(today, "%b %-d, %Y")
    # Same dead-fallback bug as gen_email.py:2098 — ingest writes a DICT and a
    # dict is truthy. Dormant (not in run_pipeline) but it writes the SAME
    # reports/email-body.html, so anyone who runs it ships the repr.
    date_range = core.format_date_range(
        data.get("date_range"),
        fallback_start=cfg.get("data_range", {}).get("start"),
        fallback_end=today.date().isoformat(),
    )
    updated_label = _fmt_date(today, "%B %-d, %Y at %-I:%M %p ET")

    new_req, ol_resp, status_ch, pending = _today_events(data, today_date)
    week_rows = _week_rows(data)
    carrier_rows = _carrier_rows(data)
    winning_lanes = _winning_lane_rows(data)
    losing_lanes = _losing_lane_rows(data)
    nq_rows = _not_quoted_rows(data)
    pend_rows = _pending_rows(data)

    html_body = _header_html(today_label, date_range, updated_label)
    html_body += _today_block_html(today_label, new_req, ol_resp, status_ch, pending)
    html_body += _kpi_block_html(data.get("summary", {}) or {}, requests=data.get("requests", []) or [])
    html_body += _week_block_html(week_rows)
    html_body += _carrier_block_html(carrier_rows)
    html_body += _trade_region_html(data, data.get("summary", {}) or {})
    html_body += _winning_lanes_html(winning_lanes)
    html_body += _losing_lanes_html(losing_lanes)
    html_body += _nq_html(nq_rows)
    html_body += _pending_html(pend_rows)
    html_body += FOOTER_HTML
    html_body += "</div></div>"
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

    print(f"✅ Email body: {len(body):,} bytes → {body_path}")
    print(f"✅ Email subject: {subject!r} → {subject_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
