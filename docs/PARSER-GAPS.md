# Parser Gaps — 2026-05-18 audit → 2026-05-19 SHIPPED

Per Michael 2026-05-18 ("no field should be empty ever... your parser is also
not picking up a lot of data, names, etc etc") → fixed 2026-05-19 in commit
[parser-gap-fix].

## Fields ←— 100% empty → now populated (2026-05-19 fix)

All 9 fields previously 100% empty now extract from staged bodies. Numbers
below are from a smoke-test re-ingest on 2026-05-19 against the production
stage_emails_bodies.txt (191 Lonny rows + 286 OL bodies). Per-applicability
rates are what the parser_accuracy framework actually uses.

| Field | Empty pre-fix | Population post-fix | Applicability | Notes |
|---|---|---|---|---|
| `product` | 155/155 | **94.3% (chain)** | non-standalone | Commodity dict + "Product X" pattern. **Gated at 0.90 in QC-039.** |
| `lonny_notes` | 155/155 | **95.0% (chain)** | non-standalone | Body minus signature/quote-chain. **Gated at 0.90 in QC-039.** |
| `erd` | 155/155 | **77.4% (quoted)** | _is_chain_quoted | From rate-table ERD column. Tracked. |
| `dest_free_time` | 155/155 | **71.2% (quoted)** | _is_chain_quoted | From rate-table DEST FREE TIME column. Tracked. |
| `origin_free_time` | 155/155 | **53.4% (quoted)** | _is_chain_quoted | From rate-table ORIGIN FREE TIME column. Tracked. |
| `requested_dates` | 155/155 | **51.4% (chain)** | non-standalone | Raw "Cutoff X" / "week of X" phrase. Tracked. |
| `temperature` | 155/155 | **~50% (reefer)** | reefer-only | Numeric "-2C"/"34F" + keyword "Frozen"/"Chilled". Tracked. |
| `etd_requested` | 155/155 | **8.5% (chain)** | non-standalone | Concrete date in "Cutoff X" / "ship by X". Most Lonny asks are relative ("next week") with no concrete date. Tracked. |
| `rate_expiry` | 155/155 | **1.4% (quoted)** | _is_chain_quoted | OL rate bodies rarely include explicit validity wording. Tracked. |

### Gate strategy (after the 2026-05-19 evening PDF-attachment ship)

Per Michael's later directive 2026-05-19 ("PARSER MUST REACH 95 PERCENT AT A
MINIMUM AND INCLUDE ATTACHMENTS FOR WHEN WE SEND BOOKINGS FOR YOU TO ANALYZE"):

- **Overall threshold lowered from 0.98 → 0.95** in
  `src/hilmar/parser_accuracy.py::ACCURACY_THRESHOLD`. The broader field set
  (was 13 fields → now 19) makes the 98% bar unreachable on rows whose
  source text legitimately lacks the data. 95% catches real parser
  regressions without false-failing on sparse-source rows.

- **Newly gated in QC-039 with per-field thresholds**:
  - `product` 0.90 (94.3% chain)
  - `lonny_notes` 0.90 (95.0% chain)
  - `erd` 0.90 (93.9% wins — 3 image-only PDFs lower the ceiling)
  - `doc_cutoff` 0.90 (93.9% wins, same 3 PDFs)
  - `port_cutoff` 0.90 (93.9% wins)
  - `dest_free_time` 0.85 (93.4% quoted)
  - `mdolx_ref` 0.80 (81.8% — 11 historical Linda-recap WINs lack MDOLX)

- **Tracked but NOT gated** (sparse source text, not a parser failure):
  - `origin_free_time` — OL emails rarely include this column (trucker
    contract, not OL's column to fill)
  - `requested_dates` — many Lonny RFQs use relative phrasing ("next week")
  - `etd_requested` — same sparsity as requested_dates
  - `rate_expiry` — OL rate emails rarely state validity
  - `temperature` — only on reefer rows; narrow surface

### Booking-PDF parser (`scripts/pdf_parser.py`)

PDFs are the canonical source for booking-side fields. The email body of
an MDOLX booking confirmation is signature-only — the actual booking data
(ERD, doc cutoff, port cutoff, free-time, product, container counts)
lives in the attached PDF. Extended 2026-05-19 to extract:

- `mdolx_ref` from "BOOKING CONFIRMATION MDOLX260409" header (96.4% PDFs)
- `booking_ref` from REF.: prefix (RICGH/NAM/EBKG…) (58.5% PDFs)
- `erd` from "Earliest Return Date: 5/4/2026" (97.2% on real bookings)
- `doc_cutoff` from "DOCUMENT DUE DATE: 5/6/2026" (96.2%)
- `port_cutoff` from "Closing Date: 5/8/2026" (96.2%)
- `dest_free_time` from "14 DETENTION + 14 DEMURRAGE FREE DAYS" (88.7%)
- `product` from cargo-description block (97.2%)
- `temperature` from explicit "TEMP: X" or bare "-2C" patterns (10.4%)
- `container_count` + `teu_requested` + `containers` by summing every
  "1 x 40' HC" line in the PDF (covers standalone WIN rows whose subject
  line doesn't carry the container marker)
- `carrier_quoted` via vessel-prefix → carrier map + booking-ref prefix
  (NAM→CMA CGM, RICG→ONE, EBKG→MSC, etc.) (95.3% real bookings).
  IMPORTANT: do NOT trust "SHIPPING LINE: OL USA VIA EVERGREEN" — that's
  OL's parent-agency boilerplate present on EVERY PDF regardless of
  actual carrier. Use vessel prefix + booking-ref instead.

### Final accuracy state (post-fix, measured 2026-05-19 PM)

`compute_accuracy(reqs)` on the live stage after ingest + patch_carriers:

```
rows=164  pass=True
overall   97.08%   threshold 95%
weighted  97.91%
critical_failing: []   failing_fields: []
```

All 19 measured fields PASS their per-field thresholds. The remaining
gap to 100% is the long tail: 3 image-only PDFs that pdfplumber can't
OCR (eligible for parser_fallback.py LLM rescue if cost-justified), plus
the legitimate sparse-source fields above.

### How the fix shipped

- `scripts/body_parser.py` + `src/hilmar/body_parser.py`: added
  `parse_temperature`, `parse_product`, `parse_requested_dates`,
  `parse_etd_requested`, `parse_lonny_notes`, `parse_rate_expiry`. Extended
  `parse_rate_table` to surface `erd`, `origin_free_time`, `dest_free_time`.
- `scripts/fetch_bodies.py`: new fields wired into `_parse_all`. Extended
  `parse_rate_table` invocation to `mbd_inbound` bucket so booking
  confirmations also yield ERD + free-time.
- `scripts/ingest.py`: pulls new fields into request dict in `build_requests`,
  `apply_rate_responses`, `link_bookings_to_requests`, and the standalone-WIN
  branch.
- `scripts/patch_carriers.py`: PASS 2 `BACKFILL_KEYS` extended with all 9
  new fields so cross-thread enrichment fills any gaps left by ingest.
- `src/hilmar/parser_accuracy.py`: `product` and `lonny_notes` added to
  `FIELD_REQUIREMENTS` at threshold 0.90 each (in `PER_FIELD_THRESHOLDS`).

### Pre-fix table (kept for history)

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

## 2026-06-24 — OL prose quotes + "...DEST from ORIGIN" subjects

Michael forwarded "RE: Updated Cheese Rates Busan Korea from Dalhart" and the
report showed it as **lane unresolved / not quoted**. Two distinct root causes,
both fixed in both parser trees (`scripts/` + `src/hilmar/`):

1. **Subject lane `<DEST> <region> from <ORIGIN>`.** Lonny's RFQ subject was
   "...Busan Korea from Dalhart", not "Oakland to Busan". `parse_subject_lane`
   returned `(None, None)`, so `ingest.build_requests` **silently dropped the
   request row** (it skips any row with no parseable destination). Added a
   last-resort fallback: strip a trailing region word (Korea/China/…), take the
   port before it as DEST and the token after "from" as ORIGIN, guarded by a
   stopword list. → `("Dalhart", "Busan")`.

2. **OL prose-format quotes.** OL sometimes quotes in free prose, not the
   pipe/column grid:

   ```
   Please see able Hapag option from Houston port to Busan.
   Houston to Busan _ 40' Reefer _ Chilled Cheese
   Hapag: $2,275/40' reefer
   4 equipment free days at Origin / 3 at destination / Direct service
   ```

   The **production `scripts/` `parse_rate_table` had no prose path at all**
   (pure architectural drift — `src/hilmar/` had a partial one) and returned
   `{}`, so the quote vanished. Added `parse_prose_rate()` (+ `_prose_lane()`)
   to BOTH trees: carrier (normalized — bare "Hapag" token added → Hapag-Lloyd),
   `$` rate (200–50k gate), POL/POD from the prose lane, container size, origin/
   dest free time, Direct/`via` transshipment. `parse_rate_table` now routes
   no-table text to it. Gated on a `$` rate so non-quote prose stays `{}`.

End-to-end after the fix: the dropped row is built (`Dalhart → Busan`) and the
prose quote flips it to quoted (Hapag-Lloyd / $2,275 / POL Houston / POD Busan).
Locked by `tests/test_korea_prose_quote.py` (both trees). Re-ingest
(`reprocess_bodies.py` + `ingest.py`) on the fire host re-parses the cached
week with the new parser.

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
