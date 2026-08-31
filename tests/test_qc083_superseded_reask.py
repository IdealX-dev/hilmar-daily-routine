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

IT NOW ABSORBS (2026-08-31). It shipped detect-only on 2026-08-28 because
absorbing the row means DELETING A LOSS, and a detector wrong in that
direction manufactures a win rate — the failure this repo has already shipped
once. So it named rows first, and waited for a fire to answer. The 2026-08-31
fire named exactly two, both HCMC. That is what earned the deletion, and it is
also why the tests below still spend most of their weight on what it must NOT
fire on.
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
    """phase_4_duplicates over `rows`; every QC-083 line it emitted.

    Fixes AND warnings, deliberately. The negative tests below assert this is
    empty, and they have to stay load-bearing across the detect-only ->
    absorb change: a check that reports through `fix` now would slip past a
    filter that only reads `warnings`.
    """
    log = qc_selfheal.Log()
    qc_selfheal.phase_4_duplicates(log, {"requests": list(rows)})
    return [m for m in (*log.fixes, *log.warnings) if "QC-083" in m]


# ── it fires on the live shape ────────────────────────────────────────────

def test_the_tokyo_pair_fires_and_the_log_names_both_rows():
    """The audit line is the only trace a deleted row leaves. It has to carry
    both request_ids and the booking it was folded into, or nobody can undo
    this by hand later."""
    booked = _row("req_0826", date="2026-08-26", mdolx="261145",
                  has_send=True, status="WIN")
    stale = _row("req_0825", date="2026-08-25")
    lines = _run([booked, stale])
    assert len(lines) == 1, f"expected one QC-083 line, got {lines}"
    assert "req_0825" in lines[0] and "req_0826" in lines[0]
    assert "261145" in lines[0]


def _absorb(rows):
    data = {"requests": list(rows)}
    log = qc_selfheal.Log()
    qc_selfheal.phase_4_duplicates(log, data)
    return data, log


def test_it_absorbs_the_duplicate_into_the_booked_row():
    """DETECT-ONLY UNTIL 2026-08-31, then earned.

    It shipped as a detector on purpose: absorbing a row DELETES A LOSS, and a
    detector wrong in that direction manufactures a win rate. So it named rows
    first. QC-083's first real fire named exactly two, both HCMC — two, not
    twenty — and the heal is now sized against a real list rather than a
    hypothesis.
    """
    booked = _row("req_0826", date="2026-08-26", mdolx="261145",
                  has_send=True, status="WIN")
    stale = _row("req_0825", date="2026-08-25")
    data, log = _absorb([booked, stale])
    assert [r["request_id"] for r in data["requests"]] == ["req_0826"], (
        "the superseded re-ask was not absorbed")
    assert any("absorbed req_0825" in f for f in log.fixes), log.fixes


def test_the_absorbed_rows_evidence_moves_to_the_survivor():
    """The duplicate carries the thread it was reached through. Dropping the
    row without folding that in loses the only record of how it was found."""
    booked = _row("req_0826", date="2026-08-26", mdolx="261145",
                  has_send=True, status="WIN")
    booked["source_imids"] = ["<a@ol>"]
    stale = _row("req_0825", date="2026-08-25")
    stale["source_imids"] = ["<b@ol>"]
    data, _log = _absorb([booked, stale])
    kept = data["requests"][0]
    assert set(kept["source_imids"]) == {"<a@ol>", "<b@ol>"}
    assert any("Absorbed superseded re-ask req_0825" in n
               for n in kept.get("merge_notes", [])), kept.get("merge_notes")


def test_an_operator_correction_outranks_the_heal():
    """operator_corrections.json is the only durable human state in a system
    that rebuilds every row each fire. A row Michael pinned is a row he looked
    at, and no heuristic gets to delete it — the conflict is REPORTED so it is
    visible, rather than silently resolved in the code's favour."""
    booked = _row("req_0826", date="2026-08-26", mdolx="261145",
                  has_send=True, status="WIN")
    stale = _row("req_0825", date="2026-08-25")
    stale["manual_locked"] = True
    data, log = _absorb([booked, stale])
    assert len(data["requests"]) == 2, "a manual_locked row was deleted"
    assert any("operator correction" in w for w in log.warnings), log.warnings
    assert not any("absorbed" in f for f in log.fixes), log.fixes


def test_absorbing_is_idempotent_across_the_two_passes_per_fire():
    """qc_selfheal runs TWICE per fire. The second pass must find nothing left
    to do rather than double-logging or touching the survivor again."""
    booked = _row("req_0826", date="2026-08-26", mdolx="261145",
                  has_send=True, status="WIN")
    stale = _row("req_0825", date="2026-08-25")
    data, first = _absorb([booked, stale])
    notes_after_one = list(data["requests"][0].get("merge_notes", []))
    log2 = qc_selfheal.Log()
    qc_selfheal.phase_4_duplicates(log2, data)
    assert len(data["requests"]) == 1
    assert data["requests"][0].get("merge_notes", []) == notes_after_one, (
        "the second pass appended a duplicate merge note")
    assert not any("absorbed" in f for f in log2.fixes), log2.fixes
    assert len(first.fixes) >= 1


def test_the_survivor_is_a_row_that_survives_phase_4():
    """QC-083 used to scan the ORIGINAL data["requests"], not what passes 1
    and 2 left standing. Harmless while it only reported. Now it folds the
    absorbed row EVIDENCE into the survivor before deleting it — and if the
    survivor it picked is a row pass 1 already discarded, the evidence goes
    out with it and the stale row is deleted anyway. Net: one row gone, its
    thread gone, and nothing to show it.

    Two dicts share request_id req_win here; pass 1 keeps the richer one. The
    poorer twin is listed FIRST, so a scan over the original list would elect
    it as the booked row.
    """
    poor = _row("req_win", date="2026-08-26", mdolx="261145", has_send=True,
                status="WIN")
    poor["source_imids"] = ["<poor@ol>"]
    rich = _row("req_win", date="2026-08-26", mdolx="261145", has_send=True,
                status="WIN")
    rich["source_imids"] = ["<rich@ol>"]
    rich["carrier"] = "Maersk"      # the extra field that makes it the keeper
    rich["rate_usd"] = 4200
    stale = _row("req_0825", date="2026-08-25")
    stale["source_imids"] = ["<stale@ol>"]

    data = {"requests": [poor, rich, stale]}
    log = qc_selfheal.Log()
    qc_selfheal.phase_4_duplicates(log, data)

    assert len(data["requests"]) == 1, [r["request_id"] for r in data["requests"]]
    kept = data["requests"][0]
    assert kept is rich, "phase 4 kept the poorer twin"
    assert "<stale@ol>" in kept["source_imids"], (
        "the absorbed row evidence was folded into a row that was then dropped")
    assert len([m for m in log.fixes if "QC-083" in m]) == 1, log.fixes


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
def test_every_stale_copy_is_absorbed_not_just_the_first(n_stale):
    booked = _row("req_win", date="2026-08-26", mdolx="261145", has_send=True,
                  status="WIN")
    stale = [_row(f"req_s{i}", date=f"2026-08-2{i}") for i in range(n_stale)]
    data = {"requests": [booked, *stale]}
    log = qc_selfheal.Log()
    qc_selfheal.phase_4_duplicates(log, data)
    lines = [m for m in log.fixes if "QC-083" in m]
    assert len(lines) == n_stale
    for r in stale:
        assert any(r["request_id"] in m for m in lines)
    assert [r["request_id"] for r in data["requests"]] == ["req_win"]
