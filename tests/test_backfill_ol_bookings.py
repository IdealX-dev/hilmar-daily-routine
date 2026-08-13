"""Recovering wins from OL's booking recap, without inventing any.

Michael, 2026-08-12: "now use the report that was sent by linda with all the
bookings and match to the lonny requests since july 1 i assume and clean up."

15 of the 35 bookings in OL's operational export never reached the tracked
mailbox — the confirmations went To: Lonny, Cc: the group. They are real
wins that cannot be derived from mail we never received, so the evidence
comes from the recap.

The danger is obvious and this session has already lived it: a report full of
quotes that never existed. So the matcher must be conservative in every
direction — right port INCLUDING terminal, right order in time, never reusing
a request, never touching a row that is already won or already has a human
verdict. What it cannot match confidently, it reports.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import backfill_ol_bookings as B  # noqa: E402
import core  # noqa: E402


def _bk(mdolx, pod, date="2026-08-27", carrier="CMA CGM", bkg="NAM1"):
    return {"mdolx": mdolx, "pod": pod, "pol": "OAKLAND,CA",
            "carrier": carrier, "booking_no": bkg, "sheet_date": date}


def _req(rid, dest, ts, **kw):
    r = {"request_id": rid, "destination": dest, "lane": f"Oakland → {dest}",
         "request_timestamp": ts, "status": "LOSS"}
    r.update(kw)
    return r


def _p(bookings, requests, since="2026-07-01", max_age=60):
    return B.propose(bookings, requests, since, max_age, core)


def test_a_booking_matches_the_latest_prior_request_on_its_lane():
    m, un, sk = _p([_bk("261046", "YOKOHAMA,JAPAN")],
                   [_req("r_old", "Yokohama", "2026-07-05T15:00:00Z"),
                    _req("r_new", "Yokohama", "2026-08-05T15:00:00Z")])
    assert len(m) == 1 and not un
    assert m[0][2]["request_id"] == "r_new", (
        "matched an older ask when a newer one on the same lane was open — "
        "OL books against the most recent request")


def test_two_bookings_never_claim_the_same_request():
    """Nine Yokohama bookings landed the same week. One booking is one
    shipment, so nine bookings cannot all be the same ask."""
    m, un, _ = _p([_bk("261046", "YOKOHAMA,JAPAN"), _bk("261047", "YOKOHAMA,JAPAN")],
                  [_req("r1", "Yokohama", "2026-08-05T15:00:00Z")])
    assert len(m) == 1, "a request was claimed twice"
    assert len(un) == 1 and "no unclaimed" in un[0][2]


def test_a_request_after_the_booking_is_refused():
    """A booking cannot answer an ask that had not happened yet — the same
    impossible ordering QC-066 exists for."""
    m, un, _ = _p([_bk("261046", "YOKOHAMA,JAPAN", date="2026-07-10")],
                  [_req("r1", "Yokohama", "2026-08-05T15:00:00Z")])
    assert not m and len(un) == 1


def test_the_terminal_must_match_not_just_the_city():
    """Manila North is not Manila South. core.same_port is the pipeline's own
    predicate and the reason this is not a substring test."""
    m, un, _ = _p([_bk("261070", "MANILA NORTH HARBOUR")],
                  [_req("r1", "Manila (South)", "2026-08-05T15:00:00Z")])
    assert not m, "a booking was matched onto the wrong terminal"


def test_an_unmapped_port_is_reported_never_guessed():
    """Bare "MANILA" is the standing example and is deliberately absent from
    POD_TO_DESTINATION: North and South are different terminals, so an
    unqualified Manila cannot be resolved to either without guessing which
    lane a real booking belongs to."""
    m, un, _ = _p([_bk("261099", "MANILA")],
                  [_req("r1", "Manila (North)", "2026-08-05T15:00:00Z")])
    assert not m
    assert "POD_TO_DESTINATION" in un[0][2]


def test_rows_already_won_or_already_referenced_are_left_alone():
    m, un, sk = _p([_bk("261046", "YOKOHAMA,JAPAN")],
                   [_req("r_won", "Yokohama", "2026-08-05T15:00:00Z", status="WIN"),
                    _req("r_ref", "Yokohama", "2026-08-04T15:00:00Z",
                         mdolx_ref="260999")])
    assert not m, "the backfill overwrote a row that already had a booking"


def test_a_booking_the_tracker_already_has_is_skipped():
    m, un, sk = _p([_bk("260892", "YOKOHAMA,JAPAN")],
                   [_req("r1", "Yokohama", "2026-08-05T15:00:00Z",
                         mdolx_ref="260892", status="WIN")])
    assert not m and not un
    assert sk and sk[0][0] == "260892"


def test_the_since_floor_is_honoured():
    """Michael scoped this: "match to the lonny requests since july 1"."""
    m, un, _ = _p([_bk("261046", "YOKOHAMA,JAPAN")],
                  [_req("r_june", "Yokohama", "2026-06-05T15:00:00Z")])
    assert not m, "a pre-July request was claimed despite the since floor"


def test_a_stale_request_beyond_max_age_is_refused():
    m, un, _ = _p([_bk("261046", "YOKOHAMA,JAPAN", date="2026-08-27")],
                  [_req("r1", "Yokohama", "2026-07-01T15:00:00Z")], max_age=10)
    assert not m and "within 10d" in un[0][2]


def test_it_writes_nothing_without_apply():
    """Dry run is the default; --apply is the only writer, and it targets a
    version-controlled file so the change is reviewable and revertible."""
    src = (ROOT / "scripts" / "backfill_ol_bookings.py").read_text(encoding="utf-8")
    assert 'if not args.apply:' in src
    assert "DRY RUN" in src
    i = src.find("path.write_text")
    assert i > src.find("if not args.apply:"), "a write happens before the dry-run guard"


# ── --create-missing: a booking with no request behind it ────────────────

def _txn(mdolx="252071", pod="CAI MEP", pol="OAKLAND", carrier="ONE",
         date="2026-01-03", teu="2.0", **kw):
    b = {"mdolx": mdolx, "pod": pod, "pol": pol, "carrier": carrier,
         "sheet_date": date, "teu": teu, "booking_no": ""}
    b.update(kw)
    return b


def test_a_created_win_carries_the_lane_carrier_and_teu():
    """Michael 2026-08-13: "if it's a booking it's a win and yes the 54 that
    predate the tracker should be entered so we can see complete and total
    volumes booked on lanes". The TEU is the whole point — a win row with
    no volume adds a tally mark and no lane volume."""
    c = B.creation("252071", _txn())
    s = c["set"]
    assert c["create"] is True and c["request_id"] == "ol_252071"
    assert s["status"] == "WIN" and s["mdolx_ref"] == "252071"
    assert s["lane"] == "Oakland → HCMC (Cai Mep)"
    assert s["teu_requested"] == 2 and s["teu_won"] == 2
    assert s["carrier_won"] == "ONE"


def test_the_win_is_stamped_with_the_SAILING_date_not_today():
    """THE one that matters. ingest's create branch stamps the WIN
    transition with booking_timestamp, falling back to NOW. Without this
    field all 54 backfilled bookings would report as won TODAY — 54 phantom
    wins on one morning's report."""
    s = B.creation("252071", _txn(date="2026-01-03"))["set"]
    assert s["booking_timestamp"].startswith("2026-01-03")
    assert s["request_date"] == "2026-01-03"


def test_a_created_row_records_no_turnaround():
    """The dates are SAILING dates (Michael, 2026-08-13). Deriving 'request
    to quote' hours from a sail date is invented timing — the exact thing
    the same day's clock reset exists to stop."""
    s = B.creation("252071", _txn())["set"]
    assert "turnaround_biz_hours" not in s
    assert "turnaround_hours" not in s
    assert "response_timestamp" not in s


def test_the_note_says_where_the_win_came_from_and_that_the_date_is_a_sailing():
    note = B.creation("252071", _txn())["note"]
    assert "transaction report" in note
    assert "SAILING DATE" in note
    assert "252071" in note


def test_an_unmapped_port_keeps_ols_spelling_on_a_created_row():
    """label_for is lenient where destination_for is strict: nothing is
    being matched, so the lane just has to be readable and distinct.
    Collapsing unmapped ports to 'Unknown' would merge real lanes."""
    assert B.label_for("LYTTELTON") == "Lyttelton"
    assert B.label_for("PASIR GUDANG,MALAYSIA") == "Pasir Gudang"
    assert B.label_for("") == "Unknown"


def test_matching_still_refuses_the_port_that_labelling_accepts():
    """The leniency must not leak into the matcher, or a booking lands on
    someone else's request."""
    assert B.destination_for("MANILA") is None, (
        "bare MANILA resolved to a terminal — North and South are different "
        "ports and core.same_port depends on that")
    assert B.label_for("MANILA") == "Manila"


def test_the_transaction_reports_port_spellings_are_mapped():
    """The 2026 export drops the country from most ports; an unmapped POD
    is skipped by the matcher, so a missing spelling silently loses a
    match."""
    for pod, want in [("YOKOHAMA", "Yokohama"), ("CAI MEP", "HCMC (Cai Mep)"),
                      ("BUSAN", "Busan"), ("XINGANG", "Xingang"),
                      ("KAOHSIUNG,TAIWAN", "Kaohsiung")]:
        assert B.destination_for(pod) == want


def test_teu_survives_the_float_string_the_export_writes():
    assert B._teu({"teu": "8.0"}) == 8
    assert B._teu({"teu": ""}) == 0
    assert B._teu({}) == 0


def test_creating_is_opt_in():
    src = (ROOT / "scripts" / "backfill_ol_bookings.py").read_text(encoding="utf-8")
    assert "--create-missing" in src
    assert "if args.create_missing:" in src, (
        "created wins are not behind the flag")


def test_the_2026_export_is_the_default_recap():
    """Michael 2026-08-13: "ahhh my transaction report is better"."""
    src = (ROOT / "scripts" / "backfill_ol_bookings.py").read_text(encoding="utf-8")
    assert "ol-transaction-report-2026.json" in src


def test_the_committed_corrections_cannot_double_count_a_booking():
    """operator_corrections.json is now 73 entries, 51 of them created wins.
    One MDOLX appearing under two request_ids is two wins for one booking —
    the failure that would quietly inflate the win rate and every lane
    volume, and it cannot be seen by reading the file."""
    import json
    from collections import Counter
    doc = json.loads((ROOT / "scripts" / "operator_corrections.json"
                      ).read_text(encoding="utf-8"))
    cs = doc["corrections"]
    ids = Counter(c["request_id"] for c in cs)
    assert [k for k, v in ids.items() if v > 1] == []
    refs = Counter(str(c.get("set", {}).get("mdolx_ref") or "") for c in cs)
    refs.pop("", None)
    assert [k for k, v in refs.items() if v > 1] == [], "one booking, two wins"


def test_every_created_win_is_dated_to_its_sailing_not_to_today():
    """Without booking_timestamp, ingest stamps the WIN transition with NOW
    and core.win_event_date reports the whole backfill as won today."""
    import json
    doc = json.loads((ROOT / "scripts" / "operator_corrections.json"
                      ).read_text(encoding="utf-8"))
    created = [c for c in doc["corrections"] if c.get("create")]
    assert len(created) >= 50
    undated = [c["request_id"] for c in created
               if not c["set"].get("booking_timestamp")
               and c["request_id"] != "ol_261071"]
    assert undated == [], f"these would report as won today: {undated}"
    future = [c["request_id"] for c in created
              if (c["set"].get("booking_timestamp") or "") > "2026-08-13"]
    assert future == [], f"a win dated in the future: {future}"


def test_the_recap_file_is_stored_and_parseable():
    """The evidence behind these wins must live in the repo, not in a chat
    message — every correction's note points at it."""
    import json
    p = ROOT / "data" / "ol-booking-recap-2026-06-01_2026-08-12.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    assert len(rows) == 35
    assert all("mdolx" in r for r in rows)
