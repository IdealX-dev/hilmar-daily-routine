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

from hilmar.parser_accuracy import FIELD_REQUIREMENTS  # noqa: E402


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


def _qc_module_ast():
    src = (SCRIPTS / "qc_selfheal.py").read_text(encoding="utf-8")
    return ast.parse(src)


def _phase6_node(tree=None):
    tree = tree if tree is not None else _qc_module_ast()
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "phase_6_rules")


def _row_mutation_sites(fn, tree):
    """Every place phase_6_rules can write a row field, as (lineno, what).

    A REAL AST walk, which the first version of this test only pretended to
    be: it parsed the tree solely to slice out the source text, then ran
    `seg.find('["field"] = ')`. That literal-string search could not see:
      - variable-key writes  — `r[_f] = None`, which is how QC-064 nulls
        garbage out of client-visible cells (six graded fields, five critical)
      - dict mutator calls   — .update(), .setdefault(), .pop()
      - writes inside module-level helpers that this phase CALLS, e.g.
        qc070_teu_sanity, which writes teu_requested / container_count
    All three are collected here. Variable-key writes and mutator calls are
    reported unconditionally, because a static walk cannot know which field a
    computed key resolves to — treating them as "unknown, therefore unsafe"
    is the only sound direction for a safety check.
    """
    graded = set(FIELD_REQUIREMENTS)
    helper_lines = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n is not fn:
            helper_lines[n.name] = n

    sites = []

    def scan(node, origin, at_line):
        for n in ast.walk(node):
            if isinstance(n, (ast.Assign, ast.AugAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    if not isinstance(t, ast.Subscript):
                        continue
                    key = t.slice
                    if isinstance(key, ast.Constant):
                        if key.value in graded:
                            sites.append((at_line or n.lineno,
                                          f"{origin}: [{key.value!r}] = ..."))
                    else:
                        sites.append((at_line or n.lineno,
                                      f"{origin}: [<computed {ast.unparse(key)}>] = ..."))
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr in ("update", "setdefault", "pop"):
                    sites.append((at_line or n.lineno,
                                  f"{origin}: .{n.func.attr}() on a mapping"))

    scan(fn, "phase_6_rules", None)
    # Follow one level into helpers this phase calls by name.
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            target = helper_lines.get(n.func.id)
            if target is not None:
                scan(target, f"{n.func.id}() called at line {n.lineno}", n.lineno)
    return sites


def test_every_mutating_heal_in_phase_6_precedes_the_gate():
    """THE GENERAL RULE: a gate measures the FINAL state of the rows.

    Any write to a graded field that lands after compute_accuracy makes the
    gate's number a lie about what ships. Both directions have now bitten:
    measuring before the carrier heals WITHHELD a good report (2026-07-27);
    measuring before QC-064's nulling heal would SHIP a bad one (2026-07-28).
    """
    tree = _qc_module_ast()
    fn = _phase6_node(tree)
    gate_lines = [n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Call)
                  and getattr(n.func, "id", "") == "compute_accuracy"]
    assert len(gate_lines) == 1, (
        f"expected exactly one compute_accuracy call in phase_6_rules, "
        f"found {len(gate_lines)} — this test's ordering premise is broken")
    gate = gate_lines[0]

    late = [(line, what) for line, what in _row_mutation_sites(fn, tree)
            if line > gate]
    assert not late, (
        "these row mutations land AFTER the accuracy gate measures, so the "
        "gate is grading a state that is about to change:\n  " +
        "\n  ".join(f"line {line}: {what}" for line, what in sorted(late)))


def test_the_ordering_check_cannot_go_inert():
    """A guard that finds nothing to guard is indistinguishable from a broken
    one. The predecessor covered ('carrier_quoted', 'carrier_won', 'ol_rate')
    by literal spelling — and `["ol_rate"] = ` appears ZERO times in
    phase_6_rules, so a third of it constrained nothing while reading as
    thorough. Assert the walk still SEES the mutations it is ordering."""
    tree = _qc_module_ast()
    sites = _row_mutation_sites(_phase6_node(tree), tree)
    assert len(sites) >= 5, (
        f"the AST walk found only {len(sites)} row-mutation sites in "
        "phase_6_rules — it has probably stopped matching the code shape, "
        "and a passing ordering test would mean nothing")
    assert any("computed" in what for _, what in sites), (
        "no variable-key write detected — QC-064's `r[_f] = None` is the "
        "exact shape this walk exists to catch; if it is gone, confirm that "
        "deliberately rather than letting the check silently weaken")


def test_every_mutating_phase_runs_before_the_gate_phase():
    """The same rule ONE LEVEL UP, which nothing enforced.

    phase_3_entries calls _heal_carrier_won on a gate-graded field. Reordering
    main() to run it after phase_6_rules reintroduces the 2026-07-27 bug
    exactly, and the whole suite stayed green — because every in-phase check
    reasons about phase_6_rules alone, and the behavioural tests drive their
    own two-phase sequence rather than production's.
    """
    tree = _qc_module_ast()
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    # Sort by line: ast.walk is breadth-first, so its yield order is nesting
    # depth, NOT source order — reading it directly reported a phase as "after
    # the gate" purely because it sits one block deeper.
    calls = sorted((n.lineno, n.func.id) for n in ast.walk(main_fn)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id.startswith("phase_"))
    seen = list(dict.fromkeys(name for _, name in calls))
    assert "phase_6_rules" in seen, "main() no longer calls phase_6_rules"
    gate_at = seen.index("phase_6_rules")
    mutating = [p for p in seen
                if p.startswith("phase_") and p != "phase_6_rules"
                and not p.startswith("phase_7")]
    late = [p for p in mutating if seen.index(p) > gate_at]
    assert not late, (
        f"these phases mutate rows but run AFTER the gate phase: {late} — "
        "the accuracy gate would grade a state they are about to change")


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


def test_send_alert_itself_warns_when_it_reached_nobody(capsys, tmp_path, monkeypatch):
    """THE PRODUCTION ENTRY POINT, not the private helper.

    Every other test here calls _warn_if_undeliverable directly, so deleting
    the single call site inside send_alert left all 2093 tests green and
    restored the 2026-07-27 silence verbatim. Nothing exercised the wiring.
    """
    monkeypatch.setattr(FA, "_github_issue", lambda *a, **k: False)
    monkeypatch.setattr(FA, "_teams", lambda *a, **k: False)
    monkeypatch.setattr(FA, "ALERTS_QUEUE", tmp_path / "alerts-queue.json")
    monkeypatch.setattr(FA, "REPORTS", tmp_path)

    results = FA.send_alert("fire blocked", "QC-039 gate tripped")

    assert FA.undeliverable(results) is True
    err = capsys.readouterr().err
    assert "ALERT UNDELIVERABLE" in err, (
        "send_alert returned an undeliverable result without saying so — "
        "the alarm failed silently, which is the original defect")


def test_send_alert_stays_quiet_when_a_remote_channel_took_it(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(FA, "_github_issue", lambda *a, **k: True)
    monkeypatch.setattr(FA, "_teams", lambda *a, **k: False)
    monkeypatch.setattr(FA, "ALERTS_QUEUE", tmp_path / "alerts-queue.json")
    monkeypatch.setattr(FA, "REPORTS", tmp_path)

    FA.send_alert("fire blocked", "QC-039 gate tripped")
    assert "ALERT UNDELIVERABLE" not in capsys.readouterr().err


def test_the_undeliverable_banner_never_raises_out_of_send_alert(monkeypatch):
    """send_alert's contract is best-effort and never-blocking. The banner
    runs on the ALREADY-BAD path, so an unwrapped print there turns 'the
    alarm could not deliver' into 'the caller crashed' at the worst possible
    moment — a closed or non-UTF-8 stderr was enough to do it."""
    class _Hostile:
        def write(self, *_a, **_k):
            raise ValueError("I/O operation on closed file")

        def flush(self, *_a, **_k):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stderr", _Hostile())
    FA._warn_if_undeliverable({"stderr": True, "queue": True,
                               "github": False, "teams": False})


def test_the_cli_exits_nonzero_when_the_alert_reached_nobody(monkeypatch, tmp_path, capsys):
    """`any(res.values())` counted stderr and queue, which are True on any
    healthy process — so the CLI reported success for an alert that reached
    nobody, the exact condition undeliverable() names."""
    monkeypatch.setattr(FA, "_github_issue", lambda *a, **k: False)
    monkeypatch.setattr(FA, "_teams", lambda *a, **k: False)
    monkeypatch.setattr(FA, "ALERTS_QUEUE", tmp_path / "alerts-queue.json")
    monkeypatch.setattr(FA, "REPORTS", tmp_path)
    monkeypatch.setattr(sys, "argv", ["fire_alert.py", "--title", "t", "--body", "b"])
    assert FA.main() == 1

    monkeypatch.setattr(FA, "_github_issue", lambda *a, **k: True)
    assert FA.main() == 0


def test_a_delivered_alert_stays_quiet(capsys):
    FA._warn_if_undeliverable({"stderr": True, "queue": True,
                               "github": True, "teams": False})
    assert "ALERT UNDELIVERABLE" not in capsys.readouterr().err


# ── [B] the newly-live GitHub channel must not spam or self-erase ───────────

def test_a_repeated_alert_comments_instead_of_opening_a_second_issue(monkeypatch):
    """QC-063 fires on EVERY fire until the failing step is fixed, so without
    dedupe a step dead for a week files five identical issues. The channel was
    a permanent no-op until 2026-07-28, which is why this never showed."""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R:
            returncode = 0
            stdout = '[{"number": 41, "title": "fire blocked"}]' if "list" in argv else ""
            stderr = ""
        return R()

    monkeypatch.setattr(FA, "_have_gh", lambda: True)
    monkeypatch.setattr(FA.subprocess, "run", fake_run)

    assert FA._github_issue("fire blocked", "again", ("fire-alert",)) is True
    verbs = [a[2] for a in calls if len(a) > 2]   # gh issue <verb>
    assert "comment" in verbs, f"expected a comment on the open issue, got {verbs}"
    assert "create" not in verbs, "a duplicate issue was opened anyway"


def test_a_new_alert_still_opens_an_issue(monkeypatch):
    """Dedupe must never be able to swallow a genuinely new alert."""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R:
            returncode = 0
            stdout = "[]" if "list" in argv else ""
            stderr = ""
        return R()

    monkeypatch.setattr(FA, "_have_gh", lambda: True)
    monkeypatch.setattr(FA.subprocess, "run", fake_run)

    assert FA._github_issue("brand new", "body", ("fire-alert",)) is True
    assert "create" in [a[2] for a in calls if len(a) > 2]


def test_a_failing_dedupe_lookup_still_files_the_alert(monkeypatch):
    """Best-effort: if the lookup breaks, create. Duplicate noise is far
    cheaper than a suppressed alarm."""
    monkeypatch.setattr(FA, "_have_gh", lambda: True)
    monkeypatch.setattr(FA.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert FA._existing_open_issue("anything") == ""


def test_fire_alerts_do_not_default_into_the_auto_closed_label():
    """liveness.yml closes EVERY open `cloud-pc-down` issue on a fresh
    heartbeat. While that was the default label, a critical alert — e.g.
    assert_fire_integrity's 'no verified report shipped' — could be filed and
    auto-closed within hours by an unrelated watchdog, with its condition
    still true."""
    import inspect
    default = inspect.signature(FA.send_alert).parameters["labels"].default
    assert "cloud-pc-down" not in default, (
        "send_alert defaults into the label liveness.yml auto-closes — a "
        "genuine alert can be erased by an unrelated recovery")
    assert "fire-alert" in default


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

def _no_channels(monkeypatch):
    """Both remote channels absent. Patches the module-level helpers rather
    than the environment: fire_alert.github_configured() also consults the gh
    CLI, so `delenv GH_TOKEN` alone leaves the result host-dependent."""
    monkeypatch.setattr(QC, "_fire_alert_github_configured", lambda: False)
    monkeypatch.setattr(QC, "_fire_alert_teams_configured", lambda: False)


def test_qc076_errors_on_a_runner_with_no_alert_channel(monkeypatch):
    """THE 2026-07-27 CONFIGURATION. Unattended, no token, no webhook — a
    failing fire could not tell anyone, and nothing said so."""
    monkeypatch.setenv("HILMAR_NONINTERACTIVE", "1")
    _no_channels(monkeypatch)
    log, _ = _run_phase6([_quoted_row_missing_carrier()])
    hits = [e for e in log.errors if "QC-076" in e]
    assert hits, "a runner with no alert channel did not raise QC-076"
    assert "reach NOBODY" in hits[0]


def test_qc076_passes_when_the_github_channel_is_wired(monkeypatch):
    monkeypatch.setenv("HILMAR_NONINTERACTIVE", "1")
    monkeypatch.setattr(QC, "_fire_alert_github_configured", lambda: True)
    monkeypatch.setattr(QC, "_fire_alert_teams_configured", lambda: False)
    log, _ = _run_phase6([_quoted_row_missing_carrier()])
    assert not [e for e in log.errors if "QC-076" in e]


def test_qc076_passes_when_only_teams_is_wired(monkeypatch):
    """Either remote channel is enough — they are alternatives, not both."""
    monkeypatch.setenv("HILMAR_NONINTERACTIVE", "1")
    monkeypatch.setattr(QC, "_fire_alert_github_configured", lambda: False)
    monkeypatch.setattr(QC, "_fire_alert_teams_configured", lambda: True)
    log, _ = _run_phase6([_quoted_row_missing_carrier()])
    assert not [e for e in log.errors if "QC-076" in e]


def test_qc076_does_not_false_fire_on_an_attended_run(monkeypatch):
    """On a dev box or an interactive run, stderr IS a human-visible channel.
    Erroring there would train the operator to ignore QC-076."""
    monkeypatch.delenv("HILMAR_NONINTERACTIVE", raising=False)
    _no_channels(monkeypatch)
    log, _ = _run_phase6([_quoted_row_missing_carrier()])
    assert not [e for e in log.errors if "QC-076" in e]


def test_qc076_is_unattended_by_the_flag_both_hosts_set_not_by_actions(monkeypatch):
    """The predicate must be HILMAR_NONINTERACTIVE, not GITHUB_ACTIONS.

    Keying on GITHUB_ACTIONS made QC-076 blind to every unattended run that
    is not on Actions — a Task Scheduler fire writes to reports/run-log.txt,
    the same 'buried in a log nobody opens' channel the check exists to
    condemn, and QC-076 called it 'attended run, stderr reaches the operator'.
    """
    _no_channels(monkeypatch)
    monkeypatch.delenv("HILMAR_NONINTERACTIVE", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    log, _ = _run_phase6([_quoted_row_missing_carrier()])
    assert not [e for e in log.errors if "QC-076" in e], (
        "GITHUB_ACTIONS alone must not drive QC-076")

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("HILMAR_NONINTERACTIVE", "1")
    log, _ = _run_phase6([_quoted_row_missing_carrier()])
    assert [e for e in log.errors if "QC-076" in e], (
        "an unattended run off Actions must still be covered")


def test_qc076_channel_helpers_follow_fire_alert_not_a_local_copy(monkeypatch):
    """BEHAVIOURAL delegation check, replacing a source-grep that a comment
    satisfied. The grep version passed while the helper was re-implemented as
    an env-only copy that ignored secrets/teams-webhook-url.txt — exactly the
    drift it claimed to prevent. Move the thing being delegated to and the
    delegator must move with it.
    """
    import fire_alert

    monkeypatch.setattr(fire_alert, "_teams_webhook_url", lambda: "https://x")
    assert QC._fire_alert_teams_configured() is True
    monkeypatch.setattr(fire_alert, "_teams_webhook_url", lambda: "")
    assert QC._fire_alert_teams_configured() is False

    monkeypatch.setattr(fire_alert, "github_configured", lambda: True)
    assert QC._fire_alert_github_configured() is True
    monkeypatch.setattr(fire_alert, "github_configured", lambda: False)
    assert QC._fire_alert_github_configured() is False


def test_the_github_channel_counts_the_gh_cli_not_just_a_token(monkeypatch):
    """fire_alert._github_issue tries the gh CLI FIRST, which needs no env var
    at all. The original QC-076 read `GH_TOKEN or GITHUB_TOKEN` inline, so it
    called a working channel dead on any box with `gh auth login` done."""
    import fire_alert

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(fire_alert, "_have_gh", lambda: True)
    assert fire_alert.github_configured() is True, (
        "gh CLI authentication is a real GitHub channel and must count")

    monkeypatch.setattr(fire_alert, "_have_gh", lambda: False)
    assert fire_alert.github_configured() is False
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")
    assert fire_alert.github_configured() is True
