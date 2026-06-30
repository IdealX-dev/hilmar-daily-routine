"""Tests for run_pipeline.py BEST_EFFORT_STEPS classification (Layer 2).

The 2026-06-01 TTSWW failure exposed the structural bug: a single
downstream telemetry step (`Sync to ol-quote-tracker`) returning rc=1
aborted the whole wrapper, killing the client email + audit email +
backup chain. Layer 2 fixes that by classifying every step as either:

  - CLIENT_BLOCKING — its failure means the email would be stale or
    broken; abort the pipeline so outlook_send is skipped.
  - BEST_EFFORT — failure has no impact on the client deliverable;
    log a warning, write to QC, continue.

These tests lock that classification (the set of names) and the
runtime behaviour (best-effort failures → exit 0; client-blocking
failures → exit 1; mixed → exit 1 with both kinds reported).
"""
from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("rp_under_test", SCRIPTS / "run_pipeline.py")
RP = importlib.util.module_from_spec(spec)
sys.modules["rp_under_test"] = RP
spec.loader.exec_module(RP)


# ── Classification membership ───────────────────────────────────────────

def test_best_effort_steps_set_exists_and_is_set_typed():
    """The classification mechanism itself must exist."""
    assert hasattr(RP, "BEST_EFFORT_STEPS")
    assert isinstance(RP.BEST_EFFORT_STEPS, set)
    assert len(RP.BEST_EFFORT_STEPS) > 0


def test_sync_to_quote_tracker_is_best_effort():
    """The 2026-06-01 failure that drove this change."""
    assert "Sync to ol-quote-tracker" in RP.BEST_EFFORT_STEPS


def test_known_best_effort_steps_classified():
    """Every step that is downstream-only telemetry/housekeeping is
    classified. If a new such step is added without classification,
    the next TTSWW-class failure will recur."""
    expected_best_effort = {
        "Sentry-driven QC actions",
        "Sentry Seer autofix trigger",
        "Carrier scorecard PDFs",
        "Share to client_intelligence",
        "Rate intelligence",
        "Sync to ol-quote-tracker",
        # Code-health self-audit — a slow run / timeout must never block the
        # client report (the 2026-06-30 GitHub production-fire abort).
        "Test + coverage routine",
    }
    assert expected_best_effort.issubset(RP.BEST_EFFORT_STEPS), (
        f"Missing best-effort classification for: "
        f"{expected_best_effort - RP.BEST_EFFORT_STEPS}"
    )


def test_test_routine_timeout_does_not_block_the_fire(monkeypatch, capsys):
    """The exact 2026-06-30 GitHub-fire regression: the 'Test + coverage
    routine' step TIMED OUT (rc=124, the process is KILLED) and was wrongly
    treated as client-blocking, aborting the fire before the email/PDF were
    built. It is a self-audit, not a deliverable, so a timeout there must exit 0
    (the client report still ships)."""
    assert "Test + coverage routine" in RP.BEST_EFFORT_STEPS

    def fake_run_step(name, cmd, dry_run=False, extra_env=None):
        # 124 = the GNU-timeout convention run_step returns on a killed step.
        return 124 if name == "Test + coverage routine" else 0

    monkeypatch.setattr(RP, "run_step", fake_run_step)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(RP, "_sentry", None)
    try:
        rc = RP.main()
        rc = 0 if rc is None else rc
    except SystemExit as e:
        rc = e.code if e.code is not None else 0
    out = capsys.readouterr().out
    assert rc == 0, "a test-routine TIMEOUT must not abort the client fire"
    assert "PIPELINE COMPLETE" in out
    assert "Client-blocking failures" not in out


def test_client_blocking_steps_NOT_in_best_effort():
    """The load-bearing client deliverable steps MUST stay client-
    blocking. If any of these gets accidentally added to
    BEST_EFFORT_STEPS, the wrapper would proceed to send a broken email."""
    client_blocking = {
        "Backup snapshot",
        "Ingest (stage → requests)",
        "Drift check (pre-QC)",
        "Carrier enrichment patch",
        "Dashboard HTML",
        "Client PDF (6-page)",
        "Email body HTML",
    }
    overlap = client_blocking & RP.BEST_EFFORT_STEPS
    assert not overlap, (
        f"Critical-path steps wrongly classified as best-effort: {overlap}. "
        f"These produce or directly enable the daily email; their failure "
        f"MUST abort the pipeline so a broken email is never sent."
    )


def test_every_classified_step_appears_in_STEPS():
    """A name in BEST_EFFORT_STEPS that doesn't match any actual STEP
    is dead weight — the classification has no effect. Catches typos."""
    step_names = {s[0] for s in RP.STEPS}
    misclassified = RP.BEST_EFFORT_STEPS - step_names
    assert not misclassified, (
        f"BEST_EFFORT_STEPS names that don't appear in STEPS (typos?): "
        f"{misclassified}"
    )


# ── Runtime behaviour — simulate failures, check exit code ──────────────
#
# Each test patches `run_step` to return a non-zero exit code (failure)
# for selected steps, runs main(), and asserts the pipeline-level exit code.

class _FakeArgs:
    skip_ingest = False
    dry_run = True   # dry_run flips most subprocess calls to no-op

def _run_main_with_step_failures(monkeypatch, failing_step_names):
    """Run RP.main() under a patched run_step that fails the named steps.
    Returns the SystemExit code (None / 0 if no exit was called)."""
    def fake_run_step(name, cmd, dry_run=False, extra_env=None):
        # run_step's contract is an int exit code: 0 == success, non-zero == failure.
        return 1 if name in failing_step_names else 0
    monkeypatch.setattr(RP, "run_step", fake_run_step)
    # Prevent argparse from reading real argv
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    # Disable the Sentry path so we don't try to send real telemetry
    monkeypatch.setattr(RP, "_sentry", None)
    try:
        RP.main()
        return 0
    except SystemExit as e:
        return e.code if e.code is not None else 0


def test_best_effort_step_failure_exits_0(monkeypatch, capsys):
    """The exact regression from 2026-06-01: Sync to ol-quote-tracker
    fails → pipeline must still complete with rc=0 so outlook_send runs."""
    rc = _run_main_with_step_failures(monkeypatch, {"Sync to ol-quote-tracker"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "PIPELINE COMPLETE" in out
    assert "best-effort warning" in out.lower()
    assert "Sync to ol-quote-tracker" in out


def test_multiple_best_effort_failures_still_exit_0(monkeypatch, capsys):
    """If 3 best-effort steps fail in the same run, still exit 0."""
    rc = _run_main_with_step_failures(monkeypatch, {
        "Sync to ol-quote-tracker",
        "Share to client_intelligence",
        "Rate intelligence",
    })
    assert rc == 0
    out = capsys.readouterr().out
    assert "PIPELINE COMPLETE" in out
    assert "3 best-effort warnings" in out


def test_client_blocking_step_failure_exits_1(monkeypatch, capsys):
    """Conversely: a client-blocking step failing MUST abort."""
    rc = _run_main_with_step_failures(monkeypatch, {"Email body HTML"})
    assert rc == 1
    out = capsys.readouterr().out
    assert "PIPELINE FAILED" in out
    assert "Email body HTML" in out


def test_mixed_failures_exit_1_with_both_reported(monkeypatch, capsys):
    """Mixed: one best-effort + one client-blocking → still exit 1, but
    both kinds reported so the operator sees the full picture.

    Order matters in this test: the best-effort step must appear BEFORE
    the client-blocking one in STEPS, so the pipeline reaches it before
    the client-blocking failure breaks the loop. Sentry-driven QC actions
    is step 8 in STEPS; Dashboard HTML is step 10."""
    rc = _run_main_with_step_failures(monkeypatch, {
        "Sentry-driven QC actions",          # best-effort (step 8)
        "Dashboard HTML",                     # client-blocking (step 10)
    })
    assert rc == 1
    out = capsys.readouterr().out
    assert "PIPELINE FAILED" in out
    assert "Dashboard HTML" in out
    assert "Sentry-driven QC actions" in out
    assert "Client-blocking failures" in out
    assert "Also (best-effort)" in out


def test_no_failures_exits_0_with_complete(monkeypatch, capsys):
    """Sanity: happy path still exits 0 and prints PIPELINE COMPLETE."""
    rc = _run_main_with_step_failures(monkeypatch, set())
    assert rc == 0
    out = capsys.readouterr().out
    assert "PIPELINE COMPLETE" in out
    assert "best-effort" not in out.lower()  # no warning shown when none failed


def test_best_effort_failure_does_not_stop_subsequent_steps(monkeypatch, capsys):
    """If a best-effort step fails MID-pipeline, the steps AFTER it must
    still run. Pre-Layer-2, the `break` killed the rest of the loop."""
    executed = []
    def fake_run_step(name, cmd, dry_run=False, extra_env=None):
        executed.append(name)
        return 1 if name == "Sentry-driven QC actions" else 0  # fail one best-effort early
    monkeypatch.setattr(RP, "run_step", fake_run_step)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(RP, "_sentry", None)
    with contextlib.suppress(SystemExit):
        RP.main()
    # Email body HTML is after Sentry-driven QC actions in STEPS; it must
    # have run despite the earlier failure.
    assert "Email body HTML" in executed, (
        f"After best-effort failure, downstream client-blocking step "
        f"did not run. Executed: {executed}"
    )


def test_client_blocking_failure_stops_subsequent_steps(monkeypatch):
    """Conversely: a client-blocking failure correctly breaks the loop.
    The wrapper will see rc=1 and skip outlook_send."""
    executed = []
    def fake_run_step(name, cmd, dry_run=False, extra_env=None):
        executed.append(name)
        return 1 if name == "Carrier enrichment patch" else 0  # fail early in pipeline
    monkeypatch.setattr(RP, "run_step", fake_run_step)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(RP, "_sentry", None)
    with contextlib.suppress(SystemExit):
        RP.main()
    assert "Email body HTML" not in executed, (
        f"After client-blocking failure, downstream steps STILL ran. "
        f"That would let a broken email reach the distribution. "
        f"Executed: {executed}"
    )
