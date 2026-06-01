# Cloud PC heartbeat setup

After PR #19 (Liveness via wrapper heartbeat — 2026-06-01) lands, the Cloud
PC's daily wrapper dispatches a GitHub Actions workflow at the end of every
successful fire. The `liveness.yml` monitor then verifies that dispatch
happened on time. Without this setup, the liveness monitor will file a
`cloud-pc-down` GitHub issue every weekday at 11:30 AM ET.

This is one-time setup. Run it on the **Cloud PC** (the machine that
actually fires the daily pipeline via Windows Task Scheduler).

## 1 — Install GitHub CLI

```powershell
winget install --id GitHub.cli
```

Verify:

```powershell
gh --version
```

Should report `gh version 2.x` or similar.

## 2 — Authenticate gh

You have two equivalent options. Pick one.

### Option A — Interactive web login (easiest)

```powershell
gh auth login --hostname github.com --git-protocol https --web
```

Follow the browser prompt to authorize the Cloud PC machine. gh stores
the token in the Windows credential vault for the current user — survives
reboots, doesn't need to be re-entered.

### Option B — PAT in a secrets file (no interactive step)

1. Create a fine-scoped PAT at https://github.com/settings/tokens?type=beta
   - Resource owner: `IdealX-dev`
   - Repository access: only `IdealX-dev/hilmar-daily-routine`
   - Permissions: **Actions: Read and write** (everything else default-deny)
   - Expiration: 90 days (rotate per QC-023 cadence) or "no expiration"
2. Save the token to:

   ```powershell
   New-Item -ItemType Directory -Path "$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR\secrets" -Force
   Set-Content -Path "$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR\secrets\github-pat.txt" -Value "ghp_yourTokenHere" -NoNewline
   ```

3. Authenticate gh from the file:

   ```powershell
   gh auth login --with-token < "$env:USERPROFILE\OneDrive - IdealX\claude\PROJECT HILMAR\secrets\github-pat.txt"
   ```

## 3 — Verify auth

```powershell
gh auth status
```

Expect: `Logged in to github.com account ...` with `Token scopes: ...`.

## 4 — Verify the heartbeat workflow is reachable

```powershell
gh workflow view heartbeat.yml -R IdealX-dev/hilmar-daily-routine
```

You should see the workflow's name and the most recent runs (initially
empty until the wrapper fires for the first time).

## 5 — Smoke-test the dispatch

Manual dispatch to confirm the end-to-end chain works before relying on
it tomorrow morning:

```powershell
gh workflow run heartbeat.yml -R IdealX-dev/hilmar-daily-routine `
  -f at="$(Get-Date -Format o)" `
  -f sha="manual-test" `
  -f status="success" `
  -f host="manual-test"
```

Wait ~30 seconds, then:

```powershell
gh run list --workflow=heartbeat.yml -R IdealX-dev/hilmar-daily-routine --limit 1
```

The latest run should show `success` and the timestamp you just sent. If
this works, the wrapper's automated dispatch will work too — `deploy\run_daily_laptop.cmd`
uses the same `gh workflow run` call wrapped in a `where gh` guard so the
fire never fails if gh is missing.

## 6 — Confirm liveness monitor picks it up

The next time the liveness monitor runs (11:30 AM ET on weekdays, or
manually trigger via Actions UI), it should report success. If you have
an open `cloud-pc-down` issue, it'll auto-close on the next run after
the heartbeat lands.

## Failure modes + recovery

| Symptom | Cause | Fix |
|---|---|---|
| Wrapper log: `gh CLI not found; heartbeat skipped` | gh not installed | Step 1 above |
| Wrapper log: `Heartbeat dispatch exit code: 1` (or other non-zero) | gh auth expired / network glitch / PAT revoked | `gh auth status` to diagnose; if expired, redo step 2 |
| Liveness fires `cloud-pc-down` issue daily but wrapper log shows heartbeat success | Workflow name mismatch or wrong repo | Confirm `gh workflow view heartbeat.yml -R IdealX-dev/hilmar-daily-routine` works |
| `gh: command not found` after install | New PowerShell session needed to pick up PATH | Close + reopen PowerShell |

## Why this design (not `daily-fire.yml` self-hosted)

The original liveness design assumed a `daily-fire.yml` workflow running
on the Cloud PC as a self-hosted Actions runner. That migration is
in-flight (`claude/move-off-cloud-pc-gha-app-auth` branch) but not
deployed. The heartbeat-via-wrapper approach decouples liveness
observability from the runner migration — the existing Windows Task
Scheduler trigger keeps working, and we get the "did it actually fire?"
signal in GitHub.

Once the self-hosted runner is fully deployed, the heartbeat dispatch
can be kept (cheap belt-and-suspenders) or replaced with `gh run list
--workflow=daily-fire.yml` (one-line change in `liveness.yml`).
