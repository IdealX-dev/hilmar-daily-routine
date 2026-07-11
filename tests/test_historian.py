"""historian.py — durable Turso/libSQL store of finalized quote rows.

Exercised against a local sqlite3 backend (HILMAR_HISTORIAN_SQLITE) so no Turso
account, network, or libsql dependency is needed — the SQL is identical
(libSQL is SQLite-compatible). Verifies: only terminal rows persist, the upsert
is idempotent (no dupes, first_seen_at preserved), and the module is a clean
no-op when unconfigured (so it can ship dormant without endangering the fire).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import historian as H  # noqa: E402


def _reqs():
    return [
        {"request_id": "r_win", "status": "WIN", "quoted": True, "destination": "Yokohama",
         "lane": "Oakland → Yokohama", "carrier_quoted": "CMA CGM", "carrier_won": "CMA CGM",
         "ol_rate": 3076.0, "teu_requested": 2, "container_count": 1, "mdolx_ref": "260432"},
        {"request_id": "r_ql", "status": "LOSS", "quoted": True, "destination": "Busan",
         "lane": "Houston → Busan", "carrier_quoted": "Hapag-Lloyd", "ol_rate": 2275.0,
         "loss_reason": "RATE"},
        {"request_id": "r_nq", "status": "LOSS", "quoted": False, "destination": "Manila",
         "lane": "Oakland → Manila", "loss_reason": "NO_RESPONSE"},
        {"request_id": "r_pending", "status": "PENDING", "quoted": False, "destination": "Ningbo"},
    ]


def _conn(tmp_path, monkeypatch):
    db = tmp_path / "hist.db"
    monkeypatch.setenv("HILMAR_HISTORIAN_SQLITE", str(db))
    return H._connect()


def test_only_finalized_rows_persist(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    n = H.upsert_finalized(_reqs(), "2026-06-24", conn=conn)
    assert n == 3                       # WIN + Q&L + NQ; PENDING excluded
    rows = conn.execute(
        "SELECT request_id, display_status FROM quote_history ORDER BY request_id").fetchall()
    ids = {r[0]: r[1] for r in rows}
    assert ids == {"r_win": "WIN", "r_ql": "Q&L", "r_nq": "NQ"}
    assert "r_pending" not in ids


def test_upsert_is_idempotent_and_preserves_first_seen(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    H.upsert_finalized(_reqs(), "2026-06-24", conn=conn)
    first = conn.execute(
        "SELECT first_seen_at, last_updated_at FROM quote_history WHERE request_id='r_win'"
    ).fetchone()
    # Re-run with the SAME rows: count of distinct rows must not grow.
    H.upsert_finalized(_reqs(), "2026-06-25", conn=conn)
    total = conn.execute("SELECT COUNT(*) FROM quote_history").fetchone()[0]
    assert total == 3                   # no duplicates
    after = conn.execute(
        "SELECT first_seen_at, report_date FROM quote_history WHERE request_id='r_win'"
    ).fetchone()
    assert after[0] == first[0]         # first_seen_at preserved
    assert after[1] == "2026-06-25"     # report_date updated on conflict


def test_dormant_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("HILMAR_HISTORIAN_SQLITE", raising=False)
    monkeypatch.delenv("HILMAR_HISTORIAN_URL", raising=False)
    monkeypatch.setattr(H, "SECRETS", tmp_path / "nope")   # no secrets file
    assert H.is_configured() is False
    res = H.run()
    assert res == {"configured": False, "written": 0,
                   "note": "historian dormant — no Turso/sqlite creds (see docs/HISTORIAN.md)"}
    assert H.upsert_finalized(_reqs(), "2026-06-24") == 0   # no-op, no crash


def test_run_end_to_end_against_sqlite(tmp_path, monkeypatch):
    db = tmp_path / "hist.db"
    monkeypatch.setenv("HILMAR_HISTORIAN_SQLITE", str(db))
    tracking = tmp_path / "tracking-data-v2.json"
    tracking.write_text(json.dumps({"requests": _reqs()}), encoding="utf-8")
    res = H.run(tracking, report_date="2026-06-24")
    assert res["configured"] is True
    assert res["written"] == 3
    assert res["total_rows"] == 3
    assert H.row_count() == 3
    age = H.latest_write_age_hours()
    assert age is not None and age < 1.0   # just written


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "hist.db"
    monkeypatch.setenv("HILMAR_HISTORIAN_SQLITE", str(db))
    tracking = tmp_path / "tracking-data-v2.json"
    tracking.write_text(json.dumps({"requests": _reqs()}), encoding="utf-8")
    res = H.run(tracking, dry=True, report_date="2026-06-24")
    assert res["would_write"] == 3
    assert H.row_count() == 0              # nothing actually written


# ── QC-058 historian freshness (driven through the real phase_6_rules) ─────
def _qc_run(monkeypatch, *, configured, age):
    import qc_selfheal as q
    # QC-058 does `import historian as _hist` — same module object as H here,
    # so patching H's attributes is what the check sees. log.ok only prints,
    # so callers use capsys for the OK-path assertions.
    monkeypatch.setattr(H, "is_configured", lambda: configured)
    monkeypatch.setattr(H, "latest_write_age_hours", lambda *a, **k: age)
    log = q.Log()
    q.phase_6_rules(log, {"version": "2", "requests": [],
                          "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                                      "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                                      "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                                      "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}})
    return log


def test_qc058_skips_when_dormant(monkeypatch, capsys):
    log = _qc_run(monkeypatch, configured=False, age=None)
    out = capsys.readouterr().out
    assert "QC-058" in out and "dormant" in out
    assert not any("QC-058" in m for m in log.warnings + log.errors)


def test_qc058_warns_when_stale(monkeypatch, capsys):
    log = _qc_run(monkeypatch, configured=True, age=40.0)
    assert any("QC-058" in m for m in log.warnings), log.warnings
    assert not any("QC-058" in m for m in log.errors)   # never ERROR — no client gate


def test_qc058_ok_when_fresh(monkeypatch, capsys):
    log = _qc_run(monkeypatch, configured=True, age=3.0)
    out = capsys.readouterr().out
    assert "QC-058" in out and "fresh" in out
    assert not any("QC-058" in m for m in log.warnings + log.errors)


def test_sqlite_connect_creates_missing_parent_dir(tmp_path, monkeypatch):
    # 2026-07-11 production path: sqlite synced through the blob store. On a
    # fresh runner data/ doesn't exist (gitignored) — _connect must create it
    # or the first-ever fire dies before the store is seeded.
    import historian
    db = tmp_path / "data" / "quote-history.db"   # parent does NOT exist
    monkeypatch.setenv("HILMAR_HISTORIAN_SQLITE", str(db))
    conn = historian._connect()
    assert conn is not None
    historian.ensure_schema(conn)
    conn.execute("SELECT 1")
    conn.close()
    assert db.exists()
