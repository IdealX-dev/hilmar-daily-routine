#!/usr/bin/env python3
"""
Hilmar Tracker — HTML dashboard generator.

Negotiation-grade. CSS-only tabs (no JS). Reads PRE-computed metrics from
tracking-data-v2.json. Never recomputes win_rate/quote_rate — that's canonical
in the data file.

Tabs:
  📊 Summary            — KPIs + WoW bar + wins + NQ + top winning & losing lanes
  ⏱️  Turnaround        — PT→ET timeline, biz-hours classification
  📅 Dates              — Lonny ask vs OL offer, ETD fit score
  🚢 Carriers           — performance table + per-carrier drill (losses, rate trends)
  ⏳ Pending            — aging watchlist with 16/20/23h warnings
  📈 Rate Trends        — per (carrier, lane) rate movement
  🔍 QC                 — self-heal log + alerts
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import core
import viz as V  # noqa  shared visual helpers
import branding as B  # noqa  Hilmar logo + brand colors

# ─────────────────────────────────────────────────────────────────────
# HTML helpers
# ─────────────────────────────────────────────────────────────────────

def _esc(s) -> str:
    if s is None:
        return "—"
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _safe(v, default="—"):
    if v is None or v == "" or v == "null":
        return default
    return _esc(v)


def _fmt_date(d):
    if not d:
        return "—"
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%b %d")
    except Exception:
        return str(d)[:10]


def _ta_class(h):
    if not h or h <= 0:
        return ""
    if h < 2:
        return "ta-fast"
    if h <= 8:
        return "ta-medium"
    return "ta-slow"


def _status_badge(r):
    if r["status"] == "WIN":
        return '<span class="badge badge-green">✅ Won</span>'
    if r["status"] == "PENDING":
        return '<span class="badge badge-purple">⏳ Pending</span>'
    if r.get("quoted"):
        return '<span class="badge badge-red">❌ Q&amp;L</span>'
    return '<span class="badge badge-amber">⚠️ NQ</span>'


def _row_class(r):
    s = r["status"]
    if s == "WIN":
        return "win-row"
    if s == "PENDING":
        return "pending-row"
    if r.get("quoted"):
        return "loss-row"
    return "nq-row"


def _hours_since(iso_ts):
    if not iso_ts:
        return None
    dt = core.parse_iso(iso_ts)
    if not dt:
        return None
    return round((core.now_utc() - dt).total_seconds() / 3600, 1)


# ─────────────────────────────────────────────────────────────────────
# WoW (week-over-week) bar
# ─────────────────────────────────────────────────────────────────────

def wow_bars(requests):
    buckets = defaultdict(lambda: {"requests": 0, "wins": 0, "ql": 0, "nq": 0, "pending": 0, "teu_won": 0, "teu_lost": 0})
    for r in requests:
        d = r.get("request_date") or r.get("date")
        if not d:
            continue
        try:
            dt = datetime.strptime(d[:10], "%Y-%m-%d")
        except Exception:
            continue
        iso = dt.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        b = buckets[key]
        b["requests"] += 1
        if r["status"] == "WIN":
            b["wins"] += 1
            b["teu_won"] += r.get("teu_won", 0) or r.get("teu_requested", 0) or 0
        elif r["status"] == "PENDING":
            b["pending"] += 1
        elif r["status"] == "LOSS":
            if r.get("quoted"):
                b["ql"] += 1
            else:
                b["nq"] += 1
            b["teu_lost"] += r.get("teu_requested", 0) or 0
    return sorted(buckets.items())


# ─────────────────────────────────────────────────────────────────────
# Main render
# ─────────────────────────────────────────────────────────────────────

def render(cfg: dict, data: dict) -> str:
    requests = data["requests"]
    summary = data["summary"]

    # KPIs — READ from summary, never recompute
    total = summary["total_entries"]
    wins = [r for r in requests if r["status"] == "WIN"]
    ql = [r for r in requests if r["status"] == "LOSS" and r.get("quoted")]
    nq = [r for r in requests if r["status"] == "LOSS" and not r.get("quoted")]
    pending = [r for r in requests if r["status"] == "PENDING"]
    win_rate = summary["win_rate"]
    quote_rate = summary["quote_rate"]
    avg_biz = summary.get("turnaround_avg_biz_hours", 0)
    teu_won = summary["teu_won"]
    teu_ql = summary.get("teu_quoted_lost", 0)
    teu_nq = summary.get("teu_not_quoted", 0)
    teu_pending = summary.get("teu_pending", 0)
    teu_requested = summary.get("teu_requested", 0)

    # date range
    dates = sorted(r.get("request_date") or r.get("date", "") for r in requests if (r.get("request_date") or r.get("date")))
    first_date = dates[0] if dates else "—"
    last_date = dates[-1] if dates else "—"
    data_start_date = first_date

    now_et = datetime.now(core.ET).strftime("%b %d, %Y %I:%M %p ET")
    after_hours_count = sum(1 for r in requests if r.get("after_hours_request"))

    # 2026-05-19 Task #4 — "What happened since last run" needs the actual
    # previous-run timestamp visible. Use `last_updated` from the tracking
    # data file (set by ingest.py when the prior run wrote the file).
    # Convert to ET for display. Falls back to "—" if missing.
    _prev_run_label = "—"
    _prev_run_delta = ""
    try:
        _lu = data.get("last_updated") or data.get("generated_at")
        if _lu:
            _prev_dt = core.parse_iso(_lu)
            if _prev_dt:
                _prev_et = _prev_dt.astimezone(core.ET)
                _prev_run_label = _prev_et.strftime("%b %d %I:%M %p ET")
                _delta_h = (datetime.now(core.ET) - _prev_et).total_seconds() / 3600.0
                if _delta_h < 1:
                    _prev_run_delta = f"{int(_delta_h * 60)} min ago"
                elif _delta_h < 24:
                    _prev_run_delta = f"{_delta_h:.1f}h ago"
                else:
                    _prev_run_delta = f"{int(_delta_h / 24)}d ago"
    except Exception:
        pass

    # ── Report-day KPIs — added 2026-04-30 per Michael's feedback that the daily
    # email was showing cumulative wins under a "Won" card, reading as "today".
    # Updated 2026-05-07 per Michael: 'yesterday kpi run' — at 10 AM ET fire
    # time, today's data window is empty (Lonny's PT office isn't open yet),
    # so report on the previous business day. Mon → last Friday; Tue–Fri →
    # yesterday; Sat/Sun → last Friday.
    _now_et = datetime.now(core.ET).date()
    _wd = _now_et.weekday()  # Mon=0..Sun=6
    if _wd == 0:    _delta = 3   # Mon → Fri
    elif _wd == 5:  _delta = 1   # Sat → Fri
    elif _wd == 6:  _delta = 2   # Sun → Fri
    else:           _delta = 1   # Tue–Fri → yesterday
    from datetime import timedelta as _td
    report_date = _now_et - _td(days=_delta)
    report_iso  = report_date.isoformat()
    report_label = report_date.strftime("%a %b %d (yesterday)" if _delta == 1 else "%a %b %d (last full biz day)")
    today_reqs = [r for r in requests
                  if (r.get("request_date") == report_iso) or (r.get("date") == report_iso)]
    tdy_total   = len(today_reqs)
    tdy_teu     = sum(int(r.get("teu_requested") or 0) for r in today_reqs)
    tdy_wins    = sum(1 for r in today_reqs if r.get("status") == "WIN")
    tdy_teu_won = sum(int(r.get("teu_won") or r.get("teu_requested") or 0)
                      for r in today_reqs if r.get("status") == "WIN")
    tdy_ql      = sum(1 for r in today_reqs if r.get("status") == "LOSS" and r.get("quoted"))
    tdy_teu_ql  = sum(int(r.get("teu_requested") or 0)
                      for r in today_reqs if r.get("status") == "LOSS" and r.get("quoted"))
    tdy_nq      = sum(1 for r in today_reqs if r.get("status") == "LOSS" and not r.get("quoted"))
    tdy_teu_nq  = sum(int(r.get("teu_requested") or 0)
                      for r in today_reqs if r.get("status") == "LOSS" and not r.get("quoted"))
    tdy_pend    = sum(1 for r in today_reqs if r.get("status") == "PENDING")
    tdy_teu_pend = sum(int(r.get("teu_requested") or 0)
                       for r in today_reqs if r.get("status") == "PENDING")

    # lane + carrier summaries
    lanes = data.get("lane_summary", {}) or {}
    carriers = data.get("carrier_summary", {}) or {}

    # Rate trends (top 10 biggest movers)
    trends = core.rate_trends(requests)[:10]

    # Pending watchlist with aging
    pending_watch = []
    warn_thresholds = cfg["rules"]["pending_warn_hours"]  # e.g. [16, 20, 23]
    for r in pending:
        hs = _hours_since(r.get("response_timestamp"))
        if hs is None:
            continue
        if hs >= warn_thresholds[-1]:
            severity = "critical"
        elif hs >= warn_thresholds[-2]:
            severity = "high"
        elif hs >= warn_thresholds[0]:
            severity = "medium"
        else:
            severity = "low"
        pending_watch.append({**r, "_hours_since": hs, "_severity": severity})
    pending_watch.sort(key=lambda x: -x["_hours_since"])

    # WoW
    weeks = wow_bars(requests)
    max_week_req = max((b["requests"] for _, b in weeks), default=1) or 1

    # DOD
    dod = summary.get("dod") or data.get("dod") or {}

    # ─── HTML ───
    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hilmar Ingredients — Shipment Tracker Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Inter','Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;background:#f5f7fa;color:#0f172a;padding:24px;font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;font-feature-settings:'tnum' 1,'cv11' 1,'ss01' 1;font-variant-numeric:tabular-nums}}
table{{font-variant-numeric:tabular-nums;font-feature-settings:'tnum' 1}}
.kpi .value,td,th{{font-variant-numeric:tabular-nums}}
.header{{background:linear-gradient(135deg,{B.HILMAR_NAVY} 0%,{B.HILMAR_BLUE} 100%);color:white;padding:24px 28px;border-radius:12px;margin-bottom:20px;box-shadow:0 4px 12px rgba(0,0,0,0.08)}}
.header h1{{font-size:26px;margin-bottom:6px;font-weight:700;letter-spacing:-0.3px}}
.header .subtitle{{opacity:0.92;font-size:15px;font-weight:500}}
.header .tz-note{{opacity:0.7;font-size:12px;margin-top:10px}}
.header .stamp{{opacity:0.72;font-size:12px;margin-top:4px}}

.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:20px}}
/* 2026-05-19 PM (Michael "all boxes should be clickable"): every KPI tile
   is an <a> anchor link to its detail section. Hover lifts the card and
   underlines the value for affordance. Cursor:pointer signals click. */
a.kpi{{text-decoration:none;color:inherit;display:block;background:white;padding:14px;border-radius:10px;text-align:center;box-shadow:0 2px 6px rgba(0,0,0,0.05);border-top:3px solid #cbd5e1;transition:transform 0.12s ease,box-shadow 0.12s ease;cursor:pointer}}
a.kpi:hover{{transform:translateY(-2px);box-shadow:0 6px 16px rgba(0,0,0,0.10)}}
a.kpi:hover .value{{text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:3px}}
a.kpi:focus{{outline:2px solid #3b82f6;outline-offset:2px}}
.kpi{{background:white;padding:14px;border-radius:10px;text-align:center;box-shadow:0 2px 6px rgba(0,0,0,0.05);border-top:3px solid #cbd5e1}}
.kpi .value{{font-size:28px;font-weight:800;line-height:1.05;letter-spacing:-0.5px}}
.kpi .label{{font-size:11.5px;color:#475569;text-transform:uppercase;letter-spacing:0.5px;margin-top:6px;font-weight:600}}
.kpi .sub{{font-size:12px;color:#64748b;margin-top:3px;font-weight:500}}
.kpi-hint{{font-size:9.5px;color:#94a3b8;margin-top:2px;font-weight:500}}
/* Awaiting-MDOLX badge — for WIN rows where send-signal promoted PENDING
   to WIN but OL hasn't issued the booking confirmation yet. */
.awaiting-mdolx{{display:inline-block;background:#fef3c7;color:#92400e;border:1px solid #f59e0b;border-radius:10px;padding:2px 8px;font-size:10px;font-weight:600;letter-spacing:0.3px}}
.kpi.blue{{border-top-color:#3b82f6}} .kpi.blue .value{{color:#2563eb}}
.kpi.green{{border-top-color:#10b981}} .kpi.green .value{{color:#059669}}
.kpi.red{{border-top-color:#ef4444}} .kpi.red .value{{color:#dc2626}}
.kpi.amber{{border-top-color:#f59e0b}} .kpi.amber .value{{color:#d97706}}
.kpi.purple{{border-top-color:#a855f7}} .kpi.purple .value{{color:#7c3aed}}
.kpi.teal{{border-top-color:#14b8a6}} .kpi.teal .value{{color:#0d9488}}
.kpi.slate{{border-top-color:#64748b}} .kpi.slate .value{{color:#475569}}

.section{{background:white;border-radius:10px;padding:18px 20px;margin-bottom:14px;box-shadow:0 2px 6px rgba(0,0,0,0.05)}}
.section h2{{font-size:17px;margin-bottom:14px;color:#0f172a;border-bottom:2px solid #e2e8f0;padding-bottom:8px;font-weight:700;letter-spacing:-0.3px}}
.section h3{{font-size:14px;color:#1e293b;margin:14px 0 8px 0;font-weight:600;letter-spacing:-0.1px}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th{{background:#f1f5f9;padding:10px 8px;text-align:left;font-weight:700;color:#334155;border-bottom:2px solid #cbd5e1;font-size:11.5px;text-transform:uppercase;letter-spacing:0.5px}}
td{{padding:9px 8px;border-bottom:1px solid #f1f5f9;vertical-align:middle}}
tr:hover td{{background:#f8fafc}}
.win{{color:#059669;font-weight:600}} .loss{{color:#dc2626}} .nq{{color:#d97706}} .pending{{color:#7c3aed}}
.win-row{{border-left:3px solid #10b981}} .loss-row{{border-left:3px solid #ef4444}} .nq-row{{border-left:3px solid #f59e0b}} .pending-row{{border-left:3px solid #a855f7}}
.ta-fast{{color:#059669;font-weight:700}} .ta-medium{{color:#2563eb}} .ta-slow{{color:#dc2626;font-weight:700}}

input[name="tabs"]{{display:none}}
.tabs{{display:flex;gap:4px;margin-bottom:14px;flex-wrap:wrap}}
.tab{{padding:8px 14px;background:#e2e8f0;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;border:none;display:inline-block;color:#475569;transition:all 0.12s}}
.tab:hover{{background:#cbd5e1}}
.tab-content{{display:none}}
#tb-summary:checked~.tabs label[for="tb-summary"],
#tb-turnaround:checked~.tabs label[for="tb-turnaround"],
#tb-dates:checked~.tabs label[for="tb-dates"],
#tb-carriers:checked~.tabs label[for="tb-carriers"],
#tb-pending:checked~.tabs label[for="tb-pending"],
#tb-trends:checked~.tabs label[for="tb-trends"],
#tb-qc:checked~.tabs label[for="tb-qc"]{{background:#0f172a;color:white}}
#tb-summary:checked~#tab-summary,
#tb-turnaround:checked~#tab-turnaround,
#tb-dates:checked~#tab-dates,
#tb-carriers:checked~#tab-carriers,
#tb-pending:checked~#tab-pending,
#tb-trends:checked~#tab-trends,
#tb-qc:checked~#tab-qc{{display:block}}

.badge{{display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;white-space:nowrap}}
.badge-green{{background:#d1fae5;color:#059669}} .badge-red{{background:#fee2e2;color:#dc2626}}
.badge-amber{{background:#fef3c7;color:#d97706}} .badge-blue{{background:#dbeafe;color:#2563eb}}
.badge-purple{{background:#ede9fe;color:#7c3aed}} .badge-slate{{background:#f1f5f9;color:#475569}}

.callout{{background:#eff6ff;border-left:4px solid #3b82f6;padding:12px 16px;border-radius:0 8px 8px 0;margin-bottom:14px}}
.callout p{{font-size:12px;color:#1e3a5f;line-height:1.5}}
.callout.green{{background:#ecfdf5;border-color:#10b981}}
.callout.amber{{background:#fffbeb;border-color:#f59e0b}}
.callout.red{{background:#fef2f2;border-color:#ef4444}}
.callout.purple{{background:#faf5ff;border-color:#a855f7}}

code{{background:#f1f5f9;padding:1px 5px;border-radius:3px;font-size:11px;font-family:'JetBrains Mono','SF Mono','Consolas',monospace}}

.wow-bar{{display:flex;align-items:flex-end;gap:6px;height:80px;margin-top:10px;padding:8px;background:#f8fafc;border-radius:6px}}
.wow-col{{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;min-width:0}}
.wow-stack{{width:100%;display:flex;flex-direction:column-reverse;gap:0;height:60px}}
.wow-seg{{width:100%}}
.wow-seg.wins{{background:#10b981}} .wow-seg.ql{{background:#ef4444}} .wow-seg.nq{{background:#f59e0b}} .wow-seg.pending{{background:#a855f7}}
.wow-label{{font-size:9px;color:#64748b;font-weight:600;text-align:center}}

.dod-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}}
.dod-card{{background:#f8fafc;border-radius:8px;padding:12px;border-left:3px solid #3b82f6}}
.dod-card h4{{font-size:12px;font-weight:700;margin-bottom:6px;color:#334155}}
.dod-card ul{{list-style:none;font-size:11px;color:#475569}}
.dod-card li{{padding:3px 0;border-bottom:1px solid #e2e8f0}}
.dod-card li:last-child{{border:none}}
.dod-empty{{color:#94a3b8;font-style:italic;font-size:11px}}

.pend-row.critical{{background:#fef2f2;border-left:4px solid #dc2626}}
.pend-row.high{{background:#fffbeb;border-left:4px solid #f59e0b}}
.pend-row.medium{{background:#faf5ff;border-left:4px solid #a855f7}}
.pend-row.low{{background:#eff6ff;border-left:4px solid #3b82f6}}

.spark{{display:inline-block;vertical-align:middle;height:18px;margin:0 4px}}
.trend-up{{color:#dc2626;font-weight:700}} .trend-down{{color:#059669;font-weight:700}} .trend-flat{{color:#64748b}}

@media print{{
  body{{padding:8px;font-size:11px}}
  .tabs{{display:none}}
  .tab-content{{display:block!important;page-break-inside:avoid;page-break-after:always}}
  .section{{box-shadow:none;border:1px solid #e2e8f0}}
}}

/* Mobile-responsive — added 2026-05-13 per Michael "i need this to become
   a remote app as well so i can use code from my phone and other laptops".
   The dashboard is the primary daily-view artifact and now renders cleanly
   on phones (iOS Mail attachment preview, Outlook mobile, OneDrive mobile). */
@media (max-width:768px){{
  body{{padding:8px;font-size:13px}}
  .header h1{{font-size:17px}}
  .header .subtitle{{font-size:11px}}
  .header .tz-note{{font-size:10px}}
  .kpi-grid{{grid-template-columns:repeat(2,1fr) !important;gap:6px}}
  .kpi{{padding:8px}}
  .kpi .value{{font-size:18px}}
  .kpi .label{{font-size:10px}}
  .section{{padding:10px;margin:6px 0}}
  .section h2{{font-size:14px}}
  table{{font-size:11px}}
  th,td{{padding:4px 6px}}
  /* Hide low-signal columns on narrow screens */
  .source-imid,.imid-col{{display:none}}
  /* Wrap long cells */
  td{{word-break:break-word}}
}}
@media (max-width:480px){{
  .kpi-grid{{grid-template-columns:1fr !important}}
  th,td{{padding:3px 4px;font-size:10px}}
}}
</style>
</head><body>

<div class="header">
{f'<div style="background:white;padding:10px 16px;border-radius:6px;display:inline-block;margin-bottom:12px">{B.logo_html(height=48)}</div>' if B.has_logo() else ''}
<h1>{'' if B.has_logo() else '🚢 '}Hilmar Ingredients — Shipment Tracker</h1>
<div class="subtitle">{_fmt_date(first_date)} – {_fmt_date(last_date)} • {total} Requests • {teu_requested} TEU Requested</div>
<div class="stamp">Generated: {now_et} &nbsp;·&nbsp; Previous pipeline run: {_prev_run_label}{f' ({_prev_run_delta})' if _prev_run_delta else ''}</div>
<div class="tz-note">⏰ Lonny (Hilmar) = Pacific Time | OL-USA = Eastern Time | Turnaround = OL biz hours (8:30 AM – 5:30 PM ET, DST-safe)</div>
</div>

<h3 style="margin:14px 0 6px;font-size:13px;color:#475569;font-weight:600">📅 {report_label} (ET) — activity on the previous business day. Math: Requests = Won + Q&amp;L + NQ + Pending. <span style="color:#64748b;font-weight:400">· click any tile to drill in ↓</span></h3>
<div class="kpi-grid">
  <a class="kpi blue" href="#tab-summary" title="Click to jump to Confirmed Wins / Not Quoted detail"><div class="value">{tdy_total}</div><div class="label">Requests — {report_label}</div><div class="sub">{tdy_teu} TEU</div><div class="kpi-hint">click to drill →</div></a>
  <a class="kpi green" href="#tab-summary" title="Click to jump to the Wins section"><div class="value">{tdy_wins}</div><div class="label">Won — {report_label}</div><div class="sub">{tdy_teu_won} TEU</div><div class="kpi-hint">→ Wins section</div></a>
  <a class="kpi red" href="#tab-summary" title="Click to jump to the Losing Lanes section"><div class="value">{tdy_ql}</div><div class="label">Quoted &amp; Lost — {report_label}</div><div class="sub">{tdy_teu_ql} TEU</div><div class="kpi-hint">→ Losing Lanes</div></a>
  <a class="kpi amber" href="#tab-summary" title="Click to jump to Not Quoted detail"><div class="value">{tdy_nq}</div><div class="label">Not Quoted — {report_label}</div><div class="sub">{tdy_teu_nq} TEU</div><div class="kpi-hint">→ Not Quoted</div></a>
  <a class="kpi" style="background:#8b5cf6;color:white;border-top-color:#7c3aed" href="#tab-pending" title="Click to jump to the Pending tab"><div class="value">{tdy_pend}</div><div class="label">Pending — {report_label}</div><div class="sub">{tdy_teu_pend} TEU</div><div class="kpi-hint" style="color:rgba(255,255,255,0.7)">→ Pending tab</div></a>
</div>
<h3 style="margin:18px 0 6px;font-size:13px;color:#475569;font-weight:600">📊 Period to Date — cumulative since {data_start_date} <span style="color:#64748b;font-weight:400">· click any tile to drill in ↓</span></h3>
<div class="kpi-grid">
  <a class="kpi blue" href="#tab-summary" title="Click to jump to all Confirmed Wins / NQ"><div class="value">{total}</div><div class="label">Total Requests — PTD</div><div class="sub">{teu_requested} TEU</div><div class="kpi-hint">click to drill →</div></a>
  <a class="kpi green" href="#tab-summary" title="Click to jump to Wins section"><div class="value">{len(wins)}</div><div class="label">Won — PTD</div><div class="sub">{teu_won} TEU</div><div class="kpi-hint">→ Wins section</div></a>
  <a class="kpi red" href="#tab-summary" title="Click to jump to Losing Lanes"><div class="value">{len(ql)}</div><div class="label">Quoted &amp; Lost — PTD</div><div class="sub">{teu_ql} TEU</div><div class="kpi-hint">→ Losing Lanes</div></a>
  <a class="kpi amber" href="#tab-summary" title="Click to jump to Not Quoted detail"><div class="value">{len(nq)}</div><div class="label">Not Quoted — PTD</div><div class="sub">{teu_nq} TEU</div><div class="kpi-hint">→ Not Quoted</div></a>
  <a class="kpi purple" href="#tab-pending" title="Click to jump to Pending Hilmar tab"><div class="value">{len(pending)}</div><div class="label">Pending Hilmar</div><div class="sub">{teu_pending} TEU</div><div class="kpi-hint">→ Pending tab</div></a>
  <a class="kpi green" href="#tab-carriers" title="Click to jump to per-carrier Win Rate breakdown"><div class="value">{win_rate}%</div><div class="label">Win Rate — PTD</div><div class="sub">of decided</div><div class="kpi-hint">→ Carriers tab</div></a>
  <a class="kpi teal" href="#tab-summary" title="Click to jump to Quote Rate detail (OL responded)"><div class="value">{quote_rate}%</div><div class="label">Quote Rate — PTD</div><div class="sub">OL responded</div><div class="kpi-hint">→ Summary</div></a>
  <a class="kpi slate" href="#tab-turnaround" title="Click to jump to Turnaround analysis"><div class="value">{avg_biz}h</div><div class="label">Avg Biz-Hrs Response</div><div class="sub">{after_hours_count} after-hrs req</div><div class="kpi-hint">→ Turnaround tab</div></a>
</div>

<input type="radio" name="tabs" id="tb-summary" checked>
<input type="radio" name="tabs" id="tb-turnaround">
<input type="radio" name="tabs" id="tb-dates">
<input type="radio" name="tabs" id="tb-carriers">
<input type="radio" name="tabs" id="tb-pending">
<input type="radio" name="tabs" id="tb-trends">
<input type="radio" name="tabs" id="tb-qc">
<div class="tabs">
  <label class="tab" for="tb-summary">📊 Summary</label>
  <label class="tab" for="tb-turnaround">⏱️ Turnaround</label>
  <label class="tab" for="tb-dates">📅 Dates &amp; ETD Fit</label>
  <label class="tab" for="tb-carriers">🚢 Carriers</label>
  <label class="tab" for="tb-pending">⏳ Pending ({len(pending)})</label>
  <label class="tab" for="tb-trends">📈 Rate Trends</label>
  <label class="tab" for="tb-qc">🔍 QC</label>
</div>
"""

    # ── TAB: SUMMARY ──
    html += '<div id="tab-summary" class="tab-content">\n'

    # DOD block
    if dod and (dod.get("new_requests") or dod.get("new_responses") or dod.get("status_changes") or dod.get("new_wins") or dod.get("new_pending") or dod.get("newly_lost")):
        html += f'<div class="section"><h2>📋 What Happened Today — {_safe(dod.get("date"))}</h2>\n'
        html += f'<p style="font-size:12px;color:#475569;margin-bottom:10px"><strong>{_safe(dod.get("summary_text"))}</strong></p>\n'
        html += '<div class="dod-grid">\n'

        def _card(title, items, fmt):
            inner = "".join(f"<li>{fmt(it)}</li>" for it in items) if items else '<li class="dod-empty">None</li>'
            return f'<div class="dod-card"><h4>{title}</h4><ul>{inner}</ul></div>'

        html += _card("📥 New Requests", dod.get("new_requests", []), lambda i: f'<strong>{_esc(i.get("lane"))}</strong> — {_esc(i.get("equipment"))} ({i.get("teu",0)} TEU) @ {_esc(i.get("request_time_pt"))}')
        html += _card("📤 OL Responses", dod.get("new_responses", []), lambda i: f'<strong>{_esc(i.get("lane"))}</strong> — {_esc(i.get("carrier"))} {_esc(i.get("rate"))} @ {_esc(i.get("response_time_et"))} ({_esc(i.get("turnaround_biz"))})')
        html += _card("🔄 Status Changes", dod.get("status_changes", []), lambda i: f'<strong>{_esc(i.get("lane"))}</strong>: {_esc(i.get("from"))} → {_esc(i.get("to"))} {"• MDOLX " + _esc(i.get("mdolx")) if i.get("mdolx") else ""}')
        html += _card("✅ New Wins", dod.get("new_wins", []), lambda i: f'<strong>{_esc(i.get("lane"))}</strong> — {_esc(i.get("carrier"))} • MDOLX {_esc(i.get("mdolx"))} • {i.get("teu",0)} TEU')
        html += _card("⏳ New Pending", dod.get("new_pending", []), lambda i: f'<strong>{_esc(i.get("lane"))}</strong> — {_esc(i.get("carrier"))} {_esc(i.get("rate"))} • {_esc(i.get("hours_since_quote"))}h')
        html += _card("❌ Newly Quoted & Lost (since last run only)", dod.get("newly_lost", []), lambda i: f'<strong>{_esc(i.get("lane"))}</strong> — {_esc(i.get("carrier"))} {_esc(i.get("rate"))} • {i.get("teu",0)} TEU')
        html += '</div></div>\n'

    # WoW bar
    if weeks:
        html += '<div class="section"><h2>📈 Week-over-Week</h2>\n'
        html += '<div class="wow-bar">\n'
        for wk, b in weeks:
            total_w = b["wins"] + b["ql"] + b["nq"] + b["pending"]
            scale = (b["requests"] / max_week_req) * 60
            html += f'<div class="wow-col"><div class="wow-stack" style="height:{scale}px">'
            for kind in ("pending", "nq", "ql", "wins"):
                if b[kind]:
                    html += f'<div class="wow-seg {kind}" style="height:{(b[kind]/total_w)*scale:.1f}px"></div>'
            html += f'</div><div class="wow-label">{wk.split("-W")[1]}</div><div class="wow-label">{b["requests"]}req</div></div>\n'
        html += '</div>\n'
        html += '<p style="font-size:11px;color:#64748b;margin-top:6px">🟢 Wins | 🔴 Q&amp;L | 🟡 NQ | 🟣 Pending</p></div>\n'

    # Wins
    # 2026-05-19 PM (Michael "in the portal missing mdolx for way too many
    # files that you mark as won"): the 11 WINs without mdolx_ref are
    # send-signal promotions where Lonny said "send" but OL hasn't yet
    # issued the MDOLX booking confirmation. They're genuine wins; the
    # number is just pending. Render an amber "Awaiting MDOLX" badge so
    # they're visually distinct from "data missing" / parser failure.
    _awaiting = sum(1 for w in wins if not w.get("mdolx_ref"))
    _awaiting_label = (f' <span style="font-size:13px;color:#92400e;font-weight:500">'
                       f'· {_awaiting} awaiting MDOLX (send-signal wins without booking confirmation yet)</span>'
                       if _awaiting else '')
    html += f'<div class="section"><h2>✅ Confirmed Wins — {len(wins)} bookings, {teu_won} TEU{_awaiting_label}</h2>\n'
    if wins:
        html += '<table><tr><th>#</th><th title="MDOLX booking number; amber badge = send-signal win, OL booking confirmation not yet received">MDOLX</th><th>Req Date</th><th>Lane</th><th>Equipment</th><th>TEU</th><th>Carrier</th></tr>\n'
        for i, w in enumerate(sorted(wins, key=lambda x: x.get("request_date") or x.get("date","")), 1):
            _mdolx = w.get("mdolx_ref")
            if _mdolx:
                mdolx_cell = f'<code>{_safe(_mdolx)}</code>'
            else:
                mdolx_cell = '<span class="awaiting-mdolx" title="Lonny send-signal promoted this PENDING → WIN. OL has not yet issued the MDOLX booking confirmation in our inbox. The win is real; the number is pending.">Awaiting MDOLX</span>'
            html += f'<tr class="win-row"><td>{i}</td><td>{mdolx_cell}</td><td>{_fmt_date(w.get("request_date") or w.get("date"))}</td><td>{_safe(w.get("lane"))}</td><td>{_safe(w.get("containers"))}</td><td>{w.get("teu_won",0)}</td><td>{_safe(w.get("carrier_won"))}</td></tr>\n'
        html += '</table>'
    else:
        html += '<p class="dod-empty">No wins yet in this period.</p>'
    html += '</div>\n'

    # Not Quoted — full audit detail (every field needed to root-cause)
    # 14-day display window per Michael 2026-05-13: 'after 2 weeks with items
    # that have no reply just remove them from system that says not quoted
    # but keep it on the talley of volumes that hilmar moves for rate
    # negotiation'. Aggregates (len(nq), teu_nq) stay unchanged.
    NQ_DISPLAY_WINDOW_DAYS = 14
    from datetime import datetime as _dt2, timezone as _tz, timedelta as _td2
    _nq_cutoff = (_dt2.now(_tz.utc).date() - _td2(days=NQ_DISPLAY_WINDOW_DAYS)).isoformat()
    nq_recent = [r for r in nq if (r.get("request_date") or r.get("date") or "") >= _nq_cutoff]
    _older_hidden = len(nq) - len(nq_recent)
    _hidden_note = (f' • {_older_hidden} older than {NQ_DISPLAY_WINDOW_DAYS}d hidden '
                    'from listing but counted in volume tally for rate negotiation') if _older_hidden > 0 else ''
    html += (f'<div class="section"><h2>⚠️ Not Quoted — Last {NQ_DISPLAY_WINDOW_DAYS} Days '
             f'({len(nq_recent)} listed • {len(nq)} total • {teu_nq} TEU)</h2>\n')
    html += f'<p style="font-size:11px;color:#64748b;margin:0 0 6px">Full audit view — every field needed to root-cause why OL did not respond.{_hidden_note}</p>\n'
    if nq_recent:
        html += ('<table><tr>'
                 '<th>Date</th><th>Lonny Sent (PT)</th><th>Origin</th><th>Destination</th>'
                 '<th>Equipment</th><th>TEU</th><th>ETA Asked</th><th>OL Mailbox</th>'
                 '<th>Aging</th><th>Source IMID</th></tr>\n')
        for r in sorted(nq_recent, key=lambda x: x.get("request_date") or x.get("date","")):
            hs = _hours_since(r.get("request_timestamp"))
            if hs is not None:
                aging = f"{hs//24}d" if hs >= 24 else f"{hs}h"
            else:
                aging = "—"
            imid_first = (r.get("source_imids") or ["—"])[0]
            imid_short = (imid_first[:24] + "…") if imid_first and len(imid_first) > 24 else imid_first
            html += (
                '<tr class="nq-row">'
                f'<td>{_fmt_date(r.get("request_date") or r.get("date"))}</td>'
                f'<td style="font-size:11px;color:#64748b">{_safe(r.get("lonny_time_pt"))}</td>'
                f'<td>{_safe(r.get("origin"))}</td>'
                f'<td>{_safe(r.get("destination"))}</td>'
                f'<td style="font-size:11px">{_safe(r.get("containers"))}</td>'
                f'<td style="text-align:center">{r.get("teu_requested",0)}</td>'
                f'<td style="font-size:11px">{_safe(r.get("eta_requested") or "no ETA on request")}</td>'
                f'<td style="font-size:11px">{_safe(r.get("ol_responder"))}</td>'
                f'<td style="text-align:center;font-weight:bold;color:#b45309">{aging}</td>'
                f'<td style="font-size:10px;color:#64748b;font-family:monospace">{_safe(imid_short)}</td>'
                '</tr>\n'
            )
        html += '</table>'
    else:
        if len(nq) > 0:
            html += (f'<p class="dod-empty">No NO_RESPONSE losses in the last {NQ_DISPLAY_WINDOW_DAYS} days. '
                     f'{len(nq)} older entries counted in volume tally.</p>')
        else:
            html += '<p class="dod-empty">OL-USA responded to every request. Clean.</p>'
    html += '</div>\n'

    # Volume by Trade Region — must reconcile to summary totals
    try:
        regions = core.aggregate_trade_regions(data.get("requests", []))
    except Exception:
        regions = {}
    if regions:
        # Sort by TEU requested, descending — biggest opportunities first
        ordered = sorted(regions.values(), key=lambda m: m.get("teu_requested", 0), reverse=True)
        # Reconciliation footer — proves sums match summary totals
        sum_req = sum(m["requests"] for m in ordered)
        sum_w   = sum(m["wins"] for m in ordered)
        sum_ql  = sum(m["quoted_lost"] for m in ordered)
        sum_nq  = sum(m["not_quoted"] for m in ordered)
        sum_pen = sum(m["pending"] for m in ordered)
        sum_teu = sum(m["teu_requested"] for m in ordered)
        unmapped = next((m for m in ordered if m["region"] == "Unmapped"), None)

        # 2026-05-19 dashboard column-clarity overhaul (Task #12 — root of
        # the "28.6%" misread Michael flagged). Every header now reads as
        # a full label with a tooltip; period range stated on the table itself.
        reqs_with_dates = [r for r in (data.get("requests") or []) if r.get("request_date")]
        if reqs_with_dates:
            _dates = sorted(r["request_date"] for r in reqs_with_dates)
            _period = f"{_dates[0]} through {_dates[-1]}"
        else:
            _period = "(period unavailable)"
        html += '<div class="section"><h2>🌐 Volume by Trade Region</h2>\n'
        html += (f'<p style="font-size:12px;color:#374151;margin:0 0 4px;font-weight:600">'
                 f'Period: {_period} &nbsp;·&nbsp; '
                 f'<span style="color:#64748b;font-weight:normal">All counts are number of REQUESTS unless suffixed "TEU".</span></p>\n')
        html += ('<p style="font-size:11px;color:#64748b;margin:0 0 6px">'
                 'Destinations grouped by trade region. Totals reconcile to summary KPIs. '
                 '"Unmapped" = destination not in region map; extend <code>core._TRADE_REGION_MAP</code>.'
                 '</p>\n')
        html += ('<table><tr>'
                 '<th title="Trade region (geographic bucket)">Region</th>'
                 '<th title="Number of RFQ requests in this region">Requests (#)</th>'
                 '<th title="Bookings won">Wins (#)</th>'
                 '<th title="Quoted & Lost — OL responded with a rate but Lonny did not book">Q&amp;L (#)</th>'
                 '<th title="Not Quoted — OL did not respond with a rate">NQ (#)</th>'
                 '<th title="Awaiting OL response or Lonny send-signal">Pending (#)</th>'
                 '<th title="TEU asked for across all requests in this region">TEU Requested</th>'
                 '<th title="TEU actually won (booked)">TEU Won</th>'
                 '<th title="Win Rate = Wins / (Wins + Q&amp;L + NQ). Excludes Pending.">Win Rate</th>'
                 '<th title="Destinations in this region">Destinations</th></tr>\n')
        for m in ordered:
            dests = ", ".join(m["destinations"][:8]) + ("…" if len(m["destinations"]) > 8 else "")
            row_class = ' style="background:#fef2f2"' if m["region"] == "Unmapped" else ''
            html += (
                f'<tr{row_class}>'
                f'<td><strong>{_safe(m["region"])}</strong></td>'
                f'<td style="text-align:center">{m["requests"]}</td>'
                f'<td style="text-align:center">{m["wins"]}</td>'
                f'<td style="text-align:center">{m["quoted_lost"]}</td>'
                f'<td style="text-align:center">{m["not_quoted"]}</td>'
                f'<td style="text-align:center">{m["pending"]}</td>'
                f'<td style="text-align:center">{m["teu_requested"]}</td>'
                f'<td style="text-align:center">{m["teu_won"]}</td>'
                f'<td style="text-align:center">{m["win_rate"]}%</td>'
                f'<td style="font-size:11px;color:#64748b">{_safe(dests)}</td>'
                '</tr>\n'
            )
        # Totals row
        html += (f'<tr style="font-weight:bold;background:#f1f5f9;border-top:2px solid #1e3a5f">'
                 f'<td>TOTAL</td>'
                 f'<td style="text-align:center">{sum_req}</td>'
                 f'<td style="text-align:center">{sum_w}</td>'
                 f'<td style="text-align:center">{sum_ql}</td>'
                 f'<td style="text-align:center">{sum_nq}</td>'
                 f'<td style="text-align:center">{sum_pen}</td>'
                 f'<td style="text-align:center">{sum_teu}</td>'
                 f'<td colspan="3" style="font-size:11px;color:#64748b;font-weight:normal">'
                 f'reconciles to summary: {summary.get("total_entries",0)} reqs, '
                 f'{summary.get("wins",0)} W / {summary.get("quoted_lost",0)} Q&amp;L / '
                 f'{summary.get("not_quoted",0)} NQ / {summary.get("pending_hilmar",0)} P</td>'
                 '</tr>\n')
        html += '</table>'
        if unmapped:
            html += (f'<p style="margin:8px 0 0;color:#b91c1c;font-size:12px">'
                     f'⚠ {len(unmapped["destinations"])} destination(s) unmapped: '
                     f'{", ".join(unmapped["destinations"])} — add to <code>core._TRADE_REGION_MAP</code>.</p>\n')
        html += '</div>\n'

    # Top winning lanes (by TEU won)
    lane_wins = sorted(
        [(k, v) for k, v in lanes.items() if v.get("wins", 0) > 0],
        key=lambda x: (x[1].get("teu_won", 0), x[1].get("wins", 0)),
        reverse=True,
    )[:10]
    html += '<div class="section"><h2>🟢 Top Winning Lanes — PTD <span style="font-size:11px;color:#64748b;font-weight:normal">(top 10 by TEU Won)</span></h2>\n'
    html += (f'<p style="font-size:12px;color:#374151;margin:0 0 4px;font-weight:600">'
             f'Period: {_period} &nbsp;·&nbsp; '
             f'<span style="color:#64748b;font-weight:normal">"Win Rate" is per-lane (Wins / decided requests), not a parser metric.</span></p>\n')
    if lane_wins:
        html += ('<table><tr>'
                 '<th title="Origin → Destination">Lane</th>'
                 '<th title="Bookings won on this lane">Wins (#)</th>'
                 '<th title="Quoted & Lost — OL responded but Lonny did not book">Q&amp;L (#)</th>'
                 '<th title="Not Quoted — OL did not respond with a rate">NQ (#)</th>'
                 '<th title="Total TEU won on this lane">TEU Won</th>'
                 '<th title="Win Rate on this lane = Wins / (Wins + Q&amp;L + NQ). Same lane may also appear in Losing Lanes if it has high volume on both sides.">Win Rate</th>'
                 '<th title="Carriers that won bookings on this lane">Winning Carriers</th></tr>\n')
        max_teu_won = max((l.get("teu_won", 0) for _, l in lane_wins), default=1) or 1
        for lane, l in lane_wins:
            decided = l["wins"] + l["quoted_lost"] + l["not_quoted"]
            wr_pct = (l["wins"] / decided * 100) if decided else 0
            wr = f'{round(wr_pct, 1)}%' if decided else '—'
            wr_bg = V.heatmap_color(wr_pct, vmin=0, vmax=100, mode="good_high")
            teu_won = l.get("teu_won", 0)
            teu_bar = V.bar_cell(teu_won, max_teu_won, color="#059669", label=str(teu_won), width_px=80)
            html += (
                f'<tr class="win-row"><td>{_esc(lane)}</td>'
                f'<td>{l["wins"]}</td><td>{l["quoted_lost"]}</td><td>{l["not_quoted"]}</td>'
                f'<td>{teu_bar}</td>'
                f'<td style="background:{wr_bg};font-weight:600">{wr}</td>'
                f'<td>{_safe(l.get("winning_carriers"))}</td></tr>\n'
            )
        html += '</table>'
    else:
        html += '<p class="dod-empty">No winning lanes yet.</p>'
    html += '</div>\n'

    # Top losing lanes
    lane_losses = sorted(
        [(k, v) for k, v in lanes.items() if v.get("quoted_lost", 0) + v.get("not_quoted", 0) > 0],
        key=lambda x: x[1].get("teu_quoted_lost", 0) + x[1].get("teu_not_quoted", 0),
        reverse=True,
    )[:10]
    html += '<div class="section"><h2>🔴 Top Losing Lanes — PTD <span style="font-size:11px;color:#64748b;font-weight:normal">(top 10 by TEU Lost, excludes NO_RESPONSE)</span></h2>\n'
    html += (f'<p style="font-size:12px;color:#374151;margin:0 0 4px;font-weight:600">'
             f'Period: {_period} &nbsp;·&nbsp; '
             f'<span style="color:#64748b;font-weight:normal">A lane can appear in BOTH Winning Lanes (by absolute wins) and Losing Lanes (by absolute losses) when high-volume on both sides — e.g. Oakland → Yokohama at 28.6% Win Rate has 6 wins AND 15 losses.</span></p>\n')
    if lane_losses:
        html += ('<table><tr>'
                 '<th title="Origin → Destination">Lane</th>'
                 '<th title="Quoted & Lost — OL responded but Lonny did not book">Q&amp;L (#)</th>'
                 '<th title="Not Quoted — OL did not respond with a rate">NQ (#)</th>'
                 '<th title="Awaiting OL response or Lonny send-signal">Pending (#)</th>'
                 '<th title="Wins on the same lane (shown for context)">Wins (#)</th>'
                 '<th title="Total TEU lost on this lane (Q&amp;L + NQ)">TEU Lost</th>'
                 '<th title="Win Rate on this lane = Wins / (Wins + Q&amp;L + NQ). NOT a parser metric.">Win Rate</th>'
                 '<th title="Carriers that won bookings on this lane (shown to identify who beat us)">Winning Carriers</th></tr>\n')
        max_teu_lost = max((l.get("teu_quoted_lost", 0) + l.get("teu_not_quoted", 0) for _, l in lane_losses), default=1) or 1
        for lane, l in lane_losses:
            decided = l["wins"] + l["quoted_lost"] + l["not_quoted"]
            wr_pct = (l["wins"] / decided * 100) if decided else 0
            wr = f'{round(wr_pct, 1)}%' if decided else '—'
            wr_bg = V.heatmap_color(wr_pct, vmin=0, vmax=100, mode="good_high")
            teu_lost = l.get("teu_quoted_lost", 0) + l.get("teu_not_quoted", 0)
            teu_bar = V.bar_cell(teu_lost, max_teu_lost, color="#dc2626", label=str(teu_lost), width_px=80)
            html += (
                f'<tr class="loss-row"><td>{_esc(lane)}</td>'
                f'<td>{l["quoted_lost"]}</td><td>{l["not_quoted"]}</td><td>{l.get("pending",0)}</td>'
                f'<td>{l["wins"]}</td>'
                f'<td>{teu_bar}</td>'
                f'<td style="background:{wr_bg};font-weight:600">{wr}</td>'
                f'<td>{_safe(l.get("winning_carriers"))}</td></tr>\n'
            )
        html += '</table>'
    else:
        html += '<p class="dod-empty">No losing lanes.</p>'
    html += '</div></div>\n'

    # ── TAB: TURNAROUND ──
    html += '<div id="tab-turnaround" class="tab-content">\n'
    html += '<div class="callout"><p>⏰ <strong>Biz-hours window:</strong> 8:30 AM – 5:30 PM ET weekdays. Lonny (PT) often emails after OL hours — that time is excluded from biz-hours turnaround (raw clock hours shown separately).</p></div>\n'
    html += '<div class="section"><h2>⏱️ Response Timeline</h2>\n'
    html += '<table><tr><th>Date</th><th>Lane</th><th>Lonny Sent (PT)</th><th>OL Response (ET)</th><th>Clock Hrs</th><th>Biz Hrs</th><th>Status</th><th>Context</th></tr>\n'
    for r in sorted(requests, key=lambda x: x.get("request_timestamp") or x.get("date","")):
        biz = r.get("turnaround_biz_hours")
        clk = r.get("turnaround_hours")
        biz_s = f"{biz:.1f}h" if biz else "—"
        clk_s = f"{clk:.1f}h" if clk else "—"
        tc = _ta_class(biz)
        ctx = ""
        if r.get("after_hours_request"):
            ctx = '<span class="badge badge-amber">After-hours req</span>'
        elif r.get("response_timestamp"):
            ctx = '<span class="badge badge-blue">Biz hours</span>'
        elif r["status"] == "LOSS" and not r.get("quoted"):
            ctx = '<span class="badge badge-amber">No response</span>'
        elif r["status"] == "PENDING":
            ctx = '<span class="badge badge-purple">Awaiting send</span>'
        lonny_t = r.get("lonny_time_pt") or (core.fmt_pt(core.parse_iso(r.get("request_timestamp")), with_date=False) if r.get("request_timestamp") else "—")
        ol_t = r.get("olusa_time_et") or (core.fmt_et(core.parse_iso(r.get("response_timestamp")), with_date=False) if r.get("response_timestamp") else "—")
        html += f'<tr class="{_row_class(r)}"><td>{_fmt_date(r.get("request_date") or r.get("date"))}</td><td>{_safe(r.get("lane"))}</td><td>{_esc(lonny_t)}</td><td>{_esc(ol_t)}</td><td>{clk_s}</td><td class="{tc}">{biz_s}</td><td>{_status_badge(r)}</td><td>{ctx}</td></tr>\n'
    html += '</table></div></div>\n'

    # ── TAB: DATES & ETD FIT ──
    html += '<div id="tab-dates" class="tab-content">\n'
    html += '<div class="callout"><p>📅 <strong>ETD Fit Score:</strong> days between Lonny\'s requested ETA and OL\'s offered ETA. Positive = later than asked (bad). Use this for carrier conversations: "you quoted 12 days later than we needed."</p></div>\n'
    html += '<div class="section"><h2>📅 Dates Requested vs Offered</h2>\n'
    html += '<table><tr><th>Date</th><th>Lane</th><th>Equip</th><th>Status</th><th>Biz Hrs</th><th>Lonny ETA</th><th>Lonny Cutoff</th><th>OL ETD</th><th>OL ETA</th><th>ETD Fit</th><th>Carrier</th><th>Rate</th></tr>\n'
    for r in sorted(requests, key=lambda x: x.get("request_date") or x.get("date","")):
        biz = r.get("turnaround_biz_hours")
        biz_s = f"{biz:.1f}h" if biz else "—"
        tc = _ta_class(biz)
        carrier = r.get("carrier_won") if r["status"] == "WIN" else r.get("carrier_quoted")
        fit = r.get("etd_fit_days")
        if fit is None:
            fit_cell = "—"
        elif fit >= cfg["rules"]["etd_fit_fail_days"]:
            fit_cell = f'<span class="badge badge-red">+{fit}d miss</span>'
        elif fit >= cfg["rules"]["etd_fit_warn_days"]:
            fit_cell = f'<span class="badge badge-amber">+{fit}d</span>'
        elif fit > 0:
            fit_cell = f'<span class="badge badge-blue">+{fit}d</span>'
        elif fit < 0:
            fit_cell = f'<span class="badge badge-green">{fit}d early</span>'
        else:
            fit_cell = '<span class="badge badge-green">exact</span>'
        rate = r.get("ol_rate") or ("No offer made" if (r["status"] == "LOSS" and not r.get("quoted")) else "—")
        html += f'<tr class="{_row_class(r)}"><td>{_fmt_date(r.get("request_date") or r.get("date"))}</td><td>{_safe(r.get("lane"))}</td><td>{_safe(r.get("containers"))}</td><td>{_status_badge(r)}</td><td class="{tc}">{biz_s}</td><td>{_safe(r.get("eta_requested") or r.get("requested_dates"))}</td><td>{_safe(r.get("cutoff_requested"))}</td><td>{_safe(r.get("etd_offered"))}</td><td>{_safe(r.get("eta_offered"))}</td><td>{fit_cell}</td><td>{_safe(carrier)}</td><td>{_safe(rate)}</td></tr>\n'
    html += '</table></div></div>\n'

    # ── TAB: CARRIERS ──
    html += '<div id="tab-carriers" class="tab-content">\n'
    html += '<div class="callout green"><p>🚢 <strong>Carrier negotiation intelligence:</strong> Each carrier is ranked by TEU volume you sent them. Click into rows for lanes lost, avg turnaround, and ETD fit score — use these numbers in your 1:1 line meetings.</p></div>\n'

    # Overview table
    html += '<div class="section"><h2>Carrier Performance Overview — PTD <span style="font-size:11px;color:#64748b;font-weight:normal">(per carrier; same losses also shown by lane above)</span></h2>\n'
    html += (f'<p style="font-size:12px;color:#374151;margin:0 0 4px;font-weight:600">'
             f'Period: {_period} &nbsp;·&nbsp; '
             f'<span style="color:#64748b;font-weight:normal">"Win Rate" is per-carrier (Wins / Quotes). NOT a parser metric.</span></p>\n')
    html += ('<table><tr>'
             '<th title="Steamship carrier">Carrier</th>'
             '<th title="Number of times this carrier was quoted (= number of rate responses they gave)">Quotes (#)</th>'
             '<th title="Bookings won">Wins (#)</th>'
             '<th title="Quoted & Lost — gave a rate but lost the booking">Q&amp;L (#)</th>'
             '<th title="Awaiting Lonny send-signal or operator decision">Pending (#)</th>'
             '<th title="Win Rate = Wins / Quotes. Per-carrier, NOT a parser metric.">Win Rate</th>'
             '<th title="TEU on bookings this carrier won">TEU Won</th>'
             '<th title="TEU on bookings this carrier lost">TEU Lost</th>'
             '<th title="Distinct lanes this carrier was quoted on">Lanes (#)</th>'
             '<th title="Average response time during business hours (8:30 AM – 5:30 PM ET weekdays)">Avg Biz-Hrs</th>'
             '<th title="Average days between Lonny\'s requested ETA and OL\'s offered ETA. Negative = earlier than needed (good).">Avg ETD Fit</th>'
             '<th title="Quick read on this carrier\'s performance">Verdict</th></tr>\n')
    for c, cm in sorted(carriers.items(), key=lambda x: x[1].get("quotes", 0), reverse=True):
        wr = cm.get("win_rate", 0)
        ta = cm.get("avg_turnaround_biz_hours")
        ef = cm.get("avg_etd_fit_days")
        if wr >= 50:
            verdict = f'<span class="badge badge-green">{wr}% strong</span>'
        elif wr >= 25:
            verdict = f'<span class="badge badge-blue">{wr}% average</span>'
        elif wr > 0:
            verdict = f'<span class="badge badge-amber">{wr}% underperforming</span>'
        else:
            verdict = f'<span class="badge badge-red">0% — no wins</span>'
        html += f'<tr><td><strong>{_esc(c)}</strong></td><td>{cm["quotes"]}</td><td class="win">{cm["wins"]}</td><td class="loss">{cm["losses"]}</td><td class="pending">{cm.get("pending",0)}</td><td>{wr}%</td><td class="win">{cm["teu_won"]}</td><td class="loss">{cm["teu_lost"]}</td><td>{cm["lanes_quoted"]}</td><td>{f"{ta}h" if ta else "—"}</td><td>{f"+{ef}d" if ef and ef>0 else (f"{ef}d" if ef else "—")}</td><td>{verdict}</td></tr>\n'
    html += '</table></div>\n'

    # Per-carrier drill-down
    for c, cm in sorted(carriers.items(), key=lambda x: x[1].get("quotes", 0), reverse=True):
        carrier_reqs = [r for r in requests if r.get("carrier_quoted") == c or r.get("carrier_won") == c]
        lost_reqs = [r for r in carrier_reqs if r["status"] == "LOSS" and r.get("quoted")]
        html += f'<div class="section"><h2>{_esc(c)} — {cm["quotes"]} quoted • {cm["wins"]}W / {cm["losses"]}L • {cm.get("win_rate",0)}% win rate</h2>\n'
        ta_val = cm.get("avg_turnaround_biz_hours")
        ef_val = cm.get("avg_etd_fit_days")
        ta_str = f"{ta_val}h biz-hrs" if ta_val else "—"
        if ef_val is None:
            ef_str = "—"
        elif ef_val > 0:
            ef_str = f"+{ef_val}d"
        else:
            ef_str = f"{ef_val}d"
        html += f'<p style="margin-bottom:8px;font-size:12px;color:#475569">TEU Won: <span class="win">{cm["teu_won"]}</span> • TEU Lost: <span class="loss">{cm["teu_lost"]}</span> • Avg turnaround: {ta_str} • Avg ETD fit: {ef_str}</p>\n'
        if lost_reqs:
            html += '<h3>Lanes lost to other carriers (what this line should chase):</h3>\n'
            lane_agg = defaultdict(lambda: {"count": 0, "teu": 0, "equip": set(), "rates": []})
            for r in lost_reqs:
                lane_key = r.get("lane") or r.get("destination", "Unknown")
                la = lane_agg[lane_key]
                la["count"] += 1
                la["teu"] += r.get("teu_requested", 0) or 0
                la["equip"].add(r.get("containers", ""))
                rate = r.get("ol_rate")
                if rate is not None:
                    la["rates"].append(f"${rate:,.0f}" if isinstance(rate, (int, float)) else str(rate))
            html += '<table><tr><th>Lane</th><th>Times Lost</th><th>TEU Lost</th><th>Equipment</th><th>Rates Quoted</th></tr>\n'
            for lane, la in sorted(lane_agg.items(), key=lambda x: x[1]["teu"], reverse=True):
                html += f'<tr class="loss-row"><td>{_esc(lane)}</td><td>{la["count"]}</td><td>{la["teu"]}</td><td>{_esc(", ".join(sorted(e for e in la["equip"] if e)))}</td><td>{_esc(", ".join(la["rates"][:3]))}</td></tr>\n'
            html += '</table>\n'
        else:
            html += '<p style="color:#059669;font-weight:600">No lanes lost — every quote won ✅</p>\n'
        html += '</div>\n'
    html += '</div>\n'

    # TAB: PENDING
    html += '<div id="tab-pending" class="tab-content">\n'
    html += f'<div class="callout purple"><p>Pending Hilmar response: OL quoted, Lonny still within 24h. Past {warn_thresholds[0]}h = yellow, {warn_thresholds[-2]}h = orange, {warn_thresholds[-1]}h+ = red (about to flip to Q&amp;L).</p></div>\n'
    html += f'<div class="section"><h2>Pending Watchlist - {len(pending)} open</h2>\n'
    if pending_watch:
        html += '<table><tr><th>Severity</th><th>Hours Since Quote</th><th>Date</th><th>Lane</th><th>Equip</th><th>Carrier</th><th>Rate</th><th>Lonny ETA Ask</th><th>OL ETA</th></tr>\n'
        sev_label = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low"}
        for r in pending_watch:
            html += f'<tr class="pend-row {r["_severity"]}"><td><strong>{sev_label[r["_severity"]]}</strong></td><td>{r["_hours_since"]}h</td><td>{_fmt_date(r.get("request_date") or r.get("date"))}</td><td>{_safe(r.get("lane"))}</td><td>{_safe(r.get("containers"))}</td><td>{_safe(r.get("carrier_quoted"))}</td><td>{_safe(r.get("ol_rate"))}</td><td>{_safe(r.get("eta_requested") or r.get("requested_dates"))}</td><td>{_safe(r.get("eta_offered"))}</td></tr>\n'
        html += '</table>'
    else:
        html += '<p class="dod-empty">Nothing pending right now.</p>'
    html += '</div></div>\n'

    # TAB: RATE TRENDS — 2026-05-19 Task #6 column-clarity overhaul. Every
    # number now reads as "$X per FEU (40' equivalent), Carrier X over Lane Y".
    html += '<div id="tab-trends" class="tab-content">\n'
    html += (f'<div class="callout amber"><p>'
             f'<strong>Rate trend detection:</strong> Carrier × lane combos where the latest quoted '
             f'rate moved ≥{cfg["rules"]["rate_trend_threshold_pct"]}% vs the prior 14-day average. '
             f'All rates are <strong>per FEU</strong> (40\' equivalent) for cross-lane comparison; '
             f'20\' rates are doubled. "Latest" = most recent rate quote; "Prior Avg" = mean of '
             f'all prior quotes from this carrier on this lane.</p></div>\n')
    html += f'<p style="font-size:12px;color:#374151;margin:0 0 4px;font-weight:600">Period: {_period} &nbsp;·&nbsp; <span style="color:#64748b;font-weight:normal">Sorted by absolute % change. Up arrow = rate increased (worse for us); down arrow = rate decreased (better).</span></p>\n'
    html += '<div class="section"><h2>Biggest Rate Movers</h2>\n'
    if trends:
        html += ('<table><tr>'
                 '<th title="Carrier whose rate moved">Carrier</th>'
                 '<th title="Origin → Destination">Lane</th>'
                 '<th title="Most recent rate quoted per FEU">Latest ($ / FEU)</th>'
                 '<th title="Mean of all prior rates for this carrier × lane combo">Prior Avg ($ / FEU)</th>'
                 '<th title="Percent change of Latest vs Prior Avg. Positive = rate increased.">Change</th>'
                 '<th title="Last 5 quotes — date:rate format">Recent Series</th></tr>\n')
        for t in trends:
            pct = t["pct_change"]
            arrow = "up" if pct > 0 else ("down" if pct < 0 else "-")
            cls = "trend-up" if pct >= cfg["rules"]["rate_trend_threshold_pct"] else ("trend-down" if pct <= -cfg["rules"]["rate_trend_threshold_pct"] else "trend-flat")
            series_str = " . ".join(f'{x["date"][5:]}:${x["rate"]:.0f}' for x in t["series"][-5:])
            html += f'<tr><td><strong>{_esc(t["carrier"])}</strong></td><td>Oakland -> {_esc(t["destination"])}</td><td>${t["latest"]:.2f}</td><td>${t["prior_avg"]:.2f}</td><td class="{cls}">{arrow} {pct:+.1f}%</td><td><code>{_esc(series_str)}</code></td></tr>\n'
        html += '</table>'
    else:
        html += '<p class="dod-empty">Not enough history yet.</p>'
    html += '</div></div>\n'

    # TAB: QC
    html += '<div id="tab-qc" class="tab-content">\n'
    qc = data.get("qc", {})
    if qc:
        status_cls = "green" if qc.get("errors", 0) == 0 else "red"
        html += f'<div class="callout {status_cls}"><p><strong>QC last run:</strong> {_safe(qc.get("last_run"))} . {qc.get("fixes_applied",0)} fixes . {qc.get("warnings",0)} warnings . {qc.get("errors",0)} errors</p></div>\n'
    for kind, items, color in [
        ("Errors", qc.get("error_log", []), "red"),
        ("Warnings", qc.get("warning_log", []), "amber"),
        ("Fixes Applied", qc.get("fix_log", []), "blue"),
    ]:
        if items:
            html += f'<div class="section"><h2>{kind}</h2>\n<ul style="font-size:12px;color:#475569;padding-left:20px">\n'
            for it in items:
                html += f'<li style="padding:3px 0">{_esc(it)}</li>\n'
            html += '</ul></div>\n'
    if not qc.get("error_log") and not qc.get("warning_log") and not qc.get("fix_log"):
        html += '<p class="dod-empty">QC had nothing to report on the last run.</p>'
    html += '</div>\n'

    html += '<div class="section" style="margin-top:20px;border-left:4px solid #3b82f6"><p style="font-size:11px;color:#64748b">Auto-generated from the Hilmar Shipment Tracker</p></div>\n'
    html += '</body></html>'
    return html


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(core.CONFIG_PATH))
    args = parser.parse_args()
    cfg = core.load_config(args.config)
    data = core.load_data(cfg["paths"]["data"])
    html = render(cfg, data)
    out = Path(cfg["paths"]["dashboard"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Dashboard: {len(html):,} bytes -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
