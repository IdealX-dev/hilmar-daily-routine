"""Tests for the heartbeat-via-wrapper liveness wiring (2026-06-01).

Workflows and .cmd files don't have unit test runners, but several
invariants can break silently:

  - The wrapper stops dispatching the heartbeat (someone edits
    run_daily_laptop.cmd and removes the `gh workflow run` block).
    Liveness would fire daily false-positives.
  - The liveness workflow reverts to checking daily-fire.yml (which
    isn't deployed). Same failure as 2026-06-01.
  - The label-creation step is removed from liveness.yml. First-run
    issue-creation fails (the bug we just fixed).
  - heartbeat.yml's workflow_dispatch inputs change shape and the
    wrapper's gh-CLI call breaks.

These tests lock all four by reading the YAML + .cmd at module level
and asserting structural properties. Cheap, deterministic, no GH API.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
WRAPPER = ROOT / "deploy" / "run_daily_laptop.cmd"


# ── heartbeat.yml shape ──────────────────────────────────────────────────

def test_heartbeat_workflow_exists():
    assert (WORKFLOWS / "heartbeat.yml").exists(), (
        "heartbeat.yml is the entire mechanism — must exist"
    )


def test_heartbeat_workflow_is_dispatch_only():
    """The heartbeat is triggered by the Cloud PC wrapper, NOT by a
    cron. A schedule trigger would record fake-positive heartbeats
    every day even if the Cloud PC never actually fired."""
    spec = yaml.safe_load((WORKFLOWS / "heartbeat.yml").read_text())
    # PyYAML parses `on:` as the Python boolean True (YAML 1.1 spec).
    triggers = spec.get(True) or spec.get("on")
    assert triggers is not None, "heartbeat.yml has no 'on' block"
    assert "workflow_dispatch" in triggers, (
        "heartbeat must be dispatch-triggered (Cloud PC fires the dispatch)"
    )
    assert "schedule" not in triggers, (
        "heartbeat MUST NOT be scheduled — that would record fake "
        "heartbeats whether or not the Cloud PC actually fired"
    )


def test_heartbeat_inputs_match_wrapper_call():
    """The wrapper passes -f at, -f sha, -f status, -f host. The
    workflow must accept all four."""
    spec = yaml.safe_load((WORKFLOWS / "heartbeat.yml").read_text())
    triggers = spec.get(True) or spec.get("on")
    inputs = triggers["workflow_dispatch"]["inputs"]
    for key in ("at", "sha", "status", "host"):
        assert key in inputs, (
            f"heartbeat.yml workflow_dispatch is missing input '{key}' "
            f"that run_daily_laptop.cmd's gh workflow run passes."
        )


# ── liveness.yml uses the new heartbeat source ───────────────────────────

def test_liveness_reads_heartbeat_not_daily_fire():
    """The 2026-06-01 redesign: liveness reads heartbeat.yml runs. The
    daily-fire.yml workflow exists (planned self-hosted-runner trigger)
    but isn't deployed — checking it produced the false-positive issue
    creation that caused this PR."""
    text = (WORKFLOWS / "liveness.yml").read_text()
    assert "gh run list --workflow=heartbeat.yml" in text, (
        "liveness.yml must check heartbeat.yml runs (the wrapper-driven "
        "heartbeat), not daily-fire.yml (the unbuilt self-hosted runner)."
    )
    assert "gh run list --workflow=daily-fire.yml" not in text, (
        "liveness.yml still references daily-fire.yml — revert this "
        "and use heartbeat.yml (see 2026-06-01 PR for context)."
    )


def test_liveness_creates_cloud_pc_down_label_idempotently():
    """First-run failure on 2026-06-01: gh issue create errored because
    the `cloud-pc-down` label didn't exist. Fix: liveness ensures the
    label exists before any issue-create step."""
    text = (WORKFLOWS / "liveness.yml").read_text()
    assert "Ensure cloud-pc-down label exists" in text, (
        "Missing the idempotent label-creation step in liveness.yml. "
        "First run after deploy will fail on `gh issue create --label "
        "cloud-pc-down` if the label isn't present."
    )
    assert "gh label create cloud-pc-down" in text


def test_liveness_runs_after_each_fire():
    """Two fire times (2026-07-16): Mon-Thu ~8 AM ET, Fri ~4:30 PM ET, no
    weekend. Backstops must land after each — Mon-Thu ~9:30 AM EDT (13:30 UTC),
    Friday ~7:30 PM EDT (23:30 UTC) — and be weekday-scoped (no weekend fire)."""
    spec = yaml.safe_load((WORKFLOWS / "liveness.yml").read_text())
    triggers = spec.get(True) or spec.get("on")
    crons = [s.get("cron", "") for s in triggers["schedule"]]
    assert "30 13 * * 1-4" in crons, (
        "liveness must fire the ~9:30 AM ET Mon-Thu tick (after the 8 AM fire)"
    )
    assert "30 23 * * 5" in crons, (
        "liveness must fire the ~7:30 PM ET Friday tick (after the 4:30 PM fire)"
    )
    # Every tick restricts to weekdays — Mon-Thu (1-4) or Friday (5). No
    # weekend backstop, because there is no weekend fire.
    assert crons, "liveness has no scheduled crons"
    assert all(c.endswith("* 1-4") or c.endswith("* 5") for c in crons), (
        f"every liveness cron must be Mon-Thu or Friday; got {crons}"
    )


# ── wrapper dispatches the heartbeat ─────────────────────────────────────

def test_wrapper_calls_gh_workflow_run_heartbeat():
    text = WRAPPER.read_text()
    assert "gh workflow run heartbeat.yml" in text, (
        "deploy/run_daily_laptop.cmd is missing the heartbeat dispatch. "
        "The liveness monitor will fire false-positive `cloud-pc-down` "
        "issues every weekday after merge if this is removed."
    )


def test_wrapper_heartbeat_passes_all_inputs():
    """The wrapper must pass at + sha + status + host so the heartbeat
    workflow's run log ties back to a specific commit + host."""
    text = WRAPPER.read_text()
    for flag in ('-f at=', '-f sha=', '-f status=', '-f host='):
        assert flag in text, (
            f"wrapper's gh workflow run call is missing {flag!r} flag "
            f"that heartbeat.yml expects."
        )


def test_wrapper_heartbeat_is_best_effort():
    """If gh CLI isn't installed (or auth expired) the wrapper MUST log
    + continue — the daily email already sent. Killing the wrapper here
    would break gen_improvements + the audit email."""
    text = WRAPPER.read_text()
    assert "where gh >nul 2>&1" in text, (
        "wrapper must check for gh CLI presence before invoking — a "
        "missing gh would otherwise produce a noisy error in the run log"
    )
    assert "heartbeat skipped" in text, (
        "wrapper must log a clear skip message when gh isn't available "
        "so the operator can diagnose"
    )


def test_wrapper_heartbeat_runs_after_email_sends():
    """Critical ordering: heartbeat dispatches AFTER outlook_send so the
    "fired" signal only goes out when the client deliverable went out.
    A heartbeat before email = false positive if email then fails."""
    text = WRAPPER.read_text()
    email_idx = text.find("outlook_send.py daily")
    heartbeat_idx = text.find("gh workflow run heartbeat.yml")
    assert email_idx > 0 and heartbeat_idx > 0
    assert heartbeat_idx > email_idx, (
        "Heartbeat dispatch must come AFTER outlook_send so the signal "
        "represents an actual client-deliverable completion. "
        f"Currently: email at char {email_idx}, heartbeat at char "
        f"{heartbeat_idx}."
    )


# ── prove-or-scream survives a pipeline failure (audit finding [2]) ───────

def test_wrapper_does_not_exit_before_integrity_gate_on_pipeline_failure():
    """The DOMINANT failure mode — a client-blocking pipeline step failing —
    must still reach the integrity gate so the out-of-band alarm + heartbeat
    fire. The old `exit /b %RC%` terminated first, going silent until liveness's
    25h staleness check. Lock that the failure branch jumps to the gate and that
    no unconditional `exit /b %RC%` sits between the rc capture and the gate."""
    text = WRAPPER.read_text()
    rc_idx = text.find("set RC=%ERRORLEVEL%")
    gate_idx = text.find("assert_fire_integrity.py --pipeline-rc")
    assert rc_idx > 0 and gate_idx > rc_idx, "integrity gate must follow rc capture"
    between = text[rc_idx:gate_idx]
    # Inspect only EXECUTABLE lines — a REM that mentions the old `exit /b %RC%`
    # for context must not count.
    exec_lines = [ln.strip() for ln in between.splitlines()
                  if not ln.strip().upper().startswith("REM")]
    assert not any("exit /b %RC%" in ln for ln in exec_lines), (
        "wrapper must NOT exit /b %RC% before the integrity gate — that bypasses "
        "the prove-or-scream alarm on the most common failure mode"
    )
    assert "goto integrity" in between, (
        "the pipeline-failure branch must jump to the :integrity gate"
    )
    assert "\n:integrity\n" in text, "the :integrity label must exist"


def test_integrity_gate_runs_before_heartbeat_dispatch():
    """The heartbeat status must reflect the integrity outcome, so the gate
    has to run before the dispatch (true on both the success and failure path)."""
    text = WRAPPER.read_text()
    gate_idx = text.find("assert_fire_integrity.py --pipeline-rc")
    hb_idx = text.find("gh workflow run heartbeat.yml")
    assert 0 < gate_idx < hb_idx


# ── heartbeat timestamp is ISO-8601 UTC, not locale (audit finding [34]) ──

def test_heartbeat_timestamp_is_iso_utc_not_locale():
    """%DATE%T%TIME% is locale-formatted (spaces/slashes/weekday) and diverges
    from daily.yml's ISO-8601 heartbeat into the SAME heartbeat.yml input. The
    wrapper must derive HB_AT from a real UTC ISO timestamp."""
    text = WRAPPER.read_text()
    # The locale form must not be the value actually sent: HB_AT is recomputed
    # from a Python-produced ISO file, with the locale string only as fallback.
    assert "hb-at.txt" in text, "HB_AT must come from the computed ISO timestamp file"
    assert 'isoformat().replace(' in text and "'+00:00','Z'" in text, (
        "HB_AT must be a UTC ISO-8601 timestamp (matching daily.yml)"
    )


# ── docs ────────────────────────────────────────────────────────────────

def test_setup_doc_present():
    """The wrapper + liveness workflow both reference docs/CLOUD-PC-
    HEARTBEAT-SETUP.md. It must exist."""
    doc = ROOT / "docs" / "CLOUD-PC-HEARTBEAT-SETUP.md"
    assert doc.exists()
    text = doc.read_text()
    assert "gh auth login" in text, "setup doc must cover authentication"
    assert "gh workflow run heartbeat.yml" in text, (
        "setup doc must include the smoke-test command"
    )
