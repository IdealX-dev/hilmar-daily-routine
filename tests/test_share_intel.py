"""
Unit tests for scripts/share_intel.py — cross-project intelligence export.

Verifies:
  - _row_fingerprint() is stable + deterministic for dedup
  - _transit_days() computes correctly from ETD/ETA
  - _build_carrier_summary() rolls up carriers with rate + transit
  - _build_lane_summary() rolls up lanes with all stats
  - _append_jsonl() de-dups by fingerprint (no duplicates)
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import share_intel as SI  # noqa: E402

# ─────────────────────────────────────────────────────────────────────
# _row_fingerprint
# ─────────────────────────────────────────────────────────────────────

def test_fingerprint_stable_for_same_row():
    r = {
        "request_id": "req_123",
        "status": "WIN",
        "response_timestamp": "2026-05-13T14:00:00Z",
        "carrier_quoted": "CMA CGM",
        "ol_rate": 3500,
    }
    fp1 = SI._row_fingerprint(r)
    fp2 = SI._row_fingerprint(r)
    assert fp1 == fp2
    assert len(fp1) == 16  # 16 hex chars


def test_fingerprint_differs_when_status_changes():
    base = {"request_id": "req_123", "status": "PENDING", "response_timestamp": "x"}
    fp_pending = SI._row_fingerprint(base)
    base["status"] = "WIN"
    fp_win = SI._row_fingerprint(base)
    assert fp_pending != fp_win


def test_fingerprint_differs_when_rate_changes():
    base = {"request_id": "req_123", "status": "WIN", "ol_rate": 3500}
    fp_a = SI._row_fingerprint(base)
    base["ol_rate"] = 3600
    fp_b = SI._row_fingerprint(base)
    assert fp_a != fp_b


def test_fingerprint_handles_missing_fields():
    # All fields optional
    fp = SI._row_fingerprint({})
    assert len(fp) == 16


# ─────────────────────────────────────────────────────────────────────
# _transit_days
# ─────────────────────────────────────────────────────────────────────

def test_transit_days_iso_dates():
    r = {"etd_offered": "2026-05-13", "eta_offered": "2026-05-25"}
    assert SI._transit_days(r) == 12


def test_transit_days_dd_mmm_yy_format():
    r = {"etd_offered": "13-May-26", "eta_offered": "25-May-26"}
    assert SI._transit_days(r) == 12


def test_transit_days_mixed_formats():
    # ETD in one format, ETA in another — both still parse
    r = {"etd_offered": "2026-05-13", "eta_offered": "25-May-26"}
    assert SI._transit_days(r) == 12


def test_transit_days_returns_none_when_missing():
    assert SI._transit_days({}) is None
    assert SI._transit_days({"etd_offered": "2026-05-13"}) is None  # no ETA
    assert SI._transit_days({"eta_offered": "2026-05-25"}) is None  # no ETD


def test_transit_days_returns_none_when_unparseable():
    r = {"etd_offered": "not a date", "eta_offered": "2026-05-25"}
    assert SI._transit_days(r) is None


def test_transit_days_rejects_out_of_bounds():
    # Negative transit shouldn't be returned
    r = {"etd_offered": "2026-05-25", "eta_offered": "2026-05-13"}
    assert SI._transit_days(r) is None
    # >90 days shouldn't either
    r = {"etd_offered": "2026-01-01", "eta_offered": "2026-12-01"}
    assert SI._transit_days(r) is None


# ─────────────────────────────────────────────────────────────────────
# Summary builders
# ─────────────────────────────────────────────────────────────────────

def _make_row(**kwargs):
    return {
        "status": "LOSS",
        "lane": "Oakland → Yokohama",
        "carrier_quoted": "CMA CGM",
        "request_date": "2026-05-01",
        "teu_requested": 4,
        "quoted": True,
        **kwargs,
    }


def test_carrier_summary_counts_wins_and_losses():
    rows = [
        _make_row(status="WIN", carrier_quoted="CMA CGM", teu_won=4),
        _make_row(status="WIN", carrier_quoted="CMA CGM", teu_won=8),
        _make_row(status="LOSS", carrier_quoted="CMA CGM"),
    ]
    out = SI._build_carrier_summary(rows)
    assert "CMA CGM" in out
    cma = out["CMA CGM"]
    assert cma["quotes"] == 3
    assert cma["wins"] == 2
    assert cma["losses"] == 1
    assert cma["teu_won"] == 12


def test_carrier_summary_aggregates_rates():
    rows = [
        _make_row(carrier_quoted="ONE", ol_rate=3500),
        _make_row(carrier_quoted="ONE", ol_rate=4000),
        _make_row(carrier_quoted="ONE", ol_rate=3700),
    ]
    out = SI._build_carrier_summary(rows)
    assert out["ONE"]["rate_min"] == 3500
    assert out["ONE"]["rate_max"] == 4000


def test_carrier_summary_includes_transit():
    rows = [
        _make_row(carrier_quoted="Evergreen",
                  etd_offered="2026-05-01", eta_offered="2026-05-15"),
        _make_row(carrier_quoted="Evergreen",
                  etd_offered="2026-05-05", eta_offered="2026-05-20"),
    ]
    out = SI._build_carrier_summary(rows)
    assert out["Evergreen"]["transit_count"] == 2
    assert out["Evergreen"]["transit_min_days"] in (14, 15)


def test_lane_summary_separates_won_and_lost_rates():
    rows = [
        _make_row(status="WIN", lane="Oakland → Tokyo", ol_rate=3000, carrier_quoted="CMA CGM", teu_won=4),
        _make_row(status="LOSS", lane="Oakland → Tokyo", ol_rate=3500, carrier_quoted="ONE"),
    ]
    out = SI._build_lane_summary(rows)
    lane = out["Oakland → Tokyo"]
    assert lane["rate_won_median"] == 3000
    assert lane["rate_lost_median"] == 3500
    assert lane["price_gap_median"] == 500


# ─────────────────────────────────────────────────────────────────────
# _append_jsonl dedup
# ─────────────────────────────────────────────────────────────────────

def test_append_jsonl_deduplicates_by_fingerprint():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "quotes.jsonl"
        row = {"_fp": "abc123", "data": "first"}
        SI._append_jsonl(path, [row])
        SI._append_jsonl(path, [row])  # same fp — should skip
        SI._append_jsonl(path, [{"_fp": "abc456", "data": "second"}])  # different fp
        loaded = SI._load_jsonl(path)
        assert len(loaded) == 2  # not 3 — first was deduped


def test_append_jsonl_returns_count_of_appended():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.jsonl"
        n = SI._append_jsonl(path, [{"_fp": "a"}, {"_fp": "b"}])
        assert n == 2
        n2 = SI._append_jsonl(path, [{"_fp": "a"}, {"_fp": "c"}])
        assert n2 == 1  # 'a' deduped


# ─────────────────────────────────────────────────────────────────────
# _ensure_schema_doc — QC-031 bootstrap
# ─────────────────────────────────────────────────────────────────────

def test_ensure_schema_doc_creates_missing_file():
    """First export into an empty SHARED dir must drop SCHEMA.md alongside
    _meta.json so QC-031 stops warning."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        created = SI._ensure_schema_doc(root)
        assert created is True
        assert (root / "SCHEMA.md").exists()
        # And it's the same bytes as the in-repo source — not a stub
        src = SI.HILMAR_ROOT / "docs" / "SHARED_CLIENT_INTELLIGENCE_SCHEMA.md"
        assert (root / "SCHEMA.md").read_bytes() == src.read_bytes()


def test_ensure_schema_doc_is_idempotent_when_unchanged():
    """Re-running with identical content must report no-op so we don't churn
    file mtimes on the OneDrive sync target every fire."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert SI._ensure_schema_doc(root) is True
        assert SI._ensure_schema_doc(root) is False
        assert SI._ensure_schema_doc(root) is False


def test_ensure_schema_doc_refreshes_when_source_changes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "SCHEMA.md").write_bytes(b"# stale content from older bootstrap\n")
        # Source differs; refresh must overwrite
        assert SI._ensure_schema_doc(root) is True
        src = SI.HILMAR_ROOT / "docs" / "SHARED_CLIENT_INTELLIGENCE_SCHEMA.md"
        assert (root / "SCHEMA.md").read_bytes() == src.read_bytes()


def test_ensure_schema_doc_silent_when_source_missing(monkeypatch):
    """If the in-repo source ever disappears (renamed / deleted), don't
    crash the export — just return False so QC-031 will keep warning."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake_root = Path(tmp) / "no-repo-here"
        monkeypatch.setattr(SI, "HILMAR_ROOT", fake_root)
        assert SI._ensure_schema_doc(root) is False
        assert not (root / "SCHEMA.md").exists()


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    funcs = [(n, f) for n, f in inspect.getmembers(mod, inspect.isfunction)
             if n.startswith("test_")]
    passed = failed = 0
    for name, fn in funcs:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed of {len(funcs)} share_intel tests")
    sys.exit(0 if failed == 0 else 1)
