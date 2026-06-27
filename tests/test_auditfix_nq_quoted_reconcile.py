"""A request carrying a real rate must never be counted Not Quoted.

User-reported (with screenshot proof): the OL-USA Responses table showed a
$3,076 CMA CGM rate for an Oakland->Yokohama RFQ, yet the period KPIs counted
that request under "Not Quoted". Two defects combined:
  (A) decide_status bucketed a quoted row with a missing response_timestamp as
      NO_RESPONSE (fixed in core.py — see test_scripts_core_decide_status.py).
  (B) the request row's `quoted` flag was desynced from the rate (quoted=False
      while a rate/carrier was present), so even the corrected classifier saw
      quoted=False. qc_selfheal's quoted-derivation only DEFAULTED when the key
      was absent; it never repaired a stored quoted=False desync.

This locks the self-heal: a row with a real rate/carrier is reconciled to
quoted=True, so it can never be miscounted as NQ.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal as q  # noqa: E402


def _row(**over):
    base = {
        "request_id": "req_yokohama", "subject": "HILMAR Oakland to Yokohama RFQ",
        "status": "LOSS", "quoted": False, "ol_rate": 3076,
        "carrier_quoted": "CMA CGM", "response_timestamp": None,
        "origin": "Oakland", "destination": "Yokohama",
        "request_timestamp": "2026-06-16T14:00:00Z",
    }
    base.update(over)
    return base


def test_rate_present_but_quoted_false_is_reconciled_to_true():
    data = {"requests": [_row()]}
    q.phase_3_entries(q.Log(), data)
    survivors = [r for r in data["requests"] if r.get("request_id") == "req_yokohama"]
    assert survivors, "row was unexpectedly dropped by phase_3 cleanup"
    assert survivors[0]["quoted"] is True, (
        "a row carrying a real rate/carrier must be reconciled quoted=True so it "
        "can never be miscounted as Not Quoted"
    )


def test_carrier_only_evidence_also_reconciles():
    data = {"requests": [_row(ol_rate=None)]}  # carrier present, no rate
    q.phase_3_entries(q.Log(), data)
    survivors = [r for r in data["requests"] if r.get("request_id") == "req_yokohama"]
    assert survivors and survivors[0]["quoted"] is True


def test_genuine_no_rate_stays_not_quoted():
    """No rate and no carrier → quoted stays False (a real NQ must not be
    flipped). Guards against over-reconciling."""
    data = {"requests": [_row(ol_rate=None, carrier_quoted=None)]}
    q.phase_3_entries(q.Log(), data)
    survivors = [r for r in data["requests"] if r.get("request_id") == "req_yokohama"]
    assert survivors and survivors[0]["quoted"] is False


def test_not_quoted_sentinel_string_is_not_a_rate():
    """ol_rate == 'Not Quoted' is a sentinel, not a real rate — must not flip."""
    data = {"requests": [_row(ol_rate="Not Quoted", carrier_quoted=None)]}
    q.phase_3_entries(q.Log(), data)
    survivors = [r for r in data["requests"] if r.get("request_id") == "req_yokohama"]
    assert survivors and survivors[0]["quoted"] is False
