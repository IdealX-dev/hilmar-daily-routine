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
| `requests.exceptions.ConnectionError` from `sync_to_quote_tracker` | ol-quote-tracker-prod Azure App Service hiccup | Graceful-degradation (audit-log + return 0); pipeline continues. If persistent, check Azure portal |

## Cron monitor — silent-failure detection

The `hilmar-daily-pipeline` monitor catches the scenario where the
SCHEDULER itself breaks (Cloud PC offline, task scheduler crashes,
wrapper crashes before any Python code runs). Plain error-event capture
can't catch this because no code is running to report.

**Schedule:** `7 18 * * 1-5` America/New_York (Mon-Fri 6:07 PM ET)
**Margin:** 290 min — alert if the start check-in doesn't arrive within 290 min of the scheduled fire (~10:57 PM ET). The wide margin absorbs GitHub-cron lateness (observed 30 min–4.5h) plus the full evening liveness-backstop window, so Sentry only pages when the pipeline truly never ran all day.
**Max runtime:** 60 min — alert if pipeline runs >60 min (typical is 30-60s)

How it works:
1. `run_pipeline.py main()` calls `sentry_setup.start_cron_checkin()` at the
   start of every fire → returns a check_in_id.
2. Pipeline runs.
3. `run_pipeline.py main()` calls `sentry_setup.finish_cron_checkin(id, ok=True/False)`
   at the end with success/error status.
4. If Sentry doesn't see the start check-in within 290 min of 6:07 PM ET on
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

### Dashboard widgets (build in Sentry UI: Dashboards → New)

Recommended Hilmar KPI dashboard layout:

| Row | Widget | Query / metric |
|---|---|---|
| 1 | Parser Accuracy (90d) | `parser.accuracy_overall` line, phase=post-patch |
| 1 | Pipeline Duration (90d) | `pipeline.duration_s` p50+p95 line |
| 2 | QC Errors by Check (30d) | `qc.errors` stacked-bar, group by `check` |
| 2 | Per-field Accuracy (latest) | `parser.accuracy_per_field` table, sort by rate ascending |
| 3 | Send Success Rate | `send.success` / (`send.success`+`send.failure`) ratio |
| 4 | Step Duration Heatmap | `pipeline.step_duration_s` p95 by `step` |
| 4 | Cron Status | monitor status board for `hilmar-daily-pipeline` |

### Recommended metric alerts (paid tier)

- `parser.accuracy_overall` < 0.98 for 2 consecutive runs → ERROR
- `pipeline.duration_s` > 2× rolling 7-day median → WARN
- `send.failure` > 0 → ERROR (any send failure)
- `qc.errors` count > 0 grouped by check_name (any new error) → already covered by issue alerts

## Sentry Seer — live 2026-05-19 PM

Per Michael: "assume you are now locked with sentry for auto fix and
seer." Seer is enabled in the hilmar-daily-tracker project; the
pipeline now uses it actively.

### Three integration points

1. **Auto-trigger on recent errors.** `scripts/sentry_seer.py trigger
   --apply` runs at the end of every daily fire (between
   `qc_actions_from_sentry` and `Dashboard HTML`). For each error-level
   issue that fired in the last 2 hours and doesn't already have an
   in-progress or completed autofix, it asks Seer to attempt one. Seer
   chews on it asynchronously; the next-day audit shows the result.

2. **Audit-email enrichment.** `gen_improvements_report._sentry_section_inline`
   calls `sentry_seer.enrich_audit_with_seer` on the issue list before
   rendering. Each issue row now shows two new lines beneath the title:
     - `🤖 Seer: <plain-English diagnosis>` (Issue Summary API)
     - `🛠️ Autofix: <STATUS>` colored by state, with optional confidence %
       and PR link when Seer proposed code
   Statuses: COMPLETED (green), PROCESSING (blue), ERROR (red),
   NEED_MORE_INFORMATION (amber).

3. **Default for unmapped errors.** `qc_actions_from_sentry.ACTIONS`
   table maps known QC checks to specific remediation actions. For
   issues that DON'T match any mapped key AND have `level=error|fatal`,
   the default action is now `trigger_seer` (was `log_only`). Seer
   becomes the safety net for novel error patterns we haven't authored
   a remediation playbook for yet. Warning/info-level unmapped issues
   stay at `log_only` to avoid noisy Seer triggers on transient signals.

### Cost model

Seer is included in the paid Sentry Team tier we already use. Per-issue
autofix invocations are metered against the Seer credit pool (separate
from event volume). At current rate (~1 error issue/week worst case),
well within the included allowance.

### Disabling / dialing down

- Remove the `sentry_seer.py trigger --apply` step from `run_pipeline.STEPS`
  to stop auto-triggering. Audit-email enrichment + Sentry-driven QC
  actions still work; only the proactive trigger goes away.
- Set `qc_actions_from_sentry.ERROR_LEVEL_DEFAULT["action"]` back to
  `"log_only"` to undo the unmapped-error → Seer default.
- Or disable Seer in the Sentry UI; every integration point silent
  no-ops when `SentrySeer.enabled == False`.

## Sentry-driven QC actions — Task #11 (live 2026-05-19)

A polling-based "webhook equivalent" — runs as a step in the daily
pipeline, scans recent unresolved Sentry issues, and dispatches QC
remediation actions per the lookup table in
`scripts/qc_actions_from_sentry.py::ACTIONS`.

### Why polling (not webhook)?

Sentry webhooks need a publicly reachable HTTPS endpoint. The Cloud PC
runs behind NAT with no static IP. For a once-a-day pipeline, polling
the Sentry REST API at the start of the post-patch QC phase is
functionally equivalent — issues created since the prior fire get
picked up within 26h. Sub-daily action would need a Cloudflare Worker
or Azure Function endpoint (a deploy task, not a code task).

### Action types

| Type | Behavior |
|---|---|
| `log_only` | Post a Sentry comment with the documented remediation. Leave open. |
| `resolve_if_post_fix` | Resolve if HEAD commit timestamp > issue lastSeen (fix has shipped). |
| `resolve_if_stale` | Resolve if no events in N hours (default 24h). |
| `rerun_parser_acc` | Re-compute parser accuracy + post the result as a comment. |
| `flag_for_operator` | Post a ⚠️ comment. Stay open until human action. |
| `trigger_seer` | Ask Seer to attempt autofix (Seer must be enabled in Sentry UI). |

### Mapped issues (initial set)

| qc_check tag | Action | Comment |
|---|---|---|
| `QC-027` (carrier extraction) | resolve_if_post_fix | Carrier extraction restored — patch_carriers PASS 4 + body_parser. |
| `QC-039` (parser accuracy) | rerun_parser_acc | Re-compute. If still <95%, see docs/PARSER-GAPS.md. |
| `QC-040` (cross-folder drift) | flag_for_operator | Align scripts/core.py and src/hilmar/core.py. |
| `QC-041` (classifier form drift) | flag_for_operator | Mixed 3/4-state status rows. Backup + single-form pass. |
| `QC-042` (data-URI guard) | resolve_if_post_fix | Branding.py uses cid: now. |
| `QC-043` (Sentry self-improvement) | log_only | Meta-issue; informational. |
| `ingest.non_hilmar_filtered` | log_only | Correct NUMIDIA rejection — suppress in Sentry filters if noisy. |

Unmapped issues fall through to `log_only` with a generic comment.

### Pipeline wiring

Inserted in `run_pipeline.py` between `QC self-heal (post-patch)` and
`Dashboard HTML`. The runner writes
`reports/qc-actions-from-sentry.json` so the daily audit email's
Sentry section can show a `🤖 Sentry-driven actions` summary
(handled by `gen_improvements_report._sentry_section_inline`).

### Knobs

- `HILMAR_QC_ACTIONS_DRY_RUN=1` — log what WOULD be done; don't post
  comments or resolve.
- `HILMAR_QC_ACTIONS_LOOKBACK_H` — issue lookback window (default 26h
  = one fire + 2h slack).

### Adding a new action

1. Add a row to `ACTIONS` keyed by `qc_check` tag (or `QC-NNN` if the
   pattern is matched from issue title).
2. Pick one of the existing dispatchers, or add a new one to
   `_DISPATCH`.
3. Include a `comment` so the Sentry comment thread documents what
   happened and why.

## LLM PDF rescue — image-only booking PDFs (live 2026-05-19)

`scripts/pdf_llm_rescue.py` is the Claude-vision fallback for the ~3%
of OL booking PDFs that pdfplumber can't read (image-only scans).

When pdf_parser.parse_booking_pdf gets empty text, it calls Claude's
document-input API (PDF as base64-encoded `document` content block)
with a structured-extraction prompt. The model returns JSON for the
same 17 fields the regex parser produces (mdolx_ref, booking_ref,
carrier_quoted, vessel_voyage, pol, pod, etd_offered, eta_offered,
erd, doc_cutoff, port_cutoff, ol_rate, container_count, containers,
teu_requested, product, temperature, origin_free_time, dest_free_time,
transshipment).

**Caching**: SHA1 of PDF bytes → `data/pdf_llm_cache.json`. Same PDF
never costs twice across runs.

**Budget**: `HILMAR_PDF_LLM_BUDGET` (default 20 calls/run). Beyond
that the rescue silent no-ops. At ~$0.001/PDF on Haiku, 20 calls
is ~$0.02 per fire.

**API key**: `secrets/anthropic-api-key.txt` (gitignored) or
`ANTHROPIC_API_KEY` env. Without it, the rescue is a silent no-op —
the pipeline still runs, image-only PDFs just stay unextracted.

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
