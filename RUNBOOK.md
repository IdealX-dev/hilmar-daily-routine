# Hilmar Tracker — Runbook

Operational guide for the daily Hilmar Ingredients tracker. Cross-reference
this when something breaks or a routine task needs doing.

**Audience**: Michael (operator), Claude (future sessions), anyone else who
inherits this system.

---

## Daily fire (6:07 PM ET weekdays)

**Trigger**: Cloud PC `CPC-micha-E552L` Windows Task Scheduler → `deploy\run_daily_laptop.cmd`

**Expected outcome by 6:25 PM ET**:
- 10 OL/IdealX recipients receive `Hilmar Ingredients — Daily Shipment Tracker Update (May N, 2026)`
- `michael.deitchman@idealx.us` receives audit `Hilmar Tracker — Daily Systems Audit (May N) — XR/YO/ZS`
- Dual offline backups written: `OneDrive - IdealX/HILMAR_BACKUPS/hilmar-YYYY-MM-DD.tar.gz` + `~/hilmar-local-backups/hilmar-YYYY-MM-DD.tar.gz`
- `reports/run-log.txt` has a new entry headed `Hilmar daily on CPC-micha-E552L — <day> <date> 18:07:0X`
- `reports/sent-YYYY-MM-DD.flag` written

**If no emails by 6:30 PM ET**:
1. RDP into Cloud PC via `windows.cloud.microsoft` from any browser
2. Check `reports/run-log.txt` tail — find today's date marker
3. Read the failure mode below that matches what you see

---

## Failure mode: wrapper exited rc=255 (no send after pipeline OK)

**Symptom**: run-log has "Pipeline exit code: 0" but no "Sent. request-id=" line. Task Scheduler reports LastTaskResult = 255.

**Root cause** (historical): early wrapper had `for /f powershell` + delayed-expansion IF/ELSE that crashed silently. Fixed in commit `5f6ac46` (2026-05-11) — wrapper now just calls scripts in sequence.

**If you see it again**:
1. Open `deploy\run_daily_laptop.cmd` — has it been reverted to the buggy version?
2. Run manually from RDP: `cd PROJECT HILMAR; deploy\run_daily_laptop.cmd`
3. If it still crashes, check Cloud PC's Python install: `where py` and `py --version`

---

## Failure mode: refresh_stage rc=3 path not found

**Symptom**: run-log has "REFRESH_STAGE FAILED rc=3" or "The system cannot find the path specified."

**Root cause**: Python interpreter path missing on this machine (hardcoded path doesn't exist).

**Fix**: wrapper already does dynamic Python discovery (commit `967f649`). If you see this:
1. Verify Python is installed: `where python` or `where py` on Cloud PC
2. If neither found, install Python 3.12+ via `winget install Python.Python.3.12`
3. Re-run wrapper

---

## Failure mode: MSAL silent token refresh failed

**Symptom**: `outlook_send.py` errors with "silent token refresh failed" or QC-023 warns ">60d cache age".

**Root cause**: MSAL refresh token expires after ~90 days of disuse. The token cache at `secrets/token-cache.json` needs interactive re-auth.

**Fix**:
1. Open Cloud PC RDP (or run from MBD-TRAVEL — same OneDrive token cache)
2. `cd "PROJECT HILMAR"`
3. `python scripts/outlook_send.py auth`
4. Follow the device-code flow — paste URL into a browser, enter code, sign in as `michael.deitchman@ol-usa.com`
5. New token cached. Next pipeline fire will use it.

**Prevention**: QC-023 warns at 60 days. Set a calendar reminder to re-auth quarterly.

---

## Failure mode: duplicate emails to recipients

**Symptom**: 10 recipients each get 2+ identical daily emails.

**Root cause**: Both MBD-TRAVEL and Cloud PC fired on the same day, OR a manual fire ran after the scheduled fire.

**Fix**: shouldn't happen — `outlook_send.py daily` has built-in idempotency via `reports/sent-YYYY-MM-DD.flag`. Second send blocked at script level. If you see it:
1. Check `reports/sent-YYYY-MM-DD.flag` — if it doesn't exist, idempotency broke
2. Verify `outlook_send.py` still has the flag-check at the top of `cmd_daily()`
3. To re-enable idempotency: redeploy from commit `f6aae29` or later

**Manual force**: `outlook_send.py daily --to-from-config ... --force` overrides the flag intentionally.

---

## Failure mode: data missing on rows (ETD, vessel, rate, etc.)

**Symptom**: Daily email shows status changes with empty carrier/rate cells, or QC-027 errors with <90% completeness.

**Root cause**: One of:
1. OL changed their email template (e.g. switched to a multi-line table — historical bug fixed 2026-05-13)
2. `patch_carriers.py` backfill didn't run
3. Source body for the row isn't in `scripts/stage_emails_bodies.txt`
4. PDF attachment not downloaded (for booking-confirmation WIN rows)

**Fix**:
1. Force a full refresh + re-pull PDFs:
   ```
   python scripts/refresh_stage.py --days-back 60 --pdf-backfill
   python scripts/run_pipeline.py
   ```
2. Audit a specific missing row:
   ```python
   import json, sys; sys.path.insert(0,'scripts')
   import body_parser as BP
   d = json.load(open('tracking-data-v2.json'))
   r = next(r for r in d['requests'] if r['request_id'] == 'req_XXXXXX')
   imids = r.get('source_imids') or []
   # check stage_emails_bodies.txt for these imids
   ```
3. If parse_rate_table fails on a body that looks parseable, inspect the body raw and adjust the regex.

---

## Failure mode: Cloud PC didn't fire at 6:07 PM

**Symptom**: No 18:07 entry in run-log for today; Task Scheduler "LastRunTime" is yesterday or older.

**Root cause** (possibilities, in order):
1. Cloud PC was offline / stopped at 6:07 PM ET
2. Task got disabled or deleted
3. Cloud PC's Windows credentials expired
4. Microsoft 365 service incident

**Fix**:
1. RDP into Cloud PC via `windows.cloud.microsoft`
2. Open Task Scheduler → find "Hilmar Daily Tracker - CloudPC"
3. Right-click → Run (manual fire)
4. If task missing entirely: re-run `deploy\setup_cloudpc.ps1` from RDP

**Fallback while Cloud PC is broken**: fire manually from MBD-TRAVEL:
```
cd "C:\Users\MichaelDeitchman\OneDrive - IdealX\claude\PROJECT HILMAR"
deploy\run_daily_laptop.cmd
```

---

## Failure mode: wrapper started but pipeline never completed (QC-021)

**Symptom**: the audit (which runs as its own scheduled task) shows
QC-021 "wrapper started but pipeline never completed — died before the
refresh_stage echo", plus everything 48-72h stale (stage, email-subject,
shared store, backups). No customer email shipped.

**Root cause — the wrapper-drift trap (hit twice: 2026-06-03, 2026-06-08)**:
the Task Scheduler runs `%ROOT%\deploy\run_daily_laptop.cmd` — a copy
**outside** the git repo. The daily wrapper's own Step 0 git-pull copies
`scripts\*.py` to production but deliberately **does NOT copy itself** (a
.cmd can't overwrite the file it's executing). So a fix committed to the
wrapper sits in `hilmar-daily-routine\deploy\` and never reaches the copy
the scheduler runs. The June 2026 dead fires were a stale wrapper hanging
on a git credential prompt (fixed by `GIT_TERMINAL_PROMPT=0`, but the fix
couldn't deploy itself).

**Fix**: deploy the current wrappers, then verify the mtime advanced:
```
deploy\sync_now.cmd
(Get-Item "$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR\deploy\run_daily_laptop.cmd").LastWriteTime
```
`sync_now.cmd` runs as its own process (not the daily wrapper), so it CAN
overwrite the production `deploy\*.cmd`. This is the supported way to ship
a wrapper change. If `sync_now.cmd` itself isn't on disk yet, bootstrap once:
```
cd "$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR\hilmar-daily-routine"
git pull
xcopy /Y "deploy\*.cmd" "..\deploy\"
xcopy /Y "scripts\*.py" "..\scripts\"
```

**Verify the right Python**: the wrapper logs `PY: <path>` near the top of
each fire. It must be a real install with pytest+pytest-cov (the daily test
routine / QC-052 needs them). Confirm with `py -0p` and
`<PY> -m pip show pytest-cov`; install into that interpreter if missing.

---

## Failure mode: QC drift (status != CLEAN)

**Symptom**: `reports/qc-result.json` shows status WARN or ERROR. `qc_alert_if_needed.py` emails Michael.

**Triage**:
1. Open the audit email at idealx.us — Red Flags section lists the failing checks
2. Cross-reference `reports/QC-INDEX.md` for what each check guards
3. Severity matters:
   - **ERROR** = pipeline gates / blocked bad data ship — investigate before next fire
   - **WARN** = surfaced for review, didn't block — handle when convenient

**Common WARNs that are usually fine**:
- QC-002: 1-2 WINs missing carrier_won → usually a Hamburg-style edge case where booking was off-channel
- QC-014b: Q&L coverage at baseline (~48% pre-body-scan, ~100% post — usually post-fix)
- QC-027 PDF-only WINs: confirmed bookings with data only in attached PDF (Apr-dated, pre-cutoff)

**WARNs that need action**:
- QC-008 stage stale >36h: refresh_stage didn't run → check MSAL token
- QC-010 preserved-from-prior >10: refresh_stage Outlook query too narrow → investigate
- QC-022 distribution list bad: someone edited config.json poorly → restore from git

---

## Failure mode: Auto-chase sent unwanted email to Lonny

**Symptom**: Lonny received a "Quick check on Oakland → Yokohama" nudge that shouldn't have been sent.

**Disable immediately**:
1. Edit `config.json` → set `auto_chase.enabled: false`
2. Commit + push (or just save — Cloud PC picks up the OneDrive sync within minutes)
3. Or set `enabled: false` via Codespaces on any device

**Investigate**:
1. Check `reports/chase-sent-YYYY-MM-DD.flag` for what was sent
2. Find the offending row in `tracking-data-v2.json` — was its `response_timestamp` mis-calculated?

**Re-enable when fixed**: flip `enabled: true` again.

---

## Failure mode: backup_offline didn't write today

**Symptom**: QC-032 ERROR: "NO backup target is fresh".

**Fix**:
1. Manually fire backup: `python scripts/backup_offline.py`
2. Check both target directories exist + writable:
   - `%USERPROFILE%/OneDrive - IdealX/HILMAR_BACKUPS/`
   - `%USERPROFILE%/hilmar-local-backups/`
3. If OneDrive folder missing, OneDrive may be paused → resume sync from system tray

---

## Failure mode: GitHub Actions CI failing

**Symptom**: GitHub repo shows red X on latest commit.

**Triage**:
1. Open `https://github.com/IdealX-dev/hilmar-daily-routine/actions`
2. Click the failed run — see which step failed
3. Common: missing dependency, module import error, test failure

**Fix**: edit code via Codespaces, commit, push — CI re-runs automatically.

---

## Failure mode: GH Actions production-fire failed (post-cutover)

Applies once `HILMAR_FIRE_FROM_ACTIONS=true` (the fire runs from the
`Daily` workflow instead of the Cloud PC).

**Symptom**: `cloud-pc-down` issue filed by liveness at 11:30 AM ET, or a
red `production-fire` job in Actions → Daily.

**Triage**:
1. Open the failed run. The job is ordered so the failing step names the
   subsystem: *Verify prerequisites* (a secret is missing/rotated) →
   *Pull state* (Azure Blob unreachable / connection string expired) →
   *pipeline* (same triage as a Cloud PC fire — read the run-log artifact)
   → *Send* (Graph app-only auth: client secret expired? Application
   Access Policy changed?) → *heartbeat* (cosmetic; fire already shipped).
2. Artifacts on every run: `run-log.txt`, `qc-result.json`,
   `email-body.html`, `improvements-report.html` (14-day retention).

**Fix + re-fire**: Actions → Daily → Run workflow → `mode=production-fire`,
`send_to=full`. Safe to re-dispatch: today's send-flags live in the blob
store, so if the email already went out the send step refuses (idempotent).

**Common roots**:
- `GRAPH_APP_CLIENT_SECRET` expired (Entra client secrets have a max 24-mo
  lifetime) → mint a new one, `gh secret set GRAPH_APP_CLIENT_SECRET`
- `AZURE_STORAGE_CONNECTION_STRING` rotated → re-copy from Storage account
  → Access keys
- Schedule didn't trigger at all → check the repo variable
  `HILMAR_FIRE_FROM_ACTIONS` is still `true`, and the `gate` job's log
  (it skips the cron that doesn't match the current ET UTC-offset)

**Rollback to the Cloud PC**: unset `HILMAR_FIRE_FROM_ACTIONS`, re-enable
the `Hilmar Daily Tracker - CloudPC` scheduled task. State note: the Cloud
PC's OneDrive copy of `tracking-data-v2.json` will be stale by however many
days GH owned the fire — copy the latest from the blob container
(`hilmar-state`) or accept re-ingest from staged emails.

---

## Routine tasks

### Push freshly-merged code to production NOW (don't wait for the evening fire)
The daily wrapper auto-pulls `main` at the start of each 6:07 PM ET fire
(Step 0), so production self-updates every evening — you normally never
need to sync by hand. To go live *immediately* after a PR merges, run:
```
deploy\sync_now.cmd
```
Double-click it or run from a shell. It does git pull + xcopy scripts to
the live PROJECT HILMAR folder — **no pipeline, no email**. Exit 0 =
synced, 3 = git pull failed (auth/network, production left on prior code).
Replaces the old manual `cd … & git pull & xcopy …` dance.

### Re-auth MSAL (every ~80 days, before QC-023 errors)
```
python scripts/outlook_send.py auth
```

### Pull historical PDFs (when adding new analytics)
```
python scripts/refresh_stage.py --days-back 60 --pdf-backfill
```

### Manually fire weekly summary (any day)
```
python scripts/gen_weekly_summary.py --force
```

### Restore a backup
```
python scripts/backup_offline.py --restore 2026-05-10 --target /tmp/restored/
```

### Add a new recipient
1. Edit `config.json` → `distribution.full_list`
2. Save → OneDrive syncs to Cloud PC overnight
3. QC-022 ensures the list stays ≤12 recipients and only @ol-usa.com / @idealx.us

### Test the full pipeline before committing a change
```
python scripts/run_tests.py        # unit tests
python -m compileall scripts/      # syntax
python scripts/run_pipeline.py     # full end-to-end
python scripts/qc_selfheal.py      # post-run validation
```

---

## Architecture quick-reference

```
Cloud PC (CPC-micha-E552L, always on)
    │
    │ 6:07 PM ET weekday — Windows Task Scheduler
    ▼
deploy/run_daily_laptop.cmd
    ├── Step 0: git pull (sync Codespaces edits)
    ├── Step 1: refresh_stage.py (Microsoft Graph → stage_emails.txt + bodies + PDFs)
    ├── Step 2: run_pipeline.py
    │       ├── backup.py
    │       ├── ingest.py
    │       ├── drift_check.py
    │       ├── qc_selfheal.py (35 checks)
    │       ├── patch_carriers.py (auto-discovery + body-scan + PDF fallback)
    │       ├── qc_selfheal.py (post-patch)
    │       ├── gen_dashboard.py
    │       ├── gen_pdf.py
    │       ├── gen_carrier_scorecard_pdf.py
    │       ├── gen_email.py
    │       ├── share_intel.py (push to SHARED/client_intelligence/hilmar/)
    │       └── gen_rate_intelligence.py
    ├── Step 3: outlook_send.py daily → 10 recipients (idempotent via sent-YYYY-MM-DD.flag)
    ├── Step 4: qc_alert_if_needed.py
    ├── Step 4.5: teams_alert.py scan (queues until webhook configured)
    ├── Step 4.7: gen_weekly_summary.py (Fridays only)
    ├── Step 4.8: auto_chase_pending.py (config-gated, max 3/day, EOD)
    ├── Step 4.9: backup_offline.py (dual-target)
    └── Step 5: gen_improvements_report.py + outlook_send → idealx.us
```

---

## Standing rule (cross-project, lives in `~/.claude/CLAUDE.md`)

> Every new code pattern, integration, or feature gets a matching QC check
> + self-heal action in the SAME commit. Never ship a new pattern without
> its QC counterpart.

Active QC checks: 35 (QC-001 through QC-033 incl. sub-variants). See
`reports/QC-INDEX.md` for the full matrix.

---

## Contacts + escalation

- **Pipeline owner**: Michael Deitchman (michael.deitchman@idealx.us / michael.deitchman@ol-usa.com)
- **Hilmar contact**: Lonny Upfold (lupfold@hilmaringredients.com)
- **OL responder mailbox**: MBD_OceanExportBookingShared@ol-usa.com

For Cloud PC issues: OL IT / Win365 admin
For OneDrive corruption: restore from `backup_offline.py --restore YYYY-MM-DD`
For Graph API auth: re-run `outlook_send.py auth` interactively
