"""Not-Quoted counting restarts Monday, because the old count was wrong.

Michael 2026-08-14, on a report section reading "Not Quoted — Last 14 Days
(9 listed • 25 total • 104 TEU)": "get rid of thjs as all quoted / and
restart the count monday."

NQ means "Lonny asked and OL never answered". Every row in that section HAD
been answered — the replies went To: Lonny with the group copied and never
reached the mailbox this pipeline reads. That is the same root cause behind
the empty OL-USA RESPONSES section and the turnaround reset. The label
measured our visibility, not OL's behaviour, and it was being shown to the
CEO as a list of OL failures.

Same mechanism as TIMING_VALID_FROM, deliberately: a floor, an explicit
count of what it excluded, a banner that says so, and one line to retire it.

THE LINE THIS MUST NOT CROSS: wins, Q&L, TEU volumes and win rate are
untouched. NQ has never been in the win-rate denominator, so suppressing it
cannot move the number — and if it ever does, that is a bug this file
catches.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import gen_email as GE  # noqa: E402

FLOOR = "2026-08-17"


def _nq(rid, date):
    """A Not-Quoted row in LEGACY storage form (LOSS + quoted=False)."""
    return {"request_id": rid, "status": "LOSS", "quoted": False,
            "loss_reason": "NO_RESPONSE", "request_date": date,
            "request_timestamp": f"{date}T15:00:00Z",
            "lane": "Oakland → Xingang", "origin": "Oakland",
            "destination": "Xingang", "teu_requested": 2}


def _win(rid, date):
    return {"request_id": rid, "status": "WIN", "quoted": True,
            "request_date": date, "request_timestamp": f"{date}T15:00:00Z",
            "lane": "Oakland → Yokohama", "origin": "Oakland",
            "destination": "Yokohama", "teu_requested": 2, "teu_won": 2}


def _ql(rid, date):
    return {"request_id": rid, "status": "LOSS", "quoted": True,
            "request_date": date, "request_timestamp": f"{date}T15:00:00Z",
            "lane": "Oakland → Algeciras", "origin": "Oakland",
            "destination": "Algeciras", "teu_requested": 2}


# ── the floor itself ────────────────────────────────────────────────

def test_the_floor_is_the_monday_michael_named():
    assert core.NQ_VALID_FROM == FLOOR


@pytest.mark.parametrize("date,ok", [
    ("2026-08-17", True),    # the floor day counts
    ("2026-08-18", True),
    ("2026-08-16", False),   # the day before does not
    ("2026-07-31", False),   # the oldest row in Michael's screenshot
])
def test_nq_is_valid_reads_the_date_only(date, ok):
    assert core.nq_is_valid(date) is ok


def test_a_row_with_no_date_still_counts():
    """An undateable NQ row is a data defect and must stay visible, not hide
    behind the floor."""
    assert core.nq_is_valid(None) is True
    assert core.nq_is_valid("") is True


def test_counts_as_not_quoted_combines_both_questions():
    assert core.counts_as_not_quoted(_nq("new", "2026-08-18")) is True
    assert core.counts_as_not_quoted(_nq("old", "2026-08-05")) is False
    # A quoted loss is Q&L and was never NQ, floor or no floor.
    assert core.counts_as_not_quoted(_ql("ql", "2026-08-18")) is False


# ── the aggregate ───────────────────────────────────────────────────

def test_pre_floor_rows_are_excluded_and_counted():
    rows = [_nq("a", "2026-07-31"), _nq("b", "2026-08-05"),
            _nq("c", "2026-08-18")]
    s = core.aggregate_summary(rows)
    assert s["not_quoted"] == 1
    assert s["not_quoted_excluded"] == 2
    assert s["nq_valid_from"] == FLOOR


def test_teu_not_quoted_follows_the_same_floor():
    """The TEU tally and the count must move together — two numbers off one
    dataset is how QC-020b's failure mode starts."""
    s = core.aggregate_summary([_nq("a", "2026-07-31"), _nq("c", "2026-08-18")])
    assert s["not_quoted"] == 1
    assert s["teu_not_quoted"] == 2


def test_win_rate_and_volumes_are_untouched():
    """THE REQUIRED NEGATIVE. NQ is not in the win-rate denominator; if
    suppressing NQ moves win rate, something is wired wrong."""
    base = [_win("w1", "2026-08-18"), _ql("l1", "2026-08-18")]
    without = core.aggregate_summary(base)
    with_old_nq = core.aggregate_summary(base + [_nq("old", "2026-07-31")])
    assert with_old_nq["win_rate"] == without["win_rate"] == 50.0
    assert with_old_nq["wins"] == without["wins"] == 1
    assert with_old_nq["quoted_lost"] == without["quoted_lost"] == 1
    assert with_old_nq["teu_won"] == without["teu_won"]


# ── the report ──────────────────────────────────────────────────────

def test_the_listing_drops_pre_floor_rows():
    data = {"requests": [_nq("a", "2026-07-31"), _nq("c", "2026-08-18")]}
    got = [r["request_id"] for r in GE._not_quoted_rows(data, cutoff_days=None)]
    assert got == ["c"]


def test_the_rate_negotiation_aggregate_uses_the_same_predicate():
    """_not_quoted_aggregate feeds the "N total • N TEU" header. If it kept
    counting pre-floor rows the header would contradict the section."""
    data = {"requests": [_nq("a", "2026-07-31"), _nq("c", "2026-08-18")]}
    assert [r["request_id"] for r in GE._not_quoted_aggregate(data)] == ["c"]


def test_the_banner_states_the_reset_and_protects_the_totals():
    html = GE._nq_reset_banner()
    assert FLOOR in html
    assert "did</em> answer" in html or "did answer" in html
    assert "win rate are" in html.replace("\n", " ")


def test_retiring_the_floor_removes_the_banner_and_the_exclusion(monkeypatch):
    """One line in both directions, same as the timing reset."""
    monkeypatch.setattr(core, "NQ_VALID_FROM", "")
    assert GE._nq_reset_banner() == ""
    assert core.nq_is_valid("2026-01-01") is True
    s = core.aggregate_summary([_nq("a", "2026-07-31")])
    assert s["not_quoted"] == 1
    assert s["not_quoted_excluded"] == 0


# ─────────────────────────────────────────────────────────────────────
# THE TRAP THIS RESET NEARLY WALKED INTO.
#
# Every NQ bucket except the summary sits in an if/elif chain whose NEXT
# branch is Quoted & Lost. Flooring them naively — `elif counts_as_not_quoted`
# — drops a floored row straight through into Q&L: losses inflate, win rate
# moves, and the one guarantee Michael was given ("wins, losses and win rate
# unaffected") breaks silently. Each site therefore captures on
# is_not_quoted and counts on the floor INSIDE that branch.
#
# QC-075 reconciles the trade-region rollup against the summary on every
# fire, so the floored row must also stay counted in `requests` — matching
# summary.total_entries — or the check fires red every morning.
# ─────────────────────────────────────────────────────────────────────

def test_a_floored_row_never_becomes_a_loss_in_trade_regions():
    regions = core.aggregate_trade_regions([_nq("old", "2026-07-31")])
    tot = {"nq": 0, "ql": 0, "req": 0}
    for m in regions.values():
        tot["nq"] += m["not_quoted"]
        tot["ql"] += m["quoted_lost"]
        tot["req"] += m["requests"]
    assert tot["ql"] == 0, "a floored NQ row fell through into Quoted & Lost"
    assert tot["nq"] == 0, "the floor did not apply to trade regions"
    assert tot["req"] == 1, (
        "the row must still count in requests — QC-075 reconciles that "
        "against summary.total_entries")


def test_trade_regions_still_reconcile_to_the_summary():
    """QC-075's exact comparison, on a mixed dataset."""
    rows = [_win("w", "2026-08-18"), _ql("l", "2026-08-18"),
            _nq("old", "2026-07-31"), _nq("new", "2026-08-18")]
    s = core.aggregate_summary(rows)
    regions = core.aggregate_trade_regions(rows)
    assert sum(m["requests"] for m in regions.values()) == s["total_entries"]
    assert sum(m["wins"] for m in regions.values()) == s["wins"]
    assert sum(m["quoted_lost"] for m in regions.values()) == s["quoted_lost"]
    assert sum(m["not_quoted"] for m in regions.values()) == s["not_quoted"]


def test_the_8_week_rollup_does_not_convert_a_floored_row_to_ql():
    weeks = GE._week_rows({"requests": [_nq("old", "2026-07-31")]})
    assert all(b["ql"] == 0 for _, b in weeks), "floored NQ became Q&L"
    assert all(b["nq"] == 0 for _, b in weeks)


def test_the_lane_tables_do_not_convert_a_floored_row_to_a_lost_lane():
    """A floored row reported as a competitive loss would put a lane in the
    Losing table it was never quoted on."""
    lanes = GE._build_lane_buckets({"requests": [_nq("old", "2026-07-31")]})
    for b in lanes.values():
        assert b.get("ql", 0) == 0, "floored NQ counted as a lane loss"
        assert b.get("nq", 0) == 0
        assert (b.get("teu_lost") or 0) == 0
