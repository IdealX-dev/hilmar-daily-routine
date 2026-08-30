# Reference data — this repo's compliance record

**The rule is not restated here.** It lives in `CLAUDE.md`, section *"SHARED
REFERENCE DATA — ports, airports, rail, carriers; do not rebuild it here"*,
which is loaded into every session automatically. A contract about not keeping
two copies of a thing does not get kept in two copies.

This file holds the two things `CLAUDE.md` cannot: **what the probe actually
returned when it was run**, and **where this repo currently stands against the
rule**. Both are findings with dates on them, not policy.

---

## What is actually consumable — measured, 2026-08-28

`CLAUDE.md` says to run the probe rather than trust anyone's summary,
including its own. This is what running it returned. **Re-run it; do not trust
this page.**

```
python -m rate_blaster.scripts.reference_data_status
```

The probe is **not on `rate-blaster` `main`**. It lives on
`claude/rate-blaster-geo-fetch-0cckio` @ `b527194`, alongside
`carrier_registry.py` — which is why it has to be run from that branch today,
and why it reports itself as unconsumable.

```
  [SHAREABLE] geo_master.db
                seaports        11928
                airports         7883
                icds            12949
                rail_yards       9177
                is_major          136   (the prose-match gate)

  [LOCAL ONLY] carrier_registry
                carriers  53   ocean  28   air  25
                note: PRESENT IN THIS CHECKOUT BUT NOT ON main —
                      another repo cannot consume it yet.

  [LOCAL ONLY] turso carrier_codes

Another repo can consume RIGHT NOW: geo_master.db
EXIT=1
```

**This snapshot goes stale the moment that branch merges.** It is here as
evidence of a decision made on a date, not as a substitute for the probe.

Corroborated independently against `geo_master.db` on `rate-blaster` `main`
(`531bd27`): `JPYOK` → `name='Port of Yokohama'`, `city='Yokohama'`,
`country_code='JP'` — which is rule 1 in one row. And `city='Lagos'` returns
`NGAPP`, `NGTIN` **and** `PTLOS` — rule 2 in three.

---

## Where this repo stands

Audited 2026-08-28.

### OPEN VIOLATION — `scripts/core.py`

`PORT_LOCODES` and `resolve_locode()`, shipped in **#230** on 2026-08-27. A
local LOCODE table containing a hard-coded UN/LOCODE: both halves of the rule
broken in one place.

`geo_master.db` is SHAREABLE, so this is replaceable now — as a `locode` →
`city` lookup, code→place only, per rule 2. **Not yet fixed**; the access
mechanism (vendor an extract vs fetch at build time) is an open operator
decision. A seaports-only extract measures **480 KB** against the full db's
5.8 MB, if that helps size it.

### BLOCKED, correctly — `core.CARRIER_ALIASES`

42 entries, untouched, and staying that way until `carrier_registry` reaches
`rate-blaster` `main`. `rate_blaster/portal/carriers.py` (in rate-blaster —
there is no such path in this repo) is **not** substituted for it —
its own docstring calls it *"curated, not exhaustive"*, and the probe's
closing line says the same.

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

### NOT YET ASSESSED

Rule 5 (3-letter IATA codes and incoterm collisions) has not been audited
against this repo. It is ocean-only today, so the exposure is likely nil — but
"likely" is not a measurement, and this line stays until someone takes one.
