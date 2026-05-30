"""Smoke test for QC-053's deployment-drift red flag in the audit.
The full QC-053 logic lives in qc_selfheal.py; here we exercise the
audit-side collector that parses reports/deployment-sha.txt and surfaces
a 🔴 RED FLAG when the marker indicates the production checkout is
behind origin/main.

Why this matters: Michael's 2026-05-28 audit ("how is this possible")
revealed that a feature branch with 4 production fixes sat unmerged for
5 days while the daily audit kept reporting the SAME problems. The
audit was blind to "the code that should fix this isn't deployed".
This collector ensures that class of failure shows up loudly in the
audit Michael actually reads."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gen_improvements_report as G  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "REPORTS", tmp_path)
    monkeypatch.setattr(G, "ROOT", tmp_path.parent)


def test_audit_flags_stale_deployment(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "deployment-sha.txt").write_text(
        "HEAD=abc1234 BEHIND=3 AT=Thu 05/28/2026 10:01:00.00\n"
    )
    flags = G.collect_red_flags({"requests": []}, {}, {})
    drift = [f for f in flags if "stale code" in f["title"].lower()
             or "behind main" in f["title"].lower()]
    assert len(drift) == 1
    assert "3 commit" in drift[0]["title"]
    assert "git pull" in drift[0]["detail"]


def test_audit_silent_when_deployment_current(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "deployment-sha.txt").write_text(
        "HEAD=abc1234 BEHIND=0 AT=Thu 05/28/2026 10:01:00.00\n"
    )
    flags = G.collect_red_flags({"requests": []}, {}, {})
    drift = [f for f in flags if "stale code" in f["title"].lower()
             or "behind main" in f["title"].lower()]
    assert drift == []


def test_audit_silent_when_marker_absent(tmp_path, monkeypatch):
    """No marker = nothing to say. The QC check itself will WARN if the
    marker is missing on a production host; the audit's red-flag section
    stays quiet (don't double-fire)."""
    _isolate(tmp_path, monkeypatch)
    flags = G.collect_red_flags({"requests": []}, {}, {})
    drift = [f for f in flags if "stale code" in f["title"].lower()
             or "behind main" in f["title"].lower()]
    assert drift == []
