@echo off
REM run_daily_laptop.cmd — Pinned Hilmar daily run for Cloud PC + MBD-TRAVEL.
REM
REM Fired by Windows Task Scheduler at 6:07 PM ET weekdays (aligned to the
REM Sentry cron monitor + daily.yml/liveness.yml schedules; 6 PM ET = 3 PM PT,
REM after Lonny's Pacific workday so the report captures the current PT day).
REM
REM Daily flow:
REM   1. refresh_stage.py — pull new Lonny↔OL emails + HILMAR booking
REM      confirmations from Outlook via Microsoft Graph (silent MSAL refresh).
REM   2. run_pipeline.py — ingest → drift_check → QC → carrier patch → QC →
REM      dashboard → PDF → carrier scorecards → email body. Ingest is ADDITIVE
REM      (preserves prior wins missing from fresh stage); drift_check audits
REM      data quality; QC self-heals known issues.
REM   3. outlook_send.py daily — full distribution (10 recipients including
REM      michael.deitchman@idealx.us). Idempotent: skipped if today's flag
REM      already exists.
REM   4. qc_alert_if_needed.py — emails Michael if QC drifts from CLEAN.
REM   5. gen_improvements_report.py + outlook_send.py — daily systems-audit
REM      report sent ONLY to michael.deitchman@idealx.us (not full distribution).
REM      Per Michael 2026-05-07: "lock this in for qc and quality checks etc
REM      daily self healing and i want a report daily on any system improvements
REM      you think would be useful to michael.deitchman@idealx.us"
REM
REM Logs append to reports/run-log.txt — purge manually as needed.

setlocal enableextensions enabledelayedexpansion
REM ROOT and PY now resolved dynamically so this wrapper works on BOTH
REM MBD-TRAVEL and Cloud PC (different user profiles, same OneDrive folder).
set ROOT=%USERPROFILE%\OneDrive - IdealX\claude\PROJECT HILMAR
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
REM Fail FAST + LOUD on a stale MSAL token instead of hanging on an unanswered
REM interactive device-code prompt until Task Scheduler kills the job (a silent
REM stop). outlook_send honors this; GH Actions already sets it.
set HILMAR_NONINTERACTIVE=1
set LOG=%ROOT%\reports\run-log.txt

REM Never let git block the daily fire on an interactive credential prompt.
REM Root cause of the 2026-06-03 dead fire (QC-021 "wrapper started but
REM pipeline never completed — died before the refresh_stage echo"): the
REM Step 0 `git pull` hit an auth/credential prompt and hung until Task
REM Scheduler killed the wrapper — so NO email shipped. These env vars
REM convert a credential prompt into an immediate non-zero exit, which the
REM unguarded pull tolerates (control flow falls through to refresh_stage).
REM Deploy must never be able to take down the customer email.
set GIT_TERMINAL_PROMPT=0
set GCM_INTERACTIVE=Never
set GIT_ASKPASS=echo

REM Python discovery: try several candidate paths, pick the first that exists.
REM Required because each Windows machine installs Python in different paths
REM (per-user vs system, py launcher vs explicit). Cloud PC fire 2026-05-07
REM 10:00 ET failed rc=3 because the hardcoded MBD-TRAVEL path didn't exist
REM on Cloud PC. This loop fixes that.
REM
REM 2026-06-25: PREFER THE PINNED interpreter (.python-version = 3.12). The box
REM silently drifted to 3.14 BECAUSE this loop listed C:\Python314 first and
REM took the first one on disk — so a 3.14 install always shadowed a 3.12 one
REM and the preflight (QC-061) then aborted the fire. With 3.12 paths first,
REM installing Python 3.12 is SUFFICIENT — no need to uninstall 3.14, which can
REM stay as a never-selected fallback. The `py -3.12` launcher resolves the
REM exact pinned build first so even a non-standard install path is found.
REM (Earlier note, 2026-06-08: the bare C:\PythonNNN system installs are the
REM ones with pytest deps; routing through py.exe obscured which ran.)
set PY=
for /f "delims=" %%E in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set PY=%%E
if not defined PY for %%P in (
  "C:\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe"
  "C:\Python314\python.exe"
  "C:\Python313\python.exe"
  "%USERPROFILE%\AppData\Local\Python\pythoncore-3.14-64\python.exe"
  "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
  "%USERPROFILE%\AppData\Local\Programs\Python\Python314\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "C:\Windows\py.exe"
) do (
  if not defined PY if exist "%%~P" set PY=%%~P
)
if not defined PY (
  echo. >> "%LOG%"
  echo CRITICAL: No Python interpreter found on %COMPUTERNAME% — aborting >> "%LOG%"
  endlocal
  exit /b 99
)

cd /d "%ROOT%"

echo. >> "%LOG%"
echo ================================================================ >> "%LOG%"
echo Hilmar daily on %COMPUTERNAME% — %DATE% %TIME% >> "%LOG%"
echo   ROOT: %ROOT% >> "%LOG%"
echo   PY:   %PY% >> "%LOG%"
echo ================================================================ >> "%LOG%"

REM Step 0 — pull latest scripts from GitHub.
REM Added 2026-05-13 per Michael "i need this to become a remote app as well
REM so i can use code from my phone and other laptops". Edits made in
REM GitHub Codespaces (browser-based VS Code from phone/any device) commit
REM to the IdealX-dev/hilmar-daily-routine repo. This step pulls the latest
REM version into the OneDrive folder so tomorrow's pipeline runs the new
REM code. Pulls are silent — no log spam if there's nothing to update.
echo --- git_pull --- >> "%LOG%"
if exist "%ROOT%\hilmar-daily-routine\.git" (
  pushd "%ROOT%\hilmar-daily-routine"
  git pull --quiet origin main >> "%LOG%" 2>&1
  REM Surface a pull failure loudly but DON'T bail — a stale checkout still
  REM produces a (slightly old) email, which beats no email at all. The
  REM deployment-sha.txt below + QC-053 will report BEHIND>0 so the audit
  REM flags the staleness. With GIT_TERMINAL_PROMPT=0 set above, an auth
  REM failure returns here in ~1s instead of hanging the whole fire.
  if errorlevel 1 (
    echo GIT_PULL FAILED rc=!ERRORLEVEL! — running on existing checkout ^(see QC-053 BEHIND^) >> "%LOG%"
  )
  REM Deployment marker for QC-053 (added 2026-05-28 after a feature
  REM branch sat unmerged for 5 days while the daily audit reported the
  REM SAME issues every morning). Captures HEAD SHA + how many commits
  REM behind origin/main this checkout currently is, so the production
  REM xcopy of scripts/ (which has no .git nearby) can still verify it
  REM matches what's on main.
  if not exist "%ROOT%\reports" mkdir "%ROOT%\reports"
  for /f "delims=" %%S in ('git rev-parse --short HEAD') do set HEAD_SHA=%%S
  for /f "delims=" %%B in ('git rev-list --count HEAD..origin/main') do set BEHIND=%%B
  echo HEAD=!HEAD_SHA! BEHIND=!BEHIND! AT=%DATE% %TIME% > "%ROOT%\reports\deployment-sha.txt"
  echo deployment-sha.txt: HEAD=!HEAD_SHA! BEHIND=!BEHIND! >> "%LOG%"
  popd
  REM Copy any updated scripts + wrapper from the repo to the live OneDrive
  REM locations the pipeline reads. xcopy /Y overwrites without prompting,
  REM /Q is quiet. /D:m-d-y is omitted so all files copy (safe — git pull
  REM already filtered to only-changed). EXCLUDES the wrapper itself
  REM (we're running it right now — replacing in-flight is dangerous).
  xcopy /Y /Q "%ROOT%\hilmar-daily-routine\scripts\*.py" "%ROOT%\scripts\" >> "%LOG%" 2>&1
  REM Also sync deploy\*.py (e.g. assert_fire_integrity.py — the final gate).
  REM These live in deploy/, not scripts/, so the scripts xcopy above misses
  REM them; without this a pull updates the checkout but the fire still runs
  REM the stale asserter (2026-06-25: the manual-copy footgun).
  if exist "%ROOT%\deploy" xcopy /Y /Q "%ROOT%\hilmar-daily-routine\deploy\*.py" "%ROOT%\deploy\" >> "%LOG%" 2>&1
  REM Also sync src\hilmar\*.py. Production runs scripts/, but scripts/qc_selfheal.py
  REM imports hilmar.parser_accuracy for the QC-039 parser-accuracy gate (and
  REM hilmar.core/body_parser for QC-040/041). Without this the box has no
  REM src\hilmar\ on the path -> "No module named 'hilmar'" -> the gate cannot
  REM evaluate. /I tells xcopy the destination is a directory so it creates it
  REM on a fresh box instead of prompting.
  if exist "%ROOT%\hilmar-daily-routine\src\hilmar" xcopy /Y /Q /I "%ROOT%\hilmar-daily-routine\src\hilmar\*.py" "%ROOT%\src\hilmar\" >> "%LOG%" 2>&1
  if exist "%ROOT%\hilmar-daily-routine\config.json" (
    xcopy /Y /Q "%ROOT%\hilmar-daily-routine\config.json" "%ROOT%\" >> "%LOG%" 2>&1
  )
) else (
  echo git pull SKIPPED: no .git in %ROOT%\hilmar-daily-routine >> "%LOG%"
)

REM Step 0.5 — PREFLIGHT: verify the box BEFORE building anything. Hard-fails
REM (rc=2) on interpreter drift (wrong Python vs .python-version) and aborts the
REM fire LOUDLY rather than build a degraded report on an unvalidated
REM interpreter — the 2026-06 silent-week root cause. It raises an out-of-band
REM alert (GitHub issue + Teams) on its own, independent of Outlook.
echo --- preflight_env --- >> "%LOG%"
"%PY%" scripts\preflight_env.py >> "%LOG%" 2>&1
if %ERRORLEVEL% EQU 2 (
  echo PREFLIGHT HARD-FAIL rc=2 — aborting fire ^(env drift; alert already raised^) >> "%LOG%"
  endlocal
  exit /b 2
)

REM Step 1 — refresh stage from Outlook
echo --- refresh_stage --- >> "%LOG%"
"%PY%" scripts\refresh_stage.py --days-back 14 >> "%LOG%" 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo REFRESH_STAGE FAILED rc=%ERRORLEVEL% >> "%LOG%"
  REM Don't bail — pipeline can still produce artifacts on stale stage with
  REM the additive ingest merge protecting prior wins. Carry on.
)

REM Step 2 — full pipeline (ingest enabled, additive merge active)
echo --- run_pipeline --- >> "%LOG%"
"%PY%" scripts\run_pipeline.py >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo Pipeline exit code: %RC% >> "%LOG%"
if %RC% NEQ 0 (
  REM Do NOT exit here. The old `exit /b %RC%` terminated before the
  REM prove-or-scream gate + heartbeat ever ran, so the DOMINANT failure mode
  REM (a client-blocking pipeline step failing) produced NO out-of-band alarm
  REM and NO heartbeat — it stayed silent until liveness's 25h staleness check.
  REM Instead jump straight to the integrity gate (skipping the client send +
  REM diagnostic steps below, so no broken/stale email ships) so it screams
  REM with the real rc and the heartbeat reports status=failed.
  echo PIPELINE FAILED rc=%RC% — skipping client send; jumping to integrity gate so the alarm + heartbeat fire >> "%LOG%"
  goto integrity
)

REM Step 3 — send daily email. outlook_send.py daily has BUILT-IN idempotency:
REM it checks reports\sent-YYYY-MM-DD.flag at script entry and refuses to send
REM if today's flag exists (writes its own flag on success). So this wrapper
REM no longer needs flag-check logic — just call the script. If today's email
REM already shipped (manual fire, earlier Cloud PC fire), the script exits 0
REM with an informative message; no dupe goes out. Simplified 2026-05-08
REM after the prior wrapper exited 255 due to delayed-expansion IF/ELSE bug.
echo --- outlook_send (full distribution) --- >> "%LOG%"
"%PY%" scripts\outlook_send.py daily --to-from-config ^
  --subject-from-file reports\email-subject.txt ^
  --body-from-file reports\email-body.html ^
  --attach reports\hilmar-dashboard.html reports\hilmar-report.pdf >> "%LOG%" 2>&1
set SEND_RC=%ERRORLEVEL%
echo Send exit code: %SEND_RC% >> "%LOG%"

REM Step 4 — alert Michael if QC drifted from CLEAN (always run)
"%PY%" deploy\qc_alert_if_needed.py >> "%LOG%" 2>&1

REM Step 4.5 — real-time Teams/Slack alerts (WIN, pending overdue, QC error,
REM big-day). No-op if webhook not configured in config.json.
echo --- teams_alerts --- >> "%LOG%"
"%PY%" scripts\teams_alert.py scan >> "%LOG%" 2>&1

REM Step 4.7 — weekly executive summary (Friday only — script self-gates)
echo --- weekly_summary --- >> "%LOG%"
"%PY%" scripts\gen_weekly_summary.py >> "%LOG%" 2>&1

REM Step 4.8 — auto-chase MOVED to deploy/run_chase_evening.cmd (2026-05-31).
REM The morning fire was always a no-op because auto_chase requires the
REM current ET hour to be >= earliest_send_hour_et (16). Set up a separate
REM Cloud PC scheduled task at 16:30 ET to fire run_chase_evening.cmd.
REM See that file's header for the schtasks one-liner.
echo --- auto_chase scheduled separately (run_chase_evening.cmd at 16:30 ET) --- >> "%LOG%"

REM Step 4.9 — dual-target offline backup (separate OneDrive folder + local
REM offline folder). Defense in depth against OneDrive corruption / accidental
REM delete to PROJECT HILMAR/. Idempotent; rotates older than retention_days.
echo --- backup_offline --- >> "%LOG%"
"%PY%" scripts\backup_offline.py >> "%LOG%" 2>&1

REM Step 5 — daily systems-audit report (idempotent at script level, idealx.us only)
echo --- improvements_report (Michael only) --- >> "%LOG%"
"%PY%" scripts\gen_improvements_report.py >> "%LOG%" 2>&1
"%PY%" scripts\outlook_send.py daily --to michael.deitchman@idealx.us ^
  --subject-from-file reports\improvements-subject.txt ^
  --body-from-file reports\improvements-report.html >> "%LOG%" 2>&1
echo Improvements send exit code: %ERRORLEVEL% >> "%LOG%"

REM ── Integrity gate + heartbeat. Reached by fall-through on a clean fire, or
REM    by `goto integrity` from the pipeline-failure branch above (which skips
REM    the client send + diagnostic steps so no broken email ships). Either
REM    way the prove-or-scream gate + heartbeat ALWAYS run.
:integrity
REM Step 6 — PROVE the fire shipped a report (or scream). Mandatory final
REM gate: asserts pipeline rc==0 + fresh artifacts + today's send-flag + token
REM cache. On ANY violation it raises an OUT-OF-BAND alarm (GitHub issue +
REM Teams + queue — never Outlook) and exits non-zero. We capture that into
REM FIRE_STATUS and pass the REAL status to the heartbeat, so liveness sees the
REM truth instead of a hardcoded "success" (the 2026-06 silent-week root cause).
echo --- assert_fire_integrity --- >> "%LOG%"
"%PY%" deploy\assert_fire_integrity.py --pipeline-rc %RC% >> "%LOG%" 2>&1
if %ERRORLEVEL% EQU 0 ( set FIRE_STATUS=success ) else ( set FIRE_STATUS=failed )
echo Fire integrity: %FIRE_STATUS% >> "%LOG%"

REM ISO-8601 UTC timestamp for the heartbeat, matching daily.yml's
REM `date -u +%Y-%m-%dT%H:%M:%SZ`. The old `set HB_AT=%DATE%T%TIME%` produced a
REM locale-formatted, unparseable value (spaces/slashes/weekday) that diverged
REM from the GH-Actions heartbeat feeding the SAME heartbeat.yml input. Compute
REM via Python to a temp file (no strftime %-escaping / for-loop quote traps),
REM falling back to the locale string only if Python is somehow unavailable.
"%PY%" -c "import datetime;print(datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z'),end='')" > "%ROOT%\reports\hb-at.txt" 2>nul
set HB_AT=%DATE%T%TIME%
if exist "%ROOT%\reports\hb-at.txt" set /p HB_AT=<"%ROOT%\reports\hb-at.txt"

REM Step 7 — GitHub Actions heartbeat (2026-06-01 — replaces the missing
REM daily-fire.yml self-hosted runner trigger). Tells the liveness monitor
REM workflow that the daily fire actually completed. Best-effort: if gh
REM CLI isn't installed, the PAT isn't configured, or github.com is
REM unreachable, this logs the failure and exits 0 — the email already
REM went out, and the liveness workflow filing an alert is a tolerable
REM secondary failure.
REM
REM One-time setup on Cloud PC:
REM   1. Install gh CLI: winget install --id GitHub.cli
REM   2. Authenticate ONCE as the operator:
REM        gh auth login --hostname github.com --git-protocol https --web
REM      OR drop a PAT (scope: actions:write) into secrets\github-pat.txt
REM      and run: gh auth login --with-token < secrets\github-pat.txt
REM   3. Verify: gh auth status
REM See docs\CLOUD-PC-HEARTBEAT-SETUP.md for the full walkthrough.
echo --- heartbeat dispatch --- >> "%LOG%"
where gh >nul 2>&1
if errorlevel 1 (
  echo gh CLI not found; heartbeat skipped ^(install: winget install GitHub.cli^) >> "%LOG%"
) else (
  REM Pass HB_AT (ISO-8601, computed above) + repo HEAD so the heartbeat
  REM workflow's run log ties back to a specific commit. Status is the REAL
  REM outcome from the integrity assertion above — heartbeat.yml fails its job
  REM on status!=success so liveness's failed branch actually fires.
  set HB_SHA=
  if exist "%ROOT%\reports\deployment-sha.txt" (
    for /f "tokens=2 delims= " %%S in ('findstr /b "HEAD=" "%ROOT%\reports\deployment-sha.txt" 2^>nul') do (
      set HB_SHA=%%S
      set HB_SHA=!HB_SHA:HEAD=!
    )
  )
  REM Forward the box's ENVIRONMENT FINGERPRINT (interpreter + dep health) that
  REM preflight stamped. heartbeat.yml — running on GitHub, independent of this
  REM box — compares it to the pinned .python-version and pages the operator
  REM when a fire shipped on a DRIFTED env (the proactive sentinel). Default
  REM signals "unknown" if preflight didn't write it.
  set HB_ENV=unknown
  if exist "%ROOT%\reports\env-fingerprint.txt" set /p HB_ENV=<"%ROOT%\reports\env-fingerprint.txt"
  gh workflow run heartbeat.yml ^
    -R IdealX-dev/hilmar-daily-routine ^
    -f at="!HB_AT!" ^
    -f sha="!HB_SHA!" ^
    -f status="!FIRE_STATUS!" ^
    -f host="cloud-pc" ^
    -f env="!HB_ENV!" >> "%LOG%" 2>&1
  echo Heartbeat dispatch exit code: !ERRORLEVEL! >> "%LOG%"
)

REM Surface the fire outcome as the wrapper's exit code so Windows Task
REM Scheduler's Last-Run-Result reflects a failed fire (was an unconditional 0).
REM FIRE_STATUS dies with endlocal, so compute EXITRC first, then return it via
REM the `endlocal & exit` parse-time-expansion trick.
REM NOTE: the DEPLOYED wrapper itself (this file) is intentionally NOT
REM self-overwritten — replacing a .cmd while cmd.exe is still reading it by
REM byte-offset is undefined behavior. A wrapper change reaches the box by
REM re-running deploy\setup_cloudpc.ps1 (which copies it safely while it's not
REM executing). deploy\*.py + scripts\*.py DO auto-sync above, so only an edit
REM to THIS file needs the setup re-run. QC-026 flags the drift if it happens.
if "!FIRE_STATUS!"=="failed" ( set EXITRC=1 ) else ( set EXITRC=0 )
endlocal & exit /b %EXITRC%
