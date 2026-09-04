"""Data-flow integrity — auto-backfill of a stale parse cache + QC-059.

refresh_stage parses each email ONCE at fetch time and caches it; ingest
consumes that cache. So a body_parser fix only reaches newly-fetched mail and
the back-catalog in the window stays stale (the "break in data flow" behind the
manual re-ingest). reprocess_bodies.reprocess() re-derives the cache from the
stored raw bodies, and QC-059 verifies/heals it. These tests exercise both
against a temp cache file — no network/fetch needed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import reprocess_bodies as RP  # noqa: E402

# A real OL prose quote (the Busan/Korea body) whose CACHED parse is empty —
# i.e. it was fetched under the pre-fix parser. html_to_text -> the prose lines.
_PROSE = (
    "Please see able Hapag option from Houston port to Busan.<br>"
    "Houston to Busan _ 40' Reefer _ Chilled Cheese<br>"
    "Hapag: $2,275/40' reefer<br>"
    "Direct service"
)
_STALE_RECORD = {
    "imid": "i-korea", "bucket": "mbd_rate_response",
    "subject": "RE: Updated Cheese Rates Busan Korea from Dalhart",
    "html_body": f"<html><body>{_PROSE}</body></html>",
    "sent_ts": "2026-06-24T17:51:23+00:00",
    "parsed": {},                       # <-- stale: nothing was extracted
}


def _seed(tmp_path, monkeypatch, records):
    bodies = tmp_path / "stage_emails_bodies.txt"
    bodies.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    monkeypatch.setattr(RP, "BODIES", bodies)
    return bodies


def test_reprocess_detects_stale_cache_without_writing(tmp_path, monkeypatch):
    bodies = _seed(tmp_path, monkeypatch, [_STALE_RECORD])
    before = bodies.read_text(encoding="utf-8")
    stats = RP.reprocess(write=False)
    assert stats["present"] and stats["total"] == 1
    assert stats["changed"] == 1
    assert stats["delta_carrier"] == 1 and stats["delta_rate"] == 1
    assert stats["delta_dest"] == 1            # Busan resolved from the subject
    assert stats["wrote"] is False
    assert bodies.read_text(encoding="utf-8") == before   # untouched


def test_reprocess_backfills_atomically_and_is_idempotent(tmp_path, monkeypatch):
    bodies = _seed(tmp_path, monkeypatch, [_STALE_RECORD])
    healed = RP.reprocess(write=True)
    assert healed["wrote"] is True and healed["changed"] == 1
    rec = json.loads(bodies.read_text(encoding="utf-8").strip())
    rt = rec["parsed"]["rate_table"]
    assert rt["carrier_quoted"] == "Hapag-Lloyd"
    assert rt["ol_rate"] == 2275.0
    assert rec["parsed"]["destination"] == "Busan"
    # Second pass: cache now matches the parser -> nothing changes.
    again = RP.reprocess(write=False)
    assert again["changed"] == 0


def test_reprocess_absent_cache_is_clean_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(RP, "BODIES", tmp_path / "nope.txt")
    stats = RP.reprocess(write=False)
    assert stats["present"] is False and stats["total"] == 0


# ── QC-059 (driven through the real phase_6_rules) ─────────────────────────
def _qc(monkeypatch, drift_stats, heal_stats=None):
    """Drive QC-059 with a stubbed cache.

    THE SEAM MOVED 2026-09-04, the contract did not. QC-059 used to ASK its
    question by calling reprocess(write=False) — re-deriving all 4,510 cached
    bodies to find out whether the pre-ingest backfill had run. That check
    cost 49.4s of a 50.9s profile and is what walked the post-patch pass into
    its 180s step timeout (HILMAR-DAILY-TRACKER-6). It now compares a parser
    fingerprint instead: cache_staleness(), one string per record.

    So the DETECT stub is cache_staleness and the HEAL stub is still
    reprocess(write=True) — the expensive path survives, it just only runs
    when the cheap check says it must. Every behavioural assertion below is
    unchanged: verified -> ok and no write; drift -> warn + fix + exactly one
    write; absent -> skip; never an error, because a stale cache degrades
    fields and does not lose live data.
    """
    import qc_selfheal as q
    calls = {"write_true": 0}

    def fake_staleness():
        return drift_stats

    def fake_reprocess(*, write=True):
        if write:
            calls["write_true"] += 1
            return heal_stats or drift_stats
        raise AssertionError(
            "QC-059 called reprocess(write=False) — that is the full re-parse "
            "this check was moved off; it is the 180s timeout coming back")
    monkeypatch.setattr(RP, "cache_staleness", fake_staleness)
    monkeypatch.setattr(RP, "reprocess", fake_reprocess)
    log = q.Log()
    q.phase_6_rules(log, {"version": "2", "requests": [],
                          "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                                      "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                                      "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                                      "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}})
    return log, calls


def _clean(**kw):
    """A cache_staleness() result. `stale` replaced `changed` when the check
    stopped re-deriving the cache to compute it; the heal path still returns
    the reprocess() shape, so both key sets appear here."""
    base = {"present": True, "total": 5, "stale": 0, "changed": 0,
            "fingerprint": "abc123456789",
            "delta_carrier": 0, "delta_rate": 0, "delta_dest": 0,
            "delta_signer": 0, "delta_vessel": 0, "wrote": False}
    base.update(kw)
    return base


def test_qc059_ok_when_cache_fresh(monkeypatch, capsys):
    log, calls = _qc(monkeypatch, _clean(changed=0))
    out = capsys.readouterr().out
    assert "QC-059" in out and "integrity verified" in out
    assert not any("QC-059" in m for m in log.warnings + log.errors)
    assert calls["write_true"] == 0            # nothing to heal -> no write


def test_qc059_warns_and_self_heals_on_drift(monkeypatch):
    drift = _clean(stale=2, delta_carrier=1, delta_rate=1)
    log, calls = _qc(monkeypatch, drift,
                     heal_stats=_clean(stale=0, changed=2, total=2,
                                       delta_carrier=1, delta_rate=1, wrote=True))
    assert any("QC-059" in m for m in log.warnings), log.warnings
    assert any("QC-059" in m for m in log.fixes), log.fixes   # backfilled
    assert not any("QC-059" in m for m in log.errors)          # never gates
    assert calls["write_true"] == 1                            # healed


def test_qc059_skips_when_no_cache(monkeypatch, capsys):
    log, calls = _qc(monkeypatch, {"present": False, "total": 0, "stale": 0,
                                   "fingerprint": "abc123456789"})
    out = capsys.readouterr().out
    assert "QC-059" in out and "skipped" in out
    assert not any("QC-059" in m for m in log.warnings + log.errors)
