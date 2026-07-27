"""Data audit batch 6 — finding #19: one booking, two different weeks.

THE DEFECT. Michael directed on 2026-07-21 ("a win belongs to the day Lonny
booked it") that the daily email count wins by EVENT date, and
`gen_email._today_summary` was changed to do exactly that: it counts →WIN
status_history transitions dated the report day. `gen_weekly_summary` was
never changed. It filters every row — wins included — by `request_date`.

So an RFQ received Friday 2026-07-24 and booking-confirmed Monday 2026-07-27:

  daily email, Mon Jul 27   →  1 win   (event date, week of Jul 27)
  weekly summary            →  that win sits in the week of Jul 20
                               (request date, the PREVIOUS week)

The same booking, credited to two different weeks, in two reports Michael
reads side by side. Worse than a wrong number: two right-looking numbers that
cannot both be true, which is the exact "CHECK YOUR REPORT" shape.

THE FIX. `core.win_event_date(r)` — one definition, called by both reports.
The weekly filters wins through `_filter_wins` (event-dated) while intake,
Q&L, NQ and pending stay `request_date`-bucketed, matching the daily's
documented split exactly.

These tests drive BOTH generators against ONE row set and assert they agree.
A future change to either clock fails here rather than in Michael's inbox.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import core  # noqa: E402
import gen_email as GE  # noqa: E402
import gen_weekly_summary as GWS  # noqa: E402

UTC = timezone.utc


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# The scenario, fixed: RFQ in Friday of week A, booked Monday of week B.
FRI_WEEK_A = "2026-07-24"
MON_WEEK_B = "2026-07-27"
WEEK_A = (date(2026, 7, 20), date(2026, 7, 24))
WEEK_B = (date(2026, 7, 27), date(2026, 7, 31))


def _friday_rfq_booked_monday():
    """One row: asked Friday, booked Monday 08:15 ET (12:15 UTC)."""
    return {
        "request_id": "rfq_fri",
        "request_date": FRI_WEEK_A,
        "request_timestamp": "2026-07-24T16:00:00Z",
        "status": "WIN",
        "quoted": True,
        "lane": "Oakland → Manila (North)",
        "carrier_quoted": "ONE",
        "carrier_won": "ONE",
        "teu_requested": 4,
        "teu_won": 4,
        "status_history": [
            {"from": "PENDING", "to": "WIN", "at": "2026-07-27T12:15:00Z",
             "reason": "MDOLX booking confirmed"},
        ],
    }


# ── core.win_event_date — the single shared definition ───────────────────────

def test_win_event_date_is_the_booking_day_not_the_request_day():
    assert core.win_event_date(_friday_rfq_booked_monday()) == MON_WEEK_B


def test_win_event_date_converts_to_ET_not_UTC():
    """A booking confirmed 20:30 ET Friday is 00:30 UTC Saturday. Bucketing
    on the UTC date would push it into a weekend no report ever covers —
    the same class of bug et_date_of exists to prevent."""
    r = _friday_rfq_booked_monday()
    r["status_history"] = [{"to": "WIN", "at": "2026-07-25T00:30:00Z"}]
    assert core.win_event_date(r) == "2026-07-24"


def test_win_event_date_falls_back_to_request_date_for_legacy_rows():
    """Rows recorded before transitions were kept have no →WIN entry. They
    must still land in exactly one bucket, not vanish from every report."""
    r = _friday_rfq_booked_monday()
    r["status_history"] = []
    assert core.win_event_date(r) == FRI_WEEK_A


def test_win_event_date_is_none_for_a_row_that_is_not_currently_a_win():
    """A win that was REVERSED is not a win. It carries a →WIN transition
    forever, so keying off the history alone would keep crediting it."""
    r = _friday_rfq_booked_monday()
    r["status"] = "LOSS"
    r["loss_reason"] = "PRICE"
    r["status_history"].append(
        {"from": "WIN", "to": "LOSS", "at": "2026-07-28T14:00:00Z",
         "reason": "booking cancelled"})
    assert core.win_event_date(r) is None


def test_win_event_date_takes_the_LAST_win_transition():
    """Reversed, then re-won. Two →WIN entries. Testing 'any transition on
    this day' credited the row to BOTH days; the booking that stands is the
    latest one."""
    r = _friday_rfq_booked_monday()
    r["status_history"] += [
        {"from": "WIN", "to": "LOSS", "at": "2026-07-28T14:00:00Z"},
        {"from": "LOSS", "to": "WIN", "at": "2026-07-30T15:00:00Z"},
    ]
    assert core.win_event_date(r) == "2026-07-30"


@pytest.mark.parametrize("row", [
    {},
    {"status": "PENDING"},
    {"status": "LOSS", "loss_reason": "NO_RESPONSE"},
    {"status": "WIN"},                      # no history, no request_date
    {"status": "WIN", "status_history": None},
])
def test_win_event_date_never_raises_on_a_partial_row(row):
    core.win_event_date(row)                # must not raise


# ── the two reports must agree ───────────────────────────────────────────────

def test_the_weekly_credits_the_win_to_the_week_the_daily_does():
    """THE FINDING. One row, both generators, one answer."""
    rows = [_friday_rfq_booked_monday()]

    # Daily: which report day counts this win?
    won_on_monday = GE._today_summary(rows, report_date=date(2026, 7, 27))
    won_on_friday = GE._today_summary(rows, report_date=date(2026, 7, 24))
    assert won_on_monday["wins"] == 1, "the daily lost the win it was booked on"
    assert won_on_friday["wins"] == 0, "the daily credited it to the request day"

    # Weekly: which week counts it?
    in_week_a = GWS.analyze_week(GWS._filter_rows(rows, *WEEK_A),
                                 GWS._filter_wins(rows, *WEEK_A))
    in_week_b = GWS.analyze_week(GWS._filter_rows(rows, *WEEK_B),
                                 GWS._filter_wins(rows, *WEEK_B))
    assert in_week_b["wins"] == 1, "the weekly did not credit the booking week"
    assert in_week_a["wins"] == 0, (
        "the weekly still credits the win to the week the RFQ arrived — "
        "the same booking counted in two different weeks")


def test_the_win_is_counted_in_exactly_one_week_never_two():
    """The failure mode is double-credit, not just mis-credit: the weekly
    total across the two weeks must be 1."""
    rows = [_friday_rfq_booked_monday()]
    a = GWS.analyze_week(GWS._filter_rows(rows, *WEEK_A),
                         GWS._filter_wins(rows, *WEEK_A))["wins"]
    b = GWS.analyze_week(GWS._filter_rows(rows, *WEEK_B),
                         GWS._filter_wins(rows, *WEEK_B))["wins"]
    assert a + b == 1, f"win counted {a + b} times across two weeks"


def test_teu_won_moves_with_the_win():
    """A win counted in week B whose TEU stayed in week A would make the
    weekly contradict ITSELF, not just the daily."""
    rows = [_friday_rfq_booked_monday()]
    a = GWS.analyze_week(GWS._filter_rows(rows, *WEEK_A),
                         GWS._filter_wins(rows, *WEEK_A))
    b = GWS.analyze_week(GWS._filter_rows(rows, *WEEK_B),
                         GWS._filter_wins(rows, *WEEK_B))
    assert (a["teu_won"], b["teu_won"]) == (0, 4)


def test_the_intake_still_belongs_to_the_week_the_rfq_arrived():
    """Only the WIN moves. `total` counts what came in, so the Friday RFQ
    stays in week A's intake — otherwise the week's request count silently
    drops rows that got booked late."""
    rows = [_friday_rfq_booked_monday()]
    a = GWS.analyze_week(GWS._filter_rows(rows, *WEEK_A),
                         GWS._filter_wins(rows, *WEEK_A))
    b = GWS.analyze_week(GWS._filter_rows(rows, *WEEK_B),
                         GWS._filter_wins(rows, *WEEK_B))
    assert a["total"] == 1, "the week that received the RFQ lost it from intake"
    assert b["total"] == 0, "the booking week invented an RFQ it never received"


def test_win_rate_cannot_exceed_100_percent_with_a_carried_win():
    """The mixed clock is deliberate (documented in analyze_week), but the
    numerator is always inside the denominator, so the headline rate stays
    bounded — a >100% win rate on a Monday-heavy week would be the first
    thing Michael saw."""
    rows = [_friday_rfq_booked_monday()]
    for bounds in (WEEK_A, WEEK_B):
        m = GWS.analyze_week(GWS._filter_rows(rows, *bounds),
                             GWS._filter_wins(rows, *bounds))
        assert 0 <= m["win_rate"] <= 100, m


# ── the downstream weekly sections move with it ──────────────────────────────

def test_carrier_of_the_week_is_crowned_in_the_booking_week():
    """The trophy has to follow the win. Crowning ONE in week A would name a
    carrier for a week the daily says it won nothing in."""
    rows = [_friday_rfq_booked_monday()]
    cow_b = GWS.carrier_of_week(GWS._filter_rows(rows, *WEEK_B),
                                GWS._filter_wins(rows, *WEEK_B))
    assert cow_b and cow_b["carrier"] == "ONE" and cow_b["wins"] == 1

    cow_a = GWS.carrier_of_week(GWS._filter_rows(rows, *WEEK_A),
                                GWS._filter_wins(rows, *WEEK_A))
    assert (cow_a or {}).get("wins", 0) == 0, (
        "the carrier was crowned for the week the RFQ arrived")


def test_top_winning_lane_is_reported_in_the_booking_week():
    rows = [_friday_rfq_booked_monday()]
    assert GWS.top_lanes_by_teu_won(GWS._filter_wins(rows, *WEEK_B))[0]["teu_won"] == 4
    assert GWS.top_lanes_by_teu_won(GWS._filter_wins(rows, *WEEK_A)) == []


def test_a_quote_lost_stays_in_the_week_it_was_quoted():
    """Losses were never event-dated and must not become so — only the win
    clock changed."""
    ql = {"request_id": "ql1", "request_date": FRI_WEEK_A, "status": "LOSS",
          "quoted": True, "carrier_quoted": "MSC", "teu_requested": 2,
          "lane": "Oakland → Busan"}
    a = GWS.analyze_week(GWS._filter_rows([ql], *WEEK_A),
                         GWS._filter_wins([ql], *WEEK_A))
    assert (a["ql"], a["teu_ql"]) == (1, 2)


def test_carrier_of_week_single_arg_form_is_unchanged():
    """Callers that pass ONE pre-filtered list (every existing test, and any
    ad-hoc analysis) must keep the old meaning: the rows are their own win
    set."""
    rows = [_friday_rfq_booked_monday()]
    assert GWS.carrier_of_week(rows)["wins"] == 1
    assert GWS.analyze_week(rows)["wins"] == 1


# ── the four-week trend uses the same clock ──────────────────────────────────

def test_four_week_trend_event_dates_its_wins_too():
    """The trend is what makes a mis-bucketed win visible as a false dip in
    one week and a false spike in the next."""
    rows = [_friday_rfq_booked_monday()]
    trend = GWS.four_week_trend(rows, date(2026, 7, 27))
    by_week = {t["week_start"]: t["wins"] for t in trend}
    assert by_week.get("2026-07-27") == 1
    assert by_week.get("2026-07-20") == 0


# ── cross-tree parity: both cores must define it the same way ────────────────

@pytest.mark.parametrize("mutate", [
    lambda r: r,
    lambda r: (r.update(status_history=[]), r)[1],
    lambda r: (r.update(status="LOSS"), r)[1],
    lambda r: (r.update(status_history=[{"to": "WIN", "at": "2026-07-25T00:30:00Z"}]), r)[1],
])
def test_win_event_date_parity_across_trees(mutate):
    """scripts/ runs the fire; src/hilmar/ is what coverage targets. A drift
    here puts a booking in different weeks again — by a different route."""
    sc = _load(SCRIPTS / "core.py", "scripts_core_b6")
    hc = _load(ROOT / "src" / "hilmar" / "core.py", "hilmar_core_b6")
    row = mutate(_friday_rfq_booked_monday())
    assert sc.win_event_date(dict(row)) == hc.win_event_date(dict(row))


# ── the same clock in teams_alert (found while fixing #19) ───────────────────

def _alert_rows(win_at):
    return [{"request_id": "w1", "status": "WIN", "lane": "Oakland → Busan",
             "carrier_won": "ONE", "teu_won": 40, "teu_requested": 40,
             "ol_rate": 2400, "request_date": "2026-07-24",
             "status_history": [{"from": "PENDING", "to": "WIN", "at": win_at}]}]


def _detect(monkeypatch, tmp_path, rows, now_et):
    """Drive teams_alert.detect_events with a pinned ET clock and an isolated
    alert-dedupe queue."""
    import teams_alert as TA
    monkeypatch.setattr(TA, "QUEUE", tmp_path / "q.json", raising=False)
    monkeypatch.setattr(TA, "_was_alerted", lambda k: False)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_et.astimezone(tz) if tz else now_et
    monkeypatch.setattr(TA, "datetime", _FrozenDT)
    return TA.detect_events({"requests": rows},
                            {"events": ["win", "big_day"],
                             "min_teu_for_big_day": 30})


def test_a_friday_evening_win_still_alerts(monkeypatch, tmp_path):
    """A booking confirmed 20:30 ET Friday is 00:30 UTC Saturday. The old
    `at[:10]` compared that UTC date to an ET today_iso, so they never
    matched and the WIN alert NEVER fired for any booking after 8 PM ET."""
    now = datetime(2026, 7, 24, 22, 0, tzinfo=UTC).astimezone(core.ET)
    events = _detect(monkeypatch, tmp_path,
                     _alert_rows("2026-07-25T00:30:00Z"), now)
    assert any(e["type"] == "win" for e in events), \
        "the evening win produced no alert — the UTC/ET clock split is back"


def test_big_day_counts_a_win_touched_by_a_later_transition(monkeypatch, tmp_path):
    """The big-day sum read `status_history[-1]` — the most recent transition
    of ANY kind — so a row won this morning and re-touched later stopped
    counting toward the day's TEU."""
    rows = _alert_rows("2026-07-24T14:00:00Z")
    rows[0]["status_history"].append(
        {"from": "WIN", "to": "WIN", "at": "2026-07-24T18:00:00Z",
         "reason": "QC-056: carrier backfilled"})
    now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC).astimezone(core.ET)
    events = _detect(monkeypatch, tmp_path, rows, now)
    assert any(e["type"] == "big_day" for e in events), \
        "40 TEU won today did not trigger the big-day alert"


def test_a_reversed_win_does_not_alert(monkeypatch, tmp_path):
    """win_event_date returns None once the row is no longer a WIN, so a
    cancelled booking cannot re-fire the celebration."""
    rows = _alert_rows("2026-07-24T14:00:00Z")
    rows[0]["status"] = "LOSS"
    rows[0]["status_history"].append(
        {"from": "WIN", "to": "LOSS", "at": "2026-07-24T16:00:00Z"})
    now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC).astimezone(core.ET)
    events = _detect(monkeypatch, tmp_path, rows, now)
    assert not any(e["type"] in ("win", "big_day") for e in events)


# ── [13] a truncated preview is not evidence a request has no containers ─────
#
# _merge_thread_dupes collapses a "header-only" Lonny email into the sibling
# that carries the container line. It decided which was which by teu==0 — but
# teu_requested comes from guess_teu_from_preview(), which reads ONLY
# summary_preview, and the stagers cut that at 300 chars. On a longer RFQ the
# equipment line falls past the cut, the row looks thin, and the merge DELETED
# it: a real rate request that never reached intake and left no trace but a
# merge_note on a different row.

import ingest as ING  # noqa: E402

_CID = "AAQkAD-thread-1"


def _lonny(rid, ts, teu, containers, dest="Manila (North)"):
    return {
        "request_id": rid, "conversation_id": _CID,
        "destination": dest, "lane": f"Oakland → {dest}",
        "request_date": "2026-07-24", "request_timestamp": ts,
        "lonny_time_pt": "9:00 AM", "status": "PENDING",
        "teu_requested": teu, "container_count": 1 if teu else 0,
        "containers": containers,
        "source_imids": [f"<{rid}@ol>"], "source_ids": [rid],
    }


def test_a_truncated_preview_row_is_not_deleted():
    """THE FINDING. Second RFQ, 10 minutes later, equipment line past the
    300-char preview cut. It must survive."""
    truncated = "Please quote Oakland to Manila for the below. " + "x" * 254
    assert len(truncated) == 300
    rows = [
        _lonny("a", "2026-07-24T16:00:00Z", 4, "2-40'HC"),
        _lonny("b", "2026-07-24T16:05:00Z", 0, truncated),
    ]
    out = ING._merge_thread_dupes(rows)
    assert len(out) == 2, "a genuinely distinct RFQ was deleted as a dupe"
    assert {r["request_id"] for r in out} == {"a", "b"}


def test_the_decline_is_recorded_not_silent():
    """Michael has to be able to see why two similar rows are both present."""
    rows = [
        _lonny("a", "2026-07-24T16:00:00Z", 4, "2-40'HC"),
        _lonny("b", "2026-07-24T16:05:00Z", 0, "y" * 300),
    ]
    notes = " ".join(ING._merge_thread_dupes(rows)[0].get("merge_notes") or [])
    assert "Declined to merge" in notes and "truncated" in notes


def test_a_genuine_header_only_email_still_merges():
    """The merge exists for a real case (Issue #5): Lonny sends 'I need two
    identical bookings', then the container line. That preview is SHORT and
    complete, so teu=0 is trustworthy and the collapse must still happen."""
    rows = [
        _lonny("hdr", "2026-07-24T16:00:00Z", 0, "I need two identical bookings"),
        _lonny("body", "2026-07-24T16:04:00Z", 4, "2-40'HC"),
    ]
    out = ING._merge_thread_dupes(rows)
    assert len(out) == 1, "the header-only merge this function exists for stopped working"
    assert out[0]["request_id"] == "body"
    assert "<hdr@ol>" in out[0]["source_imids"], "the thin sibling's evidence was lost"


def test_the_merge_still_keeps_the_earliest_ask_time():
    """Turnaround math is measured from Lonny's FIRST contact."""
    rows = [
        _lonny("hdr", "2026-07-24T16:00:00Z", 0, "need a quote"),
        _lonny("body", "2026-07-24T16:04:00Z", 4, "2-40'HC"),
    ]
    assert ING._merge_thread_dupes(rows)[0]["request_timestamp"] == "2026-07-24T16:00:00Z"


@pytest.mark.parametrize("n,truncated", [
    (199, False), (200, True), (201, False),
    (299, False), (300, True), (301, False),
    (0, False),
])
def test_truncation_is_detected_at_the_stager_caps(n, truncated):
    """200 = the hilmar-tree stager's slice, 300 = refresh_stage.py:548."""
    assert ING._preview_was_truncated("z" * n) is truncated


def test_truncation_check_survives_none_and_whitespace():
    assert ING._preview_was_truncated(None) is False
    assert ING._preview_was_truncated("   ") is False
    assert ING._preview_was_truncated(" " + "z" * 300 + " ") is True


def test_two_real_rfqs_with_containers_are_never_merged():
    """Pre-existing behaviour, pinned: if BOTH parsed containers they are
    distinct asks and neither is thin."""
    rows = [
        _lonny("a", "2026-07-24T16:00:00Z", 4, "2-40'HC"),
        _lonny("b", "2026-07-24T16:05:00Z", 2, "1-40'HC"),
    ]
    assert len(ING._merge_thread_dupes(rows)) == 2


# ── review findings on PR #124 — three sites the first pass missed ───────────

import qc_selfheal as QC  # noqa: E402


def _response_no_rate():
    """OL acknowledged the RFQ but never sent a rate: quoted=False, so this is
    NOT-QUOTED — but loss_reason is RESPONSE_NO_RATE, not NO_RESPONSE."""
    return {"request_id": "rnr1", "status": "LOSS", "quoted": False,
            "loss_reason": "RESPONSE_NO_RATE", "request_date": "2026-07-22",
            "lane": "Oakland → Busan", "origin": "Oakland",
            "destination": "Busan", "teu_requested": 4,
            "carrier_quoted": "ONE"}


def test_the_8_week_rollup_buckets_NQ_like_every_other_surface():
    """The 8-week rollup is literally the 'NQ 0 / Q&L 1' line in the finding
    #17 evidence, and it was the one NQ site in gen_email the first pass
    missed — it still tested loss_reason. Raised in review of #124."""
    row = _response_no_rate()
    agg = core.aggregate_summary([row])
    weeks = GE._week_rows({"requests": [row]})
    assert (agg["not_quoted"], agg["quoted_lost"]) == (1, 0)
    assert [(b["nq"], b["ql"]) for _, b in weeks] == [(1, 0)], (
        "the 8-week rollup calls it Q&L while the summary calls it NQ — "
        "the same email contradicting itself")


def test_the_lane_tables_bucket_NQ_like_every_other_surface():
    """_build_lane_buckets feeds the Winning/Losing lane tables. The stale
    test reported a never-quoted row as a competitive loss on that lane AND
    charged its TEU to teu_lost."""
    buckets = GE._build_lane_buckets({"requests": [_response_no_rate()]})
    got = [(b["nq"], b["ql"], b["teu_lost"]) for b in buckets.values()]
    assert got == [(1, 0, 0)], "a never-quoted row was charged as a lane loss"


def test_a_send_reply_cannot_promote_the_wrong_terminal_to_WIN():
    """apply_send_signals pooled candidates by canonical_lane_key, which
    collapses Manila (North)/(South) to one key, then tie-broke on the latest
    request_timestamp. A 'Send' on the NORTH thread could flip the SOUTH row
    to WIN — a wrong WIN is a stronger claim than a wrong rate. Raised in
    review of #124."""
    north = {"request_id": "n", "destination": "Manila (North)",
             "lane": "Oakland → Manila (North)", "status": "PENDING",
             "request_timestamp": "2026-07-20T16:00:00Z", "quoted": True,
             "carrier_quoted": "ONE", "status_history": []}
    south = {"request_id": "s", "destination": "Manila (South)",
             "lane": "Oakland → Manila (South)", "status": "PENDING",
             "request_timestamp": "2026-07-22T16:00:00Z", "quoted": True,
             "carrier_quoted": "MSC", "status_history": []}
    reply = {"subject": "RE: Oakland to Manila (North)",
             "sent": "2026-07-23T18:00:00Z", "send_signal": True,
             "body_parsed": {"send_signal": True}}

    ING.apply_send_signals([north, south], [reply])
    assert north["status"] == "WIN", "the confirmed booking was left open"
    assert south["status"] != "WIN", (
        "the send reply promoted the WRONG terminal — south was picked "
        "because its request_timestamp was later")
    assert south.get("carrier_won") is None


def test_a_send_reply_with_no_compatible_terminal_wins_nothing():
    """Narrowing is unconditional: no compatible candidate means no match,
    not 'fall back to the incompatible list'."""
    south = {"request_id": "s", "destination": "Manila (South)",
             "lane": "Oakland → Manila (South)", "status": "PENDING",
             "request_timestamp": "2026-07-22T16:00:00Z", "quoted": True,
             "carrier_quoted": "MSC", "status_history": []}
    reply = {"subject": "RE: Oakland to Manila (North)",
             "sent": "2026-07-23T18:00:00Z", "send_signal": True,
             "body_parsed": {"send_signal": True}}
    ING.apply_send_signals([south], [reply])
    assert south["status"] != "WIN"


def test_a_send_reply_still_matches_a_terminal_less_row():
    """The widening the fallback exists for must keep working: a bare 'Manila'
    request still matches a 'Manila (North)' send."""
    bare = {"request_id": "b", "destination": "Manila",
            "lane": "Oakland → Manila", "status": "PENDING",
            "request_timestamp": "2026-07-22T16:00:00Z", "quoted": True,
            "carrier_quoted": "ONE", "status_history": []}
    reply = {"subject": "RE: Oakland to Manila (North)",
             "sent": "2026-07-23T18:00:00Z", "send_signal": True,
             "body_parsed": {"send_signal": True}}
    ING.apply_send_signals([bare], [reply])
    assert bare["status"] == "WIN"


def test_qc075_reaches_the_files_humans_actually_read(tmp_path, monkeypatch):
    """THE REVIEW FINDING. QC-075 fired AFTER phase_7_save had already
    serialized log.errors into tracking-data-v2.json's qc block and
    reports/qc-result.json — the two files gen_dashboard's QC tab and
    gen_improvements_report's red-flags section read. Escalating afterwards
    appended to a list nothing re-wrote, so the divergence appeared only on
    stdout: the same `print()` QC-075 was created to replace.

    Drives the REAL phase_7_save and reads the REAL artifacts back off disk.
    """
    import json as _json
    data_path = tmp_path / "tracking-data-v2.json"
    result_path = tmp_path / "qc-result.json"

    log = QC.Log()
    data = {"requests": [], "summary": {}, "lane_summary": {},
            "carrier_summary": {}}
    # Force a divergence the way main() now does — before the save.
    monkeypatch.setattr(QC, "_trade_region_reconciliation",
                        lambda d: {"reconciled": False, "error": "regions 5 vs summary 7"})
    tr = QC._trade_region_reconciliation(data)
    if tr and tr.get("reconciled") is False:
        log.error("QC-075: trade-region rollup does not reconcile to summary — "
                  f"{tr.get('error') or tr}")
    QC.phase_7_save(log, data, data_path, result_path)

    persisted = _json.loads(data_path.read_text(encoding="utf-8"))
    blob = _json.dumps(persisted.get("qc", {}))
    assert "QC-075" in blob, (
        "tracking-data-v2.json's qc block has no QC-075 entry — the "
        "dashboard's QC tab renders as if the rollup reconciled")

    written = _json.loads(result_path.read_text(encoding="utf-8"))
    assert "QC-075" in _json.dumps(written), (
        "reports/qc-result.json has no QC-075 entry — the audit's red-flags "
        "section renders as if the rollup reconciled")


def test_qc075_fires_before_the_save_in_main_not_after():
    """Ordering is the whole fix, so pin it structurally: the escalation must
    appear ahead of the phase_7_save call in main()."""
    import ast
    src = (SCRIPTS / "qc_selfheal.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    seg = ast.get_source_segment(src, fn) or ""
    qc075_at = seg.find('log.error("QC-075')
    save_at = seg.find("phase_7_save(log, data")
    assert qc075_at != -1 and save_at != -1
    assert qc075_at < save_at, (
        "QC-075 escalates after phase_7_save again — it will not reach the "
        "persisted artifacts")
