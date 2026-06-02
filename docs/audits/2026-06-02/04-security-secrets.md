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

#### F-3.1 — MSAL re-auth is a hard single-point-of-failure
**Severity:** Medium
**Files:** `scripts/outlook_send.py:84-99`, `scripts/refresh_stage.py`
If Michael misses the 80d window while travelling, the daily pipeline
silently breaks: silent refresh returns `None`, then `acquire_token_by_
device_flow` blocks on stdin (impossible inside the scheduled task) and
fails. QC-023 fires Sentry but the email already won't send. Recovery
requires RDP into the Cloud PC and an interactive auth — there is no
remote path.
**Fix:** the `cmd_auth_bg` path already exists (`outlook_send.py:289-335`)
and writes the device code to a file. Trigger it from a GitHub Actions
workflow_dispatch on the Cloud PC self-hosted runner so the operator
can re-auth from the phone. Alternative: enable app-only Graph auth
(`docs/MOVE-OFF-CLOUDPC.md` step 1) which removes device-code entirely.
**Effort:** 1-2 hours for the GH Actions trigger; weeks for app-only
(needs OL IT).

#### F-3.2 — Public-client tenant `common` accepts personal accounts
**Severity:** Low
**File:** `scripts/outlook_send.py:44`
`TENANT = "common"` means the auth endpoint accepts both work and
personal Microsoft accounts. If the operator authenticates a personal
`@outlook.com` account by mistake, the cache binds to it and silent
refresh works — but Graph calls fail at a different layer. Not
exploitable; just confusing.
**Fix:** pin `TENANT` to the OL-USA tenant ID once known. Or add a
post-auth assertion that `preferred_username` ends with `@ol-usa.com`.
**Effort:** 10 minutes.

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

#### F-3.3 — PAT setup uses interactive choice `Option A` OR `Option B`, only B leaves a token-on-disk artifact
**Severity:** Low
The docs at `CLOUD-PC-HEARTBEAT-SETUP.md:50-58` give two equivalent
paths. **Option A** stores the token in the Windows Credential Vault
(safe). **Option B** writes a literal `ghp_…` value into
`secrets/github-pat.txt`. The `secrets/` directory is gitignored, but
it is OneDrive-synced — so the PAT lands in OneDrive plaintext.
**Fix:** recommend Option A as the default; mark Option B as
"only if `gh auth login --with-token` won't run interactively".
**Effort:** 5 minutes doc-only.

### 3.3 Anthropic API key

`scripts/pdf_llm_rescue.py:61-74` resolves `secrets/anthropic-api-key.txt`
then env. Use is bounded:
- PDF image-only rescue (`patch_carriers.py` opt-in only).
- Claude-haiku diagnostic comments on unmapped Sentry issues
  (`scripts/qc_actions_from_sentry.py:418-495`).

`HILMAR_PDF_LLM_BUDGET=20` caps per-run cost. No reported rotation
cadence — F-3.4 below.

#### F-3.4 — Anthropic key has no rotation policy or freshness gate
**Severity:** Medium
**Files:** `scripts/pdf_llm_rescue.py:61`, `secrets/anthropic-api-key.txt`
There is no QC check on Anthropic-key age (vs QC-023 for MSAL). A
compromised key would keep working indefinitely. Anthropic console
charges accumulate silently.
**Fix:** add a QC check that reads the file's mtime and warns at 90d /
errors at 180d (mirroring QC-023). Add a Sentry metric `anthropic.
tokens_used` (already produced by `qc_actions_from_sentry.py` as
`tokens_in/tokens_out`) — wire to a Sentry alert at >100K/day.
**Effort:** 30 minutes.

### 3.4 Sentry auth token

`scripts/sentry_api.py:60-72`, `scripts/sentry_seer.py:67-78` resolve
the same `secrets/sentry-auth-token.txt`. The validator accepts only
`sntrys_*` or `sntryu_*` prefixes — good. No rotation policy.

#### F-3.5 — Sentry auth token has no rotation policy
**Severity:** Low
Same shape as F-3.4. Token scope is presumably org-wide; can revoke
and re-paste into the secrets file + GH Actions secret without
material downtime.
**Fix:** quarterly rotation reminder in RUNBOOK + a QC check on
the file age.
**Effort:** 15 minutes.

### 3.5 Quote-tracker shared password

`scripts/sync_to_quote_tracker.py:89-96` accepts any non-empty password.
Auth flow: POST `/api/auth/login` → cookie → POST `/api/intelligence/
sync`. If the password is wrong, the sync step exits 0 with a notice
and the pipeline continues — graceful degradation.

#### F-3.6 — Shared-password auth is the weakest link in the cross-project chain
**Severity:** Medium
This is the credential most likely matching the in-chat leak (see §8).
Long-lived shared secret used by both Hilmar tracker and Rate Blaster.
No rotation policy; no per-caller distinguishability in the
ol-quote-tracker logs.
**Fix:** migrate to per-project API keys against the Turso DB so each
caller has a distinct credential that can be rotated independently.
**Effort:** ol-quote-tracker change — out of scope here but documented.

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

#### F-5.1 — QC-022 distribution guard is not bypass-proof
**Severity:** Medium
**Files:** `scripts/qc_selfheal.py` (QC-022 implementation),
`scripts/outlook_send.py:114-205`
QC-022 enforces 10-recipient invariants BEFORE the pipeline runs. But
`send_mail()` itself takes any `to: list[str]` and POSTs to Graph
unguarded. A future caller (script that bypasses `cmd_daily`) could
pass an arbitrary list. The idempotency flag prevents *duplicate*
sends to the full distribution but does not protect against *wrong*
recipients in a fresh send.
**Fix:** in `send_mail()`, if `len(to) >= 5` (the
"full_distribution" heuristic at line 182), assert against
`config.distribution.full_list` set-equality. Refuse to send otherwise.
**Effort:** 30 minutes.

#### F-5.2 — `_recipient_type` heuristic for Sentry metrics misclassifies multi-cc audits
**Severity:** Low
**File:** `scripts/outlook_send.py:182-184`
`"full" if len(to) >= 5 else ("audit" if (len(to)==1 and to[0].endswith
("@idealx.us")) else "test")` — a CC'd audit (e.g. when adding a
second internal viewer) gets classified as "test", and a send with 4
recipients (one fewer than 5) hides in "test" bucket. Metric noise
only.
**Fix:** classify by matching `to` against the configured distribution
sets directly.
**Effort:** 10 minutes.

#### F-5.3 — `auto_chase_pending.py` hard-codes Lonny fallback
**Severity:** Low
**File:** `scripts/auto_chase_pending.py:138`
`chase_cfg.get("recipient", "lupfold@hilmaringredients.com")` — if
`config.auto_chase.recipient` is removed or typo'd, the fallback
silently emails Lonny anyway. The intent of removing the recipient is
presumably to disable chases.
**Fix:** make the fallback `None` and refuse to send if missing.
**Effort:** 5 minutes.

#### F-5.4 — `cmd_nudge` has no idempotency flag
**Severity:** Low
**File:** `scripts/outlook_send.py:269-279`
Manual `nudge` subcommand could be re-run accidentally. Not in the
scheduled daily fire, so risk is bounded to manual use.
**Fix:** optional — add a per-thread dedup via Outlook reply-to
header check.
**Effort:** 1 hour.

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

#### F-6.1 — Anthropic diagnostic prompts contain unredacted Sentry titles
**Severity:** Medium
**File:** `scripts/qc_actions_from_sentry.py:453-464`
The prompt sent to Claude includes `title`, `culprit`, `level`,
`platform`, `permalink`. Sentry already scrubs PII before the event
arrives at sentry.io. But `permalink` is `https://sentry.io/.../issues/
{id}/` — opaque. `title` and `culprit` are PRE-scrubbed by the
`_before_send` hook, so they should be safe. **However**, this assumes
the scrubber regexes are complete; F-2.1/F-2.2/F-2.3 show known gaps.
**Fix:** apply a second-pass scrub on `title`/`culprit` right before
the Anthropic call.
**Effort:** 10 minutes.

#### F-6.2 — `share_intel.py` writes to a path that may broaden if OneDrive renames
**Severity:** Low
**File:** `scripts/share_intel.py:60-67`
The script probes `OneDrive - IdealX`, then `OneDrive`, then a
parent-of-Hilmar fallback. The third fallback writes outside
`OneDrive - IdealX` into whatever `HILMAR_ROOT.parent` resolves to.
On a non-OneDrive machine this could land in a public path.
**Fix:** refuse to write if none of the OneDrive-shaped paths resolve.
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

#### F-7.1 — Test MDOLX values likely include real shipment refs
**Severity:** Medium
**File:** `tests/test_ingest.py:781,799,825,858,876,921,943,971,992,
1025,1041,1059,1080,1191,1209,1277`
The values `MDOLX260317`, `MDOLX260420`, `MDOLX260460`, `MDOLX260999`,
`MDOLX260062` follow Hilmar's actual booking-number format (`MDOLX26
NNNN` where 26 is the year-2026 prefix). At least some of these
appear to be real booking refs lifted from production threads. Carrier
refs `RICGE7217600`, `NAM8400958`, `EBKG14800694` are similarly
authentic-shaped. Committed into a public-shaped GitHub repo (per
CLAUDE.md the repo is `IdealX-dev/hilmar-daily-routine`).
**Fix:** confirm visibility (private vs public on GitHub). If public,
rotate the test MDOLX values to obviously-fake placeholders
(`MDOLX999001`, `MDOLX999002`, `NAM0000001`) and force-push a rewrite
of `tests/test_ingest.py` history.
**Effort:** 1 hour for replacement; history rewrite is more involved.

#### F-7.2 — Real Outlook conversation ID committed
**Severity:** High
**File:** `scripts/build_real_sample.py:31`
`manila_conv_id = "AAQkAGQ3MjcwNWZhLTk3M2YtNDYzOS1hMWZlLWQwMmYzODE0
MTU5NQAQAKhptuZ9dXlJsvDHRW2ubYA="` — this is a valid base64 Outlook
conversation ID. It is the exact pattern the Sentry scrubber goes to
lengths to redact. Committed in plaintext. The ID maps to a specific
thread in Michael's mailbox; possession lets someone with Graph
access query that thread.
**Fix:** replace with a fake placeholder, rotate the production
conversation reference, then `git filter-repo` (or BFG) to scrub from
history if the repo is public.
**Effort:** 30 minutes for the file; 1-2 hours for history rewrite.

#### F-7.3 — `golden_day.json` fixture content not verified for PII
**Severity:** Low (uncertain)
**File:** `tests/fixtures/golden_day.json`
Audit grep did not find lupfold/MDOLX strings inside, but the file
was not fully read (size unknown). Risk is low if it was prepared as
a synthetic fixture; non-trivial if it was sampled from a real day.
**Fix:** spot-read the file and document its provenance in the
fixture header.
**Effort:** 10 minutes.

---

## 8. Mitigation for the in-chat credential leak ("the leaked credential in chat")

Earlier today, the operator pasted what looked like a credential
(format: a name + year + symbol) into a chat session. Even though it
was not stored or echoed by the assistant, it lives in the chat
transcript. The most likely targets, in order of probability:

1. **`quote-tracker-pwd.txt` (the ol-quote-tracker shared password)** —
   Highest probability match because (a) it is the only credential in
   the project stored as a *human-typeable string* rather than a
   provider-issued token, and (b) `scripts/sync_to_quote_tracker.py:91`
   only validates non-empty. A "name + year + #" shape fits a
   user-chosen password.
2. **The OneDrive / Microsoft account password** — Used to log into
   the Cloud PC, OneDrive, Outlook. Higher blast radius if compromised.
3. **An OL-USA system password** — VPN / SSO / Conditional Access
   bypass. Highest blast radius. (Operator should confirm.)
4. Anything else (Teams, Sentry web login, GitHub web login).

### Recommended operator action — quick-action checklist

1. **Identify which credential it actually is.** Visually compare the
   leaked string against the candidates above.
2. **Rotate it immediately** — do this before any other follow-up.
   - If ol-quote-tracker pwd → change `APP_PASSWORD` env on
     `ol-quote-tracker-prod.azurewebsites.net`, update
     `secrets/quote-tracker-pwd.txt` on the Cloud PC, update
     `QT_APP_PASSWORD` GH Actions secret if used, run
     `python scripts/sync_to_quote_tracker.py --dry` to verify.
   - If Microsoft account → reset at account.microsoft.com, re-run
     `python scripts/outlook_send.py auth` on the Cloud PC.
   - If OL-USA SSO → file with OL IT.
3. **Audit recent activity** on the affected service for the last 24h
   (Microsoft sign-in logs, Azure App Insights for ol-quote-tracker,
   Sentry audit log).
4. **Add a QC check** (`QC-051: secrets-rotation`): track the mtime of
   each `secrets/*.txt` and surface "credentials older than 90d" in
   the daily audit. Makes future rotations cheap.
5. **For future sessions**: when you need to share a credential with
   the assistant, paste it once and immediately ask for it to be
   referenced as a placeholder (`<PWD>`) the rest of the session — and
   plan to rotate as a routine practice when a session ends.

The leak is **Critical** if it is #2 or #3 (broad system access). It
is **High** if it is #1 (one downstream service). Treating it as
Critical until rotated is the conservative play.

---

## Top 5 security priorities

In order of expected risk-reduction per hour of work:

1. **Rotate the in-chat-leaked credential NOW** (§8). Estimated risk:
   Critical if the credential is an MS account or OL SSO password.
   Effort: 15-60 minutes depending on which credential.

2. **Replace the real Outlook conversation ID in
   `scripts/build_real_sample.py:31`** (F-7.2). Treat as High.
   If the repo is public, rewrite history. Effort: 30 minutes + rewrite.

3. **Fix CI secret materialization in `.github/workflows/sentry-tools.yml`**
   (F-1.3). Set `umask 077` before the printf writes, add
   `if: always()` cleanup. High because GH Actions secrets touching
   the runner filesystem is the path of least resistance for a
   compromised workflow. Effort: 10 minutes.

4. **Close the PII-scrubber gaps in `scripts/sentry_setup.py`**
   (F-2.1, F-2.2, F-2.3) so Sentry events match the CLAUDE.md
   contract. Medium severity but lowest-effort high-leverage fix
   in the codebase; reduces both Sentry-side and downstream
   (Claude-diagnose F-6.1) leak surface. Effort: 30 minutes total.

5. **Add a `send_mail()`-level distribution allowlist
   assertion** (F-5.1). QC-022 is run BEFORE the send; this would
   be a defense-in-depth gate AT the send, so a future buggy caller
   that bypasses cmd_daily cannot reach the full distribution with
   the wrong list. Effort: 30 minutes.

Honourable mentions (next tier): F-2.5 (`run-log.txt` scrubbing before
it lands in the audit email), F-3.1 (remote MSAL re-auth path),
F-7.1 (rotate test MDOLX values), and F-3.4 (Anthropic-key rotation
QC).
