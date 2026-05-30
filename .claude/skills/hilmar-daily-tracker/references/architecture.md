# Hilmar Daily Tracker — architecture

## Data model

One row per Lonny RFQ in `tracking-data-v2.json` → `requests[]`. Status state
machine:

```
Lonny sends RFQ ─────────────► PENDING
OL responds with a rate ─────► (quoted=True)
   Lonny books ──────────────► WIN     (gets an MDOLX booking number)
   rate goes stale ──────────► Q&L     (Quoted & Lost — lost on price/service)
   OL never responded ──────► NQ       (Not Quoted — loss_reason=NO_RESPONSE)
   still open ───────────────► PENDING (awaiting Lonny's decision)
```

- **WIN** — Hilmar booked through us. `mdolx_ref` is the booking number.
  Some WINs are "send-signal" promotions (Lonny said "send" before the MDOLX
  confirmation landed) — those show "Awaiting MDOLX" until the booking email
  arrives. The matcher uses email `In-Reply-To` / `References` headers to
  link a booking confirmation back to the originating RFQ thread.
- **Q&L** — a real competitive loss. OL quoted, Lonny chose a competitor.
- **NQ** — OL never responded. Not a price loss; a no-contest.
- **PENDING** — not decided yet.

**Win Rate = Wins / (Wins + Q&L)** — competitive conversion. NQ is excluded
(no contest happened) and surfaces as a separate "No-Response Rate". Pending
is excluded (not decided).

Standalone WINs (`request_id` like `stand_NNNN`) come from a booking
confirmation with no matching Lonny RFQ in the 30-day window — prior-window
rollovers. They're excluded from rate/ETD accuracy because the source RFQ
isn't in our corpus.

## Parser

`scripts/body_parser.py` (+ `src/hilmar/body_parser.py` mirror) — regex
parsers for email subjects + bodies. `scripts/pdf_parser.py` — extracts
booking fields from OL booking-confirmation PDF attachments.
`scripts/pdf_llm_rescue.py` — Claude-vision fallback for image-only PDFs
that pdfplumber can't read (needs `secrets/anthropic-api-key.txt`).

**Accuracy framework** — `src/hilmar/parser_accuracy.py`:
- `ACCURACY_THRESHOLD = 0.95` — the hard gate
- `FIELD_REQUIREMENTS` — per-field applicability predicates (a field only
  counts toward accuracy on rows where it SHOULD be populated)
- `CRITICAL_FIELDS` — subset that ERRORs the pipeline if below threshold
- `PER_FIELD_THRESHOLDS` — overrides for fields with documented data gaps
- `compute_accuracy(rows)` → per-field + overall + weighted rates

19 measured fields. Current state ~97% overall. Fields legitimately sparse
in source text (rate_expiry, etd_requested, temperature) are tracked but not
gated.

## QC + self-heal — ~46 checks (QC-001 .. QC-050)

`scripts/qc_selfheal.py` runs the full suite. Each check returns PASS / WARN
/ ERROR. ERROR-severity findings gate the pipeline AND fire Sentry events.
Self-heal auto-fixes the safe cases (dedupe, stale-folder cleanup, schema
normalization); risky fixes are flagged for the operator only.

Notable checks:
- **QC-022** — distribution-list invariants (recipient count, no external
  domains, honors the iteration lock)
- **QC-027** — carrier-extraction completeness
- **QC-039** — parser accuracy ≥ 95% gate (ERRORs the pipeline)
- **QC-040** — cross-folder enum drift (`scripts/core.py` ↔ `src/hilmar/core.py`)
- **QC-041** — classifier-form consistency (3-state vs 4-state)
- **QC-042** — data-URI guard (no `data:` URIs in email HTML)
- **QC-043** — Sentry self-improvement loop
- **QC-044** — HTML double-escape guard (`&amp;amp;` detection)
- **QC-045** — table-header visibility (Outlook strips `linear-gradient`)
- **QC-046** — Pending-timestamp population (Windows `strftime` safety)
- **QC-047** — Win Rate KPI ↔ explainer-banner consistency
- **QC-048** — turnaround sanity (flags implausible >40h biz-hours)
- **QC-049** — WIN-rows-missing-MDOLX rate
- **QC-050** — backup freshness + retention
- **QC-052** — daily test/coverage routine result (ERRORs the audit on a
  failed test or coverage below the `pyproject` gate; reads
  `reports/test-result.json` written by `scripts/run_audit_tests.py`)

When shipping a new pattern, add its QC check + self-heal in the same commit
(see the `qc-and-self-heal` skill). New QC checks also get an entry in
`qc_actions_from_sentry.py` `ACTIONS` so Sentry findings route to the right
remediation.

## Observability — Sentry + Seer + Claude

`scripts/sentry_setup.py` — single init point. DSN from `secrets/sentry-dsn.txt`.
PII-scrubbing `before_send` hook (emails, MDOLX, conversation IDs, message
IDs, carrier refs). Cron heartbeat for silent-failure detection. Custom
metrics (parser accuracy, pipeline duration, QC counts, send health).

`scripts/sentry_api.py` — REST API wrapper (auth token in
`secrets/sentry-auth-token.txt`). org `idealx-llc`, project
`hilmar-daily-tracker`.

`scripts/sentry_seer.py` — Seer integration (AI issue summary + autofix).
Endpoints are under `/api/0/organizations/{org}/issues/{id}/...` (the plain
`/issues/{id}/...` path 404s).

`scripts/qc_actions_from_sentry.py` — the closed-loop self-fix engine. On
every fire it polls unresolved Sentry issues and dispatches per the `ACTIONS`
table. Action types:
- `log_only` — comment with the documented remediation
- `resolve_if_post_fix` — resolve if HEAD commit is newer than the issue
- `resolve_if_stale` — resolve if no events in N hours
- `rerun_parser_acc` — recompute parser accuracy + comment
- `flag_for_operator` — ⚠️ comment, stay open
- `trigger_seer` — ask Seer for autofix
- `claude_diagnose` — Claude (haiku-4-5) posts a root-cause diagnosis as a
  Sentry comment. This is the guaranteed fallback when Seer can't analyze an
  issue (Seer needs stack-trace data; `trigger_seer` auto-chains to
  `claude_diagnose` on a Seer 500/404).

Unmapped ERROR-level issues default to `trigger_seer` → `claude_diagnose`.
The audit email's Sentry section is enriched with Seer's diagnosis + autofix
status per issue.

## Cross-project links

- **ol-quote-tracker** (the "rate checker") — Hilmar wins are cross-checked
  against it (Step 16). Shares the `client_intelligence` registry.
- **idealx-intel** — shared parser-agent architecture; the Anthropic API key
  Hilmar uses is sourced from `keys/idealx-intel.env`.
- **rate-blaster** — sibling project, same standing rules (QC-per-commit,
  Sentry-mandatory, ET timestamps, never-greenfield).

## Conventions

- ET in chat output, UTC in code/DB/logs.
- `PYTHONIOENCODING=utf-8` on every Python invocation (Windows cp1252 crash).
- Windows-portable `strftime` — never `%-d` / `%-I` (Unix-only, raises
  `ValueError` on the Cloud PC). Use `%d` / `%I` + `.replace(" 0", " ")`.
- Outlook email rendering: solid `background-color` before any
  `linear-gradient`; escape HTML exactly once; KPI tiles `min-height` +
  `height` together.
- `scripts/` mirrored between the repo and `PROJECT HILMAR/scripts/` — copy
  after every edit; QC-040 enforces no drift.
