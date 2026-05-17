# Hilmar Rate Desk Tracker — Current State

**Owner:** Michael Deitchman (michael.deitchman@idealx.us / @ol-usa.com)
**Last refresh:** 2026-04-29
**Repo:** `github.com/IdealX-dev/hilmar-tracker` (private)
**Local path:** `C:\Users\TTSWW\OneDrive - IdealX\claude\PROJECT HILMAR\hilmar-tracker`

Single source of truth for the project's current state. Read top-to-bottom once before any work.

---

## Status: live in production

Cut over to live email **2026-04-28**. Daily 09:00 ET (07:00 EDT) systemd timer on the C3 Azure VM picks up Lonny ↔ MBD shared-mailbox traffic, runs the QC pipeline, sends the daily HTML+PDF dashboard from `michael.deitchman@ol-usa.com`, archives to OneDrive.

| | |
|---|---|
| Tests | 481 passing |
| Coverage | 87% (gate at 85%) |
| Schema/data drift | 0 across all 5 levels |
| Hard-contract violations | 0 |
| Last 24h prod errors | 0 |

---

## Architecture (unchanged from M3)

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  Microsoft   │   │  ingest.py   │   │   qc.py      │
│  Graph (M$   │──▶│  (3 buckets, │──▶│  (10 phases, │
│  delegated   │   │   merge,     │   │   self-heal) │
│  device-code)│   │  promote)    │   │              │
└──────────────┘   └──────────────┘   └──────┬───────┘
                                              │
   ┌──────────────────────────────────────────┴───┐
   ▼                  ▼                  ▼        ▼
┌──────────┐    ┌──────────┐    ┌─────────────┐ ┌──────────┐
│ render.py│    │insights. │    │ baselines.  │ │feedback_ │
│ (HTML+   │    │ py (LLM  │    │ py (rolling │ │ ingest.py│
│ PDF)     │    │ narratives│   │  P50/P90)   │ │ (👍/👎/💤)│
└─────┬────┘    └──────────┘    └─────────────┘ └──────────┘
      │
      ▼
┌──────────────┐   ┌──────────────┐
│   send.py    │──▶│ OneDrive     │
│ (Graph SMTP) │   │ archive      │
└──────────────┘   └──────────────┘
```

Authoritative data file: `tracking-data-v2.json` on the VM. Schema: `schema.json` at repo root.

---

## What's locked (don't re-litigate)

| Decision | Answer |
|---|---|
| Repo host | GitHub private, `IdealX-dev/hilmar-tracker` |
| Deploy target | C3 Azure VM (Ubuntu, `20.127.8.119`), `/opt/hilmar-tracker` |
| Schedule | systemd timer `hilmar-tracker.timer`, Mon-Fri 09:00 ET / 07:00 EDT |
| Auth | Microsoft Graph delegated / device-code flow on Michael's personal account |
| Sender on daily email | `michael.deitchman@ol-usa.com` |
| Secrets | `.env` at `/etc/hilmar-tracker/.env` (chmod 600) |
| PDF | WeasyPrint |
| Test framework | pytest, 85% coverage floor (pyproject.toml) |
| Python | 3.11 in CI / on VM; 3.14 local Windows |

---

## QC self-heal architecture (the data-correctness backbone)

Runs BEFORE and AFTER every daily cycle. 10 phases plus invariants. **All field-name lists in heal logic are pinned to `schema.json` by invariant tests** so a typo trips CI before merge.

| Phase | Role | Mutates? |
|---|---|---|
| 1 — Files | Schema + data file presence | no |
| 2 — Structure | Top-level shape (requests, version) | no |
| 3 — Entries | Per-row heal: request_date, lane, containers, NQ contamination, has_send/carrier_won clear on non-WIN, data_range→date_range migration | yes |
| 4 — Duplicates | De-dup by request_id, keep richest | yes |
| 4.5 — Derived fields | equipment_size, rate_per_feu, trade_region, awarded_carrier, validity_window | yes |
| 5 — Summaries | **Sole writer** of summary/lanes/carriers aggregates | yes |
| 6 — Cross-check rules | QC-001..QC-008 | no |
| 8 — Parser regression | Today's parser hit-rate vs baseline P50 | no (selfheal_actions) |
| 9 — Ingest gap | Today's request count vs baseline P50 | no (selfheal_actions) |
| 10 — Schema drift | Bidirectional drift across all 5 schema levels (top-level, summary, lanes, carriers, requests) | yes (intra-data fill only) |
| 7 — Persist | Write healed tracking-data + qc-result | yes |

### QC cross-check rules (Phase 6)

| Rule | Asserts |
|---|---|
| QC-001 | At least one Q&L row when N>10 |
| QC-002 | All WINs have `carrier_won` |
| QC-003 | All WINs have chain-send signal OR MDOLX ref |
| QC-004 | No NQ contamination (carrier_quoted set on NQ) |
| QC-005 | No biz-hours > 100 |
| QC-006 | No teu_requested > 30 |
| QC-007 | No PENDING > 24h (excluding AWAITING_MDOLX/MDOLX_NO_SEND) |
| QC-008 | No `carrier_won`/`awarded_carrier` on non-WIN rows |

### Hard contracts the test suite enforces

- 0 rows with `loss_reason=NO_RESPONSE` + non-empty `response_timestamp`
- 0 legacy `LOSS` rows
- 0 `Q&L` with `quoted=False`
- 0 `WIN` without `carrier_won`
- All 4 statuses {WIN, Q&L, PENDING, NQ} present once dataset > 10 rows
- `data["summary"|"lanes"|"carriers"]` only ever assigned in `qc.py` (single-writer invariant)
- Every field name in `qc.NQ_CONTAMINATION_FIELDS` / `qc.NON_WIN_CARRIER_FIELDS` exists in `schema.json` (catches typos at PR time)
- After QC, `ol_rate` is number-or-None on every row (no string sentinels — display label is render-time only)

---

## Status pipeline (Reading B classifier)

Four states: `WIN`, `Q&L`, `PENDING`, `NQ`. Decided in `core.decide_status` from primary inputs:

| Combination | Status |
|---|---|
| `has_send=True AND mdolx_ref` | WIN |
| `has_send=True AND mdolx_ref=None` | PENDING(AWAITING_MDOLX) — auto-promotes when MDOLX arrives |
| `quoted=True AND not has_send AND aged > 24h` | Q&L |
| `quoted=True AND not has_send AND aged < 24h` | PENDING |
| `quoted=False AND not has_send` | NQ |

Aged Q&L from a stale Send-but-no-MDOLX state demotes to `Q&L(SEND_NO_BOOKING)`.

---

## File inventory

```
hilmar-tracker/
├── schema.json                        ✅ canonical schema, drift-checked by Phase 10
├── pyproject.toml                     ✅ ruff + mypy + pytest + 85% coverage gate
├── .env.example                       ✅ all VM env vars documented
├── HANDOFF.md                         ✅ this file
├── INSIGHTS-DESIGN.md                 ✅ insights/feedback loop design
├── README.md                          ✅ orientation
├── deploy/                            ✅ setup-vm.sh, systemd unit, Actions deploy chain
├── data/                              ✅ tracking-data-v2.json + daily_snapshots/ + baselines.json
├── reports/                           ✅ qc-result.json, generated artifacts
├── src/hilmar/
│   ├── ingest.py                      ✅ Graph fetch, 3-bucket classifier, idempotent merge
│   ├── qc.py                          ✅ 10-phase self-heal engine
│   ├── core.py                        ✅ status classifier, aggregators, derived fields
│   ├── body_parser.py                 ✅ rate / ETA / vessel / transshipment / container parsers
│   ├── render.py                      ✅ HTML + PDF + email templates
│   ├── insights.py                    ✅ rule-based + LLM narrative engine
│   ├── baselines.py                   ✅ rolling P50/P90 windows
│   ├── feedback_ingest.py             ✅ 👍/👎/💤 mailto round-trip
│   ├── send.py                        ✅ Graph SMTP send + OneDrive upload
│   ├── orchestrator.py                ✅ pipeline runner with failure paging
│   ├── model_router.py                ✅ Anthropic API router with cascade-down
│   ├── parser_fallback.py             ✅ LLM parser fallback (cached, budget-capped)
│   ├── logging_config.py              ✅ JSON-lines + text formatters, env-driven
│   ├── graph_client.py                ✅ MSAL device-code, persistent token cache
│   └── templates/                     ✅ Jinja templates for HTML/PDF/email
└── tests/                             ✅ 481 tests, 87% coverage
    ├── fixtures/golden_day.json       ✅ pinned, drift-clean against current schema
    └── test_*.py                      ✅ 30+ test files
```

---

## Operating the production VM

| Task | Command |
|---|---|
| SSH to VM | `ssh -i ~/.ssh/rate-blaster-v2.pem azureuser@20.127.8.119` |
| Tail today's run | `sudo journalctl -u hilmar-tracker.service -f` |
| Run QC on demand | `sudo -u hilmar /opt/hilmar-tracker/.venv/bin/hilmar-qc` |
| Inspect last result | `sudo cat /opt/hilmar-tracker/reports/qc-result.json` |
| Force a (dry-run) tracker run | `sudo HILMAR_DRY_RUN=true -u hilmar /opt/hilmar-tracker/.venv/bin/hilmar-run` |
| Deploy a merged PR | Auto via `deploy.yml` after `test.yml` passes on `main` SHA |

**Never** trigger a non-dry-run send to verify a fix. One authorization = one production send. Memory: `feedback_no_repeat_team_sends.md`.

---

## Memory pointers (Claude Code's auto-memory)

- `feedback_autonomy.md` — execute, don't ask
- `feedback_qc_self_execute.md` — Phase 8/9/10 alerts → diagnose, fix, ship
- `feedback_blanket_authorization.md` — VM/git/deploy ops pre-approved
- `feedback_pre_push_lint.md` — ruff + pytest before push
- `feedback_no_repeat_team_sends.md` — no re-trigger to verify
- `project_production_live.md` — live since 2026-04-28
- `project_status_pipeline.md` — Reading B classifier details
- `reference_vm.md` — VM access details

---

## Adding new behavior

1. New code path that reads/writes a field → make sure schema.json declares it. Phase 10 will flag drift if you forget.
2. New self-heal in Phase 3 → use the field constants (`NQ_CONTAMINATION_FIELDS`, `NON_WIN_CARRIER_FIELDS`) so the schema-pinned invariant test catches typos.
3. New QC cross-check rule → add as QC-009+ in Phase 6, test on golden fixture, document above.
4. New parser → add a regex test, add the field to `parser_fallback._FIELD_PROMPTS` if LLM fallback should pick it up, declare in schema.
5. Run `pytest` + `ruff` before push (CI gates both).
