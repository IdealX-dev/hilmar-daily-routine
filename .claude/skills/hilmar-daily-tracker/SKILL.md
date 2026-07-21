---
name: hilmar-daily-tracker
description: >-
  Hilmar Daily Shipment Tracker — the daily-fire pipeline that ingests Outlook
  emails between Lonny Upfold (Hilmar Ingredients) and OL-USA into a daily
  shipment-tracker email + dashboard + PDF + private audit. Use this skill
  WHENEVER Michael mentions Hilmar, the Hilmar tracker, the daily shipment
  tracker, Lonny Upfold, the OL-USA booking pipeline, MDOLX bookings, the
  daily fire (Mon-Fri 8 AM ET, reporting the prior business day), Hilmar parser accuracy, the Hilmar QC checks
  (QC-001..QC-050), or Sentry/Seer for Hilmar — or wants to run, check, debug,
  explain, or report on the Hilmar pipeline from ANY device including the
  iPhone Claude app. Also trigger on "run the Hilmar pipeline", "check Hilmar
  QC", "send the Hilmar sample", "Hilmar pipeline status", "review the Hilmar
  audit", or tracking-data-v2.json. Single source of truth for the project —
  consult it before any Hilmar work so file paths and commands are correct on
  whatever device you are on.
---

# Hilmar Daily Shipment Tracker

The Hilmar Daily Shipment Tracker is a production pipeline that runs a
**Mon-Fri 8:07 AM ET** (a single fire, reporting the PRIOR business day — Mon→Fri, Tue→Mon, … Fri→Thu; the report labels its KPIs "the prior business day"), plus a **Monday 5 AM ET** weekly exec summary — via GitHub Actions (Cloud PC fallback). No wrap-up, no weekend emails. It ingests the Outlook
email thread between **Lonny Upfold** (Logistics Coordinator, Hilmar
Ingredients) and **OL-USA** (the ocean freight forwarder), and produces:

- A **daily shipment-tracker email** to a 10-recipient distribution
- An interactive **HTML dashboard** (clickable KPI tiles, tabbed views)
- A **6-page client PDF** + per-carrier scorecard PDFs
- A **private systems-audit email** to `michael.deitchman@idealx.us` only

This skill makes the project usable from any of Michael's devices. It is the
authoritative reference for **where the files live, how to run the pipeline,
and what the conventions are** — consult it first so you don't guess paths.

## Step 0 — Locate the project on THIS device

The project lives in two places. Find whichever is present on the current
machine, in this priority order:

1. **OneDrive (primary)** — `<home>/OneDrive - IdealX/claude/PROJECT HILMAR/`
   - `<home>` is the current user's home dir (`%USERPROFILE%` on Windows,
     `$HOME` on macOS/Linux). On Michael's main laptop this resolves to
     `C:\Users\MichaelDeitchman\OneDrive - IdealX\claude\PROJECT HILMAR`.
   - This folder syncs across every device signed into Michael's OneDrive,
     so on a second laptop the same relative path works.
   - The **git repo** is the `hilmar-daily-routine/` subfolder inside it.
   - **Production scripts run from `PROJECT HILMAR/scripts/`** — a mirror of
     `hilmar-daily-routine/scripts/`. Edits go to the repo first, then are
     copied to `PROJECT HILMAR/scripts/`. The data file
     `tracking-data-v2.json` sits at `PROJECT HILMAR/` (repo parent).

2. **GitHub (fallback)** — if no OneDrive copy is present (e.g. a brand-new
   laptop), clone the repo:
   ```
   git clone https://github.com/IdealX-dev/hilmar-daily-routine.git
   ```
   The repo has `scripts/`, `src/hilmar/`, `tests/`, `docs/`, `config.json`.
   A fresh clone won't have `tracking-data-v2.json` or `data-backups/` — those
   live in the OneDrive parent and on the production Cloud PC.

**To detect which to use:** check whether the OneDrive path exists. If yes,
work there. If no, use a git clone. Tell Michael which one you found so he
knows the state of the device.

**Heavy pipeline runs need a real machine with Python + the staged emails.**
GitHub Actions does the scheduled fires (Cloud PC is the fallback). From the iPhone Claude app you
can review the audit, check status, explain results, and make decisions — but
you cannot run the Python pipeline there. That's expected and matches how the
rate checker works.

## What you can do

| Intent | Where to go |
|---|---|
| Run the full daily pipeline | `references/commands.md` → "Run the pipeline" |
| Run QC self-heal / check QC | `references/commands.md` → "QC self-heal" |
| Send a sample / the daily email | `references/commands.md` → "Send email" |
| Check pipeline status / last run | `references/commands.md` → "Status" |
| Check parser accuracy | `references/commands.md` → "Parser accuracy" |
| Understand the 16-step pipeline | `references/pipeline.md` |
| Understand parser / QC / Sentry / data model | `references/architecture.md` |
| Review today's audit findings | Read `reports/improvements-report.html` |

Always read the relevant reference file before acting — the commands have
device-specific path handling and safety rules (especially around sending
email) that you must not skip.

## Hard rules (do not violate)

1. **Email sends.** The pipeline's final email goes to a **10-recipient
   distribution** (`config.json` → `distribution.full_list`). NEVER send a
   test/iteration email to that list. During any formatting iteration, lock
   `full_list` to `michael.deitchman@idealx.us` only and set the
   `_iteration_mode_note` key in `config.json` (QC-022 enforces this). The
   private audit (`gen_improvements_report.py`) always goes to
   `michael.deitchman@idealx.us` only — that's correct.

2. **Timestamps in chat = Eastern Time.** Michael operates on ET. Convert any
   UTC timestamp from logs/DB before showing it. Code stays UTC; only chat
   output is ET.

3. **Parser accuracy is a 95% hard gate.** `src/hilmar/parser_accuracy.py`
   `ACCURACY_THRESHOLD = 0.95`. QC-039 blocks the pipeline if overall accuracy
   or any critical field drops below threshold. Never lower the gate to make a
   red check pass — fix the parser.

4. **Every QC is checked, self-healed, and ROOT-fixed — a constant**
   (Michael 2026-06-09: "all qc's must be checked and self healed; all root
   issues must be solved and not patched; this is a constant"). When a check
   fires, fix the cause — never suppress the check, widen a threshold, or
   paper over the data. Every QC check ships in the SAME commit with: a row in
   `reports/QC-INDEX.md`, a route in `qc_actions_from_sentry.ACTIONS` (or the
   documented default), AND a regression test in `tests/`. This is
   **mechanically enforced** by `tests/test_qc_governance.py` — a new check
   without docs/route/test fails CI, and the untested backlog is a shrink-only
   ratchet (it can never grow). New mailbox/folder → walk it; new scheduled
   job → staleness check; new API → freshness check; new email path → bounce
   check.

7. **The code is audited daily, same as the data.** `scripts/run_audit_tests.py`
   runs the full pytest suite under coverage on every fire (a step in
   `run_pipeline.py`) and writes `reports/test-result.json`. **QC-052** + the
   private systems audit red-flag a failed test or a coverage drop below the
   `pyproject.toml` gate (`--cov-fail-under`), and name modules below the
   per-module floor as the worklist for "every line tested". The coverage
   gate is a one-way ratchet — raise it, never lower it to make a run pass.
   The routine is an OBSERVER: it always exits 0 so a red test never blocks
   the client email; the audit is where the regression is loud. On a host
   without dev deps the artifact records `SKIPPED` and the audit nudges you to
   `pip install -e '.[dev]'` — that's the one manual step to make the routine
   actually run on the Cloud PC.

5. **Mirror edits.** `scripts/` exists in TWO places — the git repo
   (`hilmar-daily-routine/scripts/`) and production (`PROJECT HILMAR/scripts/`).
   After any script edit, `cp` from repo → production. QC-040 enforces no
   drift between paired files.

6. **Never greenfield.** The Hilmar project was once bifurcated (hilmar-tracker
   greenfielded as hilmar-daily-routine). Refactor the existing repo; never
   create a parallel one.

## Quick orientation

- **Repo:** `github.com/IdealX-dev/hilmar-daily-routine` (branch `main`)
- **Pipeline entry point:** `scripts/run_pipeline.py`
- **Schedule:** Mon-Fri 8:07 AM ET — one fire, reports the prior business day (Mon→Fri, Tue→Mon, … Fri→Thu) + Mon 5 AM ET weekly exec summary; no wrap-up, no weekend. GitHub Actions (Cloud PC fallback)
- **Data file:** `tracking-data-v2.json` (155-170 request rows)
- **Backups:** `data-backups/` (timestamped snapshots, retention-pruned)
- **Observability:** Sentry (org `idealx-llc`, project `hilmar-daily-tracker`)
  + Seer autofix + a Claude-API diagnosis fallback
- **Parser gate:** 95% accuracy across 19 measured fields
- **QC:** QC-001 through QC-055, contiguous (some sub-variants)
- **Secrets** (gitignored, in `secrets/`): `sentry-dsn.txt`,
  `sentry-auth-token.txt`, `anthropic-api-key.txt`, `quote-tracker-pwd.txt`,
  `token-cache.json` (MSAL)

## The data model in one paragraph

Lonny emails OL-USA an RFQ ("Oakland to Yokohama, 2x40'HC"). OL responds with
a rate. Lonny either books (→ **WIN**, gets an MDOLX booking number), the rate
goes stale (→ **Q&L**, Quoted & Lost), OL never responds (→ **NQ**, Not
Quoted), or it's still open (→ **PENDING**). One row per RFQ in
`tracking-data-v2.json`. Win Rate = Wins / (Wins + Q&L). The daily email
reports the current Pacific business day plus period-to-date rollups. See
`references/architecture.md` for the full field list + status state machine.
