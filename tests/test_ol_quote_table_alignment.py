"""OL quote tables must be read by HEADER-TO-CELL ALIGNMENT, never by scanning
the body — measured 2026-08-13 on two real OL emails Michael supplied.

THE DEFECT. OL sends every quote as an HTML <table>. html_to_text already
flattens it correctly into a header row and a data row that line up cell for
cell. parse_rate_table ignored that alignment and regex-scanned the whole
flattened body, so it picked values out of OL's standing legal boilerplate:

    carrier_quoted = "MSC"   <- "Maersk, Sealand, MSC, ONE, CMA and Cosco do
                                 not accept Dummy SI"      (standing footer)
    vessel_voyage  = "dive"  <- "... routing changes, vessel diversion, or
                                 alternate discharge ..."  (standing footer)

and the real values were lost with them: ALGECIRAS's ETA became Lonny's own
requested "ETA 10/19" quoted at the bottom of the forwarded chain, and HCMC's
$475.00 was dropped entirely by a `500 <= rate` gate on the prose fallback.

Downstream that is why the client report said "Quotes provided: No new
quotes", why OL-USA RESPONSES rendered empty, why QC-077 flagged rows with a
rate or carrier but no response timestamp, and why QC-039 ol_rate accuracy sat
at 92.8%. A wrong carrier on a Q&L row also corrupts carrier-negotiation
analytics.

The .eml files are committed under tests/fixtures/ so these are self-contained
and cannot silently stop testing the real thing. Ground truth below is read off
the tables inside those emails, not off a parser.
"""
from __future__ import annotations

import email
import sys
from email import policy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import body_parser as SBP  # noqa: E402  (production tree)

from hilmar import body_parser as HBP  # noqa: E402  (src/hilmar mirror)

FIXTURES = ROOT / "tests" / "fixtures"
_TREES = (SBP, HBP)
_IDS = ("scripts", "hilmar")


def _html_body(eml_name: str) -> str:
    """The text/html part of a committed .eml fixture."""
    with open(FIXTURES / eml_name, "rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_content()
    raise AssertionError(f"{eml_name} has no text/html part")


# ── Ground truth, read off the tables inside the two .eml files ────────────
#
# Fields shared by both trees. The trees deliberately differ on DATE FORMAT
# (see _LEGACY_SRC_CONTRACT in either body_parser): production persists the raw
# cell text, src/hilmar converts to ISO. Those are asserted separately below.
_ALGECIRAS_COMMON = {
    "pol": "Oakland",
    "pod": "Algeciras",
    "container_size": "1x40'HC",
    "vessel": "NYK METEOR",
    "voyage": "0CLNCE1MA",
    "vessel_voyage": "NYK METEOR 0CLNCE1MA",
    "ol_rate": 4938.0,
    "carrier_quoted": "CMA CGM",          # cell says "CMA" -> normalize_carrier
    "transshipment": "LE HAVRE",
    "origin_free_time": "4 DETENTION + 5 DEMURRAGE FREE DAYS",
    "dest_free_time": "7 COMBINED FREE DAYS",
}
_ALGECIRAS_DATES_RAW = {
    "erd": "1-Sep-26",
    "origin_cutoff": "1-Sep-26",
    "doc_cutoff": "3-Sep-26",
    "port_cutoff": "4-Sep-26",
    "etd_offered": "7-Sep-26",
    "eta_offered": "24-Oct-26",
}
_ALGECIRAS_DATES_ISO = {
    "erd": "2026-09-01",
    "origin_cutoff": "2026-09-01",
    "doc_cutoff": "2026-09-03",
    "port_cutoff": "2026-09-04",
    "etd_offered": "2026-09-07",
    "eta_offered": "2026-10-24",
    "etd": "2026-09-07",
    "eta": "2026-10-24",
}

_HCMC_COMMON = {
    "pol": "Oakland",
    "pod": "HCMC (CAT LAI)",
    "container_size": "2 X 20'DV",
    "vessel": "WAN HAI A01",
    "voyage": "W019",
    "vessel_voyage": "WAN HAI A01 W019",
    "ol_rate": 475.0,
    "carrier_quoted": "ONE",              # cell says "ONE LINE" -> ONE
    "transshipment": "DIRECT VIA CAI MEP",
    "dest_free_time": "14 DETENTION + 14 DEMURRAGE FREE DAYS",
}
_HCMC_DATES_RAW = {
    "erd": "24-Aug-26",
    "origin_cutoff": "24-Aug-26",
    "doc_cutoff": "27-Aug-26",
    "port_cutoff": "28-Aug-26",
    "etd_offered": "1-Sep-26",
    "eta_offered": "27-Sep-26",
}
_HCMC_DATES_ISO = {
    "erd": "2026-08-24",
    "origin_cutoff": "2026-08-24",
    "doc_cutoff": "2026-08-27",
    "port_cutoff": "2026-08-28",
    "etd_offered": "2026-09-01",
    "eta_offered": "2026-09-27",
    "etd": "2026-09-01",
    "eta": "2026-09-27",
}

_CASES = (
    ("ol_quote_algeciras.eml", _ALGECIRAS_COMMON, _ALGECIRAS_DATES_RAW,
     _ALGECIRAS_DATES_ISO),
    ("ol_quote_hcmc_cat_lai.eml", _HCMC_COMMON, _HCMC_DATES_RAW,
     _HCMC_DATES_ISO),
)
_CASE_IDS = ("algeciras", "hcmc")


def _parse(BP, eml_name):
    return BP.parse_rate_table(BP.html_to_text(_html_body(eml_name)))


# ── 1. The full field set, both trees, both emails ────────────────────────
@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
@pytest.mark.parametrize(("eml", "common", "raw", "iso"), _CASES, ids=_CASE_IDS)
def test_full_field_set_matches_the_table(BP, eml, common, raw, iso):
    rt = _parse(BP, eml)
    for key, expected in common.items():
        assert rt.get(key) == expected, f"{key}: {rt.get(key)!r} != {expected!r}"
    expected_dates = iso if BP.parse_rate_table.__globals__["_LEGACY_SRC_CONTRACT"] else raw
    for key, expected in expected_dates.items():
        assert rt.get(key) == expected, f"{key}: {rt.get(key)!r} != {expected!r}"


# ── 2. The exact wrong values that shipped, named so they cannot come back ─
@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
@pytest.mark.parametrize(("eml", "common", "raw", "iso"), _CASES, ids=_CASE_IDS)
def test_boilerplate_values_are_gone(BP, eml, common, raw, iso):
    rt = _parse(BP, eml)
    # "MSC" scraped from the Dummy-SI footer. Neither email quotes MSC.
    assert rt.get("carrier_quoted") != "MSC"
    # "dive" scraped out of "vessel diversion".
    assert rt.get("vessel_voyage") != "dive"
    assert rt.get("vessel") != "dive"
    # Lonny's own requested "ETA 10/19" from the bottom of the ALGECIRAS chain.
    assert "2026-10-19" not in (rt.get("eta_offered"), rt.get("eta"))
    # A quote with no rate is the failure that made the report say "no quotes".
    assert isinstance(rt.get("ol_rate"), float)


# ── 3. ALGECIRAS: the transshipment port must survive, not flatten ─────────
@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_transshipment_keeps_the_routing(BP):
    # Was "Direct", scraped from Lonny's "direct service if possible" ask.
    assert _parse(BP, "ol_quote_algeciras.eml")["transshipment"] == "LE HAVRE"
    # Was "Cai Mep" — .title()'d off the capture after "via", dropping "DIRECT".
    assert _parse(BP, "ol_quote_hcmc_cat_lai.eml")["transshipment"] == \
        "DIRECT VIA CAI MEP"


# ── 4. The two trees must agree on every format-independent field ──────────
@pytest.mark.parametrize(("eml", "common", "raw", "iso"), _CASES, ids=_CASE_IDS)
def test_trees_agree_on_the_shared_surface(eml, common, raw, iso):
    """body_parser.py is a CLAUDE.md paired file. Silent drift between the two
    parse_rate_table implementations is the root cause this whole module
    exists for: production was already correct on these emails while
    src/hilmar returned "MSC"/"dive", and NOTHING guarded the difference."""
    a, b = _parse(SBP, eml), _parse(HBP, eml)
    for key in common:
        assert a.get(key) == b.get(key), (
            f"{eml} {key}: scripts={a.get(key)!r} src/hilmar={b.get(key)!r} — "
            "the paired parsers drifted; mirror the edit.")


# ── 5. Boilerplate is unreachable AS A SOURCE, on its own ─────────────────
# These are the exact standing lines, isolated. Even with no table at all
# around them they must yield nothing.
_DUMMY_SI_LINE = (
    "Please note that these carriers will not accept Dummy SI Instructions:   "
    "**Maersk, Sealand, MSC, ONE, CMA and Cosco do not accept Dummy SI***"
)
_DISCLAIMER_LINE = (
    "** Disclaimer: Additional costs may arise due to carrier or market "
    "conditions, including impacts from the ongoing Middle East / Red Sea "
    "situation. Carriers may invoke force majeure or implement operational "
    "measures including voyage termination, routing changes, vessel diversion, "
    "or alternate discharge."
)


@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_dummy_si_line_alone_yields_no_carrier(BP):
    """A LIST of carriers can never identify THE quoted carrier. This line is
    the single largest source of wrong carriers in the stored data."""
    assert BP.parse_rate_table(_DUMMY_SI_LINE).get("carrier_quoted") is None
    assert BP._carrier_from_prose(_DUMMY_SI_LINE) is None


@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_vessel_diversion_prose_yields_no_vessel(BP):
    """"vessel diversion" is a disclaimer, not a ship."""
    rt = BP.parse_rate_table(_DISCLAIMER_LINE)
    assert rt.get("vessel_voyage") is None
    assert rt.get("vessel") is None


def test_parse_vessel_rejects_vessel_diversion():
    """src/hilmar/ingest.py calls parse_vessel on the raw body for EVERY
    bucket, so the vessel_voyage field stays poisoned even with a clean table
    parser unless this regex itself refuses the disclaimer."""
    assert HBP.parse_vessel(_DISCLAIMER_LINE) is None
    assert HBP.parse_vessel("Carriers may invoke ... vessel diversion, or") is None
    # A real labelled vessel still parses.
    assert HBP.parse_vessel("Vessel: MSC OSCAR / 012E") == "MSC OSCAR / 012E"


@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_boilerplate_appended_to_a_real_table_changes_nothing(BP):
    """The end-to-end guarantee: bolting OL's whole standing footer onto a
    real quote must not move a single field."""
    table = ("POL | POD | Vessel | Voyage | RATE | CARRIER | TRANSSHIPMENT\n"
             "Oakland | Algeciras | NYK METEOR | 0CLNCE1MA | $4938 | CMA | LE HAVRE")
    clean = BP.parse_rate_table(table)
    polluted = BP.parse_rate_table(
        f"{table}\n{_DUMMY_SI_LINE}\n{_DISCLAIMER_LINE}")
    assert clean == polluted, (clean, polluted)
    assert clean["carrier_quoted"] == "CMA CGM"
    assert clean["vessel_voyage"] == "NYK METEOR 0CLNCE1MA"


# ── 6. The NRA footer row is not a rate table ──────────────────────────────
@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_nra_footer_row_is_not_read_as_a_header(BP):
    """OL's NRA footer flattens to pipe-bearing lines that sit ABOVE the real
    grid in every forwarded chain. It contains the substring "RATE", so a
    substring-based header hint scored it. Whole-cell header matching is what
    makes it structurally not a header."""
    nra = (" THE SHIPPER'S BOOKING OF CARGO AFTER RECEIVING THE TERMS OF THIS "
           "NRA OR NRA AMENDMENT CONSITUTES | \n"
           " ACCEPTANCE OF THE RATES AND TERMS OF THIS NRA OR NRA AMENDMENT. | | | ")
    assert BP._find_table_rows(nra) is None
    assert BP.parse_rate_table(nra) == {}


@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_header_recognition_is_by_token_not_substring(BP):
    """The gate that rejects the NRA footer must not also reject OL's
    qualified column labels. "RATES" is not the token "rate" (footer stays
    out); "RATE (USD)" is (real header stays in)."""
    assert BP._header_key("RATES") is None
    assert BP._header_key(
        "ACCEPTANCE OF THE RATES AND TERMS OF THIS NRA OR NRA AMENDMENT.") is None
    assert BP._header_key("RATE (USD)") == "rate"
    assert BP._header_key("Ocean Rate") == "rate"
    assert BP._header_key("ETD (POL)") == "etd"
    # Ambiguous merged column names no field rather than guessing one.
    assert BP._header_key("Vessel/Voyage") is None


@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_qualified_column_labels_still_parse(BP):
    rt = BP.parse_rate_table(
        "POL | POD | Container Size | RATE (USD) | Ocean Carrier | ETD (POL) | ETA (POD)\n"
        "Oakland | Busan | 2x40'RF | $3,200.00 | Hapag | 12-Sep-26 | 3-Oct-26")
    assert rt.get("ol_rate") == 3200.0
    assert rt.get("carrier_quoted") == "Hapag-Lloyd"
    assert rt.get("pol") == "Oakland"
    assert rt.get("pod") == "Busan"
    assert rt.get("etd_offered")
    assert rt.get("eta_offered")


@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_real_header_is_found_below_the_nra_rows(BP):
    """Both fixtures carry the NRA rows above the quote; the parser must skip
    past them to the first REAL header rather than stopping at the first line
    that happens to contain pipes."""
    for eml in ("ol_quote_algeciras.eml", "ol_quote_hcmc_cat_lai.eml"):
        rows = BP._find_table_rows(BP.html_to_text(_html_body(eml)))
        assert rows is not None, eml
        assert rows[0][0] == "POL", rows[0]
        assert rows[1][0] == "Oakland", rows[1]


# ── 7. Absent stays absent ─────────────────────────────────────────────────
@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_absent_columns_are_absent_not_guessed(BP):
    """HCMC's table has no ORIGIN FREE TIME column at all. The field must be
    missing, not back-filled from the destination cell or from prose."""
    rt = _parse(BP, "ol_quote_hcmc_cat_lai.eml")
    assert "origin_free_time" not in rt
    # ALGECIRAS does have one, so this is a real distinction and not a
    # parser that simply never emits the field.
    assert _parse(BP, "ol_quote_algeciras.eml")["origin_free_time"]


@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_placeholder_cells_are_dropped(BP):
    """OL writes "-" / "N/A" / "TBD" into columns it has no value for. Those
    must not become the field's value."""
    rt = BP.parse_rate_table(
        "POL | POD | Vessel | RATE | CARRIER | TRANSSHIPMENT\n"
        "Oakland | Busan | - | $1200 | MSC | N/A")
    assert rt.get("ol_rate") == 1200.0
    assert rt.get("carrier_quoted") == "MSC"
    assert "vessel" not in rt
    assert "vessel_voyage" not in rt
    assert "transshipment" not in rt


# ── 8. Free-time day counts come from the free-time cells only ─────────────
# ── 9. The consumer whose value format changed ────────────────────────────
def test_ops_flow_splits_vessel_and_voyage():
    """scripts/build_ops_flow_v2.parse_ol_options used to recover the voyage by
    splitting src/hilmar's "NAME / VOY" vessel_voyage on "/". The table parser
    now joins in the house form ("NYK METEOR 0CLNCE1MA", matching
    scripts/pdf_parser) and emits `vessel`/`voyage` as their own keys, so that
    split would have silently produced voyage=None on every table-parsed quote.
    This module had no test at all before the change."""
    import build_ops_flow_v2 as OF

    opts = OF.parse_ol_options(
        "POL | POD | Vessel | Voyage | ERD | ETD | ETA | RATE | CARRIER\n"
        "Oakland | Algeciras | NYK METEOR | 0CLNCE1MA | 1-Sep-26 | 7-Sep-26 | "
        "24-Oct-26 | $4938 | CMA")
    assert len(opts) == 1
    assert opts[0]["vessel"] == "NYK METEOR"
    assert opts[0]["voyage"] == "0CLNCE1MA"
    assert opts[0]["carrier"] == "CMA CGM"
    assert opts[0]["rate_usd"] == 4938
    assert opts[0]["etd"] == "2026-09-07"
    assert opts[0]["eta"] == "2026-10-24"
    assert opts[0]["erd"] == "2026-09-01"


def test_ops_flow_legacy_slash_join_still_splits():
    """The vertical-column and prose paths still join with " / ", so the
    fallback split must stay live."""
    import build_ops_flow_v2 as OF

    body = "\n".join([
        "POL", "POD", "Container Size", "Vessel", "Voyage", "ERD", "Doc Cut",
        "Port Cut", "ETD", "ETA", "RATE", "CARRIER", "TRANSSHIPMENT", "",
        "Oakland", "HCMC", "5x40'DV", "WAN HAI A05", "W105", "30-Apr-26",
        "4-May-26", "5-May-26", "8-May-26", "10-Jun-26", "$420", "ONE",
        "DIRECT VIA CAI MEP",
    ])
    opts = OF.parse_ol_options(body)
    assert len(opts) == 1
    assert opts[0]["vessel"] == "WAN HAI A05"
    assert opts[0]["voyage"] == "W105"


@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_combined_free_days_yield_neither_detention_nor_demurrage(BP):
    """ALGECIRAS's destination cell reads "7 COMBINED FREE DAYS". A combined
    pool is not detention and is not demurrage; splitting it would be a guess.
    The origin cell's explicit "4 DETENTION + 5 DEMURRAGE" is what counts."""
    rt = _parse(BP, "ol_quote_algeciras.eml")
    if not BP.parse_rate_table.__globals__["_LEGACY_SRC_CONTRACT"]:
        pytest.skip("production tree does not emit day-count integers")
    assert rt.get("detention_free") == 4
    assert rt.get("demurrage_free") == 5
    hcmc = _parse(BP, "ol_quote_hcmc_cat_lai.eml")
    assert hcmc.get("detention_free") == 14
    assert hcmc.get("demurrage_free") == 14
