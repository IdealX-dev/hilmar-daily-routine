"""QC-078: nothing may be derived from a borrowed response time.

2026-08-19. qc_selfheal's sibling heal can COPY a response_timestamp from one
row's quote onto another undated ask on the same lane at the same rate,
marking it response_time_source="sibling_quote". That date is real evidence
about WHICH quote covered the lane — the win/loss ledger and QC-077 keep
reading it on purpose — but it is not proof OL sent anything at that minute.

WHY A QC CHECK AND NOT ONLY UNIT TESTS. The three derived fields
(turnaround_biz_hours, turnaround_hours, olusa_time_et) have 20+ readers
across scripts/, src/hilmar/ and two Jinja templates, so the guard lives at
ONE writer. Unit tests pin today's writers. Only a check over the real dataset
catches tomorrow's: a new heal, a backfill script, or a snapshot restored from
before the fix. QC-048 cannot substitute — it clears only >40 biz-hours, and
the measured fabrication was 6.95.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import qc_selfheal as QS  # noqa: E402


def _row(**over):
    r = {"request_id": "req_1", "status": "LOSS", "quoted": True,
         "lane": "Oakland → Algeciras", "ol_rate": 4938.0,
         "carrier_quoted": "CMA CGM",
         "request_timestamp": "2026-08-12T14:00:00Z",
         "response_timestamp": "2026-08-12T20:57:02Z",
         "status_history": []}
    r.update(over)
    return r


def _run(rows):
    log = QS.Log()
    QS.phase_6_rules(log, {"requests": rows})
    return log


def _msgs(log):
    return list(log.errors) + list(log.warnings) + list(log.oks)


def test_a_leaked_turnaround_is_an_error():
    """THE CHECK. A borrowed row carrying a turnaround means a writer ran
    after the phase-3 scrub, or bypassed it."""
    bad = _row(response_time_source=core.BORROWED_RESPONSE_TIME,
               turnaround_biz_hours=6.95)
    log = _run([bad])
    hits = [m for m in log.errors if "QC-078" in m]
    assert hits, f"QC-078 did not fire. messages={_msgs(log)[:4]}"
    assert "turnaround_biz_hours" in hits[0], "the field is not named"
    assert "req_1" in hits[0], "the row is not named"


def test_a_leaked_clock_string_is_an_error():
    """olusa_time_et is the nastiest of the three: a STORED pre-rendered
    string, so a reader that correctly guards response_timestamp still
    prints it."""
    bad = _row(response_time_source=core.BORROWED_RESPONSE_TIME,
               olusa_time_et="04:57 PM ET")
    log = _run([bad])
    assert any("QC-078" in m and "olusa_time_et" in m for m in log.errors)


def test_a_clean_borrowed_row_is_reported_not_errored(capsys):
    """The borrowed DATE itself is legitimate — it says which quote covered
    the lane. The check must not flag its mere existence, or it cries wolf
    on correct behaviour every fire.

    Asserted on captured stdout because Log.ok prints without recording;
    only fixes, warnings and errors are kept in lists."""
    ok = _row(response_time_source=core.BORROWED_RESPONSE_TIME)
    log = _run([ok])
    assert not [m for m in log.errors if "QC-078" in m]
    out = capsys.readouterr().out
    assert "QC-078" in out and "borrowed response date" in out, (
        "the check said nothing at all about a row holding a borrowed date — "
        "silence is how the count reached 41 unnoticed on QC-077")


def test_an_evidenced_row_with_a_turnaround_is_untouched():
    """Against over-application: a real quote's real turnaround is the whole
    point of the metric and must never be flagged."""
    good = _row(turnaround_biz_hours=3.0, turnaround_hours=3.0,
                olusa_time_et="04:57 PM ET")
    assert core.response_time_is_evidenced(good) is True
    log = _run([good])
    assert not [m for m in log.errors if "QC-078" in m]


def test_qc078_is_what_catches_what_qc048_cannot():
    """The gap that let 6.95 reach the KPI: QC-048 clears only >40 biz-hours,
    so a plausible-looking fabricated sample is invisible to it. These two
    checks must stay separate — do not merge them later."""
    under = _row(response_time_source=core.BORROWED_RESPONSE_TIME,
                 turnaround_biz_hours=6.95)
    assert under["turnaround_biz_hours"] <= 40, (
        "the fixture must sit under QC-048's threshold or it proves nothing")
    log = _run([under])
    assert any("QC-078" in m for m in log.errors)


# ── drift_check phase 2 ───────────────────────────────────────────────────
#
# Phase 2 asks "is there a closer same-destination NQ record than the one this
# OL reply is attached to?" and computes the answer from
# |response_timestamp - request_timestamp|. On a BORROWED row that interval
# measures nothing: no reply was ever attached to it, the date came off a
# different row's quote.
#
# This is not a cosmetic warning. THREE drift candidates halt the whole fire
# (drift_check.MATCHER_DRIFT_FAIL_FLOOR), so borrowed rows on a busy
# standing-rate lane could black out the daily send on evidence that does not
# exist — the failure mode HILMAR-DAILY-TRACKER-6 already cost days.

import drift_check as DC  # noqa: E402


def _drift(rows):
    log = {}
    DC.phase2_matcher_quality({"requests": rows}, log, auto_heal=False)
    return log["phase2"]


def _pair(borrowed: bool):
    """A quoted row whose apparent turnaround is huge, plus a much closer NQ
    row on the same destination — the exact shape phase 2 flags."""
    quoted = {"request_id": "req_quoted", "status": "LOSS", "quoted": True,
              "destination": "Singapore",
              "request_timestamp": "2026-07-01T14:00:00Z",
              "response_timestamp": "2026-08-18T17:44:45Z"}
    if borrowed:
        quoted["response_time_source"] = core.BORROWED_RESPONSE_TIME
    near = {"request_id": "req_near", "status": "LOSS", "quoted": False,
            "destination": "Singapore",
            "request_timestamp": "2026-08-18T17:00:00Z"}
    return [quoted, near]


def test_an_evidenced_row_can_still_be_flagged_as_drifted():
    """The detector must keep working — this is the shape it exists for."""
    out = _drift(_pair(borrowed=False))
    assert out["matcher_drift_count"] >= 1, (
        f"phase 2 stopped detecting real matcher drift: {out}")


def test_a_borrowed_row_is_not_matcher_drift():
    """Same shape, borrowed date. Its interval is not evidence about how the
    matcher attached anything, and three of these halt the fire."""
    out = _drift(_pair(borrowed=True))
    assert out["matcher_drift_count"] == 0, (
        f"a borrowed row was counted as matcher drift: {out}. Three of these "
        "block the daily send on an interval that measures nothing.")
