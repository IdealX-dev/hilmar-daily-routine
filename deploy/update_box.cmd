@echo off
REM update_box.cmd - one-command Cloud PC update (replaces the manual dance).
REM
REM Collapses the multi-step "git pull (xN) + pip install (xN) + copy + copy +
REM register task" sequence into a single idempotent run:
REM   1. git pull origin main  (ONCE - into the hilmar-daily-routine checkout)
REM   2. run the FRESHLY-PULLED setup_cloudpc.ps1, which:
REM        - verifies Python is the pinned 3.12
REM        - installs the COMPLETE dep set from requirements.txt (one install,
REM          against the just-pulled requirements.txt - no double install)
REM        - deploys scripts\*.py + deploy\*.py + wrapper + config to runtime
REM        - verifies MSAL silent refresh + smoke-tests refresh_stage
REM        - re-registers the 6:07 PM ET Task Scheduler entry
REM
REM Run after any code change merges to main, or whenever the box looks stale:
REM   deploy\update_box.cmd       (double-click, or run from a shell)
REM
REM Pull-THEN-run means a fix to setup_cloudpc.ps1 ITSELF lands on the same run
REM (the 2026-06-25 em-dash parse bug is exactly why that ordering matters).
REM Best-effort pull: an auth/network failure is non-fatal - setup still runs
REM on the existing checkout, same as the daily wrapper's Step 0.
REM
REM Does NOT fire the pipeline and does NOT send email. Safe to run any time.
REM
REM Exit codes: 0 = updated + task registered; 2 = no git checkout found;
REM             non-zero = setup_cloudpc.ps1 reported a failure (read its output).

setlocal enableextensions
set ROOT=%USERPROFILE%\OneDrive - IdealX\claude\PROJECT HILMAR
set REPO=%ROOT%\hilmar-daily-routine

REM Never hang on a git credential prompt (same guard as the daily wrapper).
set GIT_TERMINAL_PROMPT=0
set GCM_INTERACTIVE=Never
set GIT_ASKPASS=echo

if not exist "%REPO%\.git" (
  echo update_box: no git checkout at "%REPO%" - clone the repo there first.
  endlocal
  exit /b 2
)

echo === update_box: git pull origin main ===
pushd "%REPO%"
git pull origin main
if errorlevel 1 echo update_box: git pull FAILED ^(auth/network^) - running setup on the existing checkout.
popd

echo === update_box: running setup_cloudpc.ps1 ^(deps + deploy + register^) ===
powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO%\deploy\setup_cloudpc.ps1"
set RC=%ERRORLEVEL%
echo update_box: setup_cloudpc.ps1 exit code %RC%
endlocal & exit /b %RC%
