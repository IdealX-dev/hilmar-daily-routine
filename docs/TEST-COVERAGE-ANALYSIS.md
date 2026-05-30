# Test Coverage Analysis — 2026-05-30

Analysis of the Hilmar daily-tracker test suite, with prioritized
recommendations for where to invest in new tests.

## Snapshot

Measured by running `pytest --cov=hilmar` on `claude/test-coverage-analysis-yVeA1`:

- **594 tests pass.**
- **Total line coverage: 85.75%** against the `hilmar` package (4,574 statements, 652 missed).
- **Coverage gate: `--cov-fail-under=85`.** The suite passes by **0.75 points** — a hair above the floor.

The headline number is healthy, but it is measured against only part of the
codebase and hides several high-risk gaps. Details below.

## Per-module coverage (`src/hilmar`)

| Module | Cover | Missed | Notes |
|---|---|---|---|
| `parser_accuracy.py` | **0%** | 89 / 89 | **Entirely untested.** Enforces the 95% accuracy gate (QC-039). |
| `ingest.py` | **74%** | 190 / 744 | Lowest of the tested modules; fuzzy WIN-matching heuristics uncovered. |
| `orchestrator.py` | 84% | 47 / 297 | `main()` entry + failure-paging path untested. |
| `model_router.py` | 85% | 24 / 158 | LLM response/usage coercion + fallback selection partly untested. |
| `feedback_ingest.py` | 86% | 21 / 154 | CLI `main()` + Graph error paths. |
| `body_parser.py` | 87% | 64 / 486 | Scattered branch gaps across extraction paths. |
| `graph_client.py` | 89% | 32 / 301 | Token-refresh / retry error branches. |
| `core.py` | 90% | 69 / 716 | Scattered edge branches. |
| `qc.py` | 91% | 52 / 549 | Phase-6 cross-check error paths + CLI `main()`. |
| `render.py` | 92% | 25 / 320 | PDF fallbacks. |
| `insights.py` | 93% | 18 / 266 | LLM narrative error paths. |
| `logging_config.py` | 96% | 4 | — |
| `send.py` | 95% | 3 | — |
| `backfill.py` / `baselines.py` / `paths.py` | 98–100% | ≤1 | Well covered. |

## The biggest blind spot is not in this table

The coverage gate is `--cov=hilmar`, so it measures **only the 11k-line
packaged module.** The repository also ships a **~25,000-line `scripts/`
tree (≈60 files)** that the gate never sees. Only ~4 of those scripts have
any tests at all (`viz.py`, `branding.py`, `gen_improvements_report.py`,
`share_intel.py`, loaded via `sys.path` hacks in the test files).

Production-critical scripts referenced by `RUNBOOK.md` / `deploy/` that
currently have **zero coverage and are invisible to the gate**:

- `scripts/run_pipeline.py` — the pipeline driver.
- `scripts/outlook_send.py` — sends the daily email (most-referenced script).
- `scripts/refresh_stage.py` — staging refresh.
- `scripts/backup_offline.py` — backups.
- `scripts/qc_selfheal.py` — **135 KB**, the largest single file in the repo.
- `scripts/gen_email.py` (81 KB), `scripts/gen_dashboard.py` (65 KB).

So "85.75%" reflects roughly 11k of the ~36k lines of Python in the repo.
The real, whole-repo figure is substantially lower.

---

## Recommendations (prioritized)

### P0 — Cover `parser_accuracy.py` (0% → ≥90%)

This module computes the per-field parser accuracy that QC-039 uses to
**block the daily ship** when accuracy drops below the 95% floor Michael
mandated. It is the safety net for the project's stated "must run at 95%
minimum no matter cost" requirement — and it has **no tests at all.** A
regression in `compute_accuracy`, the per-field threshold overrides, or the
"applicable vs populated" logic would silently change whether bad data is
allowed into the daily email. This is the single highest-value gap.

Suggested cases: empty input; a field at exactly the threshold; conditional
fields (e.g. `mdolx_ref` only on WIN rows); per-field threshold overrides;
the `pass`/`overall_rate` contract; `weighted_accuracy()` vs equal-weight.

### P1 — Raise `ingest.py` from 74% (the WIN-matching heuristics)

`ingest.py` is the lowest-covered tested module and contains the riskiest
logic in the system: the Lonny-reply "send-signal" → request matching and
the **multi-tier `carrier_won` fallback chain** (lines ~1160–1254:
sibling-lane inheritance within 5/30 days, then substring-prefix lane
fallback). These are exactly the fuzzy heuristics where a bug silently
mis-attributes WINs/TEU without crashing. Add fixture-driven tests for:
the 5-day matching window boundary, carrier inheritance from a sibling
quote, the prefix fallback (`hcmc (cai mep)` vs `hcmc (cat lai)`), and the
"already matched to MDOLX → skip" guard.

### P2 — Bring `scripts/` into the coverage measurement

Decide a policy and make it explicit rather than implicit:
- If scripts are production code, add them to the coverage source
  (`--cov=hilmar --cov=scripts`) and write tests for the P0 entry points
  (`run_pipeline.py`, `outlook_send.py`, `qc_selfheal.py`).
- If they are legacy/one-off tools superseded by `src/hilmar`, document
  that and exclude them explicitly so the 85% number isn't misleading.

Several scripts (`body_parser.py`, `core.py`, `ingest.py`, `qc.py`) appear
to **duplicate** packaged modules — confirm which copy is authoritative;
testing a dead duplicate is wasted effort and a live one is a real gap.

### P3 — Entry points and failure paths

CLI `main()` functions and error-handling paths are consistently the
uncovered lines: `orchestrator.main()` + `_page_on_failure` (the
email/webhook alerting that fires when the pipeline crashes — untested, so
we'd discover a broken pager *during* an outage), `feedback_ingest.main()`,
`qc.main()`, and `model_router`'s LLM response/usage coercion + fallback
model selection. These are cheap to cover with `responses`/monkeypatch and
remove "the alarm didn't ring" failure modes.

### P4 — Harden the ratchet

At 85.75% against an 85% floor, a single new uncovered branch fails CI.
After landing P0–P1, bump the gate to lock in the gains (the config comment
already frames it as a "regression ratchet"). Consider a **per-file**
minimum for the critical modules (`parser_accuracy`, `ingest`, `qc`) so a
flood of trivially-covered lines elsewhere can't mask a regression in the
data-integrity core.

## Suggested sequencing

1. P0 `parser_accuracy.py` tests (small module, highest risk).
2. P1 `ingest.py` matching/fallback tests (fixture-driven).
3. P2 scripts policy decision + coverage wiring.
4. P3 entry-point / failure-path tests.
5. P4 raise the gate.
