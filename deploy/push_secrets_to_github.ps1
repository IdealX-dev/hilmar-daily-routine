# push_secrets_to_github.ps1 -- one-shot: load the GH Actions repo secrets
# for the off-Cloud-PC daily fire (PR #33 cutover).
#
# RUN THIS ON THE CLOUD PC (it's the machine that has secrets\*.txt):
#   & "$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR\hilmar-daily-routine\deploy\push_secrets_to_github.ps1"
#
# Prerequisites: gh CLI authenticated (gh auth status) -- already true on the
# Cloud PC (the wrapper uses gh for heartbeats).
#
# What it does:
#   1. Pushes the 4 file-based secrets from <repo>\..\secrets\*.txt
#      (falls back to <repo>\secrets\*.txt if the parent copy is absent).
#   2. Prompts for the 4 Azure values (Entra app + storage) and pushes them.
#      Where to find each value is printed at the prompt. Leave one blank
#      to skip it (e.g. if it's already set).
#
# ASCII ONLY in this file -- Windows PowerShell 5.1 reads BOM-less files as
# ANSI, and UTF-8 em-dash bytes decode to cp1252 curly quotes, which PS
# treats as string delimiters (the 2026-06-09 "Missing closing '}'" parse
# failure). Plain hyphens and arrows only.

$ErrorActionPreference = "Stop"
$repo = "IdealX-dev/hilmar-daily-routine"

# Locate secrets\ -- production layout keeps it in PROJECT HILMAR (repo
# parent); a bare clone may have it inside the repo.
$repoRoot = Split-Path $PSScriptRoot -Parent
$candidates = @(
    (Join-Path (Split-Path $repoRoot -Parent) "secrets"),
    (Join-Path $repoRoot "secrets")
)
$secretsDir = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $secretsDir) {
    Write-Error "No secrets folder found at: $($candidates -join ' OR ')"
}
Write-Host "Using secrets folder: $secretsDir"

function Set-RepoSecret([string]$Name, [string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        Write-Host "  SKIPPED $Name (empty)" -ForegroundColor Yellow
        return
    }
    gh secret set $Name -R $repo --body $Value
    if ($LASTEXITCODE -ne 0) { Write-Error "gh secret set $Name failed (rc=$LASTEXITCODE)" }
    Write-Host "  OK $Name" -ForegroundColor Green
}

Write-Host ""
Write-Host "[1/2] File-based secrets from $secretsDir"
$fileMap = [ordered]@{
    "SENTRY_DSN"        = "sentry-dsn.txt"
    "SENTRY_AUTH_TOKEN" = "sentry-auth-token.txt"
    "ANTHROPIC_API_KEY" = "anthropic-api-key.txt"
    "QT_APP_PASSWORD"   = "quote-tracker-pwd.txt"
}
foreach ($entry in $fileMap.GetEnumerator()) {
    $path = Join-Path $secretsDir $entry.Value
    if (-not (Test-Path $path)) {
        Write-Host "  MISSING $($entry.Key) - $path not found" -ForegroundColor Yellow
        continue
    }
    Set-RepoSecret $entry.Key ((Get-Content -Raw $path).Trim())
}

Write-Host ""
Write-Host "[2/2] Azure values (Entra app + storage) - paste each, or Enter to skip"
$azurePrompts = [ordered]@{
    "GRAPH_APP_TENANT_ID"             = "Azure Portal > App registrations > Hilmar app > Overview > 'Directory (tenant) ID'"
    "GRAPH_APP_CLIENT_ID"             = "same Overview page > 'Application (client) ID'"
    "GRAPH_APP_CLIENT_SECRET"         = "same app > Certificates & secrets > New client secret > copy the Value column IMMEDIATELY"
    "AZURE_STORAGE_CONNECTION_STRING" = "Storage account > Access keys > Connection string (create a Standard/LRS account if none exists)"
}
foreach ($entry in $azurePrompts.GetEnumerator()) {
    Write-Host ""
    Write-Host "  $($entry.Key)" -ForegroundColor Cyan
    Write-Host "    where: $($entry.Value)"
    $secure = Read-Host "    value (hidden, Enter to skip)" -AsSecureString
    $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringUni(
        [System.Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($secure))
    Set-RepoSecret $entry.Key $plain
}

Write-Host ""
Write-Host "Done. Verify the list (values stay hidden):"
gh secret list -R $repo
Write-Host @"

Next steps (docs/MOVE-OFF-CLOUDPC.md has the full sequence):
  1. Actions > Daily > Run workflow > mode=production-fire, send_to=test
     -> a full real fire that emails ONLY michael.deitchman@idealx.us
  2. When clean: disable the 'Hilmar Daily Tracker - CloudPC' scheduled
     task, THEN set repo variable HILMAR_FIRE_FROM_ACTIONS=true
     (Settings > Secrets and variables > Actions > Variables).
"@
