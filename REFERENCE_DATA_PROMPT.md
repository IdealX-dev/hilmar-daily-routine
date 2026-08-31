# Reference data — this repo's compliance record

**The rule is not restated here.** It lives in `CLAUDE.md`, section *"SHARED
REFERENCE DATA — ports, airports, rail, carriers; do not rebuild it here"*,
which is loaded into every session automatically. A contract about not keeping
two copies of a thing does not get kept in two copies.

This file holds the two things `CLAUDE.md` cannot: **what the probe actually
returned when it was run**, and **where this repo currently stands against the
rule**. Both are findings with dates on them, not policy.

---

## What is actually consumable — measured, 2026-08-31

`CLAUDE.md` says to run the probe rather than trust anyone's summary,
including its own. This is what running it returned. **Re-run it; do not trust
this page.**

```
python -m rate_blaster.scripts.reference_data_status
```

Run against `rate-blaster` `main` @ `6b8f8b57` (2026-08-31,
`IdealX-dev/rate-blaster#408`). The probe is
now ON `main` — it was on a branch when this page was first written, which is
the single biggest change since.

```
  [SHAREABLE] geo_master.db
                seaports        11928
                airports         7883
                icds            12949
                rail_yards       9177
                seaports.is_major           136   (quotable-terminal gate)
                rail_yards.is_major         112   (quotable-terminal gate)
                icds.is_major               117   (quotable-terminal gate)

  [SHAREABLE] carrier_registry
                carriers           53
                ocean              28
                air                25

  [SHAREABLE] turso carrier_codes
                note: publisher present. Whether the REMOTE table is current
                      is a separate question — run `carrier-codes-publish`.

Another repo can consume RIGHT NOW: geo_master.db, carrier_registry,
                                    turso carrier_codes
EXIT=0
```

**EXIT=0 — nothing is held back any more.** The 2026-08-28 snapshot this page
used to carry reported `carrier_registry` as LOCAL ONLY and `EXIT=1`, because
it and the probe were both sitting on `claude/rate-blaster-geo-fetch-0cckio`.
That branch has landed. The blocker on `core.CARRIER_ALIASES` recorded below
was real when it was written and is not real now.

Corroborated independently against `geo_master.db` on `rate-blaster` `main`:
`JPYOK` → `name='Port of Yokohama'`, `city='Yokohama'`, `country_code='JP'` —
which is rule 1 in one row. And `city='Lagos'` returns `NGAPP`, `NGTIN`
**and** `PTLOS` in Portugal — rule 2 in three.

---

## MEASURED 2026-08-31 — the gap is real and it is TINY

This page spent three days calling `scripts/core.py` an OPEN VIOLATION and
naming a missing GitHub secret as the blocker. Then someone finally ran
`len()` on the tables. Here is what this repo actually keeps:

| table | entries |
|---|---|
| `core.PORT_LOCODES` | **1** — `{"JPYOK": "Yokohama"}` |
| `core.CARRIER_ALIASES` | 42 |
| `core._TRADE_REGION_MAP` | 81 |
| `body_parser.KNOWN_ORIGINS` | 22 |

**`PORT_LOCODES` is one row.** Consuming `geo_master.db` would delete a
one-line dict in exchange for putting a private cross-repo dependency into the
daily fire. That is a bad trade today, and the page said the opposite because
it inherited the framing from its own first draft and nobody executed it.

The code already made the argument. Above that dict, in `core.py`:

> SEEDED FROM EVIDENCE, NOT FROM MEMORY. JPYOK is the only entry because it is
> the only code this book has actually produced and the only one the operator
> has confirmed. [...] Add each one when a fire actually surfaces it, with the
> UNECE citation in the comment.

with `tests/test_locode_merge.py::test_every_locode_value_is_a_real_corpus_port`
refusing anything unconfirmed. The rule in `CLAUDE.md` exists to stop a
12,000-row port list from being duplicated and drifting. One operator-confirmed
row behind a test is not that.

### So: DO NOT create `RATE_BLASTER_TOKEN` yet

Michael's call, 2026-08-31, on being shown the measurement. The credential
mechanics are recorded below because they will be right when the day comes —
not because that day is now.

**Revisit when** `PORT_LOCODES` starts growing a row per fire, or QC-015
(unresolved lane in a client-facing surface) starts firing on ports the local
tables do not carry. Then the dependency pays for itself. Until then it is
ceremony with a private-repo failure mode attached to the daily report.

`core._TRADE_REGION_MAP` can never be replaced from upstream anyway: trade
region is a classification `geo_master` does not carry.

### The mechanics, for when it IS worth doing

`IdealX-dev/rate-blaster` is **private** (`"private": true`, GitHub API), and
this repo's runner has no credential that can read it — every workflow checks
out with `actions/checkout@v5` and no `repository:` override, and the only
GitHub token in play is `${{ github.token }}`, scoped to
`IdealX-dev/hilmar-daily-routine` alone. None of the nine `secrets.*` is a
GitHub PAT.

So `pip install "git+https://github.com/IdealX-dev/rate-blaster.git@main"`
works in an agent session with the repo attached and fails on the runner. It
needs a fine-grained PAT, read-only Contents on rate-blaster, as
`RATE_BLASTER_TOKEN`.

**Check the repo out; do not put the token in a pip URL.** The obvious form
puts the credential on pip's command line, where an error message or a
re-encoding can carry it past Actions' secret masking, and leaves it in the
cached clone's git config:

```yaml
- uses: actions/checkout@v5
  with:
    repository: IdealX-dev/rate-blaster
    token: ${{ secrets.RATE_BLASTER_TOKEN }}
    path: .rate-blaster
    persist-credentials: false

- run: pip install ./.rate-blaster
```

`pip` never sees the token. `actions/checkout`'s own docs describe `token` as
*"configured with the local git config"* with *"the post-job step removes the
PAT"*; `persist-credentials: false` keeps it out of that config to begin with.
`.rate-blaster` needs a `.gitignore` line. Size is not a factor either way —
`fetch-depth` defaults to 1.

---

## Where this repo stands

Audited 2026-08-28, re-checked and **measured** 2026-08-31.

### NOT A VIOLATION IN PRACTICE — `scripts/core.py`

`PORT_LOCODES` (1 entry) and `resolve_locode()`, shipped in **#230**. It is a
hard-coded UN/LOCODE, which the letter of the rule forbids — and it is one row,
seeded from a fire, operator-confirmed, and guarded by a test that rejects
unverified additions. Reported here rather than "fixed", because the fix costs
more than the defect. See the measurement above.

### AVAILABLE, NOT URGENT — `core.CARRIER_ALIASES`

42 entries. `carrier_registry` reached `rate-blaster` `main` (53 carriers, 28
ocean + 25 air), so the upstream block recorded on 2026-08-28 has cleared. What
remains is the same credential, and the same verdict: 42 local aliases are not
worth a private cross-repo dependency on the daily fire today.

`rate_blaster/portal/carriers.py` is still **not** the substitute — its own
docstring calls it *"curated, not exhaustive"*.

### CLEAN — rule 4

`CARRIER_ALIASES` has **zero** keys of length ≤ 2, so nothing in this repo
matches a bare 2-letter code in prose. `HLAG` and `OOCL` are 4-letter but are
trading names appearing in email text, not dispatched SCACs.

### UNDER THE CONTRACT, NOT YET MIGRATED

- `core._TRADE_REGION_MAP` — port → trade region. Trade region is a
  classification `geo_master` does not carry, so by the rule it is a field to
  add in rate-blaster and publish, not to keep here.
- `body_parser.KNOWN_ORIGINS` — origin port names.
- `Huangpu`, added locally in **#228**, is in neither `name` nor `city`
  upstream. A row to contribute via PR against rate-blaster.

### ASSESSED 2026-08-31 — rule 5

Audited in **#241**. This repo is ocean-only, so no IATA code is dispatched
from it — but `body_parser._scan_for_origin` was matching 3-letter tokens
anywhere in free text, which is the collision rule 5 names: `LAX` inside
"relaxed", `FOB Shanghai` reading as Fort Bragg, `CPT Hamburg` as Cape Town,
`12.4 CBM` as Columbus. The scan is now word-bounded with explicit incoterm
and unit-token exclusion lists, covered by
`tests/test_three_letter_tokens_are_not_places.py` (35 tests).

The upstream gate `rate_blaster.geo.place_gate.iata_token_is_a_place` is the
one that should own this — same credential, same queue. The local lists are
match policy, which the rule does keep local; what must not become local is
the code table behind them.
