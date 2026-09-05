"""A capture_qc_* event's Sentry identity is its NAME, never the call path
(HILMAR-DAILY-TRACKER-K, 2026-09-05; review fixes the same day).

`sentry_setup.capture_qc_error`'s docstring said the check name "becomes the
issue fingerprint, so all instances of the same check failing group
together". The code set a tag and passed NO fingerprint; with
`attach_stacktrace=True` Sentry grouped every `capture_message` by the stack
of the ONE shared `Log.error -> capture_qc_error` path. Measured over 90 days
of `component:qc_selfheal`: issue TRACKER-3 held eight checks, TRACKER-K held
21 QC-073 events under a QC-072 title, and QC-039 was spread over three
issues because that stack moves whenever qc_selfheal.py gains a line.

The first fix carried its own gap: it fingerprinted only names matching a
QC-id regex of its own, so the two real non-QC callers (`pipeline.step_
failure`, `patch_carriers.ambiguous_match`) kept the shared-stack grouping —
the TRACKER-6 shape, a step's TIMEOUT and its `exit 39` under one title — and
the regex restated `qc_selfheal._extract_check_name`'s with nothing pinning
the pair.

These pin the property at the layer where it lives, both directions: two
names from the same call path never share a fingerprint; one name across
rows, fires and wordings always does; EVERY caller the AST finds in
scripts/ gets a deterministic fingerprint, and the only name that keeps
Sentry's default grouping is the prefix-less sentinel; the tag rides the
EVENT, so a LATER unrelated event carries no `qc_check`; per-event tags
MERGE with the process-scope tags the real `init()` sets. The real-SDK tests
run the production `init()` (a recording transport injected under it, every
default integration on) and drive `Log.error` with the two message shapes
issue K actually held.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sentry_setup  # noqa: E402

# The two message shapes HILMAR-DAILY-TRACKER-K actually held (event
# bb1664f7f5f543ee836d38f1ad32868d and the 21 QC-073 siblings), with a raw
# request id so the scrubber has something to redact.
_QC072_MSG = ("QC-072: request req_5ab6c8f0e1d2c3b4 — status=PENDING but "
              "status_history ends at QUOTED (at 2026-08-12T20:46:10Z)")
_QC072_MSG_OTHER_ROW = ("QC-072: request req_0f1e2d3c4b5a6978 — status=PENDING "
                        "but status_history ends at QUOTED (at 2026-08-12T20:57:02Z)")
_QC073_MSG = "QC-073: request stand_260928 — degenerate lane Oakland → Oakland"

# The two production callers that pass a NAME rather than a QC id. The AST
# scan below must FIND both (a superset assertion, so the scan cannot go
# vacuous on the sites this file exists for) and every name it finds must
# fingerprint on itself.
_PRODUCTION_NON_QC_NAMES = {"pipeline.step_failure", "patch_carriers.ambiguous_match"}


# ── fake-SDK layer: the kwargs capture_message receives ─────────────────────

def _fake_sdk(monkeypatch):
    fake = MagicMock()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    monkeypatch.setattr(sentry_setup, "_INITIALIZED", True)
    return fake


@pytest.mark.parametrize("fn,level", [
    (sentry_setup.capture_qc_error, "error"),
    (sentry_setup.capture_qc_warning, "warning"),
])
def test_the_check_name_is_the_fingerprint(monkeypatch, fn, level):
    fake = _fake_sdk(monkeypatch)
    fn("QC-072", _QC072_MSG)
    kw = fake.capture_message.call_args.kwargs
    assert kw["fingerprint"] == ["QC-072"]
    assert kw["level"] == level
    assert kw["tags"] == {"qc_check": "QC-072"}


def test_two_checks_from_the_same_call_path_never_share_a_fingerprint(monkeypatch):
    """The TRACKER-K shape: QC-072 and QC-073 fired from the identical
    `Log.error` stack and landed in one issue. Fingerprints must differ."""
    fake = _fake_sdk(monkeypatch)
    sentry_setup.capture_qc_error("QC-072", _QC072_MSG)
    sentry_setup.capture_qc_error("QC-073", _QC073_MSG)
    fps = [c.kwargs["fingerprint"] for c in fake.capture_message.call_args_list]
    assert fps == [["QC-072"], ["QC-073"]]
    assert fps[0] != fps[1]


def test_one_check_across_rows_and_messages_always_shares_a_fingerprint(monkeypatch):
    """One issue per CHECK, not per row and not per release: a different
    request id / timestamp / wording must not open a second issue (QC-039
    was spread over three issues by the stack-trace grouping)."""
    fake = _fake_sdk(monkeypatch)
    sentry_setup.capture_qc_error("QC-072", _QC072_MSG)
    sentry_setup.capture_qc_error("QC-072", _QC072_MSG_OTHER_ROW)
    sentry_setup.capture_qc_warning("QC-072", "QC-072: some other wording entirely")
    fps = {tuple(c.kwargs["fingerprint"]) for c in fake.capture_message.call_args_list}
    assert fps == {("QC-072",)}


def test_only_the_unknown_sentinel_keeps_sentry_default_grouping(monkeypatch):
    """`_extract_check_name` yields the sentinel for a prefix-less message
    (91 such log sites: "Data JSON MISSING at …"). Forcing a fingerprint
    there would fold every such message into ONE catch-all issue — worse
    than the default. It is the ONLY carve-out: a dotted non-QC name from a
    real caller fingerprints on itself like any check (the first cut of this
    function returned None for both production non-QC names, which kept
    them on the shared-stack grouping the function exists to kill)."""
    fake = _fake_sdk(monkeypatch)
    sentry_setup.capture_qc_warning(sentry_setup.QC_UNKNOWN_CHECK,
                                    "PARSER ACCURACY 97.2% — below 98%")
    kw = fake.capture_message.call_args.kwargs
    assert "fingerprint" not in kw
    assert kw["tags"] == {"qc_check": sentry_setup.QC_UNKNOWN_CHECK}   # still searchable
    assert sentry_setup.event_fingerprint(sentry_setup.QC_UNKNOWN_CHECK) is None
    assert sentry_setup.event_fingerprint("") is None
    assert sentry_setup.event_fingerprint("   ") is None
    assert sentry_setup.event_fingerprint(None) is None
    assert sentry_setup.event_fingerprint("QC-014a") == ["QC-014a"]
    for name in sorted(_PRODUCTION_NON_QC_NAMES):
        assert sentry_setup.event_fingerprint(name) == [name], name
        sentry_setup.capture_qc_error(name, "x")
        assert fake.capture_message.call_args.kwargs["fingerprint"] == [name]


def test_group_by_extends_the_fingerprint_and_drops_empty_parts():
    """`group_by` is how ONE name covers several defects: the parts are
    appended, stringified, and an empty / None part is dropped rather than
    spelled "None" into an issue key."""
    fp = sentry_setup.event_fingerprint("pipeline.step_failure", "Ingest", "exit 1")
    assert fp == ["pipeline.step_failure", "Ingest", "exit 1"]
    assert sentry_setup.event_fingerprint("pipeline.step_failure", None, "", "  ", 124) == \
        ["pipeline.step_failure", "124"]
    # The sentinel stays on default grouping even when a caller adds parts.
    assert sentry_setup.event_fingerprint(sentry_setup.QC_UNKNOWN_CHECK, "x") is None


def test_group_by_rides_capture_qc_error_into_the_event_fingerprint(monkeypatch):
    fake = _fake_sdk(monkeypatch)
    sentry_setup.capture_qc_error("pipeline.step_failure", "Ingest exited 1",
                                  group_by=("Ingest", "exit 1"))
    kw = fake.capture_message.call_args.kwargs
    assert kw["fingerprint"] == ["pipeline.step_failure", "Ingest", "exit 1"]
    assert kw["tags"] == {"qc_check": "pipeline.step_failure"}
    sentry_setup.capture_qc_warning("pipeline.step_failure", "Ingest exited 1")   # no group_by
    assert fake.capture_message.call_args.kwargs["fingerprint"] == ["pipeline.step_failure"]


def test_the_tag_rides_the_event_kwargs(monkeypatch):
    """Per-event: each capture carries its OWN check in `tags=`. (That a
    later event carries none is the live-SDK property test below — this
    fake layer can only see the kwargs, not what a later event inherits.)"""
    fake = _fake_sdk(monkeypatch)
    sentry_setup.capture_qc_error("QC-072", _QC072_MSG)
    sentry_setup.capture_qc_error("QC-073", _QC073_MSG)
    tags = [c.kwargs["tags"]["qc_check"] for c in fake.capture_message.call_args_list]
    assert tags == ["QC-072", "QC-073"]


def test_summary_already_prefixed_is_not_prefixed_twice(monkeypatch):
    """Issue K's title read `QC-072: QC-072: request ...` — the Log message
    carries its own prefix and capture_qc_error added a second."""
    fake = _fake_sdk(monkeypatch)
    sentry_setup.capture_qc_error("QC-072", _QC072_MSG)
    assert fake.capture_message.call_args.args[0] == _QC072_MSG
    sentry_setup.capture_qc_error("QC-039", "accuracy 97.2%")
    assert fake.capture_message.call_args.args[0] == "QC-039: accuracy 97.2%"


def test_noop_before_init_and_never_raises(monkeypatch):
    fake = _fake_sdk(monkeypatch)
    monkeypatch.setattr(sentry_setup, "_INITIALIZED", False)
    sentry_setup.capture_qc_error("QC-072", _QC072_MSG)
    assert not fake.capture_message.called
    monkeypatch.setattr(sentry_setup, "_INITIALIZED", True)
    fake.capture_message.side_effect = RuntimeError("transport down")
    sentry_setup.capture_qc_error("QC-072", _QC072_MSG)   # must not raise


# ── every caller, enumerated by AST — the docstring's claim is a scan ───────

_CAPTURE_FNS = {"capture_qc_error", "capture_qc_warning"}


def _capture_qc_call_sites():
    """Every `capture_qc_error` / `capture_qc_warning` CALL under scripts/
    (sentry_setup.py's own definitions excluded), as (file, line, name-arg
    node, keyword names). Attribute calls (`_sentry.capture_qc_error`) and
    bare-name calls both count — a caller that imports the function
    directly must not slip past the scan."""
    sites = []
    for path in sorted(SCRIPTS.glob("*.py")):
        if path.name == "sentry_setup.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            attr = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else None)
            if attr not in _CAPTURE_FNS:
                continue
            first = node.args[0] if node.args else None
            sites.append((path.name, node.lineno, first, {k.arg for k in node.keywords}))
    return sites


def _literal_name(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_extract_check_name_call(node) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "_extract_check_name")


def test_every_capture_qc_caller_gets_a_deterministic_fingerprint():
    """The docstring says every caller groups per name. This is that claim
    as a scan: every call site in scripts/ names its event with either a
    string literal (which must fingerprint on itself) or qc_selfheal's
    `_extract_check_name(msg)` (the QC path, pinned by the round-trip test
    below). Floors keep it from passing on an empty scan, and the two
    production non-QC names must be among what it found."""
    sites = _capture_qc_call_sites()
    assert len(sites) >= 4, sites
    literal, derived, other = [], [], []
    for file, line, first, _kw in sites:
        name = _literal_name(first)
        if name is not None:
            literal.append((file, line, name))
        elif _is_extract_check_name_call(first):
            derived.append((file, line))
        else:
            other.append((file, line, ast.dump(first) if first is not None else None))
    assert not other, f"capture_qc_* called with a name this scan cannot judge: {other}"
    assert len(literal) >= 2 and len(derived) >= 2, (literal, derived)
    names = {n for _f, _l, n in literal}
    assert names >= _PRODUCTION_NON_QC_NAMES, names
    for file, line, name in literal:
        assert sentry_setup.event_fingerprint(name) == [name], f"{file}:{line} {name!r}"
        assert name != sentry_setup.QC_UNKNOWN_CHECK, f"{file}:{line} passes the sentinel literally"


def test_the_step_failure_caller_discriminates_by_step():
    """`pipeline.step_failure` is ONE name for ~25 steps × two failure
    kinds; the run_pipeline site must pass `group_by`, or every step's
    failure would share one issue (the TRACKER-6 shape)."""
    step_sites = [(f, ln, kw) for f, ln, first, kw in _capture_qc_call_sites()
                  if _literal_name(first) == "pipeline.step_failure"]
    assert len(step_sites) >= 1, "the run_pipeline step-failure capture was not found"
    for file, line, kw in step_sites:
        assert "group_by" in kw, f"{file}:{line} captures pipeline.step_failure without group_by"


def test_run_pipeline_groups_a_timeout_and_an_exit_code_as_two_issues(monkeypatch):
    """EXECUTED through the real run_step and the real capture composition:
    the same step timing out and exiting 39 (the QC-039 gate) yield two
    fingerprints; two timeouts of the same step — even at different
    budgets, which change the message — share one."""
    import run_pipeline as RP
    fake = _fake_sdk(monkeypatch)
    monkeypatch.setattr(RP, "_sentry", sentry_setup)
    step = "QC self-heal (post-patch)"
    calls = {"mode": "timeout"}

    def fake_run(*a, **kw):
        if calls["mode"] == "timeout":
            raise subprocess.TimeoutExpired(cmd=a[0] if a else kw.get("cmd"),
                                            timeout=kw.get("timeout", 0))
        return subprocess.CompletedProcess(args=a[0] if a else kw.get("cmd"), returncode=39)

    monkeypatch.setattr(RP.subprocess, "run", fake_run)
    monkeypatch.setitem(RP.STEP_TIMEOUTS_S, step, 180)
    assert RP.run_step(step, ["true"]) == 124
    monkeypatch.setitem(RP.STEP_TIMEOUTS_S, step, 240)      # budget raised → message changes
    assert RP.run_step(step, ["true"]) == 124
    calls["mode"] = "exit"
    assert RP.run_step(step, ["true"]) == 39
    assert RP.run_step("Ingest", ["true"]) == 39

    sent = fake.capture_message.call_args_list
    assert [c.args[0] for c in sent] == [
        f"pipeline.step_failure: {step} TIMEOUT @ 180s",
        f"pipeline.step_failure: {step} TIMEOUT @ 240s",
        f"pipeline.step_failure: {step} exited 39",
        "pipeline.step_failure: Ingest exited 39",
    ]
    fps = [c.kwargs["fingerprint"] for c in sent]
    assert fps[0] == fps[1] == ["pipeline.step_failure", step, "timeout"]
    assert fps[2] == ["pipeline.step_failure", step, "exit 39"]
    assert fps[3] == ["pipeline.step_failure", "Ingest", "exit 39"]
    assert len({tuple(f) for f in fps}) == 3
    assert all(c.kwargs["tags"] == {"qc_check": "pipeline.step_failure"} for c in sent)


# ── the QC-id shape has ONE owner; the round trip is the pin ────────────────

def _qc_ids_spelled_in_qc_selfheal() -> set[str]:
    """Every QC id qc_selfheal.py spells (base ids and the sub-variant
    letters — QC-014a/b, QC-020a/b, QC-027b are live). Derived from the
    emitter's source, so a check added tomorrow joins the corpus tomorrow."""
    src = (SCRIPTS / "qc_selfheal.py").read_text(encoding="utf-8")
    return set(re.findall(r"QC-\d{3}[a-z]?(?![A-Za-z0-9])", src))


def test_every_spelled_check_id_round_trips_to_its_own_fingerprint():
    """`sentry_setup` carried its own copy of the check-id regex and
    returned None (default grouping) for anything it did not match — so a
    widening of the owner (`[a-zA-Z]?`) would have silently put those
    checks back on stack grouping with nothing red. Now the emitter's regex
    is the ONLY one, and every id the emitter spells must come back out of
    `_extract_check_name` unchanged and fingerprint on itself."""
    import qc_selfheal as q
    ids = _qc_ids_spelled_in_qc_selfheal()
    assert len(ids) >= 80, len(ids)                     # measured 88 on 2026-09-05
    assert {"QC-014a", "QC-014b", "QC-020a", "QC-020b", "QC-027b"} <= ids
    for cid in sorted(ids):
        assert q._extract_check_name(f"{cid}: something broke") == cid, cid
        assert q._extract_check_name(f"  {cid} leading blanks") == cid, cid
        assert sentry_setup.event_fingerprint(cid) == [cid], cid


def test_the_sentinel_is_derived_from_its_owner_and_is_the_only_default_grouping(monkeypatch):
    """qc_selfheal READS the sentinel from sentry_setup at call time. The
    pin is behavioural: move the owner's value and the emitter follows, and
    the fingerprint layer still recognises what the emitter emits. A local
    re-spelling in either module goes red here (the first cut of this test
    compared module attributes and stayed green under exactly that)."""
    import qc_selfheal as q
    for msg in ("Data JSON MISSING at /tmp/x", "'requests' is not an array", ""):
        assert q._extract_check_name(msg) == sentry_setup.QC_UNKNOWN_CHECK, msg
    assert sentry_setup.event_fingerprint(q._extract_check_name("no prefix here")) is None
    monkeypatch.setattr(sentry_setup, "QC_UNKNOWN_CHECK", "QC-none-for-this-test")
    assert q._extract_check_name("no prefix here") == "QC-none-for-this-test"
    assert sentry_setup.event_fingerprint(q._extract_check_name("no prefix here")) is None
    assert sentry_setup.event_fingerprint("QC-unknown") == ["QC-unknown"]   # no longer the sentinel
    monkeypatch.undo()
    # ... and NOTHING else the system names goes to default grouping.
    names = _qc_ids_spelled_in_qc_selfheal() | _PRODUCTION_NON_QC_NAMES
    on_default = [n for n in names if sentry_setup.event_fingerprint(n) is None]
    assert on_default == [], on_default


def test_a_widened_owner_still_fingerprints_what_it_extracts():
    """The reviewer's mutation, run rather than reasoned about: if the
    owner ever widens to an upper-case sub-variant, the fingerprint layer
    follows — it has no regex of its own to disagree with."""
    import qc_selfheal as q
    assert not hasattr(sentry_setup, "_QC_CHECK_ID_RX")
    assert q.QC_CHECK_ID_RX.match("QC-014a: x").group(1) == "QC-014a"
    for weird in ("QC-014A", "QC-1", "QC-0001z"):
        assert sentry_setup.event_fingerprint(weird) == [weird]


# ── real-SDK layer: what actually leaves the box, via the production path ──

sentry_sdk = pytest.importorskip("sentry_sdk")


class _RecordingTransport(sentry_sdk.transport.Transport):
    """Synchronous capture — the client hands the envelope straight here,
    no worker thread, no network."""

    def __init__(self):
        super().__init__()
        self.events = []

    def capture_envelope(self, envelope):
        ev = envelope.get_event()
        if ev is not None:
            self.events.append(ev)


@pytest.fixture
def live_sdk(monkeypatch):
    """The REAL `sentry_setup.init("qc_selfheal")` — its options, its
    post-init process-scope tags and context, every default integration
    production runs with — over a recording transport injected under
    `sentry_sdk.init`. Not a copy of the options: the one code path
    production takes, so a tag `init()` sets is a tag these events carry."""
    transport = _RecordingTransport()
    real_init = sentry_sdk.init

    def init_with_recorder(**kw):
        return real_init(**kw, transport=transport)

    monkeypatch.setattr(sentry_sdk, "init", init_with_recorder)
    monkeypatch.setattr(sentry_setup, "_load_dsn", lambda: "https://public@example.invalid/1")
    monkeypatch.setattr(sentry_setup, "_INITIALIZED", False)
    monkeypatch.delenv("HILMAR_QC_PHASE", raising=False)
    sentry_sdk.get_isolation_scope().clear()          # no tag bleeds in from a prior test
    assert sentry_setup.init("qc_selfheal", sample_rate=0) is True
    yield transport
    sentry_sdk.get_isolation_scope().clear()
    real_init(dsn=None, default_integrations=False)   # unbind the recorder


def test_production_log_error_path_emits_one_fingerprint_per_check(live_sdk):
    """Drive qc_selfheal's real `Log.error` (the ONE call path every check
    shares) with the two shapes issue K held, and read the events back."""
    import qc_selfheal as q
    log = q.Log()
    log.error(_QC072_MSG)
    log.error(_QC072_MSG_OTHER_ROW)
    log.error(_QC073_MSG)
    ev72a, ev72b, ev73 = live_sdk.events
    # The precondition that produced the defect is reproduced: every event
    # carries the attached stack (Sentry's default grouping key) ...
    assert all("threads" in e for e in (ev72a, ev72b, ev73))
    # ... and every event now carries an explicit per-check fingerprint.
    assert ev72a["fingerprint"] == ev72b["fingerprint"] == ["QC-072"]
    assert ev73["fingerprint"] == ["QC-073"]
    assert ev72a["fingerprint"] != ev73["fingerprint"]
    assert [e["tags"]["qc_check"] for e in (ev72a, ev72b, ev73)] == ["QC-072", "QC-072", "QC-073"]
    assert [e["level"] for e in (ev72a, ev72b, ev73)] == ["error"] * 3


def test_per_event_tags_merge_with_the_process_scope_tags(live_sdk):
    """`tags=` on capture_message must MERGE with what `init()` put on the
    process scope, never replace it: `component:qc_selfheal` is the filter
    every 90-day measurement in the K commit used and what
    qc_actions_from_sentry's whole analysis rests on. Pinned against the
    real init, not a hand-written copy of its set_tag calls."""
    sentry_setup.capture_qc_error("QC-072", _QC072_MSG)
    (ev,) = live_sdk.events
    tags = ev["tags"]
    assert tags["component"] == "qc_selfheal"
    assert tags["pipeline_run_id"] == sentry_setup._RUN_ID
    assert tags["python_version"] == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert tags["qc_check"] == "QC-072"
    assert ev["fingerprint"] == ["QC-072"]
    assert ev["contexts"]["hilmar"]["component"] == "qc_selfheal"
    assert ev["release"].startswith("hilmar-daily-tracker@")
    # What the header docstring used to claim and init() never set.
    assert "git_sha" not in tags


def test_a_later_unrelated_event_carries_no_qc_check_tag(live_sdk):
    """THE property behind "per-event, never on the process scope": after a
    QC capture, a later step failure — or any later event — must not wear
    the check's id, because `qc_actions_from_sentry._action_lookup` routes
    a whole issue (auto_resolve_safe included) off that tag. Any spelling
    of a scope write (`set_tag`, `get_isolation_scope().set_tag`, …) goes
    red here; the process-scope tags must still be there, so this cannot
    pass by wiping the scope."""
    sentry_setup.capture_qc_error("QC-072", _QC072_MSG)
    sentry_sdk.capture_message("some later step failure", level="error")
    sentry_setup.capture_step_failure("Ingest", RuntimeError("boom"))
    qc, later, step = live_sdk.events
    assert qc["tags"]["qc_check"] == "QC-072"
    assert "qc_check" not in later.get("tags", {}), later.get("tags")
    assert "qc_check" not in step.get("tags", {}), step.get("tags")
    assert step["tags"]["pipeline_step"] == "Ingest"
    assert all(e["tags"]["component"] == "qc_selfheal" for e in (qc, later, step))
    assert "fingerprint" not in later


def _without_source_context(ev):
    """The event with the frames' source listing removed — code, not data."""
    ev = json.loads(json.dumps(ev, default=str))
    for key in ("exception", "threads"):
        for v in (ev.get(key) or {}).get("values") or []:
            for fr in (v.get("stacktrace") or {}).get("frames") or []:
                for k in ("context_line", "pre_context", "post_context"):
                    fr.pop(k, None)
    return ev


def _frames(ev):
    """Every frame on the event, whichever interface holds the stack
    (`exception` for a raised error, `threads` for a message)."""
    out = []
    for key in ("exception", "threads"):
        for v in (ev.get(key) or {}).get("values") or []:
            out.extend((v.get("stacktrace") or {}).get("frames") or [])
    return out


def test_production_path_still_scrubs_and_titles_once(live_sdk):
    """The fingerprint kwarg must not bypass `_before_send`: the raw request
    id is redacted, and the title is not double-prefixed."""
    import qc_selfheal as q
    q.Log().error(_QC072_MSG)
    (ev,) = live_sdk.events
    assert ev["message"].startswith("QC-072: request [REQ_ID]")
    assert not ev["message"].startswith("QC-072: QC-072:")
    assert "req_5ab6c8f0e1d2c3b4" not in ev["message"]


def test_the_attached_stack_carries_no_locals(live_sdk):
    """STANDARDS §6 mandates `with_locals=False`; `init()` never set it.
    Measured 2026-09-05 on this exact path: the event's `message` read
    `[REQ_ID] … [EMAIL_REDACTED]` while its `threads` frame locals (`msg`,
    `summary`, `text`) carried the raw request id and a raw email address
    three times. The stack still attaches (Sentry's default grouping key,
    and useful context); the locals do not. The frame count is floored so
    "no locals" cannot be satisfied by "no frames"."""
    import qc_selfheal as q
    q.Log().error(_QC072_MSG.replace("req_5ab6c8f0e1d2c3b4",
                                     "req_5ab6c8f0e1d2c3b4 for lupfold@hilmaringredients.com"))
    (ev,) = live_sdk.events
    assert "threads" in ev
    frames = _frames(ev)
    assert len(frames) >= 1, "the stack attached no frames — nothing here was tested"   # measured 3
    assert [fr for fr in frames if fr.get("vars")] == []
    # Everything but the frames' source LISTING (`context_line` & co. quote
    # this test's own code, which spells the address) must be clean.
    assert "lupfold@hilmaringredients.com" not in json.dumps(_without_source_context(ev))


def test_a_logger_error_ships_no_email_anywhere_in_the_event(live_sdk):
    """The stdlib LoggingIntegration (a DEFAULT integration — on in
    production) files a `logging.error` under `logentry`, which sat in the
    fail-closed drop list and was never scrubbed on the normal path.
    Measured 2026-09-05 with the real SDK: `logentry.message`,
    `.formatted` and `.params` all carried the raw address. The address
    and the request id are assembled at runtime so no source line spells
    them, and the WHOLE event JSON — source context included — is walked."""
    addr = "lupfold@" + "hilmaringredients.com"
    req = "req_" + "5ab6c8f0e1d2c3b4"
    logger = logging.getLogger("hilmar.test.logentry")
    logger.error("row " + req + " for " + addr + " failed")           # pre-formatted
    logger.error("row %s for %s failed", req, addr)                    # params form
    literal, params = live_sdk.events
    for ev in (literal, params):
        assert "logentry" in ev, "the LoggingIntegration did not produce a logentry — precondition lost"
        blob = json.dumps(ev, default=str)
        assert addr not in blob
        assert req not in blob
        assert "[EMAIL_REDACTED]" in json.dumps(ev["logentry"])
    assert params["logentry"]["params"] == ["[REQ_ID]", "[EMAIL_REDACTED]"]
    assert params["logentry"]["formatted"] == "row [REQ_ID] for [EMAIL_REDACTED] failed"


def test_pre_patch_phase_still_sends_nothing(live_sdk, monkeypatch):
    """Unchanged contract: the pre-patch pass is expected-incomplete and
    must not page (patch_carriers runs next)."""
    import qc_selfheal as q
    monkeypatch.setenv("HILMAR_QC_PHASE", "pre-patch")
    q.Log().error(_QC072_MSG)
    assert live_sdk.events == []
