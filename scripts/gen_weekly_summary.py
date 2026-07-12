"""
gen_weekly_summary.py — Friday EOD executive summary PDF.

Per Michael 2026-05-13 Tier 2.4. Auto-generated each Friday afternoon and
sent to michael.deitchman@idealx.us. Designed for Slack-sharing,
exec-briefing distribution, or print.

Sections:
  - Week at a glance (Mon-Fri totals + WoW delta)
  - Top 3 winning lanes by TEU
  - Top 3 losing lanes (Q&L) — negotiation candidates
  - Carrier of the week (highest win rate)
  - 4-week trend sparklines (volume, win rate, quote rate)
  - This week's red flags from QC + improvements report

Output:
  reports/weekly-summary-<YYYY-MM-DD>.pdf
  reports/weekly-summary.html       (latest)

Fired by wrapper Step 6 on Fridays only (or any day via CLI --force).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import branding as B  # noqa: E402
import core  # noqa: E402
import viz as V  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
DATA = ROOT / "tracking-data-v2.json"


def _week_bounds(today=None):
    """Monday-Friday of THIS week (relative to today)."""
    today = today or datetime.now(core.ET).date()
    mon = today - timedelta(days=today.weekday())
    fri = mon + timedelta(days=4)
    return mon, fri


def _fire_day_et(now=None):
    """The America/New_York calendar day this fire belongs to.

    2026-07-12 fix (run 29174327034): the Friday-evening ~8:50 PM ET fire
    runs when the runner's UTC clock has ALREADY rolled into Saturday, and
    the Friday gate skipped the weekly summary. The gate must never see a
    UTC (or runner-local) date — it derives from ONE aware instant
    converted to ET. A run between midnight and 6 AM ET is the previous
    evening's very-late fire and counts as that day — the same wee-hours
    rule as core.report_business_day (2026-07-02, run #76)."""
    now_et = (now or datetime.now(timezone.utc)).astimezone(core.ET)
    day = now_et.date()
    if now_et.hour < 6:
        day -= timedelta(days=1)
    return day


def should_generate(now=None, force=False):
    """Friday-only gate, evaluated on the ET fire day (see _fire_day_et).
    ``--force`` keeps its override. Extracted so tests can pin the
    UTC-shift cases without touching the wall clock."""
    return bool(force) or _fire_day_et(now).weekday() == 4  # 4 = Friday


def _filter_rows(rows, start_date, end_date):
    """Rows with request_date in [start, end] inclusive."""
    out = []
    s, e = start_date.isoformat(), end_date.isoformat()
    for r in rows:
        d = r.get("request_date") or r.get("date") or ""
        if s <= d <= e:
            out.append(r)
    return out


def analyze_week(rows):
    """Compute headline metrics for a week."""
    total = len(rows)
    wins = sum(1 for r in rows if r["status"] == "WIN")
    teu_won = sum(int(r.get("teu_won") or r.get("teu_requested") or 0)
                  for r in rows if r["status"] == "WIN")
    ql = sum(1 for r in rows if r["status"] == "LOSS" and r.get("quoted"))
    teu_ql = sum(int(r.get("teu_requested") or 0)
                 for r in rows if r["status"] == "LOSS" and r.get("quoted"))
    nq = sum(1 for r in rows if r["status"] == "LOSS" and not r.get("quoted"))
    pending = sum(1 for r in rows if r["status"] == "PENDING")
    quoted = wins + ql
    win_rate = (wins / quoted * 100) if quoted else 0
    quote_rate = ((quoted + pending) / total * 100) if total else 0
    return {
        "total": total, "wins": wins, "teu_won": teu_won,
        "ql": ql, "teu_ql": teu_ql, "nq": nq, "pending": pending,
        "win_rate": round(win_rate, 1),
        "quote_rate": round(quote_rate, 1),
    }


def top_lanes_by_teu_won(rows, n=3):
    by_lane = defaultdict(lambda: {"wins": 0, "teu_won": 0, "carriers": set()})
    for r in rows:
        if r["status"] != "WIN":
            continue
        lane = r.get("lane") or "?"
        by_lane[lane]["wins"] += 1
        by_lane[lane]["teu_won"] += int(r.get("teu_won") or r.get("teu_requested") or 0)
        if r.get("carrier_won"):
            by_lane[lane]["carriers"].add(r["carrier_won"])
    rows_out = []
    for lane, s in by_lane.items():
        rows_out.append({"lane": lane, "wins": s["wins"], "teu_won": s["teu_won"],
                         "carriers": sorted(s["carriers"])})
    rows_out.sort(key=lambda x: -x["teu_won"])
    return rows_out[:n]


def top_lanes_losing(rows, n=3):
    by_lane = defaultdict(lambda: {"losses": 0, "teu_lost": 0, "rates": []})
    for r in rows:
        if r["status"] != "LOSS" or not r.get("quoted"):
            continue
        lane = r.get("lane") or "?"
        by_lane[lane]["losses"] += 1
        by_lane[lane]["teu_lost"] += int(r.get("teu_requested") or 0)
        if r.get("ol_rate"):
            by_lane[lane]["rates"].append(float(r["ol_rate"]))
    out = []
    for lane, s in by_lane.items():
        med = sorted(s["rates"])[len(s["rates"]) // 2] if s["rates"] else None
        out.append({"lane": lane, "losses": s["losses"], "teu_lost": s["teu_lost"],
                    "median_rate": med})
    out.sort(key=lambda x: -x["teu_lost"])
    return out[:n]


def carrier_of_week(rows):
    by_c = defaultdict(lambda: {"quotes": 0, "wins": 0, "teu_won": 0})
    for r in rows:
        c = r.get("carrier_quoted") or r.get("carrier_won")
        if not c:
            continue
        if r.get("quoted") or r["status"] == "WIN":
            by_c[c]["quotes"] += 1
        if r["status"] == "WIN":
            by_c[c]["wins"] += 1
            by_c[c]["teu_won"] += int(r.get("teu_won") or r.get("teu_requested") or 0)
    candidates = [(c, s) for c, s in by_c.items() if s["quotes"] >= 2]
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[1]["wins"] / max(x[1]["quotes"], 1), -x[1]["teu_won"]))
    name, s = candidates[0]
    return {"carrier": name, "quotes": s["quotes"], "wins": s["wins"],
            "teu_won": s["teu_won"],
            "win_rate": round(s["wins"] / max(s["quotes"], 1) * 100, 1)}


def four_week_trend(all_rows, today):
    trend = []
    for w in range(3, -1, -1):
        wk_today = today - timedelta(weeks=w)
        mon, fri = _week_bounds(wk_today)
        week_rows = _filter_rows(all_rows, mon, fri)
        m = analyze_week(week_rows)
        m["week_start"] = mon.isoformat()
        m["week_end"] = fri.isoformat()
        trend.append(m)
    return trend


def render_html(week, this_week, prev_week, top_win, top_loss, cow, trend):
    """Compact one-page HTML — easy to PDF and Slack-share."""
    def _delta(curr, prev, fmt="d"):
        if not isinstance(curr, (int, float)) or not isinstance(prev, (int, float)):
            return ""
        d = curr - prev
        if abs(d) < 0.01:
            return ""
        arrow = "▲" if d > 0 else "▼"
        color = "#16a34a" if d > 0 else "#dc2626"
        fmtspec = "+d" if fmt == "d" else "+.1f"
        return f'<span style="color:{color};font-size:12px;margin-left:6px">{arrow} {d:{fmtspec}}</span>'

    wk_label = f"{week[0].strftime('%b %-d')}–{week[1].strftime('%-d, %Y')}" if sys.platform != "win32" else f"{week[0].strftime('%b %#d')}–{week[1].strftime('%#d, %Y')}"

    win_rows = "".join(
        f'<tr><td style="padding:6px 8px">{r["lane"]}</td>'
        f'<td style="padding:6px;text-align:center">{r["wins"]}</td>'
        f'<td style="padding:6px;text-align:center;font-weight:600">{r["teu_won"]} TEU</td>'
        f'<td style="padding:6px;font-size:12px">{", ".join(r["carriers"])}</td></tr>'
        for r in top_win
    ) or '<tr><td colspan="4" style="padding:8px;color:#94a3b8;font-style:italic">No wins this week</td></tr>'

    loss_rows = "".join(
        f'<tr><td style="padding:6px 8px">{r["lane"]}</td>'
        f'<td style="padding:6px;text-align:center">{r["losses"]}</td>'
        f'<td style="padding:6px;text-align:center;color:#dc2626;font-weight:600">{r["teu_lost"]} TEU</td>'
        f'<td style="padding:6px;text-align:center">${r["median_rate"]:,.0f}</td></tr>'
        if r.get("median_rate") else
        f'<tr><td style="padding:6px 8px">{r["lane"]}</td>'
        f'<td style="padding:6px;text-align:center">{r["losses"]}</td>'
        f'<td style="padding:6px;text-align:center;color:#dc2626;font-weight:600">{r["teu_lost"]} TEU</td>'
        f'<td style="padding:6px;text-align:center">—</td></tr>'
        for r in top_loss
    ) or '<tr><td colspan="4" style="padding:8px;color:#94a3b8;font-style:italic">No quoted losses this week</td></tr>'

    # Build sparklines spanning the 4-week trend
    spark_total = V.sparkline_svg([t["total"] for t in trend], width=70, height=18, color="#3b82f6")
    spark_wins = V.sparkline_svg([t["wins"] for t in trend], width=70, height=18, color="#16a34a")
    spark_teu = V.sparkline_svg([t["teu_won"] for t in trend], width=70, height=18, color="#16a34a")
    spark_wr = V.sparkline_svg([t["win_rate"] for t in trend], width=70, height=18, color="#8b5cf6")
    spark_qr = V.sparkline_svg([t["quote_rate"] for t in trend], width=70, height=18, color="#0ea5e9")

    trend_rows = ""
    for i, t in enumerate(trend):
        wr_bg = V.heatmap_color(t["win_rate"], vmin=0, vmax=100, mode="good_high")
        is_current = (i == len(trend) - 1)
        bg = "#eff6ff" if is_current else ("#ffffff" if i % 2 == 0 else "#f8fafc")
        emphasis = "font-weight:600" if is_current else ""
        trend_rows += (
            f'<tr style="background:{bg};{emphasis}">'
            f'<td style="padding:5px 8px;font-size:12px">{t["week_start"]} → {t["week_end"]}</td>'
            f'<td style="padding:5px;text-align:center">{t["total"]}</td>'
            f'<td style="padding:5px;text-align:center">{t["wins"]}</td>'
            f'<td style="padding:5px;text-align:center">{t["teu_won"]}</td>'
            f'<td style="padding:5px;text-align:center;background:{wr_bg};font-weight:600">{t["win_rate"]}%</td>'
            f'<td style="padding:5px;text-align:center">{t["quote_rate"]}%</td>'
            f'</tr>'
        )
    # Add a sparkline summary row
    trend_rows += (
        f'<tr style="background:#1e293b;color:white">'
        f'<td style="padding:6px 8px;font-size:11px;font-weight:600">4-week trend →</td>'
        f'<td style="padding:6px;text-align:center">{spark_total}</td>'
        f'<td style="padding:6px;text-align:center">{spark_wins}</td>'
        f'<td style="padding:6px;text-align:center">{spark_teu}</td>'
        f'<td style="padding:6px;text-align:center">{spark_wr}</td>'
        f'<td style="padding:6px;text-align:center">{spark_qr}</td>'
        f'</tr>'
    )

    cow_html = ""
    if cow:
        cow_html = f"""
<div style="background:#fef3c7;border:2px solid #f59e0b;border-radius:8px;padding:16px;margin:16px 0">
  <h2 style="margin:0 0 6px;color:#92400e;font-size:16px">🏆 Carrier of the Week</h2>
  <p style="margin:0;font-size:14px"><b>{cow["carrier"]}</b> — {cow["wins"]} wins / {cow["quotes"]} quotes
  ({cow["win_rate"]}% win rate, {cow["teu_won"]} TEU)</p>
</div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hilmar Weekly Summary — {wk_label}</title>
<style>
body{{font-family:'Inter','Segoe UI',Arial,sans-serif;padding:24px;background:#f1f5f9;color:#0f172a;font-size:13px}}
.container{{max-width:900px;margin:0 auto;background:white;padding:24px;border-radius:8px}}
h1{{margin:0 0 6px;font-size:22px}}
h2{{margin:18px 0 8px;font-size:15px;color:#1e40af}}
.kpi{{display:inline-block;background:#eff6ff;border-radius:6px;padding:10px 16px;margin:4px 4px 4px 0;min-width:110px}}
.kpi .val{{font-size:20px;font-weight:700;display:block}}
.kpi .lbl{{font-size:11px;color:#64748b;margin-top:2px;display:block}}
table{{width:100%;border-collapse:collapse;margin:8px 0 16px}}
th{{background:#1e3a5f;color:white;padding:6px 8px;text-align:left;font-size:12px}}
td{{font-size:12px;border-bottom:1px solid #f1f5f9}}
</style></head><body><div class="container">
{f'<div style="margin-bottom:12px">{B.logo_html(height=42)}</div>' if B.has_logo() else ''}
<h1>{'' if B.has_logo() else '🗓 '}Hilmar Weekly Summary</h1>
<p style="margin:0 0 16px;color:#64748b">Week of <b>{wk_label}</b> · Generated {datetime.now(core.ET).strftime('%B %d, %Y at %I:%M %p ET')}</p>

<h2>Week at a glance</h2>
<div style="margin-bottom:8px">
  <div class="kpi"><span class="val">{this_week["total"]}</span><span class="lbl">Requests {_delta(this_week["total"], prev_week["total"])}</span></div>
  <div class="kpi"><span class="val">{this_week["wins"]} <span style="color:#16a34a">({this_week["teu_won"]} TEU)</span></span><span class="lbl">Wins {_delta(this_week["wins"], prev_week["wins"])}</span></div>
  <div class="kpi"><span class="val">{this_week["ql"]}</span><span class="lbl">Quoted & Lost {_delta(this_week["ql"], prev_week["ql"])}</span></div>
  <div class="kpi"><span class="val">{this_week["nq"]}</span><span class="lbl">Not Quoted {_delta(this_week["nq"], prev_week["nq"])}</span></div>
  <div class="kpi"><span class="val">{this_week["win_rate"]}%</span><span class="lbl">Win Rate {_delta(this_week["win_rate"], prev_week["win_rate"], "f")}</span></div>
  <div class="kpi"><span class="val">{this_week["quote_rate"]}%</span><span class="lbl">Quote Rate {_delta(this_week["quote_rate"], prev_week["quote_rate"], "f")}</span></div>
</div>

{cow_html}

<h2>🏆 Top 3 Winning Lanes (by TEU)</h2>
<table>
<tr><th>Lane</th><th style="text-align:center">Wins</th><th style="text-align:center">TEU Won</th><th>Carrier(s)</th></tr>
{win_rows}
</table>

<h2>📉 Top 3 Losing Lanes (negotiation candidates)</h2>
<table>
<tr><th>Lane</th><th style="text-align:center">Losses</th><th style="text-align:center">TEU Lost</th><th style="text-align:center">Median Rate</th></tr>
{loss_rows}
</table>

<h2>4-Week Trend</h2>
<table>
<tr><th>Week</th><th style="text-align:center">Requests</th><th style="text-align:center">Wins</th><th style="text-align:center">TEU Won</th><th style="text-align:center">Win %</th><th style="text-align:center">Quote %</th></tr>
{trend_rows}
</table>

<p style="margin-top:20px;font-size:11px;color:#94a3b8">Auto-generated weekly summary · scripts/gen_weekly_summary.py · Friday EOD</p>
</div></body></html>"""


def main(argv=None, now=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="Generate even if today isn't Friday (default: Friday-only)")
    args = ap.parse_args(argv)

    # ONE aware instant drives both the Friday gate and the week bounds —
    # the gate is ET (wee-hours aware), never the runner's UTC/local date
    # (2026-07-12, run 29174327034: Fri 8:50 PM ET fire = Saturday UTC).
    now = now or datetime.now(timezone.utc)
    today = _fire_day_et(now)
    if not should_generate(now=now, force=args.force):
        print(f"Fire day is {today.strftime('%A')} ET, not Friday — skipping "
              f"(use --force to override)")
        return 0

    data = json.loads(DATA.read_text(encoding="utf-8"))
    rows = data.get("requests", []) or []

    mon, fri = _week_bounds(today)
    prev_mon, prev_fri = mon - timedelta(weeks=1), fri - timedelta(weeks=1)
    this_rows = _filter_rows(rows, mon, fri)
    prev_rows = _filter_rows(rows, prev_mon, prev_fri)
    this_metrics = analyze_week(this_rows)
    prev_metrics = analyze_week(prev_rows)
    top_win = top_lanes_by_teu_won(this_rows)
    top_loss = top_lanes_losing(this_rows)
    cow = carrier_of_week(this_rows)
    trend = four_week_trend(rows, today)

    html = render_html((mon, fri), this_metrics, prev_metrics, top_win, top_loss, cow, trend)
    REPORTS.mkdir(parents=True, exist_ok=True)
    dated = REPORTS / f"weekly-summary-{fri.isoformat()}.html"
    latest = REPORTS / "weekly-summary.html"
    dated.write_text(html, encoding="utf-8")
    latest.write_text(html, encoding="utf-8")

    print(f"✅ Weekly summary: {dated.name}")
    print(f"   This week: {this_metrics['total']} req / {this_metrics['wins']} W "
          f"/ {this_metrics['teu_won']} TEU / {this_metrics['win_rate']}% win rate")
    print(f"   Prev week: {prev_metrics['total']} req / {prev_metrics['wins']} W "
          f"/ {prev_metrics['teu_won']} TEU / {prev_metrics['win_rate']}% win rate")
    if cow:
        print(f"   Carrier of week: {cow['carrier']} ({cow['win_rate']}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
