"""Tests for the per-step timeout in scripts/run_pipeline.py (Sentry-9
"Cron failure" 2026-05-28 — a hung step was dragging the pipeline past
the 60-min Sentry cron monitor window, so the cron check-in never
arrived and the monitor fired every day)."""
from __future__ import annotations

import sys
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_pipeline as RP  # noqa: E402


def test_run_step_kills_a_hung_subprocess(monkeypatch):
    """A step that blocks past its timeout must be killed and reported as
    a failure — without this, a network-bound step (Sentry, Turso) hangs
    indefinitely and the wrapper exceeds the 60-min cron window."""
    def fake_run(*a, **kw):
        # subprocess.run with timeout= raises TimeoutExpired on timeout
        raise subprocess.TimeoutExpired(cmd=a[0] if a else kw.get("cmd"), timeout=kw.get("timeout", 0))

    monkeypatch.setattr(RP.subprocess, "run", fake_run)
    monkeypatch.setitem(RP.STEP_TIMEOUTS_S, "Hung step", 1)

    ok = RP.run_step("Hung step", [sys.executable, "-c", "import time; time.sleep(99)"])
    assert ok is False


def test_run_step_succeeds_inside_budget(monkeypatch):
    """A fast step passes through; timeout doesn't trip."""
    class _Result:
        returncode = 0

    monkeypatch.setattr(RP.subprocess, "run", lambda *a, **kw: _Result())
    ok = RP.run_step("Quick step", ["/bin/true"])
    assert ok is True


def test_run_step_dry_run_skips_subprocess(monkeypatch):
    """Dry-run mode must not call subprocess at all."""
    called = {"n": 0}

    def boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(RP.subprocess, "run", boom)
    assert RP.run_step("anything", ["/bin/true"], dry_run=True) is True
    assert called["n"] == 0


def test_run_step_nonzero_exit_returns_false(monkeypatch):
    class _Result:
        returncode = 1

    monkeypatch.setattr(RP.subprocess, "run", lambda *a, **kw: _Result())
    assert RP.run_step("Broken step", ["/bin/false"]) is False


def test_per_step_timeout_overrides_default():
    """The known long-running steps must have explicit timeouts so a single
    blanket default doesn't kill them prematurely."""
    assert RP.STEP_TIMEOUTS_S["Sentry-driven QC actions"] >= 180
    assert RP.STEP_TIMEOUTS_S["Sync to ol-quote-tracker"] >= 120
    assert RP.STEP_TIMEOUT_S <= 600  # keep the whole pipeline within the cron window
