# Reliability — "prove the report shipped, or scream"

This is the root-cause fix for the 2026-06 silent week (no report for ~5 days,
no alarm). Written after an exhaustive audit (see the session that produced it).

## The single root cause

> The system assumed success by reaching the wrapper's last line. It never
> **proved** the client email shipped, and its only alarm (the QC audit email)
> routed through the very Outlook/MSAL channel most likely to be broken when
> there's something to alarm about.

Every symptom of that week descended from it: reports silently stopped; the box
drifted to Python 3.14 with deps missing; the heartbeat reported a hardcoded
`success` after an unverified send; manual reruns built artifacts but sent
nothing.

The fix flips the model from *"assume success unless an exception escaped"* to
**"prove the deliverable shipped or scream out-of-band"**, and pins the box so
it can't silently drift in the first place.

## What's fixed in-repo (done)

### Remove the drift at the source
- **`.python-version` = 3.12** — the one version CI, the box, and QC-061 all
  consume. **pyproject `requires-python ">=3.12,<3.13"`** so the toolchain
  itself rejects an unvalidated interpreter (it had no upper bound; the box ran
  3.14). `pdfplumber` + `sentry-sdk` were undeclared — added.
- **`requirements.txt`** is now the canonical, Windows-installable runtime list
  covering every module QC-054 verifies (jinja2/jsonschema/dateutil/sentry-sdk/
  anthropic were missing). **`setup_cloudpc.ps1`** pins to `.python-version`
  (was `winget install 3.14`) and installs `requirements.txt` (was a hand-typed
  4-package line).

### Enforce it where it runs (QC checks, each with index row + route + test)
- **QC-061 interpreter parity** — ERROR when `sys.version` major.minor ≠ the
  pin. Runs inside the wrapper's interpreter, so it catches the 3.14 drift.
- **QC-060 dep-list consistency** — every `RUNTIME_IMPORT_REQUIRED` module
  pinned in `requirements.txt`; `pyproject` deps == `requirements-tracker.txt`.
  Repo-state, so it fails CI too.
- **QC-062 layout hygiene** — deletes stale `tests/`+`src/` shadow copies under
  the repo root (the pytest "import file mismatch" cause); no-op in dev/CI.
- **QC-054 self-heal** — pip-installs a missing dep + re-imports instead of
  emailing a human a pip command.

### Prove-or-scream
- **`scripts/fire_alert.py`** — OUT-OF-BAND alerts (GitHub issue + Teams webhook
  + durable `reports/alerts-queue.json` + stderr). **Never** Outlook/MSAL.
- **`scripts/preflight_env.py`** — wrapper Step 0.5: HARD-fails (rc=2, aborts
  the fire) on interpreter drift; soft-flags missing deps + a behind checkout;
  raises the out-of-band alert. Builds nothing on an unvalidated interpreter.
- **`deploy/assert_fire_integrity.py`** — mandatory final wrapper step: asserts
  pipeline rc==0 + fresh artifacts + today's `sent-YYYY-MM-DD.flag` (proof the
  send happened) + token cache present. On ANY violation it raises the
  out-of-band alarm and exits non-zero.
- **Wrapper (`run_daily_laptop.cmd`)** — sets `HILMAR_NONINTERACTIVE=1` (stale
  token fails fast, never hangs); runs preflight before building; captures the
  send rc; runs the integrity asserter and passes the **real** status to the
  heartbeat; returns a non-zero exit so Task Scheduler's Last-Run-Result is red
  on a failed fire (was an unconditional `exit /b 0`).
- **`heartbeat.yml`** — FAILS its job on `status != success`, so `liveness.yml`'s
  existing (previously dead) failed/stale branch fires and files the
  `cloud-pc-down` issue + auto-recovers. The heartbeat is now honest.
- **`run_pipeline.py`** — prints a LOUD "BUILD COMPLETE — NOTHING WAS SENT"
  banner when today's send-flag is absent, so a hand-run can't be mistaken for
  a shipped report (the footgun Michael hit).

## What YOU must do on the Cloud PC (one-time, in order)

1. **Install Python 3.12, remove 3.14, repoint the wrapper.** Re-run
   `deploy\setup_cloudpc.ps1` (now pins to `.python-version` and installs the
   full `requirements.txt`). Confirm `python --version` is 3.12 in the wrapper's
   shell.
2. **Delete the stale shadow dirs** (QC-062 also self-heals these, but clear
   them now):
   `Remove-Item -Recurse -Force "$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR\tests","$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR\src"`
3. **Confirm the scheduled task fires** and surfaces a non-zero Last-Run-Result;
   set "wake to run" + "run task ASAP after a missed start" + a no-sleep power
   setting at the fire window so a sleeping box doesn't silently miss.

## Infra decisions (you decide)

- **A loud channel that reaches you and doesn't depend on the box:** configure
  `config.alerts.teams_webhook_url` (Teams → Connectors → Incoming Webhook) so
  `fire_alert`/liveness ping your phone; and/or provision a dedicated alert
  email secret (separate Graph app or SMTP) for liveness — NOT the box's MSAL
  cache. Until then the GitHub `cloud-pc-down` issue is the out-of-band channel.
- **App-only Entra app (GRAPH_APP_*)** so GitHub-Actions recovery is
  IP-independent — without it, liveness auto-dispatch is theatre (OL Conditional
  Access blocks the runner IP) and recovery must be a loud page to fire the box.
- **Move the primary fire off the single Cloud PC** (one box / one IP / one
  token is still a single point of failure even with all the above).

## What is still silent after this (be honest)

- The loud alert must reach you off-box (Teams/email secret) — the GitHub issue
  is out-of-band but you don't watch issues. Configure one of the channels above.
- A sleeping/rebooted box at 6 PM ET still MISSES until liveness pages (~next
  evening) — the integrity assert makes a miss LOUD, not impossible.
- If the GitHub PAT expires, the heartbeat dispatch AND liveness degrade
  together — needs a PAT-expiry check + a non-GitHub fallback (Teams).
