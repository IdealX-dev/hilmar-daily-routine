"""QC-083 — one move asked twice, counted as two shipments.

Lonny asks for a move, gets no answer or changes his mind, and asks AGAIN a
day or two later in the same thread. One shipment, two rows. OL books one.

Until #231 the send-signal matcher — which matched on lane and recency and
SKIPPED rows already WIN — then promoted the OTHER copy too, and that copy
aged out `LOSS/SEND_NO_BOOKING`. An invented loss on a shipment that shipped,
with a reason accusing OL of never confirming a booking OL had confirmed.
Oakland → Tokyo, 2026-08-25/26, MDOLX261145.

#231 stopped new ones. The pairs written by earlier fires are still in
`tracking-data-v2.json`, and phase 4's existing passes cannot see them: pass 2
keys on `request_date` (a re-ask is by definition a different day) and fires
only on an unconfirmed WIN (post-#231 the stale copy is a LOSS).

THIS CHECK ONLY REPORTS. Absorbing the row means deleting a LOSS, and a
detector wrong in that direction manufactures a win rate — the failure this
repo has already shipped once. So the tests below spend most of their weight
on what it must NOT fire on.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal  # noqa: E402


def _row(rid, *, date, mdolx=None, has_send=False, etd="2026-09-04",
         cid="AAkACONV1", cont="1x40hc", dest="Tokyo", status="LOSS"):
    return {
        "request_id": rid, "conversation_id": cid,
        "origin": "Oakland", "destination": dest, "lane": f"Oakland → {dest}",
        "containers": cont, "etd_requested": etd,
        "request_date": date, "mdolx_ref": mdolx, "has_send": has_send,
        "status": status, "quoted": True, "teu_requested": 2,
        "status_history": [],
    }


def _run(rows):
    """phase_4_duplicates over `rows`; returns the QC-083 warnings."""
    log = qc_selfheal.Log()
    qc_selfheal.phase_4_duplicates(log, {"requests": list(rows)})
    return [w for w in log.warnings if "QC-083" in w]


# ── it fires on the live shape ────────────────────────────────────────────

def test_the_tokyo_pair_is_detected():
    booked = _row("req_0826", date="2026-08-26", mdolx="261145",
                  has_send=True, status="WIN")
    stale = _row("req_0825", date="2026-08-25")
    warns = _run([booked, stale])
    assert len(warns) == 1, f"expected one detection, got {warns}"
    assert "req_0825" in warns[0] and "req_0826" in warns[0]
    assert "261145" in warns[0]


def test_it_reports_and_does_not_absorb():
    # The whole point. A dropped row here is a deleted LOSS.
    rows = [_row("req_0826", date="2026-08-26", mdolx="261145",
                 has_send=True, status="WIN"),
            _row("req_0825", date="2026-08-25")]
    data = {"requests": list(rows)}
    qc_selfheal.phase_4_duplicates(qc_selfheal.Log(), data)
    assert len(data["requests"]) == 2, (
        "QC-083 removed a row — it is detect-only; absorbing a loss on a "
        "heuristic is how a win rate gets manufactured")
    assert {r["request_id"] for r in data["requests"]} == {"req_0825", "req_0826"}


# ── what it must NEVER fire on ────────────────────────────────────────────

def test_two_genuine_moves_on_different_sailings_are_left_alone():
    # THE DISCRIMINATOR. Same thread, same lane, same containers — and two
    # real shipments, one of which genuinely lost. Only etd_requested tells
    # them apart. Firing here erases a real loss.
    booked = _row("req_a", date="2026-08-26", mdolx="261145", has_send=True,
                  status="WIN", etd="2026-09-04")
    lost = _row("req_b", date="2026-08-25", etd="2026-09-18")
    assert _run([booked, lost]) == []


def test_a_row_that_lonny_actually_accepted_is_left_alone():
    # has_send on the stale row means the Send landed on ITS thread — post-#231
    # that is an ask accepted in its own right, so its loss is real.
    booked = _row("req_a", date="2026-08-26", mdolx="261145", has_send=True,
                  status="WIN")
    lost = _row("req_b", date="2026-08-25", has_send=True)
    assert _run([booked, lost]) == []


def test_two_real_bookings_on_one_sailing_are_left_alone():
    # Michael, 2026-08-24: "were there two bookings? did lonny ask for two
    # same bookings on same vessel then it should all tie out to requests".
    # Two distinct MDOLX refs is two shipments he really did move.
    a = _row("req_a", date="2026-08-26", mdolx="261145", has_send=True, status="WIN")
    b = _row("req_b", date="2026-08-25", mdolx="261150", has_send=True, status="WIN")
    c = _row("req_c", date="2026-08-24")
    assert _run([a, b, c]) == []


def test_a_row_with_no_requested_sailing_is_skipped_entirely():
    # Absence is not evidence, and this is the branch where guessing costs a
    # loss. A missing etd_requested must disqualify the row, not default it.
    booked = _row("req_a", date="2026-08-26", mdolx="261145", has_send=True,
                  status="WIN")
    stale = _row("req_b", date="2026-08-25", etd=None)
    assert _run([booked, stale]) == []
    stale["etd_requested"] = ""
    assert _run([booked, stale]) == []


def test_different_threads_are_not_evidence_of_each_other():
    booked = _row("req_a", date="2026-08-26", mdolx="261145", has_send=True,
                  status="WIN", cid="AAkACONV1")
    stale = _row("req_b", date="2026-08-25", cid="AAkACONV2")
    assert _run([booked, stale]) == []


def test_an_unresolved_lane_is_not_evidence_of_another_unresolved_lane():
    # canonical_port_key returns "unknown" for a lane it cannot resolve. Two
    # rows that each FAILED to resolve would otherwise match each other as
    # ("unknown", ...) — the same sentinel trap guarded in
    # ingest._prior_win_captured.
    booked = _row("req_a", date="2026-08-26", mdolx="261145", has_send=True,
                  status="WIN", dest="")
    stale = _row("req_b", date="2026-08-25", dest="")
    assert _run([booked, stale]) == []


def test_a_same_day_pair_is_left_to_pass_2():
    # Same request_date is a same-day duplicate, which pass 2 already owns.
    # Two checks racing to collapse one row is how a heal double-counts.
    booked = _row("req_a", date="2026-08-26", mdolx="261145", has_send=True,
                  status="WIN")
    stale = _row("req_b", date="2026-08-26")
    assert _run([booked, stale]) == []


def test_different_container_lines_are_different_shipments():
    booked = _row("req_a", date="2026-08-26", mdolx="261145", has_send=True,
                  status="WIN", cont="1x40hc")
    other = _row("req_b", date="2026-08-25", cont="2x40hc")
    assert _run([booked, other]) == []


def test_a_lone_unbooked_row_is_not_a_duplicate_of_nothing():
    assert _run([_row("req_solo", date="2026-08-25")]) == []


@pytest.mark.parametrize("n_stale", [1, 2, 3])
def test_every_stale_copy_is_named_not_just_the_first(n_stale):
    booked = _row("req_win", date="2026-08-26", mdolx="261145", has_send=True,
                  status="WIN")
    stale = [_row(f"req_s{i}", date=f"2026-08-2{i}") for i in range(n_stale)]
    warns = _run([booked, *stale])
    assert len(warns) == n_stale
    for r in stale:
        assert any(r["request_id"] in w for w in warns)
