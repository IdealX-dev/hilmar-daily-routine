"""The Sentry PII scrubber must FAIL CLOSED (audit finding [30]) — and the
keys it drops on a fault are the keys it scrubs on the normal path.

scripts/sentry_setup._before_send wraps the whole scrub in try/except. The old
except branch did `pass` and returned the RAW event — so any fault inside the
scrubber shipped unredacted emails/MDOLX/conv-IDs to the third-party SaaS, the
opposite of the hook's purpose. The fix redacts the PII-bearing fields (or
drops the event) on a scrub fault instead of leaking.

2026-09-05: `logentry` had sat in that drop list since it was written and was
never scrubbed on the normal path — the stdlib LoggingIntegration shipped a
raw email through `logentry.message` / `.formatted` / `.params` under the
real SDK. The family is now ENUMERATED: `test_every_pii_bearing_key_is_
scrubbed_on_the_normal_path` parametrises over `_PII_BEARING_KEYS`, so a key
added to the drop list without a scrub goes red.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sentry_setup as S  # noqa: E402

_ADDR = "lupfold@hilmaringredients.com"

# The production shape each PII-bearing interface takes, carrying the raw
# address where that interface carries DATA. `request` / `user` never occur
# in this pipeline (no HTTP server, send_default_pii=False); their shapes
# are the SDK's documented ones so the family has no member the scrub skips.
_PII_SHAPES = {
    "message": f"boom for {_ADDR}",
    "logentry": {"message": "row %s failed", "formatted": f"row {_ADDR} failed",
                 "params": (_ADDR,)},
    "exception": {"values": [{"type": "ValueError", "value": f"bad row {_ADDR}",
                              "stacktrace": {"frames": [{"function": "f",
                                                         "vars": {"row": _ADDR}}]}}]},
    "threads": {"values": [{"stacktrace": {"frames": [{"function": "error",
                                                       "vars": {"msg": _ADDR}}]}}]},
    "extra": {"lane": _ADDR, "nested": {"who": [_ADDR]}},
    "breadcrumbs": {"values": [{"message": f"sent to {_ADDR}", "data": {"to": _ADDR}}]},
    "request": {"url": f"https://x.invalid/?to={_ADDR}", "data": {"to": _ADDR}},
    "user": {"email": _ADDR, "id": "u1"},
}


def _event_with_pii():
    return {
        "level": "error",
        "message": "boom for lupfold@hilmaringredients.com on MDOLX260100",
        "extra": {"req": "req_00aabbccddeeff11", "lane": "Oakland to Tokyo"},
        "tags": {"phase": "post-patch"},
        "fingerprint": ["pipeline.step_failure"],
        # attach_stacktrace=True puts a capture_message's stack HERE, not
        # under "exception" — and frame locals hold the raw message.
        "threads": {"values": [{"stacktrace": {"frames": [
            {"function": "error", "vars": {
                "msg": "QC-072: request req_00aabbccddeeff11 for lupfold@hilmaringredients.com"}},
        ]}}]},
    }


def test_happy_path_scrubs_inline():
    out = S._before_send(_event_with_pii(), None)
    assert "lupfold@hilmaringredients.com" not in json.dumps(out)
    assert "[EMAIL_REDACTED]" in out["message"]
    assert "[MDOLX_REDACTED]" in out["message"]


def test_scrub_fault_does_not_return_raw_event(monkeypatch):
    """If the scrubber raises mid-way, NO raw PII may survive in the result."""
    def _boom(_s):
        raise RuntimeError("scrubber tripped on a malformed field")
    monkeypatch.setattr(S, "_scrub_string", _boom)

    out = S._before_send(_event_with_pii(), None)
    # Event is kept (we still learn an error happened) but fully de-PII'd.
    assert out is not None
    blob = json.dumps(out)
    assert "lupfold@hilmaringredients.com" not in blob
    assert "MDOLX260100" not in blob
    assert "req_00aabbccddeeff11" not in blob
    assert "Oakland to Tokyo" not in blob
    assert out["message"].startswith("[SCRUBBER_FAILED")
    # The non-PII skeleton survives so Sentry still records the error.
    assert out["level"] == "error"
    assert out["tags"] == {"phase": "post-patch"}


def test_threads_frame_locals_are_scrubbed():
    """2026-09-05 (HILMAR-DAILY-TRACKER-K): the scrubber walked `exception`
    frames only. A QC event's stack lives under `threads`, so its `msg` /
    `summary` / `text` locals shipped the raw request id and email beside
    the redacted message. Both interfaces go through one helper now."""
    out = S._before_send(_event_with_pii(), None)
    frame = out["threads"]["values"][0]["stacktrace"]["frames"][0]
    assert frame["vars"]["msg"] == "QC-072: request [REQ_ID] for [EMAIL_REDACTED]"
    assert "lupfold@hilmaringredients.com" not in json.dumps(out)


def test_the_pii_family_here_is_the_drop_list():
    """The shapes above must cover the drop list exactly — a key added to
    `_PII_BEARING_KEYS` with no shape here would escape the parametrised
    test below by absence."""
    assert set(_PII_SHAPES) == set(S._PII_BEARING_KEYS)


@pytest.mark.parametrize("key", S._PII_BEARING_KEYS)
def test_every_pii_bearing_key_is_scrubbed_on_the_normal_path(key):
    """A key the fail-closed path considers PII-bearing is scrubbed on the
    normal path too — kept (not dropped) with the address redacted, and
    the address absent from the WHOLE event."""
    out = S._before_send({"level": "error", key: _PII_SHAPES[key]}, None)
    assert out is not None and key in out, key
    blob = json.dumps(out, default=str)
    assert _ADDR not in blob, f"{key}: raw address survived the normal path"
    assert "[EMAIL_REDACTED]" in json.dumps(out[key], default=str), key


def test_logentry_params_keep_their_shape_and_are_scrubbed():
    out = S._before_send({"logentry": _PII_SHAPES["logentry"]}, None)
    le = out["logentry"]
    assert le["message"] == "row %s failed"          # the template has no PII to lose
    assert le["formatted"] == "row [EMAIL_REDACTED] failed"
    assert le["params"] == ("[EMAIL_REDACTED]",)      # still a tuple, still one param


def test_stack_interfaces_are_scrubbed_at_data_fields_not_code():
    """The NEGATIVE direction of the family rule: `exception` / `threads`
    are scrubbed at `value` and frame `vars`, never walked wholesale —
    `_CARRIER_REF_RX` is case-insensitive `NAM` + six characters and would
    redact `namedtuple` out of a function name or a context line, leaving a
    stack nobody can read. Measured: `_scrub_string("namedtuple") ==
    "[CARRIER_REF_REDACTED]"`."""
    assert S._scrub_string("namedtuple") == "[CARRIER_REF_REDACTED]"
    frame = {"function": "namedtuple_rows", "module": "namespace_x",
             "context_line": "row = namedtuple('Row', 'a b')(1, 2)",
             "vars": {"row": f"Row({_ADDR})"}}
    ev = {
        "exception": {"values": [{"type": "ValueError", "value": f"bad {_ADDR}",
                                  "stacktrace": {"frames": [dict(frame)]}}]},
        "threads": {"values": [{"stacktrace": {"frames": [dict(frame)]}}]},
    }
    out = S._before_send(ev, None)
    for fr in (out["exception"]["values"][0]["stacktrace"]["frames"][0],
               out["threads"]["values"][0]["stacktrace"]["frames"][0]):
        assert fr["function"] == "namedtuple_rows"
        assert fr["module"] == "namespace_x"
        assert fr["context_line"] == "row = namedtuple('Row', 'a b')(1, 2)"
        assert fr["vars"] == {"row": "Row([EMAIL_REDACTED])"}
    assert out["exception"]["values"][0]["value"] == "bad [EMAIL_REDACTED]"


def test_fail_closed_event_drops_pii_bearing_keys():
    redacted = S._fail_closed_event(_event_with_pii())
    for k in ("exception", "threads", "extra", "breadcrumbs", "request", "user"):
        assert k not in redacted
    assert "message" in redacted and redacted["message"].startswith("[SCRUBBER_FAILED")


def test_fail_closed_event_non_dict_drops():
    assert S._fail_closed_event(None) is None
    assert S._fail_closed_event("not an event") is None
