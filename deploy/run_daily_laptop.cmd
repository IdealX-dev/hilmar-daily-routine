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

REM Step 3 — send daily email to FULL distribution.
REM Flipped 2026-05-06 per Michael's explicit authorization after Cloud PC
REM setup proven (MSAL silent refresh + Graph send work from Cloud PC IP).
REM Cowork hilmar-rate-desk-daily is stopped. claude.ai routine is disabled
REM (blocked by OL CAP from Anthropic IPs). This wrapper is now the sole
REM producer of the daily Hilmar email — runs from Cloud PC at 10:00 AM
REM weekdays (always on), or from MBD-TRAVEL as a manual fallback.
REM
REM IDEMPOTENCY: if reports\sent-YYYY-MM-DD.flag exists, today's email
REM already went out (manual fire from MBD-TRAVEL or earlier Cloud PC fire).
REM Skip the send to prevent duplicate emails to 10 recipients. Flag is
REM written by this wrapper after a successful send.
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
set SENT_FLAG=%ROOT%\reports\sent-%TODAY%.flag
set IMPROV_FLAG=%ROOT%\reports\improvements-sent-%TODAY%.flag

REM Step 3 — daily email (idempotent — skip if today's flag exists)
if exist "%SENT_FLAG%" (
  echo --- outlook_send SKIPPED: today's email already sent --- >> "%LOG%"
  type "%SENT_FLAG%" >> "%LOG%"
) else (
  echo --- outlook_send (full distribution to 10 recipients) --- >> "%LOG%"
  "%PY%" scripts\outlook_send.py daily ^
    --to-from-config ^
    --subject-from-file reports\email-subject.txt ^
    --body-from-file reports\email-body.html ^
    --attach reports\hilmar-dashboard.html reports\hilmar-report.pdf ^
    >> "%LOG%" 2>&1
  set SEND_RC=!ERRORLEVEL!
  echo Send exit code: !SEND_RC! >> "%LOG%"
  if !SEND_RC! EQU 0 (
    echo Sent %DATE% %TIME% from %COMPUTERNAME% > "%SENT_FLAG%"
  )
)

REM Step 4 — alert Michael if QC drifted from CLEAN (always run)
"%PY%" deploy\qc_alert_if_needed.py >> "%LOG%" 2>&1

REM Step 5 — daily systems-audit report (idempotent, sent to idealx.us only)
REM Generated by gen_improvements_report.py: red flags + observations + suggestions.
REM Goes to Michael's IdealX inbox alone — NOT the full distribution.
if exist "%IMPROV_FLAG%" (
  echo --- improvements_report SKIPPED: today's audit already sent --- >> "%LOG%"
  type "%IMPROV_FLAG%" >> "%LOG%"
) else (
  echo --- improvements_report (Michael only, idealx.us) --- >> "%LOG%"
  "%PY%" scripts\gen_improvements_report.py >> "%LOG%" 2>&1
  set GEN_RC=!ERRORLEVEL!
  if !GEN_RC! EQU 0 (
    "%PY%" scripts\outlook_send.py daily ^
      --to michael.deitchman@idealx.us ^
      --subject-from-file reports\improvements-subject.txt ^
      --body-from-file reports\improvements-report.html ^
      >> "%LOG%" 2>&1
    set IMPROV_RC=!ERRORLEVEL!
    echo Improvements send exit code: !IMPROV_RC! >> "%LOG%"
    if !IMPROV_RC! EQU 0 (
      echo Improvements sent %DATE% %TIME% from %COMPUTERNAME% > "%IMPROV_FLAG%"
    )
  ) else (
    echo gen_improvements_report.py failed rc=!GEN_RC! >> "%LOG%"
  )
)

endlocal
exit /b 0
