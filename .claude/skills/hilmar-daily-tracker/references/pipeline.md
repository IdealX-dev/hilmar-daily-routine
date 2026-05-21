# Hilmar Daily Tracker — the pipeline

`scripts/run_pipeline.py` defines the ordered `STEPS` list. It fires every
weekday at **10:00 AM ET** on the Windows Cloud PC. Each step is a separate
Python script invoked as a subprocess; the orchestrator wraps the whole run
in a Sentry Cron heartbeat + per-step performance spans.

## Why 10 AM ET (and why the email reports "yesterday")

Lonny's California office (Pacific Time) opens ~3 hours after the 10 AM ET
fire. At fire time, *today's* data window is empty. So the daily email
reports the **previous business day** (Mon → last Friday; Tue–Fri →
yesterday) plus period-to-date rollups. Never label cumulative numbers as
"today".

## The 16 steps (in order)

| # | Step | Script | What it does |
|---|---|---|---|
| 1 | Backup snapshot | `backup.py` | Timestamped copy of `tracking-data-v2.json` → `data-backups/`. Retention-pruned. |
| 2 | Ingest | `ingest.py` | Staged emails → request rows. Builds `tracking-data-v2.json`. |
| 3 | Drift check (pre-QC) | `drift_check.py --auto-heal` | 6-phase integrity check. FAILs + stops the pipeline if quote-rate < 80%. Auto-heals dup imids + NQ schema. |
| 4 | QC self-heal (pre-patch) | `qc_selfheal.py` | QC before enrichment. `HILMAR_QC_PHASE=pre-patch` suppresses Sentry for findings the patch step fixes. |
| 5 | Carrier enrichment patch | `patch_carriers.py` | 4-pass backfill of carrier / rate / ETD / vessel / ERD / free-time, incl. booking-PDF extraction. |
| 6 | QC self-heal (post-patch) | `qc_selfheal.py` | The real shipped-state QC. `HILMAR_QC_PHASE=post-patch` — this run fires Sentry. |
| 7 | Sentry-driven QC actions | `qc_actions_from_sentry.py --apply` | Polls unresolved Sentry issues, dispatches remediation per the ACTIONS table. |
| 8 | Sentry Seer autofix trigger | `sentry_seer.py trigger --apply` | Asks Seer to attempt autofix on recent error-level issues. |
| 9 | Dashboard HTML | `gen_dashboard.py` | Interactive dashboard → `reports/hilmar-dashboard.html`. |
| 10 | Client PDF (6-page) | `gen_pdf.py` | → `reports/hilmar-report.pdf`. |
| 11 | Carrier scorecard PDFs | `gen_carrier_scorecard_pdf.py` | Per-carrier negotiation scorecards. |
| 12 | Email body HTML | `gen_email.py` | → `reports/email-body.html` + `email-subject.txt`. |
| 13 | Share to client_intelligence | `share_intel.py export` | Pushes Hilmar data to the shared cross-project intelligence store. |
| 14 | Rate intelligence | `gen_rate_intelligence.py --quiet` | Rate-negotiation cheat sheet + cooling/regression alerts. |
| 15 | Sync to ol-quote-tracker | `sync_to_quote_tracker.py` | Pushes entities to ol-quote-tracker's Turso registry. No-op if password absent. |

The **email send** is a separate step after this list — `outlook_send.py`
delivers `reports/email-body.html` + attachments to the distribution.

## What the daily email contains

A "What Happened" cover block (4 tables: New Requests, OL-USA Responses,
Status Changes, Pending Hilmar Response), per-day + period-to-date KPI tiles,
Week-over-Week, Carrier Performance, Volume by Trade Region, Top Winning
Lanes, Top Losing Lanes, Not Quoted detail, and Pending Hilmar Response.

## The two QC phases — why there are two

`patch_carriers.py` (Step 5) backfills fields that ingest couldn't extract on
its own. If QC measured accuracy *before* the patch, it would see an
artificially low number and fire false Sentry alerts. So:

- **Pre-patch QC** (Step 4) runs on the raw ingest state. `HILMAR_QC_PHASE=pre-patch`
  tells `qc_selfheal.py` to NOT emit Sentry events.
- **Post-patch QC** (Step 6) runs on the real shipped state. `HILMAR_QC_PHASE=post-patch`
  — this is the run that fires Sentry events + powers the audit email.

## Failure handling

- `drift_check.py` exits non-zero on a hard FAIL → the orchestrator stops the
  pipeline before bad data reaches the email.
- Other steps are wrapped so a single non-fatal failure (e.g. the
  ol-quote-tracker reconcile timing out) is logged but does NOT abort the
  run — the daily email still ships.
- Every step failure → Sentry `pipeline.step_failure` event → routed through
  `qc_actions_from_sentry.py` → Seer or Claude diagnosis.
- Sentry Crons heartbeat: if the scheduled fire never starts (Cloud PC down,
  scheduler dead), Sentry fires a "missed check-in" alert — the only signal
  that survives a total pipeline-didn't-run failure.

## Where things land

| Artifact | Path |
|---|---|
| Canonical data | `tracking-data-v2.json` |
| Backups | `data-backups/tracking-data-v2*.json` |
| Dashboard | `reports/hilmar-dashboard.html` |
| Client PDF | `reports/hilmar-report.pdf` |
| Email body / subject | `reports/email-body.html`, `reports/email-subject.txt` |
| Private audit | `reports/improvements-report.html` |
| QC result | `reports/qc-result.json` |
| Sentry QC actions log | `reports/qc-actions-from-sentry.json` |
| Staged emails | `scripts/stage_emails.txt`, `scripts/stage_emails_bodies.txt` |
| Booking PDFs | `scripts/stage_pdfs/` |
