# Hilmar Daily Tracker — Test Suite & Coverage Audit (2026-06-02)

**Scope:** read-only audit of the 932-test suite, coverage map, flakiness
risk, brittle patterns, and standing-rule compliance. PRs #14-21 already
landed and their findings are excluded.

**Snapshot at audit time:**
- 932 tests collected, 932 passing, total run ~3.1s (no slow tests).
- Coverage on `src/hilmar/` = **91%** total (gate 90 — 1.0 pt margin).
- Coverage on `scripts/` = **11%** total — **no gate** measures it.
- Test fixtures: 1 (`tests/fixtures/golden_day.json`).
- `tests/conftest.py` adds `src/` to `sys.path` but NOT `scripts/`; each
  scripts-targeting test prepends `scripts/` itself.

---

## 1. Per-file coverage map for `scripts/`

The repo deliberately has two trees (CLAUDE.md §2). The 90% gate measures
ONLY `src/hilmar/`. `scripts/` is the actual production code path on the
Cloud PC. Per `pytest --cov=scripts`:

### Tested directly by a test file (≥ low coverage)

| `scripts/*` file | Stmts | Cov | Direct test file | Rating |
|---|---:|---:|---|---|
| `viz.py` | 80 | 92% | `tests/test_viz.py` (230 lines) | **High** |
| `branding.py` | 61 | 89% | `tests/test_branding.py` | **High** |
| `sync_to_quote_tracker.py` | 158 | 83% | `tests/test_sync_to_quote_tracker.py` | **High** |
| `run_audit_tests.py` | 174 | 74% | `tests/test_run_audit_tests.py` | High |
| `run_pipeline.py` | 180 | 66% | `tests/test_run_pipeline_timeout.py`, `test_pipeline_best_effort.py` | Medium |
| `share_intel.py` | 225 | 59% | `tests/test_share_intel.py` | Medium |
| `gen_improvements_report.py` | 478 | 50% | `tests/test_improvements_report.py`, `test_audit_test_diagnostics.py` | Medium |
| `auto_chase_pending.py` | 105 | 50% | `tests/test_auto_chase_pending.py` | Medium |
| `core.py` | 622 | 34% | `tests/test_scripts_core_decide_status.py`, `test_core_parity.py`, `test_smarter_price_classifier.py`, `test_aggregate_loss_reasons.py`, `test_loss_reason_mix_html.py` | Medium (3-of-4 status branches tested; carrier/lane aggregation not directly covered) |
| `teams_alert.py` | 165 | 35% | `tests/test_teams_alert_qc049.py` | Medium |
| `qc_actions_from_sentry.py` | 235 | 24% | `tests/test_sentry_stale_auto_resolve.py` | Low |
| `sentry_setup.py` | 208 | 22% | (indirect via imports) | Low |
| `sentry_api.py` | 190 | 18% | (indirect) | Low |
| `gen_email.py` | 675 | **13%** | `tests/test_loss_reason_mix_html.py` only | **Low — see §2** |

### Zero coverage (no test imports them)

CRITICAL daily-pipeline modules with **0% coverage**:

| File | Stmts | Role in 10AM fire | Severity |
|---|---:|---|---|
| `qc_selfheal.py` | 1455 | 46 QC checks + 7-phase self-heal — the gate that prevents shipping garbage. The SECOND-largest module in the repo. | **Critical** |
| `ingest.py` (scripts/) | 682 | Step 2 — stage emails → request rows | **Critical** |
| `gen_dashboard.py` | 450 | Step 9 — HTML dashboard (client deliverable) | **Critical** |
| `gen_pdf.py` | 318 | Step 10 — 6-page client PDF | **Critical** |
| `gen_carrier_scorecard_pdf.py` | 219 | Step 11 — per-carrier scorecards | High |
| `outlook_send.py` | 215 | Sends the daily email to 10 recipients | **Critical** |
| `patch_carriers.py` | 476 | Step 5 — 4-pass carrier/rate/ETD/ERD backfill | **Critical** |
| `drift_check.py` | 151 | Step 3 — 6-phase integrity gate (FAILs at <80% quote-rate) | High |
| `backup.py` | 115 | Step 1 — snapshot rotation; QC-050 freshness check | High |
| `refresh_stage.py` | 423 | Graph mailbox → stage_emails*.txt | High |
| `fetch_bodies.py` | 158 | Body cache for ingest | Medium |
| `gen_weekly_summary.py` | 163 | Friday-only weekly digest | Medium |
| `gen_rate_intelligence.py` | 162 | Step 14 — rate-negotiation cheat sheet + cooling alerts | Medium |
| `body_parser.py` (scripts/) | 385 | Mirror of src/hilmar/body_parser; QC-040 pairs them | High (drift) |
| `sentry_seer.py` | 150 | Step 8 — Seer autofix trigger | Medium |
| `pdf_parser.py` | 214 | PDF data extraction for `patch_carriers.py` | Medium |
| `pdf_llm_rescue.py` | 112 | Anthropic-LLM PDF rescue fallback | Low |
| `gen_email_new.py` | 388 | DEAD/abandoned variant — 0% coverage but also not in pipeline. **Should be deleted.** | Low (dead code finding) |
| `build_ops_flow_v2.py` | 253 | Order-flow body parser; wired via `body_parser` | Medium |
| `link_mdolx_wins.py` | 184 | Backfill utility (manual) | Low |

The 0% list spans **42 of 60** `scripts/*.py` files.

---

## 2. Untested critical paths into the client deliverable

### Finding T-001 — `gen_email.py` has 13% coverage; `build_subject` + `build_body` have **zero** direct tests
**Severity: Critical** · `scripts/gen_email.py` (1638 LOC, 36 top-level fns)
The only direct tests are the 14 in `test_loss_reason_mix_html.py` against
`_loss_reason_mix_html()` — one helper out of 36. The two entry points
**`build_subject()` and `build_body()`** that produce the daily email sent
to the 10-recipient distribution have NO direct tests. A typo in
`_kpi_block_html`, `_pending_html`, `_carrier_block_html`, or
`_winning_lanes_html` ships green to all 10 recipients.
**What to add:** golden-output tests of `build_subject(golden_day, cfg)`
and a structural test of `build_body(...)` that locks (a) subject ≤ 78 chars,
(b) presence of "Daily Tracker", week-of-date, KPI block, pending table; (c)
QC-042 + QC-044 + QC-045 invariants (no `data:`, no `&amp;amp;`, no
`linear-gradient`).
**Effort: M** (4-6 tests, lean on `tests/fixtures/golden_day.json`).

### Finding T-002 — `gen_dashboard.py` has 0% direct coverage; only the `hilmar.render` SIBLING is exercised
**Severity: Critical** · `scripts/gen_dashboard.py` (1026 LOC, ~10 fns)
`tests/test_pipeline.py::test_render_dashboard_produces_nonempty_html`
calls `hilmar.render.render_dashboard` (src tree) — the test name suggests
dashboard coverage but the actual production renderer (the one the Cloud
PC runs) is `scripts/gen_dashboard.py` and it's untouched. Same drift risk
as QC-040's `core.py` pair, with no QC check enforcing it.
**What to add:** a single smoke test that imports `scripts/gen_dashboard.py`
via `importlib.util.spec_from_file_location`, calls `render(cfg, golden_day)`,
and asserts (a) HTML > 5 KB, (b) contains the 4 KPI tile values from the
fixture, (c) no `linear-gradient` in `<style>` blocks (QC-045), (d) no
`data:` URIs (QC-042).
**Effort: S** (one test, golden fixture already in tree).

### Finding T-003 — `gen_pdf.py` has 0% coverage; PDF generation untested for `scripts/` tree
**Severity: Critical** · `scripts/gen_pdf.py` (599 LOC, 14 fns)
Same pattern as T-002. `test_pipeline.py::test_render_pdf_produces_nonempty_pdf`
covers `hilmar.render.render_pdf` (src tree) not the production PDF.
**What to add:** smoke test that builds the 6-page PDF from `golden_day`
and asserts `out.stat().st_size > 30_000` + magic bytes `%PDF-` at start.
Also lock the 6-page count via `len(PdfReader(out).pages) == 6` (using
reportlab's own reader or `pypdf`).
**Effort: S** (one test).

### Finding T-004 — `outlook_send.py` has 0% coverage; QC-022 distribution-list invariant has no DIRECT script test
**Severity: Critical** · `scripts/outlook_send.py` (383 LOC, 11 fns)
This is the module that sends the daily email. `_load_distribution_from_config`
+ `cmd_daily` orchestrate the send. CLAUDE.md §3 rule #1 says NEVER send a
test email to `full_list`; the only thing guarding this is QC-022 + a sent
flag file. There is NO test of `_load_distribution_from_config()` ever
returning `test_list` when an iteration-lock is set, no test of the
sent-flag idempotency (`reports/sent-YYYY-MM-DD.flag`), no test that
`cmd_nudge` cannot accidentally hit `full_list`.
**What to add:** (a) test that `_load_distribution_from_config()` honors
an iteration-locked `full_list` (single mailbox = michael@idealx.us);
(b) test that `cmd_daily` skips when the sent flag exists; (c) test that
attachments larger than the Graph limit raise (mirror
`test_graph_client.py::test_send_attachment_too_large_raises`).
**Effort: M** (3-4 tests, monkeypatch the MSAL + `requests` calls).

### Finding T-005 — `patch_carriers.py` (Step 5, 4-pass enrichment) has 0% coverage
**Severity: Critical** · 799 LOC. Backfills carrier/rate/ETD/ERD from email
bodies + PDF. A regression in `_discover_carrier_from_bodies()` /
`_discover_full_quote_from_bodies()` ships incomplete data or crosses the
QC-039 gate silently. Unit tests per discover-function fed synthetic body
strings from `golden_day`. **Effort: M.**

### Finding T-006 — `qc_selfheal.py` (the 46-check QC engine) has 0% coverage
**Severity: Critical** · 2840 LOC — the LARGEST module. `hilmar.qc` has 91%
coverage via `test_qc.py` (62 tests), but the scripts-tree mirror that runs
on the Cloud PC is untested. The 2026-05-30 `scripts/core.py` vs
`src/hilmar/core.py` drift is the precedent — same pattern almost
certainly exists here, undetected. **What to add:** a parity test
mirroring `test_core_parity.py` that runs `golden_day` through both engines
and asserts identical findings. **Effort: M-L.**

### Finding T-007 — `drift_check.py` (Step 3 gate, <80% quote-rate FAIL) has 0% coverage
**Severity: High** · 298 LOC. The "auto-heal" branch in `phase4_nq_schema`
mutates data; a buggy heal silently corrupts the daily fire. Per-phase
tests with hand-crafted minimal `data` dicts. **Effort: M.**

### Finding T-008 — `backup.py` + QC-050 freshness 0% scripts-side coverage
**Severity: High** · 115 LOC. A regression producing 0-byte snapshots
would pass QC-050 (file exists) until rollback was needed. `tmp_path`
round-trip test (create → list → rollback) + retention prune.
**Effort: S.**

### Finding T-009 — `gen_carrier_scorecard_pdf.py` + `gen_weekly_summary.py` 0% coverage
**Severity: High (scorecard, every fire) / Medium (weekly, Friday only).** Same pattern as T-003. **Effort: S each.**

### Finding T-010 — `refresh_stage.py` (Graph mailbox fetcher) 0% coverage
**Severity: High** · 740 LOC, producer of `stage_emails*.txt`. Network
mocking via `responses` (already a `pyproject.toml` dev dep) makes this
tractable. **Effort: M.**

### Finding T-011 — `gen_rate_intelligence.py` (Step 14) 0% coverage
**Severity: Medium.** Cheat-sheet generator from baselines. **Effort: S.**

---

## 3. Flaky test risks

### Finding F-001 — 24 call sites of `datetime.now(...)` without `freezegun`
**Severity: Medium** · `tests/test_qc.py:598,754`, `test_teams_alert_qc049.py:38..108` (×6),
`test_sentry_stale_auto_resolve.py:38..86` (×5), `test_auto_chase_pending.py:37,49,70`,
`test_loss_reason_mix_html.py:43`, `test_ingest.py:861..1334` (×8)
The pattern is `datetime.now(UTC) - timedelta(days=N)` to fabricate aged
rows. These pass today but will silently misclassify the moment the
system clock crosses a midnight/weekend boundary mid-run. Specifically:
- `test_teams_alert_qc049.py::test_old_unconfirmed_win_alerts` uses
  `days=14` against an aging threshold that's currently `>10 days` — a
  4-day cushion is plenty, but `test_alert_dedup_within_same_week` uses
  `days=3` against a 1-week dedup window. If a fire happens on a Sunday
  at 23:59 UTC, the 3-day-ago row crosses week boundaries differently.
- `test_qc.py::test_..._aged_ts` uses `hours=120` (5 days) — same risk.
- `test_loss_reason_mix_html.py::_ts(40)` is fine; `_ts(2)` is at risk if
  the 30-day window boundary shifts.
**What to add:** install `freezegun` (it's not a dep yet), add a session
fixture in `conftest.py` that freezes to `2026-06-02T15:00:00Z` for tests
that opt in via a marker. Migrate the 24 call sites.
**Effort: M.**

### Finding F-002 — `tests/test_run_audit_tests.py` does NOT monkeypatch `PYTEST_OUTPUT`
**Severity: Medium** · `tests/test_run_audit_tests.py:56-79,110-123`
`test_write_creates_artifact` + `test_skipped_path_writes_artifact_and_exits_zero`
+ `test_main_skips_when_no_test_root` monkeypatch `RAT.REPORTS` and
`RAT.ARTIFACT` to `tmp_path` but leave `RAT.PYTEST_OUTPUT` pointed at the
real `reports/pytest-output.txt`. Today's runs land there because the
SKIPPED branch never writes pytest output — but the moment a future change
extends the SKIPPED branch to write a stub, tests start scribbling into
the real reports directory and the next pipeline reads the test stub.
**What to add:** monkeypatch `RAT.PYTEST_OUTPUT = tmp_path / "pytest-output.txt"`
in all three tests.
**Effort: XS.**

### Finding F-003 — `test_run_step_kills_a_hung_subprocess` uses `time.sleep(99)` (mocked, but at risk)
**Severity: Low** · `tests/test_run_pipeline_timeout.py:33`
The line `RP.run_step("Hung step", [sys.executable, "-c", "import time; time.sleep(99)"])`
relies on the `monkeypatch.setattr(RP.subprocess, "run", fake_run)` to
intercept — if a future refactor breaks the patch target name (e.g. moves
to `subprocess.Popen`), the test will actually sleep 99s on CI before
failing. **What to add:** the subprocess command should just be
`[sys.executable, "--version"]` (any harmless arg) — the test logic only
needs the function to raise TimeoutExpired via the patched `fake_run`.
The `sleep(99)` line is misleading and a future footgun.
**Effort: XS.**

### Finding F-004 — `tests/test_branding.py::test_logo_reportlab_image_preserves_aspect_ratio` is the only test >0.5s (0.93s)
**Severity: Low.** Not flaky per se but the slowest test by a 2× margin —
likely doing a real PNG decode. Worth tagging for optimization. Suite is
otherwise blistering (3.1s for 932 tests).
**Effort: XS.**

---

## 4. Brittle test patterns

### Finding B-001 — Loss-reason mix tests assert exact escaped HTML substrings
**Severity: Medium** · `tests/test_loss_reason_mix_html.py:111-116,160-162,196-201`
`assert "3 &middot; 50%" in out or "3 · 50%" in out` — the dual form is
defensive but inverse-fragile: if `_esc` swaps escape style (`&middot;`
→ `&#xB7;`), neither matches and the test fails despite correct output.
Same for `OL didn&#x27;t respond`. **What to add:** lift the comparison
to logical structure — parse with a tiny HTML helper and assert
`row.text == "3 · 50%"` after `html.unescape`.
**Effort: S.**

### Finding B-002 — `test_pipeline_best_effort.py::test_known_best_effort_steps_classified` hardcodes step-name string set
**Severity: Medium** · `tests/test_pipeline_best_effort.py:56-67`
The test asserts a literal-string set including `"Sentry-driven QC actions"`.
If anyone renames a step in `run_pipeline.py` (a 1-line edit), classification
silently drifts. The test catches the omission but not a rename. **What to
add:** introduce a `STEP_NAMES` constant in `run_pipeline.py` and assert
the BEST_EFFORT set is a subset of it — that way renames break the import.
**Effort: S.**

### Finding B-003 — `test_invariants.py` enforces single-writer rule via regex `\\["{field}"\\]\\s*=`
**Severity: Low** · `tests/test_invariants.py:62`
Catches `data["summary"] = ...` but not `summary_local = ...; data |= {"summary": summary_local}`
or `setattr(data, "summary", ...)`. The rule is enforceable but the regex
is not exhaustive. **What to add:** a comment in the test explaining the
gap, OR (better) an AST-based check using `ast.parse + ast.walk` for
`Subscript` assignments.
**Effort: S.**

### Finding B-004 — Hardcoded `expected = "12 · 50%"` style counts depend on `_data_with_losses` shape
**Severity: Low** · `tests/test_loss_reason_mix_html.py:94,101`
`assert "6 losses" in out` ties the count to the test fixture row count
(currently 6 inside-30d). Any future test-helper edit silently breaks the
assertion. **What to add:** compute the expected count from the fixture
(`f"{sum(1 for r in data['requests'] if r['status']=='LOSS' and _within_30d(r)) } losses"`)
rather than hardcoding.
**Effort: XS.**

### Finding B-005 — `tests/test_pipeline.py` skips silently when `hilmar.render` is missing
**Severity: Medium** · `tests/test_pipeline.py:84,101,108,116`
4 of the 5 dashboard/pdf/scorecard/email tests are `pytest.importorskip(
"hilmar.render", reason="render.py ships at M3.6 — test auto-activates then.")`
The comment is stale — `hilmar.render` HAS shipped (it's 92% covered).
But if `hilmar.render` ever gets renamed/deleted, these tests SKIP rather
than FAIL, hiding the regression. **What to add:** swap to a hard `import`
+ `pytest.skip(reason=...)` only on a guard env var.
**Effort: XS.**

---

## 5. Test-vs-spec gap

### Finding S-001 — `test_section_header_and_30d_window_render` lacks negative assertion on >30d entries
**Severity: Medium** · `tests/test_loss_reason_mix_html.py:88-94`
Asserts `"6 losses"` and `"Last 30 days"` but does not assert that the
40d-old PRICE row is EXCLUDED from the 30d block. If the window logic
inverted (use `> days_ago` instead of `<= days_ago`), the test still
passes because 6 = 8 - 2 holds incidentally for the chosen counts.
**Effort: XS.**

### Finding S-002 — `test_parser_accuracy.py` asserts `ACCURACY_THRESHOLD = 0.95` but not the ERROR-gate path
**Severity: High** · `tests/test_parser_accuracy.py`
Locks the constant + the computation but doesn't lock the BEHAVIOR — that
QC-039 raises `ERROR` (not `WARN`) when accuracy < 95. A future refactor
could downgrade QC-039 to WARN (defeating the gate) and the suite stays
green. **What to add:** test that `run_qc(data_with_88pct_accuracy)`
returns a finding with `severity == "ERROR"` and that the orchestrator
treats it as gate-blocking.
**Effort: S.**

### Finding S-003 — `test_invariants.py::test_qc_writes_all_three_aggregates` checks string presence not function-call presence
**Severity: Low**
A future refactor that comments out the aggregate-write line in a
docstring or string-literal would slip past — `path.read_text` doesn't
parse code.
**Effort: XS.**

### Finding S-004 — `outlook_send.py` distribution invariant is NOT tested
**Severity: Critical (mirrors T-004)** — see above.

---

## 6. Slow tests

Per `pytest --durations=20`:

| Test | Time | Note |
|---|---:|---|
| `test_branding.py::test_logo_reportlab_image_preserves_aspect_ratio` | 0.93s | Real PNG decode in reportlab. |
| `test_graph_client.py::TestEnvFallback::test_authority_built_from_resolved_tenant` | 0.41s | Imports MSAL. |
| `test_orchestrator.py::test_dry_run_default_halts_after_render` | 0.08s | OK. |
| Everything else | ≤ 0.06s | Suite total 3.12s for 932 tests — fast. |

**No tests >1s.** Nothing visibly slow. (F-004 above flags the 0.93s as
worth a glance but not urgent.)

---

## 7. Standing-rule violations (CLAUDE.md §3 — "ships with QC + self-heal + acceptance tests")

Walked the last 30 days of commits (94 commits since 2026-05-03). Filtered
out those that touched `tests/` in the same commit — the rest are §3
candidates.

### Finding R-001 — Step 14 `gen_rate_intelligence.py` shipped without tests
**Severity: High** · commit `8c81341 "Tier 1: Cross-project client_intelligence + rate-negotiation cheat sheet"`
Whole module + a daily-pipeline step. No `tests/test_gen_rate_intelligence.py`.
QC-coverage exists (QC of generated artifact) but no behavior tests on the
producer. **Effort: M.**

### Finding R-002 — `sentry_seer.py` Seer integration shipped without unit tests
**Severity: Medium** · commit `5b673fe "Sentry Phase 2"`, follow-up `ffb0ebb "Fix Seer endpoint path"`
The bug `ffb0ebb` fixed (`/issues/{id}/...` 404 → `/organizations/{org}/issues/{id}/...`)
is EXACTLY the kind of regression a unit test of `_seer_url()` would have
caught. There is still no test of it; QC-043 + `qc_actions_from_sentry`
checks consume Seer responses but don't lock the URL shape.
**Effort: S.**

### Finding R-003 — `operator_corrections.json` durable-override layer shipped with no test
**Severity: Medium** · commit `cf00ba5 "Operator-corrections layer..."`
Touches `scripts/ingest.py` + `scripts/qc_selfheal.py` to load + apply
operator overrides. No tests added. A buggy override merge could
silently zero-out real data.
**Effort: M.**

### Finding R-004 — `pdf_llm_rescue.py` + `pdf_parser.py` shipped together with no tests
**Severity: Medium** · commit `df95e1b "PDF parsing for booking confirmations..."`
Both at 0% coverage. The fallback chain (PDF → LLM) is exactly the kind
of error-prone code that NEEDS lock-in tests.
**Effort: M.**

### Finding R-005 — Many Step-level fixes (carrier mappings, lane filters) shipped without targeted tests
**Severity: Medium** · commits `9075e6c` (Numidia exclusion), `7ba1ce6`
(trucking exclusion), `a3d5089` (Caucedo mapping), `0d58d84` (Dublin mapping)
Each commit edits `scripts/ingest.py` or `scripts/core.py` adding a single
filter rule. None of them landed with a test asserting the rule fires.
Tests added in `test_smarter_price_classifier.py` are a model — these
should follow.
**Effort: S each (cumulative M).**

### Tests landed WITH the change (compliant — for reference)
- PRs #16, #17, #18, #19 — all shipped with tests. PR #13 (`fix-send-only-win-classifier`) shipped with `test_core_parity.py` + 6 new tests in `test_core.py`. The pattern IS being followed for newer work.

---

## 8. Stale-test collection / `pytest` config hardening

### Context — what happened pre-2026-06-01
Cloud PC fires showed pytest reporting `0 failed, 22 error; coverage None%`.
Root cause: the leftover `hilmar-tracker/` checkout still lived next to
`hilmar-daily-routine/`, and pytest's `rootdir` discovery walked into it,
collecting 22 stale-import modules. PR #18 surfaced the diagnostic; the
fix today (removal of the stale dir + the `_test_root()` Cloud-PC layout
detector in `run_audit_tests.py`) addresses the immediate failure.

### Finding C-001 — `pyproject.toml` has NO `norecursedirs` and NO `collect_ignore`
**Severity: High** · `pyproject.toml:54-78`
Current `[tool.pytest.ini_options]` block is:
```toml
testpaths = ["tests"]
python_files = ["test_*.py"]
pythonpath = ["."]
addopts = "-ra --strict-markers --cov=hilmar --cov-report=term-missing --cov-fail-under=90"
```
`testpaths = ["tests"]` is a hint, not a hard restriction — pytest can
still collect from `rootdir` if invoked with a different cwd (which is
exactly what happened on the Cloud PC). **What to add:** explicit
`norecursedirs = ["plugin-build", "deploy", "deploy_legacy", "reports", "data-backups", "secrets", "src", "scripts", "docs", "assets", ".github", ".devcontainer", "*hilmar-tracker*"]`.
**Effort: XS.** This is a 3-line change and would have prevented the
2026-05-30→06-01 fire entirely.

### Finding C-002 — No `conftest.py` near root to anchor pytest's rootdir
**Severity: Medium**
`tests/conftest.py` exists but no `conftest.py` at the repo root.
Combined with the Cloud PC's exotic ROOT-is-xcopy-dir layout, pytest's
rootdir heuristic can land in unexpected places. A 1-line root
`conftest.py` (even empty) pins it. **Effort: XS.**

### Finding C-003 — `--cov=hilmar` only — `scripts/` is invisible to the gate
**Severity: High (the meta-finding behind §1)**
`addopts` has `--cov=hilmar` but not `--cov=scripts`. The 90% gate is a
LIE for the production code path. **What to add:** add `--cov=scripts`
with `--cov-fail-under=25` initially (current ratio is 11%) and ratchet
it up alongside the work in §2. **Effort: XS to add; effort to MEET the
new gate is L (largely §2 work).**

### Finding C-004 — `pythonpath = ["."]` allows tests to be imported as `tests.X` AND `tests.conftest` — duplicate-module risk
**Severity: Low** · `pyproject.toml:65`
The block-comment explains the reason (Linux CI vs Windows rootdir
inconsistency) but the side effect is `tests.conftest` and `conftest` can
both exist in `sys.modules` — leading to fixtures that don't share state.
There's no observed bug today but it's fragile. **Effort: S** to swap to
src-layout conftest discovery only.

### Finding C-005 — `[project]` name = `"hilmar-tracker"` despite the rename to `hilmar-daily-routine`
**Severity: Low (cosmetic)** · `pyproject.toml:6`
Will confuse future contributors but does not break tests. **Effort: XS.**

### Finding C-006 — `test_pipeline.py` uses bare `import hilmar.qc` without `pytest.importorskip` for ALL chained tests
**Severity: Low**
If any sub-import inside `hilmar` (e.g. `anthropic`) ever becomes a hard
runtime dep that isn't pre-installed on CI, the WHOLE `test_pipeline.py`
file fails to collect — pytest then attributes 7 ERROR rows to it (the
22-error pattern). **What to add:** module-level try/except around the
`from hilmar import orchestrator` at the top of the file.
**Effort: XS.**

---

## Summary by severity

| Severity | Count | Approx fix effort |
|---|---:|---|
| Critical | 7 (T-001…T-006, S-004) | L (mostly producer-side tests for the daily email chain) |
| High | 9 (T-007…T-011, S-002, R-001, C-001, C-003) | M-L |
| Medium | 11 | M |
| Low | 8 | XS-S |

---

## Top 5 test-suite priorities

1. **T-001 — Test `gen_email.build_subject` + `build_body` against the golden fixture.**
   This is THE module that produces the daily client email; 0 direct
   tests on either entry point. Add 4-6 tests: subject format/length,
   body structural blocks present, Outlook-safe HTML (no `linear-gradient`,
   no `data:`, no `&amp;amp;`), distribution invariants. Effort: M. Pays
   down the biggest single risk in the suite.

2. **C-001 + C-003 — Harden `pyproject.toml`: add `norecursedirs`,
   `--cov=scripts` (gate at 25% then ratchet).** Two surgical edits that
   (a) prevent recurrence of the 22-collection-error fire and (b) make
   the 11% scripts-coverage gap visible to the daily audit. Effort: XS.

3. **T-002, T-003, T-004 — One smoke test each for `gen_dashboard.py`,
   `gen_pdf.py`, `outlook_send.py`.** The same `importlib.util` pattern
   `test_loss_reason_mix_html.py` already uses. Locks the production
   render path that the Cloud PC actually executes. Effort: S each.

4. **T-006 — Cross-tree parity test for `qc_selfheal.py` ↔ `hilmar.qc`.**
   Mirrors `test_core_parity.py`. The 2026-05-30 `core.py` drift was
   discovered by accident; a `qc_selfheal.py` drift would silently send
   a broken audit while the test suite stays green. Effort: M-L.

5. **F-001 — Bring in `freezegun`, freeze `datetime.now()` in all 24
   time-dependent tests.** Eliminates the slow-burn class of bugs where
   tests pass for 11 months then fail on a particular weekend or
   midnight crossover. Effort: M, but a one-time investment.
