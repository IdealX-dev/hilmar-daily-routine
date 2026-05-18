# Sentry Observability — Runbook

The Hilmar Daily Tracker ships every QC ERROR, every pipeline-step failure,
and every uncaught Python exception to Sentry in real time. This is the
real-time channel that complements the once-a-day audit email.

## Where to look

- **Sentry org URL**: https://o4511407070904320.sentry.io (created 2026-05-17)
- **Project slug**: `hilmar-daily-tracker`
- **Issues view**: https://o4511407070904320.sentry.io/issues/ — alerts grouped by check name + error type
- **Performance view**: shows the per-step duration of every pipeline run as transactions

## What gets sent

| Source | Event | Severity |
|---|---|---|
| `run_pipeline.py` | Any step exits non-zero | `error` |
| `run_pipeline.py` | Full pipeline run (14 steps as child spans) | `info` transaction |
| `qc_selfheal.py` `log.error()` | Any of the 8 ERROR-severity QC checks tripping | `error` |
| `qc_selfheal.py` `log.warn()` | QC-039 / QC-040 / QC-041 + parser-accuracy warnings | `warning` |
| `outlook_send.py` | Uncaught exception during send | `error` |
| `sync_to_quote_tracker.py` | Auth or sync failure (network errors caught + audit-logged separately) | `error` |
| `reconcile_with_quote_tracker.py` | Same | `error` |

Every event has these tags:

- `component`: which script (run_pipeline, qc_selfheal, outlook_send, …)
- `pipeline_run_id`: 12-char hex; groups all events from one pipeline fire
- `environment`: `production` (Cloud PC) / `manual` (local) / `codespaces`
- `release`: `hilmar-daily-tracker@<git-short-sha>`

QC-specific events also have:

- `qc_check`: the QC-NNN identifier (issues group by this — one Sentry issue per check, not per row)

## PII Scrubbing — what NEVER leaves the machine

The `before_send` hook in `scripts/sentry_setup.py` scrubs the following
patterns from every event payload (message, exception text, stack-frame
locals, breadcrumbs, tags, contexts):

| Pattern | Replacement |
|---|---|
| Any email address `name@domain.tld` | `[EMAIL_REDACTED]` |
| MDOLX booking refs (`MDOLX260622`, `MDOLM260100`, etc.) | `[MDOLX_REDACTED]` |
| Carrier booking refs (NAM/RICG/ONEY/EBKG/MAEU/...) | `[CARRIER_REF_REDACTED]` |
| Internet message-IDs (`<random@server>`) | `[IMID_REDACTED]` |
| Outlook conversation IDs (`AAQ...` base64 blobs) | `[CONV_REDACTED]` |
| Internal `req_HEX` request IDs | `[REQ_ID]` |

`send_default_pii=False` is set so the SDK doesn't auto-capture cookies,
headers, or local-variable values containing what it heuristically detects
as PII. We control what gets sent explicitly.

## Configuring the DSN

The DSN is read at init time from (in order):

1. `secrets/sentry-dsn.txt` (gitignored — same pattern as `quote-tracker-pwd.txt`)
2. Environment variable `SENTRY_DSN`

Pipeline is a no-op (silent) when neither is configured — observability
must never block the daily ship.

To rotate the DSN:
1. Generate new DSN in Sentry UI → Settings → Projects → hilmar-daily-tracker → Client Keys
2. Replace contents of `secrets/sentry-dsn.txt`
3. Next pipeline fire picks it up automatically (no restart needed)

## Verifying the integration

```bash
# Send a test event to confirm DSN + scrubber work
py -3 scripts/sentry_setup.py

# Expected: prints "Test event sent. event_id=...", check Sentry UI for
# the message with [EMAIL_REDACTED] and [MDOLX_REDACTED] substituted in.
```

## Common alerts you might see

| Issue | Most likely cause | First step |
|---|---|---|
| `QC-039: parser accuracy ...` | One of the critical fields dropped below 98% | Run `py -3 scripts/parser_accuracy.py` to see per-field breakdown |
| `QC-040: cross-folder drift ...` | Someone changed an enum in scripts/core.py but not src/hilmar/core.py (or vice versa) | Align the enums; both folders MUST match per standing rule |
| `QC-041: CLASSIFIER DRIFT` | tracking-data-v2.json has rows in mixed LEGACY/STRICT form | Parser bug — at least one ingest pass wrote the wrong form. Re-check `core.decide_status` outputs. |
| `pipeline.step_failure` | A specific pipeline step exited non-zero | Look at `component` tag for which script; check the daily email or stdout from the Cloud PC scheduled-task log |
| MSAL / Graph auth errors | Token cache expired or Conditional Access changed | Re-run `outlook_send.py auth` interactively to refresh device-code token |
| `requests.exceptions.ConnectionError` from `sync_to_quote_tracker` / `reconcile` | ol-quote-tracker-prod Azure App Service hiccup | These have graceful-degradation (audit-log + return 0); pipeline continues. If persistent, check Azure portal |

## Cron monitor — silent-failure detection

The `hilmar-daily-pipeline` monitor catches the scenario where the
SCHEDULER itself breaks (Cloud PC offline, task scheduler crashes,
wrapper crashes before any Python code runs). Plain error-event capture
can't catch this because no code is running to report.

**Schedule:** `0 10 * * 1-5` America/New_York (Mon-Fri 10 AM ET)
**Margin:** 30 min — alert if start check-in doesn't arrive within 30 min of scheduled fire
**Max runtime:** 60 min — alert if pipeline runs >60 min (typical is 30-60s)

How it works:
1. `run_pipeline.py main()` calls `sentry_setup.start_cron_checkin()` at the
   start of every fire → returns a check_in_id.
2. Pipeline runs.
3. `run_pipeline.py main()` calls `sentry_setup.finish_cron_checkin(id, ok=True/False)`
   at the end with success/error status.
4. If Sentry doesn't see the start check-in within 30 min of 10 AM ET on
   any weekday → "missed check-in" alert.
5. If Sentry sees a start but no finish within 60 min → "max runtime exceeded".
6. If finish status=error → "run failed" alert.

The monitor auto-provisions on first check-in — no Sentry UI setup
needed. Configuration lives in `sentry_setup.py:_MONITOR_CONFIG`.

To change the schedule, edit that dict and the next check-in updates
the monitor in Sentry.

## Custom metrics — KPI trends

These metrics emit on every pipeline fire (paid Sentry tier required
for retention; free tier collects but doesn't trend over time):

### Parser quality (from qc_selfheal.py QC-039)
- `parser.accuracy_overall` (gauge) — overall accuracy across all fields
- `parser.accuracy_weighted` (gauge) — weighted by applicable-row count
- `parser.accuracy_per_field` (gauge, tagged `field=<name>` + `critical=true/false`)
  one row per field per run

### Pipeline performance (from run_pipeline.py)
- `pipeline.duration_s` (distribution) — total wall time
- `pipeline.step_duration_s` (distribution, tagged `step=<name>` + `status=ok/failed`)
  per-step duration heatmap
- `pipeline.status` (counter, tagged `status=ok/failed`)

### QC activity (from qc_selfheal.py Log class)
- `qc.errors` (counter, tagged `check=<QC-NNN>` + `phase=pre-patch/post-patch`)
- `qc.warnings` (counter, tagged `check=<QC-NNN>` + `phase=...`)
- `qc.fixes` (counter, tagged `phase=...`) — how often self-heal applies fixes

### Email send health (from outlook_send.py)
- `send.success` (counter, tagged `recipient_type=full/audit/test` + `attach_count`)
- `send.failure` (counter, tagged `recipient_type` + `status_code`)

### ol-quote-tracker reconciliation (from reconcile_with_quote_tracker.py)
- `reconcile.qt_wins` (gauge, tagged `window_days`)
- `reconcile.hilmar_wins` (gauge)
- `reconcile.drift_count` (gauge) — Hilmar-QT difference in win count
- `reconcile.drift_teu` (gauge) — same for TEU
- `reconcile.lanes_matched` (gauge)
- `reconcile.lanes_total` (gauge)

### Dashboard widgets (build in Sentry UI: Dashboards → New)

Recommended Hilmar KPI dashboard layout:

| Row | Widget | Query / metric |
|---|---|---|
| 1 | Parser Accuracy (90d) | `parser.accuracy_overall` line, phase=post-patch |
| 1 | Pipeline Duration (90d) | `pipeline.duration_s` p50+p95 line |
| 2 | QC Errors by Check (30d) | `qc.errors` stacked-bar, group by `check` |
| 2 | Per-field Accuracy (latest) | `parser.accuracy_per_field` table, sort by rate ascending |
| 3 | Reconcile Drift (60d) | `reconcile.drift_count` line, `reconcile.drift_teu` line (dual-axis) |
| 3 | Send Success Rate | `send.success` / (`send.success`+`send.failure`) ratio |
| 4 | Step Duration Heatmap | `pipeline.step_duration_s` p95 by `step` |
| 4 | Cron Status | monitor status board for `hilmar-daily-pipeline` |

### Recommended metric alerts (paid tier)

- `parser.accuracy_overall` < 0.98 for 2 consecutive runs → ERROR
- `pipeline.duration_s` > 2× rolling 7-day median → WARN
- `send.failure` > 0 → ERROR (any send failure)
- `reconcile.drift_count` > 5 for 3 consecutive runs → WARN
- `qc.errors` count > 0 grouped by check_name (any new error) → already covered by issue alerts

## Cost ceiling

Sentry free tier: 10K events/month. Hilmar produces:

- ~14 transactions per daily fire × 21 fires/month ≈ 300 events/month from transactions
- ~5-15 QC events on a bad day × 21 ≈ 100-300 events/month worst case
- Burst on a real outage: maybe 50 events in an hour

Comfortably inside free tier with 5-10× margin. Paid Team plan ($26/mo)
gives 100K events + custom alerts + Slack integration if/when needed.

## Alert routing — LIVE since 2026-05-17

Configured rules (Sentry → hilmar-daily-tracker → Alerts):

| Rule | Trigger | Channel | Verified |
|---|---|---|---|
| **New issue notification** | First-ever occurrence of any issue | Email to `michael.deitchman@idealx.us` | ✅ Test event `8c27d26c69c34498a79de4834a4db44c` delivered 2026-05-17 6:35 PM ET |
| **Frequency storm** | Same issue ≥5 times in 1h | Email to `michael.deitchman@idealx.us` | ✅ Configured (untested — fires only on real outage) |

Sender: `noreply@sentry.io`. Already on Michael's Outlook safe-senders list (rich content renders inline).

To add Microsoft Teams routing later (optional, when free-tier email volume becomes inconvenient):
1. Sentry → Settings → Integrations → Microsoft Teams → Install
2. Connect to the IdealX tenant Teams workspace
3. Sentry → Alerts → edit each rule → add Teams channel as additional notification target
