"""A booking ref an operator correction names belongs to ONE row: that one.

`ingest.claim_corrected_mdolx_refs`, approved by Michael 2026-09-03 after the
mechanism behind all eleven live QC-069 `duplicate_mdolx` findings was measured
(diag-blob run 33788252407):

    link_bookings_to_requests   writes mdolx_ref AND appends to mdolx_refs_all
                                on the row IT scored best
    apply_operator_corrections  row.update(changes) overwrites mdolx_ref on the
                                row the OPERATOR named — and touches
                                mdolx_refs_all not at all

They do not name the same row, so one ref ends on two. Every fixture here is a
shape READ OFF THE LIVE DATA in that run, not one invented to make the code
pass — the previous attempt at this heal was scoped from an instrument that
could not tell the shapes apart, and that is the failure these tests exist to
stop repeating.

The destructive half is what needs the most cover: 4b REMOVES a WIN row and 4c
leaves a real request row's WIN unsupported. Both were approved explicitly;
neither may fire one row wider than approved.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import core as C  # noqa: E402
import ingest as IN  # noqa: E402


@pytest.fixture
def corrections(tmp_path, monkeypatch):
    """Write a corrections file and point ingest at it."""
    def _write(entries):
        f = tmp_path / "operator_corrections.json"
        f.write_text(json.dumps({"corrections": entries}), encoding="utf-8")
        monkeypatch.setattr(IN, "CORRECTIONS_PATH", f)
        return f
    return _write


def _corr(rid, ref, source="ol-booking-recap-2026-08-12"):
    return {"request_id": rid, "set": {"status": "WIN", "mdolx_ref": ref},
            "source": source}


# ── 4a — the loser keeps a ref of its own; nothing is removed ─────────

def _rows_4a():
    """MDOLX261026, live. Both rows are WIN, both keep their own ref."""
    return [
        {"request_id": "req_1debac530d998acb", "status": "WIN",
         "mdolx_ref": "261029", "mdolx_refs_all": ["261026"], "teu_won": 2},
        {"request_id": "req_3b1d82eaa1d6450f", "status": "WIN",
         "mdolx_ref": "261026", "mdolx_refs_all": ["261028"], "teu_won": 1},
    ]


def test_4a_demotes_the_stale_entry_and_removes_no_row(corrections):
    corrections([_corr("req_3b1d82eaa1d6450f", "261026")])
    rows = _rows_4a()
    assert IN.claim_corrected_mdolx_refs(rows)[0] == 1
    assert len(rows) == 2, "4a must never remove a row"
    loser = rows[0]
    assert "261026" not in (loser["mdolx_refs_all"] or [])
    assert loser["mdolx_ref"] == "261029", "its own ref is untouched"
    assert loser["status"] == "WIN"


def test_4a_kills_the_double_count(corrections):
    corrections([_corr("req_3b1d82eaa1d6450f", "261026")])
    rows = _rows_4a()
    before = sum(C.booking_count(r) for r in rows)
    IN.claim_corrected_mdolx_refs(rows)
    after = sum(C.booking_count(r) for r in rows)
    assert after == before - 1, (
        "the shipment was counted on two rows; exactly one count must go")


def test_4a_keeps_the_ref_joinable_for_the_booking_pdf(corrections):
    """THE objection that killed the first draft of this heal.

    patch_carriers joins a booking PDF on [mdolx_ref] + mdolx_refs_all; the PDF
    supplies `pod`, and PASS 2b recovers destination/lane from it. Deleting the
    ref outright severs that join, the row falls to "Lane unresolved", and
    gen_client_email._lane_resolved drops it from every client bucket — Lonny
    told one FEWER booking for a shipment OL confirmed, with every other guard
    still green.
    """
    corrections([_corr("req_3b1d82eaa1d6450f", "261026")])
    rows = _rows_4a()
    IN.claim_corrected_mdolx_refs(rows)
    assert "261026" in rows[0]["mdolx_refs_seen"], (
        "the ref must stay joinable — demoted, not deleted")


def test_the_three_enrichment_joins_read_the_demoted_field():
    src = (ROOT / "scripts" / "patch_carriers.py").read_text(encoding="utf-8")
    # The booking-PDF join, the rate-response lookup and the carrier scan.
    assert src.count("mdolx_refs_seen") >= 3, (
        "a demoted ref must stay joinable at every enrichment site")


# ── 4b — the loser is an orphan booking row and IS removed ────────────

def _rows_4b():
    """MDOLX261027, live: a real request row beside the matcher's orphan."""
    return [
        {"request_id": "req_da035af71f7ec39d", "status": "WIN",
         "mdolx_ref": "261027", "mdolx_refs_all": ["261029"],
         "source_imids": ["<a>"], "lane": "Oakland → Yokohama"},
        {"request_id": "stand_261027", "status": "WIN",
         "mdolx_ref": "261027", "mdolx_refs_all": ["261027"],
         "source_imids": ["<b>"], "carrier_won": "CMA CGM",
         "booking_timestamp": "2026-08-13T20:04:21Z", "teu_won": 2},
    ]


def test_4b_removes_the_orphan_row(corrections):
    corrections([_corr("req_da035af71f7ec39d", "261027")])
    rows = _rows_4b()
    IN.claim_corrected_mdolx_refs(rows)
    assert [r["request_id"] for r in rows] == ["req_da035af71f7ec39d"]


def test_4b_absorbs_the_orphans_evidence_before_removing_it(corrections):
    """Removing the row must not remove what it knew."""
    corrections([_corr("req_da035af71f7ec39d", "261027")])
    rows = _rows_4b()
    IN.claim_corrected_mdolx_refs(rows)
    owner = rows[0]
    assert "<b>" in owner["source_imids"], "the orphan's message was dropped"
    assert owner["booking_timestamp"] == "2026-08-13T20:04:21Z", (
        "a booking field the owner lacked was not carried over")
    assert any("stand_261027" in n for n in owner.get("merge_notes", [])), (
        "a removed row must leave a trace naming it")


def test_4b_carries_the_orphans_booked_volume_to_the_owner(corrections):
    """The defect this heal nearly shipped, caught by rehearsing it over all
    eleven live findings before pushing.

    An orphan `stand_` row is built FROM the booking confirmation, which names
    the containers; the owner's RFQ thread often does not, so the owner sits at
    teu_won=None while the orphan holds the volume. Absorb without carrying
    teu_won and REMOVING the orphan silently deletes booked TEU for a shipment
    OL confirmed — the client-report under-count the whole design exists to
    avoid, committed by the fix for it.
    """
    corrections([_corr("req_owner", "261032")])
    rows = [
        {"request_id": "req_owner", "status": "WIN", "mdolx_ref": "261032"},
        {"request_id": "stand_261032", "status": "WIN", "mdolx_ref": "261032",
         "mdolx_refs_all": ["261032"], "teu_won": 2},
    ]
    before = sum(r.get("teu_won") or 0 for r in rows)
    IN.claim_corrected_mdolx_refs(rows)
    assert sum(r.get("teu_won") or 0 for r in rows) == before, (
        "booked TEU vanished with the absorbed row")
    assert rows[0]["teu_won"] == 2


def test_4b_does_not_double_the_volume_when_the_owner_has_its_own(corrections):
    corrections([_corr("req_owner", "261027")])
    rows = [
        {"request_id": "req_owner", "status": "WIN", "mdolx_ref": "261027",
         "teu_won": 2},
        {"request_id": "stand_261027", "status": "WIN", "mdolx_ref": "261027",
         "teu_won": 2},
    ]
    IN.claim_corrected_mdolx_refs(rows)
    assert rows[0]["teu_won"] == 2, "fill-only; the duplicate must not add"


def test_4b_never_overwrites_a_field_the_owner_already_has(corrections):
    corrections([_corr("req_da035af71f7ec39d", "261027")])
    rows = _rows_4b()
    rows[0]["carrier_won"] = "ONE"          # the owner's own evidence
    IN.claim_corrected_mdolx_refs(rows)
    assert rows[0]["carrier_won"] == "ONE", (
        "fill-only: the surviving row's own thread is the better evidence")


def test_4b_stops_the_shipment_being_counted_twice(corrections):
    corrections([_corr("req_da035af71f7ec39d", "261027")])
    rows = _rows_4b()
    before = sum(C.booking_count(r) for r in rows)
    IN.claim_corrected_mdolx_refs(rows)
    assert sum(C.booking_count(r) for r in rows) < before


def test_4b_also_fires_on_the_ol_backfill_prefix(corrections):
    """`ol_` rows are equally chain-less — core.has_no_rfq_chain owns this."""
    corrections([_corr("req_real", "261027")])
    rows = [
        {"request_id": "req_real", "status": "WIN", "mdolx_ref": "261027"},
        {"request_id": "ol_261027", "status": "WIN", "mdolx_ref": "261027"},
    ]
    IN.claim_corrected_mdolx_refs(rows)
    assert [r["request_id"] for r in rows] == ["req_real"]


# ── 4c — a REAL request row loses a win it never owned ────────────────

def _rows_4c():
    """MDOLX261031, live. req_f942b967 is dated 2026-08-26 — AFTER the 08-13
    booking — and carries teu_won=8. It is a real RFQ the matcher stamped WIN
    with someone else's booking."""
    return [
        {"request_id": "req_b789e573316ead86", "status": "WIN",
         "mdolx_ref": "261031", "mdolx_refs_all": []},
        {"request_id": "req_f942b9672ff756ab", "status": "WIN",
         "mdolx_ref": "261031", "mdolx_refs_all": ["261031"], "teu_won": 8},
    ]


def test_4c_strips_the_ref_but_keeps_the_row(corrections):
    corrections([_corr("req_b789e573316ead86", "261031")])
    rows = _rows_4c()
    IN.claim_corrected_mdolx_refs(rows)
    assert len(rows) == 2, "a row with a real RFQ thread is never removed"
    loser = rows[1]
    assert not loser["mdolx_ref"]
    assert loser["mdolx_refs_all"] == []
    assert "261031" in loser["mdolx_refs_seen"]


def test_4c_leaves_the_row_re_derivable_rather_than_a_refless_win(corrections):
    """ingest.age_requests skips a WIN only while it still holds a ref
    (ingest.py:1864). Stripping it is what makes the row re-decidable."""
    corrections([_corr("req_b789e573316ead86", "261031")])
    rows = _rows_4c()
    IN.claim_corrected_mdolx_refs(rows)
    loser = rows[1]
    assert not (loser.get("mdolx_ref") or loser.get("mdolx_refs_all")), (
        "still terminal — age_requests would never re-decide it")
    IN.age_requests(rows)
    assert loser["status"] != "WIN", (
        "the row kept a win it had no booking for")
    assert loser["teu_won"] == 0, (
        "_clear_win_evidence_on_exit must drop volume that was never booked")


def test_the_owner_keeps_its_win_through_all_of_it(corrections):
    corrections([_corr("req_b789e573316ead86", "261031")])
    rows = _rows_4c()
    IN.claim_corrected_mdolx_refs(rows)
    IN.age_requests(rows)
    assert rows[0]["status"] == "WIN"
    assert rows[0]["mdolx_ref"] == "261031"
    assert C.is_confirmed_win(rows[0])


def test_4c_never_moves_the_losers_volume_onto_the_owner(corrections):
    """THE bug this heal nearly shipped, and no test caught it until now.

    Found by rehearsing the change over the eleven live findings before
    pushing, not by a test: adding teu_won to the absorbable set (correct for
    an orphan) also absorbed it from a 4c loser (wrong). req_f942b9672ff756ab
    is its OWN 2026-08-26 RFQ carrying teu_won=8; moving that onto
    req_b789e573316ead86 fabricates 8 TEU on a confirmed win.

    A `stand_`/`ol_` row IS the booking and its evidence belongs to the owner.
    An ordinary `req_` row is a DIFFERENT ASK the matcher merely stamped. Only
    the ref moves.
    """
    corrections([_corr("req_b789e573316ead86", "261031")])
    rows = _rows_4c()
    IN.claim_corrected_mdolx_refs(rows)
    owner, loser = rows[0], rows[1]
    assert not owner.get("teu_won"), (
        f"the owner took the loser's volume: teu_won={owner.get('teu_won')}")
    assert loser["teu_won"] == 8, "the loser's own volume was taken from it"


def test_4a_never_moves_the_losers_evidence_onto_the_owner(corrections):
    """Same rule, the 4a shape: both rows are real requests with real threads."""
    corrections([_corr("req_3b1d82eaa1d6450f", "261026")])
    rows = _rows_4a()
    rows[0]["carrier_won"] = "ONE"
    rows[0]["source_imids"] = ["<loser-only>"]
    rows[1].pop("carrier_won", None)
    rows[1]["source_imids"] = ["<owner>"]
    IN.claim_corrected_mdolx_refs(rows)
    owner = rows[1]
    assert owner.get("carrier_won") != "ONE", (
        "took a carrier from a different request's thread")
    assert "<loser-only>" not in (owner.get("source_imids") or []), (
        "took another request's messages")


def test_only_a_chainless_row_is_absorbed_from():
    """The predicate itself, so the rule cannot be softened by accident."""
    src = (ROOT / "scripts" / "ingest.py").read_text(encoding="utf-8")
    body = src.split("def claim_corrected_mdolx_refs")[1]
    i = body.index("_ABSORBABLE_BOOKING_FIELDS")
    guard = body[max(0, i - 900):i]
    assert "if chainless:" in guard, (
        "absorption is no longer gated on the loser having no RFQ chain")


# ── the refusals: this must not fire one row wider than approved ──────

def test_a_ref_no_correction_names_is_never_touched(corrections):
    """Case (3) in diag_duplicate_mdolx — genuinely ambiguous, no operator
    verdict to defer to. Two rows may keep claiming it."""
    corrections([])
    rows = [
        {"request_id": "req_a", "status": "WIN", "mdolx_ref": "261099"},
        {"request_id": "req_b", "status": "WIN", "mdolx_ref": "261099"},
    ]
    assert IN.claim_corrected_mdolx_refs(rows)[0] == 0
    assert len(rows) == 2
    assert rows[1]["mdolx_ref"] == "261099"


def test_a_correction_whose_own_row_lacks_the_ref_does_nothing(corrections):
    """The owner must actually HOLD it. Otherwise this is not the collision,
    and clearing a field on evidence that never named these rows is the same
    class of error as the heal that was refused."""
    corrections([_corr("req_missing", "261031")])
    rows = [
        {"request_id": "req_missing", "status": "WIN", "mdolx_ref": None},
        {"request_id": "req_holds_it", "status": "WIN", "mdolx_ref": "261031"},
    ]
    assert IN.claim_corrected_mdolx_refs(rows)[0] == 0
    assert rows[1]["mdolx_ref"] == "261031"


def test_an_exclude_correction_is_not_a_claim(corrections):
    corrections([{"request_id": "stand_260821", "exclude": True}])
    rows = [{"request_id": "req_a", "status": "WIN", "mdolx_ref": "260821"},
            {"request_id": "req_b", "status": "WIN", "mdolx_ref": "260821"}]
    assert IN.claim_corrected_mdolx_refs(rows)[0] == 0


def test_the_owner_is_never_emptied(corrections):
    corrections([_corr("req_owner", "261031")])
    rows = [{"request_id": "req_owner", "status": "WIN", "mdolx_ref": "261031",
             "mdolx_refs_all": ["261031"]}]
    IN.claim_corrected_mdolx_refs(rows)
    assert rows[0]["mdolx_ref"] == "261031"
    assert C.is_confirmed_win(rows[0])


# ── idempotence: this runs on EVERY fire, twice per qc pass ───────────

def test_running_it_again_changes_nothing(corrections):
    """qc_selfheal re-runs it, twice per fire, and the matcher re-derives the
    duplicate every fire. A pass that is not idempotent absorbs the same row
    repeatedly and rewrites merge_notes forever."""
    corrections([_corr("req_da035af71f7ec39d", "261027")])
    rows = _rows_4b()
    first, _ = IN.claim_corrected_mdolx_refs(rows)
    snapshot = json.dumps(rows, sort_keys=True, default=str)
    assert IN.claim_corrected_mdolx_refs(rows)[0] == 0, "second pass acted again"
    assert json.dumps(rows, sort_keys=True, default=str) == snapshot
    assert first == 1


def test_running_it_again_changes_nothing_on_4a_either(corrections):
    """4b is a WEAK idempotence test and this is why: its loser is removed, so
    a second pass is trivially safe even when the claim predicate is wrong.
    4a keeps both rows, so a predicate that treats a DEMOTED ref as a claim
    re-acts on the same row every pass — forever. Measured: with
    mdolx_refs_seen counted as a claim, passes 1, 2 and 3 all report 1.
    """
    corrections([_corr("req_3b1d82eaa1d6450f", "261026")])
    rows = _rows_4a()
    assert IN.claim_corrected_mdolx_refs(rows)[0] == 1
    snapshot = json.dumps(rows, sort_keys=True, default=str)
    assert IN.claim_corrected_mdolx_refs(rows)[0] == 0
    assert IN.claim_corrected_mdolx_refs(rows)[0] == 0
    assert json.dumps(rows, sort_keys=True, default=str) == snapshot


def test_a_demoted_ref_is_not_a_claim(corrections):
    """The mechanism behind idempotence, pinned on its own: mdolx_refs_seen
    must not count as holding the ref, or every pass re-absorbs."""
    assert IN._row_holds({"mdolx_refs_seen": ["261031"]}, "261031") is False
    assert IN._row_holds({"mdolx_ref": "261031"}, "261031") is True
    assert IN._row_holds({"mdolx_refs_all": ["261031"]}, "261031") is True


# ── the shape guards the next writer will trip over ──────────────────

def test_demote_always_writes_lists_never_none():
    """ingest.py:1054 does `set(best.get("mdolx_refs_all", []) + [mdolx])`,
    which TypeErrors on None the next fire."""
    r = {"mdolx_ref": "261031", "mdolx_refs_all": None, "mdolx_refs_seen": None}
    IN._demote_ref(r, "261031")
    assert isinstance(r["mdolx_refs_all"], list)
    assert isinstance(r["mdolx_refs_seen"], list)
    # and the real call site survives it
    assert sorted(set((r.get("mdolx_refs_all") or []) + ["999999"])) == ["999999"]


def test_the_counting_fields_are_the_only_ones_that_count():
    """core.booking_count and QC-069 must NOT learn about mdolx_refs_seen —
    that is the entire point of the split."""
    core_src = (ROOT / "scripts" / "core.py").read_text(encoding="utf-8")
    qc_src = (ROOT / "scripts" / "qc_selfheal.py").read_text(encoding="utf-8")
    assert "mdolx_refs_seen" not in core_src, (
        "a demoted ref must never re-enter a count")
    assert "mdolx_refs_seen" not in qc_src, (
        "a demoted ref must never re-open the QC-069 finding it resolved")


def test_qc069_stops_firing_on_a_healed_pair(corrections):
    import qc_selfheal as QC
    corrections([_corr("req_3b1d82eaa1d6450f", "261026")])
    rows = _rows_4a()
    before = [f for f in QC.qc069_duplicate_shipment_rows(rows)
              if f[0] == "duplicate_mdolx" and f[1] == "261026"]
    assert before, "fixture does not reproduce the finding"
    IN.claim_corrected_mdolx_refs(rows)
    after = [f for f in QC.qc069_duplicate_shipment_rows(rows)
             if f[0] == "duplicate_mdolx" and f[1] == "261026"]
    assert not after, "the finding survived its own heal"


def test_both_pipeline_entry_points_run_the_claim():
    ing = (ROOT / "scripts" / "ingest.py").read_text(encoding="utf-8")
    qc = (ROOT / "scripts" / "qc_selfheal.py").read_text(encoding="utf-8")
    assert "claim_corrected_mdolx_refs(all_requests)" in ing
    assert "claim_corrected_mdolx_refs(data[\"requests\"])" in qc, (
        "the QC backstop must re-apply it, or intake and QC drift")
    # BOTH paths must re-derive the released rows, and BOTH must scope it with
    # only= — an unrestricted age_requests after the operator layer reverses
    # human verdicts (see test_the_re_derive_never_overrules_an_operator).
    for src, name, call in ((ing, "ingest", "claim_corrected_mdolx_refs(all_requests)"),
                            (qc, "qc_selfheal", 'claim_corrected_mdolx_refs(data["requests"])')):
        i = src.index(call)          # the CALL SITE, not the def
        window = src[i:i + 2600]
        assert "only=" in window and "age_requests" in window, (
            f"{name} does not re-derive the released rows with only=")
    # and the QC path must RECORD it — Log.ok() only prints
    assert "MDOLX claim resolved" in qc and "log.fix(" in qc


# ── the three defects an adversarial pass found in this heal ──────────
# Each was CONFIRMED by executing the code, not by reading it, and each was
# invisible to the 25 tests passing at the time.

def test_the_re_derive_never_overrules_an_operator(corrections):
    """BLOCKING, found 2026-09-03. The first version re-ran the whole
    `age_requests` after the claim.

    `apply_operator_corrections` is documented as "applied LAST so they win
    over every automatic classification" — and `age_requests` has NO
    manual_locked guard. A second unrestricted call after the operator layer
    therefore reverses human verdicts. Measured: a correction setting
    LOSS/SEND_NO_BOOKING on a FRESH send-signal came back
    PENDING / AWAITING_MDOLX.
    """
    from datetime import datetime, timedelta, timezone
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    row = {"request_id": "req_x", "status": "WIN", "has_send": True,
           "quoted": True, "mdolx_ref": None, "mdolx_refs_all": [],
           "send_signal_events": [{"at": fresh}], "response_timestamp": fresh,
           "etd_fit_days": None, "request_timestamp": fresh}
    rows = [row]
    corrections([{"request_id": "req_x",
                  "set": {"status": "LOSS", "loss_reason": "SEND_NO_BOOKING"},
                  "source": "linda"}])
    IN.apply_operator_corrections(rows)
    assert row["status"] == "LOSS" and row["manual_locked"] is True

    # An explicit, scoped re-derive must leave it alone...
    IN.age_requests(rows, only=rows)
    assert row["status"] == "LOSS", "the re-derive overruled a human verdict"
    assert row["loss_reason"] == "SEND_NO_BOOKING"


def test_the_unscoped_age_requests_is_what_made_that_unsafe(corrections):
    """The other half of the pin: `only=None` is the ORIGINAL pre-corrections
    call and deliberately still re-decides everything. If that ever stops being
    true the guard above is measuring nothing, so this asserts the difference
    is real rather than incidental.
    """
    from datetime import datetime, timedelta, timezone
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    row = {"request_id": "req_x", "status": "LOSS",
           "loss_reason": "SEND_NO_BOOKING", "manual_locked": True,
           "has_send": True, "quoted": True, "mdolx_ref": None,
           "mdolx_refs_all": [], "send_signal_events": [{"at": fresh}],
           "response_timestamp": fresh, "etd_fit_days": None,
           "request_timestamp": fresh}
    IN.age_requests([row])                     # unscoped: the intake call
    assert row["status"] != "LOSS", (
        "unscoped age_requests no longer re-decides — the only= guard is moot")


def test_demoting_one_ref_never_strands_another_real_booking(corrections):
    """BLOCKING, found 2026-09-03. `_demote_ref` cleared `mdolx_ref` without
    promoting a survivor out of `mdolx_refs_all`.

    A row can hold more than one booking. `mdolx_ref` is the primary
    decide_status reads, and it is the ONLY ref qc_selfheal's decide loop
    passes (qc_selfheal.py:1540). Measured without the promotion: a row holding
    261031 (disputed) and 261099 (real, undisputed) came out mdolx_ref=None and
    decide_status returned LOSS/SEND_NO_BOOKING — the surviving booking
    silently stops being a win. `is_confirmed_win` reads the union and stays
    True throughout, so nothing else notices.
    """
    corrections([_corr("req_owner", "261031")])
    rows = [
        {"request_id": "req_owner", "status": "WIN", "mdolx_ref": "261031"},
        {"request_id": "req_two", "status": "WIN", "mdolx_ref": "261031",
         "mdolx_refs_all": ["261099"], "teu_won": 4, "has_send": True,
         "quoted": True, "response_timestamp": "2026-08-01T12:00:00Z",
         "request_timestamp": "2026-08-01T10:00:00Z", "etd_fit_days": None},
    ]
    IN.claim_corrected_mdolx_refs(rows)
    survivor = rows[1]
    assert survivor["mdolx_ref"] == "261099", (
        "the surviving booking was left out of the primary field")
    assert C.decide_status(
        has_send=True, mdolx_ref=survivor["mdolx_ref"], quoted=True,
        response_timestamp=survivor["response_timestamp"],
        request_timestamp=survivor["request_timestamp"],
        etd_fit_days=None).status == "WIN", (
        "a row still holding a real booking was re-decided out of WIN")


def test_a_promoted_survivor_is_not_the_disputed_ref(corrections):
    corrections([_corr("req_owner", "261031")])
    rows = [
        {"request_id": "req_owner", "status": "WIN", "mdolx_ref": "261031"},
        {"request_id": "req_two", "status": "WIN", "mdolx_ref": "261031",
         "mdolx_refs_all": ["261031", "261099"]},
    ]
    IN.claim_corrected_mdolx_refs(rows)
    assert rows[1]["mdolx_ref"] == "261099"
    assert "261031" not in (rows[1]["mdolx_refs_all"] or [])


def test_the_qc_backstop_records_what_it_did(corrections):
    """SHOULD-FIX, found 2026-09-03: the QC path called the claim — which
    REMOVES WIN rows — and assigned the count to a variable nothing read.

    `Log.ok()` only PRINTS; it is not recorded on the Log and never reaches
    qc-result.json. A destructive edit that reaches neither is absent from the
    audit entirely, which is the QC-019/QC-077 lesson in this repo.
    """
    qc = (ROOT / "scripts" / "qc_selfheal.py").read_text(encoding="utf-8")
    i = qc.index('claim_corrected_mdolx_refs(data["requests"])')
    window = qc[i:i + 2200]
    assert "log.fix(" in window, "the claim's edits never reach the audit"
    assert "log.ok(" not in window, "log.ok only prints — it is not recorded"


def test_two_corrections_disagreeing_is_reported_not_silently_resolved(
        corrections, capsys):
    """A row the claim empties that carries its OWN operator correction.

    The re-derive skips manual_locked rows — never overrule a human — so a
    locked WIN stays a WIN with no booking ref, which is QC-049's error
    condition. That is the honest outcome; the alternative is silently
    reversing a human verdict. But the contradiction lives in
    operator_corrections.json and only a human can settle it, so it must be
    loud rather than quietly picked.
    """
    corrections([_corr("req_owner", "261031"), _corr("req_locked", "261031")])
    rows = [
        {"request_id": "req_owner", "status": "WIN", "mdolx_ref": "261031"},
        {"request_id": "req_locked", "status": "WIN", "mdolx_ref": "261031",
         "manual_locked": True},
    ]
    IN.claim_corrected_mdolx_refs(rows)
    out = capsys.readouterr().out
    assert "::warning::" in out, "a corrections conflict was resolved silently"
    assert "req_locked" in out and "operator correction of its own" in out
    # and the human's verdict is untouched
    IN.age_requests(rows, only=[rows[1]])
    assert rows[1]["status"] == "WIN", "the re-derive overruled a locked row"
