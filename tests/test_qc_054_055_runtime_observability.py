"""Regression tests for QC-054 (runtime deps importable) and QC-055
(Sentry cron heartbeat registered).

Both were added 2026-06-09 after HILMAR-DAILY-TRACKER-9 fired daily for
WEEKS because the wrapper's Python was missing sentry_sdk and the prior
audit had no QC asserting that the modules the pipeline imports actually
import. These tests drive the real phase_6_rules() with a synthetic data
dict + a tmp run-log so the check behavior is verified end-to-end, not
just string-checked. Pattern matches tests/test_qc_selfheal_checks.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal as q  # noqa: E402


def _base_data() -> dict:
    return {
        "version": "2", "requests": [],
        "summary": {
            "wins": 0, "quoted_lost": 0, "not_quoted": 0, "pending_hilmar": 0,
            "win_rate": 0.0, "quote_rate": 0.0, "teu_requested": 0, "teu_won": 0,
            "teu_quoted_lost": 0, "teu_not_quoted": 0, "teu_pending": 0,
            "total_entries": 0,
        },
    }


def _fired(data: dict) -> list[str]:
    log = q.Log()
    q.phase_6_rules(log, data)
    return log.warnings + log.errors


def _has(msgs: list[str], tag: str) -> bool:
    return any(tag in m for m in msgs)


# ── QC-054 — runtime deps importable ──────────────────────────────────────
def test_qc054_fires_when_required_module_unimportable(monkeypatch):
    """Simulate the production failure mode: sentry_sdk (or any required
    module) is uninstalled. The check must fire ERROR and name the module.

    phase_6_rules takes a local `import importlib as _imp54` then calls
    `_imp54.import_module(name)` — patching `importlib.import_module`
    intercepts that call without touching the real import system used by
    the test runner itself."""
    import importlib
    real_import = importlib.import_module

    def _fake_import(name):
        if name == "sentry_sdk":
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real_import(name)
    monkeypatch.setattr(importlib, "import_module", _fake_import)

    msgs = _fired(_base_data())
    qc054 = [m for m in msgs if "QC-054" in m]
    assert qc054, "QC-054 must fire when a required module is unimportable"
    assert "sentry_sdk" in qc054[0]
    assert "pip install" in qc054[0]  # remediation hint inline


def test_qc054_silent_when_all_required_present(monkeypatch):
    """When every required module imports cleanly, QC-054 must NOT error.

    Stub importlib.import_module to always succeed — independent of the
    local dev container's installed packages (which may be partial)."""
    import importlib

    def _all_succeed(name):
        return object()  # any non-None — caller doesn't inspect the result
    monkeypatch.setattr(importlib, "import_module", _all_succeed)
    msgs = _fired(_base_data())
    qc054_errors = [m for m in msgs if "QC-054" in m and "NOT importable" in m]
    assert not qc054_errors


# ── QC-055 — Sentry cron heartbeat registered ────────────────────────────
def test_qc055_fires_when_run_log_shows_cron_start_failed(tmp_path, monkeypatch):
    """The exact string sentry_setup.py prints when import sentry_sdk fails:
    `Sentry cron start failed (pipeline continues): <reason>`. QC-055 reads
    the wrapper's run-log; if that line appears in the recent tail, alert."""
    # Stand up a tmp 'project root' shape: <root>/reports/run-log.txt,
    # <root>/scripts/qc_selfheal.py (the check uses
    # `Path(__file__).resolve().parent.parent / 'reports' / 'run-log.txt'`).
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "run-log.txt").write_text(
        "Sentry cron start failed (pipeline continues): No module named 'sentry_sdk'\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts").mkdir()
    fake_self = tmp_path / "scripts" / "qc_selfheal.py"
    fake_self.write_text("# fake — only need __file__ to resolve in this dir\n")
    monkeypatch.setattr(q, "__file__", str(fake_self))

    msgs = _fired(_base_data())
    qc055 = [m for m in msgs if "QC-055" in m and "NOT registering" in m]
    assert qc055, "QC-055 must fire when run-log contains the cron-failed line"
    assert "HILMAR-DAILY-TRACKER-9" in qc055[0]  # ties symptom to root


def test_qc055_silent_when_run_log_clean(tmp_path, monkeypatch):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "run-log.txt").write_text(
        "Hilmar daily on CPC-micha — pipeline OK\n", encoding="utf-8",
    )
    (tmp_path / "scripts").mkdir()
    fake_self = tmp_path / "scripts" / "qc_selfheal.py"
    fake_self.write_text("# fake\n")
    monkeypatch.setattr(q, "__file__", str(fake_self))

    msgs = _fired(_base_data())
    qc055_errors = [m for m in msgs if "QC-055" in m and "NOT registering" in m]
    assert not qc055_errors
