"""Out-of-band alarms must reach Teams -- and SECURELY (no webhook URL in git).

Wiring guard for the 2026-06 "alarms filed but never delivered" gap. The three
out-of-band alarm paths must POST to a Teams Incoming Webhook so an alarm
reaches the operator's phone the moment the webhook is configured:

  - fire_alert.py            (box side: integrity / preflight alarms)
  - liveness.yml             (GitHub-side: a missed daily fire)
  - heartbeat.yml sentinel   (GitHub-side: fire shipped on a drifted env)

SECURITY: the webhook URL is resolved SECRET-FIRST and NEVER from the committed
config.json -- a GitHub Actions secret `TEAMS_WEBHOOK_URL` on the Actions side,
and the `TEAMS_WEBHOOK_URL` env var / gitignored `secrets/teams-webhook-url.txt`
on the box side. This test fails if any path loses its Teams page, or if a
workflow starts sourcing the webhook from config.json (which would commit a
mildly sensitive URL into git history).
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SECRET_REF = "secrets.TEAMS_WEBHOOK_URL"


def _job_steps(workflow_rel: str, job: str) -> list:
    d = yaml.safe_load((ROOT / workflow_rel).read_text(encoding="utf-8"))
    return d["jobs"][job]["steps"]


def _step_env(step: dict) -> dict:
    return step.get("env", {}) or {}


def test_fire_alert_resolves_webhook_secret_first():
    src = (ROOT / "scripts" / "fire_alert.py").read_text(encoding="utf-8")
    assert "def _teams" in src, "fire_alert.py must keep its Teams channel"
    assert "TEAMS_WEBHOOK_URL" in src, "fire_alert.py must read the TEAMS_WEBHOOK_URL env var"
    assert "teams-webhook-url.txt" in src, (
        "fire_alert.py must read the gitignored secrets/teams-webhook-url.txt so the "
        "URL stays out of git (secret-first resolution)."
    )


def test_liveness_pages_teams_from_a_secret_on_miss():
    steps = _job_steps(".github/workflows/liveness.yml", "check-heartbeat")
    teams = [s for s in steps if "TEAMS_WEBHOOK_URL" in _step_env(s)]
    assert teams, "liveness.yml must page Teams (a step with TEAMS_WEBHOOK_URL in its env)"
    s = teams[0]
    assert SECRET_REF in str(_step_env(s)["TEAMS_WEBHOOK_URL"]), (
        "the liveness webhook must come from the TEAMS_WEBHOOK_URL repo SECRET, not config.json"
    )
    assert "no_fire == 'true'" in s.get("if", ""), (
        "the liveness Teams page must be gated on an actual miss (no_fire == 'true')"
    )
    assert "curl" in s.get("run", ""), "the liveness Teams page must actually POST (curl)"


def test_sentinel_pages_teams_from_a_secret():
    steps = _job_steps(".github/workflows/heartbeat.yml", "record")
    teams = [s for s in steps if "TEAMS_WEBHOOK_URL" in _step_env(s)]
    assert teams, "heartbeat.yml sentinel must page Teams (TEAMS_WEBHOOK_URL in its env)"
    assert any(SECRET_REF in str(_step_env(s)["TEAMS_WEBHOOK_URL"]) for s in teams), (
        "the sentinel webhook must come from the TEAMS_WEBHOOK_URL repo SECRET"
    )


def test_workflows_never_source_the_webhook_from_committed_config():
    # Sourcing the URL from config.json in a workflow would commit a (mildly
    # sensitive) webhook into git history. Keep it secret-only.
    for wf, job in [(".github/workflows/liveness.yml", "check-heartbeat"),
                    (".github/workflows/heartbeat.yml", "record")]:
        for s in _job_steps(wf, job):
            assert "teams_webhook_url" not in s.get("run", ""), (
                f"{wf} step '{s.get('name')}' reads the webhook from config.json -- use the "
                "TEAMS_WEBHOOK_URL secret instead so it stays out of git."
            )
