"""The fire's transcript: run_pipeline must write reports/run-log.txt on the
host that actually fires, and a killed step's output must survive.

WHY THIS EXISTS — measured on production fire 33864188808 (2026-09-04):

    ##[warning]No files were found with the provided path: reports/run-log.txt
    reports/qc-result.json reports/test-result.json reports/coverage.json.
    No artifacts will be uploaded.

The "Upload run artifacts" step went green having uploaded NOTHING;
list_workflow_run_artifacts on that run returns total_count 0. Two separate
defects produced that, and both are covered here:

  1. NOBODY WROTE THE FILE ON ACTIONS. reports/run-log.txt was only ever
     written by the Cloud-PC wrapper's `>> "%LOG%"` redirect. On the runner
     the path in the upload list could never resolve, and QC-021 — which
     parses that file — sat behind `if _log_path.exists():` with no else, so
     it emitted NOTHING at all on the only host that fires. A check that
     prints no line is indistinguishable from a passing one.

  2. A KILLED STEP DISCARDED ITS OWN EVIDENCE. run_step SIGKILLs a step that
     exceeds its timeout, and SIGKILL throws away whatever is still in the
     child's stdio buffer. Python block-buffers stdout whenever it is not a
     TTY, which on a runner it never is. Both QC passes were killed at their
     180s cap that fire, and their last log lines are physically fused with
     the next process's first line — the signature of a write cut in half.
     That is why QC-057's PII-scrubbed body snippets, written expressly so a
     parser fix could be scoped from them, have never once been readable.

Between them: 31 fires of "QC self-heal (post-patch) TIMEOUT @ 180s" with no
retrievable evidence of what the step was doing when it died.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal as q  # noqa: E402
import run_pipeline as rp  # noqa: E402


# ── the gate: which host writes the transcript ────────────────────────────
def test_tee_is_off_on_the_cloud_pc():
    """The wrapper already redirects our stdout INTO run-log.txt there.
    Teeing as well would write every line twice, and QC-021 counts markers."""
    assert rp.should_tee_run_log({}) is False


def test_tee_is_on_for_the_ephemeral_runner():
    """AZURE_STORAGE_CONNECTION_STRING is the same predicate qc_selfheal
    already calls _BLOB_HOST for 'ephemeral runner'."""
    assert rp.should_tee_run_log({"AZURE_STORAGE_CONNECTION_STRING": "x"}) is True


@pytest.mark.parametrize("value,want", [("0", False), ("false", False),
                                        ("1", True), ("on", True)])
def test_explicit_override_wins_in_both_directions(value, want):
    """A Cloud PC that ever gains the connection string must not start
    double-logging silently, and a runner must be forceable on."""
    env = {"AZURE_STORAGE_CONNECTION_STRING": "x", "HILMAR_RUN_LOG_TEE": value}
    assert rp.should_tee_run_log(env) is want


# ── the tee itself ────────────────────────────────────────────────────────
# pytest's default capture is fd-level: it dup2s fd 1 to its own temp file and
# swaps sys.stdout. The tee dup2s fd 1 too, so under capture the two fight and
# the test measures pytest rather than production. capfd.disabled() hands the
# real descriptors back for the duration, which is the state a fire runs in.
def test_tee_captures_child_process_output(tmp_path, capfd):
    """THE load-bearing property. run_step calls subprocess.run WITHOUT
    capture, so every step's real output is written by a CHILD straight to
    the inherited fd. A sys.stdout wrapper would tee the orchestrator's own
    banners and lose the entire contents — hence the fd-level dup2."""
    log = tmp_path / "reports" / "run-log.txt"
    with capfd.disabled(), rp.RunLogTee(log):
        print("PARENT: banner")
        subprocess.run([sys.executable, "-c",
                        "import sys; print('CHILD: stdout');"
                        " print('CHILD: stderr', file=sys.stderr)"], check=True)
    text = log.read_text(encoding="utf-8")
    assert "PARENT: banner" in text
    assert "CHILD: stdout" in text, "child stdout must reach the transcript"
    assert "CHILD: stderr" in text, "child stderr must reach the transcript"


def test_tee_does_not_swallow_the_console(tmp_path, capfd):
    """A tee that eats its input trades one blind channel for another — the
    Actions log must still receive every line.

    This bites the teardown-ordering bug the first draft shipped: it closed
    the saved console fd BEFORE joining the pump thread, so anything still in
    flight hit a closed descriptor and vanished from the console while
    landing in the file. Measured that way round before the fix.
    """
    console = tmp_path / "console.txt"
    log = tmp_path / "reports" / "run-log.txt"
    with capfd.disabled():
        saved = os.dup(1)
        try:
            with open(console, "wb") as fh:
                os.dup2(fh.fileno(), 1)
                with rp.RunLogTee(log):
                    print("FIRST LINE")
                    print("LAST LINE BEFORE TEARDOWN")
                sys.stdout.flush()
        finally:
            os.dup2(saved, 1)
            os.close(saved)
    seen = console.read_text(encoding="utf-8")
    assert "FIRST LINE" in seen
    assert "LAST LINE BEFORE TEARDOWN" in seen, (
        "the final line reached the file but not the console — the pump was "
        "joined after the saved fd was closed")


def test_tee_appends_so_successive_fires_accumulate(tmp_path, capfd):
    """QC-021 searches for TODAY's marker and reads forward; QC-055 reads the
    tail. Truncating would delete the wrapper's history on a shared host."""
    log = tmp_path / "reports" / "run-log.txt"
    with capfd.disabled():
        with rp.RunLogTee(log):
            print("FIRE ONE")
        with rp.RunLogTee(log):
            print("FIRE TWO")
    text = log.read_text(encoding="utf-8")
    assert "FIRE ONE" in text and "FIRE TWO" in text


# ── the killed step keeps its evidence ────────────────────────────────────
def test_killed_step_still_lands_its_output(tmp_path, monkeypatch, capfd):
    """The 31-fire blind spot, reproduced.

    A step that prints a diagnostic and then outlives its timeout is
    SIGKILLed. With Python's default block buffering on a non-TTY the
    diagnostic dies in the buffer; run_step forces PYTHONUNBUFFERED=1 so it
    is already on disk when the kill lands.

    MEASURED both ways with this exact harness — buffered: line LOST;
    unbuffered: line SURVIVES. Remove the sub_env["PYTHONUNBUFFERED"] line in
    run_step and this test fails.
    """
    # Scrub the variable from the ambient env so the assertion measures
    # run_step's behaviour and not the shell's. (A first run of this
    # measurement was invalid for exactly that reason.)
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    monkeypatch.setattr(rp, "STEP_TIMEOUT_S", 2)
    monkeypatch.setattr(rp, "STEP_TIMEOUTS_S", {})

    log = tmp_path / "reports" / "run-log.txt"
    # ASSEMBLE THE MARKER AT RUNTIME. run_step's banner echoes the command
    # line verbatim — `cmd: {" ".join(cmd)}` — so a literal in the child's
    # source appears in the transcript whether or not the child ever runs.
    # The first draft of this test asserted on such a literal and passed with
    # the unbuffering removed: it was matching the banner, certifying nothing.
    child = ("import time\n"
             "tag = 'QC-057' + '-DIAG'\n"
             "print(tag + ': subject=Please confirm below has_body=True')\n"
             "time.sleep(30)\n")
    with capfd.disabled(), rp.RunLogTee(log):
        rc = rp.run_step("Slow step", [sys.executable, "-c", child])

    text = log.read_text(encoding="utf-8")
    assert rc == 124, "a timeout must report the GNU timeout convention"
    assert "QC-057-DIAG:" in text, (
        "the killed step's diagnostic was discarded with its stdio buffer — "
        "run_step must set PYTHONUNBUFFERED=1 in the child env")
    assert "TIMEOUT" in text and "Slow step" in text


def test_run_step_emits_the_marker_qc021_parses(tmp_path, capfd):
    """QC-021 names the step a dead fire stopped at with
    re.findall(r"^---\\s*(.+?)\\s*---\\s*$", ...). Without one marker per
    PIPELINE step the best it could ever say was 'run_pipeline'."""
    import re
    log = tmp_path / "reports" / "run-log.txt"
    with capfd.disabled(), rp.RunLogTee(log):
        rp.run_step("QC self-heal (post-patch)", ["true"], dry_run=True)
    found = re.findall(r"^---\s*(.+?)\s*---\s*$",
                       log.read_text(encoding="utf-8"), re.MULTILINE)
    assert "QC self-heal (post-patch)" in found


def test_header_carries_both_date_spellings_and_the_interpreter():
    """QC-021 locates today's fire by %m/%d/%Y OR %Y-%m-%d; the runbook in
    qc_actions_from_sentry tells an operator to read the 'PY:' line for the
    interpreter that actually ran."""
    from datetime import datetime
    started = datetime(2026, 9, 4, 6, 30, 0)
    head = rp.run_log_header(started, host="github-actions")
    assert "2026-09-04" in head
    assert "09/04/2026" in head
    assert "PY: " in head
    assert "github-actions" in head


# ── QC-021 must not be silent, and must not cry wolf ──────────────────────
def _base_data() -> dict:
    return {
        "version": "2", "requests": [],
        "summary": {
            "wins": 0, "quoted_lost": 0, "not_quoted": 0, "pending_hilmar": 0,
            "win_rate": 0.0, "quote_rate": 0.0, "teu_requested": 0, "teu_won": 0,
            "teu_quoted_lost": 0, "teu_not_quoted": 0, "teu_pending": 0,
            "total_entries": 0,
        },
    }


def _fired(data: dict) -> list[str]:
    log = q.Log()
    q.phase_6_rules(log, data)
    return log.warnings + log.errors


def _fake_root(tmp_path, monkeypatch, run_log_text: str | None):
    """Point qc_selfheal's __file__ at a tmp tree, the technique
    tests/test_qc_054_055_runtime_observability.py already uses."""
    (tmp_path / "reports").mkdir(exist_ok=True)
    if run_log_text is not None:
        (tmp_path / "reports" / "run-log.txt").write_text(run_log_text,
                                                         encoding="utf-8")
    (tmp_path / "scripts").mkdir(exist_ok=True)
    fake_self = tmp_path / "scripts" / "qc_selfheal.py"
    fake_self.write_text("# fake — only __file__ needs to resolve here\n")
    monkeypatch.setattr(q, "__file__", str(fake_self))


def test_qc021_absent_transcript_on_a_runner_is_a_finding_not_silence(
        tmp_path, monkeypatch):
    """Before 2026-09-04 `if _log_path.exists():` had NO else, so on Actions
    — every fire — QC-021 emitted nothing whatsoever. Not a warn, not even an
    ok. Delete the `elif not _log_path.exists():` branch and this fails."""
    _fake_root(tmp_path, monkeypatch, run_log_text=None)
    monkeypatch.setattr(q, "_BLOB_HOST", True)
    msgs = [m for m in _fired(_base_data()) if "QC-021" in m]
    assert msgs, "QC-021 must say something when the transcript it reads is missing"
    assert "absent" in msgs[0]


def test_qc021_does_not_demand_a_send_line_on_the_ephemeral_runner(
        tmp_path, monkeypatch):
    """The trap this fix had to avoid: writing the transcript on Actions makes
    the file exist, and the old code would then hunt for 'Sent. request-id='
    — a line that CANNOT be there, because outlook_send is a separate
    workflow step that runs after run_pipeline exits, outside the tee. Left
    alone it would manufacture a daily false alarm out of correct behaviour.
    """
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    _fake_root(tmp_path, monkeypatch,
               run_log_text=f"HILMAR FIRE {today} host=github-actions\n"
                            "--- Backup snapshot ---\n"
                            "--- QC self-heal (post-patch) ---\n")
    monkeypatch.setattr(q, "_BLOB_HOST", True)
    msgs = [m for m in _fired(_base_data()) if "QC-021" in m]
    assert not msgs, f"QC-021 must not warn on the ephemeral runner: {msgs}"


def test_qc021_warns_when_the_runner_transcript_has_no_fire_header(
        tmp_path, monkeypatch):
    """A stale file with no header for today means the tee never opened."""
    _fake_root(tmp_path, monkeypatch, run_log_text="something from last week\n")
    monkeypatch.setattr(q, "_BLOB_HOST", True)
    msgs = [m for m in _fired(_base_data()) if "QC-021" in m]
    assert msgs and "no" in msgs[0].lower()
    assert "HILMAR_RUN_LOG_TEE" in msgs[0]


def test_qc021_cloud_pc_path_is_unchanged(tmp_path, monkeypatch):
    """The wrapper host keeps the original semantics exactly: a send line
    after today's marker is the pass condition."""
    from datetime import datetime
    today = datetime.now().strftime("%m/%d/%Y")
    _fake_root(tmp_path, monkeypatch,
               run_log_text=f"{today} 06:30:00\n"
                            "Pipeline exit code: 0\n"
                            "Sent. request-id=abc123\n")
    monkeypatch.setattr(q, "_BLOB_HOST", False)
    msgs = [m for m in _fired(_base_data()) if "QC-021" in m]
    assert not msgs, f"a completed wrapper fire must not warn: {msgs}"


def test_qc021_cloud_pc_still_catches_the_pipeline_that_never_sent(
        tmp_path, monkeypatch):
    """The original defect QC-021 exists for: wrappers that exited 255
    between 'Pipeline exit code: 0' and the send step."""
    from datetime import datetime
    today = datetime.now().strftime("%m/%d/%Y")
    _fake_root(tmp_path, monkeypatch,
               run_log_text=f"{today} 06:30:00\nPipeline exit code: 0\n")
    monkeypatch.setattr(q, "_BLOB_HOST", False)
    msgs = [m for m in _fired(_base_data()) if "QC-021" in m]
    assert msgs, "a pipeline that completed without sending must still fire"
    assert "Sent. request-id=" in msgs[0]


# ── QC-055 must not start lying because the file got bigger ──────────────
# Added with the tee, not after it. QC-055 read `[-50000:]` — a window chosen
# when run-log.txt held only the wrapper's terse step echoes. The tee writes
# the whole fire transcript into that same file, and the sentinel is printed
# at the START of the fire while QC-055 runs two pipeline steps later.
#
# MEASURED against the pre-fix block, sentinel present in every case:
#   10,052 bytes -> ERROR (correct)
#   45,026 bytes -> ERROR (correct)
#   60,048 bytes -> SILENT OK   ** false pass **
#   90,034 bytes -> SILENT OK   ** false pass **
#
# Production fire 33864188808 carried ~89 KB of pipeline output. Shipping the
# tee without this would have converted an honest "skipped — ephemeral runner"
# into an invisible false pass on every fire, through Log.ok, which never
# reaches qc-result.json.
_SENTINEL_55 = ("Sentry cron start failed (pipeline continues): "
                "No module named 'sentry_sdk'")


def _run_log(tmp_path, monkeypatch, *, size, header=True, sentinel=True):
    from datetime import datetime
    now = datetime.now()
    body = ""
    if header:
        body += (f"HILMAR FIRE {now:%Y-%m-%d} ({now:%m/%d/%Y} {now:%H:%M:%S}) "
                 f"host=github-actions\n")
    if sentinel:
        body += _SENTINEL_55 + "\n"
    filler = "  QC-0xx: ordinary pipeline transcript output line\n"
    body += filler * (size // len(filler))
    _fake_root(tmp_path, monkeypatch, run_log_text=body)
    return _fired(_base_data())


@pytest.mark.parametrize("size", [10_000, 60_000, 200_000])
def test_qc055_catches_the_dead_heartbeat_at_any_transcript_size(
        size, tmp_path, monkeypatch):
    """The regression the tee would otherwise have caused. 60_000 and 200_000
    both false-passed against the byte-tail version."""
    msgs = [m for m in _run_log(tmp_path, monkeypatch, size=size)
            if "QC-055" in m]
    assert msgs, (
        f"QC-055 missed a dead heartbeat in a {size}-byte transcript — the "
        f"sentinel scrolled out of its read window")
    assert "NOT registering" in msgs[0]


def test_qc055_still_passes_a_healthy_fire(tmp_path, monkeypatch):
    """Scoping to today's header must not turn every fire into a finding."""
    msgs = [m for m in _run_log(tmp_path, monkeypatch, size=90_000,
                                sentinel=False) if "QC-055" in m]
    assert not msgs, f"false alarm on a healthy heartbeat: {msgs}"


def test_qc055_warns_rather_than_asserting_a_pass_when_it_cannot_see(
        tmp_path, monkeypatch, capsys):
    """A check that cannot find its evidence must never assert a pass.

    Two halves, and the second needs stdout: Log.ok() is not recorded on the
    Log at all, so a spurious "registered" line printed alongside the warn is
    invisible to log.warnings — an operator reading the run would see a
    finding and a pass for the same check in the same pass. The first draft of
    this test checked only log.warnings and could not tell the difference.
    """
    msgs = [m for m in _run_log(tmp_path, monkeypatch, size=90_000,
                                header=False, sentinel=True) if "QC-055" in m]
    assert msgs, "QC-055 asserted a pass while blind"
    assert "cannot verify" in msgs[0]
    printed = capsys.readouterr().out
    assert "heartbeat registered" not in printed, (
        "QC-055 printed a pass line beside its own 'cannot verify' warning")


def test_qc055_scopes_to_today_so_a_fixed_failure_stops_re_raising(
        tmp_path, monkeypatch):
    """Nothing un-stamps a bad value: on an accumulating log, last week's
    already-fixed failure must not be reported again today."""
    from datetime import datetime
    now = datetime.now()
    body = ("HILMAR FIRE 2026-08-20 (08/20/2026 06:30:00) host=github-actions\n"
            + _SENTINEL_55 + "\n"
            + f"HILMAR FIRE {now:%Y-%m-%d} ({now:%m/%d/%Y} {now:%H:%M:%S}) "
              f"host=github-actions\n"
            + "  QC-0xx: this fire's heartbeat registered fine\n")
    _fake_root(tmp_path, monkeypatch, run_log_text=body)
    msgs = [m for m in _fired(_base_data()) if "QC-055" in m]
    assert not msgs, f"re-raised a failure that predates today's fire: {msgs}"


def test_qc021_reads_the_latest_fire_not_the_first_of_the_day(
        tmp_path, monkeypatch, capsys):
    """PRE-EXISTING, found by the adversarial pass over this change.

    QC-021 located today's marker with `.find` — the FIRST occurrence. On the
    Cloud-PC log, which accumulates and saw two scheduled fires a day, `_after`
    therefore spanned from fire #1's header and matched fire #1's
    "Sent. request-id=". MEASURED before the fix: a second fire that was
    mid-flight and had sent nothing printed "today's wrapper completed send
    step". A send monitor reporting a send that did not happen.
    """
    from datetime import datetime
    today = datetime.now().strftime("%m/%d/%Y")
    _fake_root(tmp_path, monkeypatch, run_log_text=(
        f"Hilmar daily on BOX — {today} 06:30:00\n"
        "--- run_pipeline ---\nPipeline exit code: 0\n"
        "Sent. request-id=fire-one\n"
        f"Hilmar daily on BOX — {today} 18:00:00\n"
        "--- run_pipeline ---\n"))
    monkeypatch.setattr(q, "_BLOB_HOST", False)
    msgs = [m for m in _fired(_base_data()) if "QC-021" in m]
    printed = capsys.readouterr().out
    assert "completed send step" not in printed, (
        "QC-021 credited the current fire with the EARLIER fire's send")
    assert msgs, "the in-flight fire has not sent — that must be reported"
