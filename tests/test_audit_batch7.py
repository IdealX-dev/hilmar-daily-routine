"""Audit batch 7 — the gate that graded the data before the heals fixed it,
and the alarm that could not reach anyone.

TWO DEFECTS, ONE DAY. On 2026-07-27 the daily report did not go out. Both
causes are here, and both are the system failing to do the ONE thing it exists
to do: tell the truth about itself.

  [A] QC-039 MEASURED TOO EARLY. The parser-accuracy gate ran ~880 lines
      before QC-056's carrier backfill, inside the SAME phase_6_rules call.
      It graded the rows the heals exist to repair. Live proof from that
      fire's log:
          QC-039: carrier_quoted = 291/313 (93.0%)  -> BLOCKED the client ship
          QC-056: backfilled carrier from row text — 10 rows
      291 + 10 = 301/313 = 96.2%, comfortably over the 95% gate. A whole
      business day's report was withheld from the client distribution because
      a gate was measured before the fixes it was measuring landed.

      THIRD INSTANCE OF THIS SHAPE. Batch-5 #15 persisted aggregates before
      the heals ran; the QC-075 stale-summary bug false-fired for the same
      reason. The rule is now explicit and tested: a gate measures the FINAL
      state of the rows, after every mutating heal in its phase.

  [B] THE ALARM COULD NOT REACH ANYONE. When the fire failed it raised a
      FIRE-ALERT, and that alert returned {'github': False, 'teams': False}:
      daily.yml gave the pipeline and integrity-gate steps no GH_TOKEN and the
      job no `issues: write`, and no Teams webhook is configured. So the alarm
      existed only as a stderr banner inside a failed job's log and a queue
      file on an ephemeral runner that was then destroyed. Nobody was told —
      Michael found out because the report never arrived.
"""
from __future__ import annotations

import ast
import contextlib
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import core  # noqa: E402
import fire_alert as FA  # noqa: E402
import qc_selfheal as QC  # noqa: E402


def _quoted_row_missing_carrier(rid="q1", vessel="MAERSK SEALAND 123"):
    """A quoted row with a rate but no carrier — exactly QC-056's target, and
    exactly a miss in QC-039's carrier_quoted denominator."""
    now = core.now_utc()
    return {
        "request_id": rid, "status": "LOSS", "loss_reason": "PRICE",
        "quoted": True, "ol_rate": 2400.0, "carrier_quoted": None,
        "origin": "Oakland", "destination": "Busan",
        "lane": "Oakland → Busan", "teu_requested": 4, "container_count": 2,
        "vessel_voyage": vessel,
        "request_date": core.et_date_of(now),
        "request_timestamp": now.isoformat(),
        "source_imids": [f"<{rid}@ol>"], "status_history": [],
    }


def _run_phase6(rows):
    data = {"requests": rows, "summary": {}, "lane_summary": {},
            "carrier_summary": {}}
    log = QC.Log()
    with contextlib.redirect_stdout(io.StringIO()):
        QC.phase_5_summaries(log, data)
        QC.phase_6_rules(log, data)
    return log, data


# ── [A] the gate must grade the FINAL state ─────────────────────────────────

def test_the_carrier_heal_runs_before_the_accuracy_gate_measures():
    """THE FIX, structurally. Source order inside phase_6_rules: QC-056's
    backfill must precede compute_accuracy, or the gate grades pre-heal rows
    and blocks a report the heals had already made shippable."""
    src = (SCRIPTS / "qc_selfheal.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "phase_6_rules")
    seg = ast.get_source_segment(src, fn) or ""
    heal = seg.find('r["carrier_quoted"] = _car')
    gate = seg.find("compute_accuracy(")
    assert heal != -1, "QC-056's carrier backfill is gone"
    assert gate != -1, "QC-039's measurement is gone"
    assert heal < gate, (
        "QC-039 measures BEFORE QC-056 heals — the gate grades the rows the "
        "heals exist to repair, and will block a shippable report again")


def test_a_healable_row_does_not_trip_the_accuracy_gate():
    """Behavioural counterpart. A row QC-056 CAN fix must be counted as fixed
    by the time the gate looks — this is the 2026-07-27 shape."""
    row = _quoted_row_missing_carrier()
    log, _ = _run_phase6([row])
    assert row.get("carrier_quoted"), "QC-056 did not heal — test is inert"
    carrier_gate_errors = [e for e in log.errors
                           if "QC-039" in e and "carrier_quoted" in e]
    assert not carrier_gate_errors, (
        f"the gate still failed on a row that was healed this same run: "
        f"{carrier_gate_errors}")


def test_the_gate_still_blocks_a_row_that_cannot_be_healed():
    """Teeth retained. Reordering must not turn the gate into a rubber stamp:
    a row with no carrier anywhere in its text stays a miss."""
    row = _quoted_row_missing_carrier(vessel="TBD")
    log, _ = _run_phase6([row])
    assert not row.get("carrier_quoted"), "the row was unexpectedly healable"
    assert any("QC-039" in e for e in log.errors), (
        "an unhealable missing carrier no longer trips the gate — the gate "
        "has become a rubber stamp")


def test_every_mutating_heal_in_phase_6_precedes_the_gate():
    """The general rule, not just the one instance. ANY write to a field the
    accuracy gate grades must land before the gate measures."""
    src = (SCRIPTS / "qc_selfheal.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "phase_6_rules")
    seg = ast.get_source_segment(src, fn) or ""
    gate = seg.find("compute_accuracy(")
    graded = ("carrier_quoted", "carrier_won", "ol_rate")
    late = []
    for field in graded:
        needle = f'["{field}"] = '
        pos = seg.find(needle)
        while pos != -1:
            if pos > gate:
                late.append((field, pos))
                break
            pos = seg.find(needle, pos + 1)
    assert not late, (
        f"these gate-graded fields are written AFTER the gate measures: "
        f"{late} — the gate is grading a state that is about to change")


# ── [B] an alarm that cannot deliver must not be silent ─────────────────────

def test_an_alert_with_no_remote_channel_is_undeliverable():
    """stderr and queue are local. On an ephemeral runner both die with the
    container, so neither counts as having told anyone."""
    assert FA.undeliverable({"stderr": True, "queue": True,
                             "github": False, "teams": False}) is True


@pytest.mark.parametrize("results", [
    {"stderr": True, "queue": True, "github": True, "teams": False},
    {"stderr": False, "queue": False, "github": False, "teams": True},
    {"stderr": True, "queue": True, "github": True, "teams": True},
])
def test_an_alert_that_reached_a_remote_channel_is_deliverable(results):
    assert FA.undeliverable(results) is False


def test_the_undeliverable_case_says_so_loudly(capsys):
    """The 2026-07-27 failure was SILENT: send_alert returned
    {'github': False, 'teams': False} and the caller just printed the dict.
    An alarm that cannot deliver is a silent failure of the one thing whose
    job is not being silent."""
    FA._warn_if_undeliverable({"stderr": True, "queue": True,
                               "github": False, "teams": False})
    err = capsys.readouterr().err
    assert "ALERT UNDELIVERABLE" in err
    assert "NOBODY WILL BE TOLD" in err
    assert "QC-076" in err, "the message must point at the check that prevents it"


def test_a_delivered_alert_stays_quiet(capsys):
    FA._warn_if_undeliverable({"stderr": True, "queue": True,
                               "github": True, "teams": False})
    assert "ALERT UNDELIVERABLE" not in capsys.readouterr().err


# ── [B] the workflow must actually grant the alarm its credential ───────────

def _fire_job() -> dict:
    """The production-fire job, parsed as YAML rather than sliced as text —
    a string slice silently returned '' and made these assertions vacuous."""
    import yaml
    doc = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8"))
    job = doc["jobs"]["production-fire"]
    assert isinstance(job, dict) and job, "production-fire job not found"
    return job


def test_the_production_fire_job_can_file_an_issue():
    """fire_alert's GitHub channel needs BOTH `issues: write` and a token.
    It had neither, which is why the alarm returned github=False."""
    job = _fire_job()
    perms = job.get("permissions") or {}
    assert perms.get("issues") == "write", (
        f"the production-fire job cannot file a FIRE-ALERT issue "
        f"(permissions={perms}) — the alarm will silently fail again")


def test_the_alarm_credential_is_job_wide_not_one_step():
    """The pipeline step AND the integrity-gate step both raise FIRE-ALERTs.
    Granting the token to only one of them recreates the gap on the other, so
    it must live in the JOB-level env."""
    env = _fire_job().get("env") or {}
    assert "GH_TOKEN" in env, (
        f"no job-level GH_TOKEN (env keys: {sorted(env)}) — fire_alert's gh "
        f"CLI and REST fallbacks both fail without it, so any step that "
        f"raises an alert has no channel")


def test_both_alert_raising_steps_inherit_the_credential():
    """Named explicitly: these are the two steps that called send_alert on
    2026-07-27 and got {'github': False}."""
    job = _fire_job()
    env = job.get("env") or {}
    raisers = [s for s in job.get("steps", [])
               if any(k in str(s.get("run", ""))
                      for k in ("run_pipeline.py", "assert_fire_integrity.py"))]
    assert len(raisers) >= 2, f"expected the pipeline + integrity steps, got {len(raisers)}"
    for s in raisers:
        step_env = s.get("env") or {}
        assert "GH_TOKEN" in env or "GH_TOKEN" in step_env, (
            f"step {s.get('name')!r} can raise a FIRE-ALERT with no token")


# ── QC-076: the alarm is checked while everything is fine ───────────────────

def _qc076_lines(log):
    return [m for bucket in (log.ok_msgs if hasattr(log, "ok_msgs") else [],
                             log.warnings, log.errors)
            for m in bucket if "QC-076" in m]


def test_qc076_errors_on_a_runner_with_no_alert_channel(monkeypatch):
    """THE 2026-07-27 CONFIGURATION. Unattended, no token, no webhook — a
    failing fire could not tell anyone, and nothing said so."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(QC, "_fire_alert_teams_configured", lambda: False)
    log, _ = _run_phase6([_quoted_row_missing_carrier()])
    hits = [e for e in log.errors if "QC-076" in e]
    assert hits, "a runner with no alert channel did not raise QC-076"
    assert "reach NOBODY" in hits[0]


def test_qc076_passes_when_the_github_channel_is_wired(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GH_TOKEN", "ghs_dummy")
    monkeypatch.setattr(QC, "_fire_alert_teams_configured", lambda: False)
    log, _ = _run_phase6([_quoted_row_missing_carrier()])
    assert not [e for e in log.errors if "QC-076" in e]


def test_qc076_passes_when_only_teams_is_wired(monkeypatch):
    """Either remote channel is enough — they are alternatives, not both."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(QC, "_fire_alert_teams_configured", lambda: True)
    log, _ = _run_phase6([_quoted_row_missing_carrier()])
    assert not [e for e in log.errors if "QC-076" in e]


def test_qc076_does_not_false_fire_on_an_attended_run(monkeypatch):
    """On a dev box or an interactive run, stderr IS a human-visible channel.
    Erroring there would train the operator to ignore QC-076."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(QC, "_fire_alert_teams_configured", lambda: False)
    log, _ = _run_phase6([_quoted_row_missing_carrier()])
    assert not [e for e in log.errors if "QC-076" in e]


def test_qc076_shares_one_definition_of_teams_configured():
    """The helper delegates to fire_alert rather than re-implementing the
    secret-resolution order — a second copy would drift from the thing it is
    supposed to be checking."""
    src = (SCRIPTS / "qc_selfheal.py").read_text(encoding="utf-8")
    fn = src.split("def _fire_alert_teams_configured")[1].split("\ndef ")[0]
    assert "_teams_webhook_url()" in fn, (
        "QC-076 no longer delegates to fire_alert's own resolver")
