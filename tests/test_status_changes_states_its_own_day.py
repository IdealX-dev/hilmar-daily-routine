"""STATUS CHANGES is event-dated; the KPI tiles beside it are intake-dated.

Michael, 2026-08-31, on the delivered Aug 28 report: *"how are there zero
losses or changes in the friday kpi cards if you show four as status changes
to lost"*.

Both numbers were right. THE ROW OF KPI TILES MIXES TWO DEFINITIONS OF "that
day", and `_today_summary`'s own docstring says so:

    WON                     event-dated  — booked THAT DAY, any request date
    REQUESTS / Q&L / NQ /
    PENDING                 intake-dated — CURRENT status of the requests that
                                           CAME IN that day

STATUS CHANGES is event-dated like WON. On Aug 28 it listed four rows aging
PENDING HILMAR -> Q&L, every one requested Aug 27, while Friday's intake was
zero rows — so all four intake tiles read 0. The losses were not missing;
they sit in THURSDAY's tiles, because Thursday is when they were requested.

WHY THE OBVIOUS FIX IS WRONG, and why this file tests a NOTE rather than a
re-bucketing: those four tiles are built to sum against the REQUESTS tile —
Michael's own standing rule, "it should all tie out to requests". Event-dating
the loss tiles would put a row requested Thursday and lost Friday into
Friday's losses but not Friday's requests, and the bucket sum would exceed the
total again. That reconciliation is the thing the tiles are FOR.

So the section states its own semantics, exactly as `_pending_as_of_note`
already does for the live PENDING lists. Same defect class, same remedy.

THIS IS ALSO #232 SURFACING, NOT BREAKING. Before aging transitions were
stamped with the deadline they crossed, they carried the pipeline clock and
never landed in the report-day window at all — STATUS CHANGES would have been
empty here and nobody would have noticed the tiles disagreed.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_email  # noqa: E402

RD = date(2026, 8, 28)          # the Friday in the report Michael read
EARLIER = {"request_date": "2026-08-27", "lane": "Oakland → Xingang"}
SAME_DAY = {"request_date": "2026-08-28", "lane": "Oakland → Osaka"}


def _text(status_ch, report_date=RD):
    return re.sub(r"<[^>]+>", "", gen_email._status_change_daynote(status_ch, report_date))


# ── the report Michael actually received ──────────────────────────────────

def test_the_aug_28_report_says_where_its_four_losses_are_counted():
    note = _text([(EARLIER, {"from": "PENDING", "to": "LOSS"})] * 4)
    assert "All 4" in note
    assert "earlier day" in note
    assert "REQUEST date" in note, (
        "the note must name WHICH date the tiles bucket by — that is the whole "
        "question being answered")


def test_it_does_not_claim_the_rows_are_missing():
    # The rows ARE counted, on their request day. A note implying they vanished
    # would be a different wrong answer.
    note = _text([(EARLIER, {})] * 4)
    assert "request day" in note
    for bad in ("missing", "dropped", "not counted", "excluded"):
        assert bad not in note.lower(), f"note implies the rows are lost: {bad!r}"


# ── it must discriminate, or it is decoration ─────────────────────────────

def test_a_same_day_move_gets_the_opposite_note():
    note = _text([(SAME_DAY, {"from": "PENDING", "to": "WIN"})] * 3)
    assert "earlier day" not in note
    assert "in the tiles below too" in note


def test_a_mixed_day_reports_the_split_not_the_total():
    note = _text([(SAME_DAY, {}), (EARLIER, {}), (EARLIER, {})])
    assert "2 of 3" in note, note
    assert "All" not in note


@pytest.mark.parametrize("rows,expect", [
    ([(EARLIER, {})], "It was requested on an earlier day"),
    ([(EARLIER, {})] * 2, "All 2 were requested on an earlier day"),
    ([(SAME_DAY, {})], "It was also requested that day"),
    ([(SAME_DAY, {})] * 2, "All 2 were also requested that day"),
])
def test_it_reads_as_english_at_every_count(rows, expect):
    # Singular/plural agreement across both branches. A report that says
    # "All 1 were" reads as broken and gets trusted less than it should.
    assert expect in _text(rows)


def test_silent_when_there_is_nothing_to_say():
    assert gen_email._status_change_daynote([], RD) == ""


def test_silent_rather_than_guessing_when_the_report_day_is_unknown():
    # Saying nothing beats saying something unverifiable — the note's whole
    # job is to state a fact about WHICH day these rows belong to.
    assert gen_email._status_change_daynote([(EARLIER, {})], None) == ""


def test_it_accepts_a_row_dated_by_the_legacy_date_field():
    assert "earlier day" in _text([({"date": "2026-08-27"}, {})])
    assert "earlier day" not in _text([({"date": "2026-08-28"}, {})])


# ── it actually reaches the rendered email ────────────────────────────────

def test_the_note_is_rendered_into_the_block_not_just_computed():
    src = (ROOT / "scripts" / "gen_email.py").read_text(encoding="utf-8")
    assert "{_status_change_daynote(status_ch, report_date)}" in src, (
        "the note is computed but never rendered — a helper nobody calls "
        "fixes nothing")
    assert "report_date=report_date)" in src, (
        "_today_block_html is not being passed the report day, so the note "
        "silently returns '' in production while passing every unit test here")


def test_the_block_signature_still_defaults_so_other_callers_do_not_break():
    import inspect
    sig = inspect.signature(gen_email._today_block_html)
    assert sig.parameters["report_date"].default is None
