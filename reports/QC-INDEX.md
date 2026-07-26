# Hilmar Tracker — QC + Self-Heal Index

Source of truth for every QC check in the pipeline. Each row pairs a check
with the failure mode that triggered it (per Michael's standing rule:
"every new code pattern ships with QC + self-heal in the same commit").

Generated 2026-05-13, updated 2026-07-10. Total active checks: **66**
(QC-001 through QC-067, including the QC-014a/b and QC-020a/b sub-variants;
QC-038 retired). Last commit: see `git log scripts/qc_selfheal.py`.
The newest checks are QC-055..QC-063 (added 2026-06-25), QC-064
(garbage display-field guard, 2026-06-27), QC-065 (client-report
invariants, 2026-07-10); see the matrix below.

**Three drift-prevention + accuracy checks added 2026-05-17 evening** (historical
note — at the time these were the newest; the current ceiling is QC-063):
- **QC-039** (ERROR) — Parser accuracy ≥95% on critical fields. Gates ship.
  (Originally 98%; lowered to 95% on 2026-05-19 — see the standing-rule note below.)
- **QC-040** (WARN) — Cross-folder drift between `scripts/` and `src/hilmar/`: `core.py` enums (VALID_STATUSES, LOSS_REASONS) + `body_parser.py` `KNOWN_ORIGINS`.
- **QC-041** (ERROR) — Classifier form consistency in production data (no mixed LEGACY/STRICT).

Per Michael's standing rules in `~/.claude/CLAUDE.md`:
- *"this parser and your system have to run at minimum of 98 percent accuracy no matter COST"*
- *"never to allow drift like this as standard"*

> Note: the 98% directive above was superseded on 2026-05-19 (*"PARSER MUST
> REACH 95 PERCENT AT A MINIMUM AND INCLUDE ATTACHMENTS"*). The live gate is
> `ACCURACY_THRESHOLD = 0.95` — see `src/hilmar/parser_accuracy.py:57` and the
> in-code comment at `scripts/qc_selfheal.py:2149`.

## How to read this table

- **Severity**: ERROR = pipeline gates / blocks bad data; WARN = surfaced in
  audit but doesn't stop ship; OK = informational.
- **Catches**: the failure mode the check was added to detect.
- **Self-heal**: automatic remediation when safe (vs alert-only).

## QC matrix

| # | Severity | Catches | Self-heal | Added |
|---|---|---|---|---|
| QC-001 | WARN | Suspicious Q&L count (0 with many entries — classifier drift) | None — surface only | 2026-04 baseline |
| QC-002 | WARN | WIN with no carrier_won after auto-heal | None — manual review | 2026-04 baseline |
| QC-003 | WARN | WIN without chain-send signal or MDOLX ref | None — manual review | 2026-04 baseline |
| QC-004 | ERROR | NQ contamination (NQ entries with quoted flag) | Auto-fix to LOSS | 2026-04 baseline |
| QC-005 | WARN | Suspicious business-hours math (turnaround anomaly) | None | 2026-04 baseline |
| QC-006 | WARN | Suspiciously-large TEU on a single row | None | 2026-04 baseline |
| QC-007 | ERROR | PENDING-Hilmar past the decision window (48 clock-hrs, 72 if OL quoted Friday ET; Michael 2026-07-14) — state machine should have aged it to Q&L | Auto-age to LOSS via `core.pending_hilmar_stale` in the state machine | 2026-07-14 |
| QC-008 | WARN | Stage file > 36h old (refresh_stage stale) | None | 2026-05-05 |
| QC-009 | WARN | Stage bucket distribution drift (one bucket silent 7d) | None | 2026-05-05 |
| QC-010 | WARN | preserved_from_prior WIN count > 10 (refresh missing emails) | None | 2026-05-05 |
| QC-011 | ERROR | Email subject date != TODAY's report biz day (a fresh PREVIOUS-day subject = morning-framing regression) | None — gates ship | 2026-05-07 (`697e219`); evening-fire flip 2026-06-16 |
| QC-012 | ERROR | Week labels not Mon-Fri (4-day span) — Mon-Sun regression | None | 2026-05-07 (`401ca08`) |
| QC-013 | ERROR | Body header "What Happened Today" — today framing regression | None | 2026-05-07 (`401ca08`) |
| QC-014a | ERROR/WARN | WIN carrier coverage <90% (ERROR) / <95% (WARN) | Auto via patch_carriers | 2026-05-07 (`c24255d`) |
| QC-014b | ERROR/WARN | Q&L carrier coverage <40% (ERROR) / <60% (WARN) | Auto via body-scan | 2026-05-07 (`c24255d`) |
| QC-015 | **ERROR**/WARN | **Unresolved lane in a client-facing surface** — ERRORs when ANY unresolved row (destination None/""/"Unknown" case-insensitive, OR lane == "Lane unresolved") WOULD render client-facing: a WIN inside the client email's active-shipments window (last 14 days, mirrors `gen_client_email._active_shipments`) OR any TODAY-dated staff-section row. "within tolerance" GREEN is permissible ONLY when every unresolved row is a non-rendered historical-tail row. Retains the historical-tail map tiers: Unmapped destinations >10 (ERROR) / >5 (WARN). | None — assign the true lane on the source row / extend `_TRADE_REGION_MAP`. Poisoned "Unknown" literals are healed to None upstream (phase_3 entry-heal); the CLIENT never sees these rows (gen_client_email `_lane_resolved` filter + gen_pdf region exclusion). | 2026-05-07 (`c24255d`); contract rewritten 2026-07-14 (run 29292014093) |
| QC-016 | ERROR/WARN | Backup retention >2x cap (ERROR) / >retention+5 (WARN) | Auto-prune in backup.py | 2026-05-07 (`c24255d`) |
| QC-017 | ERROR/WARN | Single carrier holds >75% of quotes (ERROR) / >65% (WARN) | None — boilerplate guard | 2026-05-08 (`c13d831`) |
| QC-018 | ERROR | Day-row total != sum(W, Q&L, NQ, P) — hidden status math break | None — gates ship | 2026-05-08 (`f6aae29`) |
| QC-019 | ERROR | Status-change rows on report date lack carrier_quoted | Auto via patch_carriers PENDING extension | 2026-05-13 (`505f644`) |
| QC-020a | WARN | NQ section display rows older than 14 days (cutoff broken) | None | 2026-05-13 (`a6bc3d2`) |
| QC-020b | ERROR | Display window leaked into summary.not_quoted aggregate | None — gates ship | 2026-05-13 (`a6bc3d2`) |
| QC-021 | WARN | Today's wrapper completed pipeline but no "Sent. request-id=" follows | None — alert only | 2026-05-13 (new) |
| QC-022 | ERROR | Distribution list missing idealx.us OR has external domain OR wrong count | None — gates next send | 2026-05-13 (new) |
| QC-023 | ERROR/WARN | MSAL token cache > 80d (ERROR) / > 60d (WARN) — silent refresh failing soon | None — manual re-auth | 2026-05-13 (new) |
| QC-024 | ERROR/WARN | Stage path drift (legacy .jsonl newer than .txt) | None | 2026-05-13 (new) |
| QC-025 | ERROR/WARN | Today's sent-flag has > 5 entries (ERROR) / > 3 (WARN) — looping | None — investigate | 2026-05-13 (`cd5fe6c`) |
| QC-026 | WARN | Scripts in OneDrive drift from git repo (>3 files differ) — remote-edit sync broken | Auto via wrapper Step 0 git-pull next fire | 2026-05-13 (`30f6cd9`) |
| QC-027 | ERROR/WARN | Data completeness <90% on key fields (etd/eta/vessel/rate/carrier/pol/pod) | Auto via patch_carriers PASS 2 + PDF fallback | 2026-05-13 (`6efb1ad` → `df95e1b`) |
| QC-028 | WARN | Rate intelligence artifact missing or stale (>26h) | None — surface only | 2026-05-13 (`8c81341`) |
| QC-029 | WARN | Shared cross-project store stale or row-count drift between local + shared | None — investigate | 2026-05-13 (`8c81341`) |
| QC-030 | ERROR/WARN | Transit-time pair (ETD+ETA) coverage <85% on active rows | Auto via parse_rate_table extracting both dates | 2026-05-13 (`1c4f38f`) |
| QC-031 | WARN | SHARED/client_intelligence/SCHEMA.md missing (cross-project integrators lack contract) | None — surface only | 2026-05-13 (`1c4f38f`) |
| QC-032 | ERROR/WARN | Backup stale: Cloud PC = dual OneDrive/local dirs (>36h); blob-store host (GH Actions) = dated snapshot blobs from `state_store.py backup` (>1.5d warn, >3d error) | None — wrapper Step 4.9 / workflow backup step rerun each fire | 2026-05-14 (`e2ed228`), blob shape 2026-06-12 |
| QC-033 | ERROR/WARN | Hilmar brand logo missing or corrupted (assets/branding/hilmar-logo.{svg,png}) | None — graceful fallback to emoji+text in headers | 2026-05-14 (`862e2ec`/`072e569`) |
| QC-034 | ERROR | tracking-data-v2.json shape invalid (missing keys / wrong types / invalid status/loss_reason enums) | None — gates ship; structural drift surfaced loudly | 2026-05-14 (best-practices batch) |
| QC-035 | ERROR/WARN | stage_emails.txt >20MB (ERROR) / >5MB (WARN) — unbounded stage growth | None — run `refresh_stage.py --rotate-stage-older-than 90` | 2026-05-14 (best-practices batch) |
| QC-036 | ERROR/WARN | tests/ folder missing or <3 test files — regression net thin | None — write more tests | 2026-05-14 (best-practices batch) |
| QC-037 | WARN / **ERROR** | ol-quote-tracker sync log missing, stale (>36h), or last sync errored. **ERROR-severity** when ≥3 consecutive fires fail (Turso entity registry going stale; audit also raises a dedicated red flag with the actual error excerpt). | None — surfaces APP_PASSWORD missing or endpoint failure | 2026-05-16 (`c8c3d14`), streak detection 2026-05-28 |
| QC-038 | _retired 2026-05-21_ | ol-quote-tracker reconciliation — retired: a live API probe proved ol-quote-tracker holds zero Hilmar rows, so the cross-check only ever produced phantom drift | n/a — check + script + pipeline step removed | 2026-05-21 |
| QC-039 | **ERROR**/WARN | Parser accuracy <95% on CRITICAL fields (ERROR) or <95% overall / non-critical field below (WARN) | None — gates ship until backfill or parser fix | 2026-05-17 (consolidation; threshold lowered 98%→95% 2026-05-19) |
| QC-040 | WARN | Undocumented cross-folder drift between `scripts/` and `src/hilmar/`: `core.py` enums (VALID_STATUSES via LEGACY view + LOSS_REASONS strict) and `body_parser.py` `KNOWN_ORIGINS` (strict — the constant whose drift caused the "Hilmar -> X" lane-bucket bug) | None — operator must align or add to allowed-drift list | 2026-05-17 (consolidation; body_parser.KNOWN_ORIGINS added 2026-06-26) |
| QC-041 | **ERROR** | tracking-data-v2.json has MIXED classifier forms (some rows with LOSS, some with Q&L/NQ) — parser bug | None — investigate ingest split-classifier write | 2026-05-17 (consolidation) |
| QC-042 | **ERROR** | Email body contains a `data:` URI (`<img src="data:image/...">`) — Outlook blocks these so the logo renders broken | None — `branding.py` uses `cid:` attachments (commit `fa337b2`) | 2026-05-17 |
| QC-043 | WARN | Sentry self-improvement loop — surfaces unresolved-issue count + hot issues (≥5×/24h) into the audit | None — informational meta-check | 2026-05-17 |
| QC-044 | **ERROR** | Double-escaped HTML entities (`&amp;amp;`) in email body — a helper ran `_esc()` on already-escaped text | None — Seer-routed; fix the double-escaping call site | 2026-05-17 |
| QC-045 | **ERROR** | Table-header invisible in Outlook — header row uses `background:linear-gradient` with no solid `background-color` fallback (Outlook strips gradients → white-on-white) | None — Seer-routed; add `background-color:` fallback | 2026-05-17 |
| QC-046 | **ERROR**/WARN | Pending-timestamp population / Windows strftime safety — guards the `%-d`/`%-I` Unix-only formats that `ValueError` on the Cloud PC | None — use `%d`/`%I` + `.replace(" 0", " ")` | 2026-05-17 |
| QC-047 | **ERROR** | Win Rate KPI tile disagrees with the explainer banner — the headline number and its Wins/(Wins+Q&L) explanation must reconcile | None — gates ship; fix the renderer | 2026-05-17 |
| QC-048 | WARN | Turnaround sanity — flags any row with business-hours turnaround >40h as an implausible mis-paired timestamp | Auto-clamp implausible turnaround to None (commit `c36524e`) | 2026-05-17 |
| QC-049 | WARN | WIN rows missing an MDOLX booking ref (unconfirmed wins) past 7 days — honest cross-check against phantom wins | None — booking-team auto-notify (see automations below); operator links MDOLX or demotes to Q&L | 2026-05-17 (`ad9ecbf`) |
| QC-050 | **ERROR**/WARN | Backup freshness + retention — newest snapshot stale (ERROR) or retention count drifted | Auto-prune in `backup.py`; offline rerun in wrapper Step 4.9 | 2026-05-17 (`5b50aa7`) |
| QC-051 | **ERROR** | Phantom-duplicate WIN guard — verifies `phase_4`'s content-dedup didn't leave two WIN rows for the same booking | Collapse phantom duplicate wins in `phase_4` (commit `eac597f`) | 2026-05-17 |
| QC-052 | **ERROR** | Daily test/coverage routine failed — a test broke OR coverage fell below the `pyproject` gate; also WARNs on modules below the per-module floor (learning worklist for "every line tested") | None — reads `reports/test-result.json` from `run_audit_tests.py`; surfaces in audit red-flags | 2026-05-28 |
| QC-053 | **ERROR** | Local repo HEAD is behind `origin/main` — Cloud PC is running stale code, so pushed fixes aren't actually deployed. Catches the failure mode where a PR with production fixes sits unmerged or `git pull` silently failed at wrapper Step 0. | None — operator must merge / re-pull. Audit also raises a dedicated red flag with the explicit `git pull` instruction. | 2026-05-28 (after a 4-commit branch sat unmerged 5 days) |
| QC-054 | **ERROR** | Required runtime modules (`sentry_sdk`, `msal`, `requests`, `jsonschema`, `dateutil`, `tzdata`, `reportlab`, `jinja2`, `pdfplumber` — the shared `RUNTIME_IMPORT_REQUIRED` list, also enforced by QC-060) NOT importable in the wrapper's Python — pipeline observability and/or render silently degrades. | **Self-heal** (2026-06-25) — attempts `pip install --user <missing>` into the running interpreter once and re-imports; ERRORs only on the remainder. `HILMAR_QC_NO_PIP=1` disables the install (tests). | 2026-06-09 (after `sentry_sdk` missing for weeks silently fired HILMAR-DAILY-TRACKER-9 daily) |
| QC-055 | **ERROR** | Sentry cron heartbeat is NOT registering — `run-log.txt` shows `Sentry cron start failed (pipeline continues)`. Sentry's cron monitor then alerts on a missed check-in even though the pipeline ran. | None — root cause is usually QC-054 (missing `sentry_sdk`); if the dep is present, check `secrets/sentry-dsn.txt` + network. | 2026-06-09 |
| QC-056 | WARN | Row has an OL `ol_rate` but no `carrier_quoted` — OL quoted a price with no carrier attribution. Root cause: `parse_rate_table` only read a column literally headed "Carrier", so an OL-relabeled carrier column (e.g. "Ocean Carrier"/"Line"/"SSL") left the rate parsed and carrier blank (Oakland→Manila $797, "nothing should be blank"). | **Self-heal** — re-scan the row's stored text (vessel/transshipment/POL/POD/reason) for a carrier token and backfill; WARN on the remainder. Root fix in `body_parser` (header aliases + data-cell + prose carrier scan). | 2026-06-15 |
| QC-057 | WARN (ERROR ≥3) | A staged `lonny_outbound` rate request (not operational, not out-of-scope) yielded NO parseable destination, so `ingest.build_requests` silently dropped it (`if not destination: skipped_ops += 1; continue`, a counter never logged) — a real RFQ goes MISSING from the report with no alarm. This is the intake-side blind spot behind the 2026-06-24 "Busan Korea from Dalhart" miss that hid for a week (no check reconciled staged RFQs vs built rows). Reconciles via `_intake_reconciliation`, reusing ingest's own `clean_destination`/`is_operational_subject`/`out_of_scope_reason` so the guard can't drift. Every drop also emits a `QC-057-DIAG` line — the PII-scrubbed lane-hinted body lines the parser failed on (run log + audit email) — so the root fix never requires digging out the raw email. Operator-reviewed NON-RFQs (commercial notes with no lane, e.g. the 2026-07 "REEFER NEEDS"/"REEFERS" free-time/transship notes) are recorded in `scripts/intake_acknowledged.json`, DATE-SCOPED (`sent_before`): acknowledged emails are excluded from the drop count but printed as OK lines every run; a future same-subject email past the cutoff still WARNs. | **No auto-heal** (a lane can't be invented) — surfaces the dropped subject(s) + DIAG snippet for the operator. Root fix is a parser extension + re-ingest, or an `intake_acknowledged.json` entry when the DIAG shows it isn't an RFQ. WARN for 1-2; ERROR at ≥3 (systemic parser regression). | 2026-06-24 |
| QC-058 | WARN | The durable Turso stats historian (`historian.py`, finalized rows persisted past the 14-day window) is configured but its newest write is >26h old — the daily `Historian (finalized → Turso)` append is likely failing, so longitudinal stats quietly go stale. Per CLAUDE.md §3 "new API integration → freshness check". | **No auto-heal** — surfaces for the operator (check the historian step, `secrets/historian-turso.txt`, libsql install). WARN only: a write-only analytics sync must never gate the client report, and no live data is lost (tracking-data rebuilds from Outlook each fire). **SKIPS silently when dormant** (no creds), so costs nothing until the DB is provisioned. | 2026-06-24 |
| QC-059 | WARN | **Data-flow integrity** — the cached body parses match the CURRENT `body_parser`. `refresh_stage` parses each email once at fetch time and caches it; `ingest` consumes that cache, so a parser fix only reaches newly-fetched mail and the back-catalog in the window stays stale (the "break in data flow" behind the manual re-ingest). The pipeline now runs `reprocess_bodies` BEFORE ingest to self-heal every fire; this check re-parses the cache and compares. (Michael 2026-06-24: "the system should check for breaks in data flow then backfill as quality control.") | **Self-heal** — when residual drift is found (the pre-ingest backfill didn't run), backfill the stale parses now (lands next fire; re-ingest to surface today). WARN only — a stale cache degrades fields, it doesn't lose live data. **SKIPS** when no cached bodies present. | 2026-06-24 |
| QC-060 | **ERROR** | **Dependency-list consistency** — the list the box installs is PROVABLY the list QC-054 verifies: every `RUNTIME_IMPORT_REQUIRED` module is pinned in `requirements.txt`, and `pyproject [project.dependencies]` == `requirements-tracker.txt`. The 2026-06 jinja2/sentry-sdk gap existed because three dep lists disagreed and none was enforced. Repo-state invariant — fires the same in CI, so a contributor can't add a QC-054 dep without pinning it. | **No auto-heal** (config edit) — `flag_for_operator`. Add the missing pin / reconcile the lists. | 2026-06-25 |
| QC-061 | **ERROR** | **Interpreter parity** — the running Python's major.minor matches the pinned `.python-version` (the single value CI, the box, and the toolchain consume). Catches the 2026-06 silent drift to Python 3.14 (untested; CI is 3.12) at fire time, since QC runs inside the wrapper's chosen interpreter. | **No auto-heal** (can't reinstall Python from here) — `flag_for_operator`: install the pinned version + repoint the wrapper (`deploy/setup_cloudpc.ps1`). | 2026-06-25 |
| QC-062 | **ERROR** (self-heal) | **Layout hygiene** — no stale duplicate `tests/`+`src/` shadow the real `hilmar-daily-routine/` checkout. A pre-checkout-era flat copy under `PROJECT HILMAR/` caused pytest "import file mismatch" collection errors on the 2026-06-25 fire; the xcopy never cleans them. | **Self-heal** — delete the stale shadow dirs (known-safe: the checkout subdir is authoritative). Returns nothing in dev/CI (REPO_ROOT IS the checkout) so it never deletes the real trees. | 2026-06-25 |
| QC-063 | WARN | **Consecutive-failure ratchet** — a best-effort/observer step dead for DAYS, not a blip. The 8 best-effort steps + the test routine exit 0 by design, so per-fire a dead step is invisible and a step failing every fire for a week looks identical to a single blip. `run_pipeline` records each fire's failed steps to `reports/step-history.json`; this escalates any step that failed the last 3 CONSECUTIVE fires to a loud WARN **and pages out-of-band** via `fire_alert` (Teams/GitHub-issue/queue, level=warning) so a week-dead step can't rot in the Outlook-routed audit email alone. | **No auto-heal** (the step's own dep/env/config is the fix) — `flag_for_operator` with the dead step(s) named + out-of-band warn page. SKIPS until ≥3 fires of history exist. | 2026-06-25 |
| QC-064 | WARN | **Garbage in client-visible display fields** — defense-in-depth for the "absolutely wrong info" class: a phone fragment, a raw message-id (`<...@...>` or an Exchange msg-id shard), or the OL responder-mailbox name leaking into `carrier_quoted`/`carrier_won`/`origin`/`destination`/`lane`/`pol`/`pod`/`vessel_voyage`/`transshipment` — fields that ship straight into the client email + PDF. Patterns are conservative (word-boundaried) so a legitimate city/port/carrier is never flagged. | **Self-heal** — null the offending field (a blank cell beats wrong info) and log the field + request id + scrubbed value. WARN-class, NOT an ERROR gate — a false positive must never block the client email, and the heal already removed the bad value. The REAL fix is the upstream parser leak. | 2026-06-27 |
| QC-065 | **ERROR** | **Client-report invariants** — the CLIENT-facing daily email (`gen_client_email.py` → `reports/client-email-body.html`, gated by `config.json client_report.enabled`) may only ever reach the approved pair (to=`lupfold@hilmaringredients.com`, cc=`michael.deitchman@ol-usa.com`). ERRORs when: a staff/`full_list` address or a 2nd recipient appears in `client_report.to` (checked even while disabled — a wrong value goes live the moment the flag flips); the block is ENABLED with to/cc ≠ the approved pair; or the rendered body leaks internal analytics ("Win Rate", "Quoted & Lost"/"Q&L", "Not Quoted", carrier-scoreboard/negotiation intel — raw or `&amp;`-escaped, case-insensitive). | **No auto-heal** (recipient config + renderer content are operator/code decisions) — `flag_for_operator`. Fix `config.json` or `gen_client_email.py`; NEVER widen the approved-recipient tuples or trim the marker list. While disabled the check confirms sample-only mode. | 2026-07-10 |
| QC-066 | **ERROR** | **Impossible request/outcome ordering** — a row whose NEWEST `status_history` event is dated (ET) before its OWN `request_date`, or a report-day request in terminal WIN/LOSS whose history has no same-day-or-later event. This is the recurring-Outlook-thread merge artifact (2026-07-23 report: a new Jul-22 HCMC request surfaced carrying a stale WIN from the old thread, so PENDING OL showed 0 while the request was open nowhere). Legacy rows with empty history exempt. | **Detect-only** (no auto-heal until one live shape is confirmed) — audit names each row so the operator can split it: new request row (PENDING) + the prior outcome under its original ask. | 2026-07-23 |
| QC-067 | **ERROR** | **Open RFQ misfiled as a loss** — an UNQUOTED row carrying `loss_reason=NO_RESPONSE` while Lonny's request is still INSIDE the PENDING-OL response window (48h, 72h for a Friday ask). OL simply has not answered yet, so the row is open business to chase — never a loss. Root cause 2026-07-24: `decide_status` classified every unquoted row LOSS/NO_RESPONSE with ZERO grace, which made `PENDING_OL` structurally unreachable (proved: 0 of 96 input combinations) and stored live RFQs as lost, so "PENDING OL (0)" was permanently empty and nobody chased OL. | **SELF-HEAL** — restore `status=PENDING` (quoted stays False → PENDING_OL), clear `loss_reason`, and append a `status_history` entry so the audit trail survives. `core.decide_status` prevents it at the source; this is the daily proof on live rows + the catch for stale carry-forward, operator corrections, or a future regression. | 2026-07-24 |

## Self-improvement automations added 2026-05-28 PM (per Michael "do all 7-9")

These are not QC checks per se — they're operator-loop closures that build on
existing infrastructure so the daily audit stops being a list of things that
never get followed up on.

| # | What it does | Where | Trigger |
|---|---|---|---|
| Stale Sentry auto-resolve | Any UNMAPPED Sentry issue silent ≥ `STALE_AUTO_RESOLVE_DAYS` (default 7) routes to `resolve_if_stale` with an explanatory comment. Closes the loop that left HILMAR-DAILY-TRACKER-5 (NameError 'os' not defined) unresolved for 11 days after the bug was fixed in code. Mapped issues retain their explicit `ACTIONS` route. | `scripts/qc_actions_from_sentry._action_lookup` | Daily pipeline step "Sentry-driven QC actions" |
| QC-049 booking-team auto-notify | Each unconfirmed WIN (status=WIN, no `mdolx_ref`, request_date ≥ 7d ago) generates ONE Teams/Slack alert per ISO week with the lane, age, and explicit "review and either link the MDOLX booking or demote to Q&L" instruction. De-duped by `_was_alerted`. | `scripts/teams_alert.detect_events` event `qc049_unconfirmed_win` | Daily pipeline step "teams_alerts" (Step 4.5 in wrapper); requires `"qc049_unconfirmed_win"` in `config.json.alerts.events` |

> **Index backlog — RESOLVED 2026-06-09.** QC-042 through QC-051 now have
> full rows in the matrix above. `tests/test_qc_governance.py` (INV-1) now
> fails CI if any check emitted by `qc_selfheal.py` is missing from this
> index, so the backlog can't silently reopen.

## Newest checks (QC-027 through QC-037) — what each was added for

| # | Trigger | Date | Commit |
|---|---|---|---|
| QC-027 | Data audit revealed 70% etd_offered / 69% vessel_voyage / 44% ol_rate missing — drove the multi-pass patch_carriers backfill + PDF parser work that pulled completeness to 93%+ | 2026-05-13 | `6efb1ad` → `df95e1b` |
| QC-028 | New rate-intelligence module needed a freshness gate so the daily audit's negotiation section couldn't silently go stale | 2026-05-13 | `8c81341` |
| QC-029 | New cross-project shared store at `SHARED/client_intelligence/hilmar/` needs an integrity check vs the local source — drift means the rate tracker reads stale data | 2026-05-13 | `8c81341` |
| QC-030 | Transit-time analytics (carrier+lane ETA-ETD median) shipped — needs ETD+ETA pair coverage gate so analytics don't degrade silently | 2026-05-13 | `1c4f38f` |
| QC-031 | Shared schema documentation (`SHARED/client_intelligence/SCHEMA.md`) was added so rate-tracker integrators have a contract — QC ensures it stays present | 2026-05-13 | `1c4f38f` |
| QC-032 | Dual-target offline backup shipped (`backup_offline.py`) — needs freshness check on BOTH targets to confirm defense-in-depth is working | 2026-05-14 | `e2ed228` |
| QC-033 | Hilmar logo + brand asset integration shipped — checks logo file presence + magic-byte sanity so artifacts don't ship with broken `<img>` tags | 2026-05-14 | `862e2ec` → `072e569` |
| QC-034 | Best-practices batch: added `core.validate_data_shape()` schema gate to catch structural drift before downstream renderers crash silently on missing keys | 2026-05-14 | best-practices batch |
| QC-035 | Best-practices batch: stage rotation flag added — without a size gate, `stage_emails.txt` would grow unbounded over months and silently OOM the parser | 2026-05-14 | best-practices batch |
| QC-036 | Best-practices batch: tests/ folder + 50 unit tests added — needed a check that the regression net stays present and grows with future modules | 2026-05-14 | best-practices batch |
| QC-037 | Per Michael "client intelligence is on turso for it" — built sync_to_quote_tracker.py to push 13 Hilmar entities to ol-quote-tracker's `/api/intelligence/sync`; QC-037 ensures the audit log stays fresh and surfaces APP_PASSWORD / endpoint errors | 2026-05-16 | `c8c3d14` |

## Errors this session that drove the new checks

Mapping every issue Michael surfaced 2026-05-07 through 2026-05-13 to the QC
check that prevents recurrence:

| Issue | Date | Root cause | Guards added |
|---|---|---|---|
| "Today" framing in subject | 5/7 | gen_email used datetime.now() not previous biz day | QC-011 |
| "What Happened Today" body | 5/7 | Same — hardcoded label | QC-013 |
| Mon-Sun week labels | 5/7 | _week_bucket returned Sun for end-of-week | QC-012 |
| WIN missing carrier (260587) | 5/7 | patch_carriers manual dict missed it | QC-014a + auto-discovery |
| Q&L parser drift (50% coverage) | 5/7 | parse_rate_table missed table format | QC-014b + body-scan |
| Unmapped destinations | 5/7 | TRADE_REGION_MAP incomplete | QC-015 |
| Backup retention not pruning | 5/7 | _list_snapshots only globbed one format | QC-016 + dual-glob fix |
| Stale .jsonl phantom "192h stale" | 5/7 | gen_improvements_report read wrong file | QC-024 |
| Fake stand_260469 WIN | 5/7 | DRAFT RATED subject seeded standalone | ingest._OPERATIONAL_SUBJECT_HINTS |
| CMA over-attribution (55% → 75% quotes) | 5/8 | Body-scan matched CMA in boilerplate | QC-017 + parse_rate_table primary |
| Day-row math (2 = 0+0+1) | 5/8 | Pending hidden from KPI row | QC-018 + 5th Pending card |
| Multiple daily emails to recipients | 5/8 | Manual fires bypassed wrapper flag | outlook_send.py script-level idempotency |
| Wrapper exit 255 (5/8 + 5/11) | 5/11 | for/f + delayed-expansion IF/ELSE crash | Wrapper simplified (`5f6ac46`) + QC-021 |
| Cloud PC Python path (CPC vs MBD) | 5/8 | Hardcoded user-profile python.exe | Dynamic discovery (`967f649`) |
| MBD-TRAVEL Task Scheduler dupe risk | 5/8 | "Disable" only hit one of two tasks | Script-level idempotency makes harmless |
| Date confusion ("today is Friday") | 5/8, 5/11, 5/13 | Conversation crossed midnight; I anchored stale | _report_date uses system clock; QC-011 catches if I edit wrong |
| Multi-line pipe table not parsed | 5/13 | OL changed template; _find_table_rows required single line | _collapse_multiline_pipe_table |
| PENDING rows lack carrier | 5/13 | patch_carriers only handled LOSS-quoted | Extended to PENDING + QC-019 |
| 5 status-changes empty carrier (incl. 4:47 PM Algeciras) | 5/13 | New OL template + LOSS-only patch path | QC-019 ERROR-gate |
| 14-day-old NQ rows crowding the list | 5/13 | _not_quoted_rows had no cutoff | _not_quoted_aggregate + QC-020a/b |

## Self-heal actions that fire automatically

Beyond detection, these actions auto-remediate when safe:

| Trigger | Action | Where |
|---|---|---|
| Container count missing from subject | Recover from body via "I need N" pattern | qc_selfheal Phase 3 |
| TEU recomputation drift | Recompute from container_count × size | qc_selfheal Phase 3 |
| Duplicate request_id with split sources | Merge keeping richest, drop thin copy | qc_selfheal Phase 4 |
| Summary / lane / carrier aggregates stale | Rebuild from raw requests | qc_selfheal Phase 5 |
| Stage > 36h old AND it's weekday morning | (Alert only — manual investigation) | QC-008 |
| WIN missing carrier_won | Try MDOLX manual dict + auto-discovery from sibling stage subjects | patch_carriers |
| Q&L missing carrier_quoted | parse_rate_table (column-aware) + body-text scan with boilerplate stripping | patch_carriers |
| PENDING missing carrier_quoted | Same body-scan as Q&L | patch_carriers (5/13 extension) |
| Backup snapshots > retention | Auto-prune oldest (both naming formats) | backup.py |
| Today's email already sent | Refuse send + show flag content | outlook_send.py daily |
| Pipeline failed before send | Wrapper exits with pipeline rc; qc_alert emails Michael | wrapper Step 4 |

## How a new failure mode gets added

1. Surface the failure (Michael's chat, log audit, daily improvements report)
2. Diagnose root cause
3. Fix the underlying code
4. **In the SAME commit**, add a QC check that would have caught the failure
5. Update this index with: check number, severity, what it catches, commit
6. Verify check passes on current data (else self-heal or threshold tuning needed)

Standing rule (cross-project): "every new code pattern ships with QC +
self-heal in the same commit." This file is the audit trail.
