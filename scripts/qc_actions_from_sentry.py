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
from typing import Optional

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
    "QC-038": {
        "name": "Reconcile drift with ol-quote-tracker",
        "action": "flag_for_operator",
        "comment": "Reconcile drift detected. Compare reports/reconcile-quote-tracker.json to find the mismatched booking.",
        "auto_resolve_safe": False,
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
    "ingest.non_hilmar_filtered": {
        "name": "Non-HILMAR row filtered",
        "action": "log_only",
        "comment": "Correct rejection of NUMIDIA-only rows per the 2026-05-17 fix. Suppress this in Sentry filters if too noisy.",
        "auto_resolve_safe": True,
    },
}


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

def _git_head_timestamp() -> Optional[datetime]:
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


def _parse_iso_utc(s: str) -> Optional[datetime]:
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
    """Ask Seer to attempt an autofix."""
    short = issue.get("shortId") or issue.get("id", "")
    try:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        from sentry_seer import SentrySeer
        seer = SentrySeer()
        if not seer.enabled:
            return {"shortId": short, "action": "trigger_seer", "ok": False,
                    "reason": "Seer not enabled"}
        if dry_run:
            return {"shortId": short, "action": "trigger_seer", "ok": True,
                    "reason": "dry-run would trigger autofix"}
        trig = seer.trigger_autofix(issue["id"], instruction=spec.get("comment", ""))
        return {"shortId": short, "action": "trigger_seer", "ok": trig is not None}
    except Exception as e:
        return {"shortId": short, "action": "trigger_seer", "ok": False, "error": str(e)}


_DISPATCH = {
    "log_only":            _do_log_only,
    "resolve_if_post_fix": _do_resolve_if_post_fix,
    "resolve_if_stale":    _do_resolve_if_stale,
    "rerun_parser_acc":    _do_rerun_parser_acc,
    "flag_for_operator":   _do_flag_for_operator,
    "trigger_seer":        _do_trigger_seer,
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

    period_str = f"{lookback_hours}h"
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
        if result.get("ok") and action_type != "log_only":
            commented += 1
        elif action_type == "log_only" and result.get("ok"):
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
