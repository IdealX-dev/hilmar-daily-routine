"""
gen_improvements_report.py — Daily systems-audit report for michael.deitchman@idealx.us.

Produces a focused, single-recipient HTML email each morning that calls out:
  - 🔴 RED FLAGS — data issues that need action today (broken, stale, drifting)
  - 🟡 OBSERVATIONS — patterns from this week worth noting
  - 💡 SUGGESTIONS — improvements I (Claude) recommend Michael consider

The audit reads:
  - tracking-data-v2.json   — current request state
  - reports/qc-result.json  — most recent QC pass
  - reports/drift-result.json — most recent drift audit
  - reports/run-log.txt     — recent fire history (last ~500 lines)
  - data-backups/           — backup cadence

Output:
  reports/improvements-report.html
  reports/improvements-subject.txt

Then the wrapper sends it to michael.deitchman@idealx.us only (NOT the full
distribution — this is Michael's private systems-audit inbox).

Created 2026-05-07 per Michael:
  "lock this in for qc and quality checks etc daily self healing and i want
   a report daily on any system improvements you think would be useful to
   michael.deitchman@idealx.us"
"""
from __future__ import annotations
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import branding as B  # noqa: E402  Hilmar logo + brand colors

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────

def _esc(s):
    if s is None:
        return "—"
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _strftime(dt, fmt):
    """Cross-platform strftime — maps %-d / %-I to %#d / %#I on Windows."""
    if sys.platform == "win32":
        fmt = fmt.replace("%-d", "%#d").replace("%-I", "%#I").replace("%-m", "%#m").replace("%-H", "%#H")
    return dt.strftime(fmt)


def _hours_since(iso_ts):
    if not iso_ts:
        return None
    try:
        dt = core.parse_iso(iso_ts)
        if not dt:
            return None
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _report_date():
    """Mirror gen_email._report_date — the previous business day in ET."""
    now_et = datetime.now(timezone.utc).astimezone(core.ET).date()
    wd = now_et.weekday()
    if wd == 0:    delta = 3
    elif wd == 5:  delta = 1
    elif wd == 6:  delta = 2
    else:          delta = 1
    return now_et - timedelta(days=delta)


# ───────────────────────────────────────────────────────────────────────
# Audit collectors
# ───────────────────────────────────────────────────────────────────────

def collect_red_flags(data, qc, drift):
    """Hard issues that need attention today — actionable, not opinionated."""
    flags = []
    requests = data.get("requests", []) or []

    # 1. WINs missing carrier_won
    bad_wins = [r for r in requests if r.get("status") == "WIN" and not r.get("carrier_won")]
    for r in bad_wins:
        flags.append({
            "level": "🔴",
            "title": f"WIN missing carrier_won: {r.get('request_id', '?')}",
            "detail": (
                f"Lane: {r.get('lane', '?')} | Date: {r.get('request_date', '?')} | "
                f"Subject signal: {('present' if r.get('chain_send_signal') else 'absent')}. "
                f"Auto-heal couldn't infer carrier — needs manual edit to tracking-data-v2.json."
            ),
        })

    # 2. Q&L missing carrier_quoted
    ql_missing = [r for r in requests
                  if r.get("status") == "LOSS" and r.get("quoted") and not r.get("carrier_quoted")]
    if len(ql_missing) > 5:
        flags.append({
            "level": "🔴",
            "title": f"{len(ql_missing)} Q&L losses missing carrier_quoted",
            "detail": (
                "Carrier-extraction parser may be drifting — coverage threshold is "
                "70% and we're below. Check stage_emails_bodies.txt for the lost rows "
                "and confirm parse_subject_carrier / parse_body_carrier still match the "
                "MBD reply formats."
            ),
        })

    # 3. Pending past 24h
    pending = [r for r in requests if r.get("status") == "PENDING"]
    overdue = []
    for r in pending:
        h = _hours_since(r.get("response_timestamp"))
        if h is not None and h > 24:
            overdue.append((r, h))
    for r, h in overdue:
        flags.append({
            "level": "🔴",
            "title": f"Pending past 24h: {r.get('request_id', '?')}",
            "detail": f"Lane {r.get('lane', '?')} | quoted {h:.1f}h ago | "
                      f"Lonny hasn't responded — escalate or chase.",
        })

    # 4. Drift FAIL
    if drift and drift.get("status") == "FAIL":
        for reason in drift.get("fail_reasons") or []:
            flags.append({
                "level": "🔴",
                "title": "Drift check FAIL",
                "detail": reason,
            })

    # 5. QC errors (status != CLEAN)
    if qc and qc.get("status") != "CLEAN":
        flags.append({
            "level": "🔴",
            "title": f"QC status: {qc.get('status', '?')}",
            "detail": (
                f"Errors: {qc.get('errors', 0)} | Warnings: {qc.get('warnings', 0)}. "
                "Review reports/qc-result.json error_details."
            ),
        })

    # 6. Stage stale > 36h on weekday
    # Bug fix 2026-05-07: was reading stale .jsonl (legacy) instead of current
    # .txt (refresh_stage's actual output). Now matches qc_selfheal.py QC-008
    # path resolution: .txt first, fallback .jsonl.
    try:
        scripts_dir = ROOT / "scripts"
        stage_path = scripts_dir / "stage_emails.txt"
        if not stage_path.exists():
            stage_path = scripts_dir / "stage_emails.jsonl"
        if stage_path.exists():
            now_et = datetime.now(core.ET)
            if now_et.weekday() < 5:  # Mon–Fri
                latest = None
                with open(stage_path, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                            rcv = d.get("received") or d.get("received_at") or d.get("sent")
                            if rcv:
                                dt = core.parse_iso(rcv)
                                if dt and (latest is None or dt > latest):
                                    latest = dt
                        except Exception:
                            pass
                if latest:
                    age = (datetime.now(timezone.utc) - latest).total_seconds() / 3600.0
                    if age > 36:
                        flags.append({
                            "level": "🔴",
                            "title": f"Stage stale {age:.1f}h on weekday",
                            "detail": (
                                "refresh_stage.py hasn't pulled new emails in over 36h. "
                                "Outlook search may be silently broken or the MSAL token "
                                "expired. Check secrets/token-cache.json mtime."
                            ),
                        })
    except Exception:
        pass

    return flags


def collect_observations(data, qc, drift):
    """Patterns worth noting — informational, not actionable."""
    obs = []
    requests = data.get("requests", []) or []
    summary = data.get("summary") or {}

    # 1. This week's quote rate
    today = datetime.now(timezone.utc).astimezone(core.ET).date()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    week_reqs = []
    for r in requests:
        d_iso = r.get("request_date")
        if d_iso:
            try:
                d = datetime.strptime(d_iso, "%Y-%m-%d").date()
                if monday <= d <= friday:
                    week_reqs.append(r)
            except Exception:
                pass
    if week_reqs:
        wk_total = len(week_reqs)
        wk_quoted = sum(1 for r in week_reqs if r.get("quoted"))
        wk_won = sum(1 for r in week_reqs if r.get("status") == "WIN")
        wk_qrate = (wk_quoted / wk_total * 100) if wk_total else 0
        wk_wrate = (wk_won / wk_quoted * 100) if wk_quoted else 0
        obs.append({
            "level": "🟡",
            "title": f"This week ({_strftime(monday, '%b %-d')}–{_strftime(friday, '%-d')}): "
                     f"{wk_total} requests, quote {wk_qrate:.0f}%, win {wk_wrate:.0f}%",
            "detail": (
                f"{wk_won} wins / {wk_quoted - wk_won} Q&L / {wk_total - wk_quoted} not-quoted. "
                f"Compare to PTD quote rate {summary.get('quote_rate', 0):.0f}% / "
                f"win rate {summary.get('win_rate', 0):.0f}%."
            ),
        })

    # 2. Carriers cooling — quoted last 14 days
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    recent_carriers = Counter()
    all_carriers = Counter()
    for r in requests:
        c = r.get("carrier_quoted") or r.get("carrier_won")
        if c:
            all_carriers[c] += 1
            d = core.parse_iso(r.get("request_timestamp") or r.get("response_timestamp"))
            if d and d >= cutoff:
                recent_carriers[c] += 1
    cold = [c for c in all_carriers if all_carriers[c] >= 5 and recent_carriers[c] == 0]
    if cold:
        obs.append({
            "level": "🟡",
            "title": f"{len(cold)} carrier(s) silent in last 14d",
            "detail": (
                f"Quoted ≥5x historically but 0 quotes recently: {', '.join(sorted(cold)[:5])}"
                + (f" + {len(cold) - 5} more" if len(cold) > 5 else "")
                + ". Relationship may be cooling — consider proactive outreach."
            ),
        })

    # 3. Top loss-leader lanes (Q&L > 50%)
    lane_stats = defaultdict(lambda: {"total": 0, "lost": 0, "teu_lost": 0})
    for r in requests:
        lane = r.get("lane") or f"{r.get('origin', '?')}→{r.get('destination', '?')}"
        lane_stats[lane]["total"] += 1
        if r.get("status") == "LOSS" and r.get("quoted"):
            lane_stats[lane]["lost"] += 1
            lane_stats[lane]["teu_lost"] += int(r.get("teu_requested") or 0)
    losers = [
        (lane, s) for lane, s in lane_stats.items()
        if s["total"] >= 3 and (s["lost"] / s["total"]) > 0.5
    ]
    losers.sort(key=lambda x: -x[1]["teu_lost"])
    if losers:
        top = losers[:3]
        obs.append({
            "level": "🟡",
            "title": f"{len(losers)} lane(s) losing >50% of quotes",
            "detail": "; ".join(
                f"{lane} ({s['lost']}/{s['total']} lost, {s['teu_lost']} TEU)" for lane, s in top
            ) + (f"; +{len(losers) - 3} more" if len(losers) > 3 else ""),
        })

    # 4. Unmapped destinations
    unmapped = (qc or {}).get("trade_region_reconciliation", {}).get("unmapped_destinations", [])
    if unmapped:
        obs.append({
            "level": "🟡",
            "title": f"{len(unmapped)} unmapped destination(s) in trade-region table",
            "detail": (
                f"{', '.join(unmapped[:8])}"
                + (f" + {len(unmapped) - 8} more" if len(unmapped) > 8 else "")
                + ". Adding these to core.TRADE_REGIONS lets them roll up cleanly."
            ),
        })

    # 5. Preserved-from-prior count
    preserved = [r for r in requests if r.get("preserved_from_prior")]
    if preserved:
        obs.append({
            "level": "🟡",
            "title": f"{len(preserved)} WIN(s) preserved by additive merge",
            "detail": (
                "These were carried forward from prior tracking-data-v2 because the "
                "fresh refresh_stage didn't reproduce them. Small steady set is fine "
                "(off-channel bookings); growing means refresh_stage's Outlook search "
                "is missing legitimate emails."
            ),
        })

    return obs


def collect_suggestions(data, qc, drift):
    """Forward-looking improvements I'd suggest to the system."""
    sugg = []
    requests = data.get("requests", []) or []
    summary = data.get("summary") or {}

    # 1. Q&L coverage suggestion
    cov_pct = (qc or {}).get("carrier_coverage", {}).get("ql_coverage_pct", 100)
    if cov_pct < 70:
        sugg.append({
            "level": "💡",
            "title": "Carrier-extraction parser is drifting",
            "detail": (
                f"Q&L carrier coverage is {cov_pct}% — well below 70% target. The parser "
                "in body_parser.py / patch_carriers.py was tuned to MBD reply formats from "
                "earlier this year. Consider sampling 10 missing-carrier rows in "
                "stage_emails_bodies.txt and adding new regex patterns. Could also extract "
                "carrier from quoted PDF attachments via OCR."
            ),
        })

    # 2. Cloud PC fire reliability suggestion
    log_path = REPORTS / "run-log.txt"
    if log_path.exists():
        try:
            tail = log_path.read_text(encoding="utf-8", errors="ignore")[-30000:]
            today_iso = datetime.now(core.ET).date().strftime("%m/%d/%Y")
            ten_am = re.search(rf"{today_iso} 10:00:\d+\.\d+", tail)
            today_send = "Sent. request-id=" in tail and tail.find("Sent. request-id=") > tail.find(today_iso)
            if ten_am and not today_send:
                sugg.append({
                    "level": "💡",
                    "title": "Cloud PC scheduled task fired but didn't send today",
                    "detail": (
                        "10 AM ET fire entered the wrapper but no 'Sent. request-id=' line "
                        "follows it. Likely the idempotency flag triggered (manual MBD-TRAVEL "
                        "fire ran first), OR the pipeline failed before reaching the send. "
                        "Either case is fine for today, but if this happens 2 days running "
                        "without a manual fire that's a Cloud PC reliability issue."
                    ),
                })
        except Exception:
            pass

    # 3. After-hours volume suggestion
    after_hours = [r for r in requests if r.get("after_hours_request")]
    if len(after_hours) > 10:
        sugg.append({
            "level": "💡",
            "title": f"{len(after_hours)} after-hours request(s) accumulated",
            "detail": (
                "Lonny is sending RFQs outside 8:30 AM–5:30 PM ET. Either Lonny is a night "
                "owl (PT business close = 8 PM ET, that's expected), or these are forwarded "
                "from his ops team after he's offline. If turnaround on these is consistently "
                "longer, consider a separate 'overnight RFQ' SLA to keep negotiation depth honest."
            ),
        })

    # 4. Backup retention suggestion
    backups_dir = ROOT / "data-backups"
    if backups_dir.exists():
        bk_files = list(backups_dir.glob("tracking-data-v2*.json"))
        if len(bk_files) > 50:
            sugg.append({
                "level": "💡",
                "title": f"data-backups has {len(bk_files)} snapshots",
                "detail": (
                    "Retention is 14 per config.json. The fact that we have many more "
                    "suggests the prune step in backup.py isn't catching the alternate "
                    "naming format (tracking-data-v2_T...Z.json from one path vs "
                    "tracking-data-v2.YYYY-MM-DD-HHMM.json from another). Worth a one-time "
                    "cleanup + parser fix."
                ),
            })

    # 5. Trade region coverage suggestion
    unmapped_count = len((qc or {}).get("trade_region_reconciliation", {}).get("unmapped_destinations", []))
    if unmapped_count >= 5:
        sugg.append({
            "level": "💡",
            "title": "Trade-region map needs N more destinations",
            "detail": (
                f"{unmapped_count} destinations sit in 'Unmapped' bucket. These are real "
                "destinations Lonny is requesting — adding them to core.TRADE_REGIONS lets "
                "the regional rollup include them properly and gives more signal in the dashboard."
            ),
        })

    # 6. QC self-heal trend
    fix_count = (qc or {}).get("fixes", 0)
    if fix_count >= 3:
        sugg.append({
            "level": "💡",
            "title": f"QC self-heal applied {fix_count} fixes today",
            "detail": (
                "Self-heal is doing its job, but if the SAME fix keeps applying day after "
                "day that's a root-cause issue at intake, not a fix-it-on-output issue. "
                "Worth tagging which fixes are recurring — if 'teu_won defaulted to "
                "teu_requested' fires every day, the booking emails aren't carrying TEU "
                "explicitly and we should parse it from container counts."
            ),
        })

    return sugg


# ───────────────────────────────────────────────────────────────────────
# HTML rendering
# ───────────────────────────────────────────────────────────────────────

EMAIL_FONT = "'Inter','Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif"


def _section_html(title, color, items, empty_msg):
    if not items:
        rows = f"<p style='margin:6px 0;color:#94a3b8;font-size:13px;font-style:italic'>{empty_msg}</p>"
    else:
        rows = ""
        for it in items:
            rows += f"""
<div style="margin:10px 0;padding:12px;background:#f8fafc;border-left:3px solid {color};border-radius:4px">
  <div style="font-weight:600;color:#0f172a;font-size:14px">{_esc(it.get('level', ''))} {_esc(it.get('title', ''))}</div>
  <div style="margin-top:4px;color:#475569;font-size:13px;line-height:1.5">{_esc(it.get('detail', ''))}</div>
</div>"""
    return f"""
<h2 style="margin:20px 0 8px;color:{color};font-size:16px;border-bottom:1px solid #e2e8f0;padding-bottom:6px">
  {_esc(title)} ({len(items)})
</h2>
{rows}
"""


def render_html(red, yellow, suggestions, report_date, qc):
    rd_label = _strftime(report_date, "%A %B %-d, %Y")
    now_et = datetime.now(timezone.utc).astimezone(core.ET)
    stamp = _strftime(now_et, "%B %-d, %Y at %-I:%M %p ET")

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:{EMAIL_FONT}">
<div style="max-width:800px;margin:0 auto;background:white;padding:24px">
  <div style="background:linear-gradient(135deg,{B.HILMAR_NAVY} 0%,{B.HILMAR_BLUE} 100%);color:white;padding:12px 20px;border-radius:6px 6px 0 0;margin:-24px -24px 0">
    {f'<div style="background:white;padding:2px 6px;border-radius:4px;display:inline-block;margin-bottom:4px">{B.logo_html_cid(height=60)}</div>' if B.has_logo() else ''}
    <h1 style="margin:0;font-size:20px">{'' if B.has_logo() else '🔍 '}Hilmar Tracker — Daily Systems Audit</h1>
    <div style="margin-top:4px;font-size:13px;opacity:0.9">Reporting on {_esc(rd_label)} • Generated {_esc(stamp)}</div>
  </div>
  <p style="margin:18px 0 0;color:#475569;font-size:13px;line-height:1.5">
    This is your private daily audit (idealx.us only — not the full distribution). It surfaces
    data-quality issues, system-health observations, and improvements I think are worth your time.
    Status today: QC {_esc((qc or {}).get('status', '?'))}
    ({(qc or {}).get('fixes', 0)} fixes / {(qc or {}).get('warnings', 0)} warnings / {(qc or {}).get('errors', 0)} errors)
    on {(qc or {}).get('counts', {}).get('total', '?')} entries.
  </p>
{_section_html("🔴 Red Flags", "#dc2626", red, "No red flags today — system is clean.")}
{_section_html("🟡 Observations", "#f59e0b", yellow, "Nothing notable this week beyond the headline KPIs.")}
{_section_html("💡 Suggestions", "#3b82f6", suggestions, "No specific code-or-process improvements I'd push for today.")}

  <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e2e8f0;color:#94a3b8;font-size:11px">
    Auto-generated by scripts/gen_improvements_report.py.
    Raw inputs: tracking-data-v2.json, reports/qc-result.json, reports/drift-result.json,
    reports/run-log.txt. To suppress a recurring suggestion, edit the collector function.
    To request a new check, ask Claude.
  </div>
{_rate_intel_section_inline()}
{_reconcile_section_inline()}
{_sentry_section_inline()}
</div>
</body></html>
"""
    return body


def _sentry_section_inline():
    """Pull the Sentry activity section. Per Michael 2026-05-17 ("use sentry
    for self check and improvements as well"). Queries Sentry's REST API
    for unresolved + new + recurring issues in the last 24h, renders an
    embedded section in the daily audit so you see real-time errors
    alongside the QC + reconcile findings — single inbox.

    Silent no-op if auth token missing — observability degradation must
    never break the audit email.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from sentry_api import SentryAPI, get_issue_summary
        api = SentryAPI()
        if not api.enabled:
            return ""
        s = get_issue_summary(api, period="24h")
    except Exception:
        return ""

    # No issues at all = compact "all clear" badge
    if s["unresolved_count"] == 0 and len(s["resolved_in_period"]) == 0:
        return """
<h2 style="margin:24px 0 8px;color:#1a3d9c;font-size:16px;border-bottom:2px solid #76b82a;padding-bottom:6px">
  🛡️ Sentry observability (last 24h)
</h2>
<div style="background:#f0fdf4;border-left:4px solid #16a34a;padding:10px 14px;margin:6px 0 12px;border-radius:4px;font-size:13px;color:#166534">
  ✅ All clear — no unresolved issues, no events captured in the last 24h.
</div>
"""

    # Build a richer section
    def _issue_row(issue, marker="🔴"):
        short_id = (issue.get("shortId") or "?")[:25]
        title = (issue.get("title") or "?")[:80]
        count = issue.get("count", "?")
        last_seen = (issue.get("lastSeen") or "")[:16].replace("T", " ")
        permalink = issue.get("permalink") or ""
        return (
            f'<tr><td style="padding:4px 8px;font-family:monospace;font-size:11px;color:#0f172a">{marker} {short_id}</td>'
            f'<td style="padding:4px 8px;font-size:12px"><a href="{permalink}" style="color:#1a3d9c">{title}</a></td>'
            f'<td style="padding:4px 8px;text-align:center;font-size:12px;color:#475569">{count}x</td>'
            f'<td style="padding:4px 8px;font-size:11px;color:#64748b">{last_seen}</td></tr>'
        )

    rows = []
    if s["new_in_period"]:
        for issue in s["new_in_period"][:5]:
            rows.append(_issue_row(issue, "🆕"))
    if s["recurring"]:
        for issue in s["recurring"][:5]:
            if issue["id"] not in {i["id"] for i in s["new_in_period"]}:
                rows.append(_issue_row(issue, "🔁"))
    if s["resolved_in_period"]:
        for issue in s["resolved_in_period"][:3]:
            rows.append(_issue_row(issue, "✅"))

    rows_html = "\n".join(rows) if rows else (
        '<tr><td colspan="4" style="padding:8px;text-align:center;color:#475569;font-size:12px">'
        f'{s["unresolved_count"]} unresolved issue(s) in 14d, none new/recurring in 24h</td></tr>'
    )

    return f"""
<h2 style="margin:24px 0 8px;color:#1a3d9c;font-size:16px;border-bottom:2px solid #76b82a;padding-bottom:6px">
  🛡️ Sentry observability (last 24h)
</h2>
<p style="margin:0 0 8px;font-size:12px;color:#64748b">
  Real-time error + performance monitoring. 🆕 = new today, 🔁 = recurring (≥3 events), ✅ = resolved today.
</p>
<div style="background:#f8fafc;border-left:4px solid #1a3d9c;padding:10px 14px;margin:6px 0 12px;border-radius:4px">
  <div style="font-size:13px;font-weight:600;color:#0f172a">
    {s["unresolved_count"]} unresolved · {len(s["new_in_period"])} new (24h) · {len(s["recurring"])} recurring · {len(s["resolved_in_period"])} resolved (24h)
  </div>
  <div style="font-size:11px;color:#475569;margin-top:2px">
    Total events 24h: {s["total_events_in_period"]} ·
    <a href="https://idealx-llc.sentry.io/issues/" style="color:#1a3d9c">View in Sentry →</a>
  </div>
</div>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px">
  <tr style="background:#0a2350;color:white">
    <th style="padding:5px;text-align:left">Issue</th>
    <th style="padding:5px;text-align:left">Title</th>
    <th style="padding:5px;text-align:center">Count</th>
    <th style="padding:5px;text-align:left">Last Seen</th>
  </tr>
  {rows_html}
</table>
"""


def _rate_intel_section_inline():


def _rate_intel_section_inline():
    """Inline the rate-intelligence section if it exists. The section is
    produced by gen_rate_intelligence.py which runs before this script
    in the pipeline. Added 2026-05-13 per Michael 'i love all of this and
    want it now' — rate-negotiation cheat sheet + cooling/regression
    alerts surface in the daily idealx.us audit."""
    section_path = REPORTS / "rate-intelligence-section.html"
    if section_path.exists():
        try:
            return section_path.read_text(encoding="utf-8")
        except Exception:
            return ""
    return ""


def _reconcile_section_inline():
    """Inline the ol-quote-tracker reconciliation section if it exists.
    Produced by reconcile_with_quote_tracker.py (runs at end of pipeline).
    Added 2026-05-17 per Michael 'you see hilmar data is also on there as a
    good check point for won bookings'. Surfaces win-count + TEU drift
    between Hilmar's local tracking and ol-quote-tracker's Turso registry
    — drift indicates one system missed or mis-classified an OL email.
    Silent no-op if section absent (APP_PASSWORD not configured)."""
    section_path = REPORTS / "reconcile-quote-tracker-section.html"
    if section_path.exists():
        try:
            return section_path.read_text(encoding="utf-8")
        except Exception:
            return ""
    return ""


def build_subject(report_date, red, yellow, suggestions):
    rd = _strftime(report_date, "%b %-d")
    counts = f"{len(red)}R/{len(yellow)}O/{len(suggestions)}S"
    return f"Hilmar Tracker — Daily Systems Audit ({rd}) — {counts}"


# ───────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────

def main():
    data_path = ROOT / "tracking-data-v2.json"
    qc_path = REPORTS / "qc-result.json"
    drift_path = REPORTS / "drift-result.json"

    data = _read_json(data_path) or {}
    qc = _read_json(qc_path) or {}
    drift = _read_json(drift_path) or {}

    red = collect_red_flags(data, qc, drift)
    yellow = collect_observations(data, qc, drift)
    suggestions = collect_suggestions(data, qc, drift)

    report_date = _report_date()

    html = render_html(red, yellow, suggestions, report_date, qc)
    subject = build_subject(report_date, red, yellow, suggestions)

    REPORTS.mkdir(parents=True, exist_ok=True)
    body_path = REPORTS / "improvements-report.html"
    subj_path = REPORTS / "improvements-subject.txt"
    body_path.write_text(html, encoding="utf-8")
    subj_path.write_text(subject, encoding="utf-8")

    print(f"✅ Improvements body:    {len(html):,} bytes -> {body_path}")
    print(f"✅ Improvements subject: {subject!r}")
    print(f"   Counts: {len(red)} red flags / {len(yellow)} observations / {len(suggestions)} suggestions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
