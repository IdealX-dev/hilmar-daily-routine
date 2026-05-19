# Parser Gaps — 2026-05-18 audit

Per Michael 2026-05-18 ("no field should be empty ever... your parser is also
not picking up a lot of data, names, etc etc"). Full audit of which fields
are empty across the 155 production rows.

## Fields 100% empty (parser never extracts)

These fields exist in `schema.json` but `body_parser.py` has no function
that populates them. Either the parser doesn't try, or the source
field doesn't appear in the staged email bodies.

| Field | Empty | Likely source | Effort |
|---|---|---|---|
| `requested_dates` | 155/155 | Lonny's RFQ body — free-form date range | LOW (regex) |
| `etd_requested` | 155/155 | Lonny's RFQ — departure date ask | LOW (mirror parse_eta_requested) |
| `rate_expiry` | 155/155 | OL rate response — "valid through DATE" | MEDIUM (regex + table lookup) |
| `erd` | 155/155 | OL booking — Earliest Receiving Date | MEDIUM (named-anchor parser) |
| `origin_free_time` | 155/155 | OL rate — origin free days | MEDIUM (rate-table column) |
| `dest_free_time` | 155/155 | OL rate — destination free days | MEDIUM (rate-table column) |
| `product` | 155/155 | RFQ/booking — commodity description | MEDIUM (keyword + commodity dictionary) |
| `temperature` | 155/155 | Reefer rows — "+2°C", "0F", etc. | LOW (regex) |
| `lonny_notes` | 155/155 | Lonny's RFQ body — any free text | LOW (body-text extraction) |

## Fields significantly empty

| Field | Empty rate | Why (suspected) | Effort |
|---|---|---|---|
| `eta_requested` | 84-87% | parse_eta_requested anchors too narrow | MEDIUM (broaden patterns) |
| `mdolx_ref` | 16% (WIN) / 100% (LOSS) | LOSS rows don't have MDOLX (correct); WIN gaps = cross-thread cases | HIGH (LLM-assisted matcher) |
| `ol_responder_signer` | 15-29% | Email signature parser misses some formats | MEDIUM (signature heuristics) |
| `transshipment` | 16-26% | Some rate tables don't have a TS column | LOW (default "Direct" if column absent) |
| `turnaround_hours` | 16-37% | Standalone WINs lack the Lonny RFQ timestamp | (intentional — chain incomplete) |

## Recommended order of attack

### Quick wins (1-2 hours each)
1. **`temperature`** — reefer rows have explicit temperature in subject/body.
   `2X40'RF +2C` → `+2°C`. Simple regex.
2. **`product` / commodity** — Lonny's RFQ subjects + body contain
   "Hilmar Cheese", "WPC 80", "MPI", "Skim Milk Powder", etc. Extract
   into commodity dictionary.
3. **`requested_dates`** — Lonny writes "ETD 5/22" or "needs by 6/1" in
   the body. Mirror `parse_eta_requested` with broader anchors.
4. **`etd_requested`** — Same — Lonny sometimes asks for departure date
   instead of arrival.

### Medium effort (3-4 hours each)
5. **`rate_expiry`** — OL rate responses say "valid through 5/31" or
   "expires 6/15". Parse from body + rate table.
6. **`origin_free_time` / `dest_free_time`** — Rate tables have these
   columns; parse_rate_table needs to capture them.
7. **`erd`** — Booking confirmation always has an ERD line. Anchor-based
   parser like `parse_origin_cutoff`.
8. **`lonny_notes`** — Default body extraction: take any non-empty body
   text after stripping email signature + RFQ template boilerplate.

### Harder (LLM-assisted)
9. **`mdolx_ref` cross-thread match** — When the Lonny RFQ and OL booking
   confirmation are in separate email threads, deterministic matching
   fails. Use `parser_fallback.py` (already in src/hilmar/) to call
   Anthropic API for fuzzy thread-linking.

## How to test parser improvements safely

For each new parser function:
1. Add the function to `body_parser.py`
2. Wire it into `ingest.py` at the per-row processing loop
3. Re-ingest (NOT the full pipeline — just ingest.py) to populate the
   new field on existing rows
4. Re-run `parser_accuracy.py` to confirm the empty-rate dropped
5. Spot-check 5-10 rows by hand to verify the parser got the RIGHT
   value (not just any value)
6. Commit ingest + body_parser together

## Sentry telemetry

Each new parser function should emit `parser.field_extracted` (counter,
tagged by field name) so we can track extraction rates over time. The
existing `parser.accuracy_per_field` gauge in `parser_accuracy.py`
already covers this — just need to add new fields to FIELD_REQUIREMENTS.
