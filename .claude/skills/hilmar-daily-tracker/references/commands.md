# Hilmar Daily Tracker — commands

**The operating model changed on 2026-08:** the Cloud PC and the OneDrive
mirror are decommissioned. Production is GitHub Actions in
`IdealX-dev/hilmar-daily-routine`; live state (tracking data, stage caches,
token cache) lives in the Azure blob store and exists on NO local machine.
Everything operational is therefore a **workflow dispatch**, which works from
any device — laptop, web session, or the iPhone Claude app.

A local clone is for CODE work only: editing, tests, lint. Do not try to run
the pipeline locally; the secrets it needs exist only in Actions.

## Fire a report

Dispatch **`daily.yml`** on the working branch with inputs:

| input | test fire | notes |
|---|---|---|
| `mode` | `production-fire` | `audit-only` runs tests, no fire |
| `send_to` | `test` | `test` = Michael only. `full` = staff list — needs Michael's explicit approval AND the gate at `"false"` |
| `force_resend` | `true` | overrides the per-day already-sent flag |
| `days_back` | `14` (default) | widen only with a reason; first-read of a new mailbox is a bulk job — use `seed-shared-mailbox.yml` instead |

Under `HILMAR_REPORTS_PAUSED: "verify_only"` (current), every send is forced
to Michael regardless of `send_to` — the gate refuses `full` and the send
steps force `SEND_TO=test` again. Scheduled fires: Mon–Fri ~8:07 AM ET, dual
crons for DST with a gate that picks exactly one.

From a Claude session, dispatch via the GitHub MCP tools
(`actions_run_trigger` with `workflow_id: daily.yml`, `ref: <branch>`); from
a browser, the Actions tab → Daily → Run workflow. `gh` CLI is not available
in remote sessions.

## Inspect the real data (read-only)

Dispatch **`diag-blob.yml`** and read the job log. One run prints, in order:
blob-store health, the report-day rows and both undated-quote counts, why
each undated row is undated, turnaround plausibility, status-transitions by
day, standalone-booking match proposals, and the shared-mailbox endpoint
probe. It writes nothing.

This is THE window into stored state. Do not reason about live data from a
fresh clone — it has none.

## Verify a fire actually worked

Never report a fire as working from its green check alone. Read the job log
for:

- `refresh_stage: reading N mailbox(es): …` and per-mailbox counts — a
  mailbox at zero for the whole window prints a `::error::` and means a
  blind read, not a quiet day.
- `NEW staged records: N` and the bucket breakdown.
- `QC SELF-HEAL COMPLETE … NNN entries: …W | … Q&L | … NQ | … P`.
- `→ TO (…)` on each send step — under verify_only both must show only
  `michael.deitchman@idealx.us`.
- `✅ Sent. request-id=…` — the actual send proof.

## Graph token maintenance

Dispatch **`auth-refresh.yml`**: `confirm=REAUTH`,
`notify=michael.deitchman@ol-usa.com`, `include_shared` only when Michael
asks for the shared scope. The sign-in code is emailed AND printed in the
live job log (readable mid-run in the browser; the API serves logs only
after completion). The run ends by printing which mailboxes the pipeline can
read — but remember: that proves the TOKEN, not the mailbox. Only messages
returned proves a mailbox.

`include_shared` is now vestigial: Michael, 2026-08-14, "ol won't grant more
access" — the shared mailbox can never be read, so the scope buys nothing.
Do not propose the OWA self-test or an IT request; both are closed. See
SKILL.md "Mailboxes".

## Local development

```bash
git clone https://github.com/IdealX-dev/hilmar-daily-routine.git
pytest tests/ --no-cov -q                      # ~3,150+ tests, fast
pytest tests/                                  # adds the 90% src/hilmar cov gate
ruff check scripts/ src/ tests/ deploy/
```

Rules that bite:
- Edit shared logic in BOTH `scripts/` and `src/hilmar/` (parity tests fail
  otherwise). Production imports `scripts/`.
- Never push red; the full suite green before any push (CLAUDE.md).
- Branch: session work on `claude/...` branches; PRs; Michael's standing
  merge-when-green authority applies. Commit trailers per session rules.
- `operator_corrections.json` is authoritative human state — script edits to
  it, log them, and never leave one request_id with both `create` and
  `exclude`.

## Manual email send

There is almost never a reason — the fire sends. If Michael explicitly asks
for a manual send, `scripts/outlook_send.py daily --to
michael.deitchman@idealx.us --verification …` is the shape (see the send
steps in `daily.yml` for exact flags). A verification send tags the subject
`[VERIFY]` and never consumes the real send's idempotency flag. Anything
beyond Michael's own address is a distribution change — explicit approval
first.

## Sentry

`qc_actions_from_sentry.py --apply` runs inside the fire. Org `idealx-llc`,
project `hilmar-daily-tracker`. Read findings via the audit email or the
Sentry MCP tools when connected.
