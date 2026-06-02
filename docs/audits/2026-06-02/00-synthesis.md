# Hilmar Daily Tracker — Full-Codebase Audit Synthesis (2026-06-02)

**Audit date:** Tue 2026-06-02 EOD.
**Inputs:** Six parallel read-only audits — architecture (01), tests (02), data/parser (03), security (04), observability (05), revenue (06). Each is its own file under `docs/audits/2026-06-02/`. This synthesis cross-references them; numbers in `[01-§3.1]` form mean "Track 01, Section 3.1".

---

## TL;DR

1. **Two of today's-headline-KPI numbers on the client email are likely wrong.** `win_rate` divides by `Wins + Q&L + NQ` instead of `Wins + Q&L` `[03-Top5#1]`; `COVERED` rows get bucketed NQ instead of Q&L because `lonny_covered` doesn't set `quoted=True` `[03-Top5#4]`. Both have been in production. Both fix in <10 lines each.
2. **PR #21 ships UNDIFFERENTIATED tomorrow, but `schema.json` doesn't allow it** `[03-Top5#2]`. Any downstream validator (SHARED export, ol-quote-tracker registry) will reject the value the moment a Q&L row gets written.
3. **Rotate the in-chat-leaked credential.** Critical risk if it's an MS account or OL SSO password `[04-Top5#1, 04-§8]`. Other security findings are mostly defense-in-depth.
4. **The audit email itself can silently disappear** because the wrapper invokes `qc_alert_if_needed.py` and `gen_improvements_report.py` unguarded — a crash in either kills the audit chain even though PR #16 protected the pipeline `[05-§1.1, 05-§1.2]`.
5. **The "PRICE 94%" reframe is bigger than the chart** — its second-order effects (schema, Sentry tags, taxonomy honesty, the Lonny conversation, the labeling ask) ripple through 4 of the 6 tracks. The PR landed; the follow-through is the leverage point.
6. **Track 06's verdict still stands: 3 phone calls + 1 Tuesday Brief manual ship this week beat any code Michael could write.**

---

## 🚨 Do tonight or tomorrow morning

These are time-sensitive enough to not wait for a sprint plan.

### 1. Rotate the in-chat-leaked credential `[04-§8]`
That string is now in this session's transcript + my logs. If it's an MS account, OL SSO, or anything that touches the Hilmar pipeline, rotate it now. 15-60 min depending on which credential.

### 2. Hold PR #21 OR ship the schema enum patch alongside it `[03-Top5#2]`
PR #21 has `UNDIFFERENTIATED` ready to write. But `schema.json`'s `loss_reason` enum doesn't include it (nor `COVERED` or `DRAFT_ONLY`). The moment a Q&L row gets that value:
- `validate_data_shape` rejects it → drift_check flags it → audit goes red
- `share_intel` export rejects it → downstream consumers break

**1-line schema fix** plus a bump of `schema_version` is the minimum. Bundle with PR #21 or ship as a follow-up before merging.

### 3. Decide: is `daily-fire.yml`'s schedule actually safe to leave live? `[05-§9.1, Top5#3]`
The cron is on main, no self-hosted runner registered. If anyone registers a runner tomorrow without removing the Task Scheduler trigger, the pipeline fires twice. `outlook_send`'s idempotency flag prevents the double-email, but every other step runs twice (double Sentry events, double Turso sync, double backup, double LLM spend). Three-line PR to comment out the cron until you're ready.

---

## 🔴 Critical findings (cross-track)

Ordered by combined client-impact + leverage. Each has at least one Critical-tier rating from the tracks.

### C-1. `win_rate` denominator is wrong on the daily client email `[03-§4.4, Top5#1]`

`src/hilmar/core.py:832` + `scripts/core.py:896` compute:
```python
win_rate = wins / (wins + q&l + nq)
```
But CLAUDE.md §6 says:
```
Win Rate = Wins / (Wins + Q&L)
```
Per-lane math already does the right thing. The **book-level headline KPI on every email Hilmar receives** does not.

- **Fix:** 2 lines, mirrored across both trees + a QC-047 assertion that locks the formula. Add a `test_core_parity` case.
- **Effort:** XS.
- **Impact:** Every win-rate number on every daily email has been understated. Magnitude depends on NQ count; today's audit shows 2 NQ + 4 Q&L → win-rate denominator was 6 instead of 4 → headline was ~67% of the true value.

### C-2. `COVERED` rows get classified as NQ instead of Q&L `[03-§4.1, Top5#4]`

Lonny saying "covered" is the canonical signal of a real lost contest — we quoted, he went with someone else. But the `lonny_covered` honor path at `scripts/qc_selfheal.py:593-600` doesn't set `r["quoted"] = True`, so COVERED rows with no extracted rate get bucketed NQ and excluded from win-rate.

- **Fix:** 4 lines, two files. Set `quoted=True` whenever `lonny_covered=True`; in STRICT, set status=Q&L not LOSS. Fix `drift_check.phase6_covered_honor` to accept `COVERED` as a valid honored reason (currently requires `OTHER`).
- **Effort:** XS.
- **Impact:** Combined with C-1, the win-rate number has been double-distorted — denominator over-counts NQ AND under-counts Q&L.

### C-3. PR #21's `UNDIFFERENTIATED` isn't in `schema.json` `[03-§5.1, Top5#2]`

PR #21 lands tomorrow. The first Q&L row that hits the new fallback gets `loss_reason="UNDIFFERENTIATED"`. The schema only allows the pre-#21 enum.

- **Fix:** Add `UNDIFFERENTIATED`, `COVERED`, `DRAFT_ONLY` to the enum in `schema.json`. Bump `schema_version`. Mirror QC-040 to catch future enum drift between code and schema.
- **Effort:** XS.
- **Impact:** Blocks PR #21 from being safely deployed.

### C-4. `decide_status`'s MDOLX/Send branch ORDER diverges between trees `[03-§1, 01-§1.3]`

```
scripts/core.py:679-814:        WIN → MDOLX_NO_SEND → AWAITING_MDOLX
src/hilmar/core.py:570-746:     WIN → AWAITING_MDOLX → MDOLX_NO_SEND
```

A row with a stale Send (>48h) AND a fresh unpaired MDOLX gets:
- `SEND_NO_BOOKING` in `src/hilmar` (correct per Reading B)
- `PENDING/MDOLX_NO_SEND` in `scripts/` (the path production runs)

`test_core_parity.py` doesn't test this input combination. The PR #14 parity-test extension covered constants, not branch-order outcomes.

- **Fix:** Reorder scripts/ to match src/hilmar/. Add 3 parametrized parity cases covering the (stale-send, fresh-mdolx) corner.
- **Effort:** S.
- **Impact:** Edge-case row, but a real misclassification — the kind that silently flipped phantom WINs for a month.

### C-5. QC-017 (CMA CGM concentration) is STRICT-blind `[03-§3.4, Top5#3]`

`status in ("WIN", "LOSS")` at `scripts/qc_selfheal.py:2576` excludes Q&L AND NQ entirely in STRICT mode. The "CMA CGM holds 103/156 quotes (66%)" we saw today was computed from WINs alone — the real concentration in the full set may be very different.

- **Fix:** Switch to `display_status(r) in ("WIN", "Q&L")` or `not is_not_quoted(r)`.
- **Effort:** XS.
- **Impact:** Likely under-counting WHO's actually concentrated. Real number might be lower (good news) or higher (bad news) — we currently don't know.

### C-6. The audit email itself can silently disappear `[05-§1.1, §1.2]`

`deploy/run_daily_laptop.cmd:137` invokes `qc_alert_if_needed.py` and Step 5 invokes `gen_improvements_report.py` — both unguarded. If either crashes, the wrapper's remaining steps don't run, and *Michael never sees the audit*. PR #16 protected the pipeline; it didn't protect the wrapper.

- **Fix:** Wrap each in a Python-level try/except → log + continue. Or wrap in `cmd` `&& exit 0` chains. Or mirror PR #16's classification down to wrapper level.
- **Effort:** S.
- **Impact:** Today's QC-021 firing every day already shows the chain misbehaves; one bad day kills the only signal Michael has for "is anything wrong?"

### C-7. `pyproject.toml` has no `norecursedirs` `[02-C-001]`

The 2026-06-01 audit's 22-collection-error fire was caused by pytest scanning a stale `hilmar-tracker/` clone. We removed it manually but nothing prevents recurrence — any future leftover directory triggers the same failure.

- **Fix:** Add `norecursedirs = ["hilmar-tracker", "data-backups", ".venv", "dist", "build"]` to `[tool.pytest.ini_options]`.
- **Effort:** XS.

---

## 🟡 High-priority — this sprint

### H-1. `scripts/` coverage is 11% with no gate `[02-§1, §2]`

42 of 60 `scripts/*.py` files at 0% coverage including `qc_selfheal.py` (2840 LOC), `ingest.py`, `gen_dashboard.py`, `gen_pdf.py`, `outlook_send.py`, `patch_carriers.py`, `drift_check.py`, `backup.py`, `refresh_stage.py`. Production code path is essentially untested. Today's gate measures only `src/hilmar/`.

- **Fix:** Add `--cov=scripts` to `pyproject.toml`, gate initially at 25%, ratchet quarterly. Land 4-6 high-impact smoke tests for `gen_email.build_subject/build_body`, `gen_dashboard`, `gen_pdf`, `outlook_send`.
- **Effort:** XS for the gate; M for the first wave of smoke tests.

### H-2. 12 pipeline subprocesses run without Sentry init `[05-§3.1, Top5#1]`

`run_pipeline.py` initializes Sentry but the steps it shells out to don't. Any uncaught exception in `gen_dashboard`, `gen_pdf`, `patch_carriers`, `share_intel`, etc. only surfaces in `run-log.txt`. Three lines per script → 5× expansion of Sentry's error surface.

- **Fix:** Add `import sentry_setup; sentry_setup.init_for("scriptname")` to each of the 12 entry points.
- **Effort:** S total.

### H-3. QC-021's "wrapper started but pipeline never completed" is a false-positive trainer `[05-§8.1, Top5#2]`

The check scrapes `run-log.txt` looking for step markers — 40KB tail, locale-dependent date parsing, wrong-anchor `max(find())`. Today it fired even though the wrapper DID run. Recurring false positives train the operator to ignore the check, which then misses real wrapper failures.

- **Fix:** Replace log scraping with a wrapper-written `reports/last-fire-summary.json` (the wrapper writes ts + step list + exit codes); QC-021 just reads the file.
- **Effort:** M.

### H-4. `gen_email.build_subject` + `build_body` have zero direct tests `[02-T-001]`

`gen_email.py` is 13% covered. The two entry points that produce the daily client email have no acceptance tests. PR #21 introduces a label change that could break Outlook rendering; nothing locks the entry-point invariants.

- **Fix:** 4-6 tests against `golden_day.json`: subject format/length, body structural blocks, Outlook-safe HTML (no `linear-gradient`, no `data:`, no `&amp;amp;`), distribution invariants.
- **Effort:** M.

### H-5. `phase_6_rules` is 1,878 lines / 53 QC checks in one function `[01-§3.1, Top5#1]`

Biggest structural debt in the codebase. Until it's broken into one-function-per-check, every new QC-NNN compounds the problem and per-check unit testing is impossible.

- **Fix:** Extract each `# QC-NNN` block into its own function `_check_qc_NNN(log, data)`; phase_6_rules just calls them in order. PR #20 did this for QC-011 — proof of pattern.
- **Effort:** L (~2 days), but unlocks every future audit's ability to recommend per-check work.

### H-6. Port `_heal_session_paths` + `save_data_validated` to `src/hilmar/core.py` `[01-§1.1, §1.2]`

These exist only in `scripts/core.py`. The moment any `src/hilmar/` code ships in production (RateIntel migration, the test-target-becomes-canonical decision), they're missing.

- **Fix:** Port both helpers.
- **Effort:** S each.

### H-7. Expand `test_core_parity.py` `[01-§1.3, Top5#3]`

The parity test covers 4 input shapes. The real divergence sits in the QUOTED branches: NO_RESPONSE, RESPONSE_NO_RATE, ETD_MISS, PRICE-with-rate-gap, PRICE-with-bare-rate, UNDIFFERENTIATED vs QUOTED_NOT_BOOKED, `mdolx_refs_all`-only, `send_signal_events`-only. Each new case will likely FAIL on commit — that IS the audit finding.

- **Fix:** Add ~8 parameterized cases.
- **Effort:** M.

### H-8. Replace the real Outlook conversation ID in `scripts/build_real_sample.py:31` `[04-F-7.2]`

A real Outlook conversation ID is committed. If the repo is public (it is), that's PII in git history.

- **Fix:** Replace with synthetic value. If history rewrite is feasible, do that too.
- **Effort:** XS for the file edit; 30 min if rewriting history.

### H-9. `cmd_auth_bg` remote MSAL re-auth path `[04-F-3.1]`

If Michael misses the 80d MSAL window while traveling, the daily pipeline silently breaks and recovery requires RDP into the Cloud PC. The `cmd_auth_bg` path already exists (`outlook_send.py:289-335`) and writes the device code to a file. Trigger it from a GitHub Actions `workflow_dispatch` on the (eventual) self-hosted runner so Michael can re-auth from the phone.

- **Fix:** New `auth-refresh.yml` workflow + `cmd_auth_bg` trigger.
- **Effort:** 1-2 hours.

### H-10. `daily-fire.yml` schedule active without a self-hosted runner `[05-§9.1, Top5#3]` *(also listed in tonight's actions)*

See "Do tonight" #3 above. If you do it tonight, this row clears.

---

## Cross-cutting themes

### Theme 1 — PR #21's ripple isn't done landing

PR #21 changed the loss-reason taxonomy. Its second-order effects show up in 4 of 6 tracks:
- **Schema** (C-3 above): `schema.json` doesn't know UNDIFFERENTIATED.
- **Observability** (`[05-§5]`): hardcoded `loss_reason == 'PRICE'` references in Sentry tags / log queries / dashboards silently miss the new bucket.
- **Parity tests** (H-7): UNDIFFERENTIATED vs QUOTED_NOT_BOOKED is one of the untested divergences.
- **Revenue lens** (`[06]`): the bigger play isn't fixing the chart — it's the "labeling ask" email to Lonny.

**Implication:** Merging PR #21 isn't the end of the change; it's the middle. Treat the ripple as a single follow-up batch.

### Theme 2 — Cross-tree drift is a sustained pattern, not a one-time fix

PR #14 added a parity test. Today's audit found:
- `_heal_session_paths` only in scripts/ (`[01-§1.1]`)
- `save_data_validated` only in scripts/ (`[01-§1.2]`)
- `decide_status` branch order disagrees (C-4)
- Parity test covers 4 cases when it needs 12+ (H-7)
- `CLAUDE.md §2` incorrectly claims `parser_accuracy.py` is paired (`[01-§5.3]`)

**Implication:** The "two trees" pattern needs a clearer rule than "tests target src/hilmar; production runs scripts/". Either pick one canonical and document the migration order (`[01-§Top5#5]`), or accept the dual-tree state and invest in per-function parity locks. Half-measures keep producing surprises.

### Theme 3 — The wrapper is the unprotected layer

PR #16 made `run_pipeline.py` survive best-effort failures. Tracks 05 and 02 both show that the SAME failure modes exist one layer up in `run_daily_laptop.cmd`:
- `qc_alert_if_needed.py` crash → kills audit email (C-6)
- `gen_improvements_report.py` crash → same
- Wrapper Steps 4/5 are sequenced unguarded `[05-§1]`

**Implication:** PR #16's `BEST_EFFORT_STEPS` concept needs a wrapper-layer twin. Either move classification into a Python-level wrapper, or add `cmd`-level `&& exit 0` chains. The wrapper itself is the single-point-of-failure for the audit chain.

### Theme 4 — The honest answer to "94% PRICE" is the labeling ask

Track 03 found the win_rate + COVERED bugs. Track 06's strategic finding is that the bigger move is asking Lonny to label his losses in his booking-confirm replies. If he says yes, OL gets cleaner loss-reason data than any competitor — a permanent differentiator that no other broker can copy because no other broker has the email pipeline.

**Implication:** The numbers-fixing work (C-1, C-2, C-3) is necessary but the strategic move is the email Michael drafts to Lonny THIS WEEK.

### Theme 5 — Dead weight ≠ no impact

Track 01 found ~5,500 LOC across 21 orphan scripts. Track 02 found 42 of 60 scripts at 0% coverage. The orphan scripts inflate the "scripts/ untested" count and pad the audit-routine collection time. Removing them is XS-effort and improves every subsequent number.

**Implication:** Spend 30 min moving the 21 files to `scripts/legacy/` and the coverage gate gets easier to set.

---

## The week ahead — if Michael only does 6 things

In order. Each row is sized for an evening or a single deep block.

| Day | Do | Why | Source |
|---|---|---|---|
| **Tue eve (tonight)** | Rotate the leaked credential. Comment-out `daily-fire.yml` cron. Bundle the schema-enum patch into PR #21 OR hold #21 until schema lands. | Three time-sensitive items. ~45 min total. | C-3, [04-§8], [05-§9.1] |
| **Wed** | Ship win_rate-denominator fix (C-1) + COVERED-as-Q&L fix (C-2) as one PR. Add the parity test cases that lock both. | The headline KPI on Hilmar's daily email is wrong; this is the leverage. ~3 hours. | [03-Top5#1, #4] |
| **Wed late** | Start the `gen_tuesday_brief.py` Lens-1-only one-shot script. Draft the labeling-ask email to Lonny in parallel. | The strategic move for next Tue Jun 9. Tomorrow's effort starts now. | [06-§3, Top5#1] |
| **Thu AM** | Make the Evergreen call (14d silent). Send Lonny the labeling-ask email. | Three phone calls + 1 email beat any code this week. | [06-§7, Top5#2] |
| **Thu / Fri** | Finish Tuesday Brief renderer. Hand-edit it Sun night. Send Tue Jun 9 morning. | The single highest-leverage feature in the repo. | [06-Top5#1] |
| **Fri** | Wrap `qc_alert_if_needed.py` + `gen_improvements_report.py` in `try/except → log + continue`. Add `norecursedirs` to pyproject. Comment-out + smoke-test `gen_email.build_subject`. | Protects the audit chain (C-6) + prevents the pytest-collection-error recurrence (C-7) + lays the rail for H-1 / H-4. | C-6, C-7, [02-T-001] |

Everything else — `phase_6_rules` refactor, scripts/ coverage ratcheting, MSAL remote re-auth, schema-versioning rigor — waits until June 9+. The 12-week productization plan stays on hold until Jun 16+ per Track 06.

---

## Kill list

What to STOP doing or building.

| Kill | Why | Source |
|---|---|---|
| The "94% PRICE → Push carriers" narrative | PR #21 fixed it; the next chart will show the truth. Don't reuse the old slide. | [06-§4] |
| Productization Phase 1 (multi-tenant decoupling) before Jun 16 | Premature with zero prospective tenants + Tuesday Brief unshipped. | [06-§5] |
| Carrier scorecard PDFs as a daily artifact | Built, generated daily, nobody reads them. Move to weekly OR kill entirely. | [06-§Kill list] |
| Manual Sentry babysitting | Once H-2 lands and Sentry tags are clean, Seer + claude_diagnose handle 80%. Stop watching the dashboard. | [05-§3.1] |
| Daily audit-doc cadence | This audit took ~3 hours of agent time. Don't re-run before 2026-07-01 unless the pipeline materially changes. | [06-EOF] |
| The 21 orphan scripts in `scripts/` (~5,500 LOC) | Move to `scripts/legacy/` then delete after a successful daily fire. | [01-§2.1] |
| The remaining `data_range` / `pending_aging_hours` config keys | Unused. One source of truth per setting. | [01-§5.1, §5.2] |

---

## What I'm uncertain about

Calling these out so they don't go unchallenged.

1. **The decide_status MDOLX/Send divergence (C-4) might not matter in practice.** The combination "stale Send + fresh unpaired MDOLX with matcher failure" might happen 0 times a month. The fix is still cheap but the impact-vs-effort priority is debatable.

2. **The `lonny_covered` → quoted=True fix (C-2) might inflate Q&L disproportionately.** If Lonny has historically said "covered" on rows where we never quoted (and the data ingest captured the request anyway), retroactively marking them Q&L moves past-period win-rate numbers down. Want to backfill the change OR start clean from the deploy date?

3. **Whether the labeling ask actually lands with Lonny.** Track 06 calls it the single biggest product win available, but Michael knows Lonny better than any audit. Could be a 30-second "sure" or a "I don't have time for that". The ask is cheap to make either way.

4. **The Tuesday Brief's right-first-template.** Track 06 picked Lens 1 (trade-region pulse) as the safe default. Lens 4 (quote-to-decision velocity) is the most differentiated but has the most data gaps. There's a case for shipping Lens 4 first as a wedge — but more work.

5. **The `scripts/`-vs-`src/hilmar/` migration direction.** Track 01 recommended picking one. Both have valid claims to "canonical" — scripts/ is what runs in production; src/hilmar/ is what tests target. Neither track had a strong argument for which way to go, which means this is a Michael decision based on what RateIntel productization wants.

6. **Whether `daily-fire.yml` should be deleted vs. paused.** Pausing protects against runner-registration-driven double-fires. Deleting closes the door entirely until the migration is genuinely ready. Track 05 recommended commenting out the cron; deletion is the safer call if the migration is >2 months out.

---

## Sources

| File | Lines | Headline |
|---|---|---|
| `01-architecture-health.md` | 590 | phase_6_rules monolith; _heal_session_paths drift; 5500 LOC orphans |
| `02-tests-coverage.md` | 437 | scripts/ at 11% coverage, no gate; build_subject/build_body untested |
| `03-data-parser.md` | 269 | win_rate denominator bug; COVERED → NQ misclassification; UNDIFFERENTIATED missing from schema; decide_status MDOLX branch divergence |
| `04-security-secrets.md` | 498 | Rotate in-chat credential; real conv-ID in `build_real_sample.py`; CI secret cleanup |
| `05-observability-ops.md` | 450 | 12 sentry-blind subprocesses; QC-021 false positives; daily-fire.yml live without runner; wrapper steps 4+5 unguarded |
| `06-revenue-strategy.md` | 219 | Tuesday Brief Jun 9 manual; 3 phone calls; labeling ask email |

All on branch `claude/full-audit-2026-06-02`. Read whichever full report whenever a synthesis bullet here is too compressed.

---

*Next audit no earlier than 2026-07-01 unless the pipeline materially changes or PR #21's ripple is messier than expected.*
