"""
gen_weekly_summary.py — Monday-morning executive summary for the PREVIOUS week.

Per Michael 2026-05-13 Tier 2.4; moved to Monday 5 AM ET / previous-week
(2026-07-16). Emailed to the staff distribution by weekly.yml. Designed for
Slack-sharing, exec-briefing distribution, or print.

Sections:
  - Week at a glance (Mon-Fri totals + WoW delta)
  - Top 3 winning lanes by TEU
  - Top 3 losing lanes (Q&L) — negotiation candidates
  - Carrier of the week (most wins, then TEU won; a win always qualifies so the
    winner can't be benched by the min-quote floor — relabeled "Most Active
    Carrier" on a no-win week so a 0-win carrier is never crowned)
  - 4-week trend sparklines (volume, win rate, quote rate)
  - This week's red flags from QC + improvements report

Output:
  reports/weekly-summary-<YYYY-MM-DD>.pdf
  reports/weekly-summary.html       (latest)

Fired by .github/workflows/weekly.yml Monday ~5 AM ET (or any day via CLI
  --force). Covers + labels the PREVIOUS (just-completed) Mon-Fri week.
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
    """The America/New_York calendar day this weekly fire belongs to.

    2026-07-16: the weekly runs MONDAY ~5 AM ET (for the previous week). Unlike
    the evening daily fire, there is NO wee-hours rollback — 5 AM Monday IS
    Monday. (The old rule subtracted a day before 6 AM ET to attribute a
    very-late evening fire to the prior day; a legitimate 5 AM Monday fire must
    not roll back to Sunday.) Derives from ONE aware instant → ET date, so the
    runner's UTC/local date never leaks in."""
    return (now or datetime.now(timezone.utc)).astimezone(core.ET).date()


def should_generate(now=None, force=False):
    """Monday-only gate, evaluated on the ET fire day (see _fire_day_et). The
    weekly exec summary runs Monday 5 AM ET for the PREVIOUS (just-completed)
    week. ``--force`` overrides. Extracted so tests can pin cases without
    touching the wall clock."""
    return bool(force) or _fire_day_et(now).weekday() == 0  # 0 = Monday


def _filter_rows(rows, start_date, end_date):
    """Rows with request_date in [start, end] inclusive."""
    out = []
    s, e = start_date.isoformat(), end_date.isoformat()
    for r in rows:
        d = r.get("request_date") or r.get("date") or ""
        if s <= d <= e:
            out.append(r)
    return out


def _filter_wins(rows, start_date, end_date):
    """WIN rows whose win EVENT landed in [start, end] — regardless of when
    the RFQ came in.

    The counterpart to `_filter_rows`, which buckets by `request_date`. Wins
    have to be event-dated or the weekly contradicts the daily: Michael
    directed on 2026-07-21 that a win belongs to the day it was booked, and
    `gen_email` was changed to match, but the weekly still filtered wins by
    request_date. An RFQ received Friday and booked the following Monday was
    a win in Monday's daily email AND a win in the PREVIOUS week's summary —
    the same booking credited to two different weeks in two reports Michael
    reads side by side. `core.win_event_date` is the shared definition both
    now call (audit finding #19).
    """
    s, e = start_date.isoformat(), end_date.isoformat()
    out = []
    for r in rows:
        d = core.win_event_date(r)
        if d and s <= d <= e:
            out.append(r)
    return out


def analyze_week(rows, win_rows=None):
    """Compute headline metrics for a week.

    `rows` is the week's INTAKE — rows whose request_date falls in the week —
    and drives total / Q&L / NQ / pending, because those describe what came
    in and where it currently stands. `win_rows` is the separately-filtered
    set of wins whose EVENT landed in the week (see `_filter_wins`); when it
    is None the rows are treated as their own win set, which is what a caller
    passing one pre-filtered list means.

    win_rate mixes the two on purpose: `wins / (wins + ql)`, event-dated
    numerator over an intake-dated Q&L. That is the identical formula and the
    identical mix the daily email's KPI block uses, and matching it is the
    whole point — a second, "cleaner" rule here would just recreate the
    disagreement this fix removes. Since every win in `win_rows` is also in
    the denominator, the rate cannot exceed 100%.
    """
    win_rows = rows if win_rows is None else win_rows
    total = len(rows)
    wins = sum(1 for r in win_rows if core.is_win(r))
    teu_won = sum(int(r.get("teu_won") or r.get("teu_requested") or 0)
                  for r in win_rows if core.is_win(r))
    ql = sum(1 for r in rows if core.is_quoted_and_lost(r))
    teu_ql = sum(int(r.get("teu_requested") or 0)
                 for r in rows if core.is_quoted_and_lost(r))
    nq = sum(1 for r in rows if core.is_not_quoted(r))
    pending = sum(1 for r in rows if core.is_pending(r))
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
        if not core.is_quoted_and_lost(r):
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


def carrier_of_week(rows, win_rows=None):
    """Quotes come from the week's INTAKE, wins from the week's win EVENTS.

    A carrier that booked a Friday RFQ on Monday won it in Monday's week —
    crowning it for the week the RFQ arrived would credit the trophy to a
    week the daily email says it won nothing in. `win_rows=None` means the
    caller passed one pre-filtered list that is both.
    """
    win_rows = rows if win_rows is None else win_rows
    # Identity, not equality: rows are plain dicts (unhashable, and two
    # distinct RFQs can compare equal), so membership is by object.
    intake_ids = {id(r) for r in rows}
    win_ids = {id(r) for r in win_rows if core.is_win(r)}
    considered = {id(r): r for r in list(rows) + list(win_rows)}

    by_c = defaultdict(lambda: {"quotes": 0, "wins": 0, "teu_won": 0})
    for r in considered.values():
        won = id(r) in win_ids
        quoted_here = id(r) in intake_ids and bool(r.get("quoted"))
        # Attribute a WIN to the carrier that actually won it (carrier_won) and a
        # quote to the carrier quoted; fall back so a row carrying only one of the
        # two fields still counts.
        c = (r.get("carrier_won") if won else r.get("carrier_quoted")) \
            or r.get("carrier_quoted") or r.get("carrier_won")
        if not c:
            continue
        if quoted_here or won:
            by_c[c]["quotes"] += 1
        if won:
            by_c[c]["wins"] += 1
            by_c[c]["teu_won"] += int(r.get("teu_won") or r.get("teu_requested") or 0)
    if not by_c:
        return None
    # "Carrier of the Week" = who won the most business this week. A carrier that
    # WON any deal ALWAYS qualifies (a win is the strongest signal), so the
    # min-sample floor (>=2 quotes) can never bench the actual winner and hand the
    # trophy to a 0-win carrier. That floor-benches-the-winner bug is exactly why
    # CMA CGM (6 quotes, 0 wins) was crowned for the week of Jul 13-17 while a
    # different carrier won the week's one deal on a single quote. Only when
    # NOBODY won all week do we fall back to the most-active quoter (>=2 quotes);
    # the caller relabels that case so a 0-win carrier is never called "the week's
    # winner."
    candidates = [(c, s) for c, s in by_c.items() if s["wins"] >= 1 or s["quotes"] >= 2]
    if not candidates:
        return None
    # Rank: most wins, then most TEU won, then best win rate, then most quotes.
    candidates.sort(key=lambda x: (
        -x[1]["wins"], -x[1]["teu_won"],
        -(x[1]["wins"] / max(x[1]["quotes"], 1)), -x[1]["quotes"]))
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
        m = analyze_week(week_rows, _filter_wins(all_rows, mon, fri))
        m["week_start"] = mon.isoformat()
        m["week_end"] = fri.isoformat()
        trend.append(m)
    return trend


def _range_label(start, end):
    """Human range label: 'Jul 27–31, 2026'.

    Cross-month gives 'Jul 27–Aug 4, 2026'; cross-year gives
    'Dec 28, 2026–Jan 1, 2027'. A Mon-Fri week never leaves its month, so the
    original dropped the end month unconditionally — an explicit period can
    straddle one, and 'Jul 27–4, 2026' is not a date range. This is the
    caption directly above the numbers it describes.
    """
    d = "%#d" if sys.platform == "win32" else "%-d"
    if start.year != end.year:
        return f"{start.strftime(f'%b {d}, %Y')}–{end.strftime(f'%b {d}, %Y')}"
    if start.month != end.month:
        return f"{start.strftime(f'%b {d}')}–{end.strftime(f'%b {d}, %Y')}"
    return f"{start.strftime(f'%b {d}')}–{end.strftime(f'{d}, %Y')}"


def render_html(week, this_week, prev_week, top_win, top_loss, cow, trend,
                period_label="Previous week", compare_label=None):
    """Compact one-page HTML — easy to PDF and Slack-share.

    `period_label` names the reported span in the header; `compare_label`
    names what the ▲/▼ deltas are measured against. Both default to the
    Monday-fire wording so the scheduled run renders byte-identically to
    before. An explicit --start/--end catch-up passes its own strings,
    because "Previous week" on a 7-business-day recovery window would be a
    caption that contradicts the numbers under it.
    """
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

    wk_label = _range_label(week[0], week[1])

    _custom = period_label != "Previous week"
    _glance_title = "Period at a glance" if _custom else "Week at a glance"
    _compare_note = (
        f'<p style="margin:0 0 8px;font-size:11px;color:#94a3b8">'
        f'▲▼ vs the preceding {compare_label}</p>'
    ) if compare_label else ""
    _footer_note = (f"scripts/gen_weekly_summary.py · explicit period {wk_label}"
                    if _custom else
                    "scripts/gen_weekly_summary.py · Monday 5 AM ET · previous week")

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
        # A 0-win week has no "winner" — never crown a carrier that won nothing.
        # Relabel to "Most Active Carrier" so the box is honest either way.
        _won_any = cow["wins"] >= 1
        _cow_title = "🏆 Carrier of the Week" if _won_any else "📊 Most Active Carrier"
        _cow_line = (
            f'<b>{cow["carrier"]}</b> — {cow["wins"]} wins / {cow["quotes"]} quotes '
            f'({cow["win_rate"]}% win rate, {cow["teu_won"]} TEU)'
            if _won_any else
            f'<b>{cow["carrier"]}</b> — {cow["quotes"]} quotes, no wins this week'
        )
        cow_html = f"""
<div style="background:#fef3c7;border:2px solid #f59e0b;border-radius:8px;padding:16px;margin:16px 0">
  <h2 style="margin:0 0 6px;color:#92400e;font-size:16px">{_cow_title}</h2>
  <p style="margin:0;font-size:14px">{_cow_line}</p>
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
<p style="margin:0 0 16px;color:#64748b">{period_label}: <b>{wk_label}</b> · Generated {datetime.now(core.ET).strftime('%B %d, %Y at %I:%M %p ET')}</p>

<h2>{_glance_title}</h2>
{_compare_note}
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

<p style="margin-top:20px;font-size:11px;color:#94a3b8">Auto-generated weekly summary · {_footer_note}</p>
</div></body></html>"""


def _explicit_period(start_s, end_s):
    """Resolve --start/--end into (start, end, prev_start, prev_end).

    The comparison baseline is the SAME-LENGTH window ending the day before
    `start`, not the previous calendar week: a catch-up window is rarely
    Mon-Fri, and comparing 7 business days against 5 would print a delta that
    is an artifact of the window length rather than the business.

    Raises ValueError with an operator-readable message — main() turns that
    into exit code 2 rather than a traceback, because this runs unattended in
    a workflow where a traceback is just noise in a log nobody reads.
    """
    if bool(start_s) != bool(end_s):
        raise ValueError("--start and --end must be given together")
    try:
        start = datetime.strptime(start_s, "%Y-%m-%d").date()
        end = datetime.strptime(end_s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"--start/--end must be ISO dates (YYYY-MM-DD): {exc}") from exc
    if end < start:
        raise ValueError(f"--end {end} is before --start {start}")
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    return start, end, prev_start, prev_end


def _subject_for(mon, fri, custom):
    """The one place the weekly's email subject is built.

    weekly.yml used to derive it in shell (`date -u -d 'last monday -7 days'`),
    duplicating the anchor math this module already does. Two clocks, one
    header — and the subject is what the cross-host mailbox guard dedupes on,
    so a drift between them is a silently suppressed or a doubled send.
    """
    if custom:
        return f"Hilmar — Catch-Up Executive Summary ({_range_label(mon, fri)})"
    return f"Hilmar — Weekly Executive Summary (week of {mon.strftime('%b %-d')})"


def main(argv=None, now=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="Generate even if today isn't Monday (default: Monday-only)")
    ap.add_argument("--start", default="",
                    help="ISO date (YYYY-MM-DD): first day of an explicit reporting "
                         "period. Requires --end. Implies --force.")
    ap.add_argument("--end", default="",
                    help="ISO date (YYYY-MM-DD): last day of the period, inclusive.")
    args = ap.parse_args(argv)

    custom = bool(args.start or args.end)
    if custom:
        try:
            c_start, c_end, c_prev_start, c_prev_end = _explicit_period(args.start, args.end)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    # ONE aware instant drives both the Monday gate and the week bounds — ET,
    # never the runner's UTC/local date.
    now = now or datetime.now(timezone.utc)
    today = _fire_day_et(now)
    # An explicit period IS the request. Making the caller also pass --force
    # would mean a wrong weekday could silently drop a recovery run that was
    # asked for by date, which is the failure this option exists to fix.
    if not should_generate(now=now, force=args.force or custom):
        print(f"Fire day is {today.strftime('%A')} ET, not Monday — skipping "
              f"(use --force to override)")
        return 0

    data = json.loads(DATA.read_text(encoding="utf-8"))
    rows = data.get("requests", []) or []

    if custom:
        mon, fri = c_start, c_end
        prev_mon, prev_fri = c_prev_start, c_prev_end
        # Trend anchors on the LAST day reported, so the newest sparkline
        # column is the week the period ends in rather than a week the
        # report never mentions.
        report_anchor = fri
    else:
        # The Monday fire summarizes the PREVIOUS (just-completed) week — anchor
        # the week bounds on 7 days ago so "this week" in the report is LAST
        # Mon-Fri.
        report_anchor = today - timedelta(days=7)
        mon, fri = _week_bounds(report_anchor)
        prev_mon, prev_fri = mon - timedelta(weeks=1), fri - timedelta(weeks=1)
    this_rows = _filter_rows(rows, mon, fri)
    prev_rows = _filter_rows(rows, prev_mon, prev_fri)
    # Wins are filtered SEPARATELY, by the date the booking landed rather than
    # the date the RFQ came in — the same clock gen_email's day tiles use, so
    # a Friday RFQ booked on Monday is not a win in both weeks (finding #19).
    this_wins = _filter_wins(rows, mon, fri)
    prev_wins = _filter_wins(rows, prev_mon, prev_fri)
    this_metrics = analyze_week(this_rows, this_wins)
    prev_metrics = analyze_week(prev_rows, prev_wins)
    top_win = top_lanes_by_teu_won(this_wins)
    top_loss = top_lanes_losing(this_rows)
    cow = carrier_of_week(this_rows, this_wins)
    trend = four_week_trend(rows, report_anchor)

    _span = (fri - mon).days + 1
    html = render_html(
        (mon, fri), this_metrics, prev_metrics, top_win, top_loss, cow, trend,
        period_label="Reporting period" if custom else "Previous week",
        compare_label=(f"{_span} days ({prev_mon.isoformat()} → {prev_fri.isoformat()})"
                       if custom else None),
    )
    REPORTS.mkdir(parents=True, exist_ok=True)
    dated = REPORTS / f"weekly-summary-{fri.isoformat()}.html"
    latest = REPORTS / "weekly-summary.html"
    subject = REPORTS / "weekly-subject.txt"
    dated.write_text(html, encoding="utf-8")
    latest.write_text(html, encoding="utf-8")
    # weekly.yml sends whatever is in this file — see _subject_for.
    subject.write_text(_subject_for(mon, fri, custom), encoding="utf-8")

    print(f"✅ Weekly summary: {dated.name}")
    print(f"   Period:    {mon.isoformat()} → {fri.isoformat()} ({_span} days)")
    print(f"   Baseline:  {prev_mon.isoformat()} → {prev_fri.isoformat()}")
    print(f"   Subject:   {_subject_for(mon, fri, custom)}")
    print(f"   This week: {this_metrics['total']} req / {this_metrics['wins']} W "
          f"/ {this_metrics['teu_won']} TEU / {this_metrics['win_rate']}% win rate")
    print(f"   Prev week: {prev_metrics['total']} req / {prev_metrics['wins']} W "
          f"/ {prev_metrics['teu_won']} TEU / {prev_metrics['win_rate']}% win rate")
    if cow:
        print(f"   Carrier of week: {cow['carrier']} ({cow['win_rate']}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
