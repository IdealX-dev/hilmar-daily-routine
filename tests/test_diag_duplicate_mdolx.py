"""The QC-069 duplicate classifier, pinned to the shapes it was built from.

diag_duplicate_mdolx names WHICH mechanism put one MDOLX ref on two rows,
and a heal gets scoped from what it prints. A classifier that quietly
mislabels is worse than no classifier: the repo has already shipped one heal
against an inherited claim, and CLAUDE.md's rule since is that an inherited
claim is not a verified one.

So each branch is exercised against the shape MEASURED in the live data on
2026-09-03 (diag-bookings run 33783443620, diag-blob run 33720290524), not
against a shape invented to make the code pass:

  4a  req_1debac530d998acb  mdolx_ref='261029'  mdolx_refs_all=['261026']
      with correction {request_id: req_1debac530d998acb, mdolx_ref: 261029}
  4b  req_da035af71f7ec39d  mdolx_ref='261027'  beside stand_261027
      with correction {request_id: req_da035af71f7ec39d, mdolx_ref: 261027}

and the negative cases matter as much: a pair with NO correction naming the
ref must never be labelled (4), because (4)'s heal clears a field the other
mechanisms legitimately own.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import diag_duplicate_mdolx as D  # noqa: E402

SRC = (SCRIPTS / "diag_duplicate_mdolx.py").read_text(encoding="utf-8")


# ── _norm: one spelling for a ref, however it was stored ──────────────

def test_norm_collapses_every_spelling_a_writer_uses():
    # link_bookings_to_requests stores bare digits; corrections have been
    # written with the prefix; qc069 upper-cases. If these do not collapse
    # to one string, a (4) pair reads as (3) "two rows both claim it".
    assert D._norm("261029") == "261029"
    assert D._norm("MDOLX261029") == "261029"
    assert D._norm("mdolx 261029") == "261029"
    assert D._norm("0261029") == "261029"
    assert D._norm(" 261029 ") == "261029"


def test_norm_is_total_on_empty_input():
    for junk in (None, "", "   ", 0):
        assert D._norm(junk) == ""


def test_norm_never_returns_empty_for_an_all_zero_ref():
    # lstrip('0') on "000" is "" — the fallback keeps it addressable rather
    # than silently merging every zero-ish ref into one bucket.
    assert D._norm("000") == "000"


# ── 4a: the stale copy is a list entry ────────────────────────────────

def _pair_4a():
    rows = [
        {"request_id": "req_1debac530d998acb", "status": "WIN",
         "mdolx_ref": "261029", "mdolx_refs_all": ["261026"]},
        {"request_id": "req_3b1d82eaa1d6450f", "status": "WIN",
         "mdolx_ref": "261026", "mdolx_refs_all": ["261028"]},
    ]
    corr = [{"request_id": "req_1debac530d998acb",
             "source": "ol-booking-recap-2026-08-12"}]
    return rows, corr


def test_4a_is_named_when_a_correction_owns_one_row_and_a_list_holds_the_other():
    # 261026 is the ref that is genuinely on two rows in this shape: the
    # correction put it in req_3b1d82's mdolx_ref, and the matcher's earlier
    # assignment survives in req_1debac's mdolx_refs_all.
    rows, _ = _pair_4a()
    v = D._classify("261026", rows,
                    [{"request_id": "req_3b1d82eaa1d6450f",
                      "source": "ol-booking-recap-2026-08-12"}])
    assert v.startswith("(4a)"), v
    assert "req_3b1d82eaa1d6450f" in v
    assert "req_1debac530d998acb.mdolx_refs_all" in v


def test_a_ref_on_only_one_row_is_not_case_4_even_with_a_correction():
    # 261029 in the same fixture is on ONE row only. QC-069 would not flag it,
    # and if it is passed in anyway the classifier must not manufacture a
    # collision out of a correction that merely exists.
    rows, corr = _pair_4a()
    assert not D._classify("261029", rows, corr).startswith("(4")


def test_4a_names_the_field_a_heal_would_clear():
    # The label has to say WHERE the stale copy lives, because the fix for
    # 4a is "clear that list entry" and the fix for 4b is not.
    _, corr = _pair_4a()
    v = D._classify("261026", [
        {"request_id": "req_3b1d82eaa1d6450f", "mdolx_ref": "261026",
         "mdolx_refs_all": []},
        {"request_id": "req_1debac530d998acb", "mdolx_ref": "261029",
         "mdolx_refs_all": ["261026"]},
    ], [{"request_id": "req_3b1d82eaa1d6450f", "source": "recap"}])
    assert "mdolx_refs_all" in v


# ── 4b: the stale copy is a whole standalone WIN row ──────────────────

def test_4b_is_named_when_the_matcher_emitted_a_standalone_instead():
    rows = [
        {"request_id": "req_da035af71f7ec39d", "status": "WIN",
         "mdolx_ref": "261027", "mdolx_refs_all": []},
        {"request_id": "stand_261027", "status": "WIN",
         "mdolx_ref": "261027", "mdolx_refs_all": []},
    ]
    corr = [{"request_id": "req_da035af71f7ec39d", "source": "recap"}]
    v = D._classify("261027", rows, corr)
    assert v.startswith("(4b)"), v
    assert "stand_261027" in v


def test_4b_is_not_reported_as_4a_because_the_heals_differ():
    # 4a clears a field. 4b removes a WIN row, which is destructive and needs
    # a human. Collapsing them would license the destructive heal on the
    # evidence of the safe one.
    rows = [
        {"request_id": "req_da035af71f7ec39d", "mdolx_ref": "261027"},
        {"request_id": "stand_261027", "mdolx_ref": "261027"},
    ]
    v = D._classify("261027", rows,
                    [{"request_id": "req_da035af71f7ec39d", "source": "r"}])
    assert "(4a)" not in v


# ── the negatives: (4) must not swallow the other mechanisms ──────────

def test_no_correction_means_never_case_4():
    rows = [
        {"request_id": "req_a", "mdolx_ref": "261029", "mdolx_refs_all": []},
        {"request_id": "req_b", "mdolx_ref": "261030",
         "mdolx_refs_all": ["261029"]},
    ]
    v = D._classify("261029", rows, [])          # no correction names it
    assert not v.startswith("(4"), v


def test_a_correction_naming_a_row_that_does_not_hold_the_ref_is_not_case_4():
    # The correction points at req_z, but the ref sits on req_a/req_b. That is
    # not the collision — treating it as one would clear a field on evidence
    # that never mentioned these rows.
    rows = [
        {"request_id": "req_a", "mdolx_ref": "261029", "mdolx_refs_all": []},
        {"request_id": "req_b", "mdolx_ref": "261030",
         "mdolx_refs_all": ["261029"]},
    ]
    v = D._classify("261029", rows, [{"request_id": "req_z", "source": "r"}])
    assert not v.startswith("(4"), v


def test_carry_forward_still_reports_as_case_1():
    rows = [
        {"request_id": "req_a", "mdolx_ref": "261029",
         "preserved_from_prior": True},
        {"request_id": "req_b", "mdolx_ref": "261030",
         "mdolx_refs_all": ["261029"]},
    ]
    assert D._classify("261029", rows, []).startswith("(1)")


def test_an_unrecognised_shape_says_so_instead_of_guessing():
    rows = [
        {"request_id": "req_a", "mdolx_ref": "261029"},
        {"request_id": "req_b", "mdolx_ref": "261099"},
    ]
    assert D._classify("261029", rows, []).startswith("UNCLASSIFIED")


def test_a_finding_whose_rows_are_all_gone_says_so():
    assert "NO ROWS" in D._classify("261029", [], [])


# ── the script stays read-only ────────────────────────────────────────

def test_the_diagnostic_writes_nothing():
    # Same guard the other diag tests carry: a "read-only" script that grows
    # a write is how a diagnostic becomes a second writer of tracking data.
    tree = ast.parse(SRC)
    called = {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    for forbidden in ("push", "write_text", "upload_blob", "send", "unlink"):
        assert forbidden not in called, f"diag_duplicate_mdolx calls {forbidden}"


def test_corrections_are_read_from_the_repo_not_from_pulled_state():
    # The whole point of case (4) is comparing what the FILE says against
    # what the fire produced. Reading the correction file out of the pulled
    # blob would compare the fire to itself.
    fn = SRC.split("def _corrections_by_ref")[1].split("\ndef ")[0]
    assert 'ROOT / "scripts" / "operator_corrections.json"' in fn
    assert "tmp" not in fn


def test_every_source_file_is_utf8_clean():
    (SCRIPTS / "diag_duplicate_mdolx.py").read_text(encoding="utf-8")


def test_case_4b_catches_the_ol_backfill_shape_not_only_stand():
    # The 49 rows backfilled from OL's export carry an "ol_" prefix and no
    # RFQ chain either. A bare stand_ test would report this pair as
    # UNCLASSIFIED — the exact miss test_no_rfq_chain_predicate.py records
    # ("that was the SECOND surface to miss it").
    rows = [
        {"request_id": "req_da035af71f7ec39d", "mdolx_ref": "261027"},
        {"request_id": "ol_261027", "mdolx_ref": "261027"},
    ]
    v = D._classify("261027", rows,
                    [{"request_id": "req_da035af71f7ec39d", "source": "r"}])
    assert v.startswith("(4b)"), v
    assert "ol_261027" in v


def test_the_classifier_asks_core_rather_than_keeping_its_own_prefix_list():
    assert "has_no_rfq_chain" in SRC
    assert 'startswith("stand_")' not in SRC, (
        "a private copy of the prefix list — core owns it")
