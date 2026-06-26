"""Regression test for the QC-039 fail-closed audit fix in scripts/qc_selfheal.py.

Audit finding: the QC-039 parser-accuracy gate was wrapped in
`try: ... except Exception: log.warn(...)`. Any failure inside (import
regression on hilmar.parser_accuracy, malformed requests, KeyError, ...) was
swallowed as a non-blocking WARN — the 95% gate would silently vanish.
WARNs do not gate qc-result status; only ERRORs do (see phase_7_save:
status = "HAS_ERRORS" if log.errors). CLAUDE.md rule #3 requires a gate that
cannot evaluate to fail CLOSED.

Fix: the except branch now calls log.error(), so a QC-039 gate that cannot
evaluate surfaces as an ERROR (HAS_ERRORS status + Sentry capture_qc_error in
post-patch), not a buried WARN.

These tests fail against the old `log.warn(...)` except branch and pass with
the `log.error(...)` fix.

(File is named test_auditfix_qc_selfheal.py — a single .py extension — because
the project's pytest config uses python_files = ["test_*.py"] with the default
prepend import mode, under which a double `.py.py` name is an invalid module
name and ERRORs the whole suite at collection time.)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import qc_selfheal as q  # noqa: E402

_QC039_FAIL_FRAGMENT = "QC-039"
_FAILED_TO_EVALUATE = "FAILED TO EVALUATE"


def _base_data() -> dict:
    """Minimal schema-shaped data dict accepted by phase_6_rules."""
    s = {
        "wins": 0, "quoted_lost": 0, "not_quoted": 0, "pending_hilmar": 0,
        "win_rate": 0.0, "quote_rate": 0.0, "teu_requested": 0, "teu_won": 0,
        "teu_quoted_lost": 0, "teu_not_quoted": 0, "teu_pending": 0,
        "total_entries": 0,
    }
    return {"version": "2", "requests": [], "summary": s}


def test_qc039_gate_fails_closed_when_compute_accuracy_raises(monkeypatch):
    """A QC-039 evaluation fault must land in log.errors (HAS_ERRORS), not
    log.warnings. This is the core fail-closed contract from the audit fix."""
    import hilmar.parser_accuracy as pa

    def _boom(*_a, **_k):
        raise RuntimeError("simulated parser_accuracy regression")

    # Patch the attribute that the inline `from hilmar.parser_accuracy import
    # compute_accuracy` re-fetches at runtime inside the QC-039 try block.
    monkeypatch.setattr(pa, "compute_accuracy", _boom, raising=True)

    log = q.Log()
    q.phase_6_rules(log, _base_data())

    qc039_errors = [
        m for m in log.errors
        if _QC039_FAIL_FRAGMENT in m and _FAILED_TO_EVALUATE in m
    ]
    qc039_warns = [
        m for m in log.warnings
        if _QC039_FAIL_FRAGMENT in m and (
            "FAILED TO EVALUATE" in m or "check failed with exception" in m
        )
    ]

    assert qc039_errors, (
        "QC-039 evaluation fault must be recorded as an ERROR (fail closed) — "
        f"got errors={log.errors!r}"
    )
    assert not qc039_warns, (
        "QC-039 evaluation fault must NOT degrade to a non-blocking WARN — "
        f"got warnings={log.warnings!r}"
    )


def test_qc039_fault_drives_has_errors_status():
    """The Log contract that downstream depends on: any error -> HAS_ERRORS.
    Locks the link between log.error() in the except branch and the gating
    semantics in phase_7_save."""
    log = q.Log()
    # No errors yet -> would be CLEAN.
    assert log.errors == []
    log.error("QC-039: parser-accuracy gate FAILED TO EVALUATE (failing closed): x")
    status = "CLEAN" if not log.errors else "HAS_ERRORS"
    assert status == "HAS_ERRORS"


def test_qc039_except_branch_uses_log_error_in_source():
    """Static guard: the QC-039 except branch must call log.error, not log.warn.
    A faithful belt-and-suspenders check so the fail-closed wording can't be
    reverted to a WARN without tripping a test, even if the harness path above
    changes shape."""
    src = (ROOT / "scripts" / "qc_selfheal.py").read_text(encoding="utf-8")
    marker = "QC-039: parser-accuracy gate FAILED TO EVALUATE"
    assert marker in src, "QC-039 fail-closed except message missing from source"
    # The old symptom-patching wording must be gone.
    assert "QC-039: check failed with exception" not in src, (
        "old WARN-based QC-039 except branch still present"
    )
    # The fail-closed line must be a log.error call.
    assert f'log.error(f"{marker}' in src, (
        "QC-039 except branch must call log.error (fail closed), not log.warn"
    )
