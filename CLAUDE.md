# CLAUDE.md — Hilmar Daily Tracker

Guidance for Claude / other AI assistants working in this repo. Read this
first, then jump to the deeper reference for the area you're touching.

This file complements the in-repo skill (`.claude/skills/hilmar-daily-tracker/`),
the operator runbook (`RUNBOOK.md`), and the inherited handoff doc
(`docs/HANDOFF.md`). When statements conflict, prefer the most specific
source: skill references > this file > README > HANDOFF (the last is
historical from the merged `hilmar-tracker` repo).

---

## 1. What this repo is

Production pipeline for the **OL-USA / Hilmar Ingredients daily shipment
tracker** email + dashboard. Runs unattended at **10:00 AM ET** every
weekday from a Win365 Cloud PC (`CPC-micha-E552L`).

- **Client:** Hilmar Ingredients (contact: Lonny Upfold, `lupfold@hilmaringredients.com`)
- **Provider:** OL-USA (responder mailbox: `MBD_OceanExportBookingShared@ol-usa.com`)
- **Operator:** Michael Deitchman (`michael.deitchman@idealx.us` / `@ol-usa.com`)
- **Repo:** `github.com/IdealX-dev/hilmar-daily-routine` — single source of truth.
  The legacy `IdealX-dev/hilmar-tracker` repo was fully merged 2026-05-17 and
  archived. ONE repo, ONE application.

Pipeline ingests the Lonny ↔ OL email thread via Microsoft Graph, produces:
1. A daily shipment-tracker email to a 10-recipient distribution
2. An interactive HTML dashboard (clickable KPI tiles, tabbed views)
3. A 6-page client PDF + per-carrier scorecard PDFs
4. A private "Daily Systems Audit" email to `michael.deitchman@idealx.us`

## 2. Two code paths — read this before editing

The repo intentionally holds **two parallel Python trees**:

| Tree | Status | What's there | Who uses it |
|---|---|---|---|
| `scripts/` | **ACTIVE production** | The 16-step daily pipeline (`run_pipeline.py` and friends) | Cloud PC daily fire |
| `src/hilmar/` | Inherited mature library | Pytest-compatible modules (`core`, `qc`, `ingest`, `body_parser`, `baselines`, `insights`, `model_router`, `parser_fallback`, `parser_accuracy`, ...) | The 519-test suite + future migration target |

**Tests run against `src/hilmar/`. Production runs against `scripts/`.** The
two coexist on purpose during the migration period; do not delete one to
"clean up" the other.

A few files appear in BOTH places (`body_parser.py`, `core.py`, `ingest.py`,
`parser_accuracy.py`). When you edit one of these, **mirror the change to
the paired file** — QC-040 enforces no drift between the pair, and parser
accuracy is computed from `src/hilmar/parser_accuracy.py`.

## 3. Hard rules (do not violate)

1. **Email sends to the full distribution.** `config.json` →
   `distribution.full_list` has 10 recipients. **Never** send a test/iteration
   email there. Use `distribution.test_list` (Michael only) or pass explicit
   `--to` recipients. During formatting iteration, lock `full_list` to
   `michael.deitchman@idealx.us` only — QC-022 enforces this.

2. **Parser accuracy is a 95% hard gate.** `src/hilmar/parser_accuracy.py`
   `ACCURACY_THRESHOLD = 0.95`. QC-039 blocks the pipeline if overall
   accuracy or any critical field drops below threshold. **Never lower the
   gate to make a red check pass — fix the parser.**

3. **Every new code pattern ships with its QC check + self-heal in the SAME
   commit.** Standing project-wide rule. New mailbox/folder pattern → walk
   it. New scheduled job → staleness check. New API integration → freshness
   check. New email path → bounce check.

4. **Never greenfield.** The Hilmar project was once bifurcated (the old
   `hilmar-tracker` greenfielded as `hilmar-daily-routine`). Refactor the
   existing repo; never create a parallel one.

5. **Mirror scripts after editing.** `scripts/` exists in two places — the
   git repo (`hilmar-daily-routine/scripts/`) and production
   (`PROJECT HILMAR/scripts/`, the OneDrive-synced folder the Cloud PC runs
   from). After any script edit, copy from repo → production. The Cloud PC's
   `deploy/run_daily_laptop.cmd` does a `git pull` + `xcopy` at the start of
   each fire to keep this in sync from the repo side.

6. **Timezones.** Code, logs, database, and timestamps stay in UTC. **Only
   user-facing chat output and email content gets converted to ET**
   (`America/New_York`). Lonny is on Pacific; recipients are mostly Eastern.

7. **`PYTHONIOENCODING=utf-8` on every Python invocation.** OL/Lonny bodies
   contain en-dashes, smart quotes, accented names — default Windows cp1252
   crashes. The CI workflow and Cloud PC wrapper already set this; preserve
   it in any new entry points.

8. **Windows-portable strftime.** Never use `%-d` / `%-I` (Unix-only;
   `ValueError` on the Cloud PC). Use `%d` / `%I` + `.replace(" 0", " ")`.

## 4. Repo layout

```
hilmar-daily-routine/
├── README.md                  Orientation (mobile/remote access too)
├── RUNBOOK.md                 Operator failure-mode playbook
├── CLAUDE.md                  This file
├── config.json                Distribution list, paths, rules, auto-chase, backup
├── schema.json                JSON Schema for tracking-data-v2.json
├── pyproject.toml             Package metadata; test config (pytest, ruff, mypy, 85% cov gate)
├── requirements.txt           Bare runtime deps (Cloud PC wrapper install)
├── requirements-tracker.txt   Library deps mirroring pyproject [project.dependencies]
├── .devcontainer/             Codespaces config (Python 3.12 + Claude Code extension)
├── .github/workflows/test.yml CI: compile, smoke-import, pytest, optional integration
│
├── scripts/                   ACTIVE production pipeline (see §5)
├── src/hilmar/                Inherited mature library (target for §2 migration)
├── tests/                     519-test pytest suite (runs against src/hilmar/)
│   ├── conftest.py            Adds src/ to sys.path; disables LLM fallback
│   └── fixtures/golden_day.json  Pinned schema-clean fixture
├── deploy/                    Cloud PC wrapper, setup PS1, qc_alert_if_needed
├── deploy_legacy/             Old Azure VM deploy chain (kept for reference)
├── docs/                      HANDOFF, INSIGHTS-DESIGN, PARSER-GAPS, SENTRY, SHARED schema
├── assets/branding/           Logo/colors used by gen_pdf/gen_dashboard
├── reports/                   QC-INDEX.md (in git); daily artifacts (gitignored)
├── plugin-build/              Skill plugin packager (regenerated at build time)
└── .claude/skills/hilmar-daily-tracker/  In-repo skill — entry point for cross-device use
```

Runtime state that **never goes in git** (enforced by `.gitignore`):
- `tracking-data-v2.json` — current request state (rebuilt each fire)
- `scripts/stage_emails*.txt` / `stage_emails_bodies*.txt` — Graph fetch cache
- `data-backups/` — rotating snapshots (14 retained, dual-format prune)
- `secrets/` — `token-cache.json` (MSAL), `sentry-dsn.txt`,
  `sentry-auth-token.txt`, `anthropic-api-key.txt`, `quote-tracker-pwd.txt`
- `reports/*` artifacts (HTML dashboard, PDF, scorecards, run-log, sent flags)

## 5. The pipeline (`scripts/run_pipeline.py`)

16 ordered steps fired by `deploy/run_daily_laptop.cmd` at 10:00 AM ET on
the Cloud PC. Each step is a subprocess; the orchestrator wraps the run in
a Sentry Cron heartbeat + per-step Performance spans.

```
1. backup.py                       Snapshot tracking-data-v2.json → data-backups/
2. ingest.py                       Staged emails → request rows
3. drift_check.py --auto-heal      6-phase integrity gate (FAILs at <80% quote-rate)
4. qc_selfheal.py (pre-patch)      QC before enrichment; Sentry-suppressed
5. patch_carriers.py               4-pass carrier/rate/ETD/ERD backfill (+ PDF extraction)
6. qc_selfheal.py (post-patch)     Real shipped-state QC — fires Sentry events
7. qc_actions_from_sentry.py       Poll unresolved Sentry issues → dispatch remediation
8. sentry_seer.py trigger          Seer autofix on recent error issues
9. gen_dashboard.py                → reports/hilmar-dashboard.html
10. gen_pdf.py                     → reports/hilmar-report.pdf (6-page client PDF)
11. gen_carrier_scorecard_pdf.py   Per-carrier negotiation scorecards
12. gen_email.py                   → reports/email-body.html + email-subject.txt
13. share_intel.py export          Push to SHARED/client_intelligence/hilmar/
14. gen_rate_intelligence.py       Rate-negotiation cheat sheet + cooling alerts
15. sync_to_quote_tracker.py       Push to ol-quote-tracker Turso registry (no-op if pw missing)
```

After the pipeline, `deploy/run_daily_laptop.cmd` runs:
- `outlook_send.py daily` to the full distribution (idempotent via `reports/sent-YYYY-MM-DD.flag`)
- `qc_alert_if_needed.py` (emails Michael if QC ≠ CLEAN)
- `gen_improvements_report.py` + `outlook_send.py` → idealx.us audit only

### The two QC phases — why there are two

`patch_carriers.py` (Step 5) backfills what ingest couldn't extract. If QC
measured accuracy before the patch, it would see a low number and fire
false-positive Sentry alerts. So:
- **Pre-patch QC** — `HILMAR_QC_PHASE=pre-patch`, Sentry-suppressed
- **Post-patch QC** — `HILMAR_QC_PHASE=post-patch`, the run that fires Sentry

## 6. Data model in 60 seconds

One row per Lonny RFQ in `tracking-data-v2.json` → `requests[]`. Status
state machine (4 statuses):

```
Lonny sends RFQ ──► PENDING
OL responds ──► quoted=True
  Lonny books ──► WIN (mdolx_ref set; "Awaiting MDOLX" until confirm arrives)
  rate stales ──► Q&L (real competitive loss)
  OL silent ────► NQ  (loss_reason=NO_RESPONSE)
  open ─────────► PENDING
```

**Win Rate = Wins / (Wins + Q&L).** NQ excluded (no contest happened); it
surfaces as a separate "No-Response Rate".

Standalone WINs (`request_id` like `stand_NNNN`) come from booking
confirmations with no matching Lonny RFQ in the 30-day window —
prior-window rollovers. Excluded from rate/ETD accuracy.

Booking matching uses email `In-Reply-To` / `References` headers plus
conversation_id, container count, and carrier — not just lane + date.

## 7. QC + self-heal (~46 checks, QC-001 … QC-050)

`scripts/qc_selfheal.py` is the QC engine. Each check returns PASS / WARN /
ERROR. ERROR-severity findings gate the pipeline AND fire Sentry events.
Auto-heals safe cases (dedupe, stale-folder cleanup, schema normalization);
risky fixes are flagged operator-only.

The full index lives in `reports/QC-INDEX.md`. Notable checks:

| Check | Asserts |
|---|---|
| QC-022 | Distribution-list invariants (count, no external domains, iteration lock) |
| QC-027 | Carrier-extraction completeness |
| QC-039 | Parser accuracy ≥ 95% gate (ERROR-gates pipeline) |
| QC-040 | Cross-folder enum drift (`scripts/core.py` ↔ `src/hilmar/core.py`) |
| QC-041 | Classifier-form consistency |
| QC-042 | Data-URI guard (no `data:` URIs in email HTML) |
| QC-043 | Sentry self-improvement loop |
| QC-044 | HTML double-escape (`&amp;amp;`) |
| QC-045 | Table-header visibility (Outlook strips `linear-gradient`) |
| QC-046 | Pending-timestamp population (Windows strftime safety) |
| QC-047 | Win Rate KPI ↔ explainer banner |
| QC-048 | Turnaround sanity (flags >40h biz-hours) |
| QC-049 | WIN-rows-missing-MDOLX rate |
| QC-050 | Backup freshness + retention |

When a new QC check is added, also add an entry in
`qc_actions_from_sentry.py` → `ACTIONS` so Sentry findings route to the
right remediation:
- `log_only` — comment with the documented remediation
- `resolve_if_post_fix` — resolve if HEAD commit is newer than the issue
- `resolve_if_stale` — resolve if no events in N hours
- `rerun_parser_acc` — recompute parser accuracy + comment
- `flag_for_operator` — ⚠️ comment, stay open
- `trigger_seer` — ask Seer for autofix
- `claude_diagnose` — Claude (haiku-4-5) posts root-cause diagnosis as Sentry comment

Unmapped ERROR-level issues default to `trigger_seer` → `claude_diagnose`
(chained when Seer can't analyze).

## 8. Observability — Sentry + Seer + Claude

- **Sentry org:** `idealx-llc` · **Project:** `hilmar-daily-tracker`
- **Init point:** `scripts/sentry_setup.py` (DSN from `secrets/sentry-dsn.txt`)
- **REST wrapper:** `scripts/sentry_api.py` (token from `secrets/sentry-auth-token.txt`)
- **Seer integration:** `scripts/sentry_seer.py` — endpoints are under
  `/api/0/organizations/{org}/issues/{id}/...` (the plain `/issues/{id}/...`
  path 404s — don't "fix" it back)
- **PII scrubbing:** the `before_send` hook strips emails, MDOLX/carrier
  booking refs, message-IDs, conversation IDs, and internal `req_HEX` IDs
  from every event payload before send. `send_default_pii=False`.

See `docs/SENTRY.md` for the full runbook.

## 9. Development workflow

### Running tests

```bash
# Full pytest suite (519 tests against src/hilmar/)
cd hilmar-daily-routine
PYTHONIOENCODING=utf-8 python -m pytest tests/ --override-ini="addopts=" -q

# Integration tests (uses tests/fixtures/golden_day.json)
python scripts/run_tests.py

# Syntax check both trees
python -m compileall scripts/ deploy/ src/
```

`pyproject.toml` configures pytest with `--cov-fail-under=85` (the gate is
a regression ratchet — bump, never lower). CI workflow strips that with
`--override-ini="addopts="` because it does not install dev extras; do the
same in local quick-check runs.

The `conftest.py` adds `src/` to `sys.path` and sets
`HILMAR_PARSER_FALLBACK_DISABLE=1` so test runs never call the real
Anthropic API.

### Local pipeline dry-run

```bash
PYTHONIOENCODING=utf-8 python scripts/run_pipeline.py --dry-run
PYTHONIOENCODING=utf-8 python scripts/run_pipeline.py --skip-ingest  # re-render only
```

A real run is only meaningful on the Cloud PC (it needs the OneDrive token
cache + the OL-USA Conditional Access source IP). Codespaces CAN edit + test
but CANNOT send Outlook email — accept that and let the Cloud PC fire.

### Re-ingest after a parser change

```bash
PYTHONIOENCODING=utf-8 python scripts/reprocess_bodies.py
PYTHONIOENCODING=utf-8 python scripts/ingest.py
PYTHONIOENCODING=utf-8 python scripts/patch_carriers.py
```

### Authentication (MSAL device-code, every ~80 days)

```bash
python scripts/outlook_send.py auth
```

QC-023 warns at 60 days, errors at 80. The token cache lives at
`secrets/token-cache.json` (chmod 600).

### CI

`.github/workflows/test.yml` runs on push/PR to `main`:
1. `python -m compileall scripts/ deploy/ src/` (syntax)
2. Smoke-import every script + every `src/hilmar/` module
3. `pytest tests/ --override-ini="addopts="` (the cov gate isn't enforced in CI)
4. Optional integration tests via `scripts/run_tests.py` if fixtures present

### Commits

- Stage specific files (never `git add -A` — risks committing secrets or
  runtime artifacts despite `.gitignore`).
- Write the "why" not the "what" in commit messages. Match existing style
  (see `git log --oneline`): imperative, ~70-char subject, body for context.
- Never push to a branch other than the one you were told to develop on.
- Don't commit without being asked; do mirror script edits to production.

## 10. Configuration knobs (`config.json`)

| Key | What |
|---|---|
| `distribution.full_list` | The 10-recipient daily distribution (QC-022-guarded) |
| `distribution.test_list` | Safe iteration target (Michael only) |
| `rules.pending_aging_hours` | When a quoted-but-not-booked row becomes Q&L |
| `auto_chase` | Soft-nudge config for stale PENDINGs to Lonny (≤3/day, ≥4 PM ET) |
| `alerts.teams_webhook_url` | Empty → queue to `reports/alerts-queue.json` |
| `backup` | Secondary OneDrive folder + local-offline dir, retention_days |
| `ingest_scope.mailbox_folder` | The Outlook folder ingest scans (`Hilmar Tracker`) |

## 11. Pointers to deeper docs

- **`RUNBOOK.md`** — failure-mode playbook: wrapper rc=255, MSAL refresh
  expired, duplicate sends, QC drift triage, etc.
- **`docs/HANDOFF.md`** — inherited handoff from the merged `hilmar-tracker`
  repo. Historical (some VM/path details are stale) but has the canonical
  status-classifier table + the 10-phase QC engine description that
  `src/hilmar/qc.py` implements.
- **`docs/SENTRY.md`** — observability runbook (DSN config, PII scrubbing,
  dashboard URLs, what each event carries).
- **`docs/PARSER-GAPS.md`** — known parser gaps + remediation history.
- **`docs/INSIGHTS-DESIGN.md`** — `src/hilmar/insights.py` design + the
  feedback-loop architecture.
- **`docs/SHARED_CLIENT_INTELLIGENCE_SCHEMA.md`** — the cross-project
  `client_intelligence` registry shape (consumed by ol-quote-tracker).
- **`reports/QC-INDEX.md`** — full QC matrix: severity, what each catches,
  self-heal action, originating commit.
- **`.claude/skills/hilmar-daily-tracker/`** — the in-repo skill. Read
  `SKILL.md` + the three reference files (`commands.md`, `pipeline.md`,
  `architecture.md`) when working on the pipeline from the iPhone Claude
  app or any device where paths differ.
