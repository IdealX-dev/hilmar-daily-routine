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

REM Step 5 — daily systems-audit report (idempotent at script level, idealx.us only)
echo --- improvements_report (Michael only) --- >> "%LOG%"
"%PY%" scripts\gen_improvements_report.py >> "%LOG%" 2>&1
"%PY%" scripts\outlook_send.py daily --to michael.deitchman@idealx.us ^
  --subject-from-file reports\improvements-subject.txt ^
  --body-from-file reports\improvements-report.html >> "%LOG%" 2>&1
echo Improvements send exit code: %ERRORLEVEL% >> "%LOG%"

endlocal
exit /b 0
