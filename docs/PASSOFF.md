# Passoff — state of `main` after the 2026-06-25 reliability session

Read this first in a fresh context. It captures what `main` is now, why, and
what's still on the operator's plate. Pairs with `docs/RELIABILITY.md` (the deep
dive) and `CLAUDE.md` (the standing rules).

## TL;DR

A week of **silently** missing daily reports was root-caused and fixed. The
system can no longer (a) silently drift its environment, or (b) silently fail to
ship — it now **proves the report shipped or screams out-of-band**, and a fresh
Cloud-PC environment-drift **pages** the operator the same day. The Cloud PC has
been brought onto the validated environment (Python 3.12 + full deps). Eight PRs
merged today (#57–#64); full suite is green (~1310 tests).

## What was broken (the root cause)

The pipeline runs unattended on one Win365 Cloud PC. It **assumed success by
reaching the wrapper's last line** — it never proved the client email left the
mailbox, and its only alarm (the QC audit email) rode the same Outlook/MSAL
channel most likely to be broken. Meanwhile the box had silently drifted to
**Python 3.14** (untested; CI is 3.12) with **jinja2 + sentry-sdk missing**, and
**nothing checked the environment the code ran on** — only the data. So a
degraded/failed fire looked green and reports were silently absent for ~5 days.

## What changed (merged to `main` today)

| PR | What |
|---|---|
| #57 | Parser: OL **prose** quotes + `<DEST> <region> from <ORIGIN>` subjects (both trees); **QC-057** intake reconciliation (silently-dropped RFQ); 250-result fetch-cap guard; durable **Turso historian** + **QC-058**; data-flow self-heal (`reprocess_bodies` pre-ingest) + **QC-059**; `state_store.backup()` prune fix |
| #58 | `run_audit_tests` classifies a missing test-dep as *environment-incomplete* (SKIP), not a code FAIL; prefers the real checkout |
| #59 | Loss-reason bar labels no longer wrap (value sits outside the bar) |
| #60 | **Reliability core**: `.python-version`=3.12 + `requires-python` upper bound; `requirements.txt` = canonical complete runtime list; **QC-060** dep-consistency, **QC-061** interpreter parity, **QC-062** layout hygiene, **QC-054** dep self-heal; `preflight_env.py` (abort-on-drift), `fire_alert.py` (out-of-band: GitHub issue + Teams + queue, never Outlook), `deploy/assert_fire_integrity.py` (prove the send shipped); honest heartbeat; "BUILD COMPLETE — NOTHING WAS SENT" banner |
| #61 | **QC-063** consecutive-failure ratchet (a best-effort step dead 3 fires); **QC-023** token-expiry route |
| #62 | Wrapper resolves `py -3.12` first (a stray 3.14 no longer shadows 3.12) |
| #63 | Fire auto-syncs `deploy/*.py`; `setup_cloudpc.ps1` safely deploys the wrapper |
| #64 | **Environment-drift sentinel**: box stamps an env fingerprint → heartbeat → `heartbeat.yml` files a `box-env-drift` issue if a fire shipped on a drifted env, auto-closing on recovery |

New QC checks this session: **QC-057** (intake), **QC-058** (historian
freshness), **QC-059** (data-flow integrity), **QC-060** (dep consistency),
**QC-061** (interpreter parity), **QC-062** (layout hygiene), **QC-063**
(failure ratchet). All in `reports/QC-INDEX.md` with routes + tests
(`tests/test_qc_governance.py` enforces it).

## Current state (verified)

- **Cloud PC**: on Python **3.12** with the full dependency set; the live
  wrapper prefers `py -3.12`; the deployed scripts/asserter are current.
  `py -3.12 hilmar-daily-routine\scripts\preflight_env.py` returns **green**.
- **Code can't silently drift**: pinned + enforced every fire (QC-061/060/062)
  and in CI (QC-060 + the suite).
- **The fire proves-or-screams**: preflight gate → build → integrity assertion
  (rc + fresh artifacts + send-flag) → out-of-band alarm + honest heartbeat.
- **Drift is paged**: the sentinel files a `box-env-drift` GitHub issue even on
  a shipped-but-degraded day.

## OPEN ITEMS (operator / infra — NOT yet done)

1. **A loud channel that reaches Michael** *(2-min, high value)* — the alarms
   currently land as GitHub issues he doesn't watch. Set
   `config.alerts.teams_webhook_url` (Teams → Connectors → Incoming Webhook) and
   `fire_alert` + the sentinel ping his phone. **Until this, alerts are filed
   but not delivered.**
2. **Reconcile the fire TIME** *(real inconsistency)* — the Cloud-PC Task
   Scheduler + `setup_cloudpc.ps1` fire at **10:00 AM ET**, but this session
   moved the Sentry cron monitor + GH `liveness/daily` schedules toward the
   evening. They must agree or the cron monitor will false-alert "missed
   check-in." Decide one time; align Task Scheduler + `sentry_setup` monitor
   schedule + `liveness.yml`/`daily.yml` crons + the wrapper header.
3. **Scheduled-task hardening** — confirm the task is enabled, surfaces a
   non-zero Last-Run-Result, and the box won't sleep through the window
   (wake-to-run / run-ASAP-after-missed / no-sleep at fire time).
4. **Infra decisions** (Michael's call):
   - **App-only Entra app** (`GRAPH_APP_*`) so GitHub-Actions recovery is
     IP-independent — without it, liveness auto-dispatch is theater (OL
     Conditional Access blocks the runner IP). The Cloud PC *can* fire (its IP
     passes CA — verified via `verify_fire_prereqs.check_delegated_cache`).
   - **Move the fire off the single Cloud PC** — it's still one box / one IP /
     one MSAL token. Today's work makes a miss *impossible to hide*; it does not
     make the box *redundant*. A box asleep at fire time still misses until
     liveness pages (~next evening).
5. **GitHub PAT expiry** — the box's heartbeat dispatch + liveness both depend
   on it; if it expires they degrade together. Needs a PAT-expiry check + a
   non-GitHub fallback (Teams).

## How to operate / verify (Cloud PC, PowerShell)

```powershell
cd "$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR"
# verify the box env (no send):
py -3.12 hilmar-daily-routine\scripts\preflight_env.py        # want: ✅ Preflight OK
# pull + deploy after a code change:
git -C hilmar-daily-routine pull origin main
& "hilmar-daily-routine\deploy\setup_cloudpc.ps1"             # re-run after a WRAPPER change
# fire the full daily run (pull → reprocess → ingest → build → SEND):
deploy\run_daily_laptop.cmd
# re-build the report to yourself only, no full send:
python scripts\outlook_send.py daily --to michael.deitchman@idealx.us --force --no-flag `
  --subject-from-file reports\email-subject.txt --body-from-file reports\email-body.html `
  --attach reports\hilmar-dashboard.html reports\hilmar-report.pdf
```

Note: `run_pipeline.py` BUILDS but does **not** SEND — only the wrapper /
`outlook_send` send. The "BUILD COMPLETE — NOTHING WAS SENT" banner guards this.

## Pointers

- `docs/RELIABILITY.md` — the prove-or-scream design + what's still silent.
- `docs/HISTORIAN.md` — the dormant Turso stats DB (provision when wanted).
- `docs/PARSER-GAPS.md` — parser remediation history (incl. the Korea prose fix).
- `reports/QC-INDEX.md` — every QC check + self-heal + route.
- `CLAUDE.md` — standing rules (the §3 "solve root causes, never patch
  symptoms" + "every QC ships with check/heal/route/test" contract).
