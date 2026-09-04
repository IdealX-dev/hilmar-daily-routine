"""QC-059 verifies the parser backfill without re-deriving the cache.

2026-09-04. Sentry HILMAR-DAILY-TRACKER-6 — "QC self-heal (post-patch)
TIMEOUT @ 180s" — had 31 occurrences and was marked regressed. Profiled
against a production-scale fixture (427 rows, 4,510 cached bodies, 7,146
staged rows):

    phase_6_rules ......................... 50.4s of 50.9s
      reprocess_bodies.reprocess() ........ 49.4s
        body_parser.html_to_text x4510 .... 30.2s
        fetch_bodies._parse_all x4510 ..... 18.8s

QC-059 was calling `reprocess(write=False)` purely to ASK whether the
pre-ingest backfill had run — a verification costing as much as the work it
verifies, in both qc_selfheal passes, on top of the real backfill: three full
re-parses of the mail cache per fire. On timeout run_pipeline KILLS the
subprocess (rc=124) and the entire pass is discarded while the fire exits 0
and ships from pre-patch state.

Sentry Seer attributed the growth to the tracking ROWS. Measured, the rows
alone are 2s and the mail cache is the other 48 — the cost tracks the
MAILBOX, which is why it grew. These tests pin the corrected shape.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import reprocess_bodies as RP  # noqa: E402


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Point reprocess_bodies at a temp bodies cache."""
    f = tmp_path / "stage_emails_bodies.txt"

    def _write(records):
        f.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        monkeypatch.setattr(RP, "BODIES", f)
        return f
    monkeypatch.setattr(RP, "BODIES", f)
    return _write


# ── the fingerprint ───────────────────────────────────────────────────

def test_the_fingerprint_is_stable_across_calls():
    assert RP.parser_fingerprint() == RP.parser_fingerprint()


def test_the_fingerprint_tracks_the_parser_source(tmp_path, monkeypatch):
    """It must change when the parser changes — that is what makes an
    unrefreshed cache detectable at all."""
    before = RP.parser_fingerprint()
    fake = tmp_path / "scripts"
    fake.mkdir()
    (fake / "body_parser.py").write_text("# changed\n")
    (fake / "fetch_bodies.py").write_text("# changed\n")
    monkeypatch.setattr(RP, "ROOT", tmp_path)
    assert RP.parser_fingerprint() != before


def test_an_unreadable_parser_yields_a_fingerprint_that_can_never_match(
        tmp_path, monkeypatch):
    """A broken tree must report STALE (loud), never clean (silent)."""
    monkeypatch.setattr(RP, "ROOT", tmp_path)      # no scripts/ dir at all
    assert RP.parser_fingerprint() == "unreadable"


# ── cache_staleness: the cheap check ──────────────────────────────────

def test_no_cache_is_reported_as_absent_not_as_clean(cache):
    """An ephemeral runner before the fetch has no cache. That is 'skip',
    not 'verified' — reporting it as verified would make QC-059 silent on
    exactly the host where the backfill is most likely to be missing."""
    out = RP.cache_staleness()
    assert out["present"] is False
    assert out["stale"] == 0


def test_records_stamped_with_the_current_fingerprint_are_clean(cache):
    fp = RP.parser_fingerprint()
    cache([{"imid": f"<m{i}>", "html_body": "<p>x</p>", "parser_fp": fp}
           for i in range(50)])
    out = RP.cache_staleness()
    assert out == {"present": True, "total": 50, "stale": 0, "fingerprint": fp}


def test_records_with_no_stamp_are_stale(cache):
    """The pre-stamp shape. Needs no migration — the backfill step runs
    BEFORE both QC passes, so the first fire stamps the whole cache."""
    cache([{"imid": f"<m{i}>", "html_body": "<p>x</p>"} for i in range(10)])
    assert RP.cache_staleness()["stale"] == 10


def test_records_stamped_by_a_DIFFERENT_parser_are_stale(cache):
    cache([{"imid": "<m1>", "html_body": "<p>x</p>", "parser_fp": "deadbeef0000"}])
    assert RP.cache_staleness()["stale"] == 1


def test_an_unparseable_record_counts_as_stale_never_as_ok(cache, tmp_path,
                                                           monkeypatch):
    f = tmp_path / "stage_emails_bodies.txt"
    f.write_text('{"imid": "<m1>", "parser_fp": "' + RP.parser_fingerprint()
                 + '"}\n{ this is not json\n', encoding="utf-8")
    monkeypatch.setattr(RP, "BODIES", f)
    out = RP.cache_staleness()
    assert out["total"] == 2 and out["stale"] == 1


def test_blank_lines_are_not_counted_as_records(cache, tmp_path, monkeypatch):
    f = tmp_path / "stage_emails_bodies.txt"
    f.write_text("\n\n" + json.dumps(
        {"imid": "<m1>", "parser_fp": RP.parser_fingerprint()}) + "\n\n",
        encoding="utf-8")
    monkeypatch.setattr(RP, "BODIES", f)
    assert RP.cache_staleness()["total"] == 1


# ── the check must not re-derive the cache ────────────────────────────

def test_cache_staleness_never_parses_a_body(cache, monkeypatch):
    """THE POINT OF THE CHANGE. If this ever calls the parser again, the
    180s timeout comes back."""
    import body_parser as BP
    import fetch_bodies as FB
    calls = []
    monkeypatch.setattr(BP, "html_to_text",
                        lambda *a, **k: calls.append("html_to_text"))
    monkeypatch.setattr(FB, "_parse_all",
                        lambda *a, **k: calls.append("_parse_all"))
    cache([{"imid": f"<m{i}>", "html_body": "<p>x</p>" * 500} for i in range(200)])
    RP.cache_staleness()
    assert calls == [], f"cache_staleness re-derived the cache: {calls}"


def test_qc059_asks_the_cheap_question_first():
    """Matches CODE, not prose — the comment above the check quotes the old
    call by name to explain what changed, so a substring scan of the whole
    block would fail on the explanation rather than on a regression."""
    src = (ROOT / "scripts" / "qc_selfheal.py").read_text(encoding="utf-8")
    code = [ln for ln in src.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    joined = "\n".join(code)
    assert "_rp.cache_staleness()" in joined, "QC-059 no longer uses the cheap check"
    assert "_rp.reprocess(write=False)" not in joined, (
        "QC-059 re-derives the whole cache again — that is the 180s timeout")
    # the expensive path must still EXIST, as the heal
    assert "_rp.reprocess(write=True)" in joined, (
        "the backfill heal was removed along with the expensive check")


# ── reprocess still stamps what it writes ─────────────────────────────

def test_reprocess_stamps_every_record_it_writes(cache, monkeypatch):
    import body_parser as BP
    import fetch_bodies as FB
    monkeypatch.setattr(BP, "html_to_text", lambda h: "text")
    monkeypatch.setattr(FB, "_parse_all", lambda *a, **k: {"destination": "Yokohama"})
    f = cache([{"imid": f"<m{i}>", "html_body": "<p>x</p>"} for i in range(5)])
    RP.reprocess(write=True)
    recs = [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(recs) == 5
    assert all(r["parser_fp"] == RP.parser_fingerprint() for r in recs)
    # and the cheap check now agrees
    assert RP.cache_staleness()["stale"] == 0


def test_a_dry_run_does_not_stamp(cache, monkeypatch):
    """write=False must remain read-only — QC-059's heal is the only writer."""
    import body_parser as BP
    import fetch_bodies as FB
    monkeypatch.setattr(BP, "html_to_text", lambda h: "text")
    monkeypatch.setattr(FB, "_parse_all", lambda *a, **k: {})
    f = cache([{"imid": "<m1>", "html_body": "<p>x</p>"}])
    RP.reprocess(write=False)
    assert "parser_fp" not in json.loads(f.read_text(encoding="utf-8").strip())
