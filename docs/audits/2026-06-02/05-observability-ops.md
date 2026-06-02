# Audit 05 — Observability, Reliability, Operational Gaps

**Date:** 2026-06-02
**Scope:** Read-only audit. Focus: silent failure paths, QC coverage,
Sentry instrumentation, self-heal recurrence, classifier observability
fallout, backup integrity, recovery playbook, QC-021 mystery, the
half-deployed Cloud-PC runner, and "observability of observability."

PRs #14-21 are out of scope as remediations.

---

## 1. Wrapper-aborting paths still left after PR #16

PR #16 partitioned `run_pipeline.py` failures but does NOT cover
anything in `deploy/run_daily_laptop.cmd`. Several silent-bail paths
remain in the wrapper.

### Finding 1.1 — Step 4 (`qc_alert_if_needed.py`) import-time crash
- **Severity:** High
- **Files:** `deploy/run_daily_laptop.cmd:137`, `deploy/qc_alert_if_needed.py:18, 48-69`
- **What's wrong:** Invoked unguarded. The `import outlook_send` at line
  18 can fail if wrapper Step 0 just copied a syntactically broken
  scripts/ from the git checkout. That happens BEFORE `main()` is
  reached and produces no email, no Sentry event. `qc_alert_if_needed`
  itself never inits Sentry.
- **What to do:** Wrap the call in try/except with Sentry breadcrumb;
  guard the import so syntax bugs don't drop the downstream audit-email
  step.
- **Effort:** ~10 lines.

### Finding 1.2 — Step 5 audit-email send silently lost
- **Severity:** High
- **Files:** `deploy/run_daily_laptop.cmd:163-167`
- **What's wrong:** `gen_improvements_report.py` then
  `outlook_send.py daily --to ...idealx.us` is the ONLY mechanism that
  surfaces today's audit findings. Wrapper prints exit codes (line 167)
  but never alerts on non-zero. If `gen_improvements_report.py` raises,
  the outlook_send call still fires with no body file and dies. No
  Sentry, no alert, no audit landed.
- **What to do:** Guard each call; on failure, send a minimal fallback
  body ("audit-report generation failed — see run-log") so silence
  never reads as "everything's fine."
- **Effort:** ~15 lines.

### Finding 1.3 — Heartbeat dispatch depends on `gh` auth state
- **Severity:** Medium
- **Files:** `deploy/run_daily_laptop.cmd:186-207`
- **What's wrong:** `gh workflow run heartbeat.yml` runs only if `where
  gh` succeeds; auth state is never verified. Expired PAT → command
  exits non-zero, wrapper still `exit /b 0`. Liveness monitor at
  11:30 ET sees no fresh heartbeat and files a `cloud-pc-down` issue —
  even though the pipeline ran fine. False-positive pattern.
- **What to do:** Add `gh auth status` probe; on failure, email Michael
  rather than trigger tomorrow's false alarm.
- **Effort:** ~5 lines.

### Finding 1.4 — Steps 4.5 / 4.7 / 4.9 / 5 unguarded, no Sentry init
- **Severity:** Medium
- **Files:** `deploy/run_daily_laptop.cmd:142, 146, 159, 163`
- **What's wrong:** `teams_alert.py`, `gen_weekly_summary.py`,
  `backup_offline.py`, `gen_improvements_report.py` — none check
  `%ERRORLEVEL%`, none init `sentry_setup`. A Python crash exits to log
  with no telemetry.
- **What to do:** Add `sentry_setup.init` at top of each.
- **Effort:** Small, 4 scripts × ~3 lines.

### Finding 1.5 — Wrapper Step 0 `xcopy` failure mode
- **Severity:** Medium
- **Files:** `deploy/run_daily_laptop.cmd:95-98`
- **What's wrong:** `xcopy /Y /Q "...scripts\*.py" "%ROOT%\scripts\"` is
  unguarded. OneDrive sharing-violation mid-sync → silent stale-script
  fire. QC-053 catches HEAD-vs-origin drift on the git checkout but
  NOT scripts/ ↔ checkout drift (QC-026 reads wrong base — see
  `qc_selfheal.py:2386-2417`).
- **What to do:** Check xcopy exit code, log + Sentry on non-zero.
- **Effort:** ~5 lines.

---

## 2. QC-check coverage gaps

### Finding 2.1 — No QC for liveness/heartbeat workflow health
- **Severity:** High
- **Files:** `.github/workflows/heartbeat.yml`, `.github/workflows/liveness.yml`
- **What's wrong:** Liveness guards the wrapper; nothing guards
  liveness. Rate-limit, broken `gh run list` parsing, or free-tier
  scheduled-job throttling can disable it silently.
- **What to do:** QC-054: query `gh run list --workflow=liveness.yml
  --limit 3`; assert ≥1 success in 26h and not 3 consecutive errors.
- **Effort:** ~30 lines.

### Finding 2.2 — No Sentry-asleep detection
- **Severity:** High
- **Files:** `scripts/sentry_setup.py:144-156, 196-256`
- **What's wrong:** If `secrets/sentry-dsn.txt` goes missing/corrupted,
  `init()` silently returns False; the pipeline runs blind. QC-043 is a
  CONSUMER of Sentry's REST API — it doesn't probe local init.
- **What to do:** QC-055: surface the boolean from `_sentry.init()` in
  qc-result.json; ERROR if False on production host.
- **Effort:** ~15 lines.

### Finding 2.3 — No QC for `email-subject.txt` freshness
- **Severity:** Medium
- **Files:** `scripts/qc_selfheal.py:921-974` (QC-011)
- **What's wrong:** QC-011 asserts subject DATE matches previous biz
  day. It does NOT check file mtime. If `gen_email.py` is skipped but
  yesterday's correct-date subject lingers, QC-011 passes — and on
  Monday morning, yesterday's Friday-dated file is still "the previous
  business day." Outlook idempotency catches same-day dupes only.
- **What to do:** QC-011b — assert mtime within current fire window.
- **Effort:** ~10 lines.

### Finding 2.4 — Run-log truncation breaks QC-021
- **Severity:** Low (root-cause of §8.1)
- **Files:** `deploy/run_daily_laptop.cmd:23, 31`, `qc_selfheal.py:1134`
- **What's wrong:** `run-log.txt` is append-only with manual purge.
  QC-021 reads `[-40000:]` — a 40 KB tail. A single fire (xcopy verbose
  + git pull + many QC fixes) can exceed 40 KB on its own; today's
  step markers fall off the front.
- **What to do:** Rotate run-log (last ~200 fires) or bump QC-021 tail
  to 250 KB.
- **Effort:** ~15 lines.

### Finding 2.5 — No QC for `secrets/github-pat.txt` expiry
- **Severity:** High
- **Files:** `deploy/run_daily_laptop.cmd:182` (mentions PAT)
- **What's wrong:** QC-023 watches MSAL token expiry. Nothing watches
  the GitHub PAT for heartbeat dispatch. PAT expiry → silent heartbeat
  failure → false liveness alert.
- **What to do:** QC-056 mirroring QC-023's age check (or probe
  `gh auth status`).
- **Effort:** ~15 lines.

---

## 3. Sentry instrumentation gaps

Only **7** scripts init Sentry: `run_pipeline`, `qc_selfheal`,
`patch_carriers`, `outlook_send`, `sync_to_quote_tracker`, `ingest`,
`qc_actions_from_sentry`. Many pipeline steps run blind.

### Finding 3.1 — Twelve subprocesses never init Sentry
- **Severity:** High
- **Files:** `scripts/backup.py`, `drift_check.py`, `gen_dashboard.py`,
  `gen_pdf.py`, `gen_email.py`, `gen_carrier_scorecard_pdf.py`,
  `share_intel.py`, `gen_rate_intelligence.py`, `backup_offline.py`,
  `gen_improvements_report.py`, `teams_alert.py`, `gen_weekly_summary.py`
- **What's wrong:** `run_pipeline.py:204` invokes each via
  `subprocess.run`. Sentry transactions live in the PARENT
  (`run_pipeline.py:186, 321`) — children have no SDK init, so
  exceptions are invisible. Parent sees only returncode. A
  `gen_email.py` AttributeError that ships a broken HTML body lands in
  run-log only.
- **What to do:** Add `import sentry_setup; sentry_setup.init(...)` at
  top of each. The `_PRE_PATCH_ENV` env-prop already proves the pattern
  (`run_pipeline.py:41`).
- **Effort:** ~36 lines total. Biggest observability win in the audit.

### Finding 3.2 — Subprocess crash → Sentry has no stacktrace
- **Severity:** Medium
- **Files:** `scripts/run_pipeline.py:204-238`
- **What's wrong:** On non-zero subprocess exit, `run_step` calls
  `capture_qc_error("pipeline.step_failure", f"{name} {label}")` (line
  232) — a STRING, not the exception. The child's traceback is lost.
  `gen_pdf.py: KeyError` and `gen_pdf.py: timeout 124` share a
  fingerprint.
- **What to do:** Capture last 100 stderr lines as Sentry `extra`;
  fingerprint by tail signature.
- **Effort:** ~15 lines.

### Finding 3.3 — `teams_alert.py` has no Sentry — but it's the alert primitive
- **Severity:** Medium
- **Files:** `scripts/teams_alert.py` (wrapper line 142)
- **What's wrong:** Webhook misconfig or JSON serialization bug
  produces no Sentry event. The alerting tool has no alerts.
- **What to do:** Add `sentry_setup.init(component="teams_alert")`.
- **Effort:** 3 lines.

---

## 4. Self-heal recurrence — no tracking

`gen_improvements_report.py:746-758` already says in prose: "if the SAME
fix keeps applying day after day that's a root-cause issue at intake."
But the system doesn't actually track which fixes recur.

### Finding 4.1 — `qc.fixes` metric is single-dimensional
- **Severity:** High
- **Files:** `scripts/qc_selfheal.py:121-128`
- **What's wrong:** Tagged `phase=pre|post-patch` only. No `category`
  tag (e.g. `teu_defaulted`, `carrier_copied_from_quoted`). Sentry can
  show "30 fixes today" but can't answer "are these the same as
  yesterday's?" The audit's `teu_won defaulted to teu_requested` example
  is exactly the kind of recurring fix that signals an intake bug.
- **What to do:** (a) Add `category` regex extraction inside `log.fix`
  — the ~25 fix-messages start with predictable phrases. (b) Persist
  `reports/qc-fix-log.jsonl` (append-only) for 7-day rolling tally.
  (c) Audit-email widget surfaces the top-3 recurring categories.
- **Effort:** Medium — ~60 lines.

### Finding 4.2 — Right place: `qc_selfheal.py:121` `log.fix()` chokepoint
- The `log.fix(msg)` method is the only entry point — categorize there.
  Persist alongside qc-result.json. Trim to 30 days for size.

---

## 5. PRICE classifier change (PR #21) — observability fallout

`scripts/core.py:79-81` keeps PRICE and adds UNDIFFERENTIATED. Searches:

### Finding 5.1 — `_RATE_DRIVEN = {"PRICE"}` is the right abstraction, but downstream consumers reach raw
- **Severity:** Medium
- **Files:** `scripts/core.py:1076`, `src/hilmar/core.py:1034`,
  `scripts/gen_email.py:1388-1398`,
  `scripts/gen_rate_intelligence.py:160`
- **What's wrong:** `_RATE_DRIVEN` intentionally narrow per PR-21
  comment. `gen_email._REASON_META` has both PRICE and UNDIFFERENTIATED
  (safe). `gen_rate_intelligence` filters only NO_RESPONSE (safe).
  BUT: no Sentry events tag `loss_reason`, so any future Sentry
  dashboard widget filtering `loss_reason==PRICE` would silently miss
  UNDIFFERENTIATED. The risk is forward-looking, not present-broken.
- **What to do:** Unit test asserting every `core.LOSS_REASONS` enum
  appears in `gen_email._REASON_META`. Document Sentry-widget convention.
- **Effort:** ~20 lines.

### Finding 5.2 — Historical rows still tagged PRICE under the old catch-all
- **Severity:** Medium
- **Files:** `tracking-data-v2.json`, `scripts/core.py:705-721`
- **What's wrong:** Pre-2026-06-02 rows were classified under the old
  catch-all PRICE. Any 60-day rolling PRICE-share chart will show a
  step-change on 2026-06-02 that reads like market signal but is
  classifier-change.
- **What to do:** Either backfill `decide_status` over historical rows
  (with a `_reclassified=true` marker) or annotate the audit-email
  loss-mix chart with a "Classifier change 2026-06-02" caption.
- **Effort:** Backfill ~50 lines + careful re-run.

---

## 6. Backup integrity — freshness without verification

### Finding 6.1 — No tar.gz integrity check
- **Severity:** High
- **Files:** `scripts/backup_offline.py:89-100, 175-195`,
  `qc_selfheal.py:2179-2224` (QC-032)
- **What's wrong:** `_make_archive` writes tar.gz; nothing verifies the
  tarball is well-formed. Disk-full or OneDrive collision → damaged
  archive with fresh mtime → QC-032 PASSES on a broken backup.
  Recovery time at "tracking-data-v2.json corrupts" is until you
  discover the tarball is also broken.
- **What to do:** After `_make_archive`, open + `tar.getnames()`,
  verify `tracking-data-v2.json` present. ERROR on failure. QC-032b
  randomly samples one of the last 7 backups for `tar.list()` health
  check.
- **Effort:** ~20 lines.

### Finding 6.2 — No size/row-count regression alarm
- **Severity:** Medium
- **Files:** `scripts/backup_offline.py`
- **What's wrong:** If today's `tracking-data-v2.json` corrupts to
  empty `{"requests": []}`, today's backup faithfully archives the
  empty state. Tomorrow has no good backup of yesterday's data.
- **What to do:** Pre-backup invariant: only archive if request count
  is ±15% of 7-day average; else ERROR + alert.
- **Effort:** ~25 lines.

---

## 7. Recovery-playbook gaps in `RUNBOOK.md`

### Finding 7.1 — Cloud PC OneDrive sync stopped
- **Severity:** High
- **Files:** `RUNBOOK.md` (missing section)
- **What's wrong:** RUNBOOK:189 mentions "OneDrive may be paused"
  inside the backup_offline failure mode but has no dedicated entry.
  Symptoms: stale scripts/ (QC-026 fires), MSAL token cache stale,
  secrets/sentry-dsn.txt unreadable on fresh reboot, dual offline
  backup fails. Recovery: `OneDrive /resume`, possibly re-link folder.
- **What to do:** Add dedicated section: detection, recovery commands,
  expected QC fallout.
- **Effort:** ~30 lines doc.

### Finding 7.2 — Anthropic API down
- **Severity:** Medium
- **Files:** `scripts/pdf_llm_rescue.py:154-184`,
  `scripts/qc_actions_from_sentry.py:423-436`,
  `src/hilmar/orchestrator.py`, `src/hilmar/model_router.py`
- **What's wrong:** No-ops on missing key (line 156). Does NOT handle
  5xx, rate-limit, hung request — those raise from
  `client.messages.create`. `claude_diagnose` checks key existence
  only. No RUNBOOK entry.
- **What to do:** Wrap each Anthropic call in `try/except
  (RateLimitError, APIStatusError, httpx.TimeoutException,
  APIConnectionError)`. Add RUNBOOK entry.
- **Effort:** ~20 lines code + ~20 lines doc.

### Finding 7.3 — Sentry quota exhausted
- **Severity:** Medium
- **What's wrong:** On quota exhaustion, `sentry_sdk.capture_*` returns
  silent. Cron check-ins fail to register → liveness/cron monitor fires
  "missed check-in" though pipeline ran. False positive again.
- **What to do:** Document Sentry quota dashboard, configure Sentry
  native usage-alert, treat GitHub-Actions heartbeat as PRIMARY
  liveness and Sentry as secondary.
- **Effort:** Doc-only ~20 lines.

### Finding 7.4 — Anthropic key file missing — silent degradation
- **Severity:** Low
- **Files:** `scripts/pdf_llm_rescue.py:62-66`
- The key file is gitignored. Fresh Cloud PC → missing → PDF rescue
  silently no-ops → QC-027b permanently warns. Add a one-time existence
  probe at qc_selfheal startup.

---

## 8. QC-021 false-positive mystery

Today's audit fired QC-021 WARN even though the audit email landed.
Reading `qc_selfheal.py:1130-1184` reveals three combining bugs.

### Finding 8.1 — QC-021's run-log search is fragile
- **Severity:** High
- **Files:** `scripts/qc_selfheal.py:1132-1141`
- **What's wrong:** Three independent bugs combine:
  1. `_log_path.read_text()[-40000:]` (line 1134) — 40 KB tail can
     truncate today's `--- run_pipeline ---` marker off the front on a
     verbose fire (xcopy + git pull + many QC fixes).
  2. `_dt.now().strftime("%m/%d/%Y")` (line 1135) — Python-process
     local time. Cloud PC ET vs. UTC-running probe vs. the `%DATE%
     %TIME%` from cmd.exe → string mismatch.
  3. `max(_tail.find(_today_us), _tail.find(_today_iso))` picks the
     LATEST match. If yesterday's marker appears later in the buffer
     than today's (manual fire mid-day yesterday), wrong anchor →
     "no step markers" for today.
- **What to do:** Replace `find()` anchors with proper block parsing
  (split on `========` dividers, identify today's block by header
  line). Bump tail buffer to 250 KB. Best fix: have the WRAPPER write
  `reports/last-fire-summary.json` (status, last_step, ended_at) so
  QC-021 reads structured data, not scraped text.
- **Effort:** ~40 lines.

### Finding 8.2 — QC-021 should cross-check `sent-YYYY-MM-DD.flag`
- **Severity:** Medium
- **What's wrong:** Today's flag existence proves email shipped. If
  QC-021 saw `reports/sent-YYYY-MM-DD.flag` with fresh mtime, it could
  downgrade the WARN to "stale-log only."
- **What to do:** Add the cross-check before WARNing.
- **Effort:** ~10 lines.

---

## 9. Cloud-PC runner migration — half-deployed risks

`.github/workflows/daily-fire.yml` is on `main` with `on.schedule: cron:
'0 14 * * 1-5'`. Comments at lines 19-21 say the runner isn't installed.

### Finding 9.1 — `daily-fire.yml` schedule is live without a runner
- **Severity:** Critical
- **Files:** `.github/workflows/daily-fire.yml:30-33`
- **What's wrong:** Until a self-hosted runner is registered, the
  scheduled trigger queues a job that finds no runner — either pending
  indefinitely or failing with alerting on a workflow that was
  deliberately not wired. If a runner gets test-registered, the Cloud
  PC fires TWICE (Task Scheduler 10 AM ET + daily-fire.yml). Outlook
  idempotency stops dupe emails but the full pipeline runs 2×: 2× Sentry
  events, 2× Anthropic calls, possible rate-limit trips.
- **What to do:** Disable `on.schedule` until the runner is provisioned
  — delete the block or gate with workflow_dispatch-only.
- **Effort:** 3 lines.

### Finding 9.2 — Setup docs split across branches
- **Severity:** Medium
- **Files:** `docs/CLOUD-PC-RUNNER-SETUP.md`, `docs/MOVE-OFF-CLOUDPC.md`
- **What's wrong:** Docs reference unmerged branch state; a future
  Claude session may follow half-correct instructions.
- **What to do:** Add "STATUS: NOT YET DEPLOYED" banner to both.

---

## 10. Observability of observability

### Finding 10.1 — Sentry auth token has no freshness QC
- **Severity:** Medium
- **Files:** `scripts/sentry_setup.py:144-156`,
  `secrets/sentry-auth-token.txt`
- **What's wrong:** DSN doesn't expire, but the auth token used by
  `sentry_api.py`, `qc_actions_from_sentry.py`, `sentry_seer.py` DOES.
  No QC analogous to QC-023.
- **What to do:** QC-057 — Sentry auth token freshness probe (HEAD
  request to a cheap endpoint). WARN 60d, ERROR 80d.
- **Effort:** ~20 lines.

### Finding 10.2 — `qc_alert_if_needed.py` hardcoded ol-usa.com recipient
- **Severity:** Low
- **Files:** `deploy/qc_alert_if_needed.py:21`
- **What's wrong:** Sends to `michael.deitchman@ol-usa.com` — project
  convention (CLAUDE.md, RUNBOOK, audit email) is `@idealx.us`. If the
  @ol-usa account is cut, QC alerts vanish.
- **What to do:** Read from `config.json` `qc_alert.recipient` with
  fallback.
- **Effort:** ~5 lines.

### Finding 10.3 — GitHub Actions outages have no out-of-band alarm
- **Severity:** Low
- **What's wrong:** The whole liveness+heartbeat scheme assumes GHA is
  up. GHA has multi-hour incidents. Subscribe Michael to
  https://www.githubstatus.com/atom.

### Finding 10.4 — Sentry Cron & GHA liveness don't cross-check
- **Severity:** Medium
- **Files:** `scripts/sentry_setup.py:334-393`,
  `.github/workflows/liveness.yml`
- **What's wrong:** Both watch "did 10 AM fire happen" on different
  signals (Sentry: pipeline check-in; GHA: heartbeat workflow). If they
  disagree, nobody flags the divergence.
- **What to do:** End-of-pipeline log to Sentry "GHA heartbeat
  status=success/failed" for diffing in a dashboard widget.
- **Effort:** ~10 lines.

---

## Top 5 reliability priorities

1. **Wrap pipeline subprocesses with Sentry init** (§3.1). Twelve
   scripts currently run blind. Three lines each → 5× expansion of
   error surface. Single largest observability gap; makes every other
   Sentry-based finding more valuable.

2. **Fix QC-021's false-positive logic** (§8.1) — replace run-log
   scraping with a wrapper-written `reports/last-fire-summary.json` and
   cross-confirm against `sent-YYYY-MM-DD.flag`. Recurring false
   positives will train Michael to ignore the check.

3. **Disable scheduled trigger in `daily-fire.yml`** (§9.1). Live on
   main; one runner registration away from double-firing the pipeline.
   Three-line PR; large blast-radius prevented.

4. **Verify backup tar.gz integrity + row-count invariant** (§6.1,
   §6.2). QC-032 today checks freshness, not validity. A corrupt
   backup is worse than no backup because nobody investigates.

5. **Categorize self-heal fixes + persist 7-day history** (§4.1). The
   audit email already prompts Michael for this. Without categories,
   "30 fixes today" doesn't actionably surface intake bugs; with them,
   the audit can name the recurring fix and point at the root cause.

Honorable mentions: run-log rotation (§2.4), Sentry-asleep detection
(§2.2), and a PAT-freshness QC (§2.5) all close real silent-failure
windows for low effort.
