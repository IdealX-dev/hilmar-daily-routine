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


# ── case (4) reports the SET of shapes, not the first match ───────────

def _pair_4a():
    rows = [
        {"request_id": "req_1debac530d998acb", "status": "WIN",
         "mdolx_ref": "261029", "mdolx_refs_all": ["261026"]},
        {"request_id": "req_3b1d82eaa1d6450f", "status": "WIN",
         "mdolx_ref": "261026", "mdolx_refs_all": ["261028"]},
    ]
    corr = [{"request_id": "req_3b1d82eaa1d6450f",
             "source": "ol-booking-recap-2026-08-12"}]
    return rows, corr


def test_a_bare_stale_list_entry_reports_4a_and_says_it_is_exclusive():
    # 261026: the correction put it in req_3b1d82's mdolx_ref, and the
    # matcher's earlier assignment survives in req_1debac's mdolx_refs_all.
    rows, corr = _pair_4a()
    v = D._classify("261026", rows, corr)
    assert "(4a)" in v
    assert "req_1debac530d998acb.mdolx_refs_all" in v
    assert "EXCLUSIVELY 4a" in v, v


def test_a_ref_on_only_one_row_is_not_case_4_even_with_a_correction():
    # 261029 in the same fixture is on ONE row only. QC-069 would not flag it,
    # and if it is passed in anyway the classifier must not manufacture a
    # collision out of a correction that merely exists.
    rows, corr = _pair_4a()
    assert not D._classify("261029", rows, corr).startswith("(4")


def test_an_orphan_standalone_reports_4b_and_is_never_called_exclusive():
    rows = [
        {"request_id": "req_da035af71f7ec39d", "status": "WIN",
         "mdolx_ref": "261027", "mdolx_refs_all": []},
        {"request_id": "stand_261027", "status": "WIN",
         "mdolx_ref": "261027", "mdolx_refs_all": ["261027"]},
    ]
    v = D._classify("261027", rows,
                    [{"request_id": "req_da035af71f7ec39d", "source": "r"}])
    assert "(4b)" in v and "stand_261027" in v
    assert "NOT exclusively 4a" in v, v


def test_a_rival_row_claiming_it_outright_reports_4c():
    # MDOLX261031, live 2026-09-03.
    rows = [
        {"request_id": "req_b789e573316ead86", "status": "WIN",
         "mdolx_ref": "261031", "mdolx_refs_all": []},
        {"request_id": "req_f942b9672ff756ab", "status": "WIN",
         "mdolx_ref": "261031", "mdolx_refs_all": ["261031"]},
    ]
    v = D._classify("261031", rows,
                    [{"request_id": "req_b789e573316ead86", "source": "r"}])
    assert "(4c)" in v and "req_f942b9672ff756ab.mdolx_ref" in v
    assert "NOT exclusively 4a" in v, v


# THE REGRESSION THIS SECTION EXISTS FOR. Until 2026-09-03 _classify was a
# priority chain: it computed `stands` and `rivals`, returned on the 4a branch
# BEFORE testing either, and the label named neither. Verified by running it —
# all three of the row-sets below returned the identical "(4a) ... stale list
# entry" string. So "5 cases of 4a" was not a claim the tool could support, and
# a heal scoped from it would have cleared a list entry, moved the headline win
# count, and left QC-069 firing on the same ref.

def test_4a_beside_a_standalone_reports_BOTH_and_refuses_the_exclusive_label():
    rows = [
        {"request_id": "req_A", "mdolx_ref": "261029"},
        {"request_id": "req_W", "mdolx_ref": "261099",
         "mdolx_refs_all": ["261029"]},
        {"request_id": "stand_261029", "mdolx_ref": "261029"},
    ]
    v = D._classify("261029", rows, [{"request_id": "req_A", "source": "r"}])
    assert "(4a)" in v, v
    assert "(4b)" in v and "stand_261029" in v, "the standalone went unnamed"
    assert "NOT exclusively 4a" in v, v


def test_4a_beside_a_rival_reports_BOTH_and_refuses_the_exclusive_label():
    rows = [
        {"request_id": "req_A", "mdolx_ref": "261029"},
        {"request_id": "req_W", "mdolx_ref": "261099",
         "mdolx_refs_all": ["261029"]},
        {"request_id": "req_R", "mdolx_ref": "261029"},
    ]
    v = D._classify("261029", rows, [{"request_id": "req_A", "source": "r"}])
    assert "(4a)" in v, v
    assert "(4c)" in v and "req_R.mdolx_ref" in v, "the rival went unnamed"
    assert "NOT exclusively 4a" in v, v


def test_the_exclusive_marker_is_the_only_thing_a_heal_may_gate_on():
    # A heal that clears a list entry is correct ONLY when nothing else claims
    # the ref. Every non-exclusive shape must say so in the same words, so the
    # gate is a substring test and not a parse of the shape list.
    rows, corr = _pair_4a()
    assert "EXCLUSIVELY 4a" in D._classify("261026", rows, corr)
    for rows in (
        [{"request_id": "req_A", "mdolx_ref": "1"},
         {"request_id": "stand_1", "mdolx_ref": "1"}],
        [{"request_id": "req_A", "mdolx_ref": "1"},
         {"request_id": "req_R", "mdolx_ref": "1"}],
        [{"request_id": "req_A", "mdolx_ref": "1"},
         {"request_id": "req_W", "mdolx_ref": "2", "mdolx_refs_all": ["1"]},
         {"request_id": "stand_1", "mdolx_ref": "1"}],
    ):
        v = D._classify("1", rows, [{"request_id": "req_A", "source": "r"}])
        assert "NOT exclusively 4a" in v, v


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
    # RFQ chain either. A bare stand_ test would miss this — the exact miss
    # test_no_rfq_chain_predicate.py records ("the SECOND surface to miss it").
    rows = [
        {"request_id": "req_da035af71f7ec39d", "mdolx_ref": "261027"},
        {"request_id": "ol_261027", "mdolx_ref": "261027"},
    ]
    v = D._classify("261027", rows,
                    [{"request_id": "req_da035af71f7ec39d", "source": "r"}])
    assert "(4b)" in v and "ol_261027" in v, v


def test_the_classifier_asks_core_rather_than_keeping_its_own_prefix_list():
    assert "has_no_rfq_chain" in SRC
    assert 'startswith("stand_")' not in SRC, (
        "a private copy of the prefix list — core owns it")


def test_3_survives_when_no_correction_is_involved():
    # The (3) bucket still has to exist: two rows claiming one ref with NO
    # operator verdict behind either is genuinely ambiguous, and case (4)'s
    # "defer to the operator's row" has nothing to defer to.
    rows = [
        {"request_id": "req_a", "mdolx_ref": "261031"},
        {"request_id": "req_b", "mdolx_ref": "261031"},
    ]
    assert D._classify("261031", rows, []).startswith("(3)")


def test_the_docstring_records_that_refs_all_is_a_pdf_join_key():
    # The heal that nearly shipped guarded only on "the row keeps a ref".
    # patch_carriers joins on mdolx_refs_all to find a booking PDF, and that
    # PDF supplies pod -> destination/lane; a row that loses its lane is
    # dropped from every client bucket by gen_client_email._lane_resolved.
    # The next reader must not have to rediscover that.
    assert "patch_carriers" in SRC and "_lane_resolved" in SRC
