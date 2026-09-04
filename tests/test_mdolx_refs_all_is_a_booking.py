"""A booking reference is a booking reference, whichever field holds it.

scripts/core.decide_status read the primary scalar `mdolx_ref` only, while
src/hilmar/core.decide_status has always read the union of `mdolx_ref` and
`mdolx_refs_all` (src/hilmar/core.py:1381). Same row, two verdicts:

    has_send=True, mdolx_ref=None, mdolx_refs_all=['261031']
      production  LOSS / SEND_NO_BOOKING  "booking never confirmed"
      library     WIN                     "MDOLX booking confirmed"

tests/test_core_parity.py could not have caught it: production's signature
did not accept the argument at all, so the call was a TypeError rather than a
disagreement. That is why a structural signature test lives here too.

Production was the wrong side. booking_count (the counting rule) and
is_confirmed_win (what the client report renders) BOTH already read the
union — decide_status was the outlier, and it WRITES the status those two
then read. Worse, the contradiction erased itself: booking_count gates on the
stored status, so once qc_selfheal wrote LOSS the count fell 1 -> 0 and the
two agreed again with nothing left to detect.

This is NOT a return to the pre-Reading-B "has_send OR mdolx" rule that
produced phantom WINs for a month in 2026-05. Both signals are still
required; an empty mdolx_refs_all is still no booking. The negative controls
below are the guard, and must never be weakened to make a new case pass.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import core as prod  # noqa: E402
import ingest  # noqa: E402
import qc_selfheal as q  # noqa: E402

from hilmar import core as lib  # noqa: E402

_KW = dict(
    has_send=True,
    mdolx_ref=None,
    response_timestamp="2026-08-13T20:04:00+00:00",
    quoted=True,
    etd_fit_days=3,
    request_timestamp="2026-08-12T10:00:00+00:00",
)


def _won(tree, decision) -> bool:
    """Compare the SEMANTIC verdict, not the status string.

    Production writes LEGACY ("LOSS" + a quoted bool), the library writes
    STRICT ("Q&L"/"NQ"), and QC-041 forbids mixing them — so never compare
    `decision.status` across the trees directly. Production has the
    is_win/is_quoted_and_lost family CLAUDE.md says to route through; the
    library carries only its STRICT vocabulary constants, so use each tree's
    own idea of a win.
    """
    if hasattr(tree, "is_win"):
        return tree.is_win({"status": decision.status,
                            "quoted": decision.quoted})
    return decision.status == tree.STATUS_WIN


def test_both_trees_call_a_ref_in_the_list_a_booking():
    """THE divergence, pinned. Fails against the pre-fix tree with a
    TypeError, which is itself the finding."""
    p = prod.decide_status(**_KW, mdolx_refs_all=["261031"])
    l_ = lib.decide_status(**_KW, mdolx_refs_all=["261031"])
    assert _won(prod, p) is True, (
        "production called a row holding an OL booking reference a loss")
    assert _won(lib, l_) is True
    assert _won(prod, p) == _won(lib, l_)


def test_an_empty_list_is_still_not_a_booking():
    """NEGATIVE CONTROL for the 2026-05-30 regression. Loosening has_mdolx is
    a step toward the old `has_send OR mdolx` rule; this is the line that
    stops it going the rest of the way."""
    for refs in ([], None):
        p = prod.decide_status(**_KW, mdolx_refs_all=refs)
        l_ = lib.decide_status(**_KW, mdolx_refs_all=refs)
        assert _won(prod, p) is False, f"empty refs became a win: {refs!r}"
        assert _won(lib, l_) is False
        assert _won(prod, p) == _won(lib, l_)


def test_a_send_is_still_required():
    """BOTH signals, still. A booking with no Lonny send must not auto-win —
    that anomaly is held for ops review, per Reading B."""
    kw = dict(_KW, has_send=False)
    p = prod.decide_status(**kw, mdolx_refs_all=["261031"])
    l_ = lib.decide_status(**kw, mdolx_refs_all=["261031"])
    assert _won(prod, p) is False
    assert _won(lib, l_) is False


def test_the_two_signatures_accept_the_same_arguments():
    """STRUCTURAL. The parity suite compares BEHAVIOUR, so a parameter that
    exists in one tree and not the other is invisible to it — the call simply
    raises. Pin the signatures so the next divergence is caught at import."""
    pp = set(inspect.signature(prod.decide_status).parameters)
    lp = set(inspect.signature(lib.decide_status).parameters)
    assert "mdolx_refs_all" in pp, "production cannot even be asked the question"
    assert not (lp - pp), f"library-only parameters: {sorted(lp - pp)}"


# ── the end-to-end consequence ────────────────────────────────────────────
def _hazard_row() -> dict:
    return {
        "request_id": "req_hazard", "status": "WIN", "quoted": True,
        "has_send": True, "mdolx_ref": None, "mdolx_refs_all": ["261031"],
        "lane": "Oakland → Yokohama", "destination": "Yokohama",
        "origin": "Oakland", "teu_requested": 2, "teu_won": 2,
        "request_timestamp": "2026-08-12T10:00:00+00:00",
        "response_timestamp": "2026-08-13T20:04:00+00:00",
        "carrier_quoted": "HMM", "carrier_won": "HMM", "ol_rate": 2600,
    }


def test_the_qc_pass_no_longer_demotes_a_confirmed_booking():
    """qc_selfheal's decide loop runs TWICE per fire and, unlike
    ingest.age_requests, has no union-aware skip guard in front of it. It was
    the path that actually flipped these rows."""
    row = _hazard_row()
    data = {"version": "2", "requests": [row], "summary": {}}
    with contextlib.redirect_stdout(io.StringIO()):
        q.phase_3_entries(q.Log(), data)
    assert row["status"] == "WIN", (
        "the QC pass demoted a row that holds an OL booking reference")


def test_the_booking_count_does_not_erase_its_own_evidence():
    """booking_count gates on the STORED status, so a demotion took the count
    with it — 1 -> 0 — and the contradiction disappeared instead of being
    detectable. Both must survive the pass."""
    row = _hazard_row()
    assert prod.booking_count(row) == 1
    data = {"version": "2", "requests": [row], "summary": {}}
    with contextlib.redirect_stdout(io.StringIO()):
        q.phase_3_entries(q.Log(), data)
    assert prod.booking_count(row) == 1
    assert prod.is_confirmed_win(row) is True


# ── stop the shape being created at all ───────────────────────────────────
def test_prior_win_merge_always_leaves_a_primary_ref():
    """_merge_prior_win_into skips a FALSY prior value, so a prior win whose
    mdolx_ref is "" — or one already carrying its only ref in the list —
    merged into a row with mdolx_ref=None beside a populated
    mdolx_refs_all. Measured, both cases produced it."""
    for label, prior in (
        ("prior ref is an empty string",
         {"request_id": "r", "status": "WIN", "mdolx_ref": "",
          "mdolx_refs_all": ["261031"]}),
        ("prior is itself hazard-shaped",
         {"request_id": "r", "status": "WIN", "mdolx_ref": None,
          "mdolx_refs_all": ["261031"]}),
    ):
        existing = {"request_id": "r", "status": "PENDING",
                    "mdolx_ref": None, "mdolx_refs_all": []}
        ingest._merge_prior_win_into(existing, prior, "2026-09-01T00:00:00Z")
        assert existing["mdolx_ref"], (
            f"{label}: merge left a row holding a booking ref with no primary "
            f"-> {existing['mdolx_ref']!r} / {existing['mdolx_refs_all']!r}")
        assert existing["mdolx_ref"] in existing["mdolx_refs_all"]


def test_prior_win_merge_does_not_invent_a_ref():
    """The promotion may only pick a ref the row already holds."""
    existing = {"request_id": "r", "status": "PENDING",
                "mdolx_ref": None, "mdolx_refs_all": []}
    ingest._merge_prior_win_into(
        existing, {"request_id": "r", "status": "WIN"}, "2026-09-01T00:00:00Z")
    assert not existing.get("mdolx_ref")
