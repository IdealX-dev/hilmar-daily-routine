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

## 3. CORRECTED — the reference-data "blocker" was overstated. Nothing waits on Michael.

**Read this before you act on anything reference-data shaped.** An earlier
version of this very file, and `REFERENCE_DATA_PROMPT.md`, and PRs #245–#247,
all led with *"add a `RATE_BLASTER_TOKEN` secret and the shared-reference-data
rule stops being blocked."* Michael pushed back — *"what does this have to do
with hilmar?"* — and the measurement settles it:

| table | entries |
|---|---|
| `core.PORT_LOCODES` | **1** — `{"JPYOK": "Yokohama"}` |
| `core.CARRIER_ALIASES` | 42 |
| `core._TRADE_REGION_MAP` | 81 |
| `body_parser.KNOWN_ORIGINS` | 22 |

Consuming `rate_blaster.geo` would delete a **one-row dict** in exchange for
putting a private cross-repo dependency into the daily fire. Bad trade.
**Michael's call, 2026-08-31: do not create the secret.**

`core.py` already argued this above the dict — *"SEEDED FROM EVIDENCE, NOT FROM
MEMORY. JPYOK is the only entry because it is the only code this book has
actually produced"* — with a test refusing unconfirmed additions. The rule in
`CLAUDE.md` exists to stop a 12,000-row port list being duplicated and
drifting. One operator-confirmed row behind a test is not that.
`_TRADE_REGION_MAP` cannot be replaced upstream at all — trade region is a
classification `geo_master` does not carry.

**Revisit when** `PORT_LOCODES` grows a row per fire, or QC-015 starts firing
on ports the local tables do not carry. The credential mechanics (cross-repo
`actions/checkout`, never a token in a pip URL) are recorded in
`REFERENCE_DATA_PROMPT.md` for that day.

**The transferable lesson, and why it is now in `CLAUDE.md`:** this claim was
inherited from an audit page and repeated across three PRs without anyone
running `len()` on the table. It is the second time in one week — the KOBE lane
split, which the parser already merged, was the first. An inherited finding is
not a verified one.

---

## 3b. FIXED 2026-08-31 — an instruction of Michael's had never reached `main`

Found while writing this handoff. **Now landed** — Michael confirmed the
instruction was his (*"oh yes.. all into .md and you handle"*, 2026-08-31) and
it is in `CLAUDE.md` under HOW TO TALK TO ME. Kept on the record because the
failure mode is the interesting part, not the fix:

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

**How it was landed:** the two bullets were written into the `HOW TO TALK TO
ME` list directly rather than cherry-picked, because `main` had since appended
the SHARED REFERENCE DATA block right after that list and the commit would not
apply cleanly. A third bullet was added at the same time — MEASURE THE THING
BEFORE YOU WRITE IT UP — for the failure in §3.

**Still to do upstream:** `CLAUDE.md`'s working standard is duplicated into
each consuming repo from `IdealX-dev/idealx-claude-standards` →
`user-claude-md/CLAUDE.md`. These three bullets landed in THIS repo only. They
belong upstream too, or the next repo to sync will overwrite them.

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

**Nothing here is blocked on Michael.** The `RATE_BLASTER_TOKEN` ask that used
to sit at #1 was withdrawn on 2026-08-31 — see §3.

1. **Nothing.** As of 2026-08-31 this session's work is merged and no item
   below is worth starting on its own. That is a real answer, not a gap.
2. **Watch the first fire after #245** (§2). Two fewer losses is the intended
   correction; anything else is worth a look.
3. **Reference-data consumption — deliberately parked.** Retiring
   `core.PORT_LOCODES` (1 row), `core.CARRIER_ALIASES` (42) or
   `body_parser.KNOWN_ORIGINS` (22) in favour of `rate_blaster.geo` /
   `carrier_registry` costs a private cross-repo dependency on the daily fire
   and buys almost nothing today. Revisit on the trigger in §3, not on a
   schedule. `core._TRADE_REGION_MAP` (81) is not replaceable upstream at all.
4. **`Huangpu`** — added locally in #228, present upstream in neither `name`
   nor `city`. A one-row PR against rate-blaster. Cheap, no dependency, and it
   makes the shared data better for every repo. Worth doing on its own.
5. **If rule-5 logic ever needs extending**, vendor
   `rate_blaster.geo.place_gate.iata_token_is_a_place` rather than
   re-implementing the IATA position logic — `CLAUDE.md`: *"a local copy is the
   seventh table."* Not needed today; #241's word-bounded scan covers what this
   ocean-only repo sees.

**When you do reach for upstream, rule 2 still binds:** code → place only. 262
of 11,629 city names map to more than one LOCODE, and `Lagos` returns two
Nigerian ports *and* `PTLOS` in Portugal.
