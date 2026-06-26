"""Every out-of-band alarm path must be able to reach Teams (the phone).

Wiring guard for the 2026-06 "alarms filed but never delivered" gap: a real
fire miss only ever filed a GitHub issue nobody watches. The three out-of-band
alarm paths must ALL POST to `config.alerts.teams_webhook_url` so an alarm
reaches the operator's phone the moment the webhook is set:

  - fire_alert.py            (box side: integrity assertion / preflight alarms)
  - liveness.yml             (GitHub-side: a missed daily fire)
  - heartbeat.yml sentinel   (GitHub-side: fire shipped on a drifted env)

This test fails if any of the three loses its Teams page. It does NOT require a
webhook to be configured (delivery is the operator's one manual step) -- it
asserts the wiring is present and, for liveness, gated on an actual miss.
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _job_steps(workflow_rel: str, job: str) -> list:
    d = yaml.safe_load((ROOT / workflow_rel).read_text(encoding="utf-8"))
    return d["jobs"][job]["steps"]


def test_fire_alert_keeps_its_teams_channel():
    src = (ROOT / "scripts" / "fire_alert.py").read_text(encoding="utf-8")
    assert "def _teams" in src and "teams_webhook_url" in src, (
        "fire_alert.py must keep its Teams channel (_teams() reading "
        "config.alerts.teams_webhook_url) -- the box-side out-of-band alarm."
    )


def test_liveness_pages_teams_on_a_detected_miss():
    steps = _job_steps(".github/workflows/liveness.yml", "check-heartbeat")
    teams = [s for s in steps if "teams_webhook_url" in s.get("run", "")]
    assert teams, (
        "liveness.yml must POST to config.alerts.teams_webhook_url when it "
        "detects a missed fire -- otherwise a real miss never reaches the phone."
    )
    assert any("no_fire == 'true'" in s.get("if", "") for s in teams), (
        "the liveness Teams page must be gated on an actual miss "
        "(if: steps.check.outputs.no_fire == 'true')."
    )


def test_env_drift_sentinel_pages_teams():
    steps = _job_steps(".github/workflows/heartbeat.yml", "record")
    assert any("teams_webhook_url" in s.get("run", "") for s in steps), (
        "heartbeat.yml's env-drift sentinel must POST to teams_webhook_url so a "
        "fire that ships on a drifted box also reaches the phone."
    )
