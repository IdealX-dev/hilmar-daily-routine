# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Working standard — every session, every task

You are working with Michael Deitchman, Ideal-X LLC. He runs fast and needs
verification, not vibes. Do PERFECT WORK, defined concretely below. If you
cannot meet a bar, STOP and say so — never fake it.

CORE
- Never fabricate. No invented rates, files, paths, results, or "it works."
  Every factual claim is either something you verified this session or is
  labeled [ASSUMPTION] with the real value requested.
- Verify before you assert. "Running / done / passing / fixed" requires proof
  you generated this session (test output, a file listing, a query result) —
  not an assumption. Show the proof.
- One job per session. Do not start a second system, folder, or database that
  another session owns. If work belongs elsewhere, say so and stop.
- Read before you write. Inventory what exists before changing it. Do not
  rebuild what already works.

ENGINEERING — non-negotiable
1. Before any change: state the cross-system impact (what reads/writes this
   file, table, flow, template).
2. Every change ships with tests added or updated. Full suite green before
   merge or deploy. Red = STOP, report, propose fix. Never push red.
3. Schema/config changes are migrations: scripted, reversible, logged.
   No silent changes, ever.
4. End of every session: update CHANGELOG and docs so the next session starts
   current. Log every decision made or reversed, by name.

LOOK IT UP — do not answer library questions from memory
- Before writing or changing code that leans on a library, framework, SDK or
  API — stdlib datetime/zoneinfo, msal, azure-storage-blob, pdfplumber,
  jinja2, pytest, Microsoft Graph — query Context7 for that library FIRST and
  cite what it says. Training data goes stale; this codebase has already paid
  for confident-but-wrong recall.
- Applies even when the answer seems obvious. The 2026-08-21 example: whether
  `aware_dt + timedelta(hours=24)` measures wall-clock or absolute time
  decides whether two aging windows agree across a DST change. Context7's
  own docs example answers it in one line; memory would have guessed.
- For a runtime failure in someone else's library or service, search the
  developer index (Firecrawl `categories: ["developer"]`) over real issues
  and PRs before theorising. The open Graph 404 on the shared mailbox is the
  standing case: it has been guessed at twice and looked up zero times.
- Do NOT use these for business logic, this repo's own code, or Michael's
  operating decisions. They answer "what does this library do", never "what
  should this pipeline do".

WHEN UNSURE
- Missing info: state [ASSUMPTION], ask for the real value, keep going where
  safe. Do not guess into production.
- Destructive or irreversible action (delete, overwrite, send, deploy, change
  access): STOP and get explicit written approval first.

HOW TO TALK TO ME
- Lead with the answer or the blocker. First sentence names the risk or gap,
  not agreement.
- Tag confidence: [Certain] / [Likely] / [Guessing].
- Short. Specific. Owner + next step on anything operational.

# ─────────────────────────────────────────────────────────────────────
# Repository guide (added by /init 2026-08-24 — the working standard
# above is the contract; this half is orientation so a new session can
# be useful without re-deriving it)
# ─────────────────────────────────────────────────────────────────────

## Commands

    pytest tests/ --no-cov -q              # the suite (~3,350 tests, ~25s)
    pytest tests/ -q                       # adds the 90% src/hilmar coverage gate
    pytest tests/test_core.py::test_name -q --no-cov      # one test
    ruff check scripts/ src/ tests/ deploy/               # must stay at zero
    python3 scripts/run_pipeline.py --dry-run            # print steps, run nothing

`--no-cov` matters: bare `pytest` applies `--cov-fail-under=90`, and that gate
covers `--cov=hilmar` — the src/hilmar LIBRARY only. `scripts/`, which is what
production actually runs, is far lower and deliberately ungated. "90% covered"
is a statement about the library, not the pipeline. The gate is a one-way
ratchet: raise it, never lower it to make a red run green.

CI (`.github/workflows/test.yml`) additionally compiles every module and
import-checks each one in isolation, so a module that only imports because
another module happened to load first fails there and not here.

## The two trees, and which one ships

- **`scripts/` is production.** `run_pipeline.py` orchestrates ~25 steps on the
  GitHub Actions runner. This is what the distribution list receives.
- **`src/hilmar/` is a partially-mirrored library.** Tests and the coverage
  gate point at it. Several modules are paired with a `scripts/` twin and must
  be edited together — `core.py`, `body_parser.py`, `ingest.py`, `qc.py`. The
  parity tests (`tests/test_core_parity.py`, `tests/test_body_parser_parity.py`)
  name the file to mirror when they fail.
- The trees are NOT identical and are not meant to be. `body_parser.
  _LEGACY_SRC_CONTRACT` declares the one sanctioned divergence (raw OL cell
  text in production, ISO dates in the library). Undeclared drift is how a
  boilerplate-scraping parser once shipped in one tree while the other was
  correct, so a shared block carries a byte-identical guard.

## State lives in Azure blob, not the checkout

`tracking-data-v2.json`, the staged emails, the cached bodies, the MSAL token
cache and the send-idempotency flags are all in blob storage
(`scripts/state_store.py`). **Nothing meaningful runs locally** — a script that
reads the repo root finds no data. To inspect real data, add a step to
`.github/workflows/diag-blob.yml` and dispatch it; the `diag_*.py` scripts are
the existing examples and all begin with `state_store.pull(root=tmp)`.

A diagnostic that cannot fail loudly is worse than none: on 2026-08-20 one died
on its first line and the step went green in zero seconds because of a
trailing `|| true`. Emit `::error::` and a non-zero exit.

## The daily fire

`daily.yml` → `refresh_stage.py` (Graph → staged mail) → `run_pipeline.py`:
backup → reprocess_bodies → ingest → drift_check → **qc_selfheal** →
patch_carriers → **qc_selfheal again** → renderers → `outlook_send.py`.

`qc_selfheal` runs TWICE per fire, with `patch_carriers` between. Anything it
writes must be idempotent, and the second pass sees enrichment the first
cannot. Sends are idempotent through a per-day flag synced in the blob, so a
re-dispatch no-ops rather than double-sending; `--force` overrides it.

`HILMAR_REPORTS_PAUSED` is the three-state send gate (`false` = live since
2026-08-17). A manual dispatch defaults `send_to=test` (Michael only) — `full`
is deliberate, never accidental.

## Rebuild, don't merge

Every fire rebuilds rows from the staged mail. The single durable human
override is `scripts/operator_corrections.json`, re-applied on every ingest and
by qc_selfheal as a backstop. Anything else you "fix" in the data is gone next
fire — fix the parser or add a correction.

Corollary that has bitten repeatedly: **nothing un-stamps a bad value.** A heal
that writes a field must also clear it when the evidence goes away, or a wrong
value from an old rule persists forever and is re-derived every fire.

## QC is the contract

`qc_selfheal.py` emits QC-001..QC-080, each documented in
`reports/QC-INDEX.md`. `tests/test_qc_governance.py` fails if a check is
emitted without an index row, or an index row has no check. Add both together.

`Log.ok()` only PRINTS — it is not recorded on the Log and never reaches
`qc-result.json`. Downgrading a check to `ok` deletes it from the audit.

## Two storage forms for one status

Production writes LEGACY (`status="LOSS"` + `quoted` true/false); the library
writes STRICT (`"Q&L"` / `"NQ"`). QC-041 forbids mixing them in one dataset.
Never compare `r["status"]` to `"Q&L"` — route through `core.display_status`,
`core.is_win`, `core.is_quoted_and_lost`, `core.is_not_quoted`. A filter that
matched STRICT strings against LEGACY data is what produced a 100% win rate
that survived because both sides of the comparison were equally wrong.

## Time is the recurring source of defects

- `core.PENDING_HILMAR_LOSS_HOURS` (24) and `_FRIDAY` (72) are OPERATOR
  DECISIONS Michael has already revised once. `test_timer_docs_match_constants`
  fails if prose anywhere names an hour value no constant holds — including
  inside strings the report prints. Mark deliberate history `[historic]`.
- A win belongs to the day it was BOOKED — `core.win_event_date`, which prefers
  `booking_timestamp` over the status transition, because a back-entered
  booking's transition is the day the tracker was told.
- Report-day windowing is ET, and the daily reports the PRIOR business day.
  Fixtures dated relative to `now` pass or fail depending on the hour the suite
  runs; anchor them to `gen_email._report_date`.

## Reports and who sees them

`config.json` holds `distribution.full_list` (staff) and `client_report`
(Lonny, cc Michael's OL address). The client-facing renderers
(`gen_client_email.py`, `gen_client_weekly.py`) are held to a higher bar —
QC-065 locks their recipients, and a borrowed or inferred value must never
reach them.
