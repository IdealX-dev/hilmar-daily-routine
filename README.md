# Hilmar Daily Tracker — claude.ai routine deployment

Standalone Hilmar daily-email pipeline. Cloud-scheduled via claude.ai routines (the `/v1/code/triggers` API). Independent of any user device.

**Not** related to rate-blaster. **Not** related to the deprecated Azure VM. Treat as standalone.

## What this repo holds

```
scripts/         # The pipeline — refresh_stage → ingest → QC → render → send
config.json      # Distribution list, paths, rules. Paths auto-heal on session.
schema.json      # JSON Schema for tracking-data-v2.json
requirements.txt # reportlab, msal, requests, tzdata
```

## What lives in OneDrive (NOT in git)

```
tracking-data-v2.json   # Live request state — written by ingest each run
scripts/stage_emails*.jsonl   # Outlook fetch cache — refresh_stage appends
data-backups/           # 14-snapshot rotation kept by qc_selfheal
reports/                # Daily artifacts (HTML, PDF, scorecards)
secrets/                # MSAL token cache (chmod 600)
```

The routine reads/writes those via the M365 MCP each fire — never via git.

## How the routine works each fire

1. Clones this repo into the sandbox.
2. Reads `tracking-data-v2.json` + stage files from OneDrive (M365 MCP).
3. Refreshes stage from Outlook via Microsoft Graph (using the cached MSAL token from secrets/, which lives in OneDrive too).
4. Runs `python scripts/run_pipeline.py` — full pipeline (ingest → QC → carrier patch → QC → dashboard → PDF → scorecards → email body).
5. Sends the daily email via Outlook to the 9-recipient distribution.
6. Uploads the updated tracking-data + reports back to OneDrive.
7. Archives yesterday's artifacts into `reports/history/<YYYY-MM-DD>/`.

## Deployment steps (next session, one-time)

1. **Create the GitHub repo** at `github.com/IdealX-dev/hilmar-daily-routine` (private). Set its `main` branch as the default.
2. **Push this directory** to it:
   ```
   cd "C:\Users\MichaelDeitchman\OneDrive - IdealX\claude\PROJECT HILMAR\hilmar-daily-routine"
   git remote add origin https://github.com/IdealX-dev/hilmar-daily-routine.git
   git branch -M main
   git push -u origin main
   ```
3. **Fire the RemoteTrigger create** from a Claude Code session — full body in `DEPLOY-ROUTINE-2026-05-05.md`.
4. **Manual test fire** the new routine (one-shot, not on cron yet) — confirm a clean dry-run.
5. **Enable the cron** `30 11 * * 1-5` (UTC = 7:30 AM ET weekdays).
6. **Disable Cowork's `hilmar-rate-desk-daily`** to avoid duplicate sends.

## Hard scope rules (encoded in the routine prompt)

- Touch only this repo. No rate-blaster, no Azure, no other clients' bookings.
- Refuse any out-of-scope prompts; ask the user to use a separate session.
- Never device-code-prompt from a cron context. If MSAL token expired, email Michael and stop.
- Never overwrite filled fields with None (additive merge guards this in ingest).

## Standing rules (per Michael)

- **data is life** — every change goes through QC and self-heal; never silently mutate.
- Any new code pattern ships its QC + self-heal counterpart in the same commit.
- Times reported to Michael in chat are ET (DST-aware via zoneinfo).

— prepared 2026-05-05 EOD by laptop Code session, ready to push.
