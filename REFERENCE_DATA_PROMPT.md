# Shared reference data — ports, airports, rail, carriers

**Standing rule, every repo, every session.** Paste-free: point a session here
instead of re-typing the contract.

Ports, airports, inland depots, rail yards and carrier codes are maintained
ONCE, in `IdealX-dev/rate-blaster`. Never build a port / airport / rail /
LOCODE / carrier list in any other repo, and never hard-code a UN/LOCODE, IATA
code or SCAC. **Finding one is a finding — report it.**

---

## Where it lives

**`rate_blaster/geo/geo_master.db`** — committed SQLite, on `main`:

| table | rows | key |
|---|---|---|
| `seaports` | 11,928 | `locode` |
| `airports` | 7,883 | `iata_code` (plus `icao_code`) |
| `icds` | 12,949 | `icd_code` |
| `rail_yards` | 9,177 | `yard_id` |

All carry `name`, `city`, `country_code`, `lat`, `lon`.

**`rate_blaster/util/carrier_registry.py`** — 28 ocean lines (SCAC) + 25
airlines (IATA/ICAO). `canonical_for_code()` resolves any of them.

---

## Check what is consumable BEFORE writing against it

Never assume, and never take anyone's word for it — including the operator's,
including this file's:

```
python -m rate_blaster.scripts.reference_data_status
```

It prints SHAREABLE vs LOCAL ONLY per component and **exits non-zero while
anything is still on a branch**. A component marked LOCAL ONLY exists but
cannot be consumed by another repo yet, and telling a consumer otherwise is
the defect this probe exists to prevent.

`portal/carriers.py` is a UI picker whose own docstring says *"curated, not
exhaustive"*. **It is NOT the registry.** Never substitute it.

### Last verified: 2026-08-28

Run from `claude/rate-blaster-geo-fetch-0cckio` @ `b527194` — the probe is not
itself on `main` yet, which is why it must be run from that branch today.

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

**This snapshot is a convenience, not an authority.** It goes stale the moment
that branch merges. Re-run the probe.

---

## Five rules. Each is a defect that already shipped.

**1. Join on `city`, never `name`.**
`JPYOK` is `name='Port of Yokohama'`, `city='Yokohama'`. Keying a lane off
`name` rewrites every lane key in the consuming system.

**2. Code → place only, never place → code.**
262 of 11,629 city names map to more than one LOCODE. `Lagos` returns two
Nigerian ports **and** `PTLOS` in Portugal. A reverse join eventually ships to
the wrong continent.

**3. Resolution is unrestricted; MATCHING in prose is gated** on
`seaports.is_major = 1` — 136 ports, covering every lane actually quoted.
Resolving a code someone handed you is always safe; going looking for port
names inside free text is not.

**4. Never match a bare 2-letter carrier code in free text.**
`PO` is a purchase order. `CM` is centimetres. `FX` is foreign exchange. `5X`
is `5x40HC`. `VS` is what a comparison prints BETWEEN two carriers. Match a
full name or a labelled column, nothing else.

**5. 3-letter IATA codes ARE the identifier — match them, but mind POSITION.**
Seven incoterms are live IATA codes. `FOB Shanghai` resolves FOB to Fort
Bragg; `CPT Hamburg` to Cape Town; `12.4 CBM` is Columbus. A 3-letter token
**after a number** is a unit. An incoterm **before a place name** is the
incoterm.

### Two more that follow from the above

- **Anything you cannot resolve is `None`, never a guess.** A wrong carrier or
  port on a priced row misleads a human in a way a blank never does.
- **A missing code stays absent — never a placeholder.** A code that looks
  real will reach a booking.

---

## What stays local, and what does not

**Local to each repo:**

- **Aliases** — `"port of los angeles"`, `"san pedro"`. How *your*
  correspondents spell a place.
- **Match policy** — which subset your parser recognises.

**Never local:**

- The data itself.
- **A field upstream does not carry.** Add it in rate-blaster and publish.
  Corrections are a PR against rate-blaster so every repo gets them at once.

---

## Compliance status — this repo (hilmar-daily-routine)

Audited 2026-08-28.

**OPEN VIOLATION.** `scripts/core.py` — `PORT_LOCODES` and `resolve_locode()`,
shipped in #230 on 2026-08-27. A local LOCODE table containing a hard-coded
UN/LOCODE: both halves of the rule, in one place. `geo_master.db` is
SHAREABLE, so this is replaceable now with a `locode` → `city` lookup
(direction per rule 2).

**BLOCKED, correctly.** `core.CARRIER_ALIASES` (42 entries) stays as-is until
`carrier_registry` reaches `main`. Do not substitute `portal/carriers.py`.

**Clean on rule 4.** `CARRIER_ALIASES` has zero keys of length ≤ 2, so nothing
here matches a bare 2-letter code in prose. `HLAG` and `OOCL` are 4-letter but
are trading names appearing in email text, not dispatched SCACs.

**Under the contract, not yet migrated.** `core._TRADE_REGION_MAP` (port →
trade region) and `body_parser.KNOWN_ORIGINS`. Trade region is a
classification `geo_master` does not carry — by the rule above that is a field
to add in rate-blaster and publish, not to keep here. `Huangpu`, added locally
in #228, is in neither `name` nor `city` upstream and is a row to contribute.
