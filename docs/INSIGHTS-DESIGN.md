# Daily Insights + Self-Healing + Self-Learning — Design Spec

**Owner:** Michael Deitchman
**Status:** Spec locked 2026-04-26. Implementation = M3.9 → M3.12.
**Read order:** read this AFTER `HANDOFF.md`. This extends M3.

---

## TL;DR

After every daily run, the pipeline produces an **Insights section** appended to the daily email covering:
1. **System** — technical critique of itself (test gaps, slow paths, schema drift, parser regressions)
2. **Design (visual)** — dashboard/UX suggestions
3. **Data** — schema/coverage suggestions (fields we should track but don't)
4. **Business (Hilmar)** — carrier strategy, lane plays, win-rate movers

Powered by **Claude Opus** (hard-coded default) with **task-type-keyed model override** so we can dial down later without code changes. Cost is **uncapped with an alert at $2/day**. Self-healing extends the existing 7-phase QC engine. Self-learning = rolling baselines + a feedback log Michael can rate, tuning future runs.

---

## What's already covered (M3.5 in HANDOFF.md)

`scripts/qc_selfheal.py` → port to `src/hilmar/qc.py`. 7 phases:
1. File health (file exists, parses, schema-conformant)
2. Structure (required keys, types)
3. Entry healing (per-request field coercion, status re-derivation via `core.decide_status`)
4. Dedup (request_id collisions)
5. Summary recompute (rebuild aggregates from raw)
6. Cross-check rules (QC-001..004 — Q&L plausibility, WIN has carrier, WIN has send-signal, NQ contamination)
7. Persist with backup

**Don't change behavior in the port.** Add new phases as M3.9 (below).

---

## New scope (M3.9 → M3.12)

### M3.9 — Extend self-healing (`src/hilmar/qc.py` adds 3 new phases)

| Phase | What it does | Self-healing action |
|---|---|---|
| **8 — Parser-regression detection** | Scan last 14 days; for each parser (`body_parser.parse_rate_table`, `parse_eta_offered`, etc.), compute "miss rate" = % of messages where parser returned `None` vs prior baseline | If miss-rate > 2× baseline → flag in `qc-result.json.warnings`, surface in next-run insights |
| **9 — Ingest-gap detection** | Compute typical daily message volume from `lupfold@hilmaringredients.com`. Compare today's count | If today < 0.4× rolling 14d median → flag as "possible ingest gap" (Outlook outage? mailbox rule change?) |
| **10 — Schema-drift detection** | Walk every request; check for fields present in some entries but missing in others (likely new field added by recent ingest) | Auto-add missing field as `null` to all entries (idempotent), flag as "schema migration applied" |

All three log to `reports/qc-result.json` under `selfheal_actions[]` with timestamp + before/after counts.

### M3.10 — Self-learning baselines (`src/hilmar/baselines.py`)

A new module that maintains rolling statistics in `data/baselines.json`:

```json
{
  "version": 1,
  "updated_at": "2026-04-26T07:00:00Z",
  "windows": {
    "rolling_14d": {
      "ingest_volume_p50": 12,
      "ingest_volume_p90": 24,
      "biz_hours_response_p50": 1.8,
      "biz_hours_response_p90": 4.2,
      "win_rate_pct": 28.5,
      "win_rate_pct_stddev": 3.1
    },
    "rolling_90d": {
      "carrier_lane_winrate": {
        "ONE.HCMC": 78.2,
        "ANL.Melbourne": 65.0,
        "MAERSK.Singapore": 22.1
      }
    }
  }
}
```

**Update once per run, BEFORE insights generation runs.** The current run's metrics get appended; baselines roll forward. `baselines.json` is gitignored (runtime state) but backed up via `backup.py`.

**Self-learning ≠ ML.** It's stats with memory. Future runs flag deviations from the baseline ("response time today P50=3.4h, baseline P50=1.8h → 88% slower"). Insights consume `baselines.json` to write narrative.

### M3.11 — Insights engine (`src/hilmar/insights.py`)

The new module that produces the daily insights. Two halves:

#### M3.11.a — Rule-based metrics engine (no LLM)
Computes a **`InsightsContext`** dataclass each run:

```python
@dataclass
class InsightsContext:
    # Today's snapshot
    total: int
    wins: int
    quoted_lost: int
    not_quoted: int
    pending: int
    win_rate_pct: float

    # Deltas (today vs baseline)
    win_rate_delta_pp: float        # percentage points vs rolling 14d
    response_time_delta_pct: float  # +88% if response slowing
    volume_delta_pct: float

    # Patterns
    carrier_mix: dict[str, int]              # {"ONE": 12, "ANL": 8, ...}
    lane_top_3: list[tuple[str, int]]
    biggest_wins_today: list[Request]        # WINS sorted by TEU desc
    aging_pendings: list[Request]            # PENDING > 16h biz hours

    # Anomalies (from baselines comparison)
    anomalies: list[Anomaly]                 # e.g. "MAERSK win-rate dropped 30pp"

    # System-health metrics
    qc_fixes_today: int
    parser_miss_rates: dict[str, float]
    test_coverage_pct: float | None          # latest CI coverage if available
    ingest_gap_flagged: bool
```

#### M3.11.b — LLM narrative engine (Claude Opus)

Calls **Claude Opus** (`claude-opus-4-6`) with the `InsightsContext` + a structured prompt. Returns four sections in a single response:

```
SECTION A — System (technical critique of the pipeline itself)
SECTION B — Design (visual / UX critique of the dashboard + email)
SECTION C — Data (schema / coverage suggestions — fields to add, segments to split)
SECTION D — Business (Hilmar-domain advice — carriers, lanes, negotiation plays)

Each section: 2-5 bullets. Each bullet:
  • observation (what changed / what we noticed)
  • why it matters (link to win rate, response time, or carrier negotiation leverage)
  • recommended action (concrete, owner-assigned where possible)
```

Output saved to `reports/insights/<YYYY-MM-DD>.json` (structured) + `reports/insights/<YYYY-MM-DD>.html` (rendered for email append).

#### Email integration
`render.py` (M3.6) appends `insights-<date>.html` as a clearly-labeled bottom section (`<details><summary>📊 Insights & Suggestions (Apr 26)</summary>...</details>`) — collapsed by default, easy to ignore on routine days, easy to expand when curious.

#### Feedback loop (the "self-learning" part)
Each insight rendered with a tracking ID and "👍 helpful / 👎 not helpful / 💤 noise" three buttons. Buttons are mailto: links that pre-fill an email back to a `hilmar-feedback@idealx.us` mailbox (or just back to Michael):
- Subject: `INSIGHT-FEEDBACK <id> <rating>`
- The pipeline ingests these emails and writes to `data/insights-feedback.json`
- Future runs feed the feedback log into the LLM prompt as "what worked, what didn't" → biases future generation toward signal Michael cares about

### M3.12 — Model router (`src/hilmar/model_router.py`)

Default = Opus. But the router is built first-class so we can dial down later without rewriting `insights.py`.

```python
TASK_TYPE_TO_MODEL = {
    "metrics_narrative":   "claude-opus-4-6",      # default Opus per Michael
    "system_critique":     "claude-opus-4-6",
    "design_suggestions":  "claude-opus-4-6",
    "data_suggestions":    "claude-opus-4-6",
    "business_advice":     "claude-opus-4-6",
    "feedback_synthesis":  "claude-opus-4-6",
}

# Env override — flip any task to a cheaper model without code change:
#   HILMAR_INSIGHTS_MODEL=claude-sonnet-4-6           ← all tasks → Sonnet
#   HILMAR_INSIGHTS_MODEL_business_advice=claude-haiku-4-5  ← per-task override

class ModelRouter:
    def select(self, task_type: str) -> str: ...
    def call(self, task_type: str, prompt: str, system: str | None = None,
             max_tokens: int = 4096) -> ModelResponse: ...
    def daily_cost_cents(self) -> int: ...
```

**Cost telemetry:** every call appends to `data/llm-cost-log.jsonl`:
```json
{"ts":"2026-04-26T07:00:12Z","task":"business_advice","model":"claude-opus-4-6","input_tokens":3892,"output_tokens":612,"cost_cents":18}
```

**Alert (no hard cap):** if `daily_cost_cents() > 200` (= $2/day), append a banner to the daily email:
> ⚠️ Hilmar Insights spent $2.47 on Anthropic API today. Above the $2 alert threshold. Set `HILMAR_INSIGHTS_MODEL=claude-sonnet-4-6` to dial down, or raise the alert in `config.json`.

The pipeline does NOT halt — it just informs.

**Cascade-down on errors:**
- Rate-limited (429)? Retry once, then fall back to Sonnet for that task only. Log it.
- API down? Skip the LLM section entirely; ship the rule-based metrics + a note "LLM-narrative skipped: API unavailable."

---

## File additions (post-M3 layout)

```
src/hilmar/
├── qc.py                     # M3.5 + M3.9 (extends to 10 phases)
├── baselines.py              # M3.10
├── insights.py               # M3.11
├── model_router.py           # M3.12
├── feedback_ingest.py        # M3.11 — reads feedback emails into data/insights-feedback.json
└── prompts/
    ├── system_critique.md    # task-specific prompts (versioned)
    ├── design_suggestions.md
    ├── data_suggestions.md
    ├── business_advice.md
    └── feedback_synthesis.md

data/                         # gitignored runtime state
├── baselines.json            # rolling stats
├── insights-feedback.json    # 👍👎 history
└── llm-cost-log.jsonl        # per-call cost telemetry

reports/
├── insights/
│   ├── 2026-04-26.json       # structured insights output
│   └── 2026-04-26.html       # rendered email section
└── ...

tests/
├── test_qc.py                # extends to cover phases 8/9/10
├── test_baselines.py
├── test_insights.py          # mocks Anthropic API
├── test_model_router.py      # mocks routing + cost tracking
└── fixtures/
    ├── insights_context.json
    └── opus_response_sample.txt
```

---

## Environment variables (additions to `.env.example`)

```bash
# Anthropic API (provision a new key on IdealX, store here, chmod 600)
ANTHROPIC_API_KEY=

# Default model — Opus per Michael's pick 2026-04-26
HILMAR_INSIGHTS_MODEL=claude-opus-4-6

# Per-task override (optional, blank = use HILMAR_INSIGHTS_MODEL)
# HILMAR_INSIGHTS_MODEL_business_advice=claude-sonnet-4-6
# HILMAR_INSIGHTS_MODEL_design_suggestions=claude-haiku-4-5

# Alert threshold in cents (200 = $2.00)
HILMAR_INSIGHTS_COST_ALERT_CENTS=200

# Insights-feedback inbox (Michael's idealx address — keeps Hilmar feedback off OL mailbox)
HILMAR_INSIGHTS_FEEDBACK_TO=michael.deitchman@idealx.us
```

---

## Test approach

- **Mock Anthropic API** with `responses` library (same pattern as `graph_client` tests). Never make real LLM calls in tests.
- **Golden-day fixture** — run insights against `tests/fixtures/golden_day.json`, assert structure of output (sections present, baselines updated, cost log appended).
- **Self-heal phases 8/9/10** — fixture for "missing field" / "ingest gap day" / "parser regression" scenarios; assert the heal action is taken and logged.
- **Cost-log accumulation** — synthetic 10-call test, assert `daily_cost_cents()` returns expected sum.
- **Feedback ingestion** — fixture email with subject `INSIGHT-FEEDBACK <id> 👍`, assert the insight gets tagged in `insights-feedback.json`.

Coverage gate stays at 85% — these new modules should hit it easily.

---

## What "self-learning" actually means here (set expectations)

This is **NOT machine learning.** No models trained, no embeddings, no neural nets. What we have:

- **Memory:** `baselines.json` and `insights-feedback.json` persist between runs.
- **Adaptation:** the LLM narrative gets a feedback summary in its prompt → biases future generation toward what Michael actually values.
- **Observation:** the rule-based engine flags deviations from baselines automatically. Over weeks, baselines firm up, signal strengthens.

If you ever want real ML (e.g., predict win probability per quote), that's a separate Phase 4 effort and needs months of clean data. Don't scope-creep into it.

---

## Autonomy Framework — what the system can do without asking

Locked 2026-04-26. The pipeline has authority to act unsupervised in three tiers. Tier-3 actions ALWAYS require Michael's explicit go.

### Tier 1 — Full authority (auto-apply, no review, no ask)
The system DOES these. Every action is logged to `reports/autonomy-log.jsonl` with timestamp, before/after counts, and reversibility hint.

| Action | Reversibility | Where logged |
|---|---|---|
| Data heals (status re-derivation, dedup, summary recompute) | Backup snapshot before every run; rollback CLI | qc-result.json |
| Schema additions (new field with `null` default across all entries) | Reversible by removing field; idempotent | qc-result.json |
| Aging transitions (PENDING→LOSS/NO_RESPONSE after 24h biz) | Reversible via backup | autonomy-log.jsonl |
| Baseline statistics updates (rolling P50/P90, win-rate-per-carrier-lane) | `baselines.json` is append-mode-versioned | baselines.json |
| Insights-feedback ingestion (👍/👎 → bias future LLM prompts) | Feedback log is append-only | insights-feedback.json |
| New dashboard tab for emerging use case (additive only) | Old tabs untouched; new tab can be deleted | autonomy-log.jsonl |
| Backup rotation (keep N=14, prune older) | Pre-rotation manifest saved | autonomy-log.jsonl |
| Archive sweeps (yesterday's outputs → reports/history/) | Files moved, not deleted | autonomy-log.jsonl |

### Tier 2 — Auto-apply with safety net (auto + reversibility + Michael notified)
The system DOES these but flags them in the daily insights section so Michael sees what changed.

| Action | Safety net | Trigger |
|---|---|---|
| Parser auto-rollback | If miss-rate > 2× baseline for 3 consecutive days, revert parser to prior git tag | M3.9 phase-8 detection |
| Threshold tweak A/B (e.g., `pending_aging_hours` 24 → 20) | Apply for 1 run, compare KPIs, auto-revert if worse | LLM suggestion + KPI guardrail |
| New visualization additions (sparklines, heatmaps, anomaly bands) | Additive only — new tab, never replacing core 7 tabs | LLM "design suggestion" Tier-2 flag |
| Email subject-line A/B test (open-rate driven) | Two variants for 5 days; keep winner; revert on open-rate drop > 10% | Out of scope for M3 |
| Auto-tightened anomaly thresholds | Tighten only; loosening requires Michael approval | Baseline drift detection |

Every Tier 2 action gets a top-of-email banner: `🤖 System auto-applied: <action>. Effective <date>. Revert: <CLI command or git revert hint>.`

### Tier 3 — Suggest only, Michael approves (NEVER autonomous)
The system writes a recommendation in the daily insights but waits for explicit go before applying.

| Domain | Examples |
|---|---|
| Status-machine logic | New status enum, changed thresholds in `core.decide_status` |
| Send distribution | Adding/removing recipients on `distribution.full_list` |
| Hilmar-facing communication | Subject lines, sender identity, body templates, anything that touches Hilmar |
| Schema breaking changes | Renaming or dropping fields, type narrowing |
| Code refactors | New modules, reorganizations, dependency changes |
| External API additions | New connectors, new outbound calls |
| Stop-conditions | Pausing daily run, disabling the timer |

Tier 3 suggestions land in the LLM-narrative section of the daily email with a clear `🛑 Awaiting Michael's go-ahead:` prefix.

### Override knobs
- `HILMAR_AUTONOMY_TIER=1` in `.env` caps the system at Tier 1 only (the most conservative — recommended for the first month).
- `HILMAR_AUTONOMY_TIER=2` (default after the first month) — Tier 1 + 2 active.
- `HILMAR_AUTONOMY_TIER=3` reserved (would auto-apply Tier 3 too — DO NOT enable; it'd void the safety contract above).

### Audit trail
- `reports/autonomy-log.jsonl` — every autonomous action (Tier 1 + 2) appended with `{ts, tier, action, before, after, reversible, revert_hint}`.
- Weekly digest in the daily email Friday: "🤖 System acted autonomously this week: N Tier-1 heals, M Tier-2 adjustments, K Tier-3 suggestions awaiting your call."
- Full audit: `python -m hilmar.autonomy_audit --since 7d` prints a readable timeline.

### Self-growth boundary (the "growing" rule)
The system grows in 3 dimensions only:
1. **More data** — baselines deepen, history accumulates, feedback log grows
2. **More observations** — new patterns detected, new anomalies surfaced as data accumulates
3. **More dashboard surfaces** — additive tabs/sparklines as use cases emerge

It does NOT grow in these dimensions autonomously:
- Code modules (Tier 3)
- Schema (breaking — Tier 3; additive — Tier 1 OK)
- Hilmar-facing artifacts (Tier 3)
- Distribution / sender (Tier 3)

If the LLM suggests "we should integrate Salesforce" or "we should add a Slack notifier" — that's Tier 3, queued for Michael, never auto-built.

---

## Order of implementation (Claude Code, follow this exactly)

1. **M3.9** — extend `qc.py` with phases 8/9/10 + tests
2. **M3.10** — `baselines.py` + tests (writes `data/baselines.json`)
3. **M3.12** — `model_router.py` first (insights depends on it) + tests with mocked Anthropic
4. **M3.11.a** — `insights.py` rule-based engine + tests
5. **M3.11.b** — LLM-narrative engine + 5 prompt files + tests
6. **M3.11.c** — render integration: `render.py` appends insights HTML to email body
7. **M3.11.d** — feedback ingestion: `feedback_ingest.py` + tests

Stop after each numbered step, run `pytest -v && ruff check src tests`, commit, push. We review at every commit before the next step.

---

## Don't

- Don't make real Anthropic API calls in CI or tests.
- Don't write per-message LLM calls. **One LLM call per task type per day** is the cost model. Five total daily calls (system, design, data, business, feedback synthesis).
- Don't store `ANTHROPIC_API_KEY` in code, env defaults, or commits. `.env` only, chmod 600.
- Don't change the existing 7-phase QC behavior. Only add phases 8/9/10.
- Don't auto-act on insights ("the system suggested X, so it ran X"). All suggestions are advisory, Michael decides.
- Don't break the dry-run gate. Insights run in dry-run too — they just don't get emailed.

— Michael Deitchman • 2026-04-26
