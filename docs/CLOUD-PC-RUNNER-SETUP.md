# Cloud PC self-hosted GitHub Actions runner

> **SUPERSEDED (2026-06-10).** The cutover went a different way: the daily
> fire moved to GitHub-hosted runners with app-only Graph auth + Azure Blob
> state (PR #33; see `docs/MOVE-OFF-CLOUDPC.md`). No self-hosted runner was
> ever registered, and `daily-fire.yml` — the workflow this doc set up —
> has been deleted (its schedule just queued a dead run every weekday).
> Kept for historical reference only; do not follow these steps.

One-time setup to make the Cloud PC a self-hosted GitHub Actions runner.
Once installed, the runner replaces Windows Task Scheduler as the
trigger for the daily Hilmar fire — same pipeline code, same MSAL
token, same OneDrive state, just better observability + retries.

## Why this is the right move (and what didn't change)

- **GitHub Actions** becomes the cron AND the manual fire button
- **Code is checked out at the exact commit being run** — kills the
  `git pull` + `xcopy` chain that caused the 2026-05-28 silent-non-deploy
- **Re-run a failed fire** = one click in the Actions UI
- **Runner runs as YOUR Windows user**, so OneDrive paths, MSAL token
  cache (`secrets/token-cache.json`), `secrets/*.txt`, `tracking-data-v2.json`
  all work exactly as they do today
- **No OL IT involvement needed** — no app-only Graph auth migration, no
  Application Access Policy. Pure tooling change.

## Prerequisites

- Cloud PC must be on / awake at 10 AM ET weekdays (same as today)
- You (Michael) have admin access to repo settings on GitHub
- PowerShell 5+ on the Cloud PC (built-in)

## Step 1 — Register the runner with the repo

1. In a browser on the Cloud PC, go to:
   `https://github.com/IdealX-dev/hilmar-daily-routine/settings/actions/runners/new`
2. Choose **Windows** + **x64**
3. GitHub gives you a token (one-time, expires in 1 hour) and a sequence
   of commands. Copy them — they look like:
   ```powershell
   mkdir actions-runner; cd actions-runner
   Invoke-WebRequest -Uri https://github.com/actions/runner/releases/download/v2.XXX.X/actions-runner-win-x64-...zip -OutFile actions-runner.zip
   Add-Type -AssemblyName System.IO.Compression.FileSystem
   [System.IO.Compression.ZipFile]::ExtractToDirectory("$PWD/actions-runner.zip", "$PWD")
   ./config.cmd --url https://github.com/IdealX-dev/hilmar-daily-routine --token <THE_TOKEN>
   ```
4. **When `config.cmd` prompts for runner labels**, enter:
   ```
   self-hosted,Windows,hilmar-cloudpc
   ```
   The `hilmar-cloudpc` label is what `daily-fire.yml` selects on.

5. **When prompted for a work folder**, accept the default (`_work`) OR
   point at the existing `PROJECT HILMAR` directory so checkouts land
   alongside what the wrapper used to xcopy to. The default is fine —
   the workflow uses relative paths.

## Step 2 — Install as a Windows service (so it survives reboots)

In the `actions-runner` folder, as Administrator:
```powershell
./svc.sh install        # OR: ./config.cmd --runAsService when prompted earlier
./svc.sh start
Get-Service "actions.runner.IdealX-dev-hilmar-daily-routine.*"  # confirm Running
```

The service auto-starts on every Cloud PC reboot. No manual login needed.

## Step 3 — Verify the runner is online

Back in the GitHub repo:
`Settings → Actions → Runners` — your Cloud PC should appear with status
**Idle** (green dot). If it says **Offline**, the service didn't start —
check Event Viewer or run `./run.cmd` interactively to see the error.

## Step 4 — Trigger the new workflow once to validate

`Actions → Daily fire (Cloud PC self-hosted) → Run workflow → main → dry_run=true → Run`

This fires the pipeline with `--dry-run` so no email goes out. The job
should succeed within ~2 minutes (mostly the test/QC checks). Look for:
- All steps green
- The "Upload run artifacts" step produces a `daily-fire-<run_id>` artifact

If the dry-run is green, you're done. The next scheduled fire (cron
`0 14 * * 1-5` = 10 AM ET in EDT) will be the first real one.

## Step 5 — Disable Windows Task Scheduler (cut over)

**Do this AFTER one clean dry-run.** Until then, both triggers can
co-exist — `outlook_send.py`'s `sent-YYYY-MM-DD.flag` idempotency
prevents double-send even if both fire.

Cloud PC → Task Scheduler → find the Hilmar daily task → **Disable**
(not delete — leave it as a fallback you can re-enable in one click).

## Rollback

If anything goes sideways:

1. **Disable the workflow**: GitHub Actions UI → Daily fire (Cloud PC
   self-hosted) → ⋯ → Disable workflow
2. **Re-enable Windows Task Scheduler**: right-click → Enable
3. Tomorrow's 10 AM ET fire is back on the old path

The runner service can stay installed — it's idle when no workflows
target it.

## What still needs OL IT (eventually, not today)

This path keeps the device-code MSAL token (Michael's own credentials,
~80-day refresh-token TTL). When that hits expiry you'll need to re-auth
on the Cloud PC the same way you do today. QC-023 warns at 60 days,
errors at 80.

The full move (no Cloud PC, no MSAL re-auth) still needs app-only Graph
auth from OL IT per `docs/MOVE-OFF-CLOUDPC.md`. **This setup is a
pragmatic Phase 1** — most of the observability + reliability wins, none
of the OL coordination cost.

## Companion workflows

- **`liveness.yml`** — runs on GitHub-hosted Ubuntu, checks daily that
  the Cloud PC runner actually fired. Files a `cloud-pc-down` issue if
  not. Catches "Cloud PC was asleep at 10 AM" — invisible today.
- **`sentry-tools.yml`** — manual workflow_dispatch for Sentry/Seer
  operations (list unresolved, resolve, resolve-stale, trigger-seer,
  claude-diagnose). Useful when you don't want to log into the Sentry
  UI from a small screen.
