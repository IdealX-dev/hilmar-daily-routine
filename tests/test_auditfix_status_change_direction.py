"""Status-change transitions must read in operational 'who do we chase' terms.

Michael 2026-07-15 (screenshot: two rows shown as "PENDING HILMAR → QUOTED"):
  "status is waiting ol quote then after quote is pending hilmar response —
   check your steps"

The real lifecycle is:
  1. RFQ sent, OL hasn't quoted   → PENDING OL      (chase OL for a rate)
  2. OL delivers a rate response  → PENDING HILMAR  (ball now in Hilmar's court)

So a rate response (status_history from="PENDING", to="QUOTED") must render
'PENDING OL → PENDING HILMAR' — NEVER the inverted 'PENDING HILMAR → QUOTED'.

The old bug: the 'from' pill was resolved from the row's CURRENT substate,
which — because the quote already landed — is PENDING_HILMAR, mislabeling the
BEFORE end; and the 'to' pill rendered the raw internal enum 'QUOTED'. Both
are fixed in gen_email._status_change_pill, which resolves each end from the
transition direction, not the row's present state.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_email as GE  # noqa: E402

# A row exactly as it looks the moment OL's rate lands: still PENDING (Hilmar
# hasn't booked), now quoted, carrier + rate populated.
QUOTED_ROW = {
    "status": "PENDING",
    "quoted": True,
    "carrier_quoted": "HMM",
    "ol_rate": 2226.0,
    "lane": "Oakland → Rotterdam",
}
RATE_RESPONSE = {"from": "PENDING", "to": "QUOTED"}


def _from_pill():
    return GE._status_change_pill(RATE_RESPONSE["from"], QUOTED_ROW, RATE_RESPONSE["to"])


def _to_pill():
    return GE._status_change_pill(RATE_RESPONSE["to"], QUOTED_ROW, RATE_RESPONSE["from"])


def test_rate_response_from_end_is_pending_ol():
    """The BEFORE end of a rate response was 'waiting on OL to quote'."""
    html = _from_pill()
    assert "PENDING OL" in html, "rate-response 'from' must read PENDING OL"
    assert "PENDING HILMAR" not in html, (
        "the old inversion: the pre-quote end must NOT read PENDING HILMAR"
    )


def test_rate_response_to_end_is_pending_hilmar():
    """The AFTER end of a rate response is 'pending Hilmar response', not the
    raw internal 'QUOTED' enum."""
    html = _to_pill()
    assert "PENDING HILMAR" in html, "rate-response 'to' must read PENDING HILMAR"
    assert "QUOTED" not in html, "the raw enum 'QUOTED' must never reach the reader"


def test_full_transition_reads_ol_to_hilmar_not_hilmar_to_quoted():
    """End-to-end direction check on the assembled cell text."""
    transition = f"{_from_pill()} → {_to_pill()}"
    assert "PENDING OL" in transition and "PENDING HILMAR" in transition
    assert "QUOTED" not in transition, "the raw enum must never surface"
    # Ordering: OL side first (the before), HILMAR side second (the after).
    assert transition.index("PENDING OL") < transition.index("PENDING HILMAR")


def test_win_transition_from_end_is_pending_hilmar():
    """A booking win: the row was OL-quoted and waiting on Hilmar, who booked.
    'PENDING HILMAR → WIN' (not 'PENDING OL → WIN')."""
    row = {"status": "WIN", "quoted": True, "lane": "Oakland → Tokyo"}
    from_html = GE._status_change_pill("PENDING", row, "WIN")
    to_html = GE._status_change_pill("WIN", row, "PENDING")
    assert "PENDING HILMAR" in from_html
    assert "WIN" in to_html and "PENDING" not in to_html


def test_nq_loss_from_end_is_pending_ol():
    """An OL-never-answered loss (quoted=False) was still 'waiting on OL' —
    the pre-loss end is PENDING OL, not PENDING HILMAR."""
    row = {"status": "LOSS", "quoted": False, "lane": "Oakland → Nowhere"}
    from_html = GE._status_change_pill("PENDING", row, "LOSS")
    assert "PENDING OL" in from_html
    assert "PENDING HILMAR" not in from_html


def test_qandl_loss_from_end_is_pending_hilmar():
    """A quoted-and-lost row (rate went stale) was 'pending Hilmar' before it
    aged out — the pre-loss end is PENDING HILMAR."""
    row = {"status": "LOSS", "quoted": True, "lane": "Oakland → Osaka"}
    from_html = GE._status_change_pill("PENDING", row, "LOSS")
    assert "PENDING HILMAR" in from_html
