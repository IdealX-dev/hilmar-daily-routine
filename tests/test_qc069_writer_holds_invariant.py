"""QC-069 `duplicate_mdolx`, fixed at the WRITER: a booking ref an operator
correction names lands on the operator's row the moment it is linked, so no
rival row and no `stand_<ref>` row is ever stamped with it.

Sentry HILMAR-DAILY-TRACKER-N — 250 events since 2026-08-14, the same eleven
refs on every fire. The mechanism (verified 2026-09-04 by executing it):

    link_bookings_to_requests   scored each of the eight near-identical
                                CMA CGM 1x40'RF Oakland→Yokohama bookings of
                                Aug 3-5 against the same asks; every candidate
                                tied and the tiebreak ("latest ask first") is a
                                PERMUTATION of the operator's mapping. Once the
                                lane ran out of unmatched asks the rest became
                                `stand_<ref>` WIN rows with their own teu_won.
    apply_operator_corrections  then wrote the operator's ref onto the row the
                                correction names and cleared nothing off the
                                matcher's row.

`claim_corrected_mdolx_refs` (2026-09-03) restores the invariant AFTER both
writers ran. Correct and idempotent — and a released 4c row still carried
every stamp the retracted link had written (has_send, quoted, carrier_won,
booking_timestamp, olusa_time_et, product, temperature, reason_detail, a
PENDING→WIN history entry): CLAUDE.md's "nothing un-stamps a bad value",
applied to the heal itself. The fix is to never write the bad value: the
matcher reads the SAME corrections file the applier reads, before it scores.

Every fixture below is the live shape read off `operator_corrections.json`
(the eleven `ol-booking-recap-2026-08-12` entries) and the staged
confirmations (2026-08-13 20:04-20:21Z), not one invented to make the code
pass.
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
import qc_selfheal as QC  # noqa: E402


@pytest.fixture
def corrections(tmp_path, monkeypatch):
    """Write a corrections file and point ingest at it — the same fixture
    tests/test_mdolx_claim.py uses, so the matcher, the applier and the claim
    are all tested against ONE file."""
    def _write(entries):
        f = tmp_path / "operator_corrections.json"
        f.write_text(json.dumps({"corrections": entries}), encoding="utf-8")
        monkeypatch.setattr(IN, "CORRECTIONS_PATH", f)
        return f
    return _write


# ── the live data ──────────────────────────────────────────────────────

#: (request_id, mdolx_ref, booking_timestamp) verbatim from the ten Yokohama
#: entries of scripts/operator_corrections.json, source
#: ol-booking-recap-2026-08-12. 261031's correction carries NO
#: booking_timestamp — that is the 4c pair.
YOKOHAMA = (
    ("req_3b1d82eaa1d6450f", "261026", "2026-08-03T21:43:00Z"),
    ("req_da035af71f7ec39d", "261027", "2026-08-03T21:51:00Z"),
    ("req_ce5ca8fe446706aa", "261028", "2026-08-03T21:57:00Z"),
    ("req_1debac530d998acb", "261029", "2026-08-03T22:03:00Z"),
    ("req_7d605d63e8dc72ea", "261030", "2026-08-03T22:09:00Z"),
    ("req_b789e573316ead86", "261031", None),
    ("req_076574f28c6c2556", "261032", "2026-08-03T22:19:00Z"),
    ("req_21cbe69a8ee12247", "261033", "2026-08-03T22:24:00Z"),
    ("req_8d9495acc556a1b5", "261046", "2026-08-05T22:12:00Z"),
    ("req_d26f157fc80d65df", "261047", "2026-08-05T22:11:00Z"),
)

#: When the confirmations were actually staged (diag-blob 33788252407): ten
#: days AFTER the bookings, one day after the recap was back-entered.
STAGED = "2026-08-13T20:{:02d}:00Z"


def _corr(rid, ref, booking_ts=None):
    s = {"status": "WIN", "mdolx_ref": ref, "carrier_won": "CMA CGM",
         "carrier_quoted": "CMA CGM"}
    if booking_ts:
        s["booking_timestamp"] = booking_ts
    return {"request_id": rid, "set": s, "source": "ol-booking-recap-2026-08-12"}


def _staged_rfq(imid, sent, subject="Oakland to Yokohama", preview="1x40RF"):
    """A lonny_outbound stage row EXACTLY as refresh_stage.build_stage_record
    writes one; build_requests turns it into the production request row."""
    return {"imid": imid, "id": imid, "bucket": "lonny_outbound", "sent": sent,
            "subject": subject, "summary_preview": preview, "in_reply_to": None,
            "references": [], "conversation_id": None, "body_parsed": {}}


def _confirmation(ref, sent, lane="Oakland to Yokohama", spec="1X40'RF",
                  carrier="CMA", bkg="NAM8624880"):
    """The booking dict collect_bookings emits for a NEW BOOKING CONFIRMATION."""
    return {"mdolx": ref, "sent": sent,
            "subject": f"MDOLX{ref}_NEW BOOKING CONFIRMATION // HILMAR {spec} "
                       f"{lane} // {carrier} BKG # {bkg}",
            "preview": "", "source_bucket": "mbd_inbound",
            "source_imid": f"<bk-{ref}>", "source_id": f"bk-{ref}",
            "body_signer": None, "body_parsed": {}, "in_reply_to": None,
            "references": []}


def _yokohama_asks(ids, start_day=28):
    """One production request row per operator-named id. Dated Jul 28 → Aug 4,
    one a day, so the first sits OUTSIDE the matcher's 14-day window from the
    Aug-13 staging — which is what forced the live stand_ rows: the lane runs
    out of scoreable asks before it runs out of bookings.

    request_id is assigned from the corrections file because production
    derives it from a conversation hash the fixture cannot reproduce; every
    other field comes out of build_requests.
    """
    rows = []
    for i, rid in enumerate(ids):
        day = start_day + i
        sent = (f"2026-07-{day}T16:00:00Z" if day <= 31
                else f"2026-08-{day - 31:02d}T16:00:00Z")
        (req,) = IN.build_requests([_staged_rfq(f"<rfq-{rid}>", sent)])
        req["request_id"] = rid
        rows.append(req)
    return rows


def _live_fixture():
    asks = _yokohama_asks([rid for rid, _, _ in YOKOHAMA])
    bookings = {ref: _confirmation(ref, STAGED.format(4 + 2 * i))
                for i, (_, ref, _) in enumerate(YOKOHAMA)}
    return asks, bookings


def _dupes(rows):
    return [f for f in QC.qc069_duplicate_shipment_rows(rows)
            if f[0] == "duplicate_mdolx"]


def _run_both_writers(rows, bookings):
    """The production order from ingest.main, with the CLAIM NOT CALLED —
    the claim is the backstop this test must not lean on."""
    requests, standalones = IN.link_bookings_to_requests(rows, bookings)
    all_rows = requests + standalones
    IN.age_requests(all_rows)
    IN.apply_operator_corrections(all_rows)
    return all_rows


# ── (a) the eleven live findings, without the backstop ─────────────────

def test_the_live_batch_produces_no_duplicate_without_the_claim(corrections):
    """THE finding. Ten operator-named bookings through the matcher and the
    applier — no claim pass — and QC-069's predicate finds nothing.

    Deleting the pre-link consult in link_bookings_to_requests turns this red
    (measured on the unfixed tree: 10 duplicate_mdolx findings; stand_261033,
    stand_261046 and stand_261047 emitted beside their operator-named rows).
    """
    corrections([_corr(*t) for t in YOKOHAMA])
    asks, bookings = _live_fixture()
    rows = _run_both_writers(asks, bookings)
    assert _dupes(rows) == [], _dupes(rows)
    assert sum(C.booking_count(r) for r in rows) == len(rows) == len(YOKOHAMA), (
        "one shipment counted twice, or a shipment lost")


def test_every_booking_lands_on_exactly_the_operators_row(corrections):
    corrections([_corr(*t) for t in YOKOHAMA])
    asks, bookings = _live_fixture()
    requests, standalones = IN.link_bookings_to_requests(asks, bookings)
    assert standalones == [], (
        "a stand_ row was emitted for a ref whose operator-named row exists: "
        + ", ".join(r["request_id"] for r in standalones))
    by_id = {r["request_id"]: r for r in requests}
    for rid, ref, _ in YOKOHAMA:
        assert by_id[rid]["mdolx_ref"] == ref, (rid, by_id[rid]["mdolx_ref"])
        assert by_id[rid]["mdolx_refs_all"] == [ref]
        assert by_id[rid]["_booking_match_via"] == "operator_correction"
        assert by_id[rid]["status"] == "WIN"


def test_the_backstop_is_a_steady_state_no_op_after_the_writer(corrections):
    """claim_corrected_mdolx_refs stays wired (it guards carried-forward prior
    state) and must find NOTHING to do on a fresh build."""
    corrections([_corr(*t) for t in YOKOHAMA])
    asks, bookings = _live_fixture()
    rows = _run_both_writers(asks, bookings)
    acted, released = IN.claim_corrected_mdolx_refs(rows)
    assert (acted, released) == (0, [])


def test_the_matcher_would_have_permuted_them(corrections):
    """Why a writer-side fix and not a better tiebreak: with no corrections
    file the same inputs are a permutation plus standalones. This pins the
    fixture as a real reproduction, so the green test above is not vacuous."""
    corrections([])
    asks, bookings = _live_fixture()
    requests, standalones = IN.link_bookings_to_requests(asks, bookings)
    got = {r["request_id"]: r.get("mdolx_ref") for r in requests}
    want = {rid: ref for rid, ref, _ in YOKOHAMA}
    assert got != want or standalones, (
        "the scorer happened to reproduce the operator's mapping — the "
        "fixture no longer exercises the collision")


# ── (b) the 4c pair: no rival row is ever stamped, so there is no residue ──

def test_a_rival_ask_is_never_stamped_and_carries_no_residue(corrections):
    """MDOLX261031, live. The operator's row is the 07-30 ask; the rival is a
    real 08-26 RFQ the matcher stamped WIN when an 08-27 message stood in as
    the booking. After the shipped claim released it, the rival still carried
    has_send/quoted/carrier_won/booking_timestamp/olusa_time_et and a
    PENDING→WIN history entry — a released row that reads as quoted-and-lost
    on a lane it was never quoted on. At the writer there is no residue,
    because there was never a stamp."""
    corrections([_corr("req_b789e573316ead86", "261031")])
    (owner,) = IN.build_requests([_staged_rfq("<o>", "2026-07-30T18:00:00Z")])
    owner["request_id"] = "req_b789e573316ead86"
    (rival,) = IN.build_requests([_staged_rfq("<r>", "2026-08-26T16:00:00Z")])
    rival["request_id"] = "req_f942b9672ff756ab"
    bookings = {"261031": _confirmation("261031", "2026-08-27T15:00:00Z",
                                        bkg="NAM8664236")}
    requests, standalones = IN.link_bookings_to_requests([owner, rival], bookings)
    assert standalones == []
    assert owner["mdolx_ref"] == "261031"
    for k in ("has_send", "quoted", "carrier_won", "booking_timestamp",
              "olusa_time_et", "mdolx_ref"):
        assert not rival.get(k), f"rival carries {k}={rival.get(k)!r}"
    assert rival["status"] == "PENDING"
    assert rival["status_history"] == []
    assert rival["mdolx_refs_all"] == []


def test_the_operators_verdict_outranks_the_time_and_window_guards(corrections):
    """An ask dated AFTER the booking, or outside 14 days, can never win it
    by SCORE — but the applier would stamp the operator's ref on that row
    regardless, and the scorer would have placed the booking elsewhere: the
    duplicate, re-created by a guard. The correction is the verdict; the
    guards are for the rows nobody has ruled on."""
    corrections([_corr("req_late", "261099")])
    (late,) = IN.build_requests([_staged_rfq("<l>", "2026-08-26T16:00:00Z")])
    late["request_id"] = "req_late"
    (other,) = IN.build_requests([_staged_rfq("<x>", "2026-08-01T16:00:00Z")])
    other["request_id"] = "req_other"
    bookings = {"261099": _confirmation("261099", "2026-08-13T20:04:00Z")}
    requests, standalones = IN.link_bookings_to_requests([other, late], bookings)
    assert standalones == []
    assert late["mdolx_ref"] == "261099"
    assert not other.get("mdolx_ref"), "the scorer's pick was stamped as well"


def test_a_header_chain_pointing_at_the_rival_does_not_overrule_the_operator(corrections):
    """OL's confirmation usually replies INSIDE Lonny's thread, so its
    References name an ask — and when Lonny reused a thread, the ask the
    chain names is not always the one the operator ruled on. The chain is the
    strongest SCORING signal; it must not run at all once the operator has
    spoken, or the consult is overwritten one branch later. Removing the
    `best is None and` gate on the chain branch turns this red; no other test
    gives a named booking a chain."""
    corrections([_corr("req_b789e573316ead86", "261031")])
    (owner,) = IN.build_requests([_staged_rfq("<o>", "2026-07-30T18:00:00Z")])
    owner["request_id"] = "req_b789e573316ead86"
    (rival,) = IN.build_requests([_staged_rfq("<r>", "2026-08-01T16:00:00Z")])
    rival["request_id"] = "req_rival"
    bk = _confirmation("261031", "2026-08-13T20:14:00Z", bkg="NAM8664236")
    bk["in_reply_to"] = "<r>"
    bk["references"] = ["<r>"]
    requests, standalones = IN.link_bookings_to_requests([owner, rival], {"261031": bk})
    assert standalones == []
    assert owner["mdolx_ref"] == "261031"
    assert owner["_booking_match_via"] == "operator_correction"
    assert not rival.get("mdolx_ref") and rival["status"] == "PENDING", (
        "the header chain overruled the operator's verdict")


def test_named_bookings_are_placed_before_the_scorer_consumes_their_rows(corrections):
    """An UNNAMED booking on the same lane must not take the row the operator
    has reserved for a named one — that row is spoken for. Named refs are
    linked first, so the scorer's "already matched" skip keeps it out of the
    pool, and the unnamed booking lands on the ask that is actually free."""
    corrections([_corr("req_reserved", "261026")])
    (free,) = IN.build_requests([_staged_rfq("<f>", "2026-08-01T16:00:00Z")])
    free["request_id"] = "req_free"
    (reserved,) = IN.build_requests([_staged_rfq("<v>", "2026-08-04T16:00:00Z")])
    reserved["request_id"] = "req_reserved"        # the LATER ask: the tiebreak's pick
    bookings = {
        # stage order: the unnamed one first
        "261099": _confirmation("261099", "2026-08-13T20:04:00Z"),
        "261026": _confirmation("261026", "2026-08-13T20:06:00Z"),
    }
    requests, standalones = IN.link_bookings_to_requests([free, reserved], bookings)
    assert standalones == []
    assert reserved["mdolx_ref"] == "261026"
    assert free["mdolx_ref"] == "261099", (
        f"the unnamed booking took the reserved row: free={free.get('mdolx_ref')} "
        f"reserved={reserved.get('mdolx_refs_all')}")


# ── (c) NEGATIVE direction: a narrow consult must never drop a booking ──

def test_a_named_ref_whose_row_is_absent_still_links_by_score(corrections):
    """QC-082 already reports the dangling correction; the booking itself
    must still land somewhere visible — here on the one free ask."""
    corrections([_corr("req_gone", "261099")])
    (ask,) = IN.build_requests([_staged_rfq("<a>", "2026-08-01T16:00:00Z")])
    ask["request_id"] = "req_present"
    bookings = {"261099": _confirmation("261099", "2026-08-13T20:04:00Z")}
    requests, standalones = IN.link_bookings_to_requests([ask], bookings)
    assert standalones == []
    assert ask["mdolx_ref"] == "261099"
    assert ask["_booking_match_via"] != "operator_correction"


def test_a_named_ref_whose_row_is_absent_still_becomes_a_standalone(corrections):
    corrections([_corr("req_gone", "261099")])
    bookings = {"261099": _confirmation("261099", "2026-08-13T20:04:00Z")}
    requests, standalones = IN.link_bookings_to_requests([], bookings)
    assert [r["request_id"] for r in standalones] == ["stand_261099"]


def test_an_exclude_correction_names_no_owner(corrections):
    corrections([{"request_id": "req_x", "exclude": True}])
    assert IN._corrected_ref_owners() == {}


def test_a_missing_or_unreadable_corrections_file_consults_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(IN, "CORRECTIONS_PATH", tmp_path / "absent.json")
    assert IN._corrected_ref_owners() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(IN, "CORRECTIONS_PATH", bad)
    assert IN._corrected_ref_owners() == {}


def test_the_matcher_reads_the_same_file_as_the_applier():
    """One source. A second path, a second predicate, or a re-spelled key is
    how the two writers came to disagree in the first place."""
    src = (ROOT / "scripts" / "ingest.py").read_text(encoding="utf-8")
    body = src.split("def _corrected_ref_owners")[1].split("\ndef ")[0]
    assert "CORRECTIONS_PATH" in body
    assert 'corr.get("exclude")' in body
    link = src.split("def link_bookings_to_requests")[1].split("\ndef ")[0]
    assert "_corrected_ref_owners()" in link, (
        "link_bookings_to_requests no longer consults the corrections file")


# ── (d) the 4c sub-mechanism: an invoice is never the booking ─────────

INVOICE = ("MDOLX261031_ EXPORT INVOICE AVAILABLE // HILMAR 1X40'RF Oakland "
           "to Yokohama // CMA BKG # NAM8664236")
CONFIRMATION = ("MDOLX261031_NEW BOOKING CONFIRMATION // HILMAR 1X40'RF Oakland "
                "to Yokohama // CMA BKG # NAM8664236")


def _staged(subject, sent, imid):
    return {"bucket": "mbd_inbound", "subject": subject, "sent": sent,
            "received": sent, "imid": imid, "summary_preview": ""}


def test_an_export_invoice_is_operational():
    """Verbatim: the only staged message carrying MDOLX261031. It passed the
    gate, _booking_rank admitted it at tier 0, and an 08-27 invoice became
    an 08-27 "booking" — late enough for an 08-26 ask to pass the
    req_ts <= bk_ts guard."""
    assert IN.is_operational_subject(INVOICE) is True


def test_an_invoice_alone_yields_no_booking():
    assert IN.collect_bookings([_staged(INVOICE, "2026-08-27T15:00:00Z", "<i>")]) == {}


def test_the_confirmation_in_the_same_thread_still_yields_one():
    got = IN.collect_bookings([
        _staged(INVOICE, "2026-08-27T15:00:00Z", "<i>"),
        _staged(CONFIRMATION, "2026-08-13T20:14:00Z", "<c>"),
    ])
    assert list(got) == ["261031"]
    assert got["261031"]["subject"] == CONFIRMATION
    assert got["261031"]["sent"] == "2026-08-13T20:14:00Z"


def test_the_invoice_hint_is_mirrored_in_both_trees():
    """_OPERATIONAL_SUBJECT_HINTS is a mirror-by-hand surface."""
    from hilmar import ingest as lib_ingest
    for hint in ("EXPORT INVOICE", "INVOICE AVAILABLE"):
        assert hint in IN._OPERATIONAL_SUBJECT_HINTS
        assert hint in lib_ingest._OPERATIONAL_SUBJECT_HINTS


def test_a_real_confirmation_is_still_not_operational():
    """The gate must not start eating the mail it exists to protect."""
    for subject in (
        CONFIRMATION,
        "MDOLX260769_ *NEW BOOKING CONFIRMATION // HILMAR - Oakland to Osaka - "
        "3X40'RF // CMA BKG # NAM8482648",
        "Oakland to Manila (North) / MDOLX261070 / ONE BKG # RICGAZ641400",
    ):
        assert not IN.is_operational_subject(subject), subject
