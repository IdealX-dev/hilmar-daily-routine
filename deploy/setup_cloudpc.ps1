# Hilmar daily - one-shot setup on the Windows 365 Cloud PC.
#
# Purpose: move the scheduler off Michael's physical laptop (MBD-TRAVEL,
# closes lid often, sleeps) onto the always-on Win365 Cloud PC.
# Cloud PC is on OL's allowlist, OneDrive-synced, and stays on 24/7
# with no WakeToRun gymnastics.
#
# How to run: RDP into the Cloud PC, open PowerShell, run:
#   & "$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR\deploy\setup_cloudpc.ps1"
#
# Idempotent. Safe to re-run.

$ErrorActionPreference = "Stop"
$ROOT = "$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR"

Write-Host "=== Hilmar Cloud PC scheduler setup ===" -ForegroundColor Cyan
Write-Host "ROOT: $ROOT"
Write-Host ""

# Step 1 - verify OneDrive sync
Write-Host "[1/6] Verifying OneDrive sync of PROJECT HILMAR folder..." -ForegroundColor Yellow
if (-not (Test-Path "$ROOT\config.json")) {
    Write-Error "ROOT not found: $ROOT. Wait for OneDrive sync, or fix the path."
    exit 1
}
foreach ($f in @(
    "scripts\run_pipeline.py",
    "scripts\refresh_stage.py",
    "scripts\outlook_send.py",
    "config.json",
    "secrets\token-cache.json",
    "scripts\stage_emails.txt",
    "scripts\stage_emails_bodies.txt",
    "tracking-data-v2.json"
)) {
    if (-not (Test-Path "$ROOT\$f")) {
        Write-Warning "Missing: $f (OneDrive may still be syncing)"
    } else {
        Write-Host "  OK  $f" -ForegroundColor Green
    }
}

# Step 2 - verify Python is EXACTLY the pinned version (.python-version).
# The box silently drifted to 3.14 (untested; CI is 3.12) because this step
# accepted any 3.11+ AND winget-installed 3.14. We now pin to the ONE version
# the whole toolchain validates against, read from the repo's .python-version.
Write-Host ""
$PINNED = (Get-Content (Join-Path $PSScriptRoot "..\.python-version") -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $PINNED) { $PINNED = "3.12" }
$PINNED = $PINNED.Trim()
Write-Host "[2/6] Verifying Python == $PINNED (pinned)..." -ForegroundColor Yellow
$pythonOK = $false
$script:PYTHON_CMD = $null
$pinEsc = [regex]::Escape($PINNED)
foreach ($cmd in @("python", "py", "python3")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python $pinEsc(\.\d+)?$") {
            Write-Host "  OK  $cmd -> $ver" -ForegroundColor Green
            $script:PYTHON_CMD = (Get-Command $cmd).Source
            $pythonOK = $true
            break
        }
    } catch {}
}
if (-not $pythonOK) {
    Write-Host "  Python $PINNED not found. Installing via winget..." -ForegroundColor Yellow
    winget install --id "Python.Python.$PINNED" --accept-source-agreements --accept-package-agreements --silent
    $script:PYTHON_CMD = "python"
    Write-Host "  After install, RE-OPEN the shell and re-run so the wrapper picks up $PINNED." -ForegroundColor Yellow
}

# Step 3 - install the COMPLETE runtime dependency set from requirements.txt.
# (Was a hand-typed 4-package line that omitted jinja2/jsonschema/dateutil/
# sentry-sdk — the box ran for a week missing them. requirements.txt is now the
# canonical list QC-054 verifies + QC-060 reconciles, so this can't drift.)
Write-Host ""
Write-Host "[3/6] Installing runtime deps from requirements.txt..." -ForegroundColor Yellow
& $script:PYTHON_CMD -m pip install --user --quiet -r (Join-Path $PSScriptRoot "..\requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }
Write-Host "  OK" -ForegroundColor Green

# Step 4 - verify MSAL silent refresh works on this Cloud PC
# This is the make-or-break test: if the Cloud PC's IP is on OL's
# Conditional Access allowlist, silent refresh + Graph calls will
# both work, and we are clear to proceed.
Write-Host ""
Write-Host "[4/6] Verifying MSAL silent-token refresh..." -ForegroundColor Yellow
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
Push-Location $ROOT
$probeCode = @"
import sys
sys.path.insert(0, 'scripts')
import outlook_send as O
import msal
cache = O._load_cache()
app = msal.PublicClientApplication(O.CLIENT_ID, authority=f'https://login.microsoftonline.com/{O.TENANT}', token_cache=cache)
accts = app.get_accounts()
if not accts:
    print('NO_ACCOUNTS'); sys.exit(2)
r = app.acquire_token_silent(O.SCOPES, account=accts[0])
if r and 'access_token' in r:
    print('SILENT_OK'); sys.exit(0)
else:
    print('SILENT_FAIL'); sys.exit(1)
"@
$probe = & $script:PYTHON_CMD -c $probeCode
Pop-Location
if ($probe -match "SILENT_OK") {
    Write-Host "  OK  silent refresh succeeded" -ForegroundColor Green
} elseif ($probe -match "SILENT_FAIL") {
    Write-Error "Silent refresh failed - token cache stale. Run on MBD-TRAVEL: python scripts/outlook_send.py auth (interactive), then re-run this on the Cloud PC."
    exit 1
} else {
    Write-Error ("MSAL probe error: " + $probe)
    exit 1
}

# Step 5 - smoke test refresh_stage --dry
Write-Host ""
Write-Host "[5/6] Smoke test: refresh_stage --dry --days-back 1..." -ForegroundColor Yellow
Push-Location $ROOT
& $script:PYTHON_CMD scripts\refresh_stage.py --dry --days-back 1 2>&1 | Select-String -Pattern "NEW staged|skipped|cutoff|fetched" | ForEach-Object { Write-Host "  $_" }
Pop-Location
Write-Host "  OK" -ForegroundColor Green

# Step 6 - register Task Scheduler entry on the Cloud PC
Write-Host ""
Write-Host "[6/6] Registering Task Scheduler: 'Hilmar Daily Tracker - CloudPC'..." -ForegroundColor Yellow
$wrapperPath = "$ROOT\deploy\run_daily_laptop.cmd"
if (-not (Test-Path $wrapperPath)) {
    Write-Error ("Wrapper batch missing: " + $wrapperPath)
    exit 1
}
$action = New-ScheduledTaskAction -Execute $wrapperPath
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 10:00am
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)
$desc = "Hilmar daily shipment-tracker email - runs on Cloud PC at 10:00 AM ET weekdays. Replaces the laptop scheduler. OneDrive-synced code, OL-allowlisted IP."
Register-ScheduledTask `
    -TaskName "Hilmar Daily Tracker - CloudPC" `
    -Action $action -Trigger $trigger -Settings $settings -Description $desc `
    -Force | Out-Null
$t = Get-ScheduledTask -TaskName "Hilmar Daily Tracker - CloudPC"
$t | Format-List TaskName, State
($t | Get-ScheduledTaskInfo) | Format-List NextRunTime, LastRunTime
Write-Host "  OK - task registered" -ForegroundColor Green

# Done
Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "What this Cloud PC will do:"
Write-Host "  Wake:  N/A - always on"
Write-Host "  When:  10:00 AM Cloud PC local time, weekdays"
Write-Host "  What:  refresh_stage.py, run_pipeline.py, outlook_send.py daily"
Write-Host "  To:    Currently test_list (Michael only) until wrapper flipped"
Write-Host ""
Write-Host "On MBD-TRAVEL, once Cloud PC is verified, disable the duplicate:"
Write-Host '  Disable-ScheduledTask -TaskName "Hilmar Daily Tracker"'
Write-Host ""
Write-Host "To flip wrapper to full 9-recipient distribution, edit:"
Write-Host "  $ROOT\deploy\run_daily_laptop.cmd"
Write-Host "  Change line: --to michael.deitchman@ol-usa.com"
Write-Host "  To:          --to-from-config"
Write-Host ""
