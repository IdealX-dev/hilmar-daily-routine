"""A derived reversal must carry the day the row DIED, not the day we noticed.

Michael, on an Oakland → Tokyo row reading "PENDING HILMAR → WIN" beside the
reason "Lonny replied Send — REVERSED, now LOSS (SEND_NO_BOOKING)": *"why is
it still win with no further change to loss"*. Then, when told the reversal
could not appear: *"why not? what is best way.. figure it out and manage it"*.

WHY IT COULD NOT APPEAR. `core.record_transition` defaults `at` to now, and
`age_requests` passed `at=now` explicitly. The promotion is stamped from
LONNY'S EMAIL (`ingest.py`, `at=sent_dt`); the reversal was stamped from the
PIPELINE'S CLOCK. Production fires at 06:30 ET with window=previous, so the
fire day is always one day AFTER the day being reported — which put every
derived reversal exactly one day outside the window. And it never caught up:
each fire rebuilds `status_history` and re-creates the reversal at that
morning's `now`, so it walked forward with the window forever. Measured
before the fix, one row, four consecutive fires:

    fire 08-25 -> reports 08-24 : WIN->PENDING stamped 08-25   (outside)
    fire 08-26 -> reports 08-25 : WIN->LOSS    stamped 08-26   (outside)
    fire 08-27 -> reports 08-26 : WIN->LOSS    stamped 08-27   (outside)
    fire 08-28 -> reports 08-27 : WIN->LOSS    stamped 08-28   (outside)

THE FIX is the one this codebase already made in the other direction on
2026-08-11, for the prior-build WIN restore: *"DATE THE RESTORE FROM THE
PRIOR EVIDENCE, NEVER FROM NOW."* Each staleness predicate already computed
a deadline internally and threw it away; `decide_status` now returns it as
`stale_at`, and the callers stamp with that.

The deadline is in the past by construction on any aging branch (stale means
now is past it), it is IDENTICAL on every later fire, and it falls on the
business day the row actually aged out — which is the day the report covers.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import core  # noqa: E402
import gen_email  # noqa: E402
import ingest  # noqa: E402

ET = core.ET
UTC = dt.timezone.utc


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


hilmar_core = _load(ROOT / "src" / "hilmar" / "core.py", "hilmar_core_reversal_test")


# ── the live shape: a Send that never books ───────────────────────────────

SEND_ET = dt.datetime(2026, 8, 24, 14, 0, tzinfo=ET)      # Monday 2 PM ET


def _row():
    """A quoted row Lonny accepted on Monday. OL never issues the MDOLX."""
    return {
        "request_id": "req_tokyo", "status": "PENDING", "quoted": True,
        "has_send": True, "mdolx_ref": None,
        "origin": "Oakland", "destination": "Tokyo", "lane": "Oakland → Tokyo",
        "request_timestamp": (SEND_ET - dt.timedelta(days=1)).isoformat(),
        "request_date": (SEND_ET - dt.timedelta(days=1)).date().isoformat(),
        "response_timestamp": SEND_ET.isoformat(),
        "teu_requested": 2, "teu_won": 2, "status_history": [],
    }


def _fire(day: dt.date):
    """One production fire: rebuild, re-apply the send promotion, age."""
    now = dt.datetime(day.year, day.month, day.day, 6, 30, tzinfo=ET)
    r = _row()
    # ingest.py:1763 — the promotion is stamped from Lonny's email, not now.
    core.record_transition(r, "WIN", "Lonny replied Send", at=SEND_ET)
    ingest.age_requests([r], now=now)
    return r, now


def _reversal(r):
    revs = [h for h in r["status_history"] if h.get("from") == "WIN"]
    assert len(revs) == 1, f"expected exactly one reversal, got {revs}"
    return revs[0]


def test_the_reversal_lands_on_the_day_the_row_actually_died():
    # The Monday 14:00 send goes stale Tuesday 14:00. The Wednesday fire is
    # the first to see it — and it reports TUESDAY.
    r, now = _fire(dt.date(2026, 8, 26))
    report_day = core.report_business_day(now, window="previous")
    assert r["status"] == "LOSS"
    assert gen_email._et_date(_reversal(r)["at"]) == report_day, (
        "the reversal is not dated on the business day the report covers — "
        "it cannot appear in STATUS CHANGES TODAY")


def test_the_reversal_actually_renders_in_the_report():
    # The end-to-end claim, not just the stamp: the section shows it.
    r, now = _fire(dt.date(2026, 8, 26))
    report_day = core.report_business_day(now, window="previous")
    changes = gen_email._today_events({"requests": [r]}, report_day)[2]
    pairs = [(h.get("from"), h.get("to")) for _row, h in changes]
    assert ("WIN", "LOSS") in pairs, (
        f"the reversal never reaches the report; section shows {pairs}")


def test_the_stamp_does_not_walk_forward_with_the_window():
    # THE DEFECT'S SIGNATURE. Every fire rebuilds status_history, so an
    # at=now stamp moved the reversal one day later every single morning and
    # stayed permanently outside the window. The deadline does not move.
    stamps = {}
    for d in (26, 27, 28):
        r, _now = _fire(dt.date(2026, 8, d))
        stamps[d] = _reversal(r)["at"]
    assert len(set(stamps.values())) == 1, (
        f"the reversal is re-dated on each fire and will never land in a "
        f"report window: {stamps}")


def test_it_is_reported_once_and_then_goes_quiet():
    # The flip side: having appeared on its own day it must not re-appear on
    # every later day. A reversal that renders forever is the same defect
    # with the sign flipped.
    shown = []
    for d in (26, 27, 28):
        r, now = _fire(dt.date(2026, 8, d))
        report_day = core.report_business_day(now, window="previous")
        changes = gen_email._today_events({"requests": [r]}, report_day)[2]
        if any(h.get("from") == "WIN" for _row, h in changes):
            shown.append(d)
    assert shown == [26], f"reversal rendered on fires {shown}, expected [26]"


# ── the deadline must never be invented, and never be in the future ───────

def _grid():
    """A wide sweep of decide_status inputs, aging and not."""
    base = dt.datetime(2026, 8, 17, 15, 0, tzinfo=UTC)     # a Monday
    for anchor_days in (0, 1, 3, 10, 60):
        for age_h in (1, 23, 25, 47, 73, 500):
            anchor = base + dt.timedelta(days=anchor_days)
            now = anchor + dt.timedelta(hours=age_h)
            for has_send in (True, False):
                for quoted in (True, False):
                    for mdolx in (None, "261145"):
                        for resp in (anchor.isoformat(), None):
                            for etd in (None, 0, 9):
                                yield dict(
                                    has_send=has_send, mdolx_ref=mdolx,
                                    response_timestamp=resp, quoted=quoted,
                                    etd_fit_days=etd,
                                    request_timestamp=anchor.isoformat(),
                                    now=now)


#: Loss reasons produced by a WINDOW EXPIRING. Each one is an aging event and
#: must be able to say when it came due. RESPONSE_NO_RATE is deliberately not
#: here: it fires on evidence (OL replied without a rate), not on a clock.
AGING_REASONS = {
    "SEND_NO_BOOKING", "NO_RESPONSE", "NO_RESPONSE_TS",
    "ETD_MISS", "PRICE", "UNDIFFERENTIATED", "QUOTED_NOT_BOOKED",
}


def test_every_aging_loss_can_say_when_it_came_due():
    # THE COMPLETENESS GUARD. A branch added later that forgets stale_at
    # silently reverts to the fire clock — the exact defect this file exists
    # for, and invisible in any single-row test. Only a dateless row may
    # answer None, and only because inventing a date is worse.
    missing = []
    for kw in _grid():
        d = core.decide_status(**kw)
        if d.status == "LOSS" and d.loss_reason in AGING_REASONS and d.stale_at is None:
            missing.append((d.loss_reason, kw["response_timestamp"], kw["quoted"]))
    assert not missing, (
        f"{len(missing)} aging losses carry no deadline and will be stamped "
        f"with the fire clock: {sorted(set(missing))[:6]}")


def test_a_deadline_is_never_in_the_future():
    # stale_at is stamped onto history verbatim. A future date would put a
    # reversal in a report that has not happened yet.
    for kw in _grid():
        d = core.decide_status(**kw)
        if d.stale_at is not None:
            assert d.stale_at <= kw["now"], (
                f"{d.loss_reason} deadline {d.stale_at} is after now "
                f"{kw['now']} — the row is not actually past it")


def test_nothing_that_is_not_an_aging_loss_claims_a_deadline():
    for kw in _grid():
        d = core.decide_status(**kw)
        if d.status != "LOSS" or d.loss_reason not in AGING_REASONS:
            assert d.stale_at is None, (
                f"{d.status}/{d.loss_reason} carries stale_at={d.stale_at}; "
                f"only an expired window may name one")


def test_a_row_with_no_clock_falls_back_to_now_rather_than_inventing_one():
    now = dt.datetime(2026, 8, 26, 10, 30, tzinfo=UTC)
    r = {"request_id": "req_dateless", "status": "PENDING", "quoted": False,
         "has_send": False, "mdolx_ref": None, "request_timestamp": None,
         "request_date": None, "response_timestamp": None,
         "status_history": [], "lane": "Oakland → Tokyo"}
    ingest.age_requests([r], now=now)
    assert r["status"] == "LOSS" and r["loss_reason"] == "NO_RESPONSE"
    at = core.parse_iso(r["status_history"][-1]["at"])
    assert at == now, (
        "a dateless row has no deadline to name; the caller must fall back "
        "to its own clock, not fabricate one")


# ── the refactor that made this possible must not have moved a boolean ────

@pytest.mark.parametrize("weekday_anchor", [
    "2026-08-17T13:00:00Z",   # Mon
    "2026-08-21T13:00:00Z",   # Fri — the weekend carve-out
    "2026-08-22T13:00:00Z",   # Sat
    "2026-08-23T13:00:00Z",   # Sun
    "2026-03-07T13:00:00Z",   # the day before spring-forward
    "2026-10-31T13:00:00Z",   # the day before fall-back
])
def test_each_deadline_agrees_with_its_own_predicate_to_the_second(weekday_anchor):
    # A deadline that disagreed with the bool it was extracted from would
    # date a reversal to a moment the row was not yet stale. Checked either
    # side of the boundary, including across both 2026 DST transitions.
    a = dt.datetime.fromisoformat(weekday_anchor.replace("Z", "+00:00"))
    for deadline_fn, predicate, kwargs in (
        (lambda x: core.business_stale_deadline(x, 24), core.is_business_stale, {"hours": 24}),
        (lambda x: core.pending_hilmar_deadline(x), core.pending_hilmar_stale, {}),
        (lambda x: core.pending_ol_deadline(x), core.pending_ol_stale, {}),
    ):
        deadline = deadline_fn(a)
        assert deadline is not None
        assert predicate(a, deadline - dt.timedelta(seconds=1), **kwargs) is False
        assert predicate(a, deadline + dt.timedelta(seconds=1), **kwargs) is True


def test_the_two_trees_name_the_same_deadline():
    # scripts/core.py and src/hilmar/core.py are paired; a drift here would
    # date a reversal differently in production than in the tested library.
    for iso in ("2026-08-17T13:00:00Z", "2026-08-21T20:00:00Z",
                "2026-03-07T13:00:00Z", "2026-10-31T13:00:00Z"):
        a = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        assert core.business_stale_deadline(a) == hilmar_core.business_stale_deadline(a)
        assert core.pending_hilmar_deadline(a) == hilmar_core.pending_hilmar_deadline(a)
        assert core.pending_ol_deadline(a) == hilmar_core.pending_ol_deadline(a)
    assert core.business_stale_deadline(None) is hilmar_core.business_stale_deadline(None) is None
    assert core.pending_ol_deadline(None) is hilmar_core.pending_ol_deadline(None) is None


def test_a_genuinely_old_backfill_is_still_filtered_out_of_the_day_feed():
    # The composition that matters. _is_current_status_change exists BECAUSE
    # of this defect's older half (2026-08-13: 249 transitions landing on one
    # day). Honest dating does not make it redundant — a row aged out in
    # April and only now written down is dated APRIL, so it is out of the
    # window on date alone, and the lateness filter agrees. The two must not
    # start fighting: a same-day aging stays IN, a months-late one stays OUT.
    old_send = dt.datetime(2026, 4, 6, 14, 0, tzinfo=ET)
    r = _row()
    r["response_timestamp"] = old_send.isoformat()
    r["request_timestamp"] = (old_send - dt.timedelta(days=1)).isoformat()
    now = dt.datetime(2026, 8, 26, 6, 30, tzinfo=ET)
    core.record_transition(r, "WIN", "Lonny replied Send", at=old_send)
    ingest.age_requests([r], now=now)
    report_day = core.report_business_day(now, window="previous")
    assert gen_email._et_date(_reversal(r)["at"]) < report_day
    changes = gen_email._today_events({"requests": [r]}, report_day)[2]
    assert not any(h.get("from") == "WIN" for _row, h in changes), (
        "an April aging is being reported as today's news")
