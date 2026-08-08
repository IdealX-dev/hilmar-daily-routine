# Hilmar Daily Tracker

> 🟢 **CANONICAL REPO — single source of truth** for Hilmar's daily tracker.
> The earlier `IdealX-dev/hilmar-tracker` repo (Linux VM Plan A) was
> **fully merged 2026-05-17**: 17 source modules into `src/hilmar/`,
> 17 test files into `tests/` (now **519 passing tests**), HANDOFF.md +
> INSIGHTS-DESIGN.md into `docs/`, pyproject.toml at repo root. The
> dormant repo is now archived for historical reference only.
> ONE repo, ONE application — per Michael 2026-05-17.

Production pipeline for the OL-USA / Hilmar Ingredients daily shipment tracker email + dashboard. Runs unattended in **GitHub Actions at 8:07 AM ET, Mon-Fri**, reporting the prior business day. (History: it fired from a Win365 Cloud PC at 6:07 PM ET until the 2026-06 cutover; that machine is retired — see `docs/MOVE-OFF-CLOUDPC.md`.)

**Status (last reviewed 2026-06-26):** self-healing QC matrix (QC-001..QC-063; see [`reports/QC-INDEX.md`](reports/QC-INDEX.md)), 100% Q&L carrier coverage, idempotent sends, ol-quote-tracker reconciliation, schema.json + full pytest regression suite (full hilmar-tracker port), Codespaces-ready for editing from any device.

## Two code paths — both authoritative, by phase

- **`scripts/`** — the ACTIVE production pipeline. `run_pipeline.py` orchestrates the daily 8:07 AM ET fire on the GitHub Actions runner. Runs `ingest.py` → `qc_selfheal.py` → `gen_email.py` → `outlook_send.py`. This is what Lonny + the OL distribution list see daily.
- **`src/hilmar/`** — the inherited mature module library from the consolidated hilmar-tracker. Contains `baselines.py` (rolling P50/P90 stats), `insights.py` (LLM-driven daily narratives), `feedback_ingest.py` (👍/👎 self-learning), `model_router.py` (LLM task routing), `parser_fallback.py`, plus the full pytest-compatible test suite. Available for the next migration phase when `scripts/` evolves toward this richer architecture.

The two folders coexist intentionally during the migration period. Tests run against `src/hilmar/`; production runs against `scripts/`. Daily pipeline is unaffected by `src/hilmar/` additions.

---

## Remote access — view, edit, and run from anywhere

### 📱 View today's report from your phone
- **Outlook mobile app** — the daily email arrives with `hilmar-dashboard.html` attached. Tap to open in browser. The dashboard is mobile-responsive (auto-collapses KPI grid, hides low-signal columns).
- **OneDrive mobile app** — `IdealX → claude → PROJECT HILMAR → reports → hilmar-dashboard.html`. Always shows the latest pipeline output.
- **Outlook web** — `outlook.office.com` works from any browser. Search "Daily Shipment Tracker" to find today's email.
- **Audit inbox** — `michael.deitchman@idealx.us` receives the Daily Systems Audit (red flags / observations / suggestions) alongside the OL distribution copy.

### 💻 Edit code from any device — GitHub Codespaces
1. Open `https://github.com/IdealX-dev/hilmar-daily-routine` in any browser (phone, iPad, other laptop)
2. Click the green **"<> Code"** button → **"Codespaces"** tab → **"Create codespace on main"**
3. Full VS Code opens in your browser with Python 3.12, all dependencies pre-installed (per `.devcontainer/devcontainer.json`), and Claude Code extension
4. Edit, test (`python scripts/run_tests.py`), commit, push — all from the browser
5. **The next fire picks your changes up**: the GitHub Actions fire checks out `main` at run time — nothing to copy anywhere.

### 🖥️ Run the pipeline manually
Actions → **Daily** → Run workflow. That is the whole procedure, from any
browser including a phone.

The idempotency flag (`reports/sent-YYYY-MM-DD.flag`) and the mailbox guard
both block a duplicate send, so dispatching when you are unsure whether the
8:07 AM ET run already went is safe.

> **The Cloud PC is retired.** This section used to say "RDP into
> `CPC-micha-E552L` and double-click `run_daily_laptop.cmd`". There is no
> machine to log into and no wrapper to run. See RUNBOOK.md for the current
> failure modes; the Windows-era ones there are labelled `[HISTORY]`.

### 🤖 Drive via Claude (any device with claude.ai)
- Open `claude.ai` in any browser. Tell Claude what you want changed/checked. Claude can edit code via Codespaces / GitHub, schedule routines, or surface findings to your IdealX audit inbox.
- The hilmar-daily-routine repo is the single source of truth; the GitHub Actions fire checks it out at run time.

---

## What this repo holds

```
scripts/         Python pipeline modules (ingest → drift → QC → patch → render)
deploy/          [HISTORY] Cloud-PC wrapper batch + setup; qc_alert still used
config.json      Distribution list, paths, rules
schema.json      JSON Schema for tracking-data-v2.json
requirements.txt reportlab, msal, requests, tzdata
reports/         QC-INDEX.md (the QC matrix index)
.devcontainer/   Codespaces config — auto-installs Python deps + extensions
```

## What lives only in OneDrive (NOT in git)

Live data + secrets that must never hit GitHub:
- `tracking-data-v2.json` — current request state (rebuilt each fire by ingest)
- `scripts/stage_emails.txt` + `stage_emails_bodies.txt` — Outlook fetch cache
- `data-backups/` — rotating snapshots (14 retained, dual-format prune)
- `secrets/token-cache.json` — MSAL refresh token (chmod 600)
- `reports/` daily artifacts (HTML dashboard, PDF, scorecards, run-log, sent flags)

`.gitignore` enforces this separation.

## Daily flow

GitHub Actions (`.github/workflows/daily.yml`) fires at **8:07 AM ET, Mon-Fri**,
reporting the **prior business day**:

| Step | Script | What |
|---|---|---|
| 0 | `actions/checkout` + `state_store.py pull` | Check out `main`, pull state from the Azure Blob store |
| 1 | `refresh_stage.py` | Pull new Lonny↔OL emails + HILMAR booking confirmations via Microsoft Graph |
| 2 | `run_pipeline.py` | backup → ingest → drift_check → QC → patch_carriers → QC → dashboard → PDF → scorecards → email body |
| 3 | `outlook_send.py daily` | Send to full distribution (10 recipients incl. idealx.us). Idempotent via `reports/sent-YYYY-MM-DD.flag` |
| 4 | `qc_alert_if_needed.py` | Email Michael if QC drifts from CLEAN |
| 5 | `gen_improvements_report.py` + `outlook_send.py daily` | Daily Systems Audit to `michael.deitchman@idealx.us` only |
| 6 | `state_store.py push` | Push updated state back to the blob store — the runner is ephemeral |

## QC + self-heal matrix

See [`reports/QC-INDEX.md`](reports/QC-INDEX.md) for the full QC matrix index: severity, what each catches, what self-healing fires automatically, which commit added it.

Standing rule: every new code pattern ships with its QC counterpart in the same commit.

## Distribution list

Defined in `config.json` `distribution.full_list`. Currently 9 recipients:
- `michael.deitchman@ol-usa.com`
- `michael.deitchman@idealx.us`
- 7 additional OL operators (alan.baer, carrie.murphy, seada.sabic, linda.echevarria, steve.petriccione, MBD_Export_Pricing, MBD_OceanExportBookingShared)

  (caren.tobel removed 2026-07-20 per Michael — she remains a *sender* exclusion in `ingest_scope.mailboxes_excluded`, unrelated to who receives the report.)

QC-022 ERROR-gates accidental edits to this list (catches missing idealx.us, external domains, wrong count).

## Authentication

MSAL public client (`outlook_send.py`) device-code flow → token cache at `secrets/token-cache.json`. Silent refresh works as long as token is < 80 days old (QC-023 warns at 60 days). Re-auth: `python scripts/outlook_send.py auth`.

## Commit hash history

See `git log --oneline`. Major milestones since 2026-05-07 cutover:
- `697e219` Yesterday-KPI semantics + Mon-Fri week labels + QC-011
- `c24255d` Patch_carriers auto-discovery + trade-region map + QC-014/015/016
- `c13d831` parse_rate_table primary + QC-017
- `f6aae29` Day-row math reconciliation (Pending card) + QC-018 + outlook_send idempotency
- `505f644` Multi-line pipe-table parser + QC-019
- `a6bc3d2` NQ 14-day display cutoff + QC-020a/b
- `cd5fe6c` QC-021/022/023/024/025 + QC-INDEX.md
- (this commit) Codespaces config + wrapper git-pull + QC-026 + mobile-responsive dashboard
