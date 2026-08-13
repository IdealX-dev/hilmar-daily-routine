"""The response clock is off, and the reports have to say so.

Michael, 2026-08-13: "WHEN RUNNING KPI'S JUST INDICATE THE TURN AROUND CLOCK
AND SUCH IS OFF AND START RUNNING IT AGAIN STARTING TODAY AND INDICATE THAT
ON THE REPORTS."

For roughly Jul 1 - Aug 12, OL's replies were not reaching the mailbox this
pipeline reads — they went To: Lonny, Cc: the group. Every turnaround figure
measured over that period is a clock that was started and never stopped,
against whatever fraction of replies happened to arrive.

Two failure modes are worth more than the feature itself, and both are
tested here:

  1. A suppressed metric rendering as 0.0h. "0.0h avg response" is not a
     missing number, it is a FLATTERING one — it says OL replied instantly.
     That is the fabrication this project exists to not commit.
  2. A suppressed metric with no explanation. The reader assumes nothing
     broke, or assumes zero. The banner is the deliverable, not the filter.

Win, loss and TEU figures are deliberately NOT affected: those reconcile
against OL's own booking export, which does not depend on mail timing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import core as C  # noqa: E402
import gen_email as GE  # noqa: E402

from hilmar import core as HC  # noqa: E402

FLOOR = "2026-08-13"


def _req(ts, biz=4.0, clock=6.0, status="WIN", **kw):
    r = {"request_id": f"r_{ts}", "request_timestamp": ts, "status": status,
         "turnaround_biz_hours": biz, "turnaround_hours": clock,
         "destination": "Yokohama", "teu_requested": 2, "quoted": True,
         "carrier_quoted": "ONE", "carrier_won": "ONE", "teu_won": 2}
    r.update(kw)
    return r


# ── the floor itself ─────────────────────────────────────────────────────

def test_the_floor_is_the_day_michael_called_it():
    assert C.TIMING_VALID_FROM == FLOOR


@pytest.mark.parametrize("ts,ok", [
    ("2026-08-13T09:00:00Z", True),      # the floor day counts
    ("2026-08-14T09:00:00Z", True),
    ("2026-08-12T23:59:59Z", False),     # the day before does not
    ("2026-07-15T09:00:00Z", False),
    ("", False),                         # no timestamp is not "valid"
    (None, False),
])
def test_timing_is_valid_reads_the_date_only(ts, ok):
    assert C.timing_is_valid(ts) is ok


def test_a_datetime_works_as_well_as_a_string():
    from datetime import datetime, timezone
    assert C.timing_is_valid(datetime(2026, 8, 14, tzinfo=timezone.utc))
    assert not C.timing_is_valid(datetime(2026, 7, 1, tzinfo=timezone.utc))


# ── what summarize does with it ──────────────────────────────────────────

def test_pre_floor_samples_are_excluded_and_counted():
    s = C.aggregate_summary([_req("2026-07-05T15:00:00Z"), _req("2026-07-06T15:00:00Z")])
    assert s["turnaround_entries"] == 0
    assert s["turnaround_excluded"] == 2
    assert s["turnaround_valid_from"] == FLOOR


def test_no_measurable_sample_is_null_not_zero():
    """THE bug this guards: 0.0h reads as an instant reply."""
    s = C.aggregate_summary([_req("2026-07-05T15:00:00Z")])
    assert s["turnaround_avg_biz_hours"] is None, (
        "a suppressed average rendered as a number")
    assert s["turnaround_avg_clock_hours"] is None


def test_post_floor_samples_are_measured_normally():
    s = C.aggregate_summary([_req("2026-08-13T15:00:00Z", biz=3.0),
                             _req("2026-08-14T15:00:00Z", biz=5.0)])
    assert s["turnaround_entries"] == 2
    assert s["turnaround_avg_biz_hours"] == 4.0
    assert s["turnaround_excluded"] == 0


def test_a_mixed_period_averages_only_the_valid_half():
    s = C.aggregate_summary([_req("2026-07-05T15:00:00Z", biz=99.0),
                             _req("2026-08-14T15:00:00Z", biz=3.0)])
    assert s["turnaround_entries"] == 1
    assert s["turnaround_avg_biz_hours"] == 3.0, (
        "a pre-floor sample leaked into the average")
    assert s["turnaround_excluded"] == 1


def test_wins_and_teu_are_untouched_by_the_reset():
    """The reset is about TIMING only. Wins reconcile against OL's booking
    export, which does not care which mailbox a message reached."""
    s = C.aggregate_summary([_req("2026-07-05T15:00:00Z"), _req("2026-07-06T15:00:00Z")])
    assert s["wins"] == 2 and s["teu_won"] == 4
    assert s["win_rate"] == 100.0


def test_carrier_turnaround_also_honours_the_floor():
    """A per-carrier average built from pre-floor samples would rank
    carriers on a clock that was never stopped."""
    car = C.aggregate_carriers([_req("2026-07-05T15:00:00Z", biz=99.0)])
    assert car["ONE"]["avg_turnaround_biz_hours"] is None
    car2 = C.aggregate_carriers([_req("2026-08-14T15:00:00Z", biz=3.0)])
    assert car2["ONE"]["avg_turnaround_biz_hours"] == 3.0


def test_the_two_cores_agree():
    rows = [_req("2026-07-05T15:00:00Z"), _req("2026-08-14T15:00:00Z", biz=3.0)]
    a, b = C.aggregate_summary(rows), HC.aggregate_summary(rows)
    for k in ("turnaround_entries", "turnaround_avg_biz_hours",
              "turnaround_excluded", "turnaround_valid_from"):
        assert a[k] == b[k], f"{k} drifted between the trees"


# ── what the reports say ─────────────────────────────────────────────────

def test_the_note_names_the_date_and_the_cause():
    note = C.timing_reset_note()
    assert FLOOR in note
    assert "Lonny" in note and "Cc" in note, "the note does not say WHY"
    assert "unaffected" in note, (
        "the note must say wins and volumes still stand, or a reader "
        "discounts the whole report")


def test_the_banner_says_off_when_nothing_is_measurable():
    html = GE._timing_reset_banner(0, 12)
    assert "OFF" in html and FLOOR in html
    assert "12 earlier samples excluded" in html


def test_the_banner_reports_the_live_count_once_measuring():
    html = GE._timing_reset_banner(3, 0)
    assert "now measuring 3 quotes" in html
    assert "excluded" not in html


def test_the_banner_counts_one_sample_in_the_singular():
    assert "1 earlier sample excluded" in GE._timing_reset_banner(0, 1)
    assert "now measuring 1 quote." in GE._timing_reset_banner(1, 0)


def test_retiring_the_reset_removes_the_banner(monkeypatch):
    """Clearing the constant must leave no orphaned notice claiming a reset
    that is no longer in force."""
    monkeypatch.setattr(C, "TIMING_VALID_FROM", "")
    assert GE._timing_reset_banner(0, 0) == ""
    assert C.timing_reset_note() == ""
    assert C.timing_is_valid("2020-01-01T00:00:00Z") is True


def test_the_daily_email_carries_the_banner():
    """The KPI block is where the number used to be; the explanation has to
    be in the same place, not a page away."""
    summary = C.aggregate_summary([_req("2026-07-05T15:00:00Z")])
    html = GE._kpi_block_html(summary, requests=[_req("2026-07-05T15:00:00Z")])
    assert "Turnaround clock reset" in html
    assert ">OFF<" in html, "the Avg Biz-Hrs card still shows a number"
    assert "0.0h" not in html


def test_the_dashboard_tile_and_tab_both_say_it():
    src = (ROOT / "scripts" / "gen_dashboard.py").read_text(encoding="utf-8")
    assert "avg_biz_tile" in src and "{avg_biz}h" not in src, (
        "the dashboard tile still formats a raw number")
    assert "timing_banner" in src, "the turnaround tab has no reset notice"


def test_the_pdf_explains_the_empty_page():
    """The old copy said 'populated once quoted requests accumulate', which
    reads as a startup state rather than a deliberate withdrawal."""
    src = (ROOT / "scripts" / "gen_pdf.py").read_text(encoding="utf-8")
    assert "timing_reset_note" in src
    assert "TIMING_VALID_FROM" in src


def test_the_schema_allows_the_null():
    """summarize returns null now; a schema that still demands a number
    makes QC-Phase-10 fail on every clean run."""
    import json
    s = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
    props = s["definitions"]["summary"]["properties"]
    assert "null" in props["turnaround_avg_biz_hours"]["type"]
    assert "null" in props["turnaround_avg_clock_hours"]["type"]
    assert props["turnaround_excluded"]["type"] == "integer"
    assert "turnaround_valid_from" in props
