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
import branding as B  # noqa: E402  Hilmar logo + brand colors
import core  # noqa: E402

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


_QC_PREFIX_RE = re.compile(r"^\s*(QC-\d+[a-z]?)\s*:\s*", re.IGNORECASE)


def _strip_qc_prefix(msg, check_id):
    if not msg:
        return ""
    m = _QC_PREFIX_RE.match(msg)
    if m and m.group(1).upper() == check_id.upper():
        return msg[m.end():].strip()
    return msg.strip()


def _group_qc_messages(messages):
    """Group QC log messages by their QC-NNN prefix.

    Returns (grouped, ungrouped) where:
      - grouped is a list of (check_id, [msgs]) sorted by check id, preserving
        original order within each group
      - ungrouped is the messages with no QC-NNN prefix (defensive — shouldn't
        normally happen but phase_1/phase_2 hard failures don't carry one)
    """
    buckets = defaultdict(list)
    ungrouped = []
    for msg in messages or []:
        m = _QC_PREFIX_RE.match(msg or "")
        if m:
            buckets[m.group(1).upper()].append(msg)
        else:
            ungrouped.append(msg)
    return sorted(buckets.items()), ungrouped


# Max characters per excerpt line before we visually truncate. Picked so a
# pasted long stacktrace line can't blow out the audit email width in
# Outlook (which doesn't word-wrap inside <pre>).
_EXCERPT_LINE_MAX = 180
# Max per-test excerpts to render — keeps the audit email bounded when the
# 22-error collection-failure mode hits.
_MAX_EXCERPTS_RENDERED = 5


def _truncate_line(line, n=_EXCERPT_LINE_MAX):
    if not line:
        return ""
    s = str(line)
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _format_test_excerpts(errors):
    """Render a monospace <pre> block of per-test pytest excerpts.

    Each entry shows a short header (nodeid + classified error_type) and up
    to 4 truncated traceback lines. All text is ``_esc()``'d so quotes,
    unicode, ``<``/``>`` etc. can't break the audit HTML. Returns ``""``
    when there are no errors so the section just shows the headline detail.

    Outlook-safe: a plain ``<pre>`` tag with explicit ``font-family`` +
    ``white-space: pre-wrap``. No flexbox, SVG, or linear-gradient.
    """
    if not errors:
        return ""
    rendered = []
    for e in errors[:_MAX_EXCERPTS_RENDERED]:
        nodeid = _esc(_truncate_line(e.get("nodeid") or "?"))
        et = _esc(e.get("error_type") or "UnknownError")
        short = _esc(_truncate_line(e.get("short_message") or ""))
        header = f"<strong>{nodeid}</strong> — {et}"
        if short:
            header += f": {short}"
        tb = e.get("traceback_excerpt") or []
        tb_text = "\n".join(_esc(_truncate_line(ln)) for ln in tb[:4])
        block = header
        if tb_text:
            block += "\n" + tb_text
        rendered.append(block)
    extra = ""
    if len(errors) > _MAX_EXCERPTS_RENDERED:
        extra = f"\n… +{len(errors) - _MAX_EXCERPTS_RENDERED} more — see full output for the rest."
    return (
        '<pre style="margin:8px 0 0;padding:10px;background:#0f172a;color:#e2e8f0;'
        'border-radius:4px;font-family:Consolas,Menlo,monospace;font-size:11px;'
        'line-height:1.45;white-space:pre-wrap;overflow-x:auto">'
        + "\n\n".join(rendered)
        + extra
        + "</pre>"
    )


def _report_date():
    """Mirror gen_email._report_date — the business day this run reports on.
    The ~6 PM ET evening fire reports on TODAY's now-complete Pacific business
    day (Sat/Sun roll back to Friday). Single source of truth:
    core.report_business_day (window="current")."""
    return core.report_business_day()


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

    # 3. Pending past the business-stale window. Uses core.is_business_stale
    # — the SAME predicate the state machine uses to age PENDING → Q&L — so
    # the audit and the data agree. A wall-clock >24h check (the prior
    # implementation) was firing on rows the state machine had correctly
    # left in PENDING (Friday quote, still within the Friday→Tuesday EOD
    # carve-out per Michael 2026-06-04). If the state machine says PENDING,
    # the audit must not contradict it.
    pending = [r for r in requests if r.get("status") == "PENDING"]
    now = datetime.now(timezone.utc)
    overdue = []
    for r in pending:
        resp_dt = core.parse_iso(r.get("response_timestamp"))
        if resp_dt is None:
            continue
        if core.pending_hilmar_stale(resp_dt, now):
            h = (now - resp_dt).total_seconds() / 3600.0
            overdue.append((r, h))
    for r, h in overdue:
        flags.append({
            "level": "🔴",
            "title": f"Pending past stale window: {r.get('request_id', '?')}",
            "detail": f"Lane {r.get('lane', '?')} | quoted {h:.1f}h ago | "
                      f"past the {core.PENDING_WINDOW_HOURS}h biz window "
                      f"(weekend-aware) — Lonny hasn't responded.",
        })

    # 4. Drift FAIL
    if drift and drift.get("status") == "FAIL":
        for reason in drift.get("fail_reasons") or []:
            flags.append({
                "level": "🔴",
                "title": "Drift check FAIL",
                "detail": reason,
            })

    # 4a. ol-quote-tracker (Turso) sync failure streak — added 2026-05-28
    # per Michael's "verify/harden existing sync" direction. Read the audit
    # log directly so the red flag has the actual error string the operator
    # needs (QC-037 also fires, but the audit's QC line is generic).
    try:
        sync_log = REPORTS / "quote-tracker-sync.log"
        if sync_log.exists():
            lines = [
                ln for ln in sync_log.read_text(encoding="utf-8", errors="ignore").splitlines()
                if ln.strip()
            ]
            streak = 0
            last_err = None
            for ln in reversed(lines):
                if "ok=True" in ln or "no APP_PASSWORD configured" in ln:
                    break
                streak += 1
                if last_err is None:
                    last_err = ln
                if streak >= 5:
                    break
            if streak >= 3 and last_err:
                err_excerpt = last_err.split("err=", 1)[-1][:200] if "err=" in last_err else last_err[:200]
                flags.append({
                    "level": "🔴",
                    "title": f"Turso sync failed {streak} fires in a row",
                    "detail": (
                        f"ol-quote-tracker entity registry is going stale. "
                        f"Last error: {err_excerpt}. "
                        f"Check {sync_log.name} for the full sequence and run "
                        f"`python3 scripts/sync_to_quote_tracker.py --verbose` "
                        f"locally to reproduce."
                    ),
                })
    except Exception:
        pass

    # 4c. Deployment drift — local repo HEAD is behind origin/main, meaning
    # fixes that have been pushed are not actually running on the Cloud PC.
    # Reads deploy/run_daily_laptop.cmd's reports/deployment-sha.txt marker
    # (also surfaced by QC-053; this gives the audit a dedicated red flag
    # with the explicit "git pull" instruction).
    try:
        dep_marker = REPORTS / "deployment-sha.txt"
        if dep_marker.exists():
            txt = dep_marker.read_text(encoding="utf-8").strip()
            m = re.search(r"BEHIND=(\d+)", txt)
            if m and int(m.group(1)) > 0:
                flags.append({
                    "level": "🔴",
                    "title": f"Cloud PC running stale code — {m.group(1)} commit(s) behind main",
                    "detail": (
                        f"deployment-sha.txt: {txt[:200]}. "
                        "Wrapper Step 0 `git pull` either failed or there are "
                        "merge conflicts blocking the pull. Open the Cloud PC, "
                        "cd to OneDrive/PROJECT HILMAR/hilmar-daily-routine, run "
                        "`git pull origin main` manually, and check the run-log "
                        "for the next failure."
                    ),
                })
    except Exception:
        pass

    # 4b. Daily test/coverage routine failed (added 2026-05-28; 2026-06-01
    # extended with per-test diagnostics). The audit is now the place a code
    # regression surfaces — run_audit_tests.py writes reports/test-result.json
    # every fire; a FAIL means a test broke or coverage fell below the
    # pyproject gate.
    #
    # 2026-06-01 — when the artifact carries the new per-test fields
    # (errors[], error_type_buckets[], collection_error, pytest_output_path),
    # render them so the audit tells Michael WHAT broke instead of just
    # "22 error". Backward-compatible: an artifact from before this change
    # (no errors[]) renders as the original headline-only flag.
    test_res = _read_json(REPORTS / "test-result.json") or {}
    if test_res.get("status") == "FAIL":
        counts = test_res.get("counts") or {}
        why = []
        if not test_res.get("tests_ok", True):
            why.append(f"{counts.get('failed', 0)} failed, {counts.get('error', 0)} error")
        if not test_res.get("coverage_ok", True):
            why.append(
                f"coverage {test_res.get('total_coverage')}% below "
                f"gate {test_res.get('gate')}%"
            )
        buckets = test_res.get("error_type_buckets") or []
        coll = test_res.get("collection_error")
        out_ref = test_res.get("pytest_output_path") or "reports/pytest-output.txt"
        bucket_text = ""
        if buckets:
            bucket_text = "Top error types: " + ", ".join(
                f"{b.get('count', 0)}x {b.get('error_type', '?')}" for b in buckets[:4]
            ) + ". "
        coll_text = "pytest collection failed (modules failed to import). " if coll else ""

        # Build a <pre> block of up to 5 representative per-test excerpts.
        # All content goes through _esc to handle quotes / unicode / HTML
        # specials; lines that look long are visually truncated so a giant
        # traceback line can't blow the email out.
        excerpt_html = _format_test_excerpts(test_res.get("errors") or [])

        flags.append({
            "level": "🔴",
            "title": "Daily test/coverage routine FAILED",
            "detail": (
                f"{'; '.join(why)}. The shipped code is not green — fix before "
                "the next fire. "
                + coll_text
                + bucket_text
                + f"Run: python3 scripts/run_audit_tests.py. "
                f"Full pytest output: {out_ref}."
            ),
            "extra_html": excerpt_html,
        })

    # 5. QC errors (status != CLEAN) — surface each failing check as its own
    # red flag with the actual error text. Prior version pointed to
    # reports/qc-result.json, which is unreadable from the iPhone audit. Group
    # repeated firings by QC-NNN so a check that fires across 5 records
    # collapses to one row with a count + sample messages.
    if qc and qc.get("status") != "CLEAN":
        error_details = qc.get("error_details") or []
        grouped, ungrouped = _group_qc_messages(error_details)
        if not grouped and not ungrouped:
            # Counts say errors exist but no per-message detail came through —
            # fall back to the headline so we never go silent.
            flags.append({
                "level": "🔴",
                "title": f"QC status: {qc.get('status', '?')}",
                "detail": (
                    f"Errors: {qc.get('errors', 0)} | Warnings: {qc.get('warnings', 0)}. "
                    "qc-result.json had no error_details to expand."
                ),
            })
        else:
            # Avoid duplicating QC checks that ALREADY have their own
            # dedicated red flag earlier in this collector — the audit was
            # rendering "Daily test/coverage routine FAILED" (step 4b above)
            # AND a separate "QC-052 ERROR" row carrying the same string.
            # The dedicated red flags carry richer detail (per-test excerpts,
            # bucket counts) so prefer those; suppress the generic QC-line.
            QC_DEDUPLICATED_BY_DEDICATED_FLAG = {
                # QC-052 is the daily test/coverage check. Its dedicated
                # red flag (step 4b) is much richer than the generic
                # "QC-052 ERROR: <message>" expansion. Drop the generic
                # one when the dedicated flag fired.
                "QC-052": (test_res.get("status") == "FAIL"),
            }
            shown = 0
            for check_id, msgs in grouped:
                if shown >= 8:
                    break
                if QC_DEDUPLICATED_BY_DEDICATED_FLAG.get(check_id):
                    continue
                count = len(msgs)
                title = f"{check_id} ERROR" + (f" × {count}" if count > 1 else "")
                preview = msgs[:3]
                detail = " · ".join(_strip_qc_prefix(m, check_id) for m in preview)
                if count > len(preview):
                    detail += f" · +{count - len(preview)} more"
                flags.append({"level": "🔴", "title": title, "detail": detail})
                shown += 1
            for msg in ungrouped[:max(0, 8 - shown)]:
                flags.append({
                    "level": "🔴",
                    "title": "QC error (uncategorized)",
                    "detail": msg,
                })

    # 6. Stage stale > 36h on weekday
    # Bug fix 2026-05-07: was reading stale .jsonl (legacy) instead of current
    # .txt (refresh_stage's actual output). Now matches qc_selfheal.py QC-008
    # path resolution: .txt first, fallback .jsonl.
    #
    # PRE-FIRE / OFF-HOURS SUPPRESSION (added 2026-05-18 per Michael
    # "TWO TERRIBLE DAILY AUDITS CAME IN"; updated 2026-06-16 for the fire
    # move). The scheduled task now fires at ~6 PM ET Mon-Fri. Before
    # 6:30 PM ET on any weekday, the stage is naturally stale because
    # refresh_stage hasn't run yet today. Flagging this as a RED FLAG is a
    # pre-fire artifact, not a genuine problem. Only fire if it's PAST the
    # scheduled fire time AND the stage is still stale.
    try:
        scripts_dir = ROOT / "scripts"
        stage_path = scripts_dir / "stage_emails.txt"
        if not stage_path.exists():
            stage_path = scripts_dir / "stage_emails.jsonl"
        if stage_path.exists():
            now_et = datetime.now(core.ET)
            # Post-fire window: Mon-Fri AND past 6:30 PM ET (after the ~6 PM ET
            # scheduled fire). Weekends + pre-fire weekday hours are suppressed.
            is_business_hours = (
                now_et.weekday() < 5
                and (now_et.hour > 18 or (now_et.hour == 18 and now_et.minute >= 30))
            )
            if is_business_hours:
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
                                "expired. Check secrets/token-cache.bin (or legacy .json) mtime."
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

    # 0. QC warnings — surface inline so they don't stay buried in
    # qc-result.json. Warnings don't block ship, hence yellow, but Michael
    # needs to see them from the iPhone audit. Grouped by check id;
    # exception-only warnings (QC-NN: check failed with exception …) are
    # rolled into a single line so a single broken check doesn't drown the
    # section.
    if qc:
        warning_details = qc.get("warning_details") or []
        grouped, ungrouped = _group_qc_messages(warning_details)
        # Split exception warnings from real findings — exceptions point at a
        # check that crashed, not at data drift.
        exception_groups = []
        finding_groups = []
        for check_id, msgs in grouped:
            non_exc = [m for m in msgs if "check failed with exception" not in (m or "")]
            exc_only = len(non_exc) == 0 and len(msgs) > 0
            if exc_only:
                exception_groups.append((check_id, msgs))
            else:
                finding_groups.append((check_id, non_exc))
        for shown, (check_id, msgs) in enumerate(finding_groups):
            if shown >= 6:
                break
            count = len(msgs)
            title = f"{check_id} WARN" + (f" × {count}" if count > 1 else "")
            preview = msgs[:3]
            detail = " · ".join(_strip_qc_prefix(m, check_id) for m in preview)
            if count > len(preview):
                detail += f" · +{count - len(preview)} more"
            obs.append({"level": "🟡", "title": title, "detail": detail})
        if exception_groups:
            ids = ", ".join(cid for cid, _ in exception_groups[:6])
            extra = "" if len(exception_groups) <= 6 else f" +{len(exception_groups) - 6} more"
            obs.append({
                "level": "🟡",
                "title": f"{len(exception_groups)} QC check(s) crashed",
                "detail": (
                    f"Threw an exception during evaluation: {ids}{extra}. "
                    "The check itself is broken, not the data — see qc_selfheal.py."
                ),
            })
        for msg in ungrouped[:3]:
            obs.append({
                "level": "🟡",
                "title": "QC warning (uncategorized)",
                "detail": msg,
            })

    # Code-health from the daily test routine (added 2026-05-28). PASS with
    # under-tested modules is an observation (the learning worklist for
    # "every line of code has testing"); SKIPPED means dev deps are missing
    # on this host so the audit couldn't verify code health at all.
    test_res = _read_json(REPORTS / "test-result.json") or {}
    if test_res.get("status") == "SKIPPED":
        obs.append({
            "level": "🟡",
            "title": "Test/coverage routine skipped — code health unverified",
            "detail": (
                f"{test_res.get('reason', 'pytest unavailable on this host')} "
                "Install dev deps so the daily audit can confirm the suite is green: "
                "pip install -e '.[dev]'."
            ),
        })
    elif test_res.get("status") == "PASS":
        untested = test_res.get("untested_modules") or []
        below = test_res.get("modules_below_floor") or []
        if untested:
            obs.append({
                "level": "🟡",
                "title": f"{len(untested)} module(s) ship with 0% test coverage",
                "detail": (
                    f"{', '.join(untested[:6])}. Suite is green and overall "
                    f"coverage {test_res.get('total_coverage')}% clears the "
                    f"{test_res.get('gate')}% gate, but these files have no tests "
                    "at all — the top targets for 'every line tested'."
                ),
            })
        elif below:
            obs.append({
                "level": "🟡",
                "title": f"{len(below)} module(s) below the {test_res.get('module_floor')}% coverage floor",
                "detail": (
                    ", ".join(f"{m['module']} ({m['coverage']}%)" for m in below[:6])
                    + ". Overall coverage clears the gate; these are the next "
                    "tests to write."
                ),
            })

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
            # The fire moved to ~6 PM ET (2026-06-16), so the wrapper's start
            # stamp in the run log is now 18:00, not 10:00.
            fire_started = re.search(rf"{today_iso} 18:00:\d+\.\d+", tail)
            today_send = "Sent. request-id=" in tail and tail.find("Sent. request-id=") > tail.find(today_iso)
            if fire_started and not today_send:
                sugg.append({
                    "level": "💡",
                    "title": "Cloud PC scheduled task fired but didn't send today",
                    "detail": (
                        "6 PM ET fire entered the wrapper but no 'Sent. request-id=' line "
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
            # Optional pre-formatted HTML appended after the escaped detail.
            # Used by the test-diagnostics red flag (QC-052) to embed a
            # monospace <pre> block of pytest error excerpts. Stays Outlook-safe:
            # <pre> renders, no flexbox/SVG/linear-gradient.
            extra = it.get("extra_html") or ""
            rows += f"""
<div style="margin:10px 0;padding:12px;background:#f8fafc;border-left:3px solid {color};border-radius:4px">
  <div style="font-weight:600;color:#0f172a;font-size:14px">{_esc(it.get('level', ''))} {_esc(it.get('title', ''))}</div>
  <div style="margin-top:4px;color:#475569;font-size:13px;line-height:1.5">{_esc(it.get('detail', ''))}</div>
  {extra}
</div>"""
    return f"""
<h2 style="margin:20px 0 8px;color:{color};font-size:16px;border-bottom:1px solid #e2e8f0;padding-bottom:6px">
  {_esc(title)} ({len(items)})
</h2>
{rows}
"""


def _carrier_diag_section(qc):
    """Render the scrubbed carrier-extraction diagnostics from qc-result.json.

    Lists each stuck QC-056 (rate-without-carrier) / QC-002 (WIN-without-
    carrier_won) row: lane, which check, the rate, whether a body was cached,
    and the SCRUBBED text the parser failed on (in a monospace block). Lets a
    human/Claude reading the audit see WHY each row has no carrier — a missed
    token vs a genuinely bare rate — and fix the parser precisely.

    Returns "" (omits the section entirely) when there are no diagnostics.
    Every value is _esc()'d; the snippet is already PII-scrubbed upstream by
    qc_selfheal._carrier_diag_snippet (sentry_setup._scrub_string).
    """
    diags = (qc or {}).get("carrier_diagnostics") or []
    if not diags:
        return ""
    rows = ""
    for d in diags:
        lane = _esc(d.get("lane") or "?")
        check = _esc(d.get("check") or "?")
        rate = _esc(d.get("rate"))
        has_body = "yes" if d.get("has_body") else "no"
        snippet = _esc(d.get("snippet") or "")
        rows += f"""
<div style="margin:10px 0;padding:12px;background:#f8fafc;border-left:3px solid #0ea5e9;border-radius:4px">
  <div style="font-weight:600;color:#0f172a;font-size:14px">{lane} — {check}</div>
  <div style="margin-top:4px;color:#475569;font-size:13px;line-height:1.5">rate: {rate} · body cached: {has_body}</div>
  <pre style="margin:8px 0 0;padding:10px;background:#0f172a;color:#e2e8f0;border-radius:4px;font-family:Consolas,Menlo,monospace;font-size:11px;line-height:1.45;white-space:pre-wrap;overflow-x:auto">{snippet}</pre>
</div>"""
    return f"""
<h2 style="margin:20px 0 8px;color:#0ea5e9;font-size:16px;border-bottom:1px solid #e2e8f0;padding-bottom:6px">
  🔬 Carrier-extraction diagnostics (scrubbed) ({len(diags)})
</h2>
<p style="margin:0 0 8px;font-size:12px;color:#64748b">
  The exact (PII-scrubbed) text each stuck row's carrier parse failed on — a missed token vs a genuinely bare rate.
</p>
{rows}
"""


def render_html(red, yellow, suggestions, report_date, qc):
    rd_label = _strftime(report_date, "%A %B %-d, %Y")
    now_et = datetime.now(timezone.utc).astimezone(core.ET)
    stamp = _strftime(now_et, "%B %-d, %Y at %-I:%M %p ET")

    # Mobile overrides (Michael 2026-07-01 "reading email on phone is poorly
    # formatted"): the audit is sized for desktop (800px container, 8-9 column
    # rate tables). iOS Mail / the Gmail app honor <style> @media; desktop
    # Outlook ignores it — so these rules ONLY change the phone rendering:
    # full-bleed container with tighter padding + ONE horizontal scroll
    # surface, and data tables keep readable column geometry (min-width) and
    # scroll sideways instead of squishing to 3-char columns. Same .hx-*
    # vocabulary as gen_email so the two emails stay consistent.
    mobile_style = """<style>
@media only screen and (max-width:640px) {
  .hx-wrap { max-width:100% !important; padding:14px 8px !important; overflow-x:auto !important; -webkit-overflow-scrolling:touch; }
  table.hx-data { min-width:640px !important; }
}
</style>"""

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{mobile_style}</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:{EMAIL_FONT}">
<div class="hx-wrap" style="max-width:800px;margin:0 auto;background:white;padding:24px">
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
{_carrier_diag_section(qc)}

  <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e2e8f0;color:#94a3b8;font-size:11px">
    Auto-generated by scripts/gen_improvements_report.py.
    Raw inputs: tracking-data-v2.json, reports/qc-result.json, reports/drift-result.json,
    reports/run-log.txt. To suppress a recurring suggestion, edit the collector function.
    To request a new check, ask Claude.
  </div>
{_rate_intel_section_inline()}
{_insights_full_section_inline()}
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

    ACTIVE-ISSUES FILTER (per Michael 2026-05-18 "TWO TERRIBLE DAILY
    AUDITS CAME IN"). Only surface issues that are STILL FIRING — drop
    issues whose last event is older than the latest commit on main.
    Reason: during my own fix-cycles I introduced + fixed bugs in the
    same day. Those events stayed in Sentry as "🆕 new (24h)" even
    though the bug is already gone from the code. Showing them as
    alarming red items is misleading. Filter:

      issue.lastSeen < current_HEAD_commit_time → drop from rendering

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
        # 2026-05-19 PM (Michael "assume you are now locked with sentry for
        # auto fix and seer"): enrich each issue with Seer's diagnosis +
        # autofix state so the audit row shows what Seer is doing. Silent
        # no-op when Seer isn't enabled (returns issues unchanged).
        try:
            from sentry_seer import enrich_audit_with_seer
            for k in ("new_in_period", "recurring", "resolved_in_period"):
                if s.get(k):
                    s[k] = enrich_audit_with_seer(s[k], max_issues=5)
        except Exception:
            pass  # Seer enrichment is best-effort; never breaks the audit
    except Exception:
        return ""

    # Compute the latest commit time on main — issues whose last event
    # is older than this are presumed fixed by a recent commit.
    head_commit_ts = None
    try:
        import subprocess
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            head_commit_ts = out.stdout.strip()
    except Exception:
        head_commit_ts = None

    def _is_zombie(issue):
        """An issue is a 'zombie' if its last event predates the current
        HEAD commit — meaning a fix has shipped after the last fire."""
        if not head_commit_ts:
            return False
        last_seen = issue.get("lastSeen", "")
        if not last_seen:
            return False
        try:
            from datetime import datetime
            ls = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
            head = datetime.fromisoformat(head_commit_ts.replace("Z", "+00:00"))
            return ls < head
        except Exception:
            return False

    # Filter the lists to active (non-zombie) issues only
    s["new_in_period"] = [i for i in s.get("new_in_period", []) if not _is_zombie(i)]
    s["recurring"] = [i for i in s.get("recurring", []) if not _is_zombie(i)]
    # Resolved-in-period stays — those are good news to show

    # Recompute the summary counts since we filtered
    active_unresolved = 0
    try:
        # Refetch the unresolved list and exclude zombies
        all_unresolved = api.list_issues(stats_period="14d", query="is:unresolved", limit=100)
        active_unresolved = sum(1 for i in all_unresolved if not _is_zombie(i))
        s["unresolved_count"] = active_unresolved
    except Exception:
        pass

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
        # 2026-05-19 PM Seer enrichment: when enrich_audit_with_seer
        # attached seer_summary / seer_autofix, render them under the
        # issue title so the operator sees Seer's diagnosis + autofix
        # status inline. Status is colored by state.
        seer_html = ""
        seer_sum = issue.get("seer_summary") or ""
        if seer_sum:
            seer_html += f'<div style="font-size:11px;color:#475569;margin-top:3px;font-style:italic">🤖 Seer: {seer_sum[:200]}</div>'
        seer_af = issue.get("seer_autofix") or {}
        if seer_af.get("status"):
            status = seer_af["status"]
            colors = {
                "COMPLETED":  "#16a34a",
                "PROCESSING": "#3b82f6",
                "ERROR":      "#dc2626",
                "NEED_MORE_INFORMATION": "#d97706",
            }
            color = colors.get(status, "#64748b")
            pr = seer_af.get("pr_url") or ""
            pr_link = f' · <a href="{pr}" style="color:{color}">PR</a>' if pr else ""
            conf = seer_af.get("confidence")
            conf_s = f" · conf {conf:.0%}" if isinstance(conf, (int, float)) else ""
            seer_html += (
                f'<div style="font-size:11px;margin-top:2px">'
                f'<span style="color:{color};font-weight:600">🛠️ Autofix: {status}</span>{conf_s}{pr_link}'
                f'</div>'
            )
        return (
            f'<tr><td style="padding:4px 8px;font-family:monospace;font-size:11px;color:#0f172a;vertical-align:top">{marker} {short_id}</td>'
            f'<td style="padding:4px 8px;font-size:12px;vertical-align:top">'
            f'<a href="{permalink}" style="color:#1a3d9c">{title}</a>{seer_html}</td>'
            f'<td style="padding:4px 8px;text-align:center;font-size:12px;color:#475569;vertical-align:top">{count}x</td>'
            f'<td style="padding:4px 8px;font-size:11px;color:#64748b;vertical-align:top">{last_seen}</td></tr>'
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

    # 2026-05-19 Task #11 — also surface any auto-actions taken by
    # qc_actions_from_sentry on this pipeline fire. The runner writes
    # its report to reports/qc-actions-from-sentry.json; render a
    # compact summary line so Michael sees what fired without opening
    # Sentry. Silent no-op if the file is absent.
    actions_html = ""
    try:
        from pathlib import Path as _P
        _qa = _P(__file__).resolve().parent.parent / "reports" / "qc-actions-from-sentry.json"
        if _qa.exists():
            import json as _json
            _qa_data = _json.loads(_qa.read_text(encoding="utf-8"))
            if _qa_data.get("issues_scanned", 0) > 0:
                _resolved = _qa_data.get("resolved", 0)
                _commented = _qa_data.get("commented", 0)
                _dry = " (dry-run)" if _qa_data.get("dry_run") else ""
                actions_html = (
                    f'<div style="background:#eff6ff;border-left:4px solid #1a3d9c;'
                    f'padding:8px 14px;margin:6px 0 10px;border-radius:4px;font-size:12px;color:#0f172a">'
                    f'<strong>🤖 Sentry-driven actions{_dry}:</strong> '
                    f'scanned {_qa_data["issues_scanned"]} unresolved · '
                    f'{_commented} commented · {_resolved} auto-resolved. '
                    f'<span style="color:#64748b">See reports/qc-actions-from-sentry.json for per-issue detail.</span>'
                    f'</div>'
                )
    except Exception:
        pass

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
{actions_html}
<table class="hx-data" style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px">
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
    """Inline the rate-intelligence section if it exists. The section is
    produced by gen_rate_intelligence.py which runs before this script
    in the pipeline. Added 2026-05-13 per Michael 'i love all of this and
    want it now' — rate-negotiation cheat sheet + cooling/regression
    alerts surface in the daily idealx.us audit."""
    section_path = REPORTS / "rate-intelligence-section.html"
    if section_path.exists():
        try:
            html = section_path.read_text(encoding="utf-8")
            # Tag the prebuilt section's tables for the mobile @media rules
            # (min-width + sideways scroll) at embed time — cheaper and safer
            # than re-plumbing gen_rate_intelligence.py, and a no-op for any
            # table already carrying a class attribute.
            return html.replace("<table style=", '<table class="hx-data" style=')
        except Exception:
            return ""
    return ""


def _insights_full_section_inline():
    """Inline the full AI-insights narrative (System / Design / Data /
    Business) if it exists. The section is produced by gen_insights.py which
    runs before this script in the pipeline. Added 2026-07-11 wiring
    docs/INSIGHTS-DESIGN.md M3.11 — the four-section narrative is
    operational-internal (parser miss rates, schema advice), so it lands in
    THIS private idealx.us audit; the staff daily gets Business only.
    Mirrors _rate_intel_section_inline, plus a freshness guard: a stale
    yesterday narrative must never render as if it were today's."""
    section_path = REPORTS / "insights-full.html"
    try:
        if not section_path.exists():
            return ""
        if datetime.fromtimestamp(section_path.stat().st_mtime).date() != datetime.now().date():
            return ""
        html = section_path.read_text(encoding="utf-8").strip()
        if not html:
            return ""
        return (
            '<h2 style="margin:24px 0 8px;color:#1a3d9c;font-size:16px;'
            'border-bottom:2px solid #76b82a;padding-bottom:6px">'
            "🤖 AI Insights — System / Design / Data / Business</h2>"
            f'<div style="font-size:13px;color:#1f2937">{html}</div>'
        )
    except Exception:
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
