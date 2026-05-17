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

## Cost ceiling

Sentry free tier: 10K events/month. Hilmar produces:

- ~14 transactions per daily fire × 21 fires/month ≈ 300 events/month from transactions
- ~5-15 QC events on a bad day × 21 ≈ 100-300 events/month worst case
- Burst on a real outage: maybe 50 events in an hour

Comfortably inside free tier with 5-10× margin. Paid Team plan ($26/mo)
gives 100K events + custom alerts + Slack integration if/when needed.

## Alert routing (defer until first noisy week)

Currently no alerts are configured — Sentry just collects events into the
issues view. Next step (defer until we see how the signal-to-noise plays
out over the first week of operation):

1. Sentry → Alerts → Create Alert
2. Set up:
   - **High signal alert**: notify `michael.deitchman@idealx.us` on every NEW issue with severity=error (so a never-seen-before error pages you immediately)
   - **Frequency alert**: notify on >5 events of same issue in 1h (catches loops)
3. Optional: configure Sentry → Integrations → Microsoft Teams to route to the same Teams channel the daily audit uses
