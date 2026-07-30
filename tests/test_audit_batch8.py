"""Audit batch 8 — a safety net must never hold the client report hostage.

THE OUTAGE. The daily report did not ship on 2026-07-27, 07-28 or 07-29. The
last one that reached the client was Friday 2026-07-24.

Monday was a GitHub runner-allocation failure plus the QC-039 gate (fixed in
#126/#127). Tuesday and Wednesday were something else entirely, and worse:

    state_store: pulled 6 file(s): tracking-data-v2.json, ...      <- GET fine
    python3 scripts/state_store.py backup
    azure.core.exceptions.ResourceNotFoundError: ... ResourceNotFound
    ##[error]Process completed with exit code 1

Steps 9-13 — validate, run the pipeline, SEND THE DAILY EMAIL, send the client
email, prove the fire shipped — all `skipped`.

A dated gzip snapshot is a SAFETY NET. The daily report is the PRODUCT. The
step ran under the default `bash -e`, so the net failing took the product with
it. Two more business days of reports were lost to a backup that could not be
written, which is strictly worse than having no backup for two days.

The narrow fix is `continue-on-error` on that step. The fix that matters is
the invariant below: NO step between the start of the job and the send may be
able to abort the fire unless it is genuinely essential to the report being
correct. Every exception is named, with the reason it earns fatality.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

DAILY_YML = ROOT / ".github" / "workflows" / "daily.yml"

#: The step that IS the product. Everything before it is either essential to
#: that send being correct, or must not be able to prevent it.
SEND_STEP = "Send the daily email + audit"

#: Steps before the send that are allowed to abort the fire, each with the
#: reason it earns that power. A step earns fatality only when shipping
#: WITHOUT it would produce a wrong, unsafe, or unsendable report — never
#: merely because it is useful.
ESSENTIAL_BEFORE_SEND: dict[str, str] = {
    "Verify prerequisites — refuse to fire if anything's missing":
        "a missing secret means the pipeline silently degrades; better to not fire",
    "(uses: actions/checkout@v5)":
        "no source tree, no pipeline — nothing downstream can run",
    "Set up Python 3.12":
        "no interpreter, no pipeline",
    "Install dependencies":
        "no deps, no pipeline",
    "Materialize secret files the pipeline reads from secrets/*.txt":
        "the pipeline reads these from disk; absent means broken auth mid-run",
    "Pull pipeline state from blob store":
        "without prior state the fire would re-ingest from scratch and could "
        "double-send; the token cache also arrives here, so a failed pull "
        "means no working Graph auth at all",
    "Validate prerequisites for real — name any wrong secret":
        "a wrong-field paste dies mid-run as an SDK traceback; this names it first",
    "Run the daily pipeline (app-only Graph auth)":
        "this builds the report — there is nothing to send without it",
}


def _fire_job() -> dict:
    """The production-fire job, parsed as YAML rather than sliced as text.

    Slicing daily.yml as a string produced vacuously-passing assertions in an
    earlier batch (the slice returned '' and every `in` check trivially held),
    so this parses properly and every test here reads the real structure.
    """
    return yaml.safe_load(DAILY_YML.read_text(encoding="utf-8"))["jobs"]["production-fire"]


def _steps() -> list[dict]:
    return _fire_job()["steps"]


def _name(step: dict) -> str:
    return step.get("name") or f"(uses: {step.get('uses')})"


def _steps_before_send() -> list[dict]:
    steps = _steps()
    names = [_name(s) for s in steps]
    assert SEND_STEP in names, (
        f"the send step {SEND_STEP!r} is gone from daily.yml — this whole "
        f"module reasons about it; found: {names}")
    return steps[:names.index(SEND_STEP)]


def _can_abort(step: dict) -> bool:
    """True when this step failing aborts the job (and so skips the send)."""
    # `continue-on-error` is the only thing that stops a failing step from
    # aborting the job. An `if:` guard changes WHETHER a step runs, not what
    # happens when it runs and fails, so it does not make a step safe here.
    return step.get("continue-on-error") is not True


# ── the specific regression ────────────────────────────────────────────────

def test_a_failed_snapshot_backup_cannot_block_the_report():
    """THE 2026-07-28 CONFIGURATION. The blob store refused writes, the
    snapshot step exited 1 under `bash -e`, and the client got nothing for
    two more days."""
    snap = [s for s in _steps() if _name(s) == "Snapshot backup to blob store"]
    assert snap, "the snapshot backup step is gone — update this test deliberately"
    assert snap[0].get("continue-on-error") is True, (
        "the snapshot backup can abort the fire again. It is a safety net; "
        "the daily report is the product. QC-032 flags a stale backup "
        "independently, so this failing loudly is enough.")


def test_the_snapshot_failure_is_still_announced():
    """Non-fatal must not mean silent — that would trade a visible outage for
    an invisible gap in the backup history."""
    names = [_name(s) for s in _steps()]
    assert "Say so if the snapshot backup failed" in names, (
        "the snapshot can now fail without aborting the fire, and nothing "
        "reports it — a silent missing backup is exactly what QC-032 exists "
        "to prevent")
    alert = [s for s in _steps() if _name(s) == "Say so if the snapshot backup failed"][0]
    assert "steps.snapshot.outcome" in str(alert.get("if", "")), (
        "the alert step must be conditioned on the snapshot's outcome")
    assert "fire_alert" in str(alert.get("run", "")), (
        "the announcement must go out-of-band — an alarm riding the failing "
        "subsystem is no alarm (2026-07-27)")


def test_the_send_is_not_conditioned_on_the_snapshot():
    """Guarding the send on the snapshot's outcome would reintroduce the bug
    by another route."""
    send = [s for s in _steps() if _name(s) == SEND_STEP]
    assert send, f"{SEND_STEP!r} is gone from daily.yml"
    assert "snapshot" not in str(send[0].get("if", "")), (
        "the send is gated on the snapshot backup — a failed safety net would "
        "again stop the client report")


# ── the general invariant ──────────────────────────────────────────────────

def test_only_named_essential_steps_can_abort_the_fire():
    """THE RULE, not the instance.

    Any step before the send that can abort the job must be one we have
    deliberately decided is essential to the report being correct. Adding a
    convenience step without `continue-on-error` reintroduces this outage in a
    new place, so adding one must require a conscious edit here.
    """
    offenders = [
        _name(s) for s in _steps_before_send()
        if _can_abort(s) and _name(s) not in ESSENTIAL_BEFORE_SEND
    ]
    assert not offenders, (
        "these steps run before the daily send and can abort it, but are not "
        "listed as essential:\n  " + "\n  ".join(offenders) +
        "\n\nEither give the step `continue-on-error: true` (it is a safety "
        "net) or add it to ESSENTIAL_BEFORE_SEND with the reason it must be "
        "able to stop the client report. On 2026-07-28 a snapshot backup "
        "stopped three days of reports because nobody had made that choice "
        "explicitly."
    )


def test_the_essential_list_has_not_gone_stale():
    """A guard that names steps which no longer exist is drifting toward
    inert — the same failure mode as an allowlist nobody prunes."""
    actual = {_name(s) for s in _steps_before_send()}
    ghosts = sorted(set(ESSENTIAL_BEFORE_SEND) - actual)
    assert not ghosts, (
        f"ESSENTIAL_BEFORE_SEND names steps that no longer exist in "
        f"daily.yml: {ghosts} — prune them so the list keeps meaning something")


def test_the_invariant_actually_covers_something():
    """Non-inertness: if the walk stopped finding steps, every assertion above
    would pass vacuously."""
    before = _steps_before_send()
    assert len(before) >= 6, (
        f"only {len(before)} steps found before the send — the parse has "
        "probably stopped matching daily.yml's shape, and a green run here "
        "would mean nothing")
    assert any(s.get("continue-on-error") is True for s in before), (
        "no step before the send is marked continue-on-error — the snapshot "
        "fix has been reverted, or the parse is not seeing the real steps")


@pytest.mark.parametrize("step_name", sorted(ESSENTIAL_BEFORE_SEND))
def test_each_essential_step_still_exists(step_name):
    """Each named exception is a real step, so the reasons stay attached to
    something."""
    assert step_name in {_name(s) for s in _steps()}, (
        f"{step_name!r} is listed as essential but is not in daily.yml")


def test_the_push_failure_is_announced_with_the_double_send_warning():
    """A failed push means the sent-flags did not persist. The step stays
    fatal — it runs after both sends, so red is the correct signal — but
    nobody should re-dispatch without being told what that implies."""
    names = [_name(s) for s in _steps()]
    assert "Warn that state did not persist" in names
    warn = [s for s in _steps() if _name(s) == "Warn that state did not persist"][0]
    assert "steps.push.outcome" in str(warn.get("if", ""))
    body = str(warn.get("run", ""))
    assert "fire_alert" in body
    assert "re-dispatch" in body.lower(), (
        "the warning must say what the operator should NOT do")


# ── the prereq gate must test reads, not an unrelated write-plane surface ──

CONN = ("DefaultEndpointsProtocol=https;AccountName=acct;"
        "AccountKey=Zm9v;EndpointSuffix=core.windows.net")


class _FakeContainer:
    def __init__(self, ok=True):
        self._ok = ok

    def exists(self):
        if not self._ok:
            raise RuntimeError("container unreadable")
        return True


class _FakeSvc:
    """A storage account in EXACTLY the 2026-07-27 state: reads answer, the
    write plane 404s, and get_service_properties is treated as unknown —
    calling it at all is the defect under test."""

    def __init__(self, *, reads_ok=True, service_props_raises=True):
        self._reads_ok = reads_ok
        self._service_props_raises = service_props_raises
        self.called_service_properties = False

    @classmethod
    def make(cls, **kw):
        inst = cls(**kw)
        return lambda conn: inst

    def get_account_information(self):
        if not self._reads_ok:
            raise RuntimeError("account unreadable")
        return {"account_kind": "StorageV2"}

    def get_container_client(self, name):
        return _FakeContainer(ok=self._reads_ok)

    def get_service_properties(self):
        self.called_service_properties = True
        if self._service_props_raises:
            raise RuntimeError("ResourceNotFound — the write plane is dead")
        return {}


def _patch_sdk(monkeypatch, svc):
    """Install a fake azure.storage.blob.

    The real SDK is not a test dependency (it is only needed on a runner that
    actually syncs), so check_storage would otherwise short-circuit on the
    import guard and every assertion below would be vacuous — the exact
    shape of decorative test this repo keeps getting bitten by.
    """
    import types

    def from_connection_string(conn):
        # Mirror the real SDK: a bare key, not a connection string, raises
        # ValueError. check_storage's own message for that case is what
        # caught the 2026-06-10 wrong-field paste.
        if "AccountName=" not in conn:
            raise ValueError("Connection string missing required connection details.")
        return svc

    blob_mod = types.ModuleType("azure.storage.blob")
    blob_mod.BlobServiceClient = type(
        "BlobServiceClient", (),
        {"from_connection_string": staticmethod(from_connection_string)})
    storage_mod = types.ModuleType("azure.storage")
    storage_mod.blob = blob_mod
    azure_mod = types.ModuleType("azure")
    azure_mod.storage = storage_mod

    monkeypatch.setitem(sys.modules, "azure", azure_mod)
    monkeypatch.setitem(sys.modules, "azure.storage", storage_mod)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", blob_mod)


def test_the_prereq_gate_passes_when_reads_work_even_if_the_write_plane_is_dead(monkeypatch):
    """THE 2026-07-30 RISK. Every fire since 07-27 died at the snapshot step,
    so nothing ever reached this check to discover whether its call still
    answered. It is FATAL: if it had failed, the report would have died three
    steps before the send with the snapshot fix already in place.

    The fire needs the state PULL, which is a read. Gate on that.
    """
    import verify_fire_prereqs as V

    svc = _FakeSvc(reads_ok=True, service_props_raises=True)
    _patch_sdk(monkeypatch, svc)

    ok, msg = V.check_storage(CONN)
    assert ok, f"the prereq gate failed on an account whose reads all work: {msg}"
    assert not svc.called_service_properties, (
        "check_storage still calls get_service_properties — an account-"
        "configuration surface the pipeline never touches, gating the client "
        "report on something it does not need")


def test_the_prereq_gate_still_fails_when_reads_are_genuinely_broken(monkeypatch):
    """Teeth retained: if the pull could not work, the fire must not proceed —
    the delegated token cache arrives via that pull, so there would be no
    Graph auth at all."""
    import verify_fire_prereqs as V

    _patch_sdk(monkeypatch, _FakeSvc(reads_ok=False))
    ok, msg = V.check_storage(CONN)
    assert not ok, "unreadable storage no longer blocks the fire"
    assert "unreadable" in msg.lower()


def test_the_prereq_gate_still_catches_a_bare_key_paste(monkeypatch):
    """The 2026-06-10 failure: the secret held the bare AccountKey, not the
    connection string. That must stay fatal and keep saying what to paste."""
    import verify_fire_prereqs as V

    _patch_sdk(monkeypatch, _FakeSvc())
    ok, msg = V.check_storage("Zm9vYmFyYmF6")
    assert not ok
    assert "connection string" in msg.lower()


# ── QC-077: a real quote the daily report can never show ───────────────────

def _qc_mod():
    import qc_selfheal
    return qc_selfheal


def _fire_phase6(rows):
    """Drive the production phase, not a hand-rolled copy of it."""
    import contextlib
    import io
    QC = _qc_mod()
    data = {"requests": rows, "summary": {}, "lane_summary": {},
            "carrier_summary": {}}
    log = QC.Log()
    with contextlib.redirect_stdout(io.StringIO()):
        QC.phase_5_summaries(log, data)
        QC.phase_6_rules(log, data)
    return log


def _quoted_row(rid="r1", **over):
    row = {
        "request_id": rid, "status": "Q&L", "quoted": True,
        "lane": "Oakland → Busan", "origin": "Oakland", "destination": "Busan",
        "ol_rate": 3150.0, "carrier_quoted": "CMA CGM",
        "request_timestamp": "2026-07-29T17:15:00+00:00",
        "request_date": "2026-07-29",
        "response_timestamp": "2026-07-29T19:02:00+00:00",
        "status_history": [],
    }
    row.update(over)
    return row


def test_qc077_errors_on_a_quote_with_no_response_timestamp():
    """THE 2026-07-29 SHAPE. A row with a rate and a carrier but no
    response_timestamp is invisible to OL-USA RESPONSES on every day, while
    PENDING HILMAR keeps showing its quote — which is why the report looked
    self-contradictory rather than broken. 29 of 315 real rows were like this
    and nothing said so."""
    log = _fire_phase6([_quoted_row(response_timestamp=None)])
    hits = [e for e in log.errors if "QC-077" in e]
    assert hits, "a quote with no response_timestamp did not raise QC-077"
    assert "OL-USA RESPONSES" in hits[0], (
        "the error must name the section the row is invisible in")


def test_qc077_silent_when_the_quote_is_dateable():
    log = _fire_phase6([_quoted_row()])
    assert not [e for e in log.errors if "QC-077" in e]


def test_qc077_ignores_standalone_bookings():
    """A stand_* row is a booking seen with no rate-response email at all;
    ingest.py:887 leaves response_timestamp None there DELIBERATELY. Five of
    the 29 rows found on 2026-07-30 were exactly that, and flagging them would
    be crying wolf over correct behaviour."""
    log = _fire_phase6([_quoted_row(rid="stand_260426", status="WIN",
                                    response_timestamp=None)])
    assert not [e for e in log.errors if "QC-077" in e], (
        "QC-077 fired on a standalone booking, which legitimately has no "
        "response_timestamp")


def test_qc077_does_not_name_another_check_in_its_message():
    """Test helpers and the governance ratchet both scan fired messages by
    substring, so quoting another 'QC-0xx' in prose makes that check look like
    it fired from here — it broke two QC-056 tests when this check was first
    written."""
    log = _fire_phase6([_quoted_row(response_timestamp=None)])
    msg = [e for e in log.errors if "QC-077" in e][0]
    others = {t for t in __import__("re").findall(r"QC-\d+", msg)} - {"QC-077"}
    assert not others, (
        f"QC-077's message names other checks {sorted(others)} — substring "
        "scanners cannot tell that apart from those checks firing")


def test_the_report_says_how_many_quotes_it_cannot_show():
    """An empty section that is honest about being incomplete beats one that
    looks complete and isn't. The report must never render 'No activity' over
    real OL work without saying so."""
    import gen_email
    undated = gen_email.undated_quotes(
        {"requests": [_quoted_row(response_timestamp=None)]})
    assert len(undated) == 1
    note = gen_email._undated_quotes_note(undated)
    assert "cannot be dated" in note
    assert "PENDING HILMAR" in note, (
        "the note must tell the reader where the quote DID go")
    assert gen_email._undated_quotes_note([]) == "", (
        "a clean day must not carry a scary empty warning")


def test_undated_quotes_excludes_standalones_like_the_check_does():
    """The report's filter and QC-077's filter must agree, or the count in the
    note contradicts the count in the audit."""
    import gen_email
    rows = [_quoted_row(rid="stand_1", response_timestamp=None),
            _quoted_row(rid="r2", response_timestamp=None)]
    got = gen_email.undated_quotes({"requests": rows})
    assert [r["request_id"] for r in got] == ["r2"]


# ── the dashboard is an offline artifact ───────────────────────────────────

def test_the_dashboard_fetches_nothing_from_the_network():
    """It ships as an EMAIL ATTACHMENT. It is opened from Outlook, often
    offline, often behind OL's proxy — so every byte it needs must already be
    in the file.

    This is not about the old font link being broken; it had a fallback stack
    and degraded gracefully. It is about rendering being DETERMINISTIC: the
    same document should look the same on Michael's laptop, on a plane, and on
    a locked-down client machine, rather than quietly picking a different
    typeface based on whether a CDN was reachable.
    """
    src = (SCRIPTS / "gen_dashboard.py").read_text(encoding="utf-8")
    for needle in ("fonts.googleapis.com", "fonts.gstatic.com",
                   "cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com"):
        assert needle not in src, (
            f"gen_dashboard.py pulls {needle} — the dashboard is opened as an "
            "email attachment and must be self-contained")


def test_the_dashboard_sets_a_mono_stack_for_figures():
    """Decimals that line up down a column is the single biggest readability
    win in the reference document, and it needs a real monospace stack rather
    than tabular-nums alone."""
    src = (SCRIPTS / "gen_dashboard.py").read_text(encoding="utf-8")
    assert "--mono:" in src, "no monospace custom property defined"
    assert "ui-monospace" in src, (
        "the mono stack should lead with ui-monospace so each OS picks its "
        "own best monospace face without a download")
    assert ".kpi .value" in src and "var(--mono)" in src, (
        "KPI figures must use the mono stack")


# ── recovering a quote's real send time (not inventing one) ────────────────

def _pc():
    import patch_carriers
    return patch_carriers


def test_a_recovered_rate_is_dated_from_the_email_it_came_from(monkeypatch):
    """THE FIX for the 29 undated quotes. ingest.py:1199-1200 sets rate and
    timestamp together; patch_carriers is the OTHER way a rate reaches a row
    and it only ever recovered the rate — so those rows could never appear in
    OL-USA RESPONSES."""
    PC = _pc()
    monkeypatch.setitem(PC._SENT_BY_IMID, "abc@ol", "2026-07-29T19:02:00Z")
    row = {"request_id": "r1"}
    assert PC._stamp_response_time(row, {"_src_imid": "abc@ol"}) is True
    assert row["response_timestamp"] == "2026-07-29T19:02:00Z", (
        "the timestamp must be the send time of the message the rate was "
        "parsed out of — not a sibling's, not a guess")


def test_an_undateable_quote_is_left_undated_rather_than_invented(monkeypatch):
    """An undated quote is honest; an invented turnaround is not. CLAUDE.md
    forbids fabricating values, and a wrong time-to-quote is worse than a
    missing one because it silently poisons the SLA metrics."""
    PC = _pc()
    PC._SENT_BY_IMID.pop("nope@ol", None)
    row = {"request_id": "r1"}
    assert PC._stamp_response_time(row, {"_src_imid": "nope@ol"}) is False
    assert row.get("response_timestamp") is None
    # and with no provenance at all
    assert PC._stamp_response_time(row, {"ol_rate": 3150.0}) is False
    assert row.get("response_timestamp") is None


def test_an_existing_response_time_is_never_overwritten(monkeypatch):
    """ingest's value comes from the matched rate response and is the better
    source. This is a backfill, not a correction."""
    PC = _pc()
    monkeypatch.setitem(PC._SENT_BY_IMID, "abc@ol", "2026-07-29T19:02:00Z")
    row = {"request_id": "r1", "response_timestamp": "2026-07-28T12:00:00Z"}
    assert PC._stamp_response_time(row, {"_src_imid": "abc@ol"}) is False
    assert row["response_timestamp"] == "2026-07-28T12:00:00Z"


def test_the_body_loader_harvests_the_send_time(tmp_path, monkeypatch):
    """refresh_stage.py:546 has always written `sent` into the bodies file.
    Nothing read it until 2026-07-30, which is the entire reason recovered
    rates were undateable."""
    import json as _json
    PC = _pc()
    bodies = tmp_path / "scripts" / "stage_emails_bodies.txt"
    bodies.parent.mkdir(parents=True)
    bodies.write_text(_json.dumps({
        "imid": "<xyz@ol>", "text_body": "rate USD 3,150 CMA CGM",
        "sent": "2026-07-29T19:02:00Z"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(PC, "ROOT", tmp_path)
    PC._SENT_BY_IMID.clear()
    out = PC._load_bodies_by_imid()
    assert "xyz@ol" in out, "body not indexed"
    assert PC._SENT_BY_IMID.get("xyz@ol") == "2026-07-29T19:02:00Z", (
        "the send time on disk was not harvested — recovered rates will stay "
        "undateable and invisible in OL-USA RESPONSES")


def test_every_rate_recovery_dates_the_quote():
    """THE WIRING, not the helper.

    Deleting the _stamp_response_time call from patch_carriers' rate paths
    left all 2135 tests green — every other test here calls the helper
    directly, so nothing exercised the one thing that makes it run in
    production. That is the identical gap that let a deleted
    _warn_if_undeliverable call site pass earlier the same day.

    Rule: every write of r["ol_rate"] in patch_carriers must be followed
    immediately by an attempt to date it. A recovered rate with no timestamp
    is invisible in OL-USA RESPONSES forever.
    """
    import ast

    src = (SCRIPTS / "patch_carriers.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    rate_writes, stamp_calls = [], []
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Subscript)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "ol_rate"):
                    rate_writes.append(n.lineno)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "_stamp_response_time"):
            stamp_calls.append(n.lineno)

    assert rate_writes, (
        "no r['ol_rate'] assignment found in patch_carriers — this guard has "
        "gone inert and would pass on anything")

    undated = [ln for ln in rate_writes
               if not any(0 < c - ln <= 4 for c in stamp_calls)]
    assert not undated, (
        f"r['ol_rate'] is written at line(s) {undated} without a following "
        "_stamp_response_time call. That rate can never appear in OL-USA "
        "RESPONSES — see QC-077.")
