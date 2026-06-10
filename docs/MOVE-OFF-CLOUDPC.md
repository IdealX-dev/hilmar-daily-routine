# Moving the Hilmar Daily Tracker off the Cloud PC

The Cloud PC fire (`deploy/run_daily_laptop.cmd` triggered by Windows Task
Scheduler at 10 AM ET) is fragile in ways the 2026-05-28 audit made
unmistakable: `git pull` → `xcopy` to a parallel folder is a five-step
silent-failure chain, and an unmerged PR sat for 5 days while the daily
audit reported the SAME problems every morning. QC-053 catches that class
now, but the underlying brittleness is unchanged.

This doc is the cutover plan. **Each section is a prerequisite — not until
all four are met can the Cloud PC be retired.**

## CURRENT STATE (2026-06-10) — code-complete, operational steps only

PR #33 merged: app-only Graph read AND send, Azure Blob state sync
(`scripts/state_store.py`, including same-day send-flag sync so idempotency
is machine-independent), and a fully wired `production-fire` job in
`daily.yml`. The sections below are the original plan — kept for reference;
the "blocked on OL IT" framing is obsolete (the Entra app exists).

What remains is operational, in this exact order:

1. **Load the 8 repo secrets** — on the Cloud PC run
   `deploy\push_secrets_to_github.ps1` (pushes the 4 from `secrets\*.txt`,
   prompts for the 4 Azure values).
2. **Verification fire** — Actions → Daily → Run workflow →
   `mode=production-fire`, `send_to=test`. Runs the complete real pipeline
   (app-only read, blob state, app-only send) but emails ONLY
   `michael.deitchman@idealx.us`. Safe to run any time of day; it uses
   `--no-flag` so it can't disturb production idempotency state.
3. **Flip** — disable the Windows scheduled task
   (`Hilmar Daily Tracker - CloudPC`) **first**, then set the repo variable
   `HILMAR_FIRE_FROM_ACTIONS=true` (Settings → Secrets and variables →
   Actions → Variables). The variable is the whole switch: the `Daily`
   workflow's schedule then runs the real fire at 10 AM ET (DST-proof dual
   cron + gate). **Order matters:** send-flags are per-machine, so two live
   schedules would each send the client email once.
4. **Rollback** (if ever needed) — unset the variable, re-enable the task.

Liveness continuity is wired: the GH fire dispatches the same
`heartbeat.yml` the Cloud PC wrapper does (`host=github-actions`), so
`liveness.yml` keeps alerting on a missed fire regardless of which machine
owns the schedule.

## State today (2026-05-30)

What runs where:

| Component | Cloud PC location | GH Actions parallel |
|---|---|---|
| Trigger | Windows Task Scheduler 10 AM ET | `.github/workflows/daily.yml` cron 14:00 UTC |
| Code | `OneDrive/.../PROJECT HILMAR/scripts/` (xcopy from git) | Git checkout, directly from the SHA |
| Data | `OneDrive/.../PROJECT HILMAR/tracking-data-v2.json` | ❌ not yet reachable |
| MSAL token | `secrets/token-cache.json` (per-user, 80d lifetime) | ❌ replaced by app-only (P2) |
| Stage emails | `scripts/stage_emails*.txt` (OneDrive) | ❌ not yet reachable |
| Backups | `data-backups/` + `OneDrive/.../HILMAR-BACKUP-OFFLINE/` | ❌ not yet reachable |
| Outlook send | MSAL token → Graph SendMail (user mailbox) | ❌ requires app-only + send scope |
| Sentry | `secrets/sentry-dsn.txt` + `secrets/sentry-auth-token.txt` | ✅ GH Actions secrets (when set) |
| Anthropic LLM fallback | `secrets/anthropic-api-key.txt` | ✅ GH Actions secrets (when set) |
| Turso (`ol-quote-tracker`) sync | `secrets/quote-tracker-pwd.txt` | ✅ GH Actions secrets (when set) |

`.github/workflows/daily.yml` **today** runs the test+coverage routine on
a daily cron, captures artifacts (test-result.json, qc-result.json,
coverage.json) for 14-day retention, and reports green/red as a check.
It does **not** fire the production email — the four prerequisites below
gate that.

## The four prerequisites for cutover

### 1. App-only Graph auth (P2 — needs OL IT action)

**Why:** the daily fire is unattended; it should NOT own a human's
mailbox token. Today's device-code auth requires a person to log in
every ~80 days when the token expires, on a machine that's mostly asleep.

**What ships from this side (already shipped — `src/hilmar/app_auth.py`):**

- `AppOnlyCredentials` dataclass (tenant_id, client_id, client_secret)
- `app_only_credentials_from_env()` — returns credentials if the three
  env vars are set, else `None` (signals "fall back to device-code")
- `acquire_app_only_token(creds)` — MSAL `acquire_token_for_client`
  flow with the standard `.default` scope
- 8 tests covering env-var contract, frozen dataclass, MSAL happy /
  error / non-dict paths

The GraphClient extension that picks app-only when configured is a
small follow-up — it isn't in this PR because the env vars don't
exist yet anywhere, so the integration can't be exercised end-to-end.
Add `_app_auth_token()` to `GraphClient` once OL IT delivers item 6.

**OL IT needs to do (per `docs/ENTERPRISE-MODERNIZATION.md`):**

1. Register a new app in Entra ID: **"Hilmar Daily Tracker (app-only)"**.
2. Add API permission, **type=Application** (not Delegated):
   - `Mail.Read.Shared`
   - `Mail.Send.Shared` (only if we move outbound to app-only too;
     can be deferred — outbound can stay on the Cloud PC during phase 1)
3. Admin-consent the permissions.
4. **Apply an Application Access Policy** restricting the app to a
   SINGLE mailbox: `MBD_OceanExportBookingShared@ol-usa.com`. Without
   this the app could read any mailbox in the tenant — that's the
   policy decision the original enterprise IT review flagged.
   ```powershell
   New-ApplicationAccessPolicy -AppId <client-id> `
     -PolicyScopeGroupId MBD_OceanExportBookingShared@ol-usa.com `
     -AccessRight RestrictAccess `
     -Description "Hilmar daily tracker — scoped to MBD shared mailbox"
   ```
5. Generate a client secret (or upload a certificate — preferred for
   production). Hand over: tenant_id, client_id, client_secret.

**Verify with one command** (when secrets land in env):
```bash
GRAPH_APP_TENANT_ID=... GRAPH_APP_CLIENT_ID=... GRAPH_APP_CLIENT_SECRET=... \
  python3 -c "from hilmar.app_auth import app_only_credentials_from_env, acquire_app_only_token; \
             c = app_only_credentials_from_env(); print('len=', len(acquire_app_only_token(c)))"
```
Expect: `len= 2000` or so (a JWT access token).

### 2. State location

**Why:** GH Actions runners are ephemeral. `tracking-data-v2.json`,
`stage_emails*.txt`, `data-backups/`, and the MSAL cache currently live
on OneDrive and survive only because the Cloud PC's filesystem is
persistent.

**Options, ranked by lift:**

| Option | Cost | Effort | Verdict |
|---|---|---|---|
| **Azure Blob Storage (versioned)** | ~$1-5/mo | Medium — write `state_store.py` adapter that reads/writes the same files via blob client | **Recommended** — versioning is the time-machine the dual-target backup currently approximates |
| GitHub release artifacts | $0 (within plan limits) | Low — `gh release upload/download` in the workflow | Cheap but artifact retention caps at 90d max; backup story weaker |
| Commit data back to a `data-state` branch | $0 | Low — but writes to the repo, ugly history | Acceptable as a bridge, NOT a destination |
| GitHub repository file via Contents API | $0 | Low | Same downsides as commit-back; rate-limited |

Picking blob: add `azure-storage-blob` to `pyproject.toml`, write
`src/hilmar/state_store.py` with `read_state(name)` / `write_state(name,
data)`, switch `ingest.run_ingest` / `qc_selfheal` / `backup.py` to read
through it. Cloud PC + GH Actions both go through the same adapter.

**Verify:** existing test fixtures pass against the blob adapter using
[azurite](https://github.com/Azure/Azurite) as a local emulator.

### 3. Secret distribution

The Cloud PC reads secrets from `secrets/*.txt` files. GH Actions reads
from `${{ secrets.* }}`. Both need the same values.

Secrets to provision in **repo Settings → Secrets and variables → Actions**:

| Secret | Source today |
|---|---|
| `GRAPH_APP_TENANT_ID` | (new — from OL IT in prerequisite 1) |
| `GRAPH_APP_CLIENT_ID` | (new — from OL IT in prerequisite 1) |
| `GRAPH_APP_CLIENT_SECRET` | (new — from OL IT in prerequisite 1) |
| `SENTRY_DSN` | `secrets/sentry-dsn.txt` |
| `SENTRY_AUTH_TOKEN` | `secrets/sentry-auth-token.txt` |
| `ANTHROPIC_API_KEY` | `keys/idealx-intel.env` |
| `QT_APP_PASSWORD` | `secrets/quote-tracker-pwd.txt` |
| `AZURE_STORAGE_CONNECTION_STRING` | (new — from prerequisite 2) |

The workflow already hard-fails the production-fire job if any required
secret is missing — see `.github/workflows/daily.yml` "Verify
prerequisites" step.

### 4. Retry / idempotency model

**Why:** the Cloud PC is single-tenant; a step that fails just fails. GH
Actions can re-run a workflow trivially, so the pipeline must be safe to
re-run mid-day.

Current state of idempotency:

- **`outlook_send.py daily`** — has built-in idempotency via
  `reports/sent-YYYY-MM-DD.flag`. Already safe. ✅
- **`run_pipeline.py`** — each step is independently safe to re-run
  (drift_check, qc_selfheal, gen_*, share_intel are all functions of
  state). ✅
- **`ingest.py`** — additive merge protects prior WINs. ✅
- **`backup.py`** — would create duplicate snapshots on re-run. ⚠️
  Hash-dedup before write would fix.
- **`sync_to_quote_tracker.py`** — already idempotent (upsert by entity
  name). ✅

The one lift: backup.py hash-dedup. Tractable, ~30 lines.

## Cutover sequence

Once all four prerequisites are met:

1. **Dry-run week.** Flip `RUN_PRODUCTION_FIRE` flag to `true` but ALSO
   keep the Cloud PC firing. Both run the pipeline; only the Cloud PC
   sends the email. Compare artifacts daily for 5 business days. Diff
   should be zero (same code, same data store).
2. **Cutover day.** Disable the Windows Task Scheduler on the Cloud PC.
   GH Actions becomes canonical.
3. **Soak week.** Watch QC-021 + QC-053 in the daily audit. If anything
   looks off, re-enable the Cloud PC trigger (it's still fully
   functional, just disabled in the scheduler).
4. **Decommission.** After 2 clean weeks, the Cloud PC scheduler stays
   off permanently. The Cloud PC itself stays available as a manual
   fallback / dev environment for at least one quarter.

## What this PR ships (today)

- `.github/workflows/daily.yml` — daily cron + workflow_dispatch +
  artifact upload. Today: test+coverage only. Tomorrow's flip is one
  PR (add the production-fire job body once prerequisites land).
- `src/hilmar/app_auth.py` + `tests/test_app_auth.py` — the app-only
  auth scaffolding ready for OL IT to engage.
- This doc (`docs/MOVE-OFF-CLOUDPC.md`).

## What this PR does NOT do

- It does NOT replace the Cloud PC fire. The Cloud PC is still
  canonical until cutover. **Do not disable the Windows Task Scheduler
  yet.**
- It does NOT add the GraphClient app-only branch. That's a one-file
  change once the env vars exist somewhere to exercise the path.
- It does NOT migrate `tracking-data-v2.json` to blob storage. That's
  prerequisite 2 — a separate PR.

## Owners

- App-only Graph auth (prereq 1) — **OL IT** (Carrie + the tenant admin)
- State migration to blob (prereq 2) — **Michael + me** (write the
  adapter PR, validate against the existing test suite)
- Secret distribution (prereq 3) — **Michael** (paste values into GH
  Actions secrets once available)
- Retry / idempotency (prereq 4) — **me** (one-PR fix to backup.py
  hash-dedup)
