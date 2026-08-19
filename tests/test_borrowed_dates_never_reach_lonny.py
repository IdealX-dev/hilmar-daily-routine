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


# ── THE STORED-STATE INVARIANT ────────────────────────────────────────────
#
# The three DERIVED fields — turnaround_biz_hours, turnaround_hours and
# olusa_time_et — have 20+ readers across scripts/, src/hilmar/ and two Jinja
# templates. Guarding each reader is the "fix one, ship two numbers" failure
# this repo keeps paying for, so instead they are guaranteed absent at the
# WRITER and the invariant is pinned here.
#
# olusa_time_et matters most and was nearly missed: it is a STORED,
# pre-rendered "OL sent at (ET)" clock string, so a reader that correctly
# guards response_timestamp still prints it.

import qc_selfheal as QS  # noqa: E402

DERIVED = ("turnaround_biz_hours", "turnaround_hours", "olusa_time_et")


def _phase3(rows):
    log = QS.Log()
    QS.phase_3_entries(log, {"requests": rows})
    return log


def _ask(**over):
    r = {"request_id": "req_1", "status": "LOSS", "quoted": True,
         "lane": "Oakland → Algeciras", "origin": "Oakland",
         "destination": "Algeciras", "ol_rate": 4938.0,
         "carrier_quoted": "CMA CGM",
         "request_timestamp": "2026-08-12T14:00:00Z",
         "request_date": "2026-08-12", "response_timestamp": None,
         "status_history": []}
    r.update(over)
    return r


def test_the_heal_stamps_the_date_and_no_turnaround():
    """Reproduces the measured 6.95: a sibling gap deliberately UNDER 40
    biz-hours, which is the band QC-048 never clears."""
    ask = _ask()
    sib = _ask(request_id="sib", status="PENDING",
               request_timestamp="2026-08-12T13:00:00Z",
               response_timestamp="2026-08-12T20:57:02Z")
    QS._stamp_response_from_dated_sibling(QS.Log(), [ask, sib])
    assert ask["response_timestamp"] is not None, "the date is real evidence"
    assert ask["turnaround_biz_hours"] is None
    assert ask["turnaround_hours"] is None


def test_two_qc_passes_never_re_derive_a_borrowed_turnaround():
    """qc_selfheal runs TWICE per fire (run_pipeline.py:78 and :82) and
    phase_3 runs BEFORE phase_6 within each pass. So pass 1's phase-6 stamp
    is pass 2's phase-3 input: guarding only the heal moves the fabrication
    one phase later instead of removing it."""
    ask = _ask()
    sib = _ask(request_id="sib", status="PENDING",
               request_timestamp="2026-08-12T13:00:00Z",
               response_timestamp="2026-08-12T20:57:02Z")
    rows = [ask, sib]
    for _pass in (1, 2):
        _phase3(rows)                                     # phase 3
        QS._stamp_response_from_dated_sibling(QS.Log(), rows)  # phase 6
        for f in DERIVED:
            assert ask.get(f) is None, (
                f"pass {_pass} left {f}={ask.get(f)!r} on a borrowed row")
    assert ask["response_time_source"] == core.BORROWED_RESPONSE_TIME


def test_a_pre_poisoned_row_is_scrubbed():
    """MIGRATION. Nothing ever un-stamps a borrowed row — the heal skips any
    row that already has a response_timestamp — so rebuild-not-merge does NOT
    re-decide these, and values written by fires before this change sit in
    tracking-data-v2.json until something clears them."""
    poisoned = _ask(response_timestamp="2026-08-12T20:57:02Z",
                    response_time_source=core.BORROWED_RESPONSE_TIME,
                    turnaround_biz_hours=6.95, turnaround_hours=6.95,
                    olusa_time_et="04:57 PM ET")
    _phase3([poisoned])
    for f in DERIVED:
        assert poisoned.get(f) is None, f"{f} survived the scrub"
    assert poisoned["response_timestamp"] is not None


def test_an_evidenced_row_still_gets_its_backfill():
    """Against over-application: the guard must not empty a real sample, and
    lonny_time_pt derives from request_timestamp so it is never withheld."""
    real = _ask(response_timestamp="2026-08-12T18:00:00Z")
    _phase3([real])
    assert real["turnaround_hours"] is not None
    assert real["turnaround_biz_hours"] is not None
    assert real["olusa_time_et"]
    borrowed = _ask(response_timestamp="2026-08-12T18:00:00Z",
                    response_time_source=core.BORROWED_RESPONSE_TIME)
    _phase3([borrowed])
    assert borrowed["lonny_time_pt"], (
        "lonny_time_pt comes from the ASK, which is never borrowed")


def test_no_borrowed_row_carries_a_derived_time():
    """THE INVARIANT, over a mixed fixture. Any future writer that adds a
    fourth derived field or re-derives an existing one trips this instead of
    the staff email."""
    rows = [
        _ask(request_id="evidenced", response_timestamp="2026-08-12T18:00:00Z"),
        _ask(request_id="borrowed", response_timestamp="2026-08-12T18:00:00Z",
             response_time_source=core.BORROWED_RESPONSE_TIME),
        _ask(request_id="undated"),
    ]
    _phase3(rows)
    for r in rows:
        if core.response_time_is_evidenced(r):
            continue
        for f in DERIVED:
            assert r.get(f) is None, (
                f"{r['request_id']} is not evidenced but carries {f}={r.get(f)!r}")


def test_one_number_off_one_dataset():
    """The KPI and the carrier scoreboard read the same rows and must agree —
    and a borrowed row must not be mislabelled as a clock-reset exclusion,
    which gen_pdf explains to the reader with the wrong reason."""
    rows = [
        {"request_id": "a", "status": "LOSS", "quoted": True,
         "carrier_quoted": "ONE", "turnaround_biz_hours": 3.0,
         "turnaround_hours": 3.0, "request_timestamp": "2026-08-18T14:00:00Z",
         "response_timestamp": "2026-08-18T17:00:00Z"},
        {"request_id": "b", "status": "LOSS", "quoted": True,
         "carrier_quoted": "ONE", "turnaround_biz_hours": 6.95,
         "turnaround_hours": 6.95, "request_timestamp": "2026-08-18T14:00:00Z",
         "response_timestamp": "2026-08-18T20:57:00Z",
         "response_time_source": core.BORROWED_RESPONSE_TIME},
    ]
    s = core.aggregate_summary(rows)
    assert s["turnaround_entries"] == 1
    assert s["turnaround_avg_biz_hours"] == 3.0
    assert s["turnaround_excluded"] == 0, (
        "the borrowed row was counted as a clock-reset exclusion — the "
        "report would explain it with the wrong reason")
    car = core.aggregate_carriers(rows)
    one = next(v for k, v in car.items() if "ONE" in str(k).upper())
    assert one["avg_turnaround_biz_hours"] == 3.0
