"""A borrowed quote time is never stated to the client as fact.

2026-08-19. qc_selfheal's sibling heal can COPY a response_timestamp from
another row's quote onto an undated row. The first attempt at fixing its
fan-out added the marker core.BORROWED_RESPONSE_TIME and read it in exactly
ONE of the places that print or reason about a quote time — gen_email's
OL-USA RESPONSES list.

The other four are client-facing:

    gen_client_email._quoted_at   "Quoted at (ET)" in Quotes provided
                                  and in Awaiting your decision
    gen_client_weekly._quoted_on  the "Quoted" column, sent Mondays
    auto_chase_pending            licenses "quote from N days ago" in a
                                  chase email addressed to Lonny

All three go to lupfold@hilmaringredients.com. Printing a minute OL cannot
evidence is the same class of unearned assurance Michael caught in the client
templates on 2026-08-10 ("data missing.. you sent lonny we won no shipment
last week").

The row still belongs in the table — its rate, carrier and ETD are real
evidence. Only the minute is withheld.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import auto_chase_pending as ACP  # noqa: E402
import core  # noqa: E402
import gen_client_email as GCE  # noqa: E402
import gen_client_weekly as GCW  # noqa: E402

REAL = "2026-08-18T17:44:45Z"


def _row(**over):
    r = {"request_id": "req_1", "status": "PENDING", "quoted": True,
         "lane": "Oakland → Singapore", "origin": "Oakland",
         "destination": "Singapore", "ol_rate": 3289.0,
         "carrier_quoted": "OOCL", "response_timestamp": REAL,
         "request_timestamp": "2026-07-09T17:48:00Z"}
    r.update(over)
    return r


# ── the predicate ─────────────────────────────────────────────────────────

def test_an_evidenced_time_is_evidenced():
    assert core.response_time_is_evidenced(_row()) is True


def test_a_borrowed_time_is_not():
    assert core.response_time_is_evidenced(
        _row(response_time_source=core.BORROWED_RESPONSE_TIME)) is False


def test_no_time_at_all_is_not_evidenced():
    assert core.response_time_is_evidenced(
        _row(response_timestamp=None)) is False


# ── the client daily ──────────────────────────────────────────────────────

def test_client_daily_prints_a_real_quote_minute():
    assert GCE._quoted_at(_row()) != "—"


def test_client_daily_withholds_a_borrowed_minute():
    assert GCE._quoted_at(
        _row(response_time_source=core.BORROWED_RESPONSE_TIME)) == "—", (
        "Lonny is being shown a quote time no email supports")


# ── the client weekly ─────────────────────────────────────────────────────

def test_client_weekly_prints_a_real_quote_date():
    assert GCW._quoted_on(_row()) == "2026-08-18"


def test_client_weekly_withholds_a_borrowed_date():
    assert GCW._quoted_on(
        _row(response_time_source=core.BORROWED_RESPONSE_TIME)) == "—"


# ── the chase email ───────────────────────────────────────────────────────

def test_the_chase_does_not_age_a_borrowed_quote():
    """_age_dated licenses "quote from N days ago" wording. On a borrowed
    date that sentence is a fabrication addressed to the client, which this
    module's own docstring forbids."""
    borrowed = _row(response_time_source=core.BORROWED_RESPONSE_TIME,
                    request_timestamp="2026-06-01T17:00:00Z")
    out = ACP._find_overdue_pending({"requests": [borrowed]}, min_age_hours=1)
    assert len(out) == 1, "the row should still be chaseable, just not dated"
    assert out[0]["_age_dated"] is False, (
        "the chase email will tell Lonny his quote is N days old, off a "
        "timestamp copied from a different row's quote")


def test_the_chase_still_ages_a_real_quote():
    """The guard must not blind the chase to genuine quote ages."""
    out = ACP._find_overdue_pending(
        {"requests": [_row(response_timestamp="2026-06-01T17:00:00Z")]},
        min_age_hours=1)
    assert len(out) == 1
    assert out[0]["_age_dated"] is True
