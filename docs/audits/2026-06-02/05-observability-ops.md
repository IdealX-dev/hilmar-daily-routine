# Audit 05 — Observability, Reliability, Operational Gaps

**Date:** 2026-06-02
**Scope:** Read-only audit of the Hilmar Daily Tracker. Focus: silent
failure paths, QC-coverage gaps, Sentry instrumentation, self-heal
recurrence, classifier observability fallout, backup integrity, recovery
playbook, the QC-021 mystery, the half-deployed Cloud-PC runner, and
"observability of observability."

PRs #14-21 are out of scope as remediations (this audit avoids
re-recommending them) — but their seams are where most findings sit.

---

## 1. Wrapper-aborting paths still left after PR #16 (`BEST_EFFORT_STEPS`)

PR #16 partitioned `run_pipeline.py` failures; it does NOT cover anything
happening in `deploy/run_daily_laptop.cmd` (the Windows wrapper that
shells out to Python). The wrapper has several remaining bail-out paths
where a single failure suppresses downstream observability:

### Finding 1.1 — Step 4 (`qc_alert_if_needed.py`) crash kills audit email
- **Severity:** High
- **Files:** `deploy/run_daily_laptop.cmd:137`, `deploy/qc_alert_if_needed.py:48-69`
- **What's wrong:** `qc_alert_if_needed.py` is invoked unguarded. If
  `reports/qc-result.json` is missing it returns 1 (line 51). If
  `outlook_send.send_mail` raises (MSAL token wedged, Graph 5xx), the
  unhandled exception propagates and the wrapper's `%ERRORLEVEL%` is
  non-zero. The wrapper does NOT check that errorlevel — it continues —
  but the bigger silent risk is that the `outlook_send` import at line
  18 can crash (e.g. import-time module error on a freshly-pulled
  scripts/ that has a syntax bug, since wrapper Step 0 just blindly
  copied it). That import error happens BEFORE main() ever sees the
  flag and produces no email.
- **What to do:** Wrap the qc_alert invocation with a Sentry breadcrumb +
  guard the import so an import failure logs to run-log and Sentry but
  doesn't drop the audit-email step that follows.
- **Effort:** ~10 lines.

### Finding 1.2 — Step 5 audit-email send failure silent
- **Severity:** High
- **Files:** `deploy/run_daily_laptop.cmd:163-167`
- **What's wrong:** `gen_improvements_report.py` plus the subsequent
  `outlook_send.py daily ... --to michael.deitchman@idealx.us` is the
  ONLY mechanism that surfaces today's audit findings. The wrapper
  prints its exit code (line 167) but never alerts on a non-zero. If
  `gen_improvements_report.py` raises (e.g. dashboard HTML missing
  because `gen_dashboard` failed under the new best-effort classification
  — wait, no, dashboard is client-blocking, but `improvements-report.html`
  refers to several intermediate JSON files that COULD be missing), the
  outlook_send call still runs but with no body file — and dies. No
  Sentry event, no alert, no audit landed today. This is exactly the
  failure mode that makes "the audit email never told us X" believable.
- **What to do:** Guard each call individually; on failure, write a
  minimal fallback body ("audit-report generation failed — see
  run-log") and send THAT so silence never means "everything's fine."
- **Effort:** Small wrapper change, ~15 lines.

### Finding 1.3 — Heartbeat dispatch depends on `gh` CLI auth state
- **Severity:** Medium
- **Files:** `deploy/run_daily_laptop.cmd:186-207`
- **What's wrong:** `gh workflow run heartbeat.yml` (line 200) fires only
  if `where gh` succeeds; auth state is never verified. If `gh auth
  status` would say "not authenticated" or the token expired, the
  command exits non-zero but the wrapper still `exit /b 0`. Now the
  liveness monitor (`.github/workflows/liveness.yml`) at 11:30 ET sees
  no fresh heartbeat and opens a `cloud-pc-down` issue — even though
  the pipeline ran fine. This is exactly the false-positive pattern the
  liveness monitor is meant to avoid.
- **What to do:** Add a `gh auth status` probe; if it fails, alert
  Michael by email rather than triggering tomorrow's false-alarm
  liveness issue. Also log the gh exit code (currently swallowed by
  delayed expansion).
- **Effort:** ~5 lines.

### Finding 1.4 — Step 4.5 (`teams_alert.py`), 4.7 (`gen_weekly_summary.py`), 4.9 (`backup_offline.py`), 5 (`gen_improvements_report.py`) all unguarded
- **Severity:** Medium
- **Files:** `deploy/run_daily_laptop.cmd:142, 146, 159, 163`
- **What's wrong:** None of these check `%ERRORLEVEL%`. Today, that's
  benign — the wrapper continues — but it's also fragile: a Python
  interpreter-level crash (segfault, OOM, encoding bug) in any of these
  may exit with a non-trivial code that COULD trip future logic
  added between them. More importantly, none of their failures get to
  Sentry (these scripts do not init `sentry_setup`).
- **What to do:** Either wire `sentry_setup.init` at the top of each
  (current pattern in `outlook_send.py:343`) or have the wrapper push a
  best-effort heartbeat tagged with each step's exit code.
- **Effort:** Small, 4 scripts × ~3 lines.

### Finding 1.5 — Wrapper Step 0 `xcopy` failure mode
- **Severity:** Medium
- **Files:** `deploy/run_daily_laptop.cmd:95-98`
- **What's wrong:** `xcopy /Y /Q "...scripts\*.py" "%ROOT%\scripts\"` is
  unguarded and runs without `>nul 2>&1` reliability. If the production
  `scripts/` folder is mid-OneDrive-sync, xcopy can fail with sharing
  violation. Result: the OneDrive scripts/ stays stale, but the wrapper
  proceeds. QC-053 catches HEAD-vs-origin drift on the git checkout but
  NOT scripts/ ↔ git-checkout drift — that's QC-026, which reads the
  wrong base (live scripts/ vs. repo scripts/), see comment at
  `scripts/qc_selfheal.py:2386-2417`.
- **What to do:** Check xcopy exit code; if non-zero, log + Sentry +
  push a notification (xcopy partial copy is silent corruption of the
  next fire).
- **Effort:** ~5 lines.

---

## 2. QC-check coverage gaps

The QC matrix (`reports/QC-INDEX.md`) is dense, but production behaviors
without QC protection remain:

### Finding 2.1 — No QC for liveness/heartbeat workflow health
- **Severity:** High
- **Files:** `.github/workflows/heartbeat.yml`, `.github/workflows/liveness.yml`
- **What's wrong:** The liveness workflow guards the wrapper; nothing
  guards the liveness workflow. If `liveness.yml` itself starts failing
  (rate limit, broken `gh run list` JSON parsing, scheduled-job
  throttling on free GHA tier), nobody is notified. The watcher is
  unwatched.
- **What to do:** Add QC-054 that, during qc_selfheal, queries
  `gh run list --workflow=liveness.yml --limit 3` and asserts:
  (a) at least one successful run in the last 26h, (b) the last 3 didn't
  all error. Pair with a "self-loop" Sentry probe ping that liveness
  itself emits at successful end.
- **Effort:** ~30 lines.

### Finding 2.2 — No Sentry-asleep detection
- **Severity:** High
- **Files:** `scripts/sentry_setup.py:_load_dsn`, `qc_selfheal.py`
- **What's wrong:** If the Sentry DSN file goes missing/corrupted,
  `sentry_setup.init()` silently returns `False` and the entire
  pipeline keeps running blind. There's no QC that asserts "Sentry was
  initialized today" or "events fired in the last 24h." Compare
  QC-043 (Sentry SELF-IMPROVEMENT LOOP) — that's a consumer of Sentry's
  REST API; it doesn't probe Sentry's local init success.
- **What to do:** QC-055: read the boolean returned by
  `_sentry.init()`, surface in qc-result.json; ERROR if False on a
  production host.
- **Effort:** ~15 lines.

### Finding 2.3 — No QC for `email-subject.txt` freshness or content
- **Severity:** Medium
- **Files:** `scripts/qc_selfheal.py:921-974` (QC-011)
- **What's wrong:** QC-011 (PR #20 improved discrimination from QC-021)
  asserts the SUBJECT DATE matches previous biz day. It does NOT check
  file freshness (`email-subject.txt mtime within last 60min`). So if
  `gen_email.py` was skipped or crashed but the subject file from
  yesterday remains, QC-011 happily verifies yesterday's correct date,
  and the wrapper sends yesterday's email. (Mitigated by outlook_send
  idempotency flag — but only on a same-day repeat, NOT a Monday sending
  Friday's still-correct-date subject.)
- **What to do:** QC-011b — assert email-subject.txt mtime is within
  the current fire window.
- **Effort:** ~10 lines.

### Finding 2.4 — No QC for run-log truncation/rotation
- **Severity:** Low
- **Files:** `deploy/run_daily_laptop.cmd:23, 31` (LOG path), `qc_selfheal.py:1134`
- **What's wrong:** `run-log.txt` is append-only with manual purge.
  QC-021's logic reads `_log_path.read_text()[-40000:]` (line 1134) — a
  40 KB tail. After ~6 months of accumulation, a single wrapper fire
  may exceed 40 KB on its own (the run-log captures every step + every
  xcopy line + git pull verbose). When that happens, QC-021's "step
  marker" search runs against truncated bytes and produces the exact
  "no step markers found" warning we saw in today's audit (see §8).
- **What to do:** Either rotate `run-log.txt` (cap at last 200 fires) or
  extend the QC-021 tail buffer. Likely both.
- **Effort:** ~15 lines.

### Finding 2.5 — No QC for `secrets/github-pat.txt` expiry
- **Severity:** High
- **Files:** `deploy/run_daily_laptop.cmd:182` (mentions PAT)
- **What's wrong:** QC-023 watches MSAL token expiry (60d/80d). Nothing
  watches the GitHub PAT used by `gh workflow run heartbeat.yml`. PAT
  expiry → silent heartbeat failure → false liveness-monitor alert.
  Mirror QC-023's pattern for github-pat.txt mtime (or, better, probe
  `gh auth status` and parse the expiry).
- **What to do:** QC-056 — GitHub PAT freshness ≥60d WARN, ≥80d ERROR.
- **Effort:** ~15 lines.

---

## 3. Sentry instrumentation gaps

`sentry_setup.init()` is called from only **5** scripts:
`run_pipeline`, `qc_selfheal`, `patch_carriers`, `outlook_send`,
`sync_to_quote_tracker`, `ingest`, `qc_actions_from_sentry`. Several
critical pipeline steps run without ever initializing Sentry:

### Finding 3.1 — `backup.py`, `drift_check.py`, `gen_dashboard.py`, `gen_pdf.py`, `gen_email.py`, `gen_carrier_scorecard_pdf.py`, `share_intel.py`, `gen_rate_intelligence.py`, `backup_offline.py`, `gen_improvements_report.py`, `teams_alert.py`, `gen_weekly_summary.py` all lack Sentry init
- **Severity:** High
- **Files:** All of the above
- **What's wrong:** `run_pipeline.py:204` calls each via `subprocess.run`
  in a child process. Sentry transactions are started in the PARENT
  (`run_pipeline.py:186` `start_span`, `:321` `start_transaction`), but
  the child processes have NO Sentry SDK initialization — so any
  exception within them is invisible to Sentry. The parent only knows
  the subprocess returncode. A `gen_email.py` AttributeError that
  produces a broken HTML body would land in run-log, never in Sentry,
  and not be paged. The orchestrator span shows duration only, not the
  child's stacktrace.
- **What to do:** Either (a) add a 3-line `import sentry_setup;
  sentry_setup.init(component=...)` block at the top of each script —
  cheapest, fixes everything — or (b) propagate SENTRY_DSN env to the
  subprocess and have a tiny `sentry_helper.maybe_init()` they all
  import. Today, `_PRE_PATCH_ENV` already proves env-prop works.
- **Effort:** ~3 lines × 12 scripts = ~36 lines. Big observability win.

### Finding 3.2 — No subprocess crash → Sentry exception propagation
- **Severity:** Medium
- **Files:** `scripts/run_pipeline.py:204-238`
- **What's wrong:** When a subprocess exits non-zero, `run_step` calls
  `capture_qc_error("pipeline.step_failure", f"{name} {label}")`
  (line 232) — a string MESSAGE, not the actual exception. The child's
  Python traceback is lost. Sentry groups all subprocess failures
  together by name, but a `gen_pdf.py: KeyError: 'lane'` and a
  `gen_pdf.py: timeout 124` produce the same fingerprint.
- **What to do:** Capture last 100 lines of the child's stderr (which
  today goes straight to the wrapper's run-log) and include as a Sentry
  `extra` so the message + tail give actionable signal. Even better:
  capture as a separate event with a stacktrace-derived fingerprint.
- **Effort:** ~15 lines in `run_step`.

### Finding 3.3 — `teams_alert.py` lacks Sentry — but it's the Teams/Slack push
- **Severity:** Medium
- **Files:** `scripts/teams_alert.py` (referenced from wrapper line 142)
- **What's wrong:** If `teams_alert scan` itself errors (webhook URL
  malformed, JSON serialization bug), there's no Sentry trail — and
  Teams alerts are the real-time alert primitive. The alerting tool
  has no alerts.
- **What to do:** Add `sentry_setup.init(component="teams_alert")`.
- **Effort:** 3 lines.

---

## 4. Self-heal recurrence — no tracking

`gen_improvements_report.py:746-758` currently says, in prose, "if the
SAME fix keeps applying day after day that's a root-cause issue at
intake." That's the right intuition, but the system does NOT actually
track which fix categories recur.

### Finding 4.1 — `qc.fixes` Sentry metric is single-dimensional
- **Severity:** High
- **Files:** `scripts/qc_selfheal.py:127`
- **What's wrong:** The metric increments by `phase=pre|post-patch`
  only. There's no `category` tag (e.g. `category=teu_defaulted`,
  `category=carrier_copied_from_quoted`). So Sentry shows "30 fixes
  today" but cannot answer "is fix X the same as yesterday's fix X?"
  Today's audit literally hints at this with the `teu_won defaulted to
  teu_requested` example — exactly the kind of recurring fix that
  signals an intake bug.
- **What to do:** (a) Add a small `category` dimension to each
  `log.fix(...)` call (the existing 25+ fix messages all start with a
  predictable phrase — extract a regex token). (b) Persist
  `reports/qc-fix-log.jsonl` (append-only, fix-name + count + date)
  so a 7-day rolling tally can be displayed in the audit email's
  "recurring fixes" section.
- **Effort:** Medium — ~60 lines, plus a small audit-email widget.

### Finding 4.2 — Right place to add: `scripts/qc_selfheal.py` `log.fix()` + a `_persist_fix_history()` helper
- **Severity:** N/A — implementation note
- **Files:** `qc_selfheal.py:121-128`
- The `log.fix(msg)` method (line 121) is the single chokepoint. Add a
  `categorize_fix(msg) -> str` returning one of ~20 well-known keys
  ("teu_defaulted", "containers_recovered", "ol_rate_backfilled",
  "carrier_from_subject", "loss_reason_to_covered", "request_id_assigned",
  etc.), then `_sentry.metric_increment("qc.fixes", 1, phase=...,
  category=...)`. Persist a 30-day rolling JSON next to qc-result.json.

---

## 5. PRICE classifier change (PR #21) — observability fallout

`scripts/core.py:79-81` lists the active LOSS_REASONS — PRICE remains
plus the new UNDIFFERENTIATED. Searches for hardcoded `"PRICE"`
comparisons:

### Finding 5.1 — `_RATE_DRIVEN` set is the right abstraction; one consumer is hardcoded
- **Severity:** Medium
- **Files:** `scripts/core.py:1076`, `src/hilmar/core.py:1034`
- **What's wrong:** `_RATE_DRIVEN = {"PRICE"}` is intentionally narrow
  per the PR-21 design comment ("UNDIFFERENTIATED falls into 'other'
  intentionally"). That's correct. BUT downstream consumers reach into
  raw `loss_reason` without going through `aggregate_loss_reasons`:
  - `scripts/gen_email.py:1388` — `_REASON_META` has explicit `PRICE`
    AND `UNDIFFERENTIATED` entries (good, ships safely).
  - `scripts/gen_rate_intelligence.py:160` — filters `loss_reason ==
    "NO_RESPONSE"` only (irrelevant to PRICE bucket). Safe.
  - **Sentry tags** — none of the events tag by `loss_reason`, so
    Sentry queries that filter "events where loss_reason==PRICE" would
    silently miss the new bucket. Currently no such query exists, but
    if Michael builds a Sentry dashboard widget for it, the new bucket
    must be in the dropdown.
- **What to do:** Add a unit test in `tests/` asserting that every
  enum in `core.LOSS_REASONS` appears in `gen_email._REASON_META`. Add
  Sentry dashboard documentation reminding to include UNDIFFERENTIATED
  in any rate-driven filter.
- **Effort:** Small (~20 lines).

### Finding 5.2 — Old data on disk still has the catch-all PRICE classification
- **Severity:** Medium
- **Files:** `tracking-data-v2.json` (state), `scripts/core.py:705-721`
  (decide_status docstring)
- **What's wrong:** Pre-2026-06-02 rows in `tracking-data-v2.json` were
  classified under the old catch-all PRICE rule. They'll stay PRICE
  forever unless reclassified. Any historical rolling 60-day window
  comparing PRICE share will show a step-change on 2026-06-02
  ("PRICE collapsed!") that's a classifier change, not market change.
- **What to do:** Either (a) one-time backfill: re-run `decide_status`
  over historical rows with the new classifier (in a migration
  script + commit; mark rows with `_reclassified=true`), or (b) annotate
  the audit email with a "Classifier change line on 2026-06-02" caption
  so the analytics don't read as real signal.
- **Effort:** Backfill is ~50 lines + a careful re-run.

---

## 6. Backup integrity — freshness without verification

### Finding 6.1 — No tar.gz integrity check
- **Severity:** High
- **Files:** `scripts/backup_offline.py:89-100, 175-195`,
  `qc_selfheal.py:2179-2224` (QC-032)
- **What's wrong:** Both targets are tar.gz archives. `_make_archive`
  builds them; nothing verifies the tarball is well-formed afterwards.
  If `gzip` corrupts mid-write (disk-full, OneDrive sync collision), a
  damaged `.tar.gz` lands on disk; QC-032 sees a fresh mtime and PASSES.
  Recovery time at "tracking-data-v2.json corrupts" → "open today's
  backup" is until you discover the tarball is also broken.
- **What to do:** Immediately after `_make_archive`, open the archive
  and run `tar.getnames()` + verify it contains `tracking-data-v2.json`.
  On failure, retry once, then ERROR. Add QC-032b that randomly
  picks one of the last 7 backups and `tar.list()`s it — drift-detects
  corruption ahead of need.
- **Effort:** ~20 lines.

### Finding 6.2 — No size/row-count regression alarm
- **Severity:** Medium
- **Files:** `scripts/backup_offline.py`
- **What's wrong:** If today's `tracking-data-v2.json` is corrupted to
  an empty `{"requests": []}`, today's backup faithfully archives the
  empty state. Tomorrow we have NO good backup of yesterday's data.
- **What to do:** Pre-backup invariant: archive only if request count
  is within ±15% of the 7-day average. Otherwise ERROR + escalate.
- **Effort:** ~25 lines.

### Finding 6.3 — `backup_offline.py` writes `.tar.gz` extension but `_make_archive` mode `"w:gz"` will silently truncate
- **Severity:** Low
- **Files:** `scripts/backup_offline.py:92`
- **What's wrong:** If disk fills mid-write, tarfile context manager
  raises on `__exit__`; the file on disk is now a truncated archive
  with valid magic bytes but invalid contents. QC-032 again sees
  fresh mtime.
- **What to do:** Same as 6.1 — list contents after write.

---

## 7. Recovery playbook gaps in `RUNBOOK.md`

`RUNBOOK.md` covers 9 failure modes. These are **not** covered:

### Finding 7.1 — OneDrive sync paused / stopped
- **Severity:** High
- **Files:** `RUNBOOK.md` (missing entry)
- **What's wrong:** RUNBOOK.md:189 mentions "OneDrive may be paused →
  resume sync from system tray" inside the backup_offline failure mode,
  but there's no dedicated section. Symptoms: stale `scripts/` (QC-026
  fires), MSAL token cache stale, secrets/sentry-dsn.txt unreadable on
  fresh Cloud PC reboot, dual offline backup fails. Recovery: `OneDrive
  /resume`, then re-run wrapper. Possibly need to break + re-link the
  PROJECT HILMAR folder.
- **What to do:** Add a "Failure mode: Cloud PC OneDrive sync stopped"
  section with: detection (`OneDrive.exe` running? sync status icon?),
  recovery commands, expected QC fallout.
- **Effort:** ~30 lines of doc.

### Finding 7.2 — Anthropic API down — LLM fallback graceful?
- **Severity:** Medium
- **Files:** `scripts/pdf_llm_rescue.py:154-184`,
  `scripts/qc_actions_from_sentry.py:423-436`,
  `src/hilmar/orchestrator.py`, `src/hilmar/model_router.py`
- **What's wrong:** `pdf_llm_rescue` no-ops cleanly when API key missing
  (line 156). It does NOT handle 5xx, rate-limit, or hung request
  gracefully — those would raise from `client.messages.create`. The
  `claude_diagnose` path in `qc_actions_from_sentry` (line 423) checks
  for key missing, not for API down. RUNBOOK has no entry. LLM paths
  are: PDF rescue (production), Seer fallback diagnose (Sentry action),
  parser_fallback (currently disabled via env in tests but enabled in
  prod via `src/hilmar`).
- **What to do:** Wrap each Anthropic call in
  `try/except (RateLimitError, APIStatusError, httpx.TimeoutException,
  APIConnectionError)` — log + skip, never raise. Add RUNBOOK entry.
- **Effort:** ~20 lines code + ~20 lines doc.

### Finding 7.3 — Sentry quota exhausted
- **Severity:** Medium
- **Files:** Nothing handles 429s from Sentry
- **What's wrong:** When the Sentry org hits its monthly event quota,
  `sentry_sdk.capture_*` returns silently but the pipeline still calls
  it — fine in the happy path. Cron check-ins, however, switch to
  Sentry's "events disabled" path. Result: liveness monitor sees no
  failed check-in (because no check-in arrived AT ALL), and Sentry's
  Cron monitor fires "missed check-in." But the pipeline DID run.
  False positive again. RUNBOOK has no entry.
- **What to do:** Document the Sentry quota dashboard URL, set a Sentry
  quota usage alert (Sentry has this natively), and prefer GitHub-
  Actions heartbeat (`heartbeat.yml`) over Sentry Cron monitor as
  primary liveness — Sentry secondary.
- **Effort:** Doc-only ~20 lines.

### Finding 7.4 — Anthropic key file missing — silent degradation
- **Severity:** Low
- **Files:** `scripts/pdf_llm_rescue.py:62-66`
- The file is gitignored. On fresh Cloud PC, it'll be absent and PDF
  rescue silently no-ops — which means QC-027b ("PDF-only WINs") will
  permanently warn. No alert tells Michael "key missing." Add a one-time
  QC check at qc_selfheal startup.

---

## 8. QC-021 false-positive mystery

Today's audit fired QC-021 WARN: "today's wrapper started but pipeline
never completed" — yet the audit email landed (so pipeline DID
complete). Reading `scripts/qc_selfheal.py:1130-1184` reveals the root cause.

### Finding 8.1 — QC-021's run-log search is fragile to log truncation, locale, and ordering
- **Severity:** High
- **Files:** `scripts/qc_selfheal.py:1132-1141`
- **What's wrong:** Three independent bugs combine:
  1. `_tail = _log_path.read_text()[-40000:]` (line 1134) — only the
     last 40 KB. A long fire (many xcopy lines, lots of QC fixes) can
     exceed 40 KB on its own and push today's `--- run_pipeline ---`
     marker off the front.
  2. `_dt.now().strftime("%m/%d/%Y")` (line 1135) — this is the
     PYTHON-PROCESS local time. On Cloud PC: ET. In Codespaces: UTC.
     The run-log timestamp is `%DATE% %TIME%` from `cmd.exe` — which is
     LOCAL Windows time. If qc_selfheal runs in a process whose locale
     differs from the wrapper's, the `_today_us` literal won't match the
     run-log header.
  3. The check uses `max(_tail.find(_today_us), _tail.find(_today_iso))`
     to pick a marker. If yesterday's marker is `2026-06-01` and it
     appears later in the file than today's `06/02/2026` (e.g. because
     a manual fire happened mid-day yesterday after the morning fire),
     the search picks the wrong anchor and reports "no step markers"
     for today.
- **What to do:** Replace the `find()`-based anchor with a proper
  parse: split the run-log on the `========` divider, isolate today's
  block (header line contains both "Hilmar daily on <host> —" and the
  date), then search inside the block. Also rotate run-log.txt or bump
  the tail to 250 KB. Optionally: have the wrapper write
  `reports/last-fire-summary.json` directly (status, last_step, ended_at)
  so QC-021 doesn't have to scrape unstructured log text at all.
- **Effort:** ~40 lines + a small wrapper change. The
  last-fire-summary.json approach is cleanest.

### Finding 8.2 — QC-021 should query its own evidence (sent flag, audit-flag)
- **Severity:** Medium
- **What's wrong:** Today's audit email landing IS evidence the wrapper
  ran. If QC-021 cross-checked `reports/sent-YYYY-MM-DD.flag` and saw
  "exists + within last 2h," it would suppress the false-positive WARN.
- **What to do:** Add a second confirmation source — flag existence —
  before WARNing. Mark the message "stale-log" vs. "actually-broken."
- **Effort:** ~10 lines.

---

## 9. Cloud-PC runner migration — half-deployed risks

`.github/workflows/daily-fire.yml` exists (line 1 names it
"Daily fire (Cloud PC self-hosted)") and has `on.schedule: cron: '0 14
* * 1-5'`. Comments at lines 19-21 say the runner isn't installed; the
RUNNER side of the migration is on the `claude/move-off-cloud-pc-gha-app-auth`
branch.

### Finding 9.1 — `daily-fire.yml` is on `main` with an enabled schedule
- **Severity:** Critical
- **Files:** `.github/workflows/daily-fire.yml:30-33`
- **What's wrong:** Until the self-hosted runner is registered, the
  scheduled trigger at 14:00 UTC will queue a job that finds no
  available runner and either waits indefinitely (Actions UI shows
  "queued") or fails with "no runner available" — alerting on a workflow
  that was deliberately not wired yet. Worse, if a runner IS registered
  partway through (e.g. someone test-installs it), the Cloud PC will
  fire TWICE: once via Windows Task Scheduler (10 AM ET via
  `run_daily_laptop.cmd`) and once via daily-fire.yml. `outlook_send`'s
  idempotency flag stops dupe emails, but the full pipeline runs twice
  — burning ~2× Sentry events, 2× Anthropic API calls, possibly tripping
  rate-limits.
- **What to do:** Disable `on.schedule` in `daily-fire.yml` until the
  runner is provisioned. Either delete the block or add a top-level `if:
  false` workflow_dispatch-only gate.
- **Effort:** 3 lines.

### Finding 9.2 — Runner setup docs split across branches
- **Severity:** Medium
- **Files:** `docs/CLOUD-PC-RUNNER-SETUP.md` (referenced from
  daily-fire.yml:19), `docs/MOVE-OFF-CLOUDPC.md`
- **What's wrong:** Docs reference unmerged branch state. A future
  Claude session reading `docs/CLOUD-PC-RUNNER-SETUP.md` may follow
  half-correct instructions. Cross-link in the doc that the auth path
  is still being negotiated with OL IT (2026-05-30 note in
  daily-fire.yml comments).
- **What to do:** Add a "STATUS: NOT YET DEPLOYED" banner to both docs.

---

## 10. Observability of observability

### Finding 10.1 — Sentry DSN file is local, has no rotation alarm
- **Severity:** Medium
- **Files:** `scripts/sentry_setup.py:144-156`, `secrets/sentry-dsn.txt`
- **What's wrong:** Sentry DSN doesn't expire, BUT auth tokens
  (`secrets/sentry-auth-token.txt`, used by `sentry_api.py`) DO. There's
  no QC analogous to QC-023's MSAL freshness check for the Sentry auth
  token. If it expires, `qc_actions_from_sentry.py` and `sentry_seer.py`
  silently fail their REST calls but the pipeline reports green.
- **What to do:** QC-057 — Sentry auth-token freshness probe (HEAD
  request to a cheap Sentry endpoint), WARN at 60d, ERROR at 80d.
- **Effort:** ~20 lines.

### Finding 10.2 — `qc_alert_if_needed.py` recipient mismatch
- **Severity:** Low
- **Files:** `deploy/qc_alert_if_needed.py:21`
- **What's wrong:** Sends to `michael.deitchman@ol-usa.com` (line 21),
  but the project-wide convention (CLAUDE.md, RUNBOOK.md, audit email)
  uses `michael.deitchman@idealx.us`. If the @ol-usa account is ever
  cut (e.g. OL employment change), QC alerts go nowhere. Hard-coded
  address that bypasses `config.json` distribution.
- **What to do:** Read recipient from config.json `qc_alert.recipient`
  with fallback to idealx.us.
- **Effort:** ~5 lines.

### Finding 10.3 — No telemetry on GitHub Actions outages
- **Severity:** Low
- **What's wrong:** The whole liveness + heartbeat scheme assumes
  GitHub Actions is up. GH Actions historically has multi-hour incidents.
  When GHA is down, neither heartbeat nor liveness runs — and nothing
  paged Michael. Lowest-cost mitigation: subscribe the audit-recipient
  to https://www.githubstatus.com/atom feed.

### Finding 10.4 — Sentry cron monitor & GitHub liveness monitor are duplicative without cross-check
- **Severity:** Medium
- **Files:** `scripts/sentry_setup.py:334-393`, `.github/workflows/liveness.yml`
- **What's wrong:** Both watch "did 10 AM ET fire happen." They don't
  watch each other. Today, Sentry Cron AND GHA-liveness both happily
  PASSed even though QC-021 WARNed — because both judge "fire happened"
  on different signals (Sentry: pipeline.py-emitted check-in; GHA:
  heartbeat workflow run). A divergence between the two means SOMETHING
  is broken, and right now nobody flags the divergence.
- **What to do:** A small reconciliation step: at end of pipeline, log
  to Sentry "GHA heartbeat status=success/failed" so the two channels'
  conclusions can be diff'd in a Sentry dashboard widget.
- **Effort:** ~10 lines.

---

## Top 5 reliability priorities

Ordered by silent-failure risk and effort/value ratio:

1. **Wrap pipeline subprocesses with Sentry init** (§3.1). Twelve scripts
   currently run blind. Three lines each → instant 5× expansion of the
   error surface Sentry sees. This is the single largest observability
   gap; it makes every other Sentry-based finding more valuable.

2. **Fix QC-021's false-positive logic** (§8.1) — replace run-log
   scraping with a wrapper-written `reports/last-fire-summary.json`,
   and cross-confirm against `sent-YYYY-MM-DD.flag`. Today's audit
   already shows the noise; recurring false-positives will train
   Michael to ignore the check, defeating its purpose.

3. **Disable scheduled trigger in `daily-fire.yml`** (§9.1). It's
   live-on-main and one runner-registration away from double-firing
   the pipeline. Three-line PR; large blast-radius prevented.

4. **Verify backup tar.gz integrity + row-count invariant** (§6.1, §6.2).
   Today's QC-032 lies: it checks freshness, not validity. A corrupt
   backup is worse than no backup because nobody investigates. Add a
   post-write `tar.list()` and a ±15% row-count regression alarm.

5. **Categorize self-heal fixes + persist 7-day history** (§4.1). The
   audit email already prompts Michael for this. Without categories,
   "30 fixes today" doesn't actionably surface intake bugs; with them,
   the daily audit can name the recurring fix and point at the root
   cause. The biggest signal-quality win in the audit-feedback loop.

Honorable mentions: QC-021 run-log rotation (§2.4) and Sentry-asleep
detection (§2.2) are both small adds that close real silent-failure
windows.
