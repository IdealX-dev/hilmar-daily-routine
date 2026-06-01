@echo off
REM run_chase_evening.cmd — Standalone evening auto-chase task.
REM
REM Fired by Windows Task Scheduler at 16:30 ET weekdays. This is a
REM SEPARATE schedule from the 10:00 ET daily fire because
REM scripts/auto_chase_pending.py has an `earliest_send_hour_et: 16`
REM gate (chases must land in Lonny's late-afternoon PT, not at 7 AM
REM his time). The morning wrapper's call to the same script was a
REM daily no-op — it always bailed at the time gate. Discovered in
REM the 2026-05-31 audit; this evening task is the fix.
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

REM Python discovery — same probe order as run_daily_laptop.cmd.
set PY=
for %%P in (
  "%USERPROFILE%\AppData\Local\Python\pythoncore-3.14-64\python.exe"
  "%USERPROFILE%\AppData\Local\Programs\Python\Python313\python.exe"
  "%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe"
  "%USERPROFILE%\AppData\Local\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "C:\Program Files\Python313\python.exe"
  "C:\Program Files\Python312\python.exe"
  "C:\Program Files\Python311\python.exe"
) do (
  if exist %%P (
    set PY=%%P
    goto :py_found
  )
)
echo ERROR: No Python interpreter found.>> "%LOG%"
exit /b 3
:py_found

cd /d "%ROOT%"

echo === Hilmar evening chase %DATE% %TIME% === >> "%LOG%"
"%PY%" scripts\auto_chase_pending.py >> "%LOG%" 2>&1
set CHASE_RC=%ERRORLEVEL%
echo Chase exit code: %CHASE_RC% >> "%LOG%"
echo. >> "%LOG%"

exit /b %CHASE_RC%
