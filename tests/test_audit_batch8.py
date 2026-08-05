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

import json
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


def _dash_cfg():
    return json.loads((ROOT / "config.json").read_text())


def _dash_data():
    return json.loads((ROOT / "tests" / "fixtures" / "golden_day.json").read_text())


def test_the_dashboard_sets_a_mono_stack_for_figures():
    """Decimals that line up down a column is the single biggest readability
    win in the reference document, and it needs a real monospace stack rather
    than tabular-nums alone.

    Asserts on the RENDERED dashboard, not on gen_dashboard.py's source. The
    first version of this test scanned the source for "ui-monospace" and went
    red the moment the stack was centralized into branding.DOC_MONO_STACK —
    even though the emitted CSS was byte-identical. A source-substring test
    fails on a refactor that changes nothing a reader sees, and passes on a
    definition that is never emitted. What matters is what the file contains
    when it lands in Michael's inbox.
    """
    import gen_dashboard
    html = gen_dashboard.render(_dash_cfg(), _dash_data())
    assert "--mono:" in html, "no monospace custom property in the output"
    assert "ui-monospace" in html, (
        "the mono stack should lead with ui-monospace so each OS picks its "
        "own best monospace face without a download")
    assert ".kpi .value" in html and "var(--mono)" in html, (
        "KPI figures must use the mono stack")


def test_the_dashboard_paints_the_warm_paper_ground():
    """The one token that makes the dashboard, the PDF and the email read as
    one document family rather than three house styles."""
    import branding
    import gen_dashboard
    html = gen_dashboard.render(_dash_cfg(), _dash_data())
    assert branding.DOC_PAPER in html
    assert branding.DOC_LINE in html


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


# EVERY module that can recover a rate onto an existing row. Scoping this to
# patch_carriers alone was the miss: qc_selfheal._heal_missing_rate recovers
# rates by a different route and never dated them, so #140 fixed one of two
# paths while the undated count kept climbing (29 on 07-30 → 41 on 08-05).
# Adding a module here is how a new recovery route gets covered.
RATE_RECOVERY_MODULES = {
    "patch_carriers.py": "_stamp_response_time",
    "qc_selfheal.py": "_stamp_response_time_from_bodies",
}


@pytest.mark.parametrize("module,stamper", sorted(RATE_RECOVERY_MODULES.items()))
def test_every_rate_recovery_dates_the_quote(module, stamper):
    """THE WIRING, not the helper.

    Deleting the _stamp_response_time call from patch_carriers' rate paths
    left all 2135 tests green — every other test here calls the helper
    directly, so nothing exercised the one thing that makes it run in
    production. That is the identical gap that let a deleted
    _warn_if_undeliverable call site pass earlier the same day.

    Rule: every write of r["ol_rate"] must be followed immediately by an
    attempt to date it. A recovered rate with no timestamp is invisible in
    OL-USA RESPONSES forever.

    2026-08-05: parametrized over every recovery module. The first version
    hard-coded patch_carriers, so it was green the entire time
    qc_selfheal._heal_missing_rate was recovering rates and leaving them
    undated — a guard that checks one of two doors reads exactly like a guard
    that checks the building.
    """
    import ast

    src = (SCRIPTS / module).read_text(encoding="utf-8")
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
              and n.func.id == stamper):
            stamp_calls.append(n.lineno)

    assert rate_writes, (
        f"no r['ol_rate'] assignment found in {module} — this guard has "
        "gone inert and would pass on anything")

    # The NQ-contamination heal writes the sentinel string "Not Quoted" into
    # ol_rate on purpose; that is a marker, not a recovered quote, so it has
    # nothing to date and is exempt from the stamp rule.
    #
    # The exemption is BY VALUE, against qc_selfheal's own sentinel list.
    # The first version exempted ANY string constant, which is an escape hatch
    # rather than an exemption: a future recovery path writing a real rate as a
    # string — r["ol_rate"] = "2040" — would have been waved through undated,
    # and this guard exists precisely to stop that. Caught in review of #148.
    # Reading the list from qc_selfheal rather than restating it here means
    # adding a sentinel there cannot silently widen this exemption to something
    # the check no longer treats as a non-rate.
    import qc_selfheal as _QC
    _sentinels = {s.strip().lower() for s in _QC._NON_RATE_SENTINELS}

    def _is_sentinel_write(lineno):
        for n in ast.walk(tree):
            if (isinstance(n, ast.Assign) and n.lineno == lineno
                    and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str)
                    and n.value.value.strip().lower() in _sentinels):
                return True
        return False

    undated = [ln for ln in rate_writes
               if not any(0 < c - ln <= 6 for c in stamp_calls)
               and not _is_sentinel_write(ln)]
    assert not undated, (
        f"r['ol_rate'] is written in {module} at line(s) {undated} without a "
        f"following {stamper} call. That rate can never appear in OL-USA "
        "RESPONSES — see QC-077.")


def test_the_nq_sentinel_is_not_counted_as_an_undateable_quote():
    """QC-077's number has to be trustworthy — Michael reads it in the audit.

    qc_selfheal writes the STRING "Not Quoted" into ol_rate as an NQ marker,
    and the check tested `ol_rate is not None`, so every NQ-contaminated row
    counted as a quote that could not be dated. Same class of false positive
    as the stand_* rows the check already excludes, and it inflated the count
    Michael called unacceptable on 2026-08-05.
    """
    import qc_selfheal as QC
    for sentinel in (None, "", "Not Quoted", "N/A", "n/a", "  none  ", "—"):
        assert not QC._is_real_rate(sentinel), f"{sentinel!r} is not a rate"
    for real in (1234, 4874.0, "$2,040/20DV", "725"):
        assert QC._is_real_rate(real), f"{real!r} IS a rate"


def test_an_undated_quote_is_dated_from_its_own_source_message():
    """The fix for the 41. These rows carry source_imids pointing at the OL
    messages their rates were parsed from, and those messages have a
    sentDateTime that was sitting unused."""
    import qc_selfheal as QC
    bodies = {"m1@ol": {"sent": "2026-07-29T19:02:00Z", "text_body": "x"}}
    row = {"request_id": "r1", "ol_rate": 4874, "source_imids": ["m1@ol"]}
    QC._heal_undated_quote(QC.Log(), "r1", row, bodies)
    assert row["response_timestamp"] == "2026-07-29T19:02:00Z"


def test_an_undateable_quote_is_still_left_undated():
    """Recovery, not fabrication. No send time means the quote stays undated
    rather than getting a synthesised turnaround (CLAUDE.md)."""
    import qc_selfheal as QC
    row = {"request_id": "r1", "ol_rate": 4874, "source_imids": ["missing@ol"]}
    QC._heal_undated_quote(QC.Log(), "r1", row, {})
    assert "response_timestamp" not in row or not row["response_timestamp"]


def test_a_standalone_booking_is_never_back_dated():
    """ingest.py leaves response_timestamp None on stand_* rows DELIBERATELY
    to signal 'no rate response was ever seen'. Filling it erases the
    signal — the same reason QC-077 excludes them."""
    import qc_selfheal as QC
    bodies = {"m1@ol": {"sent": "2026-07-29T19:02:00Z"}}
    row = {"request_id": "stand_260928", "ol_rate": 4874, "source_imids": ["m1@ol"]}
    QC._heal_undated_quote(QC.Log(), "stand_260928", row, bodies)
    assert not row.get("response_timestamp")


def test_the_send_time_falls_back_across_schema_versions():
    """refresh_stage has moved the field name across versions, and an inbound
    copy of a message can carry `received` without `sent`. Reading only "sent"
    would leave rows undated for a reason unrelated to the data being absent —
    patch_carriers already falls back this way and the two must agree."""
    import qc_selfheal as QC
    for field in ("sent", "sentDateTime", "received"):
        row = {"request_id": "r1", "ol_rate": 4874, "source_imids": ["m@ol"]}
        QC._heal_undated_quote(QC.Log(), "r1", row, {"m@ol": {field: "2026-07-29T19:02:00Z"}})
        assert row.get("response_timestamp") == "2026-07-29T19:02:00Z", (
            f"send time in field {field!r} was not read")


def test_the_first_imid_with_a_send_time_wins():
    """A row can link several messages; the first that carries a time dates
    it. An earlier link with no timestamp must not abort the search."""
    import qc_selfheal as QC
    row = {"request_id": "r1", "ol_rate": 4874,
           "source_imids": ["nothing@ol", "m2@ol"]}
    QC._heal_undated_quote(QC.Log(), "r1", row,
                           {"nothing@ol": {"text_body": "x"},
                            "m2@ol": {"sent": "2026-07-30T12:00:00Z"}})
    assert row["response_timestamp"] == "2026-07-30T12:00:00Z"


def test_the_nq_sentinel_row_is_not_dated_either():
    """A row marked Not Quoted has no quote, so there is nothing to date."""
    import qc_selfheal as QC
    bodies = {"m1@ol": {"sent": "2026-07-29T19:02:00Z"}}
    row = {"request_id": "r1", "ol_rate": "Not Quoted", "source_imids": ["m1@ol"]}
    QC._heal_undated_quote(QC.Log(), "r1", row, bodies)
    assert not row.get("response_timestamp")


# ── the pause switch (Michael 2026-07-30: "pause all hilmar reports") ──────

WORKFLOWS = ROOT / ".github" / "workflows"


def _wf(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8"))


def _wf_text(name: str) -> str:
    return (WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8")


def _paused(name: str):
    return (_wf(name).get("env") or {}).get("HILMAR_REPORTS_PAUSED")


def _gate_code(name: str) -> str:
    """The gate step's shell with COMMENT LINES STRIPPED.

    These tests scan for identifiers like HILMAR_REPORTS_PAUSED and locate the
    branch around them. The gate is heavily commented — deliberately, it is
    the most dangerous step in the repo — so the first textual occurrence of
    any identifier is usually in prose, not in code. Anchoring on prose means
    the test measures how the comment is worded, and it went red on
    2026-08-04 for exactly that: a comment explaining the pause was added
    above the branch and pushed `proceed=false` outside the scan window,
    while the shell was untouched.

    Same family as the QC-ID substring scanners: an identifier in prose is
    indistinguishable from an identifier in code unless you strip one.
    """
    job = "gate" if name == "daily" else list(_wf(name)["jobs"])[0]
    run = _wf(name)["jobs"][job]["steps"][0]["run"]
    return "\n".join(ln for ln in run.splitlines() if not ln.lstrip().startswith("#"))


def test_the_daily_and_weekly_share_one_pause_switch():
    """One switch, one word to flip. A pause spread across several files is a
    pause somebody half-resumes."""
    assert _paused("daily") is not None, "daily.yml has no pause switch"
    assert _paused("weekly") is not None, "weekly.yml has no pause switch"
    assert _paused("daily") == _paused("weekly"), (
        f"daily is {_paused('daily')!r} but weekly is {_paused('weekly')!r} — "
        "one report would resume without the other")


def test_liveness_agrees_with_the_daily_about_whether_reports_are_paused():
    """liveness.yml holds its own literal rather than parsing daily.yml, so
    the only thing keeping them honest is this test. Drift means either the
    watchdog pages about an intentional silence, or it stays asleep after the
    reports resume — and the second is the dangerous one."""
    live = _wf_text("liveness")
    assert "HILMAR_REPORTS_PAUSED" in live, "liveness has no pause guard"
    import re
    m = re.search(r'HILMAR_REPORTS_PAUSED:\s*"(\w+)"', live)
    assert m, "could not read liveness's pause literal"
    assert m.group(1) == _paused("daily"), (
        f"liveness says {m.group(1)!r}, daily says {_paused('daily')!r} — "
        "resume both or neither")


def test_the_pause_actually_suppresses_the_scheduled_fire():
    """Presence is not enforcement. The gate must READ the switch and set
    proceed=false, or the flag is decoration and the fire still sends."""
    gate = _gate_code("daily")
    assert "HILMAR_REPORTS_PAUSED" in gate, (
        "the daily gate never reads the pause switch — scheduled fires would "
        "still send")
    pause_at = gate.index('if [ "$HILMAR_REPORTS_PAUSED" = "true" ]')
    assert "proceed=false" in gate[pause_at:pause_at + 400], (
        "the pause branch does not set proceed=false")
    assert "exit 0" in gate[pause_at:pause_at + 400], (
        "the pause branch does not short-circuit — execution falls through "
        "into the branches that set proceed=true")


def test_a_manual_dispatch_still_works_while_paused():
    """Pausing must not remove the ability to send on purpose — otherwise the
    only way to ship a report is to edit and merge a workflow file."""
    gate = _wf("daily")["jobs"]["gate"]["steps"][0]["run"]
    assert 'github.event_name }}" != "schedule"' in gate, (
        "the manual-dispatch branch is gone")
    assert "proceed=true" in gate, "no path sets proceed=true any more"


def test_liveness_stands_down_rather_than_alarming_while_paused():
    """A watchdog that pages about a silence you chose is one you learn to
    ignore, and then it is worthless on the day something is genuinely
    wrong."""
    live = _wf_text("liveness")
    i = live.index('if [ "$HILMAR_REPORTS_PAUSED" = "true" ]')
    window = live[i:i + 700]
    assert "no_fire=false" in window, (
        "liveness does not clear no_fire while paused — it will alarm about "
        "the intentional silence")
    assert "exit 0" in window, "the pause branch does not short-circuit"


def test_pausing_removes_the_cron_triggers_not_just_the_flag():
    """Michael 2026-08-03: "make it stop  zero to go out".

    A gate is not enough on its own. The 2026-07-30 pause gated the schedule
    correctly, but the weekly still fired on 08-03 at 12:33 UTC because the
    RUN had already been created from a pre-pause commit — GitHub checks out
    the SHA at spawn time, so a gate merged 24 minutes later cannot help. The
    only thing that guarantees zero is having no trigger to spawn from.

    Written as a CONDITIONAL invariant rather than as "there are no crons".
    The first version asserted the absence outright, which was true while the
    hard stop was on and became a false failure the moment Michael said
    "crons back on" (2026-08-04) — a test that pins an operational state
    fails on the day the state legitimately changes, and teaches people to
    edit tests to ship. The rule that is true in BOTH states is the pairing:
    paused means flag AND no triggers; live means flag AND triggers.
    """
    for name in ("daily", "weekly"):
        on = _wf(name).get(True) or _wf(name).get("on")
        if _paused(name) == "true":
            assert "schedule" not in on, (
                f"{name}.yml is flagged paused but still has a cron trigger — "
                "a run can spawn from the pre-pause SHA and never see the gate")
        else:
            assert "schedule" in on, (
                f"{name}.yml is not paused but has no cron trigger — it would "
                "only ever run by hand, which is a silent outage")


def test_a_half_pause_is_impossible_across_the_two_report_workflows():
    """Both report workflows must be in the same state. Half-paused is how
    2026-08-03 happened, and it is invisible unless something checks."""
    states = {n: (_paused(n), "schedule" in (_wf(n).get(True) or _wf(n).get("on")))
              for n in ("daily", "weekly")}
    assert states["daily"] == states["weekly"], (
        f"daily and weekly disagree about whether reports are live: {states}")


def test_the_hard_stop_blocks_manual_dispatch_too():
    """"Zero to go out" means zero. The earlier pause deliberately left manual
    dispatch open so a deliberate send stayed possible; that is the right
    design for a pause and the wrong one for a hard stop."""
    for name in ("daily", "weekly"):
        run = _gate_code(name)
        i = run.index('if [ "$HILMAR_REPORTS_PAUSED" = "true" ]')
        before = run[:i]
        assert 'event_name }}" != "schedule"' not in before, (
            f"{name}.yml checks the dispatch branch BEFORE the hard stop — a "
            "manual dispatch would slip through and send")


def test_the_sentinel_exemption_cannot_wave_through_a_real_rate():
    """The exemption in test_every_rate_recovery_dates_the_quote must be BY
    VALUE, not "any string constant".

    Raised in review of #148 (Copilot, 2026-08-05) and correct: exempting any
    string-constant write means a future recovery path assigning a real rate
    as a string — r["ol_rate"] = "2040" — is silently excused from the
    must-stamp rule, and that rule is the only thing standing between a
    recovered quote and permanent invisibility in OL-USA RESPONSES.

    An escape hatch in a guard reads exactly like a guard. This is the third
    instance of that shape this week (the AST walk that was really a substring
    scan; the wiring guard scoped to one of two modules), so it gets its own
    test rather than a comment.
    """
    import ast

    import qc_selfheal as QC

    sentinels = {s.strip().lower() for s in QC._NON_RATE_SENTINELS}
    assert "not quoted" in sentinels, (
        "the NQ sentinel is missing from _NON_RATE_SENTINELS — the exemption "
        "would stop covering the one write it exists for")

    # A real rate written as a string must NOT be treated as a sentinel.
    for real in ('"2040"', '"$2,040/20DV"', '"725"'):
        tree = ast.parse(f'r["ol_rate"] = {real}')
        node = tree.body[0]
        assert node.value.value.strip().lower() not in sentinels, (
            f'{real} is a real rate and must never be exempted from the '
            f'must-stamp rule')

    # And the sentinel itself must still be exempt, or the guard goes red on
    # the deliberate NQ-contamination write.
    tree = ast.parse('r["ol_rate"] = "Not Quoted"')
    assert tree.body[0].value.value.strip().lower() in sentinels


# ── one predicate for "is there a real rate here" (Copilot, PR #149) ─────────

def test_the_report_note_and_the_audit_banner_agree_on_the_nq_sentinel():
    """The bug Copilot found on #148, and the invariant
    test_undated_quotes_excludes_standalones_like_the_check_does already
    stated in its own docstring: "The report's filter and QC-077's filter must
    agree, or the count in the note contradicts the count in the audit."

    #148 added _is_real_rate and routed QC-077 through it, so the banner
    stopped counting rows the NQ heal had stamped "Not Quoted". It left
    gen_email.undated_quotes — the twin consumer feeding the STAFF email's
    note — still spelling the test as `ol_rate is not None`, which that
    sentinel passes. Two counts, one dataset, and the tests all used numeric
    rates so nothing saw it.
    """
    import gen_email as GE
    import qc_selfheal as QC
    row = {"request_id": "r1", "ol_rate": "Not Quoted", "carrier_quoted": None}
    assert QC._is_real_rate(row["ol_rate"]) is False
    assert GE.undated_quotes({"requests": [row]}) == [], (
        "the staff email counts an NQ-sentinel row the audit banner excludes"
    )


def test_the_two_filters_agree_on_every_sentinel_not_just_the_one_we_saw():
    """Per-value, over the whole sentinel list. A test naming only
    "Not Quoted" proves only the case someone already hit."""
    import core
    import gen_email as GE
    for sentinel in core.NON_RATE_SENTINELS:
        row = {"request_id": "r", "ol_rate": sentinel}
        assert not core.has_quote_evidence(row)
        assert GE.undated_quotes({"requests": [row]}) == [], (
            f"ol_rate={sentinel!r} counted as a quote by the staff email")


def test_a_real_rate_or_a_carrier_still_counts():
    """The other direction — a fix that silences the note entirely would pass
    every assertion above and destroy the check."""
    import gen_email as GE
    assert len(GE.undated_quotes({"requests": [{"request_id": "a", "ol_rate": 3150.0}]})) == 1
    assert len(GE.undated_quotes({"requests": [{"request_id": "b", "carrier_quoted": "MSC"}]})) == 1


def test_the_quoted_flag_reconciler_uses_the_shared_predicate():
    """It carried its own three-sentinel list, so ol_rate="N/A" read as a real
    rate and would flip quoted=True on a row with no quote."""
    import core
    assert core.has_quote_evidence({"ol_rate": "N/A"}) is False
    assert core.has_quote_evidence({"ol_rate": "—"}) is False
    assert core.has_quote_evidence({"ol_rate": 3150.0}) is True


def test_no_module_rolls_its_own_rate_sentinel_list():
    """The ratchet. This predicate had FIVE spellings across two modules; #148
    added a sixth (the correct one) instead of replacing them, which is how
    the counts diverged. A new inline sentinel tuple is the same mistake."""
    import re
    ROOT_ = Path(__file__).resolve().parent.parent
    offenders = {}
    for path in sorted((ROOT_ / "scripts").glob("*.py")):
        if path.name == "core.py":
            continue          # where the list is defined
        hits = [
            f"line {i}: {line.strip()[:100]}"
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            # an ol_rate compared against a tuple/list that mentions the NQ
            # sentinel — i.e. a hand-rolled copy of NON_RATE_SENTINELS
            if re.search(r'ol_rate.*\b(?:not )?in \(.*"Not Quoted"', line)
        ]
        if hits:
            offenders[path.name] = hits
    # The NQ-contamination heal keeps its own guard on purpose: it asks "is
    # this ALREADY the sentinel", which is a different question with a
    # different answer for "—", and normalising is not detecting.
    offenders.pop("qc_selfheal.py", None)
    assert not offenders, (
        f"a module rolled its own rate-sentinel list: {offenders} — "
        "use core.is_real_rate / core.has_quote_evidence")


# ── QC-077's survivor split explains every survivor ─────────────────────────

def test_the_undated_reason_split_is_exhaustive():
    """Copilot on #149: _no_body tested whether the imid was in the bodies
    index AT ALL, but the heal needs sent/sentDateTime/received. A row whose
    message is cached but carries none of them was counted in neither bucket,
    so the banner's two numbers could sum to less than the total they claimed
    to explain — a diagnostic that adds up only because nobody checked."""
    import qc_selfheal as QC
    idx = {"ok": {"imid": "ok", "sent": "2026-04-01T10:00:00Z"},
           "timeless": {"imid": "timeless", "text_body": "..."}}
    rows = [
        {"ol_rate": 3150.0},                                    # no imids
        {"ol_rate": 3150.0, "source_imids": ["gone"]},          # not cached
        {"ol_rate": 3150.0, "source_imids": ["timeless"]},      # cached, no time
        {"ol_rate": 3150.0, "source_imids": ["ok"]},            # dateable
    ]
    labels = [QC._undated_reason(r, idx) for r in rows]
    assert labels == ["no_imids", "no_body", "no_send_time", "unexplained"]
    assert len(labels) == len(rows), "a row fell into no bucket"


def test_the_classifier_and_the_heal_read_the_same_fields():
    """They diverged because the classifier re-derived the heal's success
    condition. Both go through _body_send_time now, so they cannot."""
    import qc_selfheal as QC
    for field in QC._BODY_SEND_FIELDS:
        rec = {"imid": "m", field: "2026-04-01T10:00:00Z"}
        assert QC._body_send_time(rec)
        r = {"ol_rate": 3150.0, "source_imids": ["m"]}
        assert QC._undated_reason(r, {"m": rec}) == "unexplained", (
            f"the classifier ignores {field}, which the heal accepts")
        r2 = {"ol_rate": 3150.0, "source_imids": ["m"]}
        assert QC._stamp_response_time_from_bodies(r2, {"m": rec}) is True
