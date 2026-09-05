"""A QC finding's Sentry identity is the CHECK, never the call path
(HILMAR-DAILY-TRACKER-K, 2026-09-05).

`sentry_setup.capture_qc_error`'s docstring said the check name "becomes the
issue fingerprint, so all instances of the same check failing group
together". The code set a tag and passed NO fingerprint; with
`attach_stacktrace=True` Sentry grouped every `capture_message` by the stack
of the ONE shared `Log.error -> capture_qc_error` path. Measured over 90 days
of `component:qc_selfheal`: issue TRACKER-3 held eight checks, TRACKER-K held
21 QC-073 events under a QC-072 title, and QC-039 was spread over three
issues because that stack moves whenever qc_selfheal.py gains a line.

These pin the property at the layer where it lives, both directions:
two checks from the same call path never share a fingerprint; one check
across rows, fires and messages always does; a non-QC name keeps Sentry's
default grouping; the tag rides the EVENT and not the process scope. The
real-SDK tests drive the production `Log.error` path with the two message
shapes issue K actually held and read the event off a recording transport.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sentry_setup  # noqa: E402

# The two message shapes HILMAR-DAILY-TRACKER-K actually held (event
# bb1664f7f5f543ee836d38f1ad32868d and the 21 QC-073 siblings), with a raw
# request id so the scrubber has something to redact.
_QC072_MSG = ("QC-072: request req_5ab6c8f0e1d2c3b4 — status=PENDING but "
              "status_history ends at QUOTED (at 2026-08-12T20:46:10Z)")
_QC072_MSG_OTHER_ROW = ("QC-072: request req_0f1e2d3c4b5a6978 — status=PENDING "
                        "but status_history ends at QUOTED (at 2026-08-12T20:57:02Z)")
_QC073_MSG = "QC-073: request stand_260928 — degenerate lane Oakland → Oakland"


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


def test_a_non_qc_name_keeps_sentry_default_grouping(monkeypatch):
    """`_extract_check_name` yields "QC-unknown" for a prefix-less message
    (the parser-accuracy warning). Forcing a fingerprint there would fold
    every such message into ONE catch-all issue — worse than the default."""
    fake = _fake_sdk(monkeypatch)
    sentry_setup.capture_qc_warning("QC-unknown", "PARSER ACCURACY 97.2% — below 98%")
    kw = fake.capture_message.call_args.kwargs
    assert "fingerprint" not in kw
    assert kw["tags"] == {"qc_check": "QC-unknown"}   # still searchable
    assert sentry_setup.qc_fingerprint("QC-unknown") is None
    assert sentry_setup.qc_fingerprint("") is None
    assert sentry_setup.qc_fingerprint("QC-014a") == ["QC-014a"]


def test_the_tag_rides_the_event_not_the_process_scope(monkeypatch):
    """The old `sentry_sdk.set_tag("qc_check", ...)` wrote the isolation
    scope, so every LATER event in the process wore the last check's id —
    and `qc_actions_from_sentry._action_lookup` routes on that tag."""
    fake = _fake_sdk(monkeypatch)
    sentry_setup.capture_qc_error("QC-072", _QC072_MSG)
    sentry_setup.capture_qc_error("QC-073", _QC073_MSG)
    assert not any(c.args[:1] == ("qc_check",) for c in fake.set_tag.call_args_list)
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
    """The real SDK on the PRODUCTION options (`sentry_setup._sdk_options`,
    the dict `init()` passes — not a copy of it), bound to a recorder."""
    transport = _RecordingTransport()
    opts = sentry_setup._sdk_options("https://public@example.invalid/1", "test", "r", sample_rate=0)
    sentry_sdk.init(**opts, transport=transport, default_integrations=False)
    monkeypatch.setattr(sentry_setup, "_INITIALIZED", True)
    monkeypatch.delenv("HILMAR_QC_PHASE", raising=False)
    yield transport
    sentry_sdk.init(dsn=None, default_integrations=False)   # unbind the recorder


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


def _without_source_context(ev):
    """The event with the frames' source listing removed — code, not data."""
    ev = json.loads(json.dumps(ev, default=str))
    for key in ("exception", "threads"):
        for v in (ev.get(key) or {}).get("values") or []:
            for fr in (v.get("stacktrace") or {}).get("frames") or []:
                for k in ("context_line", "pre_context", "post_context"):
                    fr.pop(k, None)
    return ev


def _frame_vars(ev):
    """Every frame-locals snapshot on the event, whichever interface holds
    the stack (`exception` for a raised error, `threads` for a message)."""
    out = []
    for key in ("exception", "threads"):
        for v in (ev.get(key) or {}).get("values") or []:
            for fr in (v.get("stacktrace") or {}).get("frames") or []:
                if fr.get("vars"):
                    out.append(fr["vars"])
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
    and useful context); the locals do not."""
    import qc_selfheal as q
    q.Log().error(_QC072_MSG.replace("req_5ab6c8f0e1d2c3b4",
                                     "req_5ab6c8f0e1d2c3b4 for lupfold@hilmaringredients.com"))
    (ev,) = live_sdk.events
    assert "threads" in ev
    assert _frame_vars(ev) == []
    # Everything but the frames' source LISTING (`context_line` & co. quote
    # this test's own code, which spells the address) must be clean.
    assert "lupfold@hilmaringredients.com" not in json.dumps(_without_source_context(ev))


def test_pre_patch_phase_still_sends_nothing(live_sdk, monkeypatch):
    """Unchanged contract: the pre-patch pass is expected-incomplete and
    must not page (patch_carriers runs next)."""
    import qc_selfheal as q
    monkeypatch.setenv("HILMAR_QC_PHASE", "pre-patch")
    q.Log().error(_QC072_MSG)
    assert live_sdk.events == []
