"""
historian.py — durable, append-only longitudinal store of FINALIZED Hilmar
quote rows in a Turso (libSQL) database.

WHY (Michael 2026-06-24, "i concur with building the data base for stats"):
tracking-data-v2.json only ever holds the ~14-day fetch window, so terminal
rows age out and "win rate over 6 months" / "every Hapag quote this year"
cannot be answered. This historian appends each row that has reached a
terminal state (WIN / Q&L / NQ) to a Turso table keyed by request_id, so the
longitudinal record survives beyond the window.

It is PURELY ADDITIVE and cannot endanger the daily pipeline:
  - Outlook stays the source of truth.
  - tracking-data-v2.json stays the rebuilt-each-fire working state.
  - This DB is WRITE-ONLY from the pipeline's perspective — we never read it
    back as authority — so it cannot drift the daily run (the QC-038
    phantom-drift lesson: a second read-authority is what caused that).

GRACEFUL DEGRADATION: with no creds configured the whole module is a no-op
(exit 0 + notice), exactly like sync_to_quote_tracker.py — so it ships dormant
and never breaks the fire. Activate by provisioning a Turso DB and dropping
creds (see docs/HISTORIAN.md).

CONFIG (first match wins):
  - HILMAR_HISTORIAN_SQLITE=/path/to.db   → local sqlite3 (tests / offline)
  - secrets/historian-turso.txt           → line 1 = libsql URL, line 2 = token
  - HILMAR_HISTORIAN_URL + HILMAR_HISTORIAN_TOKEN env → Turso via libsql

CLI:
  python scripts/historian.py            # upsert finalized rows from tracking-data
  python scripts/historian.py --status   # row count + latest write age
  python scripts/historian.py --dry      # show what WOULD upsert, write nothing
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core as C  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
DEFAULT_TRACKING = ROOT / "tracking-data-v2.json"

TABLE = "quote_history"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  request_id          TEXT PRIMARY KEY,
  report_date         TEXT,
  status              TEXT,    -- raw stored status (WIN/LOSS/PENDING)
  display_status      TEXT,    -- 4-state label (WIN/Q&L/NQ)
  origin              TEXT,
  destination         TEXT,
  lane                TEXT,
  pol                 TEXT,
  pod                 TEXT,
  carrier_quoted      TEXT,
  carrier_won         TEXT,
  ol_rate             REAL,
  teu_requested       INTEGER,
  container_count     INTEGER,
  etd_offered         TEXT,
  eta_offered         TEXT,
  mdolx_ref           TEXT,
  loss_reason         TEXT,
  quoted              INTEGER,
  request_timestamp   TEXT,
  response_timestamp  TEXT,
  subject             TEXT,
  conversation_id     TEXT,
  first_seen_at       TEXT,    -- set once on first insert, never overwritten
  last_updated_at     TEXT
);
"""

_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ix_qh_report_date  ON {TABLE}(report_date)",
    f"CREATE INDEX IF NOT EXISTS ix_qh_carrier      ON {TABLE}(carrier_quoted)",
    f"CREATE INDEX IF NOT EXISTS ix_qh_destination  ON {TABLE}(destination)",
    f"CREATE INDEX IF NOT EXISTS ix_qh_disp_status  ON {TABLE}(display_status)",
)

# Column order used by both the INSERT and the row→tuple builder.
_COLS = (
    "request_id", "report_date", "status", "display_status", "origin",
    "destination", "lane", "pol", "pod", "carrier_quoted", "carrier_won",
    "ol_rate", "teu_requested", "container_count", "etd_offered", "eta_offered",
    "mdolx_ref", "loss_reason", "quoted", "request_timestamp",
    "response_timestamp", "subject", "conversation_id",
)


# ───────────────────────── configuration / connection ─────────────────────

def _turso_creds() -> tuple[str | None, str | None]:
    """Resolve (url, token) from secrets/historian-turso.txt or env. None,None
    if unconfigured."""
    f = SECRETS / "historian-turso.txt"
    if f.exists():
        lines = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        if lines:
            url = lines[0]
            token = lines[1] if len(lines) > 1 else None
            return url, token
    url = os.environ.get("HILMAR_HISTORIAN_URL")
    token = os.environ.get("HILMAR_HISTORIAN_TOKEN")
    return (url or None), (token or None)


def is_configured() -> bool:
    """True if SOME backend is configured (local sqlite or Turso)."""
    if os.environ.get("HILMAR_HISTORIAN_SQLITE"):
        return True
    url, _ = _turso_creds()
    return bool(url)


def _connect():
    """Return a DB-API connection (sqlite3 or libsql) or None when dormant.

    Both backends speak the SQLite dialect and the same .execute()/.commit()
    surface, so all SQL below is backend-agnostic.
    """
    sqlite_path = os.environ.get("HILMAR_HISTORIAN_SQLITE")
    if sqlite_path:
        import sqlite3
        conn = sqlite3.connect(sqlite_path)
        return conn
    url, token = _turso_creds()
    if not url:
        return None  # dormant — nothing configured
    libsql = None
    for modname in ("libsql_experimental", "libsql"):
        with contextlib.suppress(ImportError):
            libsql = __import__(modname)
            break
    if libsql is None:
        print("historian: Turso configured but libsql not installed "
              "(pip install libsql-experimental) — skipping.", file=sys.stderr)
        return None
    return libsql.connect(url, auth_token=token)


def ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    for ix in _INDEXES:
        conn.execute(ix)
    conn.commit()


# ───────────────────────────── row mapping ────────────────────────────────

def is_finalized(r: dict) -> bool:
    """A row is finalized once it leaves PENDING (WIN / Q&L / NQ). Standalone
    WINs (stand_*) count — they're real terminal outcomes. Uses the
    storage-agnostic display label so LEGACY and STRICT rows agree."""
    return bool(r) and C.display_status(r) not in (None, "", "PENDING")


def _record_tuple(r: dict, report_date: str) -> tuple:
    g = r.get
    teu = g("teu_requested")
    cc = g("container_count")
    return (
        g("request_id"),
        report_date,
        g("status"),
        C.display_status(r),
        g("origin"),
        g("destination"),
        g("lane"),
        g("pol"),
        g("pod"),
        g("carrier_quoted"),
        g("carrier_won"),
        float(g("ol_rate")) if isinstance(g("ol_rate"), (int, float)) else None,
        int(teu) if isinstance(teu, (int, float)) else None,
        int(cc) if isinstance(cc, (int, float)) else None,
        g("etd_offered"),
        g("eta_offered"),
        g("mdolx_ref"),
        g("loss_reason"),
        1 if g("quoted") else 0,
        g("request_timestamp"),
        g("response_timestamp"),
        g("subject"),
        g("conversation_id"),
    )


def _upsert_sql() -> str:
    cols = ", ".join(_COLS)
    placeholders = ", ".join("?" for _ in _COLS)
    # Update every column EXCEPT request_id (the key) and first_seen_at
    # (set once on insert). last_updated_at is stamped separately below.
    updates = ", ".join(f"{c}=excluded.{c}" for c in _COLS if c != "request_id")
    return (
        f"INSERT INTO {TABLE} ({cols}, first_seen_at, last_updated_at) "
        f"VALUES ({placeholders}, ?, ?) "
        f"ON CONFLICT(request_id) DO UPDATE SET {updates}, "
        f"last_updated_at=excluded.last_updated_at"
    )


# ─────────────────────────────── operations ───────────────────────────────

def upsert_finalized(requests: list[dict], report_date: str, *, conn=None) -> int:
    """Upsert every finalized row. Idempotent — re-running a fire updates the
    existing row (first_seen_at preserved) rather than duplicating. Returns the
    number of rows written. Caller-supplied conn is used as-is (tests); else a
    fresh connection is opened and closed."""
    own = conn is None
    if own:
        conn = _connect()
    if conn is None:
        return 0
    try:
        ensure_schema(conn)
        now = datetime.now(timezone.utc).isoformat()
        sql = _upsert_sql()
        n = 0
        for r in requests:
            if not is_finalized(r) or not r.get("request_id"):
                continue
            conn.execute(sql, (*_record_tuple(r, report_date), now, now))
            n += 1
        conn.commit()
        return n
    finally:
        if own:
            with contextlib.suppress(Exception):
                conn.close()


def latest_write_age_hours(*, conn=None) -> float | None:
    """Hours since the most-recent write, or None when dormant / empty. Used by
    QC-058 to verify the historian is being fed daily."""
    own = conn is None
    if own:
        conn = _connect()
    if conn is None:
        return None
    try:
        with contextlib.suppress(Exception):
            ensure_schema(conn)
            row = conn.execute(
                f"SELECT MAX(last_updated_at) FROM {TABLE}").fetchone()
            if not row or not row[0]:
                return None
            newest = datetime.fromisoformat(row[0])
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - newest).total_seconds() / 3600.0
        return None
    finally:
        if own:
            with contextlib.suppress(Exception):
                conn.close()


def row_count(*, conn=None) -> int | None:
    own = conn is None
    if own:
        conn = _connect()
    if conn is None:
        return None
    try:
        with contextlib.suppress(Exception):
            ensure_schema(conn)
            row = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()
            return int(row[0]) if row else 0
        return None
    finally:
        if own:
            with contextlib.suppress(Exception):
                conn.close()


def _default_report_date() -> str:
    return C.report_business_day(datetime.now(C.ET).date()).isoformat()


def run(tracking_path: Path | None = None, *, dry: bool = False,
        report_date: str | None = None) -> dict:
    """Pipeline entry point. No-op (exit-0 shape) when dormant."""
    if not is_configured():
        return {"configured": False, "written": 0,
                "note": "historian dormant — no Turso/sqlite creds (see docs/HISTORIAN.md)"}
    path = tracking_path or DEFAULT_TRACKING
    if not Path(path).exists():
        return {"configured": True, "written": 0, "error": f"no tracking data at {path}"}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    requests = data.get("requests", [])
    finalized = [r for r in requests if is_finalized(r)]
    report_date = report_date or _default_report_date()
    if dry:
        return {"configured": True, "dry": True, "would_write": len(finalized),
                "report_date": report_date}
    written = upsert_finalized(requests, report_date)
    return {"configured": True, "written": written, "report_date": report_date,
            "total_rows": row_count()}


def main() -> int:
    ap = argparse.ArgumentParser(description="Hilmar quote historian (Turso)")
    ap.add_argument("--dry", action="store_true", help="Show what would upsert; write nothing")
    ap.add_argument("--status", action="store_true", help="Show row count + latest write age")
    ap.add_argument("--tracking", type=str, default=None, help="Path to tracking-data JSON")
    args = ap.parse_args()
    if args.status:
        if not is_configured():
            print("historian: dormant (no creds configured).")
            return 0
        print(json.dumps({"rows": row_count(),
                          "latest_write_age_h": latest_write_age_hours()},
                         indent=2, default=str))
        return 0
    res = run(Path(args.tracking) if args.tracking else None, dry=args.dry)
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
