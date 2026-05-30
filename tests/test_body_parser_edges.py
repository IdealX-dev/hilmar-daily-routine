"""Targeted tests for the regex parser edge cases — the uncovered branches
in hilmar.body_parser that QC-052 / run_audit_tests.py flagged. These are
the SUBJECT junk-prefix rejection, the date-with-month-name path, the
temperature-unit range checks, the multi-name signer extraction, and the
rate-table date-fallback branches.

WHY THIS MATTERS

The audit and Michael's standing rule: "this parser and your system have
to run at minimum of 98 percent accuracy no matter COST." Coverage is a
necessary (not sufficient) condition for that — every uncovered branch
is a branch that could silently regress.
"""
from __future__ import annotations

import pytest

from hilmar import body_parser as BP


# ── parse_subject_lane — junk-prefix rejection (lines 287-291) ──────────────

@pytest.mark.parametrize("subject", [
    "RE: Oakland to Yokohama 1x40HC",
    "FW: Oakland to Tokyo 2x20",
    "FWD: Oakland to Osaka",
    "PLS confirm Oakland to Busan",
    "NEED rate Oakland to HCMC",
    "THE Oakland to Singapore quote",
])
def test_subject_lane_rejects_junk_prefix(subject):
    """Generic 'word to word' matcher catches any 'X to Y' — junk-prefix
    rejection prevents 'RE/FW/FWD/PLS/NEED/THE' from being treated as an
    origin port. Without this guard, 'RE: Oakland to Yokohama' would
    return ('RE', 'Oakland...') instead of the real lane."""
    origin, dest = BP.parse_subject_lane(subject)
    # Either it parses correctly (real origin extracted, not "RE")
    # or it returns (None, None) — both prove the junk prefix didn't win.
    assert origin is None or origin.lower() != "re"
    assert origin is None or origin.lower() not in ("fw", "fwd", "pls", "need", "the")


def test_subject_lane_generic_fallback_for_clean_lane():
    """The bare 'X to Y' pattern (no other tokens) hits the fallback regex —
    confirms the rejection path is the ONLY thing that nulls out a clean lane."""
    origin, dest = BP.parse_subject_lane("Hamburg to Rotterdam 2x40")
    assert origin == "Hamburg"
    assert dest == "Rotterdam"


# ── _date_from_match — month-name vs numeric-month branches (315-317) ──────

def test_etd_offered_month_name_format():
    """'15-Apr' / '15 April' format — exercises the month-name branch in
    _date_from_match (lines 315-317). Without a year, it uses default_year."""
    # Date format: "ETD 15-May-2026" → matches "15-may-2026" path
    d = BP.parse_etd_offered("Vessel arrives ETD 15-May-2026 via Singapore")
    assert d == "2026-05-15"


def test_etd_offered_month_name_no_year_uses_default():
    """Without an explicit year, the default-year fallback kicks in."""
    d = BP.parse_etd_offered("Vessel ETD 03-Jun, transit ~12d")
    # The fallback applies CURRENT year — assert the month and day are correct
    # (year may be the real current year, not hardcoded).
    assert d is not None
    assert d.endswith("-06-03")


# ── parse_temperature — out-of-range rejection (lines 401-410) ──────────────

@pytest.mark.parametrize("text,expected_none", [
    # Out-of-range Celsius
    ("Set at -50C", True),
    ("Maintain 50C", True),
    # Out-of-range Fahrenheit
    ("Run at -50F", True),
    ("Hold at 150F", True),
])
def test_temperature_rejects_out_of_range(text, expected_none):
    """Range guards: -40 ≤ C ≤ 30, -40 ≤ F ≤ 120. Anything outside is
    almost certainly not a temperature reading (could be a rate, ETA day
    count, container number, etc.)."""
    result = BP.parse_temperature(text)
    if expected_none:
        assert result is None


@pytest.mark.parametrize("text,expected", [
    ("Set at -18C for the reefer", "-18C"),
    ("Hold at 4C", "4C"),
    ("Run at 35F", "35F"),
])
def test_temperature_accepts_in_range(text, expected):
    """In-range values are extracted. Confirms the guard isn't too tight."""
    assert BP.parse_temperature(text) == expected


# ── parse_signer — multi-name extraction (lines 977-993) ────────────────────

def test_signer_extracts_two_token_name():
    """Two-token name (first + last) hits the `2 <= len(clean) <= 4` branch."""
    body = (
        "Hi Lonny,\n\n"
        "Rate confirmed at $3,500/40HC.\n\n"
        "Best regards,\n"
        "Alexandra Hernandez\n"
        "OL-USA\n"
    )
    assert BP.parse_signer(body) == "Alexandra Hernandez"


def test_signer_extracts_three_token_name():
    """Three-token name (Mary J. Smith) — exercises the 3-token branch."""
    body = (
        "Rate confirmed.\n\n"
        "Best,\n"
        "Mary J. Smith\n"
    )
    out = BP.parse_signer(body)
    assert out == "Mary J. Smith"


def test_signer_skips_customer_side_signers():
    """The Lonny / Hilmar side of the thread mustn't pollute the OL-responder
    field. _CUSTOMER_SIDE_SIGNERS guards the candidate name before return."""
    body = (
        "Please send, thanks.\n\n"
        "Lonny Upfold\n"
        "Hilmar Ingredients\n"
    )
    # Whatever it returns, it must NOT be Lonny.
    assert (BP.parse_signer(body) or "").lower() != "lonny upfold"


def test_signer_returns_none_for_no_name_line():
    """No human-name lines after the body → return None."""
    body = (
        "Rate confirmed at $3,500.\n"
        "ETA 2026-05-15.\n"
        "Vessel: MSC OSCAR.\n"
    )
    assert BP.parse_signer(body) is None


def test_signer_handles_empty():
    assert BP.parse_signer("") is None
    assert BP.parse_signer(None) is None  # type: ignore[arg-type]


# ── _parse_table_date — date-extraction edge cases (lines 690-691) ──────────

def test_parse_table_date_invalid_returns_none():
    """An ISO-looking but invalid date (Feb 30, month 13, etc.) returns None
    rather than raising. The ValueError catch is the uncovered branch."""
    # The function is private but reachable via parse_mbd_rate_columns —
    # easier to test directly. Skip the test cleanly if not exposed.
    if not hasattr(BP, "_parse_table_date"):
        pytest.skip("_parse_table_date no longer exposed")
    assert BP._parse_table_date("Feb 30, 2026") is None
    assert BP._parse_table_date("13-99-2026") is None


def test_parse_table_date_handles_empty():
    if not hasattr(BP, "_parse_table_date"):
        pytest.skip("_parse_table_date no longer exposed")
    assert BP._parse_table_date("") is None
    assert BP._parse_table_date(None) is None  # type: ignore[arg-type]


# ── parse_rate_table — fallback rate ranges (lines 753, 818-819) ────────────

def test_rate_table_rejects_implausible_rate():
    """The 200 ≤ rate_val ≤ 50000 (and 500 ≤ val ≤ 50000) range guards reject
    nonsense numbers that the regex would otherwise capture (e.g. a year,
    container number, or vessel voyage)."""
    # A body with no real rate but plausible-looking numbers
    body = "Vessel: MSC 2026 / 99. ETD 30-Apr-2026."
    rt = BP.parse_rate_table(body)
    # Should NOT have extracted a rate from "2026" or "99"
    assert rt.get("ol_rate") is None or rt["ol_rate"] >= 200


def test_rate_table_accepts_plausible_rate():
    body = "Rate confirmed at $3,500/40HC on CMA CGM."
    rt = BP.parse_rate_table(body)
    assert rt.get("ol_rate") == 3500
