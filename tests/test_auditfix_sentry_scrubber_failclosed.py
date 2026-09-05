"""The Sentry PII scrubber must FAIL CLOSED (audit finding [30]).

scripts/sentry_setup._before_send wraps the whole scrub in try/except. The old
except branch did `pass` and returned the RAW event — so any fault inside the
scrubber shipped unredacted emails/MDOLX/conv-IDs to the third-party SaaS, the
opposite of the hook's purpose. The fix redacts the PII-bearing fields (or
drops the event) on a scrub fault instead of leaking.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sentry_setup as S  # noqa: E402


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


def test_fail_closed_event_drops_pii_bearing_keys():
    redacted = S._fail_closed_event(_event_with_pii())
    for k in ("exception", "threads", "extra", "breadcrumbs", "request", "user"):
        assert k not in redacted
    assert "message" in redacted and redacted["message"].startswith("[SCRUBBER_FAILED")


def test_fail_closed_event_non_dict_drops():
    assert S._fail_closed_event(None) is None
    assert S._fail_closed_event("not an event") is None
