# Shared Client Intelligence — Schema

This folder is the **cross-project intelligence store** that any of Michael's
client-tracking systems can read/write. Today: Hilmar Tracker writes to
`./hilmar/`. Tomorrow: Rate Tracker writes to `./akamai/`, `./henco/`, etc.
Either system can read **any client's data** — cross-client insights become
trivial (rate parity asks, multi-client carrier scorecards, shared preferred
carriers, anomaly detection).

Schema version: **1** (defined 2026-05-13).

## Folder structure

```
SHARED/client_intelligence/
    SCHEMA.md                       ← this file
    _meta.json                      ← registry of known clients + last-update
    hilmar/                         ← per-client folder
        quotes.jsonl                ← append-only log of every quoted/won row
        wins.jsonl                  ← append-only log of confirmed bookings only
        carrier_summary.json        ← rolled-up carrier performance (derived)
        lane_summary.json           ← rolled-up lane performance (derived)
        _client_meta.json           ← client-specific meta + last-updated
    akamai/                         ← future client (rate-tracker)
    henco/                          ← future client (rate-tracker)
```

Append-only logs preserve full history. Rolled-up summaries are derived
(rebuildable from the logs) for fast consumption.

## Producer side (one per system)

Each system that owns a client writes to the appropriate folder:

| Client | Producer system | Producer script |
|---|---|---|
| `hilmar` | Hilmar Tracker (this repo) | `scripts/share_intel.py export` |
| `akamai`, `henco`, … | Rate Tracker | (to be built, same shape) |

Producers must:
1. Be **idempotent** — appending the same record twice should be a no-op
   (we use a SHA-256 fingerprint of `request_id + status + response_timestamp
   + carrier_quoted + ol_rate` as `_fp` in each quote row).
2. **Never modify other clients' folders** — write only to your own.
3. **Rebuild summaries on every export** — they're derived, not authoritative.
4. **Update `_client_meta.json`** with `last_updated`, `row_count`, etc.
5. **Update `_meta.json`** (global registry) — call the producer's helper.

## Schema — `quotes.jsonl` (append-only)

One JSON object per line. Required fields:

| Field | Type | Required | Notes |
|---|---|---|---|
| `_fp` | string | ✓ | Stable fingerprint for dedup (16-char hex) |
| `_client` | string | ✓ | Lowercase client identifier (`hilmar`, `akamai`, …) |
| `request_id` | string | ✓ | Stable ID within the client system |
| `status` | enum | ✓ | `WIN` / `LOSS` / `PENDING` |
| `loss_reason` | enum | ⚠ | `NO_RESPONSE` / `PRICE` / `ETD_MISS` / `COVERED` / `DRAFT_ONLY` / `OTHER` |
| `request_date` | string (ISO date) | ✓ | When client sent the RFQ |
| `request_timestamp` | string (ISO 8601) | ⚠ | Full timestamp |
| `response_timestamp` | string (ISO 8601) | ⚠ | When carrier/provider responded |
| `origin` | string | ✓ | City name (canonicalized) |
| `destination` | string | ✓ | City name (canonicalized) |
| `lane` | string | ✓ | Display form, typically `Origin → Destination` |
| `pol` | string | ⚠ | Port of Loading |
| `pod` | string | ⚠ | Port of Discharge |
| `containers` | string | ⚠ | Free-text equipment spec |
| `container_size` | string | ⚠ | Normalized (e.g. `1X40HC`) |
| `container_count` | integer | ⚠ | |
| `teu_requested` | integer | ⚠ | |
| `teu_won` | integer | ⚠ | (WIN only) |
| `carrier_quoted` | string | ⚠ | Canonical carrier name |
| `carrier_won` | string | ⚠ | (WIN only) |
| `ol_rate` | number | ⚠ | Quoted rate in USD |
| `etd_offered` | string (ISO date) | ⚠ | |
| `eta_offered` | string (ISO date) | ⚠ | |
| `etd_requested` | string (ISO date) | ⚠ | |
| `eta_requested` | string (ISO date) | ⚠ | |
| `vessel_voyage` | string | ⚠ | |
| `transshipment` | string | ⚠ | |
| `turnaround_biz_hours` | number | ⚠ | |
| `ol_responder` | string | ⚠ | |
| `ol_responder_signer` | string | ⚠ | |
| `mdolx_ref` | string | ⚠ | Booking reference (Hilmar-specific format) |
| `exported_at` | string (ISO 8601) | ✓ | When this row was written to the shared store |

✓ = required; ⚠ = optional but expected when available.

## Schema — `wins.jsonl`

Same shape as `quotes.jsonl`, filtered to `status == "WIN"`. Useful for
booking-confirmed analytics that don't want to filter the larger log.

## Schema — `carrier_summary.json`

```jsonc
{
  "CMA CGM": {
    "quotes": 124,
    "wins": 22,
    "losses": 78,
    "win_rate_pct": 17.7,
    "teu_won": 80,
    "teu_lost": 312,
    "rate_count": 110,
    "rate_min": 500,
    "rate_median": 3500,
    "rate_max": 4500,
    "transit_count": 81,
    "transit_min_days": 12,
    "transit_median_days": 17,
    "transit_max_days": 60,
    "lane_count": 21,
    "last_quote_date": "2026-05-13",
    "last_win_date": "2026-05-12"
  },
  "Evergreen": { ... }
}
```

## Schema — `lane_summary.json`

```jsonc
{
  "Oakland → Yokohama": {
    "quotes": 14,
    "wins": 5,
    "losses": 9,
    "win_rate_pct": 35.7,
    "teu_won": 18,
    "teu_requested": 35,
    "winning_carriers": ["CMA CGM", "Evergreen"],
    "all_carriers": ["CMA CGM", "Evergreen", "ONE"],
    "rate_won_min": 3500,
    "rate_won_median": 3700,
    "rate_won_max": 4193,
    "rate_lost_min": 3200,
    "rate_lost_median": 3650,
    "rate_lost_max": 4500,
    "price_gap_median": -50,
    "transit_median_days": 17,
    "transit_min_days": 12,
    "transit_max_days": 35,
    "last_request_date": "2026-05-12"
  },
  "Oakland → Singapore": { ... }
}
```

## Schema — `_client_meta.json`

```jsonc
{
  "client_id": "hilmar",
  "last_updated": "2026-05-14T12:34:56+00:00",
  "schema_version": 1,
  "row_count": 167,
  "win_count": 77,
  "source_system": "hilmar-daily-routine",
  "source_data": "C:/Users/.../tracking-data-v2.json",
  "carrier_count": 8,
  "lane_count": 37
}
```

## Schema — `_meta.json` (global registry)

```jsonc
{
  "schema_version": 1,
  "last_updated": "2026-05-14T12:34:56+00:00",
  "clients": ["hilmar", "akamai"],
  "client_metadata": {
    "hilmar": { ...as above... },
    "akamai": { ...as above... }
  }
}
```

## Consumer-side conventions

When reading from this store (e.g. rate-tracker consuming Hilmar data):

1. **Read summaries first** for fast queries. The append-only logs are for
   re-deriving or historical analysis.
2. **Check `_meta.json`** to see which clients are populated.
3. **Filter by `_client`** in quote-level queries (records may share lane
   names but represent different clients' contracts — never mix).
4. **Respect freshness** — if `_client_meta.json.last_updated` is >36h old
   on a weekday, the producer system may be down. Surface in your own QC.

## Cross-client analyses we can run from this store

Once multiple clients populate the store, these queries become trivial:

- **Rate parity asks**: lanes where one client pays significantly more than
  another for the same carrier (`Hilmar pays $3500, Akamai pays $4200 on
  Oakland-Yokohama with CMA — Akamai should ask for parity`).
- **Multi-client carrier scorecards**: aggregate win rate, transit time,
  rate competitiveness across all clients to identify carrier-level patterns.
- **Preferred-carrier propagation**: if one client has a strong relationship
  with a carrier (high win rate, fast turnaround), share that signal to other
  clients who might benefit.
- **Lane volume forecasting**: total Hilmar+Akamai+Henco volume on a lane
  is the negotiation lever, not any one client's volume.
- **Anomaly detection**: a quote that's >2σ off the multi-client median is
  suspect.

## Adding a new producer (for rate-tracker integration)

When the rate-tracker comes online for a new client:

1. **Choose the client_id** (lowercase, no spaces — `akamai`, `henco`).
2. **Write the export script** that takes the rate-tracker's data and
   writes to `SHARED/client_intelligence/<client_id>/` using the same
   schema as `scripts/share_intel.py`.
3. **Hook it into the rate-tracker's daily pipeline** — runs once per fire.
4. **Verify in `_meta.json`** that the new client appears after the first
   export.

Reference implementation: `scripts/share_intel.py` in the Hilmar Tracker
repo (`github.com/IdealX-dev/hilmar-daily-routine`).

## Schema evolution

Schema changes happen here. Process:
1. Bump `SCHEMA_VERSION` in `share_intel.py` (and the rate-tracker mirror).
2. Update this file with new field definitions.
3. Old consumers should tolerate missing fields gracefully (use `.get()`,
   never key access).
4. New consumers should tolerate the old schema by defaulting absent fields.

Adding fields is safe (consumers ignore unknowns). Renaming or removing is
breaking — coordinate across all producers + consumers before changing.

---

Last updated: 2026-05-14 · Schema version 1 · See `share_intel.py` for the
canonical producer implementation.
