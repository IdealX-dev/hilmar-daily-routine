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
        assert by_id[rid]["_booking_match_via"] == IN.OPERATOR_CORRECTION_VIA
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
    assert owner["_booking_match_via"] == IN.OPERATOR_CORRECTION_VIA
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
    assert ask["_booking_match_via"] != IN.OPERATOR_CORRECTION_VIA


def test_a_named_ref_whose_row_is_absent_still_becomes_a_standalone(corrections):
    corrections([_corr("req_gone", "261099")])
    bookings = {"261099": _confirmation("261099", "2026-08-13T20:04:00Z")}
    requests, standalones = IN.link_bookings_to_requests([], bookings)
    assert [r["request_id"] for r in standalones] == ["stand_261099"]


def test_a_missing_or_unreadable_corrections_file_consults_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(IN, "CORRECTIONS_PATH", tmp_path / "absent.json")
    assert IN._corrected_ref_owners() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(IN, "CORRECTIONS_PATH", bad)
    assert IN._corrected_ref_owners() == {}


# ── ONE predicate, three readers — proven by RUNNING them ───────────────
#
# The first version of this guard searched scripts/ingest.py for the string
# 'corr.get("exclude")' inside _corrected_ref_owners. It went red on the
# refactor that hoisted the predicate into the shared _named_ref (the very
# "one source, no re-spelling" it claimed to prove) and stayed green with the
# exclude gate deleted outright — review, 2026-09-05. A guard pins the
# PROPERTY: feed one corrections list to the consult, the applier and the
# claim, and assert they agree row for row.

#: Every shape the predicate must rule on, each named so a failure names it.
_SHAPES = [
    _corr("req_a", "261026"),                             # plain set: A owns 261026
    {"request_id": "req_x", "exclude": True,               # an exclude that CARRIES a ref —
     "set": {"mdolx_ref": "261050"}},                      #   the shape the gate defends
    {"request_id": "req_b", "set": {"status": "WIN"}},     # a set naming no ref
    {"set": {"mdolx_ref": "261060"}},                      # a ref naming no row
    _corr("req_c", "261070"),                             # two corrections, one ref:
    _corr("req_d", "261070"),                             #   the FIRST wins
]


def _rows(pairs):
    """Rows built through build_requests (the production shape), one per
    (request_id, ref); a ref, when given, is held verbatim as a WIN."""
    out = []
    for i, (rid, ref) in enumerate(pairs):
        (r,) = IN.build_requests([_staged_rfq(f"<{rid}>", f"2026-08-{1 + i:02d}T16:00:00Z")])
        r["request_id"] = rid
        if ref:
            r["mdolx_ref"], r["mdolx_refs_all"], r["status"] = ref, [ref], "WIN"
        out.append(r)
    return out


def _holders(rows, ref):
    return sorted(r["request_id"] for r in rows if IN._row_holds(r, ref))


def test_the_consult_names_exactly_the_refs_a_set_correction_carries(corrections):
    corrections(_SHAPES)
    assert IN._corrected_ref_owners() == {"261026": "req_a", "261070": "req_c"}


def test_an_exclude_correction_that_carries_a_ref_names_no_owner(corrections):
    """The shipped fixture had no `set`, so it was dropped for emptiness
    before the exclude gate was ever consulted and passed with the gate
    deleted. This one carries the ref the gate must refuse."""
    excl = {"request_id": "req_x", "exclude": True, "set": {"mdolx_ref": "261050"}}
    corrections([excl])
    assert IN._corrected_ref_owners() == {}
    assert IN._named_ref(excl) is None
    assert IN._named_ref({"request_id": "req_x", "set": {"mdolx_ref": " 261050 "}}) == "261050"
    assert IN._named_ref({"request_id": "req_x", "set": {"status": "WIN"}}) is None
    assert IN._named_ref({"request_id": "req_x", "exclude": True}) is None


def test_the_three_callers_agree_about_which_row_owns_each_ref(corrections):
    """One list, three readers. The consult decides where the WRITER places a
    booking; the applier decides what it STAMPS; the claim decides what it
    RESOLVES. They must name the same owner for every ref and the same
    non-owners. Deleting the exclude gate from _named_ref turns this red in
    the consult and the claim at once."""
    corrections(_SHAPES)
    owners = IN._corrected_ref_owners()

    # THE WRITER — one booking per candidate ref, every row on the same lane.
    rows = _rows([(rid, None) for rid in ("req_a", "req_x", "req_b", "req_c", "req_d", "req_e")])
    bookings = {ref: _confirmation(ref, STAGED.format(4 + 2 * i))
                for i, ref in enumerate(("261026", "261050", "261060", "261070"))}
    requests, standalones = IN.link_bookings_to_requests(rows, bookings)
    assert standalones == []
    placed = {r["mdolx_ref"]: r["request_id"] for r in requests
              if r.get("_booking_match_via") == IN.OPERATOR_CORRECTION_VIA}
    assert placed == owners == {"261026": "req_a", "261070": "req_c"}
    scored = {r["mdolx_ref"] for r in requests
              if r.get("mdolx_ref") and r.get("_booking_match_via") != IN.OPERATOR_CORRECTION_VIA}
    assert scored == {"261050", "261060"}, "an excluded or row-less ref was placed as if named"

    # THE APPLIER — removes the excluded row, stamps the named rows, writes
    # nothing for a set naming no ref or a ref naming no row. It stamps BOTH
    # rows two corrections name: that is the contradiction the claim resolves.
    rows = _rows([(rid, None) for rid in ("req_a", "req_x", "req_b", "req_c", "req_d")])
    IN.apply_operator_corrections(rows)
    by_id = {r["request_id"]: r for r in rows}
    assert "req_x" not in by_id
    assert by_id["req_a"]["mdolx_ref"] == "261026"
    assert not by_id["req_b"].get("mdolx_ref")
    assert not any(r.get("mdolx_ref") == "261060" for r in rows)
    assert by_id["req_c"]["mdolx_ref"] == by_id["req_d"]["mdolx_ref"] == "261070"

    # THE CLAIM — acts on exactly the refs the consult names, keeps the
    # consult's owner, and leaves the holders of an excluded ref alone.
    rows = _rows([("req_a", "261026"), ("req_rival_a", "261026"),
                  ("req_x", "261050"), ("req_rival_x", "261050"),
                  ("req_c", "261070"), ("req_d", "261070")])
    acted, released = IN.claim_corrected_mdolx_refs(rows)
    assert _holders(rows, "261026") == ["req_a"]
    assert _holders(rows, "261070") == ["req_c"]
    assert _holders(rows, "261050") == ["req_rival_x", "req_x"], "the claim acted on an excluded ref"
    assert acted == 2 and sorted(r["request_id"] for r in released) == ["req_d", "req_rival_a"]


def test_the_first_of_two_corrections_naming_one_ref_wins_at_the_writer_and_after_the_claim(corrections):
    """Two corrections, one ref, two rows: a contradiction in the file. The
    consult takes the FIRST (setdefault); the claim walks the file in order
    and the first owner holding the ref demotes the other. Both must land on
    the same row, or the writer places a booking the backstop then moves —
    the two-writers-disagree mechanism, re-created inside the fix.
    Last-wins at the consult (`owners[ref] = rid`) turns this red."""
    corrections([_corr("req_first", "261099"), _corr("req_second", "261099")])
    (first,) = IN.build_requests([_staged_rfq("<1>", "2026-08-01T16:00:00Z")])
    first["request_id"] = "req_first"
    (second,) = IN.build_requests([_staged_rfq("<2>", "2026-08-04T16:00:00Z")])
    second["request_id"] = "req_second"          # the later ask: the tiebreak's own pick
    bookings = {"261099": _confirmation("261099", "2026-08-13T20:04:00Z")}
    requests, standalones = IN.link_bookings_to_requests([first, second], bookings)
    assert standalones == []
    assert first["mdolx_ref"] == "261099"
    assert first["_booking_match_via"] == IN.OPERATOR_CORRECTION_VIA
    assert not second.get("mdolx_ref"), "the second correction overruled the first at the writer"
    rows = requests + standalones
    IN.age_requests(rows)
    IN.apply_operator_corrections(rows)          # stamps both — the contradiction
    IN.claim_corrected_mdolx_refs(rows)          # resolves it in file order
    assert _holders(rows, "261099") == ["req_first"]


# ── the receipt is a composition, and it is RUN ─────────────────────────

def test_the_receipt_reports_how_many_bookings_the_operator_placed(corrections):
    """The commit named this log line as the production verification, and
    its count compared a re-typed literal against the writer's stamp — a
    receipt that could report 0 forever while the fix worked (measured:
    changing the reader's literal alone left the whole suite green). The
    composer and the writer now read ONE constant, and the composition is
    run here over the live batch."""
    corrections([_corr(*t) for t in YOKOHAMA])
    asks, bookings = _live_fixture()
    requests, standalones = IN.link_bookings_to_requests(asks, bookings)
    assert IN.link_receipt(requests, bookings, standalones) == (
        "Linked 10/10 bookings to requests; 0 standalone wins; "
        "10 placed on the row an operator correction names, before scoring")


def test_the_receipt_is_silent_about_the_operator_when_nothing_was_placed(corrections):
    corrections([])
    asks, bookings = _live_fixture()
    requests, standalones = IN.link_bookings_to_requests(asks, bookings)
    line = IN.link_receipt(requests, bookings, standalones)
    wins = sum(1 for r in requests if r["status"] == "WIN")
    assert line == f"Linked {wins}/10 bookings to requests; {len(standalones)} standalone wins"


def test_ingest_main_prints_the_receipt(corrections, tmp_path, monkeypatch, capsys):
    """main() end to end over the live shape — staged RFQs and confirmations
    in, the receipt line out — so the print in main() is exercised, not just
    the composer. request_ids are whatever build_requests derives from the
    staged rows, so the corrections are written against THOSE."""
    rfqs = [_staged_rfq(f"<rfq-{i}>", f"2026-08-{i:02d}T16:00:00Z") for i in range(1, 11)]
    ids = [r["request_id"] for r in IN.build_requests(rfqs)]
    assert len(set(ids)) == 10
    refs = [ref for _, ref, _ in YOKOHAMA]
    corrections([_corr(rid, ref) for rid, ref in zip(ids, refs, strict=True)])
    confs = [_staged(_confirmation(ref, None)["subject"], STAGED.format(4 + 2 * i), f"<bk-{ref}>")
             for i, ref in enumerate(refs)]
    monkeypatch.setattr(IN, "load_stage", lambda: rfqs + confs)
    monkeypatch.setattr(IN, "OUT_PATH_DEFAULT", tmp_path / "tracking-data-v2.json")
    monkeypatch.setattr(sys, "argv", ["ingest"])
    assert IN.main() == 0
    out = capsys.readouterr().out
    assert ("Linked 10/10 bookings to requests; 0 standalone wins; "
            "10 placed on the row an operator correction names, before scoring") in out
    assert "MDOLX claim:" not in out, "the backstop found work on a fresh build"
    written = json.loads((tmp_path / "tracking-data-v2.json").read_text(encoding="utf-8"))
    assert _dupes(written["requests"]) == []
    assert sorted(r["mdolx_ref"] for r in written["requests"]) == sorted(refs)


# ── a named booking with no clock still lands on the operator's row ─────

@pytest.mark.parametrize("sent", [None, "", "not a timestamp"])
def test_an_undated_named_booking_still_lands_on_the_operators_row(corrections, sent):
    """`if not bk_ts and best is None: continue` — the carve-out, pinned.
    Without it an undated booking is skipped by the matcher and falls through
    to the standalone loop, which has NO timestamp guard: a `stand_<ref>`
    WIN beside the operator's row, which the applier then stamps — the exact
    duplicate this fix exists to prevent, produced by a missing date.
    Reverting to `if not bk_ts: continue` turns this red.

    What the writer stores for a booking with no clock: booking_timestamp
    and the history entry's `at` carry what the stage said, VERBATIM — None,
    or a string nothing can parse — never a substitute date. That is the
    shape the standalone path has always written for an undated booking
    (`"booking_timestamp": bk.get("sent")`, `"at": bk_ts_iso`), and every
    reader parses and falls back: core.win_event_date lands on the ask's own
    date, QC-066 and QC-072 stay quiet, gen_email's STATUS CHANGES skips it.
    The applier that follows supplies the operator's clock when the
    correction carries one."""
    corrections([_corr("req_owner", "261031")])
    (owner,) = IN.build_requests([_staged_rfq("<o>", "2026-07-30T18:00:00Z")])
    owner["request_id"] = "req_owner"
    (rival,) = IN.build_requests([_staged_rfq("<r>", "2026-08-01T16:00:00Z")])
    rival["request_id"] = "req_rival"
    requests, standalones = IN.link_bookings_to_requests(
        [owner, rival], {"261031": _confirmation("261031", sent)})
    assert standalones == [], "an undated named booking became a stand_ row beside its owner"
    assert owner["mdolx_ref"] == "261031"
    assert owner["_booking_match_via"] == IN.OPERATOR_CORRECTION_VIA
    assert owner["status"] == "WIN" and owner["teu_won"] == owner["teu_requested"] == 2
    assert owner["booking_timestamp"] == sent and C.parse_iso(owner["booking_timestamp"]) is None
    assert owner.get("olusa_time_et") is None
    assert owner["status_history"] == [{"at": sent, "from": "PENDING", "to": "WIN",
                                        "reason": "MDOLX261031 booking confirmed"}]
    assert not rival.get("mdolx_ref") and rival["status"] == "PENDING"
    assert _dupes(requests) == []
    # Readers of a None `at` fall back; none fails.
    assert C.win_event_date(owner) == owner["request_date"] == "2026-07-30"
    assert QC.qc066_impossible_states(requests) == []
    assert QC.qc072_history_contradicts_status(requests) == []
    # The applier supplies the operator's clock, and the win is dated by it.
    corrections([_corr("req_owner", "261031", "2026-08-03T22:15:00Z")])
    IN.apply_operator_corrections(requests)
    assert owner["booking_timestamp"] == "2026-08-03T22:15:00Z"
    assert C.win_event_date(owner) == "2026-08-03"


# ── a correction spelled unlike the booking's key is REPORTED ───────────

def _refs_of(r):
    """The distinct spellings a row holds — mdolx_ref echoes into
    mdolx_refs_all by design (ingest unions them), so this is a set."""
    return {x for x in [r.get("mdolx_ref"), *(r.get("mdolx_refs_all") or [])] if x}


def test_a_zero_padded_correction_is_reported_by_qc069_not_hidden(corrections):
    """The reviewer's counter-case, pinned. A correction spelled "0261026"
    for a booking keyed "261026": the consult compares spellings and misses,
    the rival takes the booking by score, the applier writes "0261026" onto
    the owner verbatim — one shipment on two rows — and QC-069's old
    `.strip().upper()` key called those two different refs. Measured silent.
    The check now keys on ingest.mdolx_identity and names both rows."""
    corrections([_corr("req_owner", "0261026")])
    (owner,) = IN.build_requests([_staged_rfq("<o>", "2026-08-01T16:00:00Z")])
    owner["request_id"] = "req_owner"
    (rival,) = IN.build_requests([_staged_rfq("<r>", "2026-08-04T16:00:00Z")])
    rival["request_id"] = "req_rival"
    rows = _run_both_writers([owner, rival],
                             {"261026": _confirmation("261026", "2026-08-13T20:04:00Z")})
    # The writers' identity is the spelling — stated, so the pair exists:
    assert owner["mdolx_ref"] == "0261026" and rival["mdolx_ref"] == "261026"
    # — and the check reports it under the collapsed ref, both rows named.
    assert _dupes(rows) == [("duplicate_mdolx", "261026", ["req_owner", "req_rival"])]
    # It is a PAIR OF ROWS, never two spellings on ONE row. That is the shape
    # a zero-collapsing consult produces (measured 2026-09-05: the owner
    # ended mdolx_ref="0261026" + mdolx_refs_all=["261026"], booking_count 2
    # on one row, the rival emptied, QC-069 silent). core.booking_count
    # unions the two fields verbatim, so that row counts one shipment twice
    # with nothing to report it. A collapsing consult turns this red.
    for r in rows:
        assert len({IN.mdolx_identity(x) for x in _refs_of(r)}) == len(_refs_of(r)), (
            f"{r['request_id']} carries two spellings of one ref: {_refs_of(r)}")
    assert [C.booking_count(r) for r in rows] == [1, 1]


def test_mdolx_identity_is_the_create_dedups_spelling_and_qc069s_key():
    assert (IN.mdolx_identity("0261026") == IN.mdolx_identity("261026")
            == IN.mdolx_identity(" 261026 ") == "261026")
    assert IN.mdolx_identity("000") == "000"
    assert IN.mdolx_identity(None) == ""


def test_the_create_branch_still_dedups_across_a_leading_zero(corrections):
    """apply_operator_corrections' `create` branch used two inline
    `.lstrip("0")`; both now read mdolx_identity. Behaviour-identical, pinned:
    a created "0261026" is skipped when a row already holds "261026"."""
    corrections([{"request_id": "req_new", "create": True,
                  "set": {"status": "WIN", "mdolx_ref": "0261026",
                          "booking_timestamp": "2026-08-03T22:00:00Z"}}])
    rows = _rows([("req_have", "261026")])
    IN.apply_operator_corrections(rows)
    assert [r["request_id"] for r in rows] == ["req_have"]


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
