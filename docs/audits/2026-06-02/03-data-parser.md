# Hilmar Daily Tracker — Data Integrity & Parser Accuracy Audit

**Date:** 2026-06-02
**Auditor:** Claude (read-only)
**Scope:** state machine, loss-reason taxonomy, carrier extraction, NQ/Q&L boundary, schema drift, timezone hygiene, rate parsing, additive merge, parser-accuracy gate.
**Constraints:** read-only — no code changes, no branches, no PRs. PR #21's UNDIFFERENTIATED work and #14–#19 foundation fixes are out of scope (do not re-recommend).

This report calls out each finding with severity (Critical / High / Medium / Low), the file(s) involved, what's wrong, what to do, and effort. A prioritized "Top 5" closes the document.

---

## 1. State-machine soundness (`decide_status`)

Both `src/hilmar/core.py:570-746` and `scripts/core.py:679-814` implement the same decision tree but with one structural difference: `scripts/core.py:733-735` returns `PENDING / MDOLX_NO_SEND` BEFORE the send-aging branch, while `src/hilmar/core.py:649-675` reaches MDOLX_NO_SEND AFTER the `has_send AND !has_mdolx` branch. **Result: a row with both a stale send (>48h) AND a fresh MDOLX, where the matcher failed to pair them, classifies as SEND_NO_BOOKING in src/hilmar but PENDING/MDOLX_NO_SEND in scripts/.** The parity test (`tests/test_core_parity.py`) does not cover this input combination.

| Transition | Reachable? | Notes |
|---|---|---|
| WIN | yes | strict has_send_eff AND has_mdolx_eff |
| PENDING / AWAITING_MDOLX | yes | send received, awaiting MDOLX |
| PENDING / MDOLX_NO_SEND | yes (rare) | anomaly; scripts/ may steal this from SEND_NO_BOOKING |
| Q&L / SEND_NO_BOOKING | yes | aging branch fires correctly |
| NQ / NO_RESPONSE | yes | |
| NQ / RESPONSE_NO_RATE | yes | |
| Q&L / UNDIFFERENTIATED | yes (PR #21) | |
| Q&L / PRICE | yes | requires lane median + ≥3 lane wins |
| Q&L / ETD_MISS | yes | etd_fit_days ≥ 5 |
| Q&L / QUOTED_NOT_BOOKED | yes | no rate, no ETD, no lane med |
| Q&L / OTHER | yes (rare) | unparseable response_timestamp |
| Q&L / COVERED | **unreachable from `decide_status`** | only set via `qc_selfheal._reclassify_covered` (scripts/qc_selfheal.py:256) or the explicit `lonny_covered` honor path (scripts/qc_selfheal.py:594) |
| Q&L / DRAFT_ONLY | **unreachable from `decide_status`** | listed in `scripts/core.LOSS_REASONS` (line 84) and `src/hilmar/core.LOSS_REASONS` (line 105) but nothing in either tree ever sets it |

**Finding 1.1 — DRAFT_ONLY is dead** (Medium, `scripts/core.py:84`, `src/hilmar/core.py:105`). Listed in `LOSS_REASONS` but never written. Either wire it up (original intent: MDOLX confirmations of `DRAFT RATED` quotes with no full rate) or drop the enum so QC-040 stops permitting it.

**Finding 1.2 — MDOLX_NO_SEND ordering drift** (Medium). See above — canonicalize ordering across both trees, add fixture.

**Finding 1.3 — `record_transition` only fires on status change** (Low, `src/hilmar/core.py:749-762`). When `loss_reason` changes but `status` is the same (e.g., PRICE → UNDIFFERENTIATED), no audit-trail row is written. PR #21 will quietly retag many rows on first run with no history. Consider an aux `loss_reason_history`.

---

## 2. Loss-reason taxonomy — heuristic-as-determination patterns

PR #21 fixed PRICE-as-catch-all. Other reasons still over-classify:

**Finding 2.1 — RESPONSE_NO_RATE collapses two failure modes** (High, `src/hilmar/core.py:683-687`). MBD-acknowledged-but-didn't-quote is NQ/RESPONSE_NO_RATE → excluded from win-rate denominator. Many of these are real competitive losses (MBD couldn't get a competitive rate, ghosted, competitor booked). No distinction between "wait, checking" (legit NQ) and "we ran the desk and ghosted" (real Q&L). Add a `ol_declined_to_quote` sub-flag, or cross-reference `lonny_covered=True` (see finding 4.1).

**Finding 2.2 — `aggregate_loss_reasons` "other" bucket hides COVERED + UNDIFFERENTIATED together** (Medium, `src/hilmar/core.py:1029-1043`). The actionable_mix lumps COVERED (a competitor-won-the-rate signal, fundamentally rate-driven from Lonny's perspective) into "other" alongside UNDIFFERENTIATED. Daily email banner under-reports rate pressure. Promote COVERED to `rate_driven` or its own bucket.

**Finding 2.3 — ETD_MISS threshold is magic 5** (Medium, `src/hilmar/core.py:709`, `scripts/core.py:778`). Unlike `PRICE_GAP_THRESHOLD_MULT`, the ≥5d cutoff has no constant. Promote to `ETD_MISS_DAYS = 5`, mirror, add to parity test.

**Finding 2.4 — `analyze_lane_regression` LEGACY-only filter + missing RESPONSE_NO_RATE exclusion** (High, `scripts/gen_rate_intelligence.py:158-161`). Filter `q.get("status") not in ("WIN", "LOSS")` — never matches STRICT form's Q&L/NQ; in STRICT form this analysis returns ZERO rows. Separately, it excludes `NO_RESPONSE` but not `RESPONSE_NO_RATE` or `SEND_NO_BOOKING` — a lane repeatedly hitting RESPONSE_NO_RATE looks like a "losing streak" when OL just never quoted. Fix to STRICT-aware (`display_status` / `is_quoted_and_lost`) AND broaden the NQ exclusion (or invert to "must have ol_rate").

---

## 3. Carrier extraction — is CMA CGM 66% real or parser bias?

The 66% concentration QC-017 surfaced today has **three contributing biases**:

**Finding 3.1 — Carrier alias map folds APL + ANL + CMA + CGM into "CMA CGM"** (High, `src/hilmar/core.py:200-246`, `scripts/core.py` equivalent). The alias map declares APL and ANL as CMA CGM subsidiaries:

```python
"APL":       "CMA CGM",    # APL is a CMA CGM subsidiary
"ANL":       "CMA CGM",    # ANL is a CMA CGM subsidiary
```

Corporate truth: APL operates ships under its own brand and quotes through a separate sales channel; ANL is a niche reefer line. Collapsing them inflates CMA CGM's count without reflecting how Lonny actually negotiates. **If 5–8 of the 103 CMA CGM quotes are actually APL or ANL, the real CMA CGM share is 60–63%, not 66% — and QC-017 would shift from WARN to OK.** Recommend: split APL/ANL out of the alias map (keep them aliased to themselves with case-canonical "APL", "ANL") and add per-line scorecards. The "CMA CGM family" rollup is fine as a derived view but should not be the canonical bucketing.

**Finding 3.2 — `src/hilmar/body_parser._find_carrier` is naked substring match** (High, `src/hilmar/body_parser.py:616-625`). `for tok in _CARRIER_TOKENS: if tok in up`. `"ONE" in up` matches "DONE"/"PHONE"/"STANDALONE"; `"CMA" in up` matches inside CMAU… container number prefixes. Production is safe — scripts/body_parser uses column extraction — but this is the migration target per CLAUDE.md §2 and the test suite touches it. Replace with `re.search(rf"\b{re.escape(tok)}\b", up)`.

**Finding 3.3 — QC-017 silently undercounts in STRICT classifier** (Critical, `scripts/qc_selfheal.py:2576`):

```python
if c and (r.get("status") in ("WIN", "LOSS")) and (r.get("quoted") or r.get("status") == "WIN"):
```

In STRICT form (current schema.json line 76 enforces `"status": enum WIN/Q&L/PENDING/NQ`), `status == "LOSS"` is impossible. **QC-017 then only counts WIN rows.** If Hilmar's tracking-data is currently STRICT, the "66%" denominator is purely WINs — much more concentrated than the prose implies. This is a wrong number on the audit email TODAY. Replace with `display_status(r) in ("WIN", "Q&L")` or `not is_not_quoted(r)`.

---

## 4. NQ vs Q&L boundary

The NQ/Q&L distinction drives win-rate accuracy: NQ is excluded from the denominator, Q&L is included.

**Finding 4.1 — COVERED rows can be silently NQ** (Critical, `scripts/qc_selfheal.py:593-600`):

```python
if r.get("lonny_covered"):
    prior_status = r.get("status")
    if prior_status != "LOSS" or r.get("loss_reason") != "COVERED":
        r["status"] = "LOSS"
        r["loss_reason"] = "COVERED"
```

The honor path sets `status=LOSS, loss_reason=COVERED` but **never sets `quoted=True`**. If the row arrived with `quoted=False` (no rate was extracted from OL's body), the row stays `LOSS/quoted=False/COVERED`. In `aggregate_summary` (scripts/core.py:891-893), that row lands in the `nq` bucket — excluded from win-rate. But **Lonny said "covered with a competitor"** — a contest happened and we lost. This row should count as Q&L. **Win rate is biased upward by every COVERED-without-rate row.** Fix: when applying lonny_covered, also set `quoted = True` (we know OL was given the chance; the absence of an extracted rate is a parser miss or off-system quote). Or, in STRICT form, demote NQ → Q&L explicitly on COVERED.

**Finding 4.2 — `RESPONSE_NO_RATE` rows that were really covered** (High, see finding 2.1). Same direction of bias as 4.1.

**Finding 4.3 — `drift_check.phase6_covered_honor` is keyed to LEGACY-only** (Medium, `scripts/drift_check.py:182`): `if r.get("lonny_covered") and (r.get("status") != "LOSS" or r.get("loss_reason") != "OTHER")`. The check requires `loss_reason == "OTHER"` but the canonical post-honor value is `COVERED`. So this check fires WARN even after the honor path successfully ran. False-positive in the audit. Should be `not in ("OTHER", "COVERED", None)`.

**Finding 4.3 — `aggregate_summary` `win_rate` includes NQ in the denominator** (Critical, `src/hilmar/core.py:832,850`, `scripts/core.py:896,914`):

```python
total_decided = len(wins) + len(ql) + len(nq)
"win_rate": round(len(wins) / total_decided * 100, 1) if total_decided else 0.0,
```

CLAUDE.md §6 hard rule: **"Win Rate = Wins / (Wins + Q&L). NQ excluded."** The per-lane email math in `scripts/gen_email.py:1067` correctly excludes NQ (`_decided_comp = b['won'] + ql_count`), but the headline `summary.win_rate` does NOT — it includes NQ. The headline KPI on the dashboard and email subject is computed off `summary.win_rate`. **This is a wrong number on the client email TODAY.** Either fix `aggregate_summary` to drop nq from `total_decided`, or rename the field and stop using it as the headline. QC-047 (per CLAUDE.md §7) asserts "Win Rate KPI ↔ explainer banner" alignment but does not assert "Win Rate KPI matches the documented formula" — it slipped through.

---

## 5. Schema drift / contamination

**Finding 5.1 — `schema.json` missing UNDIFFERENTIATED, COVERED, DRAFT_ONLY in `loss_reason` enum** (Critical, `schema.json:84`):

```json
"loss_reason": {"enum": ["NO_RESPONSE", "RESPONSE_NO_RATE", "QUOTED_NOT_BOOKED", "PRICE", "ETD_MISS", "OTHER", "AWAITING_MDOLX", "MDOLX_NO_SEND", "SEND_NO_BOOKING", null]}
```

Missing: `UNDIFFERENTIATED` (added 2026-06-02, in `LOSS_REASONS` set), `COVERED` (used by qc_selfheal lonny_covered honor), `DRAFT_ONLY` (in LOSS_REASONS, never written). If any consumer validates against schema.json (e.g., the SHARED `client_intelligence` schema check, QC-031, ol-quote-tracker registry sync), a row with `loss_reason="UNDIFFERENTIATED"` will FAIL validation. **PR #21 likely just shipped writes of UNDIFFERENTIATED into tracking-data-v2.json with no corresponding schema update.** Update schema.json to add the three values; bump version.

**Finding 5.2 — `NQ_CONTAMINATION_FIELDS` does not include `ol_rate`** (Medium, `src/hilmar/qc.py:45-51`):

```python
NQ_CONTAMINATION_FIELDS = (
    "carrier_quoted", "vessel_voyage", "etd_offered", "eta_offered", "transshipment",
)
```

`ol_rate` is wiped separately a few lines later (qc.py:419-421) — but only inside the `phase_3_entries` flow, not part of the named tuple. If another caller relies on the contamination list (e.g., a future SHARED-export filter), `ol_rate` will leak on NQ rows.

**Finding 5.3 — Legacy `vessel` field is not in `NQ_CONTAMINATION_FIELDS`** (Low, `scripts/merge_ingest.py:133`). `vessel` is set on WIN rows; `vessel_voyage` is the canonical. If `vessel` is dead code, drop it; if alive, add to contamination list to prevent NQ contamination via the legacy alias.

**Finding 5.4 — Preserved-from-prior rows escape full out-of-scope exclusion** (Low, `scripts/ingest.py:1352`). The exclusion check passes `{"subject": w.get("subject")}` only — body / lane / commodity rules don't apply. A row preserved before the scope rule was added is never evicted. Add a one-time sweep on prior load.

---

## 6. Date / timezone hygiene

CLAUDE.md hard rule §6: code/logs/database/timestamps in UTC; only user-facing email/chat in ET.

**Finding 6.1 — `body_parser._find_date_near` uses `datetime.utcnow()` and Python 3.12+ deprecates it** (High, `src/hilmar/body_parser.py:331`, `scripts/body_parser.py:376`):

```python
now_year = datetime.utcnow().year
```

`datetime.utcnow()` is deprecated in 3.12 and naive (no tzinfo). On the Cloud PC running 3.12 this emits a DeprecationWarning; in 3.13 it's removed. Use `datetime.now(timezone.utc).year`. The `now_year` is the fallback year for date parses missing a year — if the system clock somehow returns a naive date in PT and we're near a year boundary, we'd land on the wrong year for "Dec 31" parses. Edge case but real.

**Finding 6.2 — `scripts/run_pipeline.py` uses naive `datetime.now()`** (Medium, `scripts/run_pipeline.py:260,360,405`):

```python
started = datetime.now()
elapsed_s = (datetime.now() - started).total_seconds()
```

Naive `datetime.now()` returns local time. The Cloud PC is on ET, so the deltas are right, but the printed timestamps shouldn't be naive — Sentry breadcrumbs depend on these. Use `datetime.now(timezone.utc)`.

**Finding 6.3 — `merge_ingest.py:89,91` uses Unix-only `%-I` strftime** (Critical, `scripts/merge_ingest.py:89,91`):

```python
lonny_pt = req_dt.astimezone(core.PT).strftime("%-I:%M %p PT")
olusa_et = resp_dt.astimezone(core.ET).strftime("%-I:%M %p ET")
```

CLAUDE.md §3 hard rule #8: "Never use `%-d` / `%-I`". This file is not currently called by `run_pipeline.py` (I checked) so the breakage is latent — but the moment merge_ingest.py is re-introduced (it's still in the tree), it crashes on Cloud PC. Either delete merge_ingest.py if dead, or fix to `%I` + `.lstrip("0")`.

**Finding 6.4 — `render.py:85` naive `datetime.now().strftime(...)`** (Low). Result is local time; should be ET-explicit so internal "generated_at" matches logs.

**Finding 6.5 — `auto_chase_pending.py:120,157` naive `datetime.now()` in flag filenames** (Low). Midnight-ET race; `chase-sent-YYYY-MM-DD.flag` could double-fire or skip. Use `datetime.now(core.ET)`.

---

## 7. Rate parsing robustness

`parse_rate` (`src/hilmar/core.py:1415-1438`, `scripts/core.py` equivalent) handles `"$3,500"`, `"$3500/40HC"`, numeric `3500`. PR #21 added `compute_lane_winning_medians`. Two robustness concerns:

**Finding 7.1 — `compute_lane_winning_medians` uses **all-time** dataset, not a 30-day window** (Medium, `src/hilmar/core.py:903-967`). Per the docstring: "WIN scope = ALL WINs in the input dataset." Tracking-data is described as "an active rolling window," but the window is not enforced here. If a 4-month-old WIN at $4500 on Oakland→Yokohama is still in the dataset (e.g., a preserved_from_prior row), the lane median drifts upward and current losses get classified UNDIFFERENTIATED instead of PRICE. Pass a `since: datetime` cutoff (e.g., 30 days).

**Finding 7.2 — `parse_rate_per_feu` size inference defaults to 40' for 45' containers** (Low, `src/hilmar/core.py:1441-1469`). The size inference is `if "20" in rate_str → 20 elif "40" in rate_str → 40 else default 40`. A `"$3500/45HC"` rate string defaults to 40-equivalent. Hilmar does occasionally ship 45-footers. Add a `45` branch.

**Finding 7.3 — `parse_rate_table` floor inconsistency between column and prose paths** (Low, `src/hilmar/body_parser.py:751,816`). Column parser drops anything <$200; prose fallback requires ≥$500. A genuinely small rate ($300, e.g., a short-haul intra-Asia leg) is accepted by one path, rejected by the other. Reconcile to a single floor.

---

## 8. Additive merge — 9 WINs preserved daily

`scripts/ingest.py:1297-1386` carries forward prior WINs the fresh stage didn't reproduce. QC-010 (`scripts/qc_selfheal.py:902-919`) warns when `len(preserved) > 10`. Today's 9 is one below the threshold.

**Finding 8.1 — QC-010 threshold is static, no trend detection** (High, `scripts/qc_selfheal.py:909`). `PRESERVED_THRESHOLD = 10`. CLAUDE.md says "small steady set is fine; growing means refresh_stage is missing legitimate emails." Today's 9 is one below threshold — tomorrow's 10 fires WARN once, then 11/12/13 are identical noise. Add a 7-day rolling mean: WARN when today's count >1.5σ above the mean regardless of absolute. Persist `preserved_count` in `daily_snapshots/{date}.json` summary.

**Finding 8.2 — QC-010 does not distinguish preservation REASON** (Medium, `scripts/qc_selfheal.py:902-919`). Counts the flag but doesn't sub-bucket by (a) MDOLX matched but search-window missed (refresh_stage gap — BAD), (b) lane+date match with renormalized request_id (parser drift), (c) pure prior carry-forward (stale data). Audit can't tell them apart. Add `preserved_reason` on the carried row.

---

## 9. Parser accuracy at 95%

`ACCURACY_THRESHOLD = 0.95` (`src/hilmar/parser_accuracy.py:54`). QC-039 ERROR-gates the pipeline at <95% overall or <per-field-threshold.

I cannot run the harness against live `tracking-data-v2.json` (file is empty in this snapshot — see qc-result.json `"total": 0`). Looking at the per-field thresholds (lines 63-86):

| Field | Threshold | Source comment |
|---|---|---|
| `mdolx_ref` | 0.80 | 9 of 62 historical WINs awaiting manual backfill |
| `product` | 0.90 | 94.3% on chain |
| `lonny_notes` | 0.90 | 95.0% on chain |
| `erd` | 0.90 | 93.9% wins |
| `doc_cutoff` | 0.90 | 93.9% |
| `port_cutoff` | 0.90 | 93.9% |
| `dest_free_time` | 0.85 | 93.4% quoted |

**Finding 9.1 — `erd` / `doc_cutoff` / `port_cutoff` sit at 93.9%, one bad PDF away from breakage** (High, `src/hilmar/parser_accuracy.py:63-86`). Per-field threshold 0.90, real rate 93.9%, three image-only PDFs drive most of the misses. One more image-only PDF and any of these flips below threshold → QC-039 ERROR → pipeline gates. Either lower thresholds to 0.88 (if 93.9% is the data ceiling) or wire a Tesseract/OCR fallback.

**Finding 9.2 — Standalone WIN exclusion may hide `product`/`lonny_notes` misses** (Medium, `src/hilmar/parser_accuracy.py:183-184`). `not _is_standalone(r)` excludes ALL standalones — but a standalone with a body (e.g., subject `"… Cheese 2x40RF …"`) could and should be measured for `product`. Net: over-reports rate.

**Finding 9.3 — `weighted_accuracy` is reported but not gated** (Low, `src/hilmar/parser_accuracy.py:274`). If 14 rare fields at 99% drag the equal-weight mean up while 3 critical fields with 100s of rows each sit at 92%, overall passes; critical-fields gate catches the worst case but not all. Adding `weighted_rate >= 0.95` would be defensive.

---

## 10. Other findings

**Finding 10.1 — `aggregate_carriers.win_rate = wins / quotes` includes Pending** (High, `src/hilmar/core.py:1101`). Per-carrier denominator = Wins + Q&L + Pending; per-lane email denominator = Wins + Q&L. Two different formulas between aggregate_carriers and the row renderer. CLAUDE.md spec is Wins / (Wins + Q&L) — fix aggregate_carriers.

**Finding 10.2 — `aggregate_lanes` / `aggregate_carriers` hard-code STRICT status checks** (Medium, `src/hilmar/core.py:863-967`). Helpers `is_quoted_and_lost` and `is_not_quoted` (lines 145-162) exist but are unused — the aggregators compare against `STATUS_Q_AND_L` / `STATUS_NQ`. LEGACY LOSS rows silently drop out of per-lane buckets. Route through the storage-agnostic helpers.

**Finding 10.3 — `compute_dod.cutoff_iso` mixes ET and UTC** (Low, `src/hilmar/core.py:1869`). `as_of.isoformat() + "T23:59:59+00:00"` — that's UTC even though `as_of` was derived ET. A row at 2026-06-02T22:00 ET (= 2026-06-03T02:00 UTC) lands on the wrong day. Convert consistently.

---

## Top 5 data-integrity priorities

1. **Fix `aggregate_summary.win_rate` to drop NQ from denominator (Finding 4.4, Critical).** This is the headline Win Rate on every daily client email. Today it's `Wins / (Wins + Q&L + NQ)`; CLAUDE.md §6 says it must be `Wins / (Wins + Q&L)`. Per-lane math already does the right thing — the headline KPI does not. Effort: 2 lines in `src/hilmar/core.py:832` + `scripts/core.py:896`, mirrored, plus QC-047 test that asserts the formula. Add a parity test.

2. **Update `schema.json` `loss_reason` enum to include `UNDIFFERENTIATED`, `COVERED`, `DRAFT_ONLY` (Finding 5.1, Critical).** PR #21 ships writes of UNDIFFERENTIATED today; the schema does not allow it. Any downstream validator (SHARED export, ol-quote-tracker registry sync) will reject the new value. Effort: 1-line schema change + bump version + QC-040 cross-folder enum drift check sees both trees. Tag DRAFT_ONLY as deprecated if it's truly unused.

3. **Fix QC-017 STRICT-classifier blindness (Finding 3.4, Critical).** `status in ("WIN", "LOSS")` excludes Q&L and NQ entirely in STRICT mode — meaning today's "66%" denominator is purely WINs and the real concentration may be very different. Switch to `display_status(r) in ("WIN", "Q&L")` or `not is_not_quoted(r)`. Effort: 1-line change at `scripts/qc_selfheal.py:2576` + a STRICT-form fixture test.

4. **COVERED rows must count as Q&L, not NQ (Finding 4.1, Critical).** The `lonny_covered` honor path doesn't set `quoted=True`, so COVERED rows with `quoted=False` (no extracted rate) get bucketed NQ and excluded from win-rate. Lonny saying "covered" is the canonical signal of a real lost contest. Fix: in `scripts/qc_selfheal.py:593-600`, also set `r["quoted"] = True` whenever `lonny_covered=True` (and in STRICT, set status=Q&L not LOSS). Also fix `drift_check.phase6_covered_honor` to accept `COVERED` as a valid honored reason (currently it requires `OTHER`). Effort: 4 lines, two files.

5. **Decouple APL / ANL from CMA CGM bucketing AND tighten `src/hilmar/body_parser._find_carrier` (Findings 3.1 + 3.2, High).** The 66% CMA CGM concentration is partially inflated by APL/ANL fold-in and at risk from naked-substring carrier matching in src/. Production today is protected (scripts/body_parser uses column extraction), but the src/ migration will surface the bug. Split APL and ANL out of `CARRIER_ALIASES` to standalone canonical names; replace `_find_carrier`'s `if tok in up` with word-boundary regex. Effort: ~10 lines + tests + add an APL/ANL family-rollup helper for analytics that genuinely want the parent-company view.

**Honorable mentions:** Finding 2.4 (LEGACY-only filter in gen_rate_intelligence — zero rows in STRICT); Finding 6.3 (merge_ingest.py Unix-only strftime — latent crash); Finding 9.1 (three fields at 93.9%, one OCR miss from gating).
