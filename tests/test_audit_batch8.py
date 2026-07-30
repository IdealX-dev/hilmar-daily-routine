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
