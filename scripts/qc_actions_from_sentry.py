"""
qc_actions_from_sentry.py — Sentry-driven QC remediation engine.

Per Michael 2026-05-19 ("go with task 11 and llm"). Sentry surfaces
errors + warnings in real time, but the Cloud PC pipeline doesn't have
a public IP to receive webhooks. This module is the polling equivalent:
on every daily fire (run AFTER qc_selfheal post-patch), pull unresolved
Sentry issues + dispatch a remediation action per issue via the lookup
table below.

The output:
  1. Each actioned issue gets a Sentry comment saying what was done.
  2. Safe-to-auto-resolve issues are resolved on success.
  3. Actions that need operator decision get a Sentry comment with the
     diagnosis but stay open.
  4. A summary line is written to reports/qc-actions-from-sentry.json
     (machine-readable) + appended to qc_selfheal.results so the daily
     audit shows what fired.

ARCHITECTURE — why polling vs webhook?

Sentry webhooks need a publicly reachable HTTPS endpoint. The Cloud PC
that runs Hilmar's pipeline is behind NAT with no static IP. Polling
on the daily fire is functionally equivalent for once-a-day cadence —
issues created since the last fire get picked up at the next fire,
which is at most 24h later. For sub-daily action we'd need a webhook
endpoint (Cloudflare Worker / Azure Function) — that's a deploy task,
not a code task.

ACTION TYPES

  log_only             — comment on the issue, do nothing else
  rerun_parser_acc     — re-run src.hilmar.parser_accuracy + comment
  resolve_if_stale     — resolve issue if lastSeen > N hours ago
  resolve_if_post_fix  — resolve issue if HEAD commit timestamp is newer
                         than issue's lastSeen (= a fix shipped after the
                         issue, even if Sentry hasn't auto-resolved)
  trigger_seer         — request Seer autofix (via sentry_seer.py)
  flag_for_operator    — add high-priority comment + tag, leave open

ENVIRONMENT

  HILMAR_QC_ACTIONS_DRY_RUN=1 — dry-run, log what WOULD be done.
                                 No comments posted, no resolves.
  HILMAR_QC_ACTIONS_LOOKBACK_H — default 26h (one daily fire + 2h slack)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Action table — keyed by qc_check tag from the Sentry event.
#
# qc_selfheal sets `qc_check` tag on every capture_qc_error/_warning
# event (see sentry_setup.before_send). Issues without a qc_check tag
# fall through to the message-pattern matcher below.
# ─────────────────────────────────────────────────────────────────────

ACTIONS: dict[str, dict] = {
    "QC-027": {
        "name": "Carrier extraction regression",
        "action": "resolve_if_post_fix",
        "comment": "Carrier extraction restored — see patch_carriers.py PASS 4 + body_parser.parse_subject_carrier.",
        "auto_resolve_safe": True,
    },
    "QC-039": {
        "name": "Parser accuracy below threshold",
        "action": "rerun_parser_acc",
        "comment": "Parser accuracy regression. Re-run computed accuracy below; if still <95% see docs/PARSER-GAPS.md.",
        "auto_resolve_safe": False,
    },
    "QC-040": {
        "name": "Cross-folder enum drift",
        "action": "flag_for_operator",
        "comment": "scripts/core.py and src/hilmar/core.py have drifted on a constant. Align or add to ALLOWED_CROSS_FOLDER_DRIFT.",
        "auto_resolve_safe": False,
    },
    "QC-041": {
        "name": "Classifier form drift in data",
        "action": "flag_for_operator",
        "comment": "Mixed 3-state / 4-state status rows in tracking-data-v2.json. Backup + run a single-form pass.",
        "auto_resolve_safe": False,
    },
    "QC-042": {
        "name": "Data-URI in email body",
        "action": "resolve_if_post_fix",
        "comment": "Data-URI guard tripped. Branding.py now uses cid: attachments (commit fa337b2). Re-verify reports/email-body.html.",
        "auto_resolve_safe": True,
    },
    "QC-043": {
        "name": "Sentry self-improvement loop",
        "action": "log_only",
        "comment": "Meta-issue — Sentry self-reporting via QC-043. No action; informational.",
        "auto_resolve_safe": True,
    },
    # 2026-05-19 PM (Michael "publish this... make sure it's in qc audits
    # self heal sentry/seer with autofix"): email-format invariants from
    # the v3-v7 iteration. ERROR-level checks; resolve_if_post_fix is
    # appropriate when the offending render is replaced by a subsequent
    # fix commit, but for active issues we want Seer to triage so it
    # diagnoses the root cause (often a Python-side render bug).
    "QC-044": {
        "name": "Double-escaped HTML entities in email body",
        "action": "trigger_seer",
        "comment": "Email body contains &amp;amp; sequences — Outlook will render them literally. Look for call sites passing pre-escaped strings into gen_email helpers that run _esc() again. Seer: diagnose which helper double-escapes.",
        "auto_resolve_safe": False,
    },
    "QC-045": {
        "name": "Table header invisible in Outlook (gradient only)",
        "action": "trigger_seer",
        "comment": "Email table header row uses background:linear-gradient without a solid background-color fallback. Outlook strips the gradient → white text on white background = invisible header. Seer: pinpoint which helper needs background-color: before background:linear-gradient.",
        "auto_resolve_safe": False,
    },
    "QC-046": {
        "name": "Pending Hilmar timestamps all rendering as dashes",
        "action": "trigger_seer",
        "comment": "Pending section has 5+ dash cells and zero real PT/ET timestamps. Likely Windows-incompatible strftime token (%-d / %-I) — Unix-only, raises ValueError on Win Cloud PC, except returns dash. Seer: confirm gen_email._fmt_local_full uses %d / %I + .replace() strip pattern.",
        "auto_resolve_safe": False,
    },
    "QC-047": {
        "name": "Win Rate KPI ↔ explainer banner drift",
        "action": "trigger_seer",
        "comment": "KPI tile Win Rate % and explainer banner % disagree by >0.2pp. Means somebody changed one formula but not the other. Both should be Wins / (Wins + Q&L). Seer: locate the divergent computation and align.",
        "auto_resolve_safe": False,
    },
    "QC-048": {
        "name": "Implausible turnaround (>40h biz-hours)",
        "action": "trigger_seer",
        "comment": "Rows have turnaround_biz_hours > 40h. Real OL response time is sub-day. Likely cause: link_bookings_to_requests leaked booking timestamp into turnaround calc when no prior rate response existed. Seer: confirm scripts/ingest.py link_bookings_to_requests leaves turnaround_biz_hours None when bk.get('sent') is the only response signal.",
        "auto_resolve_safe": False,
    },
    # NOTE: this dict briefly carried TWO "QC-049" keys (the first silently
    # shadowed — Python keeps the last). Merged 2026-06-10; keep one entry
    # per check, the governance test can't see duplicate literals.
    "QC-049": {
        "name": "Unconfirmed wins — WIN rows missing MDOLX booking ref",
        "action": "flag_for_operator",
        "comment": "Send-signal promotions creating WIN rows that don't pick up the matching MDOLX booking confirmation. Review each: link the real booking confirmation, or demote the false win via the operator-corrections layer. Fix landed: link_bookings_to_requests now matches via In-Reply-To / References headers. If still high after a few daily fires, check refresh_stage.py is fetching internetMessageHeaders and ingest is reading row['in_reply_to']/['references'].",
        "auto_resolve_safe": False,
    },
    "QC-050": {
        "name": "Backup freshness / retention",
        "action": "flag_for_operator",
        "comment": "Daily backup not written in expected window OR data-backups/ directory missing. Pipeline Step 1 (backup.py) should create a tracking-data-v2_<timestamp>.json snapshot every fire. If wedged: rules.backup_retention_count config, disk write perms, scripts/backup.py logic.",
        "auto_resolve_safe": False,
    },
    "QC-051": {
        "name": "Phantom-duplicate WIN survived dedup",
        "action": "resolve_if_post_fix",
        "comment": "phase_4 content-dedup left two WIN rows for the same booking. Collapse fix shipped (commit eac597f). If recurring, check the dedup key in qc_selfheal.phase_4_duplicates against the new row shape.",
        "auto_resolve_safe": True,
    },
    "QC-052": {
        "name": "Daily test/coverage routine failed",
        "action": "flag_for_operator",
        "comment": "A test broke OR coverage fell below the pyproject gate OR pytest/pytest-cov isn't importable on the Cloud PC. Run scripts/run_audit_tests.py; if collection failed on missing deps, install into the wrapper's Python (see RUNBOOK 'wrapper started but pipeline never completed'). Real failures are root bugs — fix, don't lower the gate.",
        "auto_resolve_safe": False,
    },
    "QC-053": {
        "name": "Deployment drift — local HEAD behind origin/main",
        "action": "flag_for_operator",
        "comment": "Cloud PC is running stale code (a merged fix didn't deploy). Run deploy\\sync_now.cmd on the Cloud PC, or git pull + xcopy. See RUNBOOK 'wrapper started but pipeline never completed'.",
        "auto_resolve_safe": False,
    },
    "QC-054": {
        "name": "Wrapper Python missing required runtime deps",
        "action": "flag_for_operator",
        "comment": "One or more import-required modules aren't installed in the interpreter the wrapper uses. The error message lists the exact pip command. Install into the SAME Python the wrapper resolves to (see reports/run-log.txt 'PY:' line). If sentry_sdk is the missing module, this is the root of QC-055 + HILMAR-DAILY-TRACKER-9 missed-check-in alerts.",
        "auto_resolve_safe": False,
    },
    "QC-055": {
        "name": "Sentry cron heartbeat not registering",
        "action": "flag_for_operator",
        "comment": "Pipeline ran but the cron check-in didn't reach Sentry — the missed-check-in alert (HILMAR-DAILY-TRACKER-9) is a false positive. Usually QC-054 root (missing sentry_sdk). If sentry_sdk is installed, verify secrets/sentry-dsn.txt and that the Cloud PC can reach sentry.io.",
        "auto_resolve_safe": False,
    },
    "QC-056": {
        "name": "OL rate quoted but carrier missing",
        "action": "flag_for_operator",
        "comment": "A row has an OL rate but no carrier. Root cause is usually a rate-response whose carrier column OL relabeled (the body_parser carrier scan should now catch header aliases + data-cell + prose). If it persists: re-ingest (reprocess_bodies.py + ingest.py + patch_carriers.py) so the strengthened parser re-reads the body. If the carrier is genuinely absent from OL's quote (bare rate, carrier assigned at booking), it will fill from the booking confirmation on the WIN — no action needed.",
        "auto_resolve_safe": False,
    },
    "QC-057": {
        "name": "Staged Lonny RFQ silently dropped at intake",
        "action": "flag_for_operator",
        "comment": "A staged lonny_outbound rate request (not operational, not out-of-scope) yielded no parseable destination, so ingest.build_requests dropped it and it is MISSING from the report — the silent-drop failure mode behind the 2026-06-24 Busan/Korea miss. No auto-heal is possible (a lane can't be invented). FIX: read the dropped subject(s) in the QC-057 message, extend body_parser.parse_subject_lane to resolve that lane shape, then re-ingest (reprocess_bodies.py + ingest.py) so the row is rebuilt. >=3 dropped at once means a systemic parser regression — diff body_parser before triaging individual subjects.",
        "auto_resolve_safe": False,
    },
    "QC-058": {
        "name": "Turso historian stale (finalized-row append failing)",
        "action": "flag_for_operator",
        "comment": "The durable Turso stats historian is configured but its newest write is >26h old — the daily 'Historian (finalized → Turso)' append likely failed. This does NOT affect the client report (the historian is write-only analytics), so it never gates a fire. Check: the historian step's exit in run-log.txt, secrets/historian-turso.txt (URL+token), and that libsql is installed on the fire host. Stats accuracy degrades while stale but no live data is lost (tracking-data is rebuilt from Outlook each fire).",
        "auto_resolve_safe": False,
    },
    "QC-059": {
        "name": "Data-flow break — cached parse stale vs current parser",
        "action": "flag_for_operator",
        "comment": "Cached email parses no longer match what the current body_parser produces — a parser fix did not reach the back-catalog already in the window. QC-059 already SELF-HEALED (backfilled the stale parses), so this is informational: it means the pre-ingest 'Parser backfill (reprocess cache)' step did not run or failed this fire (the back-stop caught it). The backfilled fields land on the NEXT fire automatically; to surface them in TODAY's report, re-run ingest. Check that the reprocess step is wired + exited 0 in run-log.txt. No live data is lost — tracking-data rebuilds from Outlook each fire.",
        "auto_resolve_safe": False,
    },
    "cron.missed_checkin": {
        "name": "Sentry cron monitor missed check-in (hilmar-daily-pipeline)",
        "action": "resolve_if_post_fix",
        "comment": (
            "Cron 'missed check-in' for hilmar-daily-pipeline. Root cause is "
            "GitHub cron lateness + the evening liveness backstop recovering a "
            "fire past the old 95-min margin — NOT a real missed fire "
            "(liveness.yml + heartbeat.yml confirm the run independently). "
            "Margin widened to 290 min in sentry_setup.py so the monitor pages "
            "only on a true all-evening miss; resolving now that the fix is "
            "deployed. Sentry recovery_threshold=1 also auto-resolves on the "
            "next in-window check-in."
        ),
        "auto_resolve_safe": True,
    },
    "ingest.non_hilmar_filtered": {
        "name": "Non-HILMAR row filtered",
        "action": "log_only",
        "comment": "Correct rejection of NUMIDIA-only rows per the 2026-05-17 fix. Suppress this in Sentry filters if too noisy.",
        "auto_resolve_safe": True,
    },
}


#: Days of silence after which an UNMAPPED issue auto-resolves (added
#: 2026-05-28). Mapped issues honor their ACTIONS entry; this only fires for
#: orphaned unresolved errors that the operator never closed manually.
#: A re-occurrence after auto-resolve creates a fresh issue, so nothing is
#: actually lost — Sentry's grouping means the same fingerprint re-opens.
STALE_AUTO_RESOLVE_DAYS = 7


# Fallback action when no qc_check tag matches
DEFAULT_ACTION = {
    "name": "Unmapped issue",
    "action": "log_only",
    "comment": "No QC-action mapping for this issue. Add an entry to qc_actions_from_sentry.ACTIONS if remediation is known.",
    "auto_resolve_safe": False,
}

# 2026-05-19 PM (Michael "assume you are now locked with sentry for auto
# fix and seer"): for error-level issues with no documented QC mapping,
# upgrade the default from log_only → trigger_seer so Seer attempts an
# autofix on every unmapped error. The Seer trigger is a no-op when Seer
# isn't enabled (sentry_seer.SentrySeer.enabled == False) so this stays
# safe in non-Seer projects.
ERROR_LEVEL_DEFAULT = {
    "name": "Unmapped error — Seer triage",
    "action": "trigger_seer",
    "comment": "Unmapped error issue. Asking Seer to diagnose + propose autofix. Add to qc_actions_from_sentry.ACTIONS once remediation pattern is known.",
    "auto_resolve_safe": False,
}


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _git_head_timestamp() -> datetime | None:
    """UTC timestamp of HEAD commit, used by resolve_if_post_fix."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return datetime.fromisoformat(out.stdout.strip().replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def _parse_iso_utc(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _action_lookup(issue: dict) -> tuple[str, dict]:
    """Return (qc_check_tag_or_key, action_spec). Falls back to DEFAULT_ACTION."""
    tags = issue.get("tags") or []
    # Sentry returns tags as a list of {"key": ..., "value": ...} or similar
    qc_check = None
    for t in tags:
        if isinstance(t, dict) and t.get("key") == "qc_check":
            qc_check = t.get("value")
            break
    if not qc_check:
        # Fall back to scanning the issue title for "QC-NNN" or "qc.NNN" patterns
        import re
        title = (issue.get("title") or "") + " " + (issue.get("metadata", {}).get("value") or "")
        m = re.search(r"\b(QC-\d{3}[A-Za-z\.]?)\b", title)
        if m:
            qc_check = m.group(1).upper()
        else:
            m = re.search(r"\b(ingest\.\w+|parser\.\w+|patch_carriers\.\w+)\b", title)
            if m:
                qc_check = m.group(1)
    if qc_check and qc_check in ACTIONS:
        return qc_check, ACTIONS[qc_check]
    # Sentry CRON-MONITOR "missed check-in" issue (HILMAR-DAILY-TRACKER-9).
    # It carries no qc_check tag and no QC-NNN in the title, so without this it
    # falls through to ERROR_LEVEL_DEFAULT and gets sent to Seer every fire —
    # but Seer can't analyze a cron miss (no stack trace), so it never clears.
    # Recognize it by the monitor slug / cron-failure title and route it to
    # resolve_if_post_fix instead (clears once the margin fix is deployed and
    # the misses have stopped; stays open while it's genuinely still missing).
    _ct = ((issue.get("title") or "") + " "
           + (issue.get("metadata", {}).get("value") or "")).lower()
    if ("hilmar-daily-pipeline" in _ct or "cron failure" in _ct
            or "missed check-in" in _ct or "missed checkin" in _ct):
        return "cron.missed_checkin", ACTIONS["cron.missed_checkin"]
    # STALE AUTO-RESOLVE (added 2026-05-28). Per Michael "do all 7-9": close
    # the loop on unresolved errors that have stopped firing — e.g.
    # HILMAR-DAILY-TRACKER-5 (NameError 'os' not defined) hasn't fired since
    # 2026-05-17 because the bug was fixed, but it sat unresolved in Sentry
    # for 11 days because no action routed it to resolve. From now on any
    # unmapped issue silent >= STALE_AUTO_RESOLVE_DAYS auto-resolves with
    # an explanatory comment. Mapped issues stay on their explicit ACTIONS
    # route (so an explicit `flag_for_operator` action wins over auto-stale).
    if not qc_check:
        last_seen = _parse_iso_utc(issue.get("lastSeen") or "")
        if last_seen:
            age_days = (datetime.now(timezone.utc) - last_seen).total_seconds() / 86400.0
            if age_days >= STALE_AUTO_RESOLVE_DAYS:
                return "unmapped-stale", {
                    "name": f"Unmapped issue stale {age_days:.0f}d — auto-resolve",
                    "action": "resolve_if_stale",
                    "comment": (
                        f"No qc_check tag and silent for {age_days:.0f} days "
                        f"(threshold {STALE_AUTO_RESOLVE_DAYS}d). Auto-resolving — "
                        "if this issue truly needs attention it will re-fire on next "
                        "occurrence."
                    ),
                    "auto_resolve_safe": True,
                }
    # Unmapped: pick the right default based on issue level. Errors get
    # Seer triage; warnings/info get log_only.
    level = (issue.get("level") or "").lower()
    if level in ("error", "fatal"):
        return "unmapped-error", ERROR_LEVEL_DEFAULT
    return "unmapped", DEFAULT_ACTION


# ─────────────────────────────────────────────────────────────────────
# Action executors
# ─────────────────────────────────────────────────────────────────────

def _do_log_only(api, issue: dict, spec: dict, *, dry_run: bool) -> dict:
    """Post the spec's comment as a note. Do not resolve."""
    short = issue.get("shortId") or issue.get("id", "")
    if not dry_run:
        try:
            api._request("POST", f"/issues/{issue['id']}/comments/",
                         json={"text": f"[qc_actions_from_sentry] {spec['comment']}"})
        except Exception as e:
            return {"shortId": short, "action": "log_only", "ok": False, "error": str(e)}
    return {"shortId": short, "action": "log_only", "ok": True}


def _do_resolve_if_post_fix(api, issue: dict, spec: dict, *, dry_run: bool) -> dict:
    """Resolve issue if HEAD commit timestamp is newer than issue lastSeen."""
    short = issue.get("shortId") or issue.get("id", "")
    last_seen = _parse_iso_utc(issue.get("lastSeen") or "")
    head_ts = _git_head_timestamp()
    if not last_seen or not head_ts:
        return {"shortId": short, "action": "resolve_if_post_fix", "ok": False,
                "reason": "missing-timestamps"}
    if head_ts <= last_seen:
        # Issue is newer than the fix — leave alone
        return {"shortId": short, "action": "resolve_if_post_fix", "ok": True,
                "reason": "issue-newer-than-fix", "resolved": False}
    if dry_run:
        return {"shortId": short, "action": "resolve_if_post_fix", "ok": True,
                "reason": f"dry-run would resolve (HEAD={head_ts.isoformat()} > lastSeen={last_seen.isoformat()})",
                "resolved": False}
    if spec.get("auto_resolve_safe"):
        ok = api.resolve_issue(issue["id"],
                               reason=f"fixed in HEAD ({head_ts.isoformat()}) — {spec['comment']}")
        return {"shortId": short, "action": "resolve_if_post_fix", "ok": ok, "resolved": ok}
    return {"shortId": short, "action": "resolve_if_post_fix", "ok": True,
            "reason": "not-safe-to-auto-resolve", "resolved": False}


def _do_resolve_if_stale(api, issue: dict, spec: dict, *, dry_run: bool,
                          stale_hours: int = 24) -> dict:
    short = issue.get("shortId") or issue.get("id", "")
    last_seen = _parse_iso_utc(issue.get("lastSeen") or "")
    if not last_seen:
        return {"shortId": short, "action": "resolve_if_stale", "ok": False,
                "reason": "missing-lastSeen"}
    age = datetime.now(timezone.utc) - last_seen
    if age < timedelta(hours=stale_hours):
        return {"shortId": short, "action": "resolve_if_stale", "ok": True,
                "reason": f"not stale yet ({age.total_seconds()/3600:.1f}h)",
                "resolved": False}
    if dry_run:
        return {"shortId": short, "action": "resolve_if_stale", "ok": True,
                "reason": f"dry-run would resolve (age={age.total_seconds()/3600:.1f}h)",
                "resolved": False}
    ok = api.resolve_issue(issue["id"],
                           reason=f"stale — no events for {age.total_seconds()/3600:.1f}h")
    return {"shortId": short, "action": "resolve_if_stale", "ok": ok, "resolved": ok}


def _do_rerun_parser_acc(api, issue: dict, spec: dict, *, dry_run: bool) -> dict:
    """Compute current parser accuracy + comment on the issue with the result."""
    short = issue.get("shortId") or issue.get("id", "")
    try:
        import sys
        src_dir = ROOT / "src"
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
        from hilmar.parser_accuracy import compute_accuracy
        data_path = ROOT / "tracking-data-v2.json"
        if not data_path.exists():
            data_path = ROOT.parent / "tracking-data-v2.json"
        if data_path.exists():
            data = json.loads(data_path.read_text(encoding="utf-8"))
            acc = compute_accuracy(data.get("requests", []))
            msg = (f"Re-computed parser accuracy: overall {acc['overall_rate']:.1%}, "
                   f"weighted {acc['weighted_rate']:.1%}, "
                   f"pass={acc['pass']}, failing={acc['failing_fields']}")
        else:
            msg = "tracking-data-v2.json not found for accuracy recompute."
    except Exception as e:
        msg = f"parser accuracy recompute failed: {e}"
    if not dry_run:
        try:
            api._request("POST", f"/issues/{issue['id']}/comments/",
                         json={"text": f"[qc_actions_from_sentry] {spec['comment']}\n\n{msg}"})
        except Exception as e:
            return {"shortId": short, "action": "rerun_parser_acc", "ok": False, "error": str(e)}
    return {"shortId": short, "action": "rerun_parser_acc", "ok": True, "summary": msg}


def _do_flag_for_operator(api, issue: dict, spec: dict, *, dry_run: bool) -> dict:
    """High-priority comment with no resolve. Operator must take action."""
    short = issue.get("shortId") or issue.get("id", "")
    text = f"⚠️ OPERATOR ATTENTION: {spec['name']}\n\n{spec['comment']}"
    if not dry_run:
        try:
            api._request("POST", f"/issues/{issue['id']}/comments/",
                         json={"text": text})
        except Exception as e:
            return {"shortId": short, "action": "flag_for_operator", "ok": False, "error": str(e)}
    return {"shortId": short, "action": "flag_for_operator", "ok": True}


def _do_trigger_seer(api, issue: dict, spec: dict, *, dry_run: bool) -> dict:
    """Ask Seer to attempt an autofix.

    2026-05-19 PM (Michael "make sure sentry/seer and all backups work
    no joke"): When Seer's autofix can't start (500 "Autofix failed to
    start" — happens when the issue lacks event/stack-trace data),
    automatically chain to Claude (claude_diagnose) so the operator
    still gets a useful AI diagnosis posted as a Sentry comment. Seer
    is preferred when available; Claude is the guaranteed fallback.
    """
    short = issue.get("shortId") or issue.get("id", "")
    try:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        from sentry_seer import SentrySeer
        seer = SentrySeer()
        if not seer.enabled:
            # Skip Seer entirely; go straight to Claude
            return _do_claude_diagnose(api, issue, spec, dry_run=dry_run, _via="seer-disabled")
        if dry_run:
            return {"shortId": short, "action": "trigger_seer", "ok": True,
                    "reason": "dry-run would trigger autofix"}
        trig = seer.trigger_autofix(issue["id"], instruction=spec.get("comment", ""))
        if trig is not None:
            # Seer accepted; queued for analysis
            return {"shortId": short, "action": "trigger_seer", "ok": True, "seer_triggered": True}
        # Seer rejected (500 / 404) — fall through to Claude
        return _do_claude_diagnose(api, issue, spec, dry_run=dry_run, _via="seer-rejected")
    except Exception as e:
        return {"shortId": short, "action": "trigger_seer", "ok": False, "error": str(e)}


def _do_claude_diagnose(api, issue: dict, spec: dict, *, dry_run: bool, _via: str = "direct") -> dict:
    """Use Claude (Anthropic API) to diagnose the issue + post the
    diagnosis as a Sentry comment.

    Independent of Seer — works as long as the Anthropic API key is in
    secrets/anthropic-api-key.txt (or ANTHROPIC_API_KEY env). Acts as
    the guaranteed AI-diagnosis layer; Seer is preferred when its
    autofix succeeds because it can also propose code patches, but
    Claude's diagnosis is always available.
    """
    short = issue.get("shortId") or issue.get("id", "")
    try:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        from pdf_llm_rescue import _load_api_key
        api_key = _load_api_key()
        if not api_key:
            return {"shortId": short, "action": "claude_diagnose", "ok": False,
                    "reason": "no ANTHROPIC_API_KEY"}
        try:
            import anthropic
        except ImportError:
            return {"shortId": short, "action": "claude_diagnose", "ok": False,
                    "reason": "anthropic SDK not installed"}
        if dry_run:
            return {"shortId": short, "action": "claude_diagnose", "ok": True,
                    "reason": f"dry-run would call Claude (via={_via})"}

        # Build a compact context for Claude: title + culprit + level +
        # short metadata. Skip the full event payload to keep tokens low.
        title = issue.get("title") or ""
        culprit = issue.get("culprit") or ""
        level = issue.get("level") or ""
        count = issue.get("count") or "?"
        platform = issue.get("platform") or ""
        prompt = (
            f"You are diagnosing a Sentry issue from the Hilmar daily shipment "
            f"tracker pipeline. In 2-3 sentences: state the most likely root "
            f"cause and the specific code change that would fix it. Be terse.\n\n"
            f"Issue: {title}\n"
            f"Culprit: {culprit}\n"
            f"Level: {level}\n"
            f"Platform: {platform}\n"
            f"Occurrence count: {count}\n"
            f"Project: hilmar-daily-tracker (Python)"
        )
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        diagnosis = resp.content[0].text.strip() if resp.content else ""

        # Post the diagnosis as a Sentry comment so the operator sees it
        # alongside the issue. Tagged so it's clear this is AI-generated.
        comment_text = (
            f"[qc_actions_from_sentry → claude_diagnose, via={_via}]\n\n"
            f"🤖 Claude (haiku-4-5) diagnosis:\n{diagnosis}\n\n"
            f"_Note: this is an AI-generated diagnostic comment, not a "
            f"verified fix. Confirm before applying._\n"
            f"Token usage: in={resp.usage.input_tokens}, out={resp.usage.output_tokens}."
        )
        ok = True
        try:
            api._request("POST", f"/issues/{issue['id']}/comments/",
                         json={"text": comment_text})
        except Exception as e:
            ok = False
            return {"shortId": short, "action": "claude_diagnose", "ok": False,
                    "error": f"comment post failed: {e}"}
        return {"shortId": short, "action": "claude_diagnose", "ok": ok,
                "diagnosis_chars": len(diagnosis), "via": _via,
                "tokens_in": resp.usage.input_tokens,
                "tokens_out": resp.usage.output_tokens}
    except Exception as e:
        return {"shortId": short, "action": "claude_diagnose", "ok": False, "error": str(e)}


_DISPATCH = {
    "log_only":            _do_log_only,
    "resolve_if_post_fix": _do_resolve_if_post_fix,
    "resolve_if_stale":    _do_resolve_if_stale,
    "rerun_parser_acc":    _do_rerun_parser_acc,
    "flag_for_operator":   _do_flag_for_operator,
    "trigger_seer":        _do_trigger_seer,
    "claude_diagnose":     _do_claude_diagnose,
}


# ─────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────

def run(*, dry_run: bool = False, lookback_hours: int = 26,
        limit: int = 50) -> dict:
    """List unresolved issues, dispatch QC actions, write summary report.

    Returns: {
        "issues_scanned": N,
        "actions": [...],            # one per issue
        "resolved": M,                # how many auto-resolved
        "commented": K,               # how many got comments
        "dry_run": bool,
    }
    """
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from sentry_api import SentryAPI
    except ImportError as e:
        log.warning("qc_actions_from_sentry: sentry_api unavailable: %s", e)
        return {"issues_scanned": 0, "actions": [], "resolved": 0, "commented": 0,
                "dry_run": dry_run, "error": "sentry_api unavailable"}

    api = SentryAPI()
    if not api.enabled:
        log.info("qc_actions_from_sentry: Sentry auth not configured — no-op")
        return {"issues_scanned": 0, "actions": [], "resolved": 0, "commented": 0,
                "dry_run": dry_run, "error": "sentry not configured"}

    # Sentry's stats_period only accepts '', '24h', or '14d' (verified
    # 2026-05-19 PM health check when '26h' returned HTTP 400 "Invalid
    # stats_period"). Clamp the configured lookback to the closest
    # supported window.
    if lookback_hours <= 0:
        period_str = ""
    elif lookback_hours <= 24:
        period_str = "24h"
    else:
        period_str = "14d"
    issues = api.list_issues(stats_period=period_str,
                              query="is:unresolved",
                              limit=limit)

    actions_out: list[dict] = []
    resolved = 0
    commented = 0
    for issue in issues:
        key, spec = _action_lookup(issue)
        action_type = spec.get("action", "log_only")
        executor = _DISPATCH.get(action_type, _do_log_only)
        result = executor(api, issue, spec, dry_run=dry_run)
        result["qc_check"] = key
        result["title"] = (issue.get("title") or "")[:120]
        actions_out.append(result)
        if result.get("resolved"):
            resolved += 1
        if result.get("ok") and action_type != "log_only" or action_type == "log_only" and result.get("ok"):
            commented += 1

    summary = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "issues_scanned": len(issues),
        "actions": actions_out,
        "resolved": resolved,
        "commented": commented,
        "dry_run": dry_run,
        "lookback_hours": lookback_hours,
    }
    # Write the run report for the daily audit
    try:
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / "qc-actions-from-sentry.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("qc_actions_from_sentry: report write failed: %s", e)
    log.info("qc_actions_from_sentry: %d issues, %d commented, %d resolved (dry_run=%s)",
             len(issues), commented, resolved, dry_run)
    return summary


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry = "--apply" not in sys.argv
    lookback = int(os.environ.get("HILMAR_QC_ACTIONS_LOOKBACK_H", "26"))
    result = run(dry_run=dry, lookback_hours=lookback)
    print(f"Scanned {result['issues_scanned']} unresolved Sentry issues "
          f"({lookback}h lookback)")
    print(f"  Commented: {result['commented']}")
    print(f"  Resolved:  {result['resolved']}")
    if dry:
        print("  (DRY-RUN — pass --apply to actually post comments + resolve)")
    for a in result["actions"][:20]:
        ok = "OK " if a.get("ok") else "ERR"
        print(f"  {ok} {a.get('shortId','?'):12s} {a.get('qc_check','?'):20s} "
              f"{a.get('action','?'):20s} {a.get('title','')[:60]}")
