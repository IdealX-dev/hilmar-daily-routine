@echo off
REM sync_now.cmd — pull latest main + copy scripts to production, NO fire.
REM
REM Use this when you want the Cloud PC / laptop running the newest merged
REM code RIGHT NOW instead of waiting for the next 10:00 AM ET scheduled
REM fire. It does ONLY what Step 0 of run_daily_laptop.cmd does:
REM   1. git pull origin main  (into the hilmar-daily-routine checkout)
REM   2. write reports\deployment-sha.txt  (HEAD + commits-behind for QC-053)
REM   3. xcopy scripts\*.py + config.json into the live PROJECT HILMAR folder
REM
REM It does NOT run the pipeline, does NOT send any email. Safe to run any
REM time, as many times as you like. Double-click it, or run from a shell.
REM
REM Replaces the manual 3-command dance:
REM   cd ...\hilmar-daily-routine & git pull & xcopy scripts ...
REM
REM Exit codes:
REM   0  synced (or already current)
REM   2  no .git checkout found under PROJECT HILMAR\hilmar-daily-routine
REM   3  git pull failed (auth / network) — production left on prior code

setlocal enableextensions enabledelayedexpansion
set ROOT=%USERPROFILE%\OneDrive - IdealX\claude\PROJECT HILMAR
set REPO=%ROOT%\hilmar-daily-routine
set LOG=%ROOT%\reports\run-log.txt

REM Same anti-hang guard as the daily wrapper — never block on a credential
REM prompt. An auth failure returns in ~1s instead of hanging.
set GIT_TERMINAL_PROMPT=0
set GCM_INTERACTIVE=Never
set GIT_ASKPASS=echo

if not exist "%ROOT%\reports" mkdir "%ROOT%\reports"

echo. >> "%LOG%"
echo ================================================================ >> "%LOG%"
echo sync_now on %COMPUTERNAME% — %DATE% %TIME% >> "%LOG%"
echo ================================================================ >> "%LOG%"

if not exist "%REPO%\.git" (
  echo SYNC_NOW: no .git at %REPO% — clone the repo there first. >> "%LOG%"
  echo SYNC_NOW: no .git at %REPO% — clone the repo there first.
  endlocal
  exit /b 2
)

echo --- sync_now: git pull --- >> "%LOG%"
pushd "%REPO%"
git pull origin main >> "%LOG%" 2>&1
set PULL_RC=!ERRORLEVEL!

REM Deployment marker for QC-053 — HEAD SHA + how far behind origin/main.
for /f "delims=" %%S in ('git rev-parse --short HEAD') do set HEAD_SHA=%%S
for /f "delims=" %%B in ('git rev-list --count HEAD..origin/main 2^>nul') do set BEHIND=%%B
if not defined BEHIND set BEHIND=?
echo HEAD=!HEAD_SHA! BEHIND=!BEHIND! AT=%DATE% %TIME% > "%ROOT%\reports\deployment-sha.txt"
echo deployment-sha.txt: HEAD=!HEAD_SHA! BEHIND=!BEHIND! >> "%LOG%"
popd

if not "!PULL_RC!"=="0" (
  echo SYNC_NOW: git pull FAILED rc=!PULL_RC! — production left on prior code. >> "%LOG%"
  echo SYNC_NOW: git pull FAILED rc=!PULL_RC! ^(auth/network^). Production NOT updated.
  endlocal
  exit /b 3
)

echo --- sync_now: xcopy scripts + config --- >> "%LOG%"
xcopy /Y /Q "%REPO%\scripts\*.py" "%ROOT%\scripts\" >> "%LOG%" 2>&1
if exist "%REPO%\config.json" (
  xcopy /Y /Q "%REPO%\config.json" "%ROOT%\" >> "%LOG%" 2>&1
)

echo SYNC_NOW: done — HEAD=!HEAD_SHA! BEHIND=!BEHIND! >> "%LOG%"
echo SYNC_NOW: done. Production now at HEAD=!HEAD_SHA! ^(BEHIND=!BEHIND!^).
echo The next 10 AM ET fire will use this code. No email was sent.
endlocal
exit /b 0
