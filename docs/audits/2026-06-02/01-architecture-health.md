# Hilmar Daily Tracker — Architecture & Code-Health Audit

Date: 2026-06-02
Scope: read-only audit of `hilmar-daily-routine` HEAD (`b092618` — smarter PRICE classifier)
Auditor: Claude (Opus 4.7)

Findings are concrete (file + line + function). Severity tiers:

- **Critical** — broken production behavior, today or imminently.
- **High** — wrong/surprising behavior in a foreseeable scenario.
- **Medium** — quality/maintenance burden; no immediate behavior risk.
- **Low** — nit.

Open PRs #20 (QC-011) and #21 (smarter PRICE classifier) are NOT
re-recommended. Recently merged work (#14 foundation parity test, #16
best-effort, #17 QC-007 drift, #19 liveness) is treated as the new
baseline.

---

## 1. Dual-tree drift — `scripts/` vs `src/hilmar/`

PR #14 closed the gross drift on `core.py` constants + `decide_status`
canonical outcomes. Real subtle drift remains; the parity tests don't
cover enough surface yet.

### 1.1 [Critical] `load_config` silently drops session-path healing in `src/hilmar/core.py`

- `scripts/core.py:343` (`load_config`) calls `_heal_session_paths()`
  to rewrite stale absolute paths from a previous Cowork session into
  the live root in memory.
- `src/hilmar/core.py:272` (`load_config`) does NOT call any heal — it
  just opens whatever `paths.root` says. `_heal_session_paths` is not
  defined in `src/hilmar/core.py`.
- `config.json:34` currently shows `"root": "/sessions/gallant-practical-brown/..."` — a stale Cowork session path. If anything in
  `src/hilmar/` reaches the production pipeline (e.g. via the open
  migration plan in `docs/MOVE-OFF-CLOUDPC.md` + `src/hilmar/send.py`
  / `orchestrator.py`), it will try to write artifacts under a path
  that does not exist on the Cloud PC.

What to do: port `_heal_session_paths` into `src/hilmar/core.py`
identically, OR factor it into a shared `paths.py`-style helper both
trees consume. Add a parity test that calls `load_config()` with a
known-stale `paths.root` and asserts both trees return the live root.

Effort: S.

### 1.2 [High] `save_data` lacks schema validation in `src/hilmar/core.py`

- `scripts/core.py:367-432` defines `validate_data_shape()` and
  `save_data_validated()`. Comment at line 27 says the function is
  invoked "in every save_data path" to catch structural drift before
  it writes to disk.
- `src/hilmar/core.py:283-287` `save_data` writes blindly with no
  validation. `validate_data_shape` / `save_data_validated` are absent.
- The 519-test suite runs against `src/hilmar/`, so tests can write
  invalid-shape data that production's `save_data_validated` would
  refuse. The regression ratchet is incomplete.

What to do: port `validate_data_shape` + `save_data_validated` into
`src/hilmar/core.py`. Add parity test calling
`save_data_validated({"requests":[{"status":"BOGUS"}]}, ..., strict=True)`
and asserting both raise `ValueError`.

Effort: S.

### 1.3 [High] `decide_status` decision tree differs in subtle, behaviorally-significant ways

The `test_decide_status_win_and_send_outcomes_parity` cases (parametrized
in `tests/test_core_parity.py:206`) only cover four input shapes — the
WIN/AWAITING_MDOLX/SEND_NO_BOOKING/MDOLX_NO_SEND outcomes. They do NOT
cover the QUOTED branches, where the trees actually differ:

| Case | scripts/core.py:679-815 | src/hilmar/core.py:570-737 |
|---|---|---|
| `mdolx_refs_all` secondary field | not honored | merged into `has_mdolx_eff` at L578 |
| `send_signal_events` secondary | only used for aging timestamp | also merged into `has_send_eff` at L577 |
| QUOTED + no rate / no lane signal | returns `UNDIFFERENTIATED` (L814) | returns `QUOTED_NOT_BOOKED` (L226) |
| MDOLX-without-send branch order | runs BEFORE the no-response branch (L730) | runs AFTER (L612) |

The branch-order difference (MDOLX_NO_SEND vs NO_RESPONSE) is the most
behaviorally significant: a row with `mdolx_ref` set + no
`response_timestamp` returns `PENDING/MDOLX_NO_SEND` in scripts/ but
`PENDING/MDOLX_NO_SEND` in hilmar/ via a different path — equivalent for
the four pinned cases, but trivially divergent if a future tweak
reorders one branch.

What to do: either rewrite `scripts/core.py.decide_status` to call the
`src/hilmar/` implementation (preferred — collapses the duplicate) or
expand the parity test parameter set to cover every documented decision-
tree branch (NO_RESPONSE, RESPONSE_NO_RATE, OTHER, UNDIFFERENTIATED vs
QUOTED_NOT_BOOKED, `mdolx_refs_all`-only, `send_signal_events`-only).

Effort: M.

### 1.4 [High] `parse_rate` accepts bare numbers in hilmar/ but requires `$` in scripts/

- `scripts/core.py:1253` `_RATE_RX = re.compile(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)")` — requires literal `$`.
- `src/hilmar/core.py:1407-1414` defines TWO regexes
  (`_RATE_DOLLAR_RX` + `_RATE_BARE_RX`) and the function tries both.

A booking confirmation that quotes "3500 / FEU" (no $) parses to 3500 in
hilmar/ and None in scripts/. The PRICE classifier in `decide_status`
runs `parse_rate(ol_rate)` — so the SAME row can be `UNDIFFERENTIATED` in
production (scripts/) but `PRICE` in tests (hilmar/) and the parity test
won't catch it.

What to do: align scripts/ to the dual-regex form, or factor `parse_rate`
into a shared helper. Add a parameterized parity test covering: bare,
$-prefixed, comma-separated, decimal, integer, None.

Effort: S.

### 1.5 [Medium] `parse_subject_lane` strips MDOLX-prefix HILMAR tag in scripts/ but not in hilmar/

- `scripts/body_parser.py:274-282` strips a leading "HILMAR" customer
  tag on MDOLX subjects ("MDOLX260432_ ... HILMAR 2X40'RF Oakland to
  Yokohama// CMA: ..."). Comment cites the 2026-05-05 "Hilmar →
  Yokohama" bug Michael flagged.
- `src/hilmar/body_parser.py:245-280` doesn't have this strip — would
  re-introduce the same "Hilmar → Yokohama" parse error if production
  ever switched to the src/ implementation.

What to do: copy the MDOLX-HILMAR strip into `src/hilmar/body_parser.py`.
Add a parity test fixture using the exact subject from the screenshot.

Effort: XS.

### 1.6 [Medium] `body_parser` API surface differs substantially

Function-level inventory (top-level defs only):

| In scripts/ only | In src/hilmar/ only |
|---|---|
| `parse_subject_containers` | `parse_container_spec`, `parse_container_spec_from_subject` (alias) |
| (uses inline carrier walk in patch_carriers) | `_find_carrier`, `parse_vessel`, `parse_transshipment`, `parse_mbd_rate_columns`, `parse_signer`, `parse_send_signal` |
| `_collapse_multiline_pipe_table`, `_find_table_rows`, `_norm_header` | (none — `parse_rate_table` is monolithic) |

Several of the hilmar-only parsers (`parse_vessel`, `parse_transshipment`,
`parse_send_signal`) are documented in `body_parser`'s scripts/-side
docstring (`scripts/body_parser.py:13-18`) as being THERE — they aren't.
The docstring lies.

What to do: either (a) port the missing parsers into `scripts/body_parser.py`
and reconcile naming, or (b) update the scripts/ docstring to reflect
reality and add a "see src/hilmar/body_parser.py for advanced parsers"
pointer. (a) is preferred — `parse_signer` already used to be in
`scripts/core.py` and is duplicated there now (see 1.10).

Effort: M.

### 1.7 [Medium] `ingest.py` has effectively zero parity

- `scripts/ingest.py` (1423 lines) is procedural, file-IO + state-merge.
- `src/hilmar/ingest.py` (1650 lines) is class-based (`IngestConfig`,
  `MessageMeta`, `IngestDryDiff`), introduces idempotent merge with
  `_RECOMPUTED_FIELDS` (L1323), has `fetch_window` that talks to
  `GraphClient` directly, has `backfill_quoted_containers` /
  `backfill_standalone_rates` / `finalize_status` — none of which exist
  on the scripts/ side.

There's no parity test. The two ingest pipelines have diverged into
two different products.

What to do: this is the largest single piece of the migration plan in
`docs/HANDOFF.md`. Treat it as a discrete project — at minimum, document
which tree's ingest is authoritative for which behaviors (production →
scripts/, accuracy bench → hilmar/), and add a "shared-data-shape" test
that round-trips both pipelines through the same `tests/fixtures/`
input and asserts the resulting `requests[]` rows are field-equal on
the columns both populate.

Effort: L.

### 1.8 [Medium] `Iterable` imported from two different modules

- `scripts/core.py:23` `from typing import Any, Iterable` (deprecated path)
- `src/hilmar/core.py:20` `from collections.abc import Iterable` (correct path)

`Iterable` from `typing` is deprecated as of 3.9 and slated for removal.
Cosmetic but illustrative of how a "looks aligned" header drifts.

What to do: standardize on `collections.abc.Iterable` in both trees.

Effort: XS.

### 1.9 [Medium] `trade_region` lives under TWO different names

- `scripts/core.py:167` `trade_region_for(destination)` + map at L107
- `src/hilmar/core.py:1533` `trade_region(destination)`
- `src/hilmar/qc.py:494` uses `core.trade_region(r.get("destination"))`
- `scripts/qc_selfheal.py:2650` + `scripts/gen_pdf.py:355` etc. use
  `core.aggregate_trade_regions(...)` (only in scripts/)
- `aggregate_trade_regions` does NOT exist in `src/hilmar/core.py`.

Same purpose, two names, asymmetric API. Any future code reading from
hilmar/ that wants the aggregation has to either re-implement it or
reach into scripts/.

What to do: pick one name (`trade_region`) and port both `trade_region`
and `aggregate_trade_regions` into both trees.

Effort: S.

### 1.10 [Medium] `parse_signer` lives in `core` in scripts/ but in `body_parser` in hilmar/

- `scripts/core.py:1412` `parse_signer(from_name, body=None)` —
  takes TWO args.
- `src/hilmar/body_parser.py:936` `parse_signer(text: str)` — takes
  ONE arg.

Different module home (layering boundary differs), different signature.
A caller that switched implementations would silently break:
`C.parse_signer(None, body)` becomes `parse_signer(None)` in hilmar/ form.
Today scripts/fetch_bodies.py:216 + scripts/ingest.py call the 2-arg form;
src/hilmar/ingest.py:1078 calls the 1-arg form.

What to do: settle on `body_parser.parse_signer(text)` as the home
(signature parsing is a body concern, not a status concern); keep a
backwards-compat 2-arg shim in `scripts/core.py` that calls into the
canonical implementation.

Effort: S.

### 1.11 [Low] CLAUDE.md says `parser_accuracy.py` exists in BOTH trees — it doesn't

- CLAUDE.md §2 lists `parser_accuracy.py` among the paired files.
- `scripts/parser_accuracy.py` does not exist; only
  `src/hilmar/parser_accuracy.py`. Both production callers
  (`scripts/qc_selfheal.py:1895`, `scripts/qc_actions_from_sentry.py:350`)
  cross the tree boundary via `from hilmar.parser_accuracy import ...`.

Stale orientation doc. Not a code bug — just misleading.

What to do: fix CLAUDE.md §2 paired-files paragraph.

Effort: XS.

---

## 2. Dead code

### 2.1 [Medium] Orphaned scripts (no production / test / CLI reference)

These have zero references outside their own file in any `.py`, `.cmd`,
`.md`, or `.json` in the repo:

| File | Lines | Status |
|---|---|---|
| `scripts/gen_email_new.py` | 800 | Pre-`gen_email.py` draft; diff from current shows it's missing the `viz` + `branding` imports plus an except branch. Pure dead-weight. |
| `scripts/append_one.py` | 74 | Phase-2 backfill helper from spring 2026. Imports `fetch_bodies` + `merge_shards` + `upsert_one`. |
| `scripts/upsert_one.py` | 59 | Sister to append_one. |
| `scripts/batch_upsert_fetched.py` | 98 | One-shot rebuild helper. |
| `scripts/merge_shards.py` | 80 | Used only by `append_one.py`. |
| `scripts/build_ingest_extract.py` | 404 | One-shot data builder; uses `merge_ingest`. |
| `scripts/build_ops_flow_inquiries.py` | 590 | Sister builder. |
| `scripts/build_rate_responses.py` | 227 | Sister builder. |
| `scripts/build_real_sample.py` | 162 | Test-fixture builder. |
| `scripts/restructure_two_table.py` | 259 | One-shot rebuilder (uses `link_mdolx_wins`). |
| `scripts/parse_ol_table.py` | 150 | Standalone parser; superseded by `body_parser.parse_rate_table`. |
| `scripts/identify_recaps.py` | 13 | 13-line shell. |
| `scripts/debug_recap_tables.py` | 21 | One-shot debug. |
| `scripts/extract_hilmar_recaps.py` | 565 | One-shot extractor. |
| `scripts/extract_msg_metadata.py` | 92 | Used only by `build_ops_flow_v2`. |
| `scripts/build_ops_flow_v2.py` | 482 | Used only by `extract_msg_metadata`. |
| `scripts/healthcheck_ping.py` | 114 | No referencer; the heartbeat path is now `deploy/run_daily_laptop.cmd` Step 6 via gh CLI. |
| `scripts/sentry_dashboard_setup.py` | 172 | One-shot dashboard creator. |
| `scripts/backfill_bodies.py` | 150 | One-shot. |
| `scripts/backfill_mdolx.py` | 285 | Referenced only from `parser_accuracy.py` docstring + `patch_carriers.py` (in a no-op fallback path). |
| `scripts/merge_ingest.py` | 224 / `merge_ingest_v2.py` 273 | One-shot helpers. |

These eat ~5,500 LOC of repo surface, slow CI smoke-import, confuse
new readers about what's load-bearing, and create attractive nuisances
(the next bug fix may "fix" a dead file).

What to do: move them out of `scripts/` into `scripts/legacy/` (parallel
to `deploy_legacy/`), or hard-delete after one final tag. The smoke-
import step in `.github/workflows/test.yml` will catch any production
import that was actually using them.

Effort: S to relocate; M to delete safely (one fire to confirm nothing
silently broke).

### 2.2 [Low] `src/hilmar/orchestrator.py`, `render.py`, `send.py`, `feedback_ingest.py`, `app_auth.py`, `backfill.py`, `logging_config.py` have zero production callers

These live only in `src/hilmar/` and are exercised exclusively by the
test suite. That's by design — the README §2 calls them the "migration
target" — but a developer should not edit `send.py` thinking it ships
the daily email. It doesn't; `scripts/outlook_send.py` does.

What to do: add a one-line header banner to each
("NOT-IN-PRODUCTION — migration-target module; production sender is
`scripts/outlook_send.py`"). Don't move the files.

Effort: XS.

### 2.3 [Low] `fetch_bodies.py:67` `import fetch_bodies as FB` chain in `refresh_stage.py` re-implements `_parse_all`

`scripts/refresh_stage.py:67` imports `fetch_bodies` to reuse `_parse_all`,
but `fetch_bodies._parse_all` (L122) and `ingest._parse_all` /
`hilmar/ingest._parse_all` (L316) are three separately-evolved copies
of the same notional helper.

What to do: collapse to one canonical `body_parser.parse_all(text, subject, bucket)` and have the three call sites import it.

Effort: S.

---

## 3. God functions (>200 lines)

| Lines | File:line | Function | Notes |
|---|---|---|---|
| 1,878 | `scripts/qc_selfheal.py:751` | `phase_6_rules` | This is the entire 46-check QC matrix in one function. Each QC-NNN check is a nested `try:` block. Every new check makes it worse. **This is the single most pressing structural problem in the codebase.** |
| 870 | `scripts/gen_dashboard.py:139` | `render` | Builds the entire HTML dashboard inline (KPIs, tabs, tables, mobile-responsive CSS, sparklines). Hard to test piece-by-piece. |
| 416 | `scripts/patch_carriers.py:380` | `main` | 4-pass carrier enrichment + PDF extraction + writeback, all in `main`. Should be 4 named pass-functions. |
| 373 | `scripts/pdf_parser.py:134` | `parse_booking_pdf` | Multi-format PDF dispatch + heuristic fallback. Splittable into per-carrier handlers. |
| 301 | `scripts/ingest.py:529` | `link_bookings_to_requests` | Matching algorithm — In-Reply-To/References + carrier + container + lane. The brains of standalone-WIN detection. Testable as a pure function but currently too long to reason about. |
| 292 | `scripts/gen_email.py:419` | `_today_block_html` | "Today's" KPI block for the email. |
| 283 | `scripts/build_ops_flow_v2.py:196` | `build` | Dead code (see 2.1). |
| 273 | `src/hilmar/qc.py:166` | `phase_3_entries` | The src/-side 10-phase QC; phase_3 alone is 273 lines but the function is still scoped to one phase, unlike `phase_6_rules`. |
| 268 | `scripts/refresh_stage.py:469` | `main` | Two Graph queries + body fetch + classify + dedupe + write — all `main`. |
| 266 | `scripts/gen_improvements_report.py:186` | `collect_red_flags` | The audit-email "red flags" enumerator. Should be a per-flag iterator. |
| 245 | `scripts/qc_selfheal.py:399` | `phase_3_entries` | scripts/-side phase_3 — comparable to src/'s but they have drifted in 1.7's sense. |
| 212 | `scripts/gen_improvements_report.py:834` | `_sentry_section_inline` | Renders a Sentry section. |
| 212 | `scripts/build_ingest_extract.py:190` | `main` | Dead code (see 2.1). |
| 201 | `scripts/gen_improvements_report.py:454` | `collect_observations` | Audit observations enumerator. |
| 200 | `scripts/run_tests.py:48` | `run_core_tests` | Inline test runner. |

### 3.1 [High] `phase_6_rules` is structurally unmaintainable

1,878 lines, 53 QC-NNN distinct checks (counted from `QC-NNN` literal
occurrences in `scripts/qc_selfheal.py`). Each check is loosely bounded
by a comment header — there's no enforced "one function per check"
boundary. Symptoms today:

- `QC-040` and `QC-039` cross-reference each other inside the same
  enormous function (`scripts/qc_selfheal.py:151, 1967`).
- `_drift_findings` (L1993) is a name reused at multiple checks.
- New checks tend to grow nested `try/except: pass` to insulate from
  earlier checks' state leakage (L1964, L2026).
- Phase-aware Sentry suppression (`HILMAR_QC_PHASE=pre-patch`) requires
  reading the variable inside this giant function, line 90 (helper
  `_qc_phase_is_pre_patch`).

What to do: extract each QC-NNN to its own `def check_qc_NNN(log, data,
ctx) -> None`, register them in a list, iterate. The QC-INDEX.md already
documents the matrix; this turns the index into executable structure.
This unlocks per-check unit tests too (currently impossible — you'd
have to invoke the whole phase).

Effort: L. Worth scheduling — every audit will keep flagging this until
it's done.

---

## 4. Tangled modules / layering violations

### 4.1 [Medium] Production code imports across the tree boundary in 4 places

```
scripts/qc_selfheal.py:1895  from hilmar.parser_accuracy import compute_accuracy, ACCURACY_THRESHOLD, CRITICAL_FIELDS
scripts/qc_selfheal.py:1978  from hilmar import core as _h_core
scripts/qc_selfheal.py:2079  from hilmar.core import detect_classifier_form
scripts/qc_actions_from_sentry.py:350  from hilmar.parser_accuracy import compute_accuracy
```

That's not necessarily wrong — it's how `parser_accuracy.py` lives only
in hilmar/ but production needs the score. But the import is done
lazily inside a function with sys.path manipulation, which means a
cold-broken `src/hilmar/` (e.g. missing dep) silently degrades QC-039
to a `log.warn` (`scripts/qc_selfheal.py:1965`). The 95% gate is
supposed to be a hard gate — falling back to WARN on import error
means the gate effectively isn't.

What to do: make `import hilmar.parser_accuracy` a top-of-file import
in production qc_selfheal so a missing module is loud; OR add a QC-051
"parser_accuracy module importable" check that fires Sentry at
ERROR-severity.

Effort: XS.

### 4.2 [Low] `scripts/run_pipeline.py:138` runtime `sys.path.insert(0, str(SCRIPTS))`

The orchestrator mutates sys.path at module-load time so it can `import
sentry_setup` from `scripts/`. Works but means anyone tracing imports
has to know this. Mild surprise.

What to do: leave as-is; flag for future. The pattern is consistent
across `scripts/` (each script does `sys.path.insert(0, str(__file__.parent))`
to import its siblings as bare names rather than `from scripts import`).

Effort: N/A (cosmetic).

### 4.3 [Low] `viz.py` is a healthy shared-utility module

Worth calling out the good case: `scripts/viz.py` is imported by
`gen_dashboard`, `gen_pdf`, `gen_email`, `gen_carrier_scorecard_pdf`,
`gen_weekly_summary`, `gen_rate_intelligence` — exactly the leaf
modules a viz utility should serve. No circular imports detected
anywhere via grep.

---

## 5. Refactoring debt — legacy aliases / shims

### 5.1 [Low] `send_signal_stale = is_business_stale` alias

- `scripts/core.py:676` and `src/hilmar/core.py:567` both assign
  `send_signal_stale = is_business_stale` as a backward-compat alias.
- Only ONE non-test caller exists: `scripts/core.py:747`, inside
  `decide_status` itself.
- `test_core_parity.py:100` `test_send_signal_stale_alias_is_same_callable`
  explicitly locks the alias in place.

The alias has effectively zero external users (grep over the repo finds
none). It exists for hypothetical migration safety only.

What to do: replace the one production caller with the canonical name
and delete the alias + the parity test for it. The "alias must exist"
assertion is calcifying dead surface.

Effort: XS.

### 5.2 [Low] `VALID_STATUSES` lives as a documented intentional drift

`ALLOWED_CROSS_FOLDER_DRIFT` (`tests/test_core_parity.py:112`) and
QC-040 (`scripts/qc_selfheal.py:1990`) both record that `VALID_STATUSES`
is intentionally different (LEGACY 3-set in scripts/, STRICT∪LEGACY in
hilmar/). It's correct as-of-today, but it institutionalizes the
LOSS-vs-Q&L vocabulary split. Every new piece of code has to decide
which set of statuses it consumes.

What to do: schedule (post-stabilization) a "deprecate LEGACY" project
that flips all stored rows + downstream consumers to STRICT, then
deletes `VALID_STATUSES_LEGACY` from both trees. Not urgent.

Effort: L.

---

## 6. Naming / vocabulary consistency

### 6.1 [Medium] LOSS vs Q&L vocab is split everywhere

In `scripts/`: 88 occurrences of literal `"LOSS"` (decisions, comparisons,
display branches) vs 3 of `"Q&L"`. Q&L is the **display** label; LOSS is
the **storage** label. The split is intentional but undocumented inline,
so reading `scripts/core.py:202`
```
elif st == "LOSS" and lr == "NO_RESPONSE":
```
requires knowing that this row will RENDER as "NQ" in the email, not "LOSS".

What to do: add a `STATUS_DISPLAY = {"LOSS": "Q&L", "PENDING": "Pending", ...}`
constant + a `display_status(r)` helper (the latter exists in
`src/hilmar/core.py:121` — just not in `scripts/`). Then every render
site goes through one funnel and the vocab split is documented.

Effort: S.

### 6.2 [Low] `trade_region_for` vs `trade_region` (covered under 1.9).

### 6.3 [Low] `parse_subject_containers` vs `parse_container_spec` (covered under 1.6).

---

## 7. Configuration smell

### 7.1 [Medium] Unused config.json keys

Counting references in `scripts/` + `src/`:

| Key | Refs | Status |
|---|---|---|
| `_distribution_history` | 0 | Documentation comment, fine. |
| `after_hours_large_teu_threshold` | 0 | Dead. |
| `escalation_cc`, `escalation_to`, `escalation_cooldown_hours` | 0 each | Escalation flow not wired. |
| `files_to_upload` | 0 | OneDrive upload not used (production uses `outlook_send` attach). |
| `folder_name` | 0 | OneDrive folder name not used. |
| `mailboxes_to_scan` | 0 | Ingest scans via Graph `$search`, not via this list. |
| `mcp_connectors` | 0 | Legacy from Cowork-MCP era. |
| `overdue_no_response_hours` | 0 | Dead. |
| `rule_filter` | 0 | Dead (refresh_stage hard-codes the filter). |
| `suspicious_biz_hours` | 0 | Dead. |
| `onedrive.folder_id` | 3 — only in `src/hilmar/send.py` | Production doesn't use this path. |

11 keys with zero code consumers; another 1 with consumers only in the
not-in-production tree. They're noise in a config that gets edited
under pressure (e.g. iteration locks on `distribution.full_list`).

What to do: delete (or move to `_legacy: {...}` block) the 11 zero-ref
keys. Add a QC-NNN that walks `config.json` keys against the set of
keys read by `core.load_config` + grep-found consumers, and warns on
orphans.

Effort: S.

### 7.2 [Low] `paths.root` stale Cowork session path

`config.json:34` reads `"root": "/sessions/gallant-practical-brown/..."`.
`_heal_session_paths` (scripts/) rewrites this in memory, but every new
fresh-session pull sees the bad value first. Mostly harmless because
the heal works, but the file SHOULD be the production root.

What to do: rewrite `config.json:paths.*` to relative or env-var paths
(e.g. `"${ROOT}/scripts"`) once on the Cloud PC and commit. The heal
becomes vestigial after that.

Effort: S.

### 7.3 [Low] `rules.pending_aging_hours` is 24 but `PENDING_WINDOW_HOURS` is 48

- `config.json:84` `"pending_aging_hours": 24`
- `scripts/core.py:45` `PENDING_WINDOW_HOURS = 48`

These COULD be the same setting; the constant overrode the config a
month ago (per the comment at L38) but the config still carries the
old value. The constant wins (grep confirms `pending_aging_hours` has
0 callers in scripts/src/). Misleading to a reader who'd assume config
is the source of truth.

What to do: either delete `pending_aging_hours` from config OR move
`PENDING_WINDOW_HOURS` to read from config (with the test asserting
config matches the in-code default). Pick one source of truth.

Effort: XS.

---

## Top 5 architectural priorities

Ranked by combined severity + leverage.

1. **Refactor `phase_6_rules` (1,878 lines, 53 checks).**
   *Section 3.1 — High.* The single biggest structural debt. Until this
   is broken into one-function-per-check, every new QC-NNN compounds the
   problem and per-check unit testing is impossible. Effort: L, but
   unlocks every future audit's ability to recommend smaller changes.

2. **Port `_heal_session_paths` + `save_data_validated` to `src/hilmar/core.py`.**
   *Sections 1.1, 1.2 — Critical + High.* These are the parity gaps
   that will silently break the moment any `src/hilmar/`-tree code
   ships in production. Cheap (S each) and remove a category of
   surprise-bug.

3. **Expand `test_core_parity.py` to cover the QUOTED branches + secondary-signal fields in `decide_status`.**
   *Section 1.3 — High.* The parity test only covers four input shapes.
   The actual divergence (Section 1.3, 1.4) lives in the QUOTED side of
   the tree. Add ~8 parameterized cases covering NO_RESPONSE,
   RESPONSE_NO_RATE, ETD_MISS, PRICE-with-rate-gap, PRICE-with-bare-rate,
   UNDIFFERENTIATED-vs-QUOTED_NOT_BOOKED, `mdolx_refs_all`-only,
   `send_signal_events`-only. Effort: M. Each new case will likely
   FAIL on commit — that IS the audit finding.

4. **Excise dead-code scripts (Section 2.1 — ~5,500 LOC, 21 files).**
   *Medium.* Move to `scripts/legacy/` first to keep the diff reversible;
   delete after one daily fire confirms nothing important broke.
   Reduces cognitive load for every subsequent piece of work.

5. **Pick one tree as canonical per module and document.**
   *Sections 1.6, 1.7, 1.10 — Medium.* `body_parser.py` and `ingest.py`
   are wholly different products today. Either (a) commit to scripts/
   being production-canonical until the migration ships and keep
   src/hilmar/ as the test target only — in which case the divergence
   is acceptable but should be documented per-function — or (b)
   actually migrate piece-by-piece, starting with the smaller surface
   (`parse_signer`, `parse_subject_lane`, `trade_region`). Pick one and
   record it in CLAUDE.md §2 with a concrete migration order.

---

## Appendix — checks NOT flagged

Categories examined that produced no findings:

- **Circular imports.** None detected; `viz.py` + `branding.py` are
  clean leaves, `core.py` doesn't import any leaf module in either tree.
- **Layering violations in `core.py`.** Neither tree's `core.py` imports
  `gen_email`, `gen_dashboard`, `outlook_send`, or any I/O-heavy
  sibling. Good.
- **`scripts/run_pipeline.py` step ordering.** PR #16's best-effort
  classification is in place; carrier scorecards / share_intel / Sentry
  / rate-intel / Turso are all flagged `BEST_EFFORT_STEPS` correctly
  (L126).
- **`outlook_send.py` distribution-list logic.** `--to-from-config` is
  the only path the wrapper uses; full vs test list is selected via
  flag and QC-022 still gates accidental edits. Clean.
- **`viz.py` reuse pattern.** Healthy — 6 callers, all are render-leaf
  modules. (Section 4.3.)
