# HANDOFF — 2026-08-31

Written at the end of a session that ran without Turso authorization, for a
session that has it. Everything below is either **verified in that session**
(command shown) or explicitly marked `[ASSUMPTION]`. Nothing here is recalled.

**Read `CLAUDE.md` first — it is the contract. This is orientation.**

---

## 1. TURSO — read this before you touch anything Turso-shaped

You have Turso live and the last session did not. That asymmetry is exactly
where a new session invents work. Three facts, in priority order:

### 1a. The historian is NOT on Turso, and that was a decision

`docs/HISTORIAN.md` describes Turso provisioning at length. **Production does
not use it.** `.github/workflows/daily.yml` sets:

```yaml
HILMAR_HISTORIAN_SQLITE: data/quote-history.db
```

with this comment, verbatim, above it:

> 2026-07-11 (Michael *"you handle turso tokens... i cannot read this as it
> works"*): NO Turso, no tokens — a plain sqlite file synced through the SAME
> blob store as the rest of the pipeline state (state_store pulls it pre-fire,
> historian appends, state_store pushes it back). Live from the first fire with
> zero owner action.

**Do not "finish" the Turso migration for the historian.** It is not an
unfinished feature; it is a road Michael closed because it needed a token he
did not want to hold. libSQL is SQLite-compatible, so the same SQL runs either
way — the sqlite path is not a degraded mode.

If you want to change this, it is an operator decision, and the burden is
showing what Turso buys over a blob-synced sqlite file that already works.

### 1b. Where Turso IS live: rate-blaster's carrier codes

The `rate-blaster` probe reports:

```
[SHAREABLE] turso carrier_codes
     note: publisher present. Whether the REMOTE table is current is a
           separate question — run `carrier-codes-publish` to make it so.
```

"Publisher present" is not "table current." If you need those codes, run the
publisher and check freshness rather than trusting the SHAREABLE label.

### 1c. The MCP server needed OAuth the last session could not do

The `TURSO` MCP server reported "requires authentication," and the session was
non-interactive, so it could never complete the flow. If it is live for you,
that is the difference — not a code change in this repo.

---

## 2. State right now: nothing of this session's is left open

Michael approved all three on 2026-08-31 and they merged the same morning.

| PR | what | note |
|---|---|---|
| **#245** | QC-083 stops reporting, starts absorbing | **changes live numbers on the next fire** |
| **#246** | reference-data probe re-run; docs only | carries the `RATE_BLASTER_TOKEN` ask below |
| **#247** | this file | — |

**Watch the first report after #245.** It deletes rows from
`tracking-data-v2.json` — two HCMC pairs that were counted as Q&L losses on
shipments that shipped — so the loss count drops by two and the win rate moves.
That is the intended correction, not a regression. `backup` runs first in the
pipeline and every absorb is recorded in the surviving row's `merge_notes`, so
it is reversible by hand if a number looks wrong.

---

## 3. The one thing that genuinely waits on Michael

**Add a repo secret: `RATE_BLASTER_TOKEN`** — a fine-grained PAT with
**read-only Contents on `IdealX-dev/rate-blaster`**.

Why, measured 2026-08-31:

- `IdealX-dev/rate-blaster` is **private** (`"private": true`, GitHub API).
- Every workflow in `.github/workflows/` checks out with `actions/checkout@v5`
  and **no** `repository:` override; the only GitHub token in play is
  `${{ github.token }}`, scoped to `IdealX-dev/hilmar-daily-routine` alone.
- The nine `secrets.*` this repo uses are Graph, Sentry, Azure, Anthropic,
  Teams and QT credentials. **None is a GitHub PAT.**

So `pip install "git+https://github.com/IdealX-dev/rate-blaster.git@main"`
works in an agent session that already has the repo attached, and **fails on
the Actions runner that fires the daily report.**

**Check the repo out; do not put the token in a pip URL.** The obvious form
puts the credential on pip's command line, where an error message or a
re-encoding can carry it past Actions' secret masking, and leaves it in the
cached clone's git config. Use the documented cross-repo checkout in
`daily.yml` and `test.yml`:

```yaml
- uses: actions/checkout@v5
  with:
    repository: IdealX-dev/rate-blaster
    token: ${{ secrets.RATE_BLASTER_TOKEN }}
    path: .rate-blaster
    persist-credentials: false

- run: pip install ./.rate-blaster
```

`pip` never sees the token. `actions/checkout`'s docs describe `token` as
*"configured with the local git config"* with *"the post-job step removes the
PAT"*; `persist-credentials: false` keeps it out of that config at all.
`.rate-blaster` needs a `.gitignore` line so the nested checkout is not
untracked.

Size is not a reason to hesitate either way: `fetch-depth` defaults to 1, so
this takes the tip tree — `geo_master.db` at 6.2 MB — not rate-blaster's
~176 MB of history.

**Until that secret exists, write no code against `rate_blaster.geo`.** A
consumption path CI cannot install is worse than the violation it replaces: it
ships code that never runs in production and tests that skip.

---

## 3b. FINDING — an instruction of Michael's never reached `main` (written 2026-08-24, still missing 2026-08-31)

Found while writing this handoff, verified 2026-08-31, **not fixed** (he said
write the handoff, so this is reported rather than actioned):

Commit **`cb4e88a`** — *"No preamble — Michael's standing instruction, in the
working standard"*, authored 2026-08-24 — adds this to `CLAUDE.md`:

```
- NO PREAMBLE (Michael, 2026-08-22, said twice). No filler, no hedging, no
  restating the question, no narrating what you are about to do. Short direct
  sentences. Show the result, then stop.
- The confidence tags stay — a tag is precision, not hedging. So is naming a
  number you could not verify. Brevity never buys an unverified claim.
```

**It is not on `main`.** `git grep -c "NO PREAMBLE" origin/main -- CLAUDE.md`
returns nothing. It sits only on `claude/pr-65-passoff-docs-0rjg0y`, whose PR
(**#222**, *"The time system"*) **merged on 2026-08-22** — two days BEFORE this
commit was written. The commit was pushed to a branch whose PR was already
closed, so it had nothing left to ride into `main` on.

So a standing instruction Michael gave on 2026-08-22 and a commit wrote down
on 2026-08-24 was still absent from `main` on 2026-08-31, and every session
reading `CLAUDE.md` from `main` in that window never saw it. **Re-check before
acting on this section** — `git grep -c "NO PREAMBLE" origin/main -- CLAUDE.md`
answers it in one line, and a non-zero count means someone has since landed it.

**To land it:** cherry-pick `cb4e88a` onto a fresh branch off `main`. It
conflicts — `main` has since appended the SHARED REFERENCE DATA block directly
after the `HOW TO TALK TO ME` list. The resolution is keep-both: the two
bullets belong inside that list, the reference-data block stays where it is.
It was deliberately not done in this session because the ask was the handoff.

**Also note the branch itself.** `claude/pr-65-passoff-docs-0rjg0y` is named
like a docs branch and is not one — it carried the #222 time-system work (19
files, ~1,300 lines), which merged. Everything on it except `cb4e88a` is
already-merged history.

---

## 4. What the last session did — do not redo it

Merged: **#229–#234, #238–#241, #243–#247** — everything, as of 2026-08-31.

- **#239** — the dead schedule gate. #228 moved the crons and left the hour
  matcher on the old values, so three "successful" runs sent nothing. Fixed to
  `10`/`11`.
- **#240** — carrier fabrication on every Vietnam/Panama lane. Booking-ref
  prefixes (`CARRIER_REF_PREFIXES`) now identify the line.
- **#241** — rule 5. `_scan_for_origin` was matching 3-letter tokens anywhere
  in free text: `LAX` inside "relaxed", `FOB Shanghai` as Fort Bragg,
  `CPT Hamburg` as Cape Town, `12.4 CBM` as Columbus. Now word-bounded with
  incoterm and unit-token exclusions. 35 tests.
- **#243** — the KPI tiles and STATUS CHANGES are dated differently (event vs
  intake). Both numbers were right; the report now says which day it means.
- **#244** — lanes bucket on the canonical port, not the spelling, so HCMC's
  two terminal spellings stop splitting one lane's median.
- **#245** — QC-083 absorb: a superseded re-ask stops being counted as its own
  shipment. Three guards, each mutation-checked.
- **#246** — reference-data probe re-run; the blocker is the secret in §3.
- **#247** — this file.

### Corrections made on the record last session — inherit them, do not re-derive

- **KOBE was not splitting the lane.** A claim that six operator corrections
  pinning `KOBE` were splitting it did not survive execution:
  `title_case_destination('Kobe')` returns `'KOBE'` — the parser already
  merges. The real divergences are HCMC / Cat Lai / Cai Mep / Port Busan /
  Lat Krabang / Manila N-S.
- **"44 of 134 rows" for JPYOK was measured on OL's export, not live tracking
  data,** where it is 1 row.
- An ad-hoc isolated-import script reported 15 failures. **Bogus** — it exec'd
  every `.py` including CLI scripts. CI's actual named module lists show 0.

---

## 5. Traps that have bitten more than once

- **Nothing meaningful runs locally.** State lives in Azure blob
  (`scripts/state_store.py`). To inspect real data, add a step to
  `.github/workflows/diag-blob.yml` and dispatch it. Every `diag_*.py` begins
  `state_store.pull(root=tmp)`.
- **A diagnostic that cannot fail loudly is worse than none.** On 2026-08-20
  one died on its first line and went green in zero seconds because of a
  trailing `|| true`. Emit `::error::` and exit non-zero.
- **`Log.ok()` only PRINTS.** It never reaches `qc-result.json`. Downgrading a
  check to `ok` deletes it from the audit.
- **Nothing un-stamps a bad value.** A heal that writes a field must also clear
  it when the evidence goes away, or a wrong value persists forever and is
  re-derived every fire.
- **Two storage forms for one status.** Production writes LEGACY
  (`status="LOSS"` + `quoted`); the library writes STRICT (`"Q&L"`/`"NQ"`).
  Never compare `r["status"]` to `"Q&L"` — route through `core.display_status`,
  `core.is_win`, `core.is_quoted_and_lost`, `core.is_not_quoted`. A filter
  matching STRICT strings against LEGACY data produced a 100% win rate that
  survived because both sides were equally wrong.
- **`qc_selfheal` runs TWICE per fire**, with `patch_carriers` between.
  Anything it writes must be idempotent.
- **QC governance is mechanical.** `tests/test_qc_governance.py` fails if a
  check is emitted without a row in `reports/QC-INDEX.md`, or vice versa. Add
  both together.
- **Fixtures dated relative to `now`** pass or fail depending on the hour the
  suite runs. Anchor to `gen_email._report_date`.
- **Heredoc escaping has corrupted files twice.** A non-raw outer Python string
  turned `r"\b("` into a backspace character. Write the helper to the
  scratchpad first, then lift it; check with `repr()`.
- **Replacing typographic apostrophes broke `gen_email.py` into 70 syntax
  errors** (they sat inside single-quoted f-strings). Reword instead.
- **CHANGELOG edits have silently no-op'd twice** because the anchor existed
  only on another branch. `assert` that the replacement applied.

---

## 6. Commands

```bash
pytest tests/ --no-cov -q                    # the suite (~3,650 tests, ~28s)
pytest tests/ -q                             # adds the 90% src/hilmar gate
ruff check scripts/ src/ tests/ deploy/      # must stay at zero
python3 scripts/run_pipeline.py --dry-run    # print steps, run nothing
python -m rate_blaster.scripts.reference_data_status   # from /home/user/rate-blaster
```

`--no-cov` matters: bare `pytest` applies `--cov-fail-under=90` over
`--cov=hilmar` — the **library** only. `scripts/`, which is what production
runs, is far lower and deliberately ungated. The gate is a one-way ratchet.

---

## 7. Open, ranked

1. **`RATE_BLASTER_TOKEN`** — Michael's, one secret, unblocks everything below.
2. **Retire `core.PORT_LOCODES` / `resolve_locode()`** — an open violation of
   the shared-reference-data rule since #230. Replace with a `locode` → `city`
   lookup against `rate_blaster.geo`, **code→place only** (rule 2: 262 of
   11,629 city names map to more than one LOCODE; `Lagos` returns two Nigerian
   ports *and* `PTLOS` in Portugal). Blocked on 1.
3. **Retire `core.CARRIER_ALIASES` (42 entries)** in favour of
   `rate_blaster.util.carrier_registry.canonical_for_code()`. No longer blocked
   upstream — `carrier_registry` reached rate-blaster `main`; blocked on 1 only.
4. **`core._TRADE_REGION_MAP`** — trade region is a classification
   `geo_master` does not carry. By the rule it is a field to **add in
   rate-blaster and publish**, never to keep here.
5. **`body_parser.KNOWN_ORIGINS`**, and **`Huangpu`** (added locally in #228,
   present upstream in neither `name` nor `city`) — a PR against rate-blaster.
6. **Vendor `rate_blaster.geo.place_gate.iata_token_is_a_place`** rather than
   re-implementing the IATA position logic. `CLAUDE.md` is explicit: *"a local
   copy is the seventh table."*

`[ASSUMPTION]` on 2–6: that Michael still wants full consumption rather than a
narrower subset. He settled the *mechanism* (consume the package, do not vendor
an extract) on 2026-08-30; he has not scoped which tables come first.
