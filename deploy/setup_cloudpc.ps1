# ============================================================================
# HISTORY - THE CLOUD PC WAS DECOMMISSIONED 2026-08-07. DO NOT RUN THIS.
#
# Everything live runs in GitHub Actions (.github/workflows/daily.yml).
# There is no Windows 365 box, no Task Scheduler entry, and no RDP session
# to open. This file is kept because it explains why the deploy code and
# the environment pins look the way they do - it is NOT procedure.
#
# RUNBOOK.md opens with the same warning, for the same reason: its FIRST
# recovery step said "RDP into Cloud PC" for weeks after the machine was
# gone, which is the worst possible place for a stale instruction since it
# is what you reach for when something is already broken.
#
# It happened again on 2026-08-27. Moving the daily fire from 8:07 to
# 6:30 AM ET forced an edit HERE, because tests/test_fire_time_consistency
# .py pinned the live fire time to this dead script's -At trigger; I then
# warned Michael that "the live Cloud-PC task still fires at 8:07". His
# reply: "we are not using the cloud pc anymore rememebr? you turned it off
# months ago and migrated system". The coupling is now cut - nothing live
# is pinned to this file, and a test asserts this header stays here.
# ============================================================================
#
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
# Ask the py launcher for the EXACT pinned build first (py -3.12). Bare
# python/py default to whatever's first on PATH (3.14 on the drifted box), so
# without this the setup would install deps into the wrong interpreter and
# never find the 3.12 you just installed.
try {
    $exe = (& py "-$PINNED" -c "import sys;print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -eq 0 -and $exe) {
        $exe = $exe.Trim()
        Write-Host "  OK  py -$PINNED -> $exe" -ForegroundColor Green
        $script:PYTHON_CMD = $exe
        $pythonOK = $true
    }
} catch {}
if (-not $pythonOK) {
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
}
if (-not $pythonOK) {
    Write-Host "  Python $PINNED not found. Installing via winget..." -ForegroundColor Yellow
    winget install --id "Python.Python.$PINNED" --accept-source-agreements --accept-package-agreements --silent
    $script:PYTHON_CMD = "python"
    Write-Host "  After install, RE-OPEN the shell and re-run so the wrapper picks up $PINNED." -ForegroundColor Yellow
}

# Step 3 - install the COMPLETE runtime dependency set from requirements.txt.
# (Was a hand-typed 4-package line that omitted jinja2/jsonschema/dateutil/
# sentry-sdk -- the box ran for a week missing them. requirements.txt is now the
# canonical list QC-054 verifies + QC-060 reconciles, so this can't drift.)
Write-Host ""
Write-Host "[3/6] Installing runtime deps from requirements.txt..." -ForegroundColor Yellow
& $script:PYTHON_CMD -m pip install --user --quiet -r (Join-Path $PSScriptRoot "..\requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }
Write-Host "  OK" -ForegroundColor Green

# Step 3b - deploy the latest code from the checkout into the runtime locations
# the scheduled task reads. The fire's xcopy auto-syncs scripts\ + deploy\*.py,
# but it deliberately CANNOT replace the running wrapper (run_daily_laptop.cmd)
# -- so a wrapper change reaches the box by re-running THIS setup, which copies
# it safely (the wrapper is not executing now). Idempotent.
Write-Host ""
Write-Host "[3b/6] Deploying scripts + wrapper from checkout to runtime..." -ForegroundColor Yellow
$checkout = Join-Path $ROOT "hilmar-daily-routine"
if (Test-Path (Join-Path $checkout "deploy\run_daily_laptop.cmd")) {
    Copy-Item -Force (Join-Path $checkout "scripts\*.py") (Join-Path $ROOT "scripts\")
    if (Test-Path (Join-Path $ROOT "deploy")) {
        Copy-Item -Force (Join-Path $checkout "deploy\*.py") (Join-Path $ROOT "deploy\")
        Copy-Item -Force (Join-Path $checkout "deploy\run_daily_laptop.cmd") (Join-Path $ROOT "deploy\")
    }
    if (Test-Path (Join-Path $checkout "config.json")) {
        Copy-Item -Force (Join-Path $checkout "config.json") (Join-Path $ROOT "config.json")
    }
    # src\hilmar\*.py is needed at runtime: scripts\qc_selfheal.py imports
    # hilmar.parser_accuracy for the QC-039 gate (+ hilmar.core/body_parser for
    # QC-040/041). Without it the box has no 'hilmar' on the path and the gate
    # cannot evaluate ("No module named 'hilmar'").
    if (Test-Path (Join-Path $checkout "src\hilmar")) {
        $hilmarDst = Join-Path $ROOT "src\hilmar"
        if (-not (Test-Path $hilmarDst)) { New-Item -ItemType Directory -Force -Path $hilmarDst | Out-Null }
        Copy-Item -Force (Join-Path $checkout "src\hilmar\*.py") $hilmarDst
    }
    Write-Host "  OK  scripts\*.py + deploy\*.py + wrapper + config + src\hilmar deployed" -ForegroundColor Green
} else {
    Write-Warning "Checkout not found at $checkout - skipping deploy (the fire's git-pull will sync scripts later)."
}

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
$TaskName = "Hilmar Daily Tracker - CloudPC"
$action = New-ScheduledTaskAction -Execute $wrapperPath
# One trigger (2026-07-21): a single fire Mon-Fri 8:07 AM ET that reports the
# PRIOR business day. No wrap-up, no weekend trigger. The wrapper always sets
# HILMAR_REPORT_WINDOW=previous to match daily.yml's gate.
$triggerMorning = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 6:30am
$trigger = @($triggerMorning)
# -WakeToRun nudges the box if it ever sleeps at fire time.
# ExecutionTimeLimit raised 15 -> 50 min (2026-06-26): run_pipeline's per-step
# timeouts alone sum to ~25 min worst case, plus refresh_stage + 2x outlook_send
# + teams + weekly + backup + improvements + integrity + heartbeat. The old
# 15-min cap could SIGTERM the whole tree mid-run on a slow box. 50 min matches
# daily.yml's timeout-minutes:50 for the same pipeline.
# RestartCount 3 -> 0: auto-restarting an email-sending wrapper is unsafe. If
# the old 15-min kill landed DURING outlook_send (after Graph accepted the
# message but before the sent-flag was written), the restart re-ran the send
# and double-mailed all 10 recipients. With a 50-min budget the run is allowed
# to finish, so no restart is needed. (RestartInterval is dropped -- it only
# applies when RestartCount > 0.)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 50) `
    -MultipleInstances IgnoreNew `
    -RestartCount 0
$desc = "Hilmar daily shipment-tracker email - runs on Cloud PC Mon-Fri 8:07 AM ET whether logged on or not (S4U). Reports the prior business day (Mon->Fri, Tue->Mon, ... Fri->Thu). No wrap-up, no weekend fire. Aligned to daily.yml + the Sentry cron monitor."
# Register to run WHETHER OR NOT THE USER IS LOGGED ON (S4U). Root cause of the
# 2026-06 silent miss: an INTERACTIVE task quietly skipped 10 straight fires
# once the RDP session stopped staying logged on. S4U needs an ELEVATED shell;
# if this setup is NOT elevated the registration throws Access Denied -- we
# catch it, warn, and LEAVE ANY EXISTING TASK UNCHANGED, so a non-admin
# update_box.cmd run can never downgrade a good S4U task back to interactive.
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
    Write-Host "  OK - registered S4U (runs whether logged on or not)" -ForegroundColor Green
} catch {
    Write-Warning ("Task registration failed: " + $_.Exception.Message)
    Write-Warning "S4U needs an ELEVATED PowerShell. Re-run this setup as Administrator to (re)create the run-whether-logged-on task. Any existing task was left UNCHANGED."
}
$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($t) {
    $t | Format-List TaskName, State
    ($t | Get-ScheduledTaskInfo) | Format-List NextRunTime, LastRunTime
    $t.Principal | Format-List UserId, LogonType
}

# Done
Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "What this Cloud PC will do:"
Write-Host "  Wake:  N/A - always on"
Write-Host "  When:  8:07 AM ET Mon-Fri (reports the prior business day)"
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
