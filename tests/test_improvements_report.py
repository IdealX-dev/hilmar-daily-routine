"""Tests for scripts/gen_improvements_report — the daily systems-audit
collector.

Locks in the QC-error / QC-warning surfacing behavior added 2026-05-25 after
an audit landed with the opaque single-line headline "Errors: 2 | Warnings:
9. Review reports/qc-result.json error_details." — unreadable from the
iPhone audit. The collector now expands each failing check into its own
red flag (errors) or observation (warnings), grouped by QC-NNN id.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gen_improvements_report as g  # noqa: E402


def _qc(error_details=None, warning_details=None, status="HAS_ERRORS"):
    return {
        "status": status,
        "fixes": 0,
        "warnings": len(warning_details or []),
        "errors": len(error_details or []),
        "error_details": list(error_details or []),
        "warning_details": list(warning_details or []),
        "counts": {"total": 153, "wins": 30, "ql": 90, "nq": 25, "pending": 8},
    }


def test_qc_errors_expand_into_individual_red_flags():
    qc = _qc(error_details=[
        "QC-007: R-1234 still PENDING past 24h",
        "QC-027: data completeness on 153 reachable rows — etd 78% (below 90% target)",
    ])
    red = g.collect_red_flags({"requests": []}, qc, {})
    titles = [r["title"] for r in red]
    assert "QC-007 ERROR" in titles
    assert "QC-027 ERROR" in titles
    # The detail must not punt to qc-result.json — the whole point of the
    # change is that the message is actionable in the email itself.
    for r in red:
        assert "qc-result.json" not in r["detail"]
        # Prefix should be stripped — the check id is already in the title.
        assert not r["detail"].startswith("QC-")


def test_qc_errors_group_repeated_check_with_count():
    qc = _qc(error_details=[
        "QC-007: R-1001 still PENDING past 24h",
        "QC-007: R-1002 still PENDING past 24h",
        "QC-007: R-1003 still PENDING past 24h",
        "QC-007: R-1004 still PENDING past 24h",
    ])
    red = g.collect_red_flags({"requests": []}, qc, {})
    # All 4 collapse into a single row with the multiplier.
    qc007 = [r for r in red if r["title"].startswith("QC-007")]
    assert len(qc007) == 1
    assert "× 4" in qc007[0]["title"]
    # First 3 messages shown inline, fourth rolled up.
    assert "R-1001" in qc007[0]["detail"]
    assert "R-1003" in qc007[0]["detail"]
    assert "+1 more" in qc007[0]["detail"]


def test_qc_warnings_surface_as_observations():
    qc = _qc(warning_details=[
        "QC-001: 0 Quoted & Lost among 153 entries — verify",
        "QC-015: 7 unmapped destinations",
    ])
    obs = g.collect_observations({"requests": []}, qc, {})
    titles = [o["title"] for o in obs]
    assert "QC-001 WARN" in titles
    assert "QC-015 WARN" in titles


def test_qc_warning_check_crash_separated_from_findings():
    # An exception-only QC group points at a broken check, not at the data —
    # the audit should call that out distinctly so Michael isn't misled.
    qc = _qc(warning_details=[
        "QC-028: check failed with exception: KeyError('foo')",
        "QC-029: check failed with exception: ValueError",
    ])
    obs = g.collect_observations({"requests": []}, qc, {})
    crashed = [o for o in obs if "crashed" in o["title"]]
    assert len(crashed) == 1
    assert "QC-028" in crashed[0]["detail"]
    assert "QC-029" in crashed[0]["detail"]
    # Should NOT also surface as data findings.
    assert not any(o["title"].startswith("QC-028 WARN") for o in obs)


def test_clean_qc_produces_no_red_flags_or_warning_observations():
    qc = _qc(status="CLEAN")
    red = g.collect_red_flags({"requests": []}, qc, {})
    obs = g.collect_observations({"requests": []}, qc, {})
    assert not any(r["title"].endswith("ERROR") or "QC status" in r["title"] for r in red)
    assert not any(o["title"].endswith("WARN") for o in obs)


def test_missing_error_details_falls_back_to_headline():
    # Belt-and-suspenders: if counts say errors exist but no detail came
    # through, we still surface a red flag rather than going silent.
    qc = {
        "status": "HAS_ERRORS",
        "fixes": 0,
        "warnings": 0,
        "errors": 2,
        "error_details": [],
        "warning_details": [],
        "counts": {"total": 153},
    }
    red = g.collect_red_flags({"requests": []}, qc, {})
    headline = [r for r in red if "QC status" in r["title"]]
    assert len(headline) == 1


def test_strip_qc_prefix_idempotent_and_safe():
    assert g._strip_qc_prefix("QC-007: hello", "QC-007") == "hello"
    assert g._strip_qc_prefix("QC-014a: hi", "QC-014a") == "hi"
    # Wrong check id — leave the message alone so we never silently mangle.
    assert g._strip_qc_prefix("QC-007: hello", "QC-099") == "QC-007: hello"
    assert g._strip_qc_prefix("", "QC-007") == ""
    assert g._strip_qc_prefix(None, "QC-007") == ""
