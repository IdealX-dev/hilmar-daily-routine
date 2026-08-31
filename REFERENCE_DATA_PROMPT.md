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

## The one thing standing between this repo and the rule

**`rate-blaster` is a PRIVATE repository, and this repo's runner has no
credential that can read it.** Verified 2026-08-31, both halves:

- `IdealX-dev/rate-blaster` → `"private": true` (GitHub API).
- Every workflow in `.github/workflows/` checks out with `actions/checkout@v5`
  and no `repository:` override, and the only GitHub token in play is
  `${{ github.token }}` — scoped to `IdealX-dev/hilmar-daily-routine` alone.
  The nine `secrets.*` this repo uses are all service credentials (Graph,
  Sentry, Azure, Anthropic, Teams, QT). **None is a GitHub PAT.**

So the sharing standard —

```
pip install "git+https://github.com/IdealX-dev/rate-blaster.git@main"
```

— installs fine in a session that already has the repo attached, and **fails
on the GitHub Actions runner** that actually fires the daily report. That is
the whole gap. It is not a design question; it is one missing secret.

### What unblocks it

A fine-grained PAT with **read-only Contents on `IdealX-dev/rate-blaster`**,
stored as a repo secret on `IdealX-dev/hilmar-daily-routine` (say
`RATE_BLASTER_TOKEN`). Creating that secret is an access change, so it is
Michael's to make — a session must not provision it.

**Check it out; do not put it in a pip URL.** The obvious form —
`pip install "git+https://<token>@github.com/..."` — puts the credential on
pip's command line, where it can reach logs through an error message or a
re-encoding that defeats Actions' secret masking, and leaves it in the cached
clone's git config. Use the documented cross-repo checkout instead, in
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

`pip` never sees the token. `actions/checkout`'s own docs describe `token` as
*"configured with the local git config"* with *"the post-job step removes the
PAT"*, and `persist-credentials: false` keeps it out of that config in the
first place. `.rate-blaster` needs a `.gitignore` line so the nested checkout
does not show up as untracked.

**Size is not a reason to hesitate either way.** `actions/checkout` defaults to
`fetch-depth: 1`, so it takes the tip tree, not rate-blaster's ~176 MB of
history; `geo_master.db` in it is 6.2 MB. (For the record, the pip route is not
slow either — pip's docs, `topics/vcs-support`: *"Pip defaults to partial
clones for Git 2.17 or later."* Size was never the objection; credential
handling is.)

---

## Where this repo stands

Audited 2026-08-28, re-checked 2026-08-31.

### OPEN VIOLATION — `scripts/core.py`

`PORT_LOCODES` and `resolve_locode()`, shipped in **#230** on 2026-08-27. A
local LOCODE table containing a hard-coded UN/LOCODE: both halves of the rule
broken in one place.

`geo_master.db` is SHAREABLE, so the replacement exists — as a `locode` →
`city` lookup, code→place only, per rule 2. **Still not fixed**, and the
reason changed on 2026-08-31: it is no longer an open design decision
(Michael settled that — consume the package, do not vendor an extract), it is
the missing runner credential above. Nothing should be written against
`rate_blaster.geo` until CI can install it, because a consumption path that
cannot run in production is worse than the violation it replaces.

### NO LONGER BLOCKED — `core.CARRIER_ALIASES`

42 entries. The block recorded here on 2026-08-28 was *"staying that way until
`carrier_registry` reaches `rate-blaster` `main`."* **It has.** 53 carriers,
28 ocean + 25 air, SHAREABLE as of the probe run above.

It now sits behind the same credential as the LOCODE table, and behind nothing
else. `rate_blaster/portal/carriers.py` is still **not** the substitute — its
own docstring calls it *"curated, not exhaustive"*.

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
