# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is
Standalone daily email pipeline for Hilmar Ingredients shipment tracking. Every weekday at 10:00 AM ET a Cloud PC (Win365, always-on) runs the full pipeline: pulls fresh Outlook emails (Lonny Upfold's requests + OL booking confirmations) via Microsoft Graph, runs ingest → drift check → QC self-heal → enrichment → dashboard → PDFs → email body, and sends a single email with HTML dashboard + 6-page PDF + per-carrier scorecards to the 9-recipient distribution. A 10th systems-audit email goes to Michael only.

This repo is **independent from `hilmar-tracker`**. They share a domain (Hilmar) but not code, schemas, or deployment. Live data lives in OneDrive (`tracking-data-v2.json` and the `reports/` outputs are gitignored). See `README.md`.

## Stack
- Python 3.11+ (verified at setup)
- Runtime libs: `reportlab` (PDF), `requests` (HTTP), `msal` (token cache), `tzdata` (DST-safe zones)
- Pure stdlib in `core.py`: only `json`, `re`, `pathlib`, `dataclasses`, `zoneinfo`, `datetime`, `hashlib`
- Windows Task Scheduler on a Cloud PC (primary) + MBD-TRAVEL laptop (fallback, currently disabled)
- No `.env` — all knobs live in `config.json` and `schema.json`

## Layout
- `config.json` (v6.0-claude-native) — client / provider / paths / distribution / business rules / mailbox filters
- `schema.json` — JSON Schema v7 for `tracking-data-v2.json` (request fields, summary, lanes, carriers)
- `core.py` — pure functions: status rules, TEU math, business-hours-ET turnaround, dedup hash (`conversationId + request_timestamp + destination`), DST-safe timezones via `zoneinfo`
- `scripts/` — pipeline stages, orchestrated by `run_pipeline.py`:
  1. `backup.py` (rotating 14 snapshots in `data-backups/`)
  2. `ingest.py` (Outlook → request JSON, **additive merge** — preserves prior wins)
  3. `drift_check.py` (6-phase QC audit: 80% quote-rate floor, schema, dup detection, auto-heal)
  4. `qc_selfheal.py` (run twice: pre- and post-patch; recomputes summaries)
  5. `patch_carriers.py` (idempotent enrichment — only fills missing fields)
  6. `gen_dashboard.py` (interactive HTML)
  7. `gen_pdf.py` (6-page client report via reportlab)
  8. `gen_carrier_scorecard_pdf.py` (per-carrier PDFs)
  9. `gen_email.py` (HTML email body)
- `scripts/refresh_stage.py` — fetch new Outlook emails into `stage_emails*.jsonl` cache (run before pipeline)
- `scripts/outlook_send.py` — Graph send with daily idempotency flag (`sent-YYYY-MM-DD.flag`)
- `scripts/qc_alert_if_needed.py` — alert Michael if QC drifts from CLEAN
- `scripts/gen_improvements_report.py` — daily systems audit, Michael only
- `deploy/setup_cloudpc.ps1` — one-time Cloud PC provisioning (verifies OneDrive sync + Python, installs deps, registers Task Scheduler trigger)
- `deploy/run_daily_laptop.cmd` — wrapper that chains refresh_stage → run_pipeline → outlook_send → qc_alert → improvements_report

## Run
```bash
# Full pipeline (ingest + QC + PDFs + email body)
python3 scripts/run_pipeline.py

# Reuse already-staged emails, just regenerate artifacts
python3 scripts/run_pipeline.py --skip-ingest

# Show steps without executing
python3 scripts/run_pipeline.py --dry-run

# Refresh staged emails from Outlook (14-day lookback)
python3 scripts/refresh_stage.py --days-back 14

# Send the daily email manually
python3 scripts/outlook_send.py daily --to-from-config \
  --subject-from-file reports/email-subject.txt \
  --body-from-file reports/email-body.html \
  --attach reports/hilmar-dashboard.html reports/hilmar-report.pdf

# Cloud PC provisioning (one-time, RDP required)
PowerShell -ExecutionPolicy Bypass -File deploy/setup_cloudpc.ps1
```

There is no test runner in this repo. The QC + drift-check stages are the validation layer.

## Auth and config
- MSAL silent refresh from `secrets/token-cache.json` (chmod 600, OneDrive-synced). `CLIENT_ID` / `TENANT` / `SCOPES` are hardcoded in `outlook_send.py`. **No interactive device-code flow** — the scheduler runs unattended; if the cache expires, refresh on the Cloud PC manually.
- OneDrive folder for live data + reports is identified by ID `01JZE2M6FTFSWA3VUUKNAKRUE56FNH44ZP` in `config.json`.
- Timezone: ET (`America/New_York`) for business hours and turnaround math; PT for client contact times.

## Project-specific guardrails
- **Data is life.** Every change to `tracking-data-v2.json` must be paired with QC + self-heal in the same step. Never silently mutate the data file.
- **Additive ingest merge.** `ingest.py` preserves prior wins that aren't in the fresh stage (Outlook can drop messages from the search window). Don't replace the merge with an overwrite.
- **Drift check before QC, not after.** The 6-phase audit fails the pipeline if quality gates aren't met — that's intentional. Don't move it later in the chain to "let bad data through and fix it downstream."
- **Daily idempotency.** `outlook_send.py` writes `sent-YYYY-MM-DD.flag` and refuses to resend the same day. Honor it; don't add a `--force` path without thinking through the duplicate-email blast radius.
- **Escalation cooldown is 24h, rate-trend threshold is 10%.** These live in `config.json`; change them there, not inline.
- **Touch only this repo.** Don't reach into `hilmar-tracker` or other sibling projects from here.
