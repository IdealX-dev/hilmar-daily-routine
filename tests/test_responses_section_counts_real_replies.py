"""OL-USA RESPONSES must list replies OL actually sent that day.

2026-08-19, Michael on the Aug-18 report — NEW REQUESTS FROM LONNY (4) above
OL-USA RESPONSES (11): "there is data missing and the request count and reply
count vary greatly as well as container count."

The screenshot gave the mechanism away before any data was pulled. FOUR
Singapore rows all read "OL Quoted Aug 18 1:44 PM ET" and BOTH Xingang rows
"4:42 PM ET" — one real email fanned across every old same-lane row. Those
rows also rendered with no signer, no time-to-quote, and container counts
belonging to a different ask, because no email sits behind them.

qc_selfheal's sibling-date heal now refuses ambiguous fan-out at the source,
and marks the single-row stamps it does make with
response_time_source="sibling_quote". This file holds the SECOND line of
defence: a borrowed date must never put a row in the day's reply list, even
if a future heal starts stamping again.

The date itself stays on the row — it is real evidence about which quote
covered that lane, and QC-077/the win-loss ledger still use it. What it is
not is proof OL sent something that day.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_email as GE  # noqa: E402

# _et_date returns a datetime.date, and _today_events compares with ==, so a
# string here silently matches nothing and every assertion passes vacuously.
DAY = date(2026, 8, 18)


def _row(rid, **over):
    r = {"request_id": rid, "status": "LOSS", "quoted": True,
         "lane": "Oakland → Singapore", "origin": "Oakland",
         "destination": "Singapore", "ol_rate": 3289.0,
         "request_timestamp": "2026-07-09T17:48:00Z",
         "request_date": "2026-07-09",
         "response_timestamp": "2026-08-18T17:44:45Z",
         "teu_requested": 10, "status_history": []}
    r.update(over)
    return r


def _responses(rows):
    _new, ol_resp, _sc, _pend = GE._today_events({"requests": rows}, DAY)
    return ol_resp


def test_a_real_reply_is_listed():
    """The section must still work — this is the row an email backs."""
    real = _row("req_real")
    assert [r["request_id"] for r in _responses([real])] == ["req_real"]


def test_a_borrowed_date_is_not_a_reply():
    """THE REGRESSION. No email sits behind a sibling-stamped date, which is
    why these rendered with no signer and no time-to-quote."""
    borrowed = _row("req_borrowed", response_time_source="sibling_quote")
    assert _responses([borrowed]) == [], (
        "a row whose date was copied off another row's quote is being counted "
        "as a reply OL sent that day — this is what showed 11 responses "
        "against 4 new requests on 2026-08-18.")


def test_the_fanned_out_day_reports_only_the_real_reply():
    """One genuine Aug-18 quote plus four old asks that borrowed its time:
    the day shows ONE reply, not five."""
    rows = [_row("req_real")] + [
        _row(f"req_old_{i}", response_time_source="sibling_quote")
        for i in range(4)
    ]
    assert [r["request_id"] for r in _responses(rows)] == ["req_real"]


def test_the_borrowed_date_is_not_deleted_from_the_row():
    """Suppressing the row from a day listing is a RENDER decision. The
    timestamp is still evidence about which quote covered the lane, and the
    win-loss ledger and QC-077 both read it — silently blanking it would
    trade one wrong number for another."""
    borrowed = _row("req_borrowed", response_time_source="sibling_quote")
    _responses([borrowed])
    assert borrowed["response_timestamp"] == "2026-08-18T17:44:45Z"


def test_new_requests_are_untouched_by_the_marker():
    """Only the reply list is filtered. An ask is an ask regardless of how
    its response was dated."""
    asked_today = _row("req_today", request_date="2026-08-18",
                       request_timestamp="2026-08-18T12:00:00Z",
                       response_time_source="sibling_quote")
    new, _resp, _sc, _p = GE._today_events({"requests": [asked_today]}, DAY)
    assert [r["request_id"] for r in new] == ["req_today"]
