"""A booking recovered from OL's export is not an undated quote.

Michael, 2026-08-13, on the report banner reading "70 further quotes are
recorded with a rate or carrier but no response time": "this is absurd and
you should have this fixed since you have the entire booking report and we
should be clean."

49 of those 70 were the bookings backfilled from OL's own export that
morning. They carry carrier_quoted (OL's export names the carrier) and can
never carry a response_timestamp, because there was never a quote — there
was never an email. QC-077 excluded rows whose request_id starts with
"stand_" and did not know about the "ol_" prefix the backfill introduced.

That was the SECOND surface to miss it. The first blocked a fire: QC-039
graded the same 49 rows on a rate they cannot have. Two misses of one fact
is a missing abstraction, so the prefixes now live once in core and every
exclusion site asks core.

The exception matters as much as the rule and is tested below: qc_selfheal's
scope purge drops a stand_ row whose SUBJECT does not say HILMAR, and an ol_
row has no subject at all — adopting the shared predicate there would delete
all 49 recovered wins.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import core as C  # noqa: E402


def test_both_prefixes_are_recognised():
    assert C.has_no_rfq_chain("stand_260905") is True
    assert C.has_no_rfq_chain("ol_252071") is True


def test_an_ordinary_request_is_not():
    """The predicate must never widen to rows that DO have a chain — those
    are exactly where a real parser miss or a real undated quote shows up."""
    assert C.has_no_rfq_chain("req_c499ccd17e8763ff") is False
    assert C.has_no_rfq_chain("") is False
    assert C.has_no_rfq_chain(None) is False


def test_it_takes_a_row_or_a_bare_id():
    assert C.has_no_rfq_chain({"request_id": "ol_252071"}) is True
    assert C.has_no_rfq_chain({"request_id": "req_x"}) is False
    assert C.has_no_rfq_chain({}) is False


def test_the_two_cores_agree():
    from hilmar import core as HC
    assert C.NO_RFQ_CHAIN_PREFIXES == HC.NO_RFQ_CHAIN_PREFIXES
    for rid in ("stand_1", "ol_1", "req_1", ""):
        assert C.has_no_rfq_chain(rid) == HC.has_no_rfq_chain(rid)


def test_parser_accuracy_asks_core_rather_than_keeping_a_copy():
    """Two copies of one prefix list is how the second surface got it wrong."""
    src = (ROOT / "src" / "hilmar" / "parser_accuracy.py").read_text(encoding="utf-8")
    assert "_NO_CHAIN_PREFIXES" not in src, "a second copy of the prefixes"
    assert "has_no_rfq_chain" in src


def test_qc077_no_longer_counts_a_backfilled_booking():
    """THE banner. A backfilled booking carries carrier_quoted from OL's
    export and no response time; it is not a quote that failed to be dated."""
    import qc_selfheal  # noqa: F401  (import guards against a syntax slip)
    src = (ROOT / "scripts" / "qc_selfheal.py").read_text(encoding="utf-8")
    i = src.find("_q_nots = [")
    assert i > 0, "the QC-077 population moved — re-point this test"
    block = src[i:i + 400]
    assert "has_no_rfq_chain" in block, (
        "QC-077 is back to a bare stand_ check and will count the 49 "
        "backfilled bookings as undated quotes again")


def test_the_scope_purge_deliberately_does_not_use_the_predicate():
    """The one site that must NOT adopt it, guarded by a comment and by
    this test: an ol_ row has no subject, so the HILMAR check fails for it
    by construction and the purge would delete every recovered win."""
    src = (ROOT / "scripts" / "qc_selfheal.py").read_text(encoding="utf-8")
    i = src.find('"HILMAR" not in subj_up')
    assert i > 0
    context = src[max(0, i - 700):i]
    assert "DELIBERATELY a bare stand_ check" in context, (
        "the scope purge lost the comment explaining why it must stay a "
        "bare stand_ check")
    assert 'rid.startswith("stand_") and "HILMAR" not in subj_up' in src


def test_no_bare_stand_check_survives_outside_the_two_known_sites():
    """A new bare startswith("stand_") is the bug recurring. Two are
    intentional: the scope purge above, and ingest's healer which SELECTS
    stand_ rows rather than excluding them."""
    hits = []
    for f in sorted((ROOT / "scripts").glob("*.py")) + \
             sorted((ROOT / "src" / "hilmar").glob("*.py")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if 'startswith("stand_")' in line and not line.lstrip().startswith("#"):
                hits.append(f"{f.name}:{n}")
    # Line numbers shift when code is inserted above these sites; the pin is
    # the FILE-AND-COUNT, the exact line is informative. 2026-08-14: the
    # qc_selfheal site moved 938 → 1033 when _stamp_response_from_dated_sibling
    # was added above it. Same two deliberate sites, nothing new.
    assert sorted(h.split(":")[0] for h in hits) == ["ingest.py", "qc_selfheal.py"], hits
    assert len(hits) == 2, hits
