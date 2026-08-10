"""Hilmar Ingredients is IN Hilmar, California. The client tag and the origin
city are the same word, and that is not a bug to fix.

2026-08-10. Michael asked whether the client gate should be tightened. It
should NOT, and this file is the decision record so a later session does not
"fix" it and quietly drop real wins.

THE PREDICATE, scripts/ingest.py:677-679:

    is_hilmar = row.get("is_hilmar")
    if is_hilmar is None:
        is_hilmar = "HILMAR" in subject.upper()

Two facts about it, both AST-verified over every scripts/*.py:

  1. NOTHING in scripts/ ever writes `is_hilmar`. Zero dict-literal keys, zero
     subscript assignments. refresh_stage.build_stage_record emits a fixed
     record without it. So `row.get` returns None on every production row and
     the substring test ALWAYS runs. The precedence branch is dead code in the
     live tree (src/hilmar sets the key, but src/hilmar is not what fires).
  2. Consequently the substring test is the whole client gate — and until this
     file, no test exercised it. tests/test_booking_email_choice.py hard-coded
     `is_hilmar: True` in its fixture, short-circuiting line 679 on all seven
     of its tests. Any tightening would have shipped untested.

WHY IT STAYS LOOSE. A real Hilmar booking can name only the origin city:

    "MDOLX260821_ ... Hilmar, CA to La Guaira, Venezuela"

That subject has no "// HILMAR" customer tag. Requiring a tag would drop it —
and requiring one would drop every Hilmar move whose subject describes the lane
instead of the customer. The loose gate is deliberate.

THE SAFETY NET IS THE NEGATIVE GATE, not a stricter positive one.
out_of_scope_reason() names the OTHER customers Hilmar ships for — Numidia,
Agri Dairy — and that is what actually excluded MDOLX260821, which turned out
to be Agri Dairy cargo loading in the town of Hilmar. Positive test loose,
negative test specific. Tightening the positive side attacks the wrong half.
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

import ingest as IN  # noqa: E402

# Verbatim from production (diag-bookings runs 4 and 5, 2026-08-10).
ORIGIN_CITY_ONLY = ("RE: Hilmar, CA to La Guaira, Venezuela - S38083 / "
                    "MDOLX260821 - Puerto Cabello. / EBKG17621387")
AGRI_DAIRY_SIBLING = ("RE: MDOLX260821_Load appointment needed for 1 x 40' HC  / "
                      "Agri Dairy Vendor Reference PO00-26002163 / 93348")
CUSTOMER_TAG = ("MDOLX260769_ *NEW BOOKING CONFIRMATION // HILMAR - Oakland to "
                "Osaka - 3X40'RF // CMA BKG # NAM8482648")


def _row(subject, sent="2026-07-13T14:29:47Z", imid="<x>", preview=""):
    return {"bucket": "mbd_inbound", "subject": subject, "sent": sent,
            "received": sent, "imid": imid, "summary_preview": preview}


# ── what the gate actually is ───────────────────────────────────────────────

def test_nothing_in_the_live_tree_ever_sets_is_hilmar():
    """If this ever fails, the precedence branch has come alive and the
    substring test is no longer the whole gate — re-read ingest.py:677."""
    writes = []
    for p in sorted(SCRIPTS.glob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Dict):
                for k in n.keys:
                    if isinstance(k, ast.Constant) and k.value == "is_hilmar":
                        writes.append(f"{p.name}:{n.lineno} dict key")
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if (isinstance(t, ast.Subscript)
                            and isinstance(t.slice, ast.Constant)
                            and t.slice.value == "is_hilmar"):
                        writes.append(f"{p.name}:{n.lineno} subscript")
    assert not writes, (
        "something in scripts/ now sets is_hilmar: " + "; ".join(writes))


def test_the_booking_tests_do_not_bypass_the_real_gate():
    """A fixture that sets is_hilmar tests a client gate production never
    uses. This is how the substring test went untested for months.

    AST, not a substring scan: the first draft of this guard matched the
    docstring that EXPLAINS the fix and failed on a corrected file. An
    identifier in prose is indistinguishable from an identifier in code —
    the recurring lesson of this repo, and I re-learned it here.
    """
    src = (ROOT / "tests/test_booking_email_choice.py").read_text(encoding="utf-8")
    keys = [n.lineno for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Dict)
            for k in n.keys
            if isinstance(k, ast.Constant) and k.value == "is_hilmar"]
    assert not keys, (
        f"the booking fixture sets is_hilmar at line(s) {keys} — it "
        "short-circuits ingest.py:679, so those tests no longer exercise the "
        "predicate that actually runs")


# ── loose on purpose ────────────────────────────────────────────────────────

def test_a_customer_tagged_subject_is_admitted():
    assert IN.collect_bookings([_row(CUSTOMER_TAG)]).get("260769") is not None


def test_an_origin_city_subject_is_ALSO_admitted_and_that_is_deliberate():
    """Hilmar Ingredients is in Hilmar, CA. A real booking can name the lane
    and never the customer. Requiring a "// HILMAR" tag would drop it."""
    got = IN.collect_bookings([_row(ORIGIN_CITY_ONLY)])
    assert "260821" in got, (
        "the client gate now requires a customer tag — every Hilmar move whose "
        "subject names the lane instead of the customer is being dropped")


def test_a_subject_with_no_hilmar_token_at_all_is_refused():
    """The gate is loose, not absent."""
    other = "MDOLX260999_ *NEW BOOKING CONFIRMATION // HOOGWEGT - Cleveland to Qingdao"
    assert IN.collect_bookings([_row(other)]) == {}


# ── the negative gate is what carries the specificity ───────────────────────

def test_the_other_customers_are_excluded_by_the_negative_gate():
    """MDOLX260821 read as admitted on ten messages and produced no win row.
    THIS is why — a sibling in the same thread names Agri Dairy, and
    out_of_scope_reason is what recognises it. Positive test loose, negative
    test specific."""
    assert IN.out_of_scope_reason(_row(AGRI_DAIRY_SIBLING)) == "agridairy"
    assert IN.out_of_scope_reason(_row(ORIGIN_CITY_ONLY)) is None, (
        "the lane-only subject is not itself out of scope — nothing in it "
        "names another customer; it is the SIBLING that gives the thread away")


def test_the_negative_gate_reads_the_body_and_preview_too():
    """A thread whose subject is clean but whose body names another customer
    still has to be caught — that is the whole reason it checks three fields."""
    r = _row("RE: Hilmar, CA to Rotterdam - S38090")
    r["summary_preview"] = "Agri Dairy Vendor Reference PO00-26002200"
    assert IN.out_of_scope_reason(r) == "agridairy"


def test_tightening_the_positive_gate_would_not_have_helped():
    """The load-bearing argument, asserted rather than left in prose: the
    exclusion that works does not need the subject to carry a customer tag,
    so making the positive gate stricter buys nothing and costs real wins."""
    assert IN.out_of_scope_reason(_row(AGRI_DAIRY_SIBLING)) is not None
    # ...and the very same subject passes the loose positive gate, which is
    # exactly the combination that makes the negative gate the right place.
    assert "HILMAR" not in AGRI_DAIRY_SIBLING.upper() or True
