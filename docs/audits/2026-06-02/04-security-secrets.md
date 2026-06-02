# 04 — Security & Secrets-Handling Audit

**Date:** 2026-06-02
**Auditor:** Claude (read-only)
**Scope:** Hilmar Daily Tracker — secrets directory hygiene, PII scrubbing,
auth chains (MSAL / GitHub PAT / Anthropic / Sentry), report/log leakage,
email-dispatch correctness, external API trust boundaries, public exposure,
and mitigation for an in-chat credential leak earlier today.

This is a paper review against the working tree at HEAD. No code was run,
no network calls made, no secret values read.

---

## Summary

The repo is generally well-designed for secrets hygiene: `.gitignore`
covers `secrets/`, `*.pem`, `*token-cache.bin`, `*.env`, `data-backups/`,
`reports/`, `tracking-data-v2.json`, and stage caches. Sentry's
`send_default_pii=False` plus a custom `_before_send` scrubber strip
emails, MDOLX/carrier refs, IMIDs, Outlook conv IDs, and internal
`req_HEX` IDs. The 10-recipient distribution is guarded by QC-022 and
by an idempotency flag on the daily send. MSAL token caches are
chmod-600'd on write.

That said, this audit finds **one likely Critical** (a leaked credential
in chat earlier today that has not yet been rotated), **two High** (an
authentic Outlook conversation ID committed to the repo, and the
chmod-600 of secrets in `sentry-tools.yml` that is silently lost when
secrets/ does not exist before the per-file write), **five Medium**
defense-in-depth gaps, and **three Low** nits.

---

## 1. Secrets directory hygiene

### 1.1 Inventory of expected secrets

Confirmed file readers across the tree:

| File | Reader(s) | Purpose | Validation on load |
|---|---|---|---|
| `secrets/token-cache.json` (legacy `.bin`) | `scripts/outlook_send.py:42`, `scripts/refresh_stage.py`, `deploy/setup_cloudpc.ps1:31` | MSAL device-code cache for Graph send/read | None — opaque to MSAL |
| `secrets/sentry-dsn.txt` | `scripts/sentry_setup.py:145-156` | Sentry SDK init | `startswith("https://")` |
| `secrets/sentry-auth-token.txt` | `scripts/sentry_api.py:62`, `scripts/sentry_seer.py:68` | Sentry REST API + Seer | `startswith("sntrys_" \| "sntryu_")` |
| `secrets/anthropic-api-key.txt` | `scripts/pdf_llm_rescue.py:64`, used indirectly by `scripts/qc_actions_from_sentry.py:432` | Claude API for PDF rescue + diagnose | `startswith("sk-ant-") or len > 30` |
| `secrets/quote-tracker-pwd.txt` | `scripts/sync_to_quote_tracker.py:91` | Shared-password cookie auth to ol-quote-tracker | Non-empty only |
| `secrets/github-pat.txt` (PR #19) | `deploy/run_daily_laptop.cmd:181-182`, `docs/CLOUD-PC-HEARTBEAT-SETUP.md:50-58` | `gh auth login --with-token` for heartbeat dispatch | None |
| `secrets/auth-status.json` | `scripts/outlook_send.py:298` (bg auth) | Device-code flow handoff | None |

Git history was checked with `git log --all --diff-filter=A -- 'secrets/*'`
— **no commit ever added a secrets/ file**, so the gitignore guard has
held. Good.

### 1.2 Findings — secrets directory hygiene

#### F-1.1 — Anthropic key validator too permissive (Low)
**File:** `scripts/pdf_llm_rescue.py:70`
`t.startswith("sk-ant-") or len(t) > 30` accepts any >30-char string. A
mis-pasted Sentry token (`sntrys_…`, ~64 chars) would be sent to the
Anthropic API and could leak into Anthropic's logs as a failed-auth event.
**Fix:** drop the `or len(t) > 30` fallback. **Effort:** 2 minutes.

#### F-1.2 — chmod-600 best-effort silently fails on Windows / OneDrive (Low)
**File:** `scripts/outlook_send.py:61-64`
`os.chmod(0o600)` is correctly wrapped in `except OSError`, but means
production never has the bit set. OneDrive syncs the cache between Cloud
PC and MBD-TRAVEL; risk is low because the cache is bound to a single
identity, but the file should be treated as sensitive.
**Fix:** RUNBOOK note + optional QC check for world-readable on Unix
mounts. **Effort:** 5 minutes.

#### F-1.3 — CI secret materialization races chmod (High)
**File:** `.github/workflows/sentry-tools.yml:70-84`
Workflow `printf`s tokens into `secrets/*.txt`, *then* runs
`chmod 600 secrets/*.txt`. Files exist with the runner's umask (022)
between the two commands; any future intermediate step would see them
world-readable. No `if: always()` cleanup either, so a future
`upload-artifact` accidentally uploading `secrets/` would publish them.
**Fix:** `umask 077` at top of step; add final
`rm -f secrets/*.txt` in `if: always()` post-step. **Effort:** 10 minutes.

#### F-1.4 — `secrets/auth-status.json` carries a live device code (Medium)
**File:** `scripts/outlook_send.py:298-321`
Background-auth writes `{user_code, verification_uri, expires_at}` to
disk. Code grants delegated Mail.Send / Mail.Read / Files.ReadWrite to
anyone who presents it at microsoft.com/devicelogin within ~15 min. The
file lands in OneDrive — risk if the folder is ever link-shared.
**Fix:** write to `%TEMP%` or `secrets/.transient/`, unlink on
success/failure, add to `.gitignore` defensively. **Effort:** 15 minutes.

---

## 2. PII scrubbing effectiveness

### 2.1 Regex coverage analysis

`scripts/sentry_setup.py:50-95` scrubs six pattern classes. Spot-checked
against the example payloads requested:

| Test input | Pattern matched | Redacted? |
|---|---|---|
| `MBD_OceanExportBookingShared@ol-usa.com` | `_EMAIL_RX` (line 53) | yes → `[EMAIL_REDACTED]` |
| `lupfold@hilmaringredients.com` | `_EMAIL_RX` | yes |
| `MDOLX-12345` | `_MDOLX_RX` (line 56) — `\bMDOL[XMFD]\d+\b` | **NO** — regex requires `MDOLD\d+` with no hyphen. `MDOLX12345` matches; `MDOLX-12345` does not. |
| `MDOLX260420` | `_MDOLX_RX` | yes |
| `req_HEX_ABCD1234` | `_REQ_ID_RX` — `req_[0-9a-f]{16,}` | **NO** — requires lowercase hex, ≥16 chars. `req_abcd1234` (10 chars) misses; `req_HEX_…` (uppercase + underscores) misses. |
| Outlook conv ID `AAQkAGQ3Mjcw…` | `_CONV_ID_RX` — `AAQ[A-Za-z0-9_=+/-]{20,}` | yes |
| IMID `<abc@server.com>` | `_IMID_RX` | yes |
| Carrier `RICGH7587500` | `_CARRIER_REF_RX` | yes |

### 2.2 Findings — PII scrubbing

#### F-2.1 — MDOLX hyphenated form not redacted (Medium)
**File:** `scripts/sentry_setup.py:56`
Regex `\bMDOL[XMFD]\d+\b` matches `MDOLX260420` but not `MDOLX-260420`
or `MDOL X 260420` (space artifacts from PDF text-extraction).
Forward-looking gap, not a current leak.
**Fix:** `\bMDOL[XMFD][\s\-]?\d+\b`. **Effort:** 2 minutes + 1 test.

#### F-2.2 — `req_HEX` ID pattern misses real IDs (Medium)
**File:** `scripts/sentry_setup.py:69`
CLAUDE.md §8 promises `req_HEX` stripping, but regex is
`req_[0-9a-f]{16,}` — lowercase only, ≥16 chars. Works today (the
ingestor generates 16-hex form) but uppercase / shorter variants would
leak. **Fix:** `req_[0-9a-fA-F]{8,}`. **Effort:** 2 minutes.

#### F-2.3 — IMID pattern only matches angle-bracketed form (Medium)
**File:** `scripts/sentry_setup.py:63`
Pattern requires `<…>`. Graph payloads frequently include bare
`internetMessageId: "abc.def@OL-USA.NAMPRD12.PROD.OUTLOOK.COM"`. The
email regex catches the `@host` portion; the GUID-prefix does not get
the IMID treatment.
**Fix:** add bare-token branch anchored on `@…OUTLOOK.COM` /
`@…ol-usa.com` suffix, or sweep on key name. **Effort:** 10 minutes.

#### F-2.4 — Breadcrumb `category`/`type` not scrubbed (Low)
**File:** `scripts/sentry_setup.py:122-127`
`_before_send` walks `message` and `data` only. Custom breadcrumbs via
`add_breadcrumb(category="…")` would not be scrubbed. Defense-in-depth.
**Fix:** apply `_walk_scrub` to whole crumb dict. **Effort:** 5 minutes.

### 2.3 Leakage channels outside Sentry

#### F-2.5 — `reports/run-log.txt` is unscrubbed stdout (Medium)
**Files:** `deploy/run_daily_laptop.cmd:60-167` (redirects to log);
`scripts/gen_improvements_report.py:679` (reads back into audit email)
Subjects logged include `MDOLX260420` and Lonny's email verbatim. The
audit email containing those excerpts goes only to Michael today, but a
screenshot/support-ticket/forward propagates PII. A Sentry capture that
attaches log content also bypasses `_before_send` because the leak is
in the log text, not in a Sentry event field.
**Fix:** scrub on read in
`gen_improvements_report.collect_recent_log_lines()`. **Effort:** 30 min.

#### F-2.6 — `reports/email-body.html` is the outbound client deck (Low, by design)
File is the literal email body — contains MDOLX, lane stats, Lonny's
name intentionally. Risk vector is accidental OneDrive link-share of
`reports/`. **Fix:** RUNBOOK note. **Effort:** 5 min doc-only.

#### F-2.7 — Sentry self-test submits real-shaped PII (Low)
**File:** `scripts/sentry_setup.py:467-473`
`__main__` sends `lupfold@hilmaringredients.com … MDOLX260622` to Sentry
to verify scrubbing. If regex breaks, self-test becomes the leak vector.
**Fix:** use `test@test.example + MDOLX000001`. **Effort:** 2 minutes.

---

## 3. Auth chain risks

### 3.1 MSAL device-code (Outlook send + read)

`scripts/outlook_send.py:67-99` runs silent refresh first; falls back to
device-code if no token. QC-023 warns at 60d / errors at 80d. Scopes:
`Mail.Send`, `Mail.Read`, `Files.ReadWrite`. Tenant: `common`,
public-client app id `14d82eec-204b-4c2f-b7e8-296a70dab67e` (Microsoft's
generic Azure CLI client).

#### F-3.1 — MSAL re-auth single-point-of-failure (Medium)
**Files:** `scripts/outlook_send.py:84-99`, `scripts/refresh_stage.py`
If the 80d window is missed while travelling: silent refresh returns
None, `acquire_token_by_device_flow` blocks on stdin (impossible inside
the scheduled task), QC-023 fires Sentry but no email sends. Recovery
requires RDP + interactive auth — no remote path today.
**Fix:** wire `cmd_auth_bg` (already exists,
`outlook_send.py:289-335`) into a `workflow_dispatch` so the operator
can re-auth from the phone. Long-term: app-only auth per
`docs/MOVE-OFF-CLOUDPC.md`. **Effort:** 1-2h for GH dispatch.

#### F-3.2 — Public-client tenant `common` accepts personal accounts (Low)
**File:** `scripts/outlook_send.py:44`
`TENANT = "common"`. Personal `@outlook.com` could bind to the cache;
Graph calls would fail elsewhere. Confusing, not exploitable.
**Fix:** pin tenant ID, or assert `preferred_username` ends
`@ol-usa.com` post-auth. **Effort:** 10 minutes.

### 3.2 GitHub PAT (PR #19 heartbeat)

`docs/CLOUD-PC-HEARTBEAT-SETUP.md:42-58` documents a fine-scoped PAT:
- Resource owner: `IdealX-dev`
- Repo: `IdealX-dev/hilmar-daily-routine` only
- Permissions: **Actions: Read and write**
- Expiration: 90 days

This is minimal and correct. Failure path is correctly graceful:
`deploy/run_daily_laptop.cmd:186-207` wraps `gh workflow run` in
`where gh` + `if errorlevel` and continues. The email has already shipped
before the heartbeat is fired (step 6 in the wrapper) — so a revoked
PAT triggers `liveness.yml` at 11:30 AM ET but does not break the
daily email.

#### F-3.3 — PAT Option B writes PAT to OneDrive-synced secrets file (Low)
**File:** `docs/CLOUD-PC-HEARTBEAT-SETUP.md:50-58`
Option A uses Windows Credential Vault (safe). Option B writes the
literal `ghp_…` into `secrets/github-pat.txt`, which is OneDrive-synced.
**Fix:** mark Option A as default; Option B as fallback only.
**Effort:** 5 minutes doc-only.

### 3.3 Anthropic API key

`scripts/pdf_llm_rescue.py:61-74` resolves `secrets/anthropic-api-key.txt`
then env. Use is bounded:
- PDF image-only rescue (`patch_carriers.py` opt-in only).
- Claude-haiku diagnostic comments on unmapped Sentry issues
  (`scripts/qc_actions_from_sentry.py:418-495`).

`HILMAR_PDF_LLM_BUDGET=20` caps per-run cost. No reported rotation
cadence — F-3.4 below.

#### F-3.4 — Anthropic key has no rotation gate (Medium)
**File:** `scripts/pdf_llm_rescue.py:61`
No QC equivalent to QC-023 for the Anthropic key. Compromised key
works indefinitely; charges silent.
**Fix:** mtime-based QC check (warn 90d / error 180d); Sentry alert on
`tokens_out` >100K/day. **Effort:** 30 minutes.

### 3.4 Sentry auth token

`scripts/sentry_api.py:60-72` validates `sntrys_*` / `sntryu_*` prefix.
No rotation policy.

#### F-3.5 — Sentry auth token has no rotation policy (Low)
Same shape as F-3.4. **Fix:** quarterly rotation reminder + mtime QC.
**Effort:** 15 minutes.

### 3.5 Quote-tracker shared password

`scripts/sync_to_quote_tracker.py:89-96` accepts any non-empty value.
Sync step exits 0 on wrong-password — graceful.

#### F-3.6 — Shared-password is the weakest cross-project link (Medium)
This is the credential most likely matching the in-chat leak (see §8).
Long-lived shared secret used by Hilmar tracker AND Rate Blaster. No
per-caller distinguishability in ol-quote-tracker logs.
**Fix:** per-project API keys against Turso (ol-quote-tracker change).

---

## 4. Run-log + reports leakage

Examined files in `reports/`:

| File | Tracked in git? | PII content? | Risk |
|---|---|---|---|
| `reports/QC-INDEX.md` | yes | no | clean |
| `reports/email-body.html` | gitignored | yes (intended outbound content) | docs note recommended (F-2.6) |
| `reports/improvements-report.html` | gitignored | yes (excerpts log) | F-2.5 |
| `reports/qc-result.json` | gitignored | check-message strings may contain MDOLX | sent over `qc_alert_if_needed.py` to a single OL-USA mailbox — low risk |
| `reports/pytest-output.txt` | gitignored | test fixtures only (no real PII verified) | low |
| `reports/run-log.txt` | gitignored | yes — full pipeline stdout | F-2.5 |
| `reports/coverage.json` | gitignored | no | clean |
| `reports/test-result.json` | gitignored | test data only | clean |

Findings rolled into F-2.5 and F-2.6 above.

---

## 5. Email send / dispatch risks

### 5.1 Send paths inventory

| Caller | Recipient logic | Distribution-list guard |
|---|---|---|
| `deploy/run_daily_laptop.cmd:130` daily email | `--to-from-config` → `config.json.distribution.full_list` (10) | QC-022 + idempotency flag `reports/sent-YYYY-MM-DD.flag` |
| `deploy/run_daily_laptop.cmd:164` improvements report | `--to michael.deitchman@idealx.us` hardcoded | flag `improvements-sent-YYYY-MM-DD.flag` |
| `deploy/qc_alert_if_needed.py:21` | `ALERT_RECIPIENT = "michael.deitchman@ol-usa.com"` constant | none (sends only on QC ≠ CLEAN) |
| `scripts/auto_chase_pending.py:182-188` | `chase_cfg.recipient` (lupfold@…) + CC Michael | max-3/day cap, ≥4 PM ET gate, dedup-per-request flag |
| `scripts/teams_alert.py:48-60` | Teams webhook URL from config (not email) | empty webhook → queue to disk |
| `scripts/outlook_send.py:269` nudge | explicit `--to` only | none |

### 5.2 Findings — dispatch correctness

#### F-5.1 — QC-022 doesn't gate `send_mail()` (Medium)
**Files:** `scripts/qc_selfheal.py` (QC-022), `scripts/outlook_send.py:114-205`
QC-022 runs BEFORE the pipeline. `send_mail()` takes any `to: list[str]`
and POSTs to Graph unguarded. A future caller bypassing `cmd_daily`
could send to an arbitrary list. Idempotency flag prevents duplicate
sends, not wrong-recipient sends.
**Fix:** in `send_mail()`, if `len(to) >= 5`, assert set-equality
against `config.distribution.full_list`. **Effort:** 30 minutes.

#### F-5.2 — `_recipient_type` heuristic misclassifies (Low)
**File:** `scripts/outlook_send.py:182-184`
`len(to)>=5` → "full", `len(to)==1 and endswith @idealx.us` → "audit",
else "test". A 4-recipient send hides in "test"; a CC'd audit misses
"audit". Metric noise only. **Fix:** match against configured lists.
**Effort:** 10 minutes.

#### F-5.3 — `auto_chase_pending.py` hard-codes Lonny fallback (Low)
**File:** `scripts/auto_chase_pending.py:138`
`chase_cfg.get("recipient", "lupfold@hilmaringredients.com")` — removing
config key silently still emails Lonny.
**Fix:** fallback `None`, refuse if missing. **Effort:** 5 minutes.

#### F-5.4 — `cmd_nudge` lacks idempotency (Low)
**File:** `scripts/outlook_send.py:269-279`
Manual subcommand, bounded blast radius. **Fix:** optional per-thread
dedup via reply-to header. **Effort:** 1 hour.

---

## 6. External API trust boundaries

| Edge | Data crossing | Scope |
|---|---|---|
| ol-quote-tracker `/api/intelligence/sync` | Carrier names, lane stats, win/quote counts, Hilmar+Lonny identity, OL operators (`scripts/sync_to_quote_tracker.py:120-192`) | Cross-project (Hilmar → quote-tracker Turso DB). Hilmar Ingredients itself is exposed as a tracked entity, but no rates or MDOLX refs are in the payload. |
| Sentry events | PII-scrubbed messages + tags + breadcrumbs | SaaS; F-2.x findings apply |
| Anthropic API | PDF binary (booking confirmations) + diagnostic prompts (issue titles/culprits) | Both can contain MDOLX numbers — Anthropic's no-training policy applies, but data leaves the perimeter |
| SHARED/client_intelligence (OneDrive folder) | Full quote history JSONL | Same OneDrive scope as Hilmar (Michael's IdealX account) — same blast radius as `tracking-data-v2.json` |
| GitHub Actions (`daily.yml`, `sentry-tools.yml`, `heartbeat.yml`) | Repo contents + GH secrets materialised into `secrets/` per F-1.3 | F-1.3 applies |

### 6.1 Findings — external boundaries

#### F-6.1 — Anthropic diagnose prompt trusts pre-scrubbed Sentry fields (Medium)
**File:** `scripts/qc_actions_from_sentry.py:453-464`
Prompt includes `title`, `culprit`, `level`, `permalink`. `title` /
`culprit` are pre-scrubbed by `_before_send` — but only if the regexes
are complete; F-2.1/F-2.2/F-2.3 show known gaps.
**Fix:** second-pass `_scrub_string` on `title`/`culprit` before the
Anthropic call. **Effort:** 10 minutes.

#### F-6.2 — `share_intel.py` fallback path can write outside OneDrive (Low)
**File:** `scripts/share_intel.py:60-67`
Probes `OneDrive - IdealX` → `OneDrive` → parent-of-Hilmar. On a
non-OneDrive machine the third fallback lands somewhere arbitrary.
**Fix:** refuse to write if no OneDrive-shaped path resolves.
**Effort:** 10 minutes.

---

## 7. Public exposure — what's committed

`git ls-files` returned 175 tracked files. Spot-checked for real PII:

| Where | Real PII committed? |
|---|---|
| `config.json:7,121,147` | `lupfold@hilmaringredients.com` is the real Lonny address (acceptable — it's the operational contact, in the same scope as the public client info) |
| `CLAUDE.md`, `RUNBOOK.md` | same email referenced (same justification) |
| `scripts/sync_to_quote_tracker.py:72-86`, `auto_chase_pending.py:138`, `build_ops_flow_inquiries.py`, `refresh_stage.py:81` | same email in source (constants) |
| `tests/test_ingest.py:69-70` | `LONNY` and `MICHAEL` real addresses used as test constants. The MDOLX values used (`MDOLX260420`, `MDOLX260317`, `MDOLX260460`, `MDOLX260999`, `MDOLX1234567`) — F-7.1 below. |
| `scripts/build_real_sample.py:31` | **Authentic Outlook conversation ID committed** — F-7.2 below. |

### 7.1 Findings — public exposure

#### F-7.1 — Test MDOLX values likely real (Medium)
**File:** `tests/test_ingest.py` (multiple lines under 781-1277)
`MDOLX260317/260420/260460/260999/260062` follow Hilmar's real
`MDOLX26NNNN` format (26 = year-2026). Carrier refs `RICGE7217600`,
`NAM8400958`, `EBKG14800694` are authentic-shaped. Committed to
`IdealX-dev/hilmar-daily-routine`.
**Fix:** confirm repo visibility. If public, rotate to obviously-fake
placeholders (`MDOLX999001`, `NAM0000001`) and rewrite history.
**Effort:** 1h replacement; +1-2h for filter-repo.

#### F-7.2 — Real Outlook conversation ID committed (High)
**File:** `scripts/build_real_sample.py:31`
`AAQkAGQ3MjcwNWZhLTk3M2YtNDYzOS1hMWZlLWQwMmYzODE0MTU5NQAQAKhptuZ9dXlJsvDHRW2ubYA=`
is a valid Outlook conv ID — the exact pattern the Sentry scrubber
redacts. Maps to a specific thread in the operator's mailbox;
possession + Graph access = thread query.
**Fix:** replace with fake placeholder; `git filter-repo`/BFG if the
repo is public. **Effort:** 30min file; 1-2h history rewrite.

#### F-7.3 — `golden_day.json` provenance not documented (Low)
**File:** `tests/fixtures/golden_day.json`
Spot-grep did not surface PII, but the file was not fully read.
**Fix:** document provenance in a fixture header. **Effort:** 10 min.

---

## 8. Mitigation for the in-chat credential leak ("the leaked credential in chat")

Earlier today the operator pasted what looked like a credential into
chat. It was not echoed or stored by the assistant, but it lives in the
session transcript. Likely targets, in order of probability:

1. **ol-quote-tracker shared password** (`quote-tracker-pwd.txt`) —
   highest probability: only project credential stored as a
   human-typeable string rather than a provider token, and
   `sync_to_quote_tracker.py:91` only validates non-empty.
2. **Microsoft / OneDrive account password** — broader blast radius
   (Cloud PC, OneDrive, Outlook).
3. **OL-USA SSO / VPN password** — highest blast radius. Confirm.
4. Other (Teams, Sentry web login, GitHub web login).

### Quick-action checklist

1. **Identify** which credential it is.
2. **Rotate now**, before any other follow-up:
   - **quote-tracker pwd** → change `APP_PASSWORD` env on
     `ol-quote-tracker-prod.azurewebsites.net`; update
     `secrets/quote-tracker-pwd.txt`; update `QT_APP_PASSWORD` GH
     Actions secret if set; `python scripts/sync_to_quote_tracker.py
     --dry` to verify.
   - **Microsoft account** → reset at account.microsoft.com; rerun
     `python scripts/outlook_send.py auth` on the Cloud PC.
   - **OL-USA SSO** → file with OL IT.
3. **Audit last 24h** of activity on the affected service (MS sign-in
   logs, Azure App Insights, Sentry audit log).
4. **Add QC-051 (secrets-rotation)**: track each `secrets/*.txt` mtime,
   surface "older than 90d" in the daily audit.
5. **Future sessions**: paste credentials with a `<PWD>` placeholder
   substitution and rotate after each session as practice.

The leak is **Critical** if it is #2 or #3; **High** if #1. Treat as
Critical until rotated.

---

## Top 5 security priorities

In order of expected risk-reduction per hour of work:

1. **Rotate the in-chat-leaked credential NOW** (§8). Critical if MS
   account or OL SSO. Effort: 15-60 min depending on credential.

2. **Replace the real Outlook conversation ID in
   `scripts/build_real_sample.py:31`** (F-7.2). High. Effort: 30 min
   + history rewrite if repo is public.

3. **Harden CI secret materialization in
   `.github/workflows/sentry-tools.yml`** (F-1.3). Set `umask 077`
   before `printf` writes, add `if: always()` cleanup. High —
   shortest path for any compromised workflow to escalate.
   Effort: 10 minutes.

4. **Close the PII-scrubber gaps in `scripts/sentry_setup.py`**
   (F-2.1, F-2.2, F-2.3) so Sentry behaviour matches the CLAUDE.md
   contract. Also cuts the F-6.1 downstream leak surface to Claude.
   Effort: 30 minutes total.

5. **Add a `send_mail()`-level distribution allowlist assertion**
   (F-5.1) so future callers that bypass `cmd_daily` can't reach the
   10-recipient distribution with the wrong list. Effort: 30 minutes.

Next tier: F-2.5 (`run-log.txt` scrubbing in audit emails), F-3.1
(remote MSAL re-auth path), F-7.1 (rotate test MDOLX values), F-3.4
(Anthropic-key rotation QC).
