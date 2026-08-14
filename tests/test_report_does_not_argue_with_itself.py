"""STATUS CHANGES and the PENDING sections must not contradict each other.

Michael 2026-08-13, on the Aug 12 email: "in status shows nothing pending
hilmar in the chart but then in words says yes, we had 2 new requests but
three open etc etc.. it's all wrong".

The email rendered three rows as

    PENDING OL  →  PENDING HILMAR

and, immediately below, "PENDING HILMAR (0) — No activity".

NEITHER NUMBER WAS WRONG, which is why this needed a rendering fix and not a
counting one. The pill describes the TRANSITION — at the moment OL quoted,
the ball really was in Hilmar's court. The section describes NOW — and by
render time the row had aged out of PENDING. Measured on stored state
(diag-blob 31790544681): "rows with status == PENDING: 0".

It is not a rare race. core.PENDING_HILMAR_LOSS_HOURS is 24 — Michael's own
figure, deliberately not 48 — so a quote OL sends on Wednesday evening ages
out before Thursday's fire renders. The transition and the section are almost
never describing the same state.

The fix says where the row went, exactly as the reversed-WIN branch already
did. Re-deriving the pill from current status would be the WRONG fix: it
would erase the fact that OL quoted at all, which is the one thing STATUS
CHANGES exists to record.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_email as GE  # noqa: E402

DAY = date(2026, 8, 12)
AT = "2026-08-12T20:46:10Z"


def _quoted_then_aged(**over):
    """The exact shape: quoted Aug 12, aged to Q&L before the render."""
    r = {
        "request_id": "req_34213cc401395756",
        "status": "LOSS", "quoted": True, "loss_reason": "QUOTED_NOT_BOOKED",
        "lane": "Oakland → HCMC (Cat Lai)", "origin": "Oakland",
        "destination": "HCMC (Cat Lai)",
        "containers": "2-20'", "container_count": 2, "teu_requested": 2,
        "request_timestamp": "2026-08-12T13:43:39Z", "request_date": "2026-08-12",
        "response_timestamp": AT, "ol_rate": 475.0, "carrier_quoted": "ONE",
        "status_history": [{"at": AT, "from": "PENDING", "to": "QUOTED",
                            "reason": "MBD rate response — carrier=ONE, rate=475.0"}],
    }
    r.update(over)
    return r


def _render(rows):
    new_req, ol_resp, status_ch, pending = GE._today_events({"requests": rows}, DAY)
    return GE._today_block_html("Aug 12, 2026", new_req, ol_resp,
                                status_ch, pending), status_ch, pending


def test_a_quote_that_aged_out_says_so_next_to_the_arrow():
    html, status_ch, pending = _render([_quoted_then_aged()])
    assert len(status_ch) == 1, "the transition vanished"
    assert pending == [], "fixture is meant to have aged out of PENDING"
    assert "since aged to" in html, (
        "the row shows an arrow into PENDING HILMAR while PENDING HILMAR "
        "reads 0, and nothing says the row moved on")
    assert "Q&amp;L" in html or "Q&L" in html


def test_the_arrow_still_records_that_ol_quoted():
    """The fix must not erase the quote. Re-deriving the pill from current
    status would turn 'OL answered' into 'this is a loss', which is the one
    thing this section exists to record."""
    html, _sc, _p = _render([_quoted_then_aged()])
    assert "PENDING HILMAR" in html, (
        "the transition target was rewritten from current status — the fact "
        "that OL quoted has been erased")


def test_a_still_pending_quote_gets_no_aged_note():
    """The line this must not cross: a quote still awaiting Lonny is NOT
    annotated, because nothing has moved on."""
    row = _quoted_then_aged(status="PENDING", loss_reason=None)
    html, _sc, pending = _render([row])
    assert len(pending) == 1
    assert "since aged to" not in html


def test_a_reversed_win_still_reports_reversed_not_aged():
    """The pre-existing branch must keep its own wording — two different
    events, two different explanations."""
    row = _quoted_then_aged(
        status="LOSS", loss_reason="SEND_NO_BOOKING",
        status_history=[{"at": AT, "from": "PENDING", "to": "WIN",
                         "reason": "MDOLX261070 booking confirmed"}])
    html, _sc, _p = _render([row])
    assert "REVERSED" in html
    assert "since aged to" not in html
