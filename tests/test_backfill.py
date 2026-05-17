"""Tests for hilmar.backfill — synthesize daily_snapshots/*.json from
tracking-data-v2.json's status_history.

Covers the CLI entry point (``backfill.main``) — argument parsing,
``--month`` vs ``--start/--end`` modes, ``--overwrite``, default end-date,
and the error path when neither --start nor --month is given.

The underlying core helpers (synthesize_snapshot_for_date,
backfill_daily_snapshots, status_as_of) are exercised in test_core.py;
this file targets the wiring around them.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import date
from pathlib import Path

import pytest
from conftest import GOLDEN_DAY

from hilmar import backfill, core


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    """Tmp dir with the golden fixture cloned to tracking-data-v2.json and
    a writable daily_snapshots/ output dir."""
    data = tmp_path / "tracking-data-v2.json"
    shutil.copy2(GOLDEN_DAY, data)
    snaps = tmp_path / "daily_snapshots"
    return {"tmp": tmp_path, "data": data, "snaps": snaps}


def _run_main(monkeypatch, argv: list[str]) -> int:
    """Invoke backfill.main() with the given argv (excluding program name)."""
    monkeypatch.setattr(sys, "argv", ["hilmar-backfill", *argv])
    return backfill.main()


def test_parse_month_first_half_year():
    """'2026-04' → April 1..30."""
    s, e = backfill._parse_month("2026-04")
    assert s == date(2026, 4, 1)
    assert e == date(2026, 4, 30)


def test_parse_month_december_rolls_year():
    """December must roll into next-January arithmetic, not crash with mi+1=13."""
    s, e = backfill._parse_month("2025-12")
    assert s == date(2025, 12, 1)
    assert e == date(2025, 12, 31)


def test_parse_month_february_non_leap():
    s, e = backfill._parse_month("2025-02")
    assert s == date(2025, 2, 1)
    assert e == date(2025, 2, 28)


def test_parse_month_february_leap_year():
    s, e = backfill._parse_month("2024-02")
    assert s == date(2024, 2, 1)
    assert e == date(2024, 2, 29)


def test_main_month_writes_full_month(workspace, monkeypatch, capsys):
    """--month 2026-04 writes 30 files (Apr 1..30) when none pre-exist."""
    rc = _run_main(monkeypatch, [
        "--data", str(workspace["data"]),
        "--snapshots", str(workspace["snaps"]),
        "--month", "2026-04",
    ])
    assert rc == 0
    files = sorted(workspace["snaps"].glob("*.json"))
    assert len(files) == 30
    assert files[0].name == "2026-04-01.json"
    assert files[-1].name == "2026-04-30.json"
    # Every file must carry the synthesized marker so insights can
    # distinguish backfilled from live snapshots.
    for f in files:
        snap = json.loads(f.read_text())
        assert snap["_synthesized"] is True
        assert snap["date"] == f.stem
    out = capsys.readouterr().out
    assert "wrote 30 snapshot file(s)" in out


def test_main_start_and_end_inclusive(workspace, monkeypatch):
    """--start/--end inclusive range writes exactly (end - start + 1) files."""
    rc = _run_main(monkeypatch, [
        "--data", str(workspace["data"]),
        "--snapshots", str(workspace["snaps"]),
        "--start", "2026-04-10",
        "--end", "2026-04-12",
    ])
    assert rc == 0
    names = sorted(p.name for p in workspace["snaps"].glob("*.json"))
    assert names == ["2026-04-10.json", "2026-04-11.json", "2026-04-12.json"]


def test_main_start_only_defaults_end_to_yesterday(workspace, monkeypatch):
    """When --end is omitted, the range stops at yesterday (UTC).
    Pin core.now_utc to a known date so the test isn't time-flaky."""
    import datetime as _dt
    fixed = _dt.datetime(2026, 4, 5, 12, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(core, "now_utc", lambda: fixed)
    rc = _run_main(monkeypatch, [
        "--data", str(workspace["data"]),
        "--snapshots", str(workspace["snaps"]),
        "--start", "2026-04-01",
    ])
    assert rc == 0
    # 2026-04-01 .. 2026-04-04 inclusive (yesterday relative to fixed "now").
    names = sorted(p.name for p in workspace["snaps"].glob("*.json"))
    assert names == [
        "2026-04-01.json", "2026-04-02.json",
        "2026-04-03.json", "2026-04-04.json",
    ]


def test_main_requires_start_or_month(workspace, monkeypatch, capsys):
    """argparse should bail with SystemExit(2) if neither --start nor --month is given."""
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, [
            "--data", str(workspace["data"]),
            "--snapshots", str(workspace["snaps"]),
        ])
    # parser.error() raises SystemExit(2)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--start is required" in err


def test_main_default_overwrite_skips_existing(workspace, monkeypatch):
    """Today's REAL snapshot must survive a backfill run that overlaps it.

    Pre-stage 2026-04-15 as a real snapshot (_synthesized=False) and run
    backfill across Apr 14-16. Expect: Apr 15 untouched, Apr 14 + 16 written."""
    workspace["snaps"].mkdir(parents=True, exist_ok=True)
    real_payload = {"date": "2026-04-15", "_synthesized": False, "summary": {}}
    (workspace["snaps"] / "2026-04-15.json").write_text(json.dumps(real_payload))

    rc = _run_main(monkeypatch, [
        "--data", str(workspace["data"]),
        "--snapshots", str(workspace["snaps"]),
        "--start", "2026-04-14",
        "--end", "2026-04-16",
    ])
    assert rc == 0
    # Apr 15 untouched.
    real = json.loads((workspace["snaps"] / "2026-04-15.json").read_text())
    assert real.get("_synthesized") is False
    # Apr 14 + 16 newly synthesized.
    for d in ("2026-04-14", "2026-04-16"):
        snap = json.loads((workspace["snaps"] / f"{d}.json").read_text())
        assert snap.get("_synthesized") is True


def test_main_overwrite_flag_replaces_existing(workspace, monkeypatch):
    """--overwrite must replace ALL files in range, including ones that
    look real. This is the explicit-rebuild path; the safety in the
    default mode is intentional, so we test both directions."""
    workspace["snaps"].mkdir(parents=True, exist_ok=True)
    real_payload = {"date": "2026-04-15", "_synthesized": False, "summary": {}}
    (workspace["snaps"] / "2026-04-15.json").write_text(json.dumps(real_payload))

    rc = _run_main(monkeypatch, [
        "--data", str(workspace["data"]),
        "--snapshots", str(workspace["snaps"]),
        "--start", "2026-04-15",
        "--end", "2026-04-15",
        "--overwrite",
    ])
    assert rc == 0
    rewritten = json.loads((workspace["snaps"] / "2026-04-15.json").read_text())
    # _synthesized flips to True after overwrite — the backfill is canonical now.
    assert rewritten.get("_synthesized") is True


def test_main_idempotent_on_rerun(workspace, monkeypatch, capsys):
    """Running backfill twice over the same range produces stable output:
    second run writes 0 new files (all skipped) without mutating the first."""
    args = [
        "--data", str(workspace["data"]),
        "--snapshots", str(workspace["snaps"]),
        "--start", "2026-04-01",
        "--end", "2026-04-03",
    ]
    assert _run_main(monkeypatch, args) == 0
    first_contents = {
        p.name: p.read_text()
        for p in workspace["snaps"].glob("*.json")
    }
    capsys.readouterr()  # drain
    assert _run_main(monkeypatch, args) == 0
    second_out = capsys.readouterr().out
    assert "wrote 0 snapshot file(s)" in second_out
    second_contents = {
        p.name: p.read_text()
        for p in workspace["snaps"].glob("*.json")
    }
    assert first_contents == second_contents


def test_main_creates_snapshots_dir_if_missing(workspace, monkeypatch):
    """The orchestrator might invoke backfill on a fresh deploy where
    daily_snapshots/ doesn't exist yet — must mkdir, not crash."""
    assert not workspace["snaps"].exists()
    rc = _run_main(monkeypatch, [
        "--data", str(workspace["data"]),
        "--snapshots", str(workspace["snaps"]),
        "--start", "2026-04-01",
        "--end", "2026-04-01",
    ])
    assert rc == 0
    assert workspace["snaps"].is_dir()
    assert (workspace["snaps"] / "2026-04-01.json").exists()


def test_main_filters_rows_after_as_of_date(workspace, monkeypatch):
    """Rows whose request_date is after the snapshot date must be excluded.

    Golden fixture has fixture-pending-001 dated 2026-04-14 — a snapshot
    for 2026-04-10 must NOT include it in row_state."""
    rc = _run_main(monkeypatch, [
        "--data", str(workspace["data"]),
        "--snapshots", str(workspace["snaps"]),
        "--start", "2026-04-10",
        "--end", "2026-04-10",
    ])
    assert rc == 0
    snap = json.loads((workspace["snaps"] / "2026-04-10.json").read_text())
    row_ids = set(snap["row_state"].keys())
    # Apr 14 row didn't exist on Apr 10.
    assert "fixture-pending-001" not in row_ids
    # Apr 2/5/6/8 rows did.
    assert {"fixture-win-001", "fixture-win-002", "fixture-ql-001",
            "fixture-nq-001"}.issubset(row_ids)


def test_main_zero_day_range(workspace, monkeypatch, capsys):
    """start == end → single-file run, no off-by-one."""
    rc = _run_main(monkeypatch, [
        "--data", str(workspace["data"]),
        "--snapshots", str(workspace["snaps"]),
        "--start", "2026-04-07",
        "--end", "2026-04-07",
    ])
    assert rc == 0
    files = list(workspace["snaps"].glob("*.json"))
    assert len(files) == 1
    assert "wrote 1 snapshot file(s)" in capsys.readouterr().out
