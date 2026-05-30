@echo off
REM run_daily_laptop.cmd — Pinned Hilmar daily run for Cloud PC + MBD-TRAVEL.
REM
REM Fired by Windows Task Scheduler at 10:00 AM ET weekdays.
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
set LOG=%ROOT%\reports\run-log.txt

REM Python discovery: try several candidate paths, pick the first that exists.
REM Required because each Windows machine installs Python in different paths
REM (per-user vs system, py launcher vs explicit). Cloud PC fire 2026-05-07
REM 10:00 ET failed rc=3 because the hardcoded MBD-TRAVEL path didn't exist
REM on Cloud PC. This loop fixes that.
set PY=
for %%P in (
  "%USERPROFILE%\AppData\Local\Python\pythoncore-3.14-64\python.exe"
  "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
  "%USERPROFILE%\AppData\Local\Programs\Python\Python314\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
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
  if exist "%ROOT%\hilmar-daily-routine\config.json" (
    xcopy /Y /Q "%ROOT%\hilmar-daily-routine\config.json" "%ROOT%\" >> "%LOG%" 2>&1
  )
) else (
  echo git pull SKIPPED: no .git in %ROOT%\hilmar-daily-routine >> "%LOG%"
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
  echo PIPELINE FAILED rc=%RC% >> "%LOG%"
  exit /b %RC%
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
echo Send exit code: %ERRORLEVEL% >> "%LOG%"

REM Step 4 — alert Michael if QC drifted from CLEAN (always run)
"%PY%" deploy\qc_alert_if_needed.py >> "%LOG%" 2>&1

REM Step 4.5 — real-time Teams/Slack alerts (WIN, pending overdue, QC error,
REM big-day). No-op if webhook not configured in config.json.
echo --- teams_alerts --- >> "%LOG%"
"%PY%" scripts\teams_alert.py scan >> "%LOG%" 2>&1

REM Step 4.7 — weekly executive summary (Friday only — script self-gates)
echo --- weekly_summary --- >> "%LOG%"
"%PY%" scripts\gen_weekly_summary.py >> "%LOG%" 2>&1

REM Step 4.8 — pending auto-chase (only fires if config.auto_chase.enabled
REM == true AND it's past earliest_send_hour_et). No-op by default.
echo --- auto_chase --- >> "%LOG%"
"%PY%" scripts\auto_chase_pending.py >> "%LOG%" 2>&1

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

endlocal
exit /b 0
