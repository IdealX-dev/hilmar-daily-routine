@echo off
REM run_chase_evening.cmd — Standalone evening auto-chase task.
REM
REM Fired by Windows Task Scheduler at 16:30 ET weekdays. This is a
REM SEPARATE schedule from the 6:07 PM ET daily fire because
REM scripts/auto_chase_pending.py has an `earliest_send_hour_et: 16`
REM gate (chases must land in Lonny's late-afternoon PT, not at 7 AM
REM his time). When the fire was a 10 AM morning run, the wrapper's
REM call to the same script was a daily no-op — it always bailed at the
REM time gate. Discovered in the 2026-05-31 audit; this task was the fix.
REM
REM NOTE (2026-06-25): the daily fire moved from 10 AM to 6:07 PM ET, so
REM this 16:30 chase now runs ~1.5h BEFORE the fire — i.e. on the PRIOR
REM fire's data. Consider re-timing this task to ~18:45 ET so it chases on
REM fresh post-fire data:
REM   schtasks /Change /TN "Hilmar Auto-Chase - CloudPC" /ST 18:45
REM
REM Idempotency: auto_chase writes reports\chase-sent-YYYY-MM-DD.flag
REM with the request_ids of every chase sent today. If this task runs
REM twice on the same day (manual + scheduled), the second pass
REM dedupes per-request and sends nothing.
REM
REM Pre-requisites:
REM   - secrets\token-cache.json must be present (MSAL device flow done
REM     within the last 80 days; QC-023 warns + errors on freshness).
REM   - config.json auto_chase.enabled == true (defaults true post-audit).
REM
REM Cloud PC scheduled task setup (one-time, run as the user). Two
REM equivalent install lines — pick the shell you're actually in:
REM
REM   PowerShell (verified working 2026-05-31 on Cloud PC; ` escapes
REM   the inner double-quotes, $env: replaces %% env-var syntax):
REM     schtasks /Create /TN "Hilmar Auto-Chase Evening" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:30 /TR "`"$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR\deploy\run_chase_evening.cmd`""
REM
REM   cmd.exe (uses ^ line continuation + \" inner-quote escape):
REM     schtasks /Create /TN "Hilmar Auto-Chase Evening" /SC WEEKLY /D MON,TUE,WED,THU,FRI ^
REM       /ST 16:30 /TR "\"%USERPROFILE%\OneDrive - IdealX\claude\PROJECT HILMAR\deploy\run_chase_evening.cmd\""
REM
REM Verify after install (either shell):
REM   schtasks /Query /TN "Hilmar Auto-Chase Evening" /V /FO LIST | findstr /R "Task.To.Run Next.Run Status"

setlocal enableextensions enabledelayedexpansion
set ROOT=%USERPROFILE%\OneDrive - IdealX\claude\PROJECT HILMAR
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set LOG=%ROOT%\reports\run-log.txt

REM Python discovery — mirrors run_daily_laptop.cmd EXACTLY so the two can't
REM drift. Probe the PINNED `py -3.12` launcher FIRST, then list 3.12 paths
REM BEFORE any 3.14/3.13 fallback. The OLD order here listed 3.14 first and
REM took the first interpreter on disk, so a 3.14 install silently shadowed
REM 3.12 — the exact drift the main fire was hardened against on 2026-06-25,
REM and dangerous here because this task sends LIVE email to Lonny.
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
  echo CRITICAL: No Python interpreter found on %COMPUTERNAME% — aborting chase >> "%LOG%"
  endlocal
  exit /b 3
)

cd /d "%ROOT%"

REM PREFLIGHT — same env gate as the daily fire (run_daily_laptop.cmd Step 0.5).
REM The chase sends LIVE email to Lonny, so a drifted interpreter (rc=2) must
REM abort rather than chase on an unvalidated env. preflight raises its own
REM out-of-band alert (GitHub issue + Teams) independent of Outlook.
echo --- preflight_env (chase) --- >> "%LOG%"
"%PY%" scripts\preflight_env.py >> "%LOG%" 2>&1
if %ERRORLEVEL% EQU 2 (
  echo PREFLIGHT HARD-FAIL rc=2 — aborting chase ^(env drift; alert already raised^) >> "%LOG%"
  endlocal
  exit /b 2
)

echo === Hilmar evening chase %DATE% %TIME% === >> "%LOG%"
"%PY%" scripts\auto_chase_pending.py >> "%LOG%" 2>&1
set CHASE_RC=%ERRORLEVEL%
echo Chase exit code: %CHASE_RC% >> "%LOG%"
echo. >> "%LOG%"

exit /b %CHASE_RC%
