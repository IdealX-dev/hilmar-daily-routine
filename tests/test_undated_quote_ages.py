"""A quote with no response timestamp must still age out to a loss.

Michael, verbatim: "if you have the quotes and you do not see a booking for
the quote, then it's a loss  that's it".

The 2026-08-12 report carried this banner:

    21 further quotes are recorded with a rate or carrier but no response
    time, so they cannot be dated and are not counted above. They appear
    under PENDING HILMAR.

Twenty-one rows holding a rate, with no booking, sitting PENDING. Not for a
day — structurally, forever.

WHERE IT ACTUALLY WAS. The obvious suspect was
`pending_hilmar_stale`'s `if resp_dt is None: return False`, and that suspect
is innocent: every one of its call sites already guards the argument, so the
line is unreachable and changing it alone is a no-op that looks like a fix.
decide_status's QUOTE-aging branch also already falls back to Lonny's request
(the `NO_RESPONSE_TS` block) — measured: a 3-week-old quoted row with no send
and no MDOLX returned LOSS/NO_RESPONSE_TS before this change and after it.

The hole was one branch earlier. On `has_send and not has_mdolx`, `send_at`
was derived ONLY from response_timestamp and send_signal_events, and
is_business_stale returns False on None. A row with neither — exactly what
patch_carriers produces when it recovers a rate from a sibling thread or a
booking PDF — had NO clock on that branch at all. Measured before the fix:
PENDING/AWAITING_MDOLX at +1d, +30d, +365d and +3650d, identically. Lonny
accepted, OL never booked, and the row was never a loss. pending_substate
keys off `quoted`, so it rendered under PENDING HILMAR — the banner's
population.

AND NOBODY SAW IT, because all three detectors skipped undated rows on the
way in: QC-007 (`if rt and ...`), gen_improvements_report (`if resp_dt is
None: continue`) and auto_chase_pending (`if not r.get("response_timestamp"):
continue`). A stuck row raised nothing and got no chase.

THE LINE THIS MUST NOT CROSS: "Do not call a quote given TODAY a loss." Every
positive test below is paired with a today-anchored negative.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import auto_chase_pending as ACP  # noqa: E402
import core  # noqa: E402
import gen_improvements_report as GIR  # noqa: E402
import qc_selfheal  # noqa: E402

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

NOW = dt.datetime(2026, 8, 13, 17, 0, tzinfo=UTC)
THREE_WEEKS_AGO = dt.datetime(2026, 7, 23, 17, 0, tzinfo=UTC)   # a Thursday
TODAY = NOW - dt.timedelta(hours=2)

#: TWO CLOCKS, ON PURPOSE (2026-08-14). The pure-function tests above pass
#: `now` explicitly, so the FROZEN clock (NOW) keeps them deterministic —
#: including the weekday-sensitive boundary cases. The DETECTORS below
#: (qc_selfheal.phase_6_rules, collect_red_flags, _find_overdue_pending) read
#: the REAL clock internally and accept no override, so their fixtures must be
#: built relative to real time. Freezing those too is the bug this comment
#: replaces: "a request from TODAY" silently became a request from yesterday
#: when the calendar rolled, and four negatives started failing exactly 22
#: hours after they were written. Detector-positive fixtures use ages past the
#: 72h Friday window so they fire on ANY weekday; detector-negative ("today")
#: fixtures stay hours old, safe on any weekday.
REAL_NOW = dt.datetime.now(UTC)
REAL_TODAY = REAL_NOW - dt.timedelta(hours=2)


def _row(**kw) -> dict:
    """The shape the banner counts: quote evidence, no response timestamp."""
    base = dict(has_send=False, mdolx_ref=None, quoted=True, etd_fit_days=None,
                response_timestamp=None,
                request_timestamp=THREE_WEEKS_AGO.isoformat(),
                ol_rate="$3,500", now=NOW)
    base.update(kw)
    return base


# ─────────────────────────────────────────────────────────────────────
# pending_hilmar_stale — the fallback anchor
# ─────────────────────────────────────────────────────────────────────

def test_two_arg_callers_are_bit_for_bit_unchanged():
    """request_dt defaults to None, so `anchor = resp_dt` for every existing
    call — including the None case. This is what makes the signature change
    safe to ship without touching six call sites at once."""
    for resp in (None, THREE_WEEKS_AGO, TODAY,
                 dt.datetime(2026, 7, 10, 12, 55, tzinfo=ET)):
        for now in (NOW, NOW + dt.timedelta(days=400)):
            assert (core.pending_hilmar_stale(resp, now)
                    is core.pending_hilmar_stale(resp, now, request_dt=None))


def test_request_dt_is_keyword_only():
    """Positionally it would land in `now`. That is the same class of mistake
    that once put a hardcoded 24h literal in QC-007 while decide_status ran
    24h/72h-Friday — a silent, plausible-looking wrong answer."""
    with pytest.raises(TypeError):
        core.pending_hilmar_stale(None, NOW, THREE_WEEKS_AGO)


def test_the_request_anchor_ages_an_undated_quote():
    assert core.pending_hilmar_stale(None, NOW, request_dt=THREE_WEEKS_AGO) is True


def test_a_request_from_today_does_not_age():
    """THE REQUIRED NEGATIVE. core.PENDING_HILMAR_LOSS_HOURS is the window;
    inside it there is no loss, whichever anchor is carrying the clock."""
    assert core.pending_hilmar_stale(None, NOW, request_dt=TODAY) is False


def test_the_quote_anchor_still_wins_when_both_are_present():
    """A fallback, not an override. The request is always EARLIER than the
    quote, so preferring it would expire rows sooner than the rule says."""
    old_req = NOW - dt.timedelta(days=30)
    fresh_quote = NOW - dt.timedelta(hours=1)
    assert core.pending_hilmar_stale(fresh_quote, NOW, request_dt=old_req) is False


def test_no_clock_at_all_is_still_not_stale():
    """Neither timestamp means missing EVIDENCE, not elapsed time. The row
    holds PENDING and surfaces as a data defect. Inventing a date here is the
    fabricated-timing failure core.TIMING_VALID_FROM exists because of."""
    assert core.pending_hilmar_stale(None, NOW, request_dt=None) is False


def test_friday_carve_out_applies_to_the_request_anchor_too():
    """The 72h Friday window is a property of the ANCHOR's weekday, and on the
    fallback path the anchor is Lonny's request. A Friday request must get the
    same weekend carry a Friday quote gets — otherwise the fallback is
    stricter than the rule it stands in for."""
    fri = dt.datetime(2026, 7, 10, 12, 0, tzinfo=ET)     # Friday
    assert fri.weekday() == 4
    assert core.pending_hilmar_stale(None, fri + dt.timedelta(hours=71),
                                     request_dt=fri) is False
    assert core.pending_hilmar_stale(None, fri + dt.timedelta(hours=73),
                                     request_dt=fri) is True
    thu = dt.datetime(2026, 7, 9, 12, 0, tzinfo=ET)      # Thursday
    assert core.pending_hilmar_stale(None, thu + dt.timedelta(hours=25),
                                     request_dt=thu) is True


def test_both_trees_agree_on_the_fallback():
    """scripts/ is what the Cloud PC runs; src/hilmar/ is what the coverage
    gate measures. On 2026-05-30 they had drifted in LOGIC for over a month
    while CI stayed green."""
    import hilmar.core as hc
    for resp in (None, THREE_WEEKS_AGO):
        for req in (None, THREE_WEEKS_AGO, TODAY):
            assert (core.pending_hilmar_stale(resp, NOW, request_dt=req)
                    is hc.pending_hilmar_stale(resp, NOW, request_dt=req))


# ─────────────────────────────────────────────────────────────────────
# decide_status — the branch that actually had no clock
# ─────────────────────────────────────────────────────────────────────

def test_three_week_old_quote_with_a_send_and_no_booking_is_a_loss():
    """THE FIX. Before: PENDING/AWAITING_MDOLX at any age, forever.
    After: LOSS/SEND_NO_BOOKING. Michael's rule, applied."""
    d = core.decide_status(**_row(has_send=True))
    assert d.status == "LOSS"
    assert d.loss_reason == "SEND_NO_BOOKING"
    assert d.has_send is True, (
        "has_send is EVIDENCE, not state — erasing it relabels the row "
        "UNDIFFERENTIATED on the next self-heal pass")


@pytest.mark.parametrize("age_days", [2, 30, 365, 3650])
def test_the_send_branch_now_has_a_clock_at_every_age(age_days):
    """Pins the measurement that identified the defect: before the fix +1d,
    +30d, +365d and +3650d ALL returned PENDING identically, which is what
    "never ages" looks like when you probe it.

    Starts at 2d, not 1d, because is_business_stale compares with a strict
    `>` — at exactly 24h the row is ON the deadline and correctly not yet
    stale. That boundary is pinned separately below rather than smuggled in
    here, so a future change to the comparison fails one obvious test instead
    of this one for an unrelated-looking reason."""
    req = NOW - dt.timedelta(days=age_days)
    d = core.decide_status(**_row(has_send=True, request_timestamp=req.isoformat()))
    assert d.status == "LOSS"


def test_the_send_window_boundary_is_exclusive():
    """Measured, and NOT a defect: exactly 24h after a midweek request the row
    is still PENDING; a minute later it is a loss. Documented because a
    24h-old row reading PENDING otherwise looks like the bug this file fixes."""
    req = NOW - dt.timedelta(hours=24)
    assert req.astimezone(ET).weekday() < 4, "boundary case must stay midweek"
    assert core.decide_status(
        **_row(has_send=True, request_timestamp=req.isoformat())).status == "PENDING"
    req2 = NOW - dt.timedelta(hours=24, minutes=1)
    assert core.decide_status(
        **_row(has_send=True, request_timestamp=req2.isoformat())).status == "LOSS"


def test_a_send_on_a_request_from_today_is_not_a_loss():
    """THE REQUIRED NEGATIVE for this branch. Lonny accepted this morning and
    OL has not booked yet — that is normal business in flight, and the row
    must stay PENDING so a later fire can promote it to WIN."""
    d = core.decide_status(**_row(has_send=True, request_timestamp=TODAY.isoformat()))
    assert d.status == "PENDING"
    assert d.loss_reason == "AWAITING_MDOLX"


def test_a_send_row_with_no_clock_at_all_stays_pending():
    """is_business_stale must keep returning False on None. A row with no
    request AND no response timestamp is a DATA defect for QC-007 to raise,
    not a loss to assert."""
    d = core.decide_status(**_row(has_send=True, request_timestamp=None))
    assert d.status == "PENDING"


def test_a_real_send_event_still_outranks_the_request_fallback():
    """send_signal_events is the canonical clock. The request is the last
    resort, never a replacement — otherwise a stale request would age out a
    send Lonny gave us an hour ago."""
    d = core.decide_status(**_row(
        has_send=True,
        send_signal_events=[{"at": TODAY.isoformat()}]))
    assert d.status == "PENDING"
    assert d.loss_reason == "AWAITING_MDOLX"


def test_the_plain_undated_quote_was_already_handled_and_is_unchanged():
    """Guards against a regression in the OTHER direction. decide_status's
    quote-aging branch already anchored on the request; this fix must not
    have disturbed it."""
    aged = core.decide_status(**_row())
    assert (aged.status, aged.loss_reason) == ("LOSS", "NO_RESPONSE_TS")
    fresh = core.decide_status(**_row(request_timestamp=TODAY.isoformat()))
    assert (fresh.status, fresh.loss_reason) == ("PENDING", "NO_RESPONSE_TS")


def test_a_booking_without_a_send_is_still_not_a_loss():
    """OUT OF SCOPE, PINNED SO IT STAYS THAT WAY. `has_mdolx and not has_send`
    holds PENDING/MDOLX_NO_SEND with no clock consulted at all — unbounded,
    and a real gap. But there IS a booking, so under Michael's rule ("you do
    not see a booking") it is not a loss. It needs an ops-review SLA, not this
    change. Anyone who ages it here is answering a question nobody asked."""
    d = core.decide_status(**_row(mdolx_ref="MDOLX260980"))
    assert d.status == "PENDING"
    assert d.loss_reason == "MDOLX_NO_SEND"


def test_decide_status_parity_on_the_undated_send_row():
    import hilmar.core as hc
    for age_days, req_ts in ((21, THREE_WEEKS_AGO), (0, TODAY)):
        a = core.decide_status(**_row(has_send=True, request_timestamp=req_ts.isoformat()))
        b = hc.decide_status(has_send=True, mdolx_ref=None, quoted=True,
                             etd_fit_days=None, response_timestamp=None,
                             request_timestamp=req_ts.isoformat(),
                             ol_rate="$3,500", now=NOW)
        assert (a.status == "PENDING") == (b.status == "PENDING"), (
            f"trees disagree at {age_days}d: {a.status} vs {b.status}")


# ─────────────────────────────────────────────────────────────────────
# The three detectors that were blind
# ─────────────────────────────────────────────────────────────────────

def _pending_row(**kw) -> dict:
    base = {
        "request_id": "REQ-UNDATED-1",
        "status": "PENDING",
        "quoted": True,
        "has_send": False,
        "ol_rate": 3500,
        "lane": "Oakland → Algeciras",
        "response_timestamp": None,
        "request_timestamp": THREE_WEEKS_AGO.isoformat(),
    }
    base.update(kw)
    return base


def test_qc007_now_fires_on_a_stuck_undated_quote(capsys):
    log = qc_selfheal.Log()
    qc_selfheal.phase_6_rules(log, {"requests": [_pending_row()]})
    hits = [e for e in log.errors if "QC-007" in e]
    assert hits, "QC-007 is still blind to undated quotes"
    assert "quote undated" in hits[0] or "request" in hits[0], (
        "the message must say which clock it measured from — a request anchor "
        f"reported as a quote time is fabricated timing: {hits[0]!r}")


def test_qc007_stays_silent_on_a_quote_from_today():
    """THE REQUIRED NEGATIVE. Widening the detector must not make it shout at
    live business."""
    log = qc_selfheal.Log()
    qc_selfheal.phase_6_rules(
        log, {"requests": [_pending_row(request_timestamp=REAL_TODAY.isoformat())]})
    assert not [e for e in log.errors if "QC-007" in e]


@pytest.mark.parametrize("row,why", [
    (_pending_row(quoted=False), "PENDING_OL — OL has not quoted yet, chase OL not Lonny"),
    (_pending_row(loss_reason="AWAITING_MDOLX", has_send=True),
     "decide_status holds this on purpose while the booking is in flight"),
    (_pending_row(loss_reason="MDOLX_NO_SEND"),
     "held for ops review; there IS a booking"),
])
def test_qc007_does_not_fire_on_rows_the_state_machine_holds_on_purpose(row, why):
    """Dropping `if rt` also dropped the scoping it was doing by accident:
    `pending` is EVERY PENDING row. Without explicit scoping QC-007 starts
    erroring on rows decide_status is correctly holding — the same drift the
    comment above it was written about, in the other direction."""
    log = qc_selfheal.Log()
    qc_selfheal.phase_6_rules(log, {"requests": [row]})
    assert not [e for e in log.errors if "QC-007" in e], why


def test_improvements_report_names_the_stuck_undated_row():
    flags = GIR.collect_red_flags({"requests": [_pending_row()]}, {}, {})
    hits = [f for f in flags if "Pending past stale window" in f["title"]]
    assert hits, "the audit still cannot see an undated stuck quote"
    detail = hits[0]["detail"]
    assert "requested" in detail and "quote undated" in detail, (
        f"a request anchor is being reported as a quote time: {detail!r}")
    assert "quoted " not in detail, f"claims a quote time it does not have: {detail!r}"
    assert str(core.PENDING_HILMAR_LOSS_HOURS) in detail, (
        "cites a window the predicate does not enforce")


def test_improvements_report_still_says_quoted_when_the_quote_is_dated():
    old_quote = (REAL_NOW - dt.timedelta(days=5)).isoformat()
    flags = GIR.collect_red_flags(
        {"requests": [_pending_row(response_timestamp=old_quote)]}, {}, {})
    hits = [f for f in flags if "Pending past stale window" in f["title"]]
    assert hits and "quoted" in hits[0]["detail"]
    assert "quote undated" not in hits[0]["detail"]


def test_improvements_report_stays_quiet_on_a_request_from_today():
    flags = GIR.collect_red_flags(
        {"requests": [_pending_row(request_timestamp=REAL_TODAY.isoformat())]}, {}, {})
    assert not [f for f in flags if "Pending past stale window" in f["title"]]


def test_auto_chase_selects_the_undated_row_and_marks_the_anchor():
    got = ACP._find_overdue_pending({"requests": [_pending_row()]}, min_age_hours=24)
    assert len(got) == 1
    assert got[0]["_age_dated"] is False


def test_auto_chase_does_not_chase_a_request_from_today():
    got = ACP._find_overdue_pending(
        {"requests": [_pending_row(request_timestamp=REAL_TODAY.isoformat())]},
        min_age_hours=24)
    assert got == []


def test_the_chase_email_never_claims_a_quote_time_it_cannot_evidence():
    """THIS ONE GOES TO THE CLIENT. Telling Lonny "your quote from 21 days
    ago" off a REQUEST timestamp asserts a quote time we do not have, over
    Michael's signature. Fabricated timing has shipped from this repo once
    already."""
    row = ACP._find_overdue_pending({"requests": [_pending_row()]}, 24)[0]
    subject, body = ACP._build_chase_email(row)
    assert "quote from" not in body
    assert "the quote is" not in body
    assert "request from" in body
    assert "carries no timestamp" in body


def test_the_chase_email_is_unchanged_when_the_quote_is_dated():
    """The old copy is correct whenever we actually have a quote time, and it
    must not regress into hedged language for rows that never had a problem."""
    old_quote = (REAL_NOW - dt.timedelta(days=6)).isoformat()
    row = ACP._find_overdue_pending(
        {"requests": [_pending_row(response_timestamp=old_quote)]}, 24)[0]
    _subject, body = ACP._build_chase_email(row)
    assert "quote from 6 days ago" in body
    assert "carries no timestamp" not in body


def test_chase_copy_defaults_to_the_dated_wording_for_foreign_rows():
    """_build_chase_email is reachable from callers that did not run
    _find_overdue_pending, so the marker must default safely rather than
    KeyError or silently hedge."""
    _subject, body = ACP._build_chase_email({"lane": "Oakland → Algeciras",
                                             "_age_hours": 50.0})
    assert "carries no timestamp" not in body


# ─────────────────────────────────────────────────────────────────────
# Ordering — the guard against re-introducing fabricated timestamps
# ─────────────────────────────────────────────────────────────────────

def test_undated_quotes_are_healed_before_they_are_aged():
    """qc_selfheal._heal_undated_quote recovers a REAL response_timestamp from
    the cached OL body, and it must run BEFORE decide_status in the same loop.
    Reverse the order and a row whose true quote time is one parse away gets
    aged on the request instead — the aggressive anchor firing when the exact
    evidence to avoid it was already on disk.

    Pinned on source position because the two calls sit in one function and
    there is no return value to observe."""
    src = (ROOT / "scripts" / "qc_selfheal.py").read_text(encoding="utf-8")
    heal = src.index("_heal_undated_quote(log, rid_label, r, bodies_idx)")
    decide = src.index("decision = core.decide_status(")
    assert heal < decide, (
        "aging now runs before the undated-quote heal — a row whose real "
        "quote time is recoverable will be aged off the request instead")


def test_is_business_stale_still_returns_false_on_none():
    """The fix is a fallback at ONE call site, deliberately not a change to
    the shared predicate. is_business_stale must stay conservative for every
    other caller."""
    import hilmar.core as hc
    assert core.is_business_stale(None, NOW) is False
    assert hc.is_business_stale(None, NOW) is False
    assert core.send_signal_stale is core.is_business_stale


def test_scripts_is_the_tree_production_runs():
    """Re-verified every session. A diagnosis made against src/hilmar and
    reported as production cost hours on 2026-08-13, and the two trees still
    differ (scripts returns "LOSS" where src returns its Q&L constant)."""
    spec = importlib.util.spec_from_file_location("_probe", ROOT / "scripts" / "core.py")
    assert spec is not None
    wf = (ROOT / ".github" / "workflows" / "daily.yml")
    if wf.exists():
        text = wf.read_text(encoding="utf-8")
        assert "scripts/refresh_stage.py" in text
        assert "src/hilmar/refresh_stage.py" not in text
    assert not (ROOT / "src" / "hilmar" / "refresh_stage.py").exists(), (
        "intake now exists in the mirror too — the intake fix must be mirrored")


# ─────────────────────────────────────────────────────────────────────
# The two defects the 2026-08-13 live fire exposed, once quotes could
# actually reach the tracker and actually age. Both are cases of a
# check or a sentence that was correct only while the bug was present.
# ─────────────────────────────────────────────────────────────────────

def test_quoted_pending_is_not_a_history_contradiction():
    """QC-072 must not call the intended quoted-and-pending shape an error.

    Live fire 31728462371 raised two red errors for the two OL forwards it
    had just admitted — both real quotes, correctly recorded, awaiting
    Lonny. "QUOTED" is not a status (VALID_STATUSES is {WIN, LOSS,
    PENDING}); it is the sub-state ingest records when OL answers.
    """
    import qc_selfheal as QS
    row = {
        "request_id": "req_34213cc401395756",
        "status": "PENDING",
        "status_history": [
            {"at": "2026-08-12T13:05:00Z", "from": None, "to": "PENDING"},
            {"at": "2026-08-12T20:46:10Z", "from": "PENDING", "to": "QUOTED"},
        ],
    }
    assert QS.qc072_history_contradicts_status([row]) == []


def test_qc072_still_catches_the_shape_it_was_built_for():
    """The narrow exemption must not blunt the check. History saying WIN on a
    LOSS row is the 2026-07-26 defect QC-072 exists for."""
    import qc_selfheal as QS
    row = {
        "request_id": "req_regression",
        "status": "LOSS",
        "status_history": [{"at": "2026-07-01T00:00:00Z", "to": "WIN"}],
    }
    found = QS.qc072_history_contradicts_status([row])
    assert [k for _, k, _ in found] == ["history-contradiction"]


def test_undated_banner_states_where_the_rows_actually_are():
    """The banner used to assert "They appear under PENDING HILMAR" — true
    only while an undated quote could never age. Once they age, saying it
    anyway points the reader at the wrong section."""
    import gen_email as GE
    undated = ([{"status": "LOSS", "quoted": True}] * 20) + [{"status": "PENDING"}]
    html = GE._undated_quotes_note(undated)
    assert "20 under Quoted &amp; Lost" in html
    assert "1 under PENDING HILMAR" in html
    # The old flat claim must be gone.
    assert "They appear under PENDING HILMAR." not in html


def test_undated_banner_still_says_pending_when_that_is_true():
    import gen_email as GE
    html = GE._undated_quotes_note([{"status": "PENDING"}, {"status": "PENDING"}])
    assert "2 under PENDING HILMAR" in html


def test_undated_banner_is_empty_when_there_is_nothing_to_say():
    import gen_email as GE
    assert GE._undated_quotes_note([]) == ""
