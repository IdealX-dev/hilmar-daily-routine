# Quote Historian — durable longitudinal stats (Turso / libSQL)

`scripts/historian.py` appends every **finalized** Hilmar quote row (WIN / Q&L /
NQ) to a Turso (libSQL) database, keyed by `request_id`. It exists because
`tracking-data-v2.json` only ever holds the **~14-day fetch window** — terminal
rows age out, so questions like *"win rate over 6 months"* or *"every Hapag
quote this year"* can't be answered from the working state alone.

Added 2026-06-24 (Michael: "i concur with building the data base for stats").

## Why this can't hurt the daily pipeline

The historian is **purely additive and write-only**:

- **Outlook** stays the source of truth for the raw emails.
- **`tracking-data-v2.json`** stays the rebuilt-each-fire working state.
- **The Turso DB** is written but **never read back as authority** by the
  pipeline. A second *read* authority is exactly what caused the QC-038
  phantom-drift problem (retired 2026-05-21); a write-only sink has no such
  failure mode.
- The pipeline step is **best-effort** (`run_pipeline.BEST_EFFORT_STEPS`) and
  the module is a **no-op when no creds are configured**, so it ships dormant
  and can never block or corrupt the client report.

## Status: dormant until provisioned

Out of the box the historian does nothing (logs a dormant notice, exits 0).
The pipeline runs the `Historian (finalized → Turso)` step every fire; until
you provision a DB and drop creds, it's a clean skip. QC-058 (freshness) also
skips silently while dormant.

## Provisioning (one-time)

1. **Create a Turso DB** (free tier is ample for this volume):
   ```bash
   turso db create hilmar-quote-history
   turso db show hilmar-quote-history --url        # → libsql://...
   turso db tokens create hilmar-quote-history     # → auth token
   ```
2. **Install the client on the fire host** (the Cloud PC):
   ```bash
   pip install libsql-experimental
   ```
3. **Drop the creds** in `secrets/historian-turso.txt` (gitignored, chmod 600):
   ```
   libsql://hilmar-quote-history-<org>.turso.io
   <auth-token>
   ```
   …or set `HILMAR_HISTORIAN_URL` + `HILMAR_HISTORIAN_TOKEN` in the environment.

That's it — the next fire starts appending. The schema is created on first
write (`CREATE TABLE IF NOT EXISTS`), so there's no migration step.

## Configuration precedence (first match wins)

| Source | Use |
|---|---|
| `HILMAR_HISTORIAN_SQLITE=/path.db` | Local sqlite3 — tests / offline backfill (no Turso, no libsql) |
| `secrets/historian-turso.txt` | Production Turso creds (URL line 1, token line 2) |
| `HILMAR_HISTORIAN_URL` + `HILMAR_HISTORIAN_TOKEN` | Production creds via env (CI) |

Because libSQL is SQLite-compatible, the same SQL runs against a local
`sqlite3` file — which is how the test suite exercises the module with no
network or Turso account.

## Schema — table `quote_history`

One row per `request_id` (upserted; re-running a fire updates in place, never
duplicates; `first_seen_at` is preserved across updates).

| Column | Notes |
|---|---|
| `request_id` (PK) | stable row id; `stand_*` for standalone WINs |
| `report_date` | business day of the fire that last wrote the row |
| `status` / `display_status` | raw (`WIN`/`LOSS`/`PENDING`) + 4-state label (`WIN`/`Q&L`/`NQ`) |
| `origin` / `destination` / `lane` / `pol` / `pod` | lane |
| `carrier_quoted` / `carrier_won` | normalized carrier names |
| `ol_rate` | OL quoted rate (USD) |
| `teu_requested` / `container_count` | volume |
| `etd_offered` / `eta_offered` | schedule |
| `mdolx_ref` / `loss_reason` / `quoted` | booking ref, loss reason, quoted flag |
| `request_timestamp` / `response_timestamp` | Lonny ask / OL reply |
| `subject` / `conversation_id` | provenance |
| `first_seen_at` / `last_updated_at` | UTC ISO write stamps |

Indexed on `report_date`, `carrier_quoted`, `destination`, `display_status`.

Only finalized rows are written (`display_status != PENDING`) so the history is
stable — a row enters once it has a real outcome.

## Operating

```bash
python scripts/historian.py            # upsert finalized rows from tracking-data
python scripts/historian.py --dry      # show what WOULD upsert, write nothing
python scripts/historian.py --status   # row count + hours since last write
```

## Example longitudinal queries

```sql
-- Win rate by carrier over all history
SELECT carrier_quoted,
       SUM(display_status='WIN')  AS wins,
       SUM(display_status='Q&L')  AS losses,
       ROUND(100.0*SUM(display_status='WIN')/NULLIF(SUM(display_status IN ('WIN','Q&L')),0),1) AS win_pct
FROM quote_history GROUP BY carrier_quoted ORDER BY wins DESC;

-- Every quote to Busan this year, newest first
SELECT report_date, carrier_quoted, ol_rate, display_status
FROM quote_history WHERE destination='Busan' ORDER BY request_timestamp DESC;

-- Monthly volume
SELECT substr(report_date,1,7) AS month, COUNT(*) AS decided
FROM quote_history GROUP BY month ORDER BY month;
```

## Monitoring

**QC-058** (historian freshness) WARNs in the daily audit if the historian is
configured but its newest write is >26h old (the append is failing). It never
ERROR-gates — a write-only analytics sink must not block the client report —
and skips silently while dormant. No live data is ever lost if the historian
falls behind: `tracking-data-v2.json` rebuilds from Outlook every fire, so a
backfill (`python scripts/historian.py`) catches the history back up.
