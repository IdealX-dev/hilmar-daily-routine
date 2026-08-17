---
name: hilmar-daily-tracker
description: >-
  Hilmar Daily Shipment Tracker — the daily-fire pipeline that ingests Outlook
  emails between Lonny Upfold (Hilmar Ingredients) and OL-USA into a daily
  shipment-tracker email + dashboard + PDF + private audit. Use this skill
  WHENEVER Michael mentions Hilmar, the Hilmar tracker, the daily shipment
  tracker, Lonny Upfold, the OL-USA booking pipeline, MDOLX bookings, the
  daily fire (Mon-Fri 8 AM ET, reporting the prior business day), Hilmar
  parser accuracy, the Hilmar QC checks (QC-001..QC-077), or Sentry/Seer for
  Hilmar — or wants to run, check, debug, explain, or report on the Hilmar
  pipeline from ANY device including the iPhone Claude app. Also trigger on
  "run the Hilmar pipeline", "check Hilmar QC", "send the Hilmar sample",
  "Hilmar pipeline status", "review the Hilmar audit", or
  tracking-data-v2.json. Single source of truth for the project — consult it
  before any Hilmar work so paths, commands and the current operating state
  are correct on whatever device you are on.
---

# Hilmar Daily Shipment Tracker

A production pipeline that fires **Mon–Fri ~8:07 AM ET** via **GitHub
Actions** (repo `IdealX-dev/hilmar-daily-routine`), each fire reporting the
PRIOR business day (Mon→Fri, Tue→Mon, … Fri→Thu). It ingests the Outlook
traffic between **Lonny Upfold** (Hilmar Ingredients) and **OL-USA** and
produces:

- A **daily shipment-tracker email** (staff distribution)
- An interactive **HTML dashboard** + **combined PDF** + carrier scorecards
- A **client-facing email** for Lonny (separately gated)
- A **private systems-audit email** to `michael.deitchman@idealx.us` only

## Step 0 — start CURRENT, not stale

1. **Read the top two entries of `CHANGELOG.md` before acting.** It is the
   session log; every session ends by writing what changed and what is open.
   Facts in your memory of this project may be days stale — the changelog is
   not.
2. **`CLAUDE.md` governs.** Verify before asserting; "working / fixed /
   sent" requires proof generated this session. The specific failure that
   rule exists for happened here: shared-mailbox access was reported
   "working" when only the token had been checked, never an actual message.
   A capability claim requires observed OUTPUT (messages returned, mail
   sent, rows changed) — never a passed precondition.
3. **There is no Cloud PC and no OneDrive mirror.** Both are decommissioned.
   The git repo IS the deployable; GitHub Actions is the only production
   host. If you find instructions telling you to copy scripts to
   `PROJECT HILMAR/` — including old copies of this skill — they are dead.

## The operating model (2026-08 reality)

- **Code:** `scripts/` in the repo is production (imported directly by the
  fires). `src/hilmar/` is the mirrored library; parity is enforced by
  `tests/test_core_parity.py` and QC-040. Edit both when touching shared
  logic. Diagnose against `scripts/`, never the mirror.
- **State lives in the Azure blob store** (container `hilmar-state`), not in
  the repo: `tracking-data-v2.json`, `scripts/stage_emails.txt`,
  `scripts/stage_emails_bodies.txt`, `secrets/token-cache.*`,
  `data/quote-history.db`. Every fire pulls state → runs → pushes it back.
  A fresh clone has NO data; to inspect real state, dispatch `diag-blob.yml`
  (read-only) and read its log.
- **Ingest rebuilds rather than merges.** Rows are rebuilt from staged mail
  each fire; `scripts/operator_corrections.json` is the ONLY durable human
  state (create/exclude/set per request_id). Never let one request_id carry
  both a `create` and an `exclude` — outcome would depend on file order
  (tests pin this).
- **Auth is delegated device-code** as `michael.deitchman@ol-usa.com`
  (Microsoft Graph Command Line Tools, client `14d82eec-…`), token cache
  synced through the blob. OL IT declined app-only; `GRAPH_APP_*` stays
  empty. Re-seed via the `auth-refresh.yml` workflow (code is emailed AND
  printed in the run log).
- **Mailboxes — SETTLED, do not reopen:** intake reads `/me`
  (Michael's mailbox) and that is now permanent. `Mail.Read.Shared` IS
  consented (2026-08-13), but `MBD_OceanExportBookingShared` returns 404
  store-not-found on every folder, which needs Full Access to clear.
  Michael, 2026-08-14: **"ol won't grant more access."** That closes it —
  the OWA self-test and the IT request are both dead leads; do not propose
  them again. `HILMAR_READ_SHARED_ONLY` now DEFAULTS to `"false"` in code
  (it defaulted to `true`, the one setting that cannot work, and a fire
  went blind and green on it). `seed-shared-mailbox.yml` is dead code kept
  only as a record. A mailbox yielding zero for a whole window is a red
  error, never a quiet day.
- **The tracker is NOT blind to OL's replies — measured, 2026-08-15.** An
  earlier version of this file claimed the opposite ("the visibility gap is
  PERMANENT… the count re-inflates… only a process fix"). That was wrong,
  and it was wrong twice: first as a theory built on the shared mailbox's
  404, then repeated to Michael as fact. He corrected it — **"i am in the
  group email so i see them"** — and diag-blob run 31877434357 backs him,
  not me:

      response_timestamp by ET day   … 2026-08-12: 5, 2026-08-13: 5
      turnaround, 294 dated rows     254 in 0-4h, 0 negative
      rate responses in transitions  08-12: 3, 08-13: 1, 07-22: 2 …

  Michael is on the distribution, the group copy lands in his OL mailbox,
  and `/me` is exactly what intake reads — so OL's replies DO arrive and
  ARE captured, usually within four hours. The mechanism is already in
  `classify()`: an OL-domain sender with Lonny on the message, plus the
  `LonnyThreads` linkage for forwards that strip him.
- **What the 2026-08-14 spike actually was.** 255 transitions in one day —
  33 "OL-USA never responded", 33 "Send but no MDOLX" — reads like mass
  blindness and is not. The reasons carry their own age: *"Quoted 2952.7h
  ago"* is 123 days. It is the aging sweep catching up on APRIL rows, not
  33 new failures. Do not quote that number as a current-week signal.
- **The genuinely undated rows are 60, and mostly not email at all:** 49
  have no `source_imids` whatsoever (they entered from OL's transaction
  report, never as mail — nothing to date them with), 10 link only to
  Lonny's own ask, and 1 is unexplained and worth chasing. That is the
  QC-077 population; it is a backlog, not a live leak.
- **The win/loss ledger reconciles to OL's own book:**
  `data/ol-transaction-report-2026.json` (Michael's transaction report) is
  the authority for bookings; absence from it IS the cancellation signal.
  `ol_*`/`stand_*` rows are standalone bookings with no RFQ chain (~50
  predate the tracker's email history and stay standalone — do not invent
  links; `diag_match_standalones` proposes, humans apply).
- **Timing:** `core.TIMING_VALID_FROM = ""` — the turnaround clock is ON.
  Turnaround above 40 biz-hours is excluded from statistics (date kept).
  Re-arming the clock is a one-line change; do not delete the mechanism.

## Distribution gate — the rule that must never be guessed

`HILMAR_REPORTS_PAUSED` (in `daily.yml`, `weekly.yml`, `liveness.yml` — a
test asserts all three agree) is three-state:

| value | behaviour |
|---|---|
| `"true"` | hard stop — crons commented out too (both halves or neither) |
| `"verify_only"` | runs on schedule, scans mail, **sends ONLY to michael.deitchman@idealx.us** |
| `"false"` | live — staff distribution + gated client email |

**Current: `"false"` (LIVE), since 2026-08-17.** Michael, unprompted and
explicit: "release all reports to everyone and resend them.. they looked
fine this morning." That is the unambiguous approval the gate always
required — distinct from a bare "go"/"send" after other discussion, which
is NOT that call and still means ask. The client email to Lonny is
additionally gated by `config.json → client_report.enabled`, which has been
`true` since 2026-07-12 — so flipping this ONE switch released both the
staff list AND the client email in the same fire. Full staff list lives in
`config.json → distribution.full_list`; do not assume who is on it without
reading it.

Rolling back to `verify_only` is the one-line reverse of this edit, in all
three workflows (the test that enforces they agree will catch a partial
revert).

## What you can do, from anywhere (including the phone)

The pipeline cannot run locally (secrets exist only in Actions). Everything
operational is a **workflow dispatch** on the working branch:

| Intent | Do this |
|---|---|
| Fire a test report (Michael only) | dispatch `daily.yml` — `mode=production-fire`, `send_to=test`, `force_resend=true` |
| Inspect the real stored data | dispatch `diag-blob.yml`, read the job log |
| Re-seed / widen the Graph token | dispatch `auth-refresh.yml` (`confirm=REAUTH`; `include_shared` only when told) |
| Bulk backfill a new mailbox | dispatch `seed-shared-mailbox.yml` (writes stage caches ONLY) |
| Run tests / lint locally | `pytest tests/ --no-cov -q` · `ruff check scripts/ src/ tests/ deploy/` |

Details and exact input names: `references/commands.md`. The 16-step
pipeline: `references/pipeline.md`. Parser/QC/data model:
`references/architecture.md`.

## Hard rules (do not violate)

1. **Sends.** Live since 2026-08-17 — every scheduled and manual
   `send_to=full` fire now reaches the real staff list and Lonny. If the
   gate is ever set back to `verify_only`, every send goes to Michael alone
   and the workflow forces it in two places; do not "fix" that. Any future
   distribution change (going live again, adding/removing recipients)
   requires Michael's explicit, unambiguous approval. Never send a test to
   the staff list or Lonny.
2. **Timestamps in chat = Eastern Time.** Code stays UTC; chat converts.
3. **Parser accuracy is a 95% hard gate** (QC-039). Fix the parser, never
   lower the gate.
4. **Every QC is checked, self-healed, and ROOT-fixed — a constant.** New
   check ⇒ same commit carries QC-INDEX row + Sentry route + regression test
   (`tests/test_qc_governance.py` enforces mechanically).
5. **Trees stay paired.** `scripts/` ↔ `src/hilmar/` drift is a bug
   (QC-040, parity tests). No OneDrive copies exist anymore.
6. **Never greenfield.** Refactor this repo; never start a parallel one.
7. **Destructive or outward-facing actions** (deletes, sends beyond Michael,
   deploy/config flips that widen audience) need explicit written approval
   first — CLAUDE.md governs.
8. **End of session:** update `CHANGELOG.md` (decisions by name, corrections
   included) and this skill if the operating model changed. That is what
   makes Step 0 work for the next session.

## Quick orientation

- **Repo:** `github.com/IdealX-dev/hilmar-daily-routine` (default `main`;
  session work on `claude/...` branches, PRs, standing merge-when-green
  authority from Michael)
- **Pipeline entry:** `scripts/run_pipeline.py` (invoked by `daily.yml`)
- **Data:** ~380–390 request rows, wins reconciled to OL's 2026 book
- **QC:** QC-001 .. QC-077 (contiguous, some variants)
- **Observability:** Sentry org `idealx-llc` / `hilmar-daily-tracker` + Seer
- **Suite:** ~3,150+ tests; `--no-cov` for the plain run (the cov gate
  measures `src/hilmar` only)
- **Key people:** Lonny Upfold (client buyer), Linda Echevarria (OL desk,
  quotes from the shared address), Michael Deitchman (owner — reports run
  to him only until he says otherwise)

## The data model in one paragraph

Lonny emails an RFQ ("Oakland to Yokohama, 2x40'HC"). OL responds with a
rate (→ row is quoted; PENDING_HILMAR while Lonny decides). Lonny books
(→ **WIN**, MDOLX number) or the quote ages out (→ **Q&L**) or OL never
answers within its window (→ **NQ**). Storage may be LEGACY
(`LOSS`+`quoted`) or STRICT (`Q&L`/`NQ`) — always classify through
`core.display_status` / `pending_substate`, never raw `status`. One row per
RFQ in `tracking-data-v2.json`; Win Rate = Wins / (Wins + Q&L). Bookings
with no RFQ email are standalone `ol_*`/`stand_*` rows. Full field list and
state machine: `references/architecture.md`.
