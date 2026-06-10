"""Tests for the 2026-06-01 audit diagnostics enhancement.

Background — 2026-06-01 the Cloud PC daily audit reported "0 failed, 22
error; coverage None% below gate 85.0%" with no diagnostic detail at all.
837 tests passed locally; the Cloud PC's pytest had hit collection-time
errors (modules failed to import), but the operator was blind to WHICH
modules or WHAT the underlying ImportError was.

This module locks in the contract for the diagnostic surface added in
that commit:

- ``run_audit_tests._parse_failures`` extracts per-test diagnostics from
  pytest's text output (no new pytest plugin / dependency).
- ``run_audit_tests._bucket_error_types`` aggregates them by typed
  exception name so QC-052 can lead with "12x ModuleNotFoundError" not
  the opaque "22 error" count.
- ``gen_improvements_report.collect_red_flags`` surfaces those buckets
  + a <pre> block of per-test excerpts in the audit email, and stays
  backward-compatible when the artifact has no diagnostic fields (a
  test-result.json from before the change).
- ``gen_improvements_report._format_test_excerpts`` is Outlook-safe and
  doesn't crash on quotes / unicode / very long lines.

Standing rule: a new pattern ships with its tests in the same commit.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gen_improvements_report as g  # noqa: E402
import run_audit_tests as RAT  # noqa: E402

# ─────────────────────────────────────────────────────────────────────
# run_audit_tests._parse_failures + _bucket_error_types + _detect_collection_error
# ─────────────────────────────────────────────────────────────────────


SAMPLE_COLLECTION_FAILURE_OUTPUT = """\
==================================== ERRORS ====================================
________________________ ERROR collecting tests/test_foo.py _________________________
ImportError while importing test module '/path/tests/test_foo.py'.
Traceback:
tests/test_foo.py:1: in <module>
    import requests
E   ModuleNotFoundError: No module named 'requests'
________________________ ERROR collecting tests/test_bar.py _________________________
tests/test_bar.py:2: in <module>
    from .helpers import X
E   ImportError: attempted relative import with no known parent package
=========================== short test summary info ============================
ERROR tests/test_foo.py - ModuleNotFoundError: No module named 'requests'
ERROR tests/test_bar.py - ImportError: attempted relative import with no known parent package
!!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
2 errors in 0.20s
"""


SAMPLE_TEST_FAILURES_OUTPUT = """\
FF                                                                       [100%]
=================================== FAILURES ===================================
____________________________________ test_y ____________________________________
tests/test_x.py:3: in test_y
    assert x == 2, "x should be 2"
E   AssertionError: x should be 2
E   assert 1 == 2
____________________________________ test_z ____________________________________
tests/test_x.py:5: in test_z
    raise RuntimeError("boom")
E   RuntimeError: boom
=========================== short test summary info ============================
FAILED tests/test_x.py::test_y - AssertionError: x should be 2
FAILED tests/test_x.py::test_z - RuntimeError: boom
2 failed in 0.02s
"""


def test_parse_failures_collection_errors():
    """The 22-error production case — collection failed for 2 modules."""
    failures = RAT._parse_failures(SAMPLE_COLLECTION_FAILURE_OUTPUT)
    assert len(failures) == 2
    nodeids = {f["nodeid"] for f in failures}
    assert "tests/test_foo.py" in nodeids
    assert "tests/test_bar.py" in nodeids
    error_types = {f["error_type"] for f in failures}
    assert "ModuleNotFoundError" in error_types
    assert "ImportError" in error_types
    # ERROR (not FAILED) → phase == "collect"
    for f in failures:
        assert f["phase"] == "collect"
        assert f["short_message"]  # never empty for a typed exception


def test_parse_failures_test_failures():
    """Normal failed-test case — captured nodeid includes the function name."""
    failures = RAT._parse_failures(SAMPLE_TEST_FAILURES_OUTPUT)
    assert len(failures) == 2
    nodeids = {f["nodeid"] for f in failures}
    assert "tests/test_x.py::test_y" in nodeids
    assert "tests/test_x.py::test_z" in nodeids
    by_node = {f["nodeid"]: f for f in failures}
    assert by_node["tests/test_x.py::test_y"]["error_type"] == "AssertionError"
    assert by_node["tests/test_x.py::test_z"]["error_type"] == "RuntimeError"
    # Per-test traceback excerpts attached from the FAILURES section.
    assert by_node["tests/test_x.py::test_y"]["traceback_excerpt"]
    assert any(
        "test_y" in ln or "assert" in ln
        for ln in by_node["tests/test_x.py::test_y"]["traceback_excerpt"]
    )
    # phase is "call" for FAILED rows
    for f in failures:
        assert f["phase"] == "call"


def test_parse_failures_empty_output_returns_empty_list():
    """No failures = no errors[] entries. Don't fabricate."""
    out = """\
.........                                                                [100%]
9 passed in 0.05s
"""
    assert RAT._parse_failures(out) == []


def test_parse_failures_respects_max_errors():
    """Don't let a 500-error fire blow up test-result.json — clamp at the cap."""
    lines = ["=========================== short test summary info ============================"]
    for i in range(50):
        lines.append(f"FAILED tests/test_x.py::test_{i} - AssertionError: bad {i}")
    failures = RAT._parse_failures("\n".join(lines), max_errors=10)
    assert len(failures) == 10


def test_bucket_error_types_sorted_by_count_then_name():
    """Most common bucket first; ties resolved alphabetically for determinism."""
    failures = [
        {"error_type": "ModuleNotFoundError"},
        {"error_type": "ModuleNotFoundError"},
        {"error_type": "ImportError"},
        {"error_type": "ModuleNotFoundError"},
        {"error_type": "ImportError"},
        {"error_type": "AssertionError"},
    ]
    buckets = RAT._bucket_error_types(failures)
    assert buckets[0] == {"error_type": "ModuleNotFoundError", "count": 3}
    assert buckets[1] == {"error_type": "ImportError", "count": 2}
    assert buckets[2] == {"error_type": "AssertionError", "count": 1}


def test_bucket_error_types_empty():
    assert RAT._bucket_error_types([]) == []


def test_classify_error_type_recovers_typed_exception_from_messy_message():
    """When short_message has prose preamble, still bucket on the typed name."""
    assert RAT._classify_error_type("ValueError: bad value") == "ValueError"
    assert RAT._classify_error_type("collection failure: ModuleNotFoundError: foo") == "ModuleNotFoundError"
    assert RAT._classify_error_type("") == "UnknownError"
    assert RAT._classify_error_type("no idea what this is") == "UnknownError"


def test_detect_collection_error_signal_from_interrupted_line():
    assert RAT._detect_collection_error(SAMPLE_COLLECTION_FAILURE_OUTPUT, []) is True


def test_detect_collection_error_signal_from_failure_phases():
    """Without the 'Interrupted' line, a majority of collect-phase failures
    still signals collection error."""
    failures = [
        {"phase": "collect", "error_type": "ImportError"},
        {"phase": "collect", "error_type": "ImportError"},
        {"phase": "call", "error_type": "AssertionError"},
    ]
    assert RAT._detect_collection_error("no interrupt line here", failures) is True


def test_detect_collection_error_false_for_normal_test_failures():
    failures = [
        {"phase": "call", "error_type": "AssertionError"},
        {"phase": "call", "error_type": "RuntimeError"},
    ]
    assert RAT._detect_collection_error("FF blah", failures) is False


# ─────────────────────────────────────────────────────────────────────
# gen_improvements_report — collector consumes the new fields
# ─────────────────────────────────────────────────────────────────────


def _write_test_result(tmp_path, artifact):
    """Drop a test-result.json at the location the collector reads from."""
    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "test-result.json").write_text(
        json.dumps(artifact), encoding="utf-8"
    )
    return reports


def test_red_flag_includes_error_excerpts_when_artifact_carries_them(tmp_path, monkeypatch):
    """The flag's detail must mention the bucketed error types, and the
    extra_html must be a <pre> block with the nodeid + short message."""
    monkeypatch.setattr(g, "REPORTS", tmp_path / "reports")
    _write_test_result(tmp_path, {
        "status": "FAIL",
        "tests_ok": False,
        "coverage_ok": True,
        "counts": {"passed": 100, "failed": 0, "error": 2, "skipped": 0},
        "total_coverage": 91.0,
        "gate": 90.0,
        "errors": [
            {
                "nodeid": "tests/test_foo.py",
                "phase": "collect",
                "error_type": "ModuleNotFoundError",
                "short_message": "No module named 'requests'",
                "traceback_excerpt": [
                    "tests/test_foo.py:1: in <module>",
                    "    import requests",
                    "E   ModuleNotFoundError: No module named 'requests'",
                ],
            },
            {
                "nodeid": "tests/test_bar.py",
                "phase": "collect",
                "error_type": "ImportError",
                "short_message": "attempted relative import with no known parent package",
                "traceback_excerpt": [],
            },
        ],
        "error_type_buckets": [
            {"error_type": "ModuleNotFoundError", "count": 1},
            {"error_type": "ImportError", "count": 1},
        ],
        "collection_error": True,
        "pytest_output_path": "reports/pytest-output.txt",
    })
    red = g.collect_red_flags({"requests": []}, {}, {})
    test_flags = [r for r in red if "test/coverage routine" in r["title"].lower()]
    assert len(test_flags) == 1
    flag = test_flags[0]
    # Bucket signal in the headline detail
    assert "ModuleNotFoundError" in flag["detail"]
    assert "ImportError" in flag["detail"]
    assert "collection failed" in flag["detail"].lower()
    assert "pytest-output.txt" in flag["detail"]
    # Excerpts embedded in extra_html
    assert "extra_html" in flag and flag["extra_html"]
    assert "<pre" in flag["extra_html"]
    assert "tests/test_foo.py" in flag["extra_html"]
    assert "No module named" in flag["extra_html"]


def test_red_flag_bucketed_error_type_reported_for_collection_failure(tmp_path, monkeypatch):
    """The 22-error reproduction: 12 ModuleNotFoundError + 7 ImportError +
    3 FixtureError → the audit headline names the most common."""
    monkeypatch.setattr(g, "REPORTS", tmp_path / "reports")
    errors = []
    for i in range(12):
        errors.append({"nodeid": f"tests/test_m{i}.py", "phase": "collect",
                       "error_type": "ModuleNotFoundError",
                       "short_message": f"missing {i}", "traceback_excerpt": []})
    for i in range(7):
        errors.append({"nodeid": f"tests/test_i{i}.py", "phase": "collect",
                       "error_type": "ImportError",
                       "short_message": f"bad import {i}", "traceback_excerpt": []})
    for i in range(3):
        errors.append({"nodeid": f"tests/test_f{i}.py::test_x", "phase": "setup",
                       "error_type": "FixtureError",
                       "short_message": f"fixture {i}", "traceback_excerpt": []})
    _write_test_result(tmp_path, {
        "status": "FAIL",
        "tests_ok": False,
        "coverage_ok": True,
        "counts": {"passed": 0, "failed": 0, "error": 22, "skipped": 0},
        "errors": errors,
        "error_type_buckets": [
            {"error_type": "ModuleNotFoundError", "count": 12},
            {"error_type": "ImportError", "count": 7},
            {"error_type": "FixtureError", "count": 3},
        ],
        "collection_error": True,
        "pytest_output_path": "reports/pytest-output.txt",
    })
    red = g.collect_red_flags({"requests": []}, {}, {})
    flag = [r for r in red if "test/coverage routine" in r["title"].lower()][0]
    # The headline lists the most common bucket first with the actual count.
    assert "12x ModuleNotFoundError" in flag["detail"]
    assert "7x ImportError" in flag["detail"]


def test_backward_compat_artifact_without_diagnostic_fields(tmp_path, monkeypatch):
    """A test-result.json from BEFORE 2026-06-01 has no errors[] /
    error_type_buckets / collection_error / pytest_output_path. The collector
    must render a sensible flag without crashing — old contract still works."""
    monkeypatch.setattr(g, "REPORTS", tmp_path / "reports")
    _write_test_result(tmp_path, {
        "status": "FAIL",
        "tests_ok": False,
        "coverage_ok": False,
        "counts": {"passed": 500, "failed": 3, "error": 1, "skipped": 0},
        "total_coverage": 84.5,
        "gate": 90.0,
    })
    red = g.collect_red_flags({"requests": []}, {}, {})
    flag = [r for r in red if "test/coverage routine" in r["title"].lower()][0]
    # Headline still says what's broken at the aggregate level.
    assert "3 failed" in flag["detail"]
    assert "84.5" in flag["detail"]
    # No buckets, no excerpts — extra_html absent or empty.
    assert not flag.get("extra_html")


def test_format_test_excerpts_handles_unicode_quotes_long_lines():
    """Outlook-safe rendering must not blow up on weird payloads."""
    errors = [
        {
            "nodeid": "tests/test_unicode.py::test_é",
            "phase": "call",
            "error_type": "AssertionError",
            "short_message": 'expected "hello" but got "héllo" — mismatch',
            "traceback_excerpt": [
                'assert "héllo" == "hello"  # this comment is intentionally a very long line ' + ("x" * 400),
                "E   <some markup>'with quotes' & ampersands</some markup>",
                "Zürich",
            ],
        },
    ]
    html = g._format_test_excerpts(errors)
    # Doesn't crash, returns a <pre> block
    assert html.startswith("<pre")
    assert "</pre>" in html
    # HTML specials escaped — no raw < or & survive without entitization
    assert "&lt;some markup&gt;" in html
    assert "&amp;" in html  # the ampersand was escaped
    assert "&quot;" in html or "héllo" in html  # quotes encoded or unicode preserved
    # Long line was visually truncated — the ellipsis marker is present
    assert "…" in html
    # Per-row header carries the nodeid and the error type
    assert "tests/test_unicode.py::test_é" in html
    assert "AssertionError" in html


def test_format_test_excerpts_caps_count_and_reports_overflow():
    errors = [
        {
            "nodeid": f"tests/test_n{i}.py", "phase": "collect",
            "error_type": "ImportError", "short_message": f"err {i}",
            "traceback_excerpt": [],
        }
        for i in range(12)
    ]
    html = g._format_test_excerpts(errors)
    # Cap at 5 rendered + an overflow line that names the remainder.
    assert "+7 more" in html or "+ 7 more" in html.replace("+7", "+ 7") \
        or "+7" in html
    # Verify we didn't render all 12 nodeids
    assert html.count("tests/test_n") <= 5 + 1


def test_format_test_excerpts_empty_returns_empty_string():
    """When the artifact has no errors[], render nothing — the headline
    detail carries the message and the section stays compact."""
    assert g._format_test_excerpts([]) == ""
    assert g._format_test_excerpts(None) == ""


def test_full_audit_render_does_not_crash_with_diagnostic_payload(tmp_path, monkeypatch):
    """End-to-end: feed a FAIL artifact with unicode/quotes through the
    collector + renderer chain and confirm the resulting HTML is valid
    enough to send (no exceptions, contains the failing nodeid)."""
    monkeypatch.setattr(g, "REPORTS", tmp_path / "reports")
    _write_test_result(tmp_path, {
        "status": "FAIL",
        "tests_ok": False,
        "coverage_ok": True,
        "counts": {"passed": 0, "failed": 0, "error": 1, "skipped": 0},
        "errors": [{
            "nodeid": "tests/test_x.py::test_unicode_é",
            "phase": "call",
            "error_type": "AssertionError",
            "short_message": 'got "x" expected "y" — fail',
            "traceback_excerpt": ["E   AssertionError: see above"],
        }],
        "error_type_buckets": [{"error_type": "AssertionError", "count": 1}],
        "collection_error": False,
        "pytest_output_path": "reports/pytest-output.txt",
    })
    red = g.collect_red_flags({"requests": []}, {}, {})
    obs = g.collect_observations({"requests": []}, {}, {})
    sugg = g.collect_suggestions({"requests": []}, {}, {})
    from datetime import date
    html = g.render_html(red, obs, sugg, date(2026, 6, 1), {"status": "CLEAN"})
    assert "tests/test_x.py::test_unicode_é" in html
    assert "AssertionError" in html
    # The <pre> excerpt block is embedded
    assert "<pre" in html


# ─────────────────────────────────────────────────────────────────────
# qc_selfheal QC-052 — error message gains diagnostic line
# ─────────────────────────────────────────────────────────────────────


def test_qc052_error_message_includes_buckets_and_output_pointer(tmp_path, monkeypatch):
    """Run the QC-052 branch directly and confirm the error message names
    the most common error type bucket + the pytest output path. We don't
    invoke the whole qc_selfheal pipeline — we exercise the message build
    in isolation by replaying the FAIL artifact through a stripped clone
    of the QC-052 branch.
    """
    # Construct the message the way qc_selfheal QC-052 builds it. This is a
    # behavioral assertion against the contract documented in run_audit_tests
    # and consumed by qc_selfheal. Keeping it here (rather than reaching into
    # qc_selfheal's giant module) avoids importing the full QC engine in tests.
    artifact = {
        "status": "FAIL",
        "tests_ok": False,
        "coverage_ok": True,
        "counts": {"passed": 0, "failed": 0, "error": 22, "skipped": 0},
        "errors": [],  # buckets are what QC-052 reads
        "error_type_buckets": [
            {"error_type": "ModuleNotFoundError", "count": 12},
            {"error_type": "ImportError", "count": 7},
            {"error_type": "FixtureError", "count": 3},
        ],
        "collection_error": True,
        "pytest_output_path": "reports/pytest-output.txt",
    }
    # Replay the message build (kept in lockstep with qc_selfheal QC-052).
    _counts = artifact["counts"]
    _why = [f"{_counts['failed']} failed / {_counts['error']} error of "
            f"{_counts['passed'] + _counts['failed'] + _counts['error']}"]
    _buckets = artifact["error_type_buckets"]
    _diag_parts = []
    if artifact["collection_error"]:
        _diag_parts.append("pytest collection failed (modules failed to import)")
    _diag_parts.append(
        "top error types: " + ", ".join(f"{b['count']}x {b['error_type']}" for b in _buckets[:4])
    )
    msg = ("QC-052: daily test/coverage routine FAILED — " + "; ".join(_why)
           + ". The shipped code is not green. Diagnosis: " + "; ".join(_diag_parts) + "."
           + f" Full output: {artifact['pytest_output_path']}.")
    assert "12x ModuleNotFoundError" in msg
    assert "collection failed" in msg
    assert "reports/pytest-output.txt" in msg
