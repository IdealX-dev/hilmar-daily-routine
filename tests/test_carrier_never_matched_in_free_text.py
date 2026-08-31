"""A carrier is claimed from a NAME or a BOOKING REFERENCE — never a substring.

Shared reference-data contract, rule 4: "Never match a bare 2-letter carrier
code in free text. PO is a purchase order, CM is centimetres, FX is foreign
exchange, 5X is 5x40HC, VS is what a comparison prints BETWEEN two carriers.
Match a full name or a labelled column, nothing else."

THE DEFECT THIS FILE EXISTS FOR, measured 2026-08-31 against production code.
Three sites matched carrier tokens with a bare `in`. The worst ran in the
daily fire — patch_carriers PASS 4, whose table listed "NAM" as a CMA CGM
booking-ref prefix and searched for it anywhere in the subject line:

    CMA CGM  <- MDOLX261145_ HILMAR Oakland to Cat Lai, VIETNAM 2x40RF
    CMA CGM  <- MDOLX260502_ HILMAR Oakland to Cai Mep, VIETNAM 1x40HC
    CMA CGM  <- HILMAR Oakland to Manzanillo, PANAMA 2x40RF

VIET-NAM. PA-NAM-A. Every Vietnam and Panama lane, and Cai Mep alone was 16 of
134 bookings in OL's 2026 export. The branch only fills a WIN whose
carrier_won is blank, so it never overwrote a known carrier — it INVENTED one
where the honest answer was None, which is the failure the contract names:
a wrong carrier on a priced row misleads a human in a way a blank never does.

And it did not stay here. share_intel exports carrier_summary, and
sync_to_quote_tracker upserts those names into a Turso registry another repo
reads, as canonical vendor entities with aliases, every fire.

THE FIX IS THIS REPO'S OWN EXISTING ANSWER. body_parser.detect_carrier_token
(2026-06-15) already scanned on word boundaries and refused ambiguous short
tokens outside a carrier cell; parse_subject_carrier's Pattern D already
anchored a ref prefix to its digits. The two older sites simply never adopted
them. Nothing new was invented — the correct matcher was lifted into
body_parser and the private tables deleted.

MOST OF THIS FILE IS POSITIVE ASSERTIONS ON PURPOSE. A matcher that matches
nothing passes every negative test ever written.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

# src/hilmar/body_parser.py uses relative imports, so it is imported as a
# package member; the scripts/ tree is a flat module on sys.path.
import backfill_mdolx as BM  # noqa: E402
import body_parser as BP  # noqa: E402

from hilmar import body_parser as HBP  # noqa: E402

#: PASS 4's matcher, reached through the module rather than reimplemented —
#: a test that re-types the logic tests the copy, not the code that ships.
PC_SRC = (ROOT / "scripts" / "patch_carriers.py").read_text(encoding="utf-8")


# ── the live regression ───────────────────────────────────────────────────

#: Real Hilmar subject shapes. NONE of these names a carrier.
NO_CARRIER_SUBJECTS = [
    "MDOLX261145_ HILMAR Oakland to Cat Lai, VIETNAM 2x40RF",
    "MDOLX260502_ HILMAR Oakland to Cai Mep, VIETNAM 1x40HC",
    "HILMAR Oakland to Ho Chi Minh, VIETNAM  2x40'RF",
    "HILMAR Oakland to Manzanillo, PANAMA 2x40RF",
    "HILMAR Oakland to Balboa, PANAMA 1x40HC",
    "HILMAR rate request Oakland to Busan",
]


@pytest.mark.parametrize("subject", NO_CARRIER_SUBJECTS)
def test_a_country_name_is_not_a_carrier(subject):
    assert BP.detect_carrier_token(subject) is None, (
        f"a carrier was detected in a subject that names none: {subject!r}")
    assert BP.carrier_from_booking_ref(subject) is None, (
        f"a booking ref was detected with no digits behind it: {subject!r}")


@pytest.mark.parametrize("subject", NO_CARRIER_SUBJECTS)
def test_verification_refuses_the_country_name_too(subject):
    # backfill_mdolx._carrier_match gates whether an MDOLX is bound to a WIN.
    # find_mdolx_for_win's own docstring: "false matches are far worse than no
    # match — they corrupt data."
    assert BM._carrier_match("CMA CGM", subject) is False, (
        f"MDOLX would be bound to a WIN on a false carrier match: {subject!r}")


def test_pass_4_no_longer_carries_its_own_substring_table():
    assert "_CARRIER_PREFIXES" not in PC_SRC, (
        "patch_carriers grew back a private carrier table; the ref prefixes "
        "live in body_parser.CARRIER_REF_PREFIXES and nowhere else")
    assert "BP.carrier_from_booking_ref" in PC_SRC, (
        "PASS 4 no longer routes through the anchored booking-ref matcher")


# ── the booking-ref anchor is the whole guard ─────────────────────────────

def test_a_ref_prefix_counts_only_with_digits_behind_it():
    assert BP.carrier_from_booking_ref("MDOLX260114 / 2x40'RF CMA: NAM8322223") == "CMA CGM"
    assert BP.carrier_from_booking_ref("MDOLX260473 ... // CMA BKG # NAM8451437") == "CMA CGM"
    assert BP.carrier_from_booking_ref("MDOLX260453 ... // MSC: EBKG16491184") == "MSC"
    # ...and the same prefix, unanchored, is just letters
    assert BP.carrier_from_booking_ref("shipment to VIETNAM") is None
    assert BP.carrier_from_booking_ref("routed via PANAMA") is None
    assert BP.carrier_from_booking_ref("NAM") is None
    assert BP.carrier_from_booking_ref("NAM without digits") is None


def test_the_ref_table_holds_no_bare_name_tokens():
    """Mixing names into the ref table is what made NAM searchable as prose.
    Every entry must be a booking-ref prefix, never a carrier's spoken name."""
    flat = [p for ps in BP.CARRIER_REF_PREFIXES.values() for p in ps]
    for spoken in ("CMA", "CGM", "MSC", "ONE", "ZIM", "HMM", "YML", "EMC",
                   "MAERSK", "EVERGREEN", "HAPAG", "COSCO", "YANG", "OOCL",
                   "MSK", "HLAG", "HYUNDAI", "COSCON", "YANGMING"):
        assert spoken not in flat, (
            f"{spoken!r} is a NAME, not a booking-ref prefix — it belongs to "
            f"detect_carrier_token, which knows whether it is ambiguous")


def test_carrier_named_in_asks_about_one_carrier_not_the_first_of_many():
    both = "MDOLX1 // MSC: EBKG16491184 (was quoted by Maersk)"
    assert BP.carrier_named_in(both, "Maersk") is True
    assert BP.carrier_named_in(both, "MSC") is True
    assert BP.carrier_named_in(both, "COSCO") is False


# ── the library tree's prose scanner ──────────────────────────────────────

@pytest.mark.parametrize("prose,was", [
    ("Please call my phone for details", "ONE"),
    ("stone container", "ONE"),
    ("Booking done, no money yet", "ONE"),
    ("We are done with this lane", "ONE"),
    ("ZIMBABWE inland move", "ZIM"),
])
def test_an_english_word_is_not_a_carrier(prose, was):
    # ingest.py:597 feeds this f"{subject}\n{preview}" — unlabelled prose.
    assert HBP._find_carrier(prose) is None, (
        f"{prose!r} still reads as the carrier {was}")


def test_the_ambiguous_token_still_matches_in_a_carrier_CELL():
    # The guard is about CONTEXT, not about refusing the token forever. A cell
    # known to be the carrier column is evidence; prose is not.
    assert HBP._find_carrier("ONE", allow_short=True) == "ONE"
    assert HBP._find_carrier("CMA CGM", allow_short=True) is not None


# ── POSITIVE: the matchers must still do their job ────────────────────────

#: (prose, what _find_carrier returns, what it canonicalises to).
#: The middle column is _find_carrier's own .title() convention, which
#: PREDATES this fix and is deliberately left alone — ingest.py:597 pipes the
#: result through core.normalize_carrier, so the canonical form is what
#: reaches a row. Pinning both columns keeps that contract visible: if the
#: title-casing is ever "tidied", the third column fails and says why it matters.
@pytest.mark.parametrize("text,raw,canonical", [
    ("Rate via MSC on the 14th", "MSC", "MSC"),
    ("Wan Hai A01 vessel", "Wan Hai", "Wan Hai"),
    ("Maersk quote attached", "Maersk", "Maersk"),
    ("EVERGREEN booking", "Evergreen", "Evergreen"),
    ("COSCO sailing", "Cosco", "COSCO"),
    ("CMA CGM quote attached", "Cma Cgm", "CMA CGM"),
])
def test_a_real_carrier_name_still_resolves_in_the_library_tree(text, raw, canonical):
    import core as C
    got = HBP._find_carrier(text)
    assert got == raw, f"{text!r} -> {got!r}"
    assert (C.normalize_carrier(got) or got) == canonical, (
        f"{got!r} does not canonicalise to {canonical!r} — the value that "
        f"reaches a row would be wrong")


@pytest.mark.parametrize("subject,expected", [
    ("MDOLX260407 ... // EVERGREEN", "Evergreen"),
    ("MDOLX260453_UPDATED BOOKING CONFIRMATION// BTG 1X40'HC // MSC: EBKG16491184", "MSC"),
    ("MDOLX260114 / 2x40'RF CMA: NAM8322223", "CMA CGM"),
])
def test_a_real_booking_subject_still_resolves(subject, expected):
    got = BP.parse_subject_carrier(subject) or BP.detect_carrier_token(subject) \
        or BP.carrier_from_booking_ref(subject)
    assert got == expected, f"{subject!r} -> {got!r}, expected {expected!r}"


def test_verification_still_confirms_a_genuine_match():
    assert BM._carrier_match("CMA CGM", "MDOLX260114 / 2x40'RF CMA: NAM8322223") is True
    assert BM._carrier_match("MSC", "MDOLX260453 ... // MSC: EBKG16491184") is True
    assert BM._carrier_match("Evergreen", "MDOLX260407 ... // EVERGREEN") is True


# ── parity ────────────────────────────────────────────────────────────────

def test_both_trees_agree_on_the_booking_ref_table():
    assert BP.CARRIER_REF_PREFIXES == HBP.CARRIER_REF_PREFIXES
    for t in ("CMA: NAM8322223", "// MSC: EBKG16491184", "to VIETNAM", "PANAMA"):
        assert BP.carrier_from_booking_ref(t) == HBP.carrier_from_booking_ref(t), (
            f"the trees disagree about {t!r}")
