"""Tests for hilmar.baselines — rolling stats persistence + computation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hilmar import baselines

UTC = timezone.utc


def _request(
    *,
    request_id: str,
    request_timestamp: datetime,
    status: str,
    quoted: bool = True,
    carrier_won: str | None = None,
    carrier_quoted: str | None = None,
    destination: str = "Shanghai",
    ol_rate: int | None = 2400,
    eta_offered: str | None = "2026-05-15",
    vessel_voyage: str | None = "MSC OSCAR / 012E",
    transshipment: str | None = "Direct",
    mdolx_ref: str | None = None,
    biz_hours: float | None = 2.0,
) -> dict:
    return {
        "request_id": request_id,
        "request_timestamp": request_timestamp.isoformat(),
        "status": status,
        "quoted": quoted,
        "carrier_won": carrier_won,
        "carrier_quoted": carrier_quoted,
        "destination": destination,
        "ol_rate": ol_rate,
        "eta_offered": eta_offered,
        "vessel_voyage": vessel_voyage,
        "transshipment": transshipment,
        "mdolx_ref": mdolx_ref,
        "turnaround_biz_hours": biz_hours,
    }


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 4, 26, 12, 0, tzinfo=UTC)


# ── load / save round-trip ─────────────────────────────────────────────


def test_load_returns_empty_when_missing(tmp_path: Path):
    b = baselines.load(tmp_path / "nope.json")
    assert b.version == baselines.BASELINES_VERSION
    assert b.rolling_14d.ingest_volume_p50 is None
    assert b.rolling_90d.carrier_lane_winrate == {}


def test_load_handles_corrupt_json(tmp_path: Path):
    p = tmp_path / "baselines.json"
    p.write_text("not-json", encoding="utf-8")
    b = baselines.load(p)
    assert b.rolling_14d.parser_miss_rate == {}


def test_save_then_load_round_trip(tmp_path: Path, now: datetime):
    b = baselines.Baselines(
        updated_at=now.isoformat(),
        rolling_14d=baselines.BaselineWindow14d(
            ingest_volume_p50=12.0, ingest_volume_p90=24.0,
            biz_hours_response_p50=1.8, biz_hours_response_p90=4.2,
            win_rate_pct=28.5, win_rate_pct_stddev=3.1,
            parser_miss_rate={"eta_offered": 5.0},
        ),
        rolling_90d=baselines.BaselineWindow90d(
            carrier_lane_winrate={"MSC.Shanghai": 78.2},
        ),
    )
    p = tmp_path / "baselines.json"
    baselines.save(b, p)
    assert p.exists()

    loaded = baselines.load(p)
    assert loaded.rolling_14d.ingest_volume_p50 == 12.0
    assert loaded.rolling_14d.parser_miss_rate == {"eta_offered": 5.0}
    assert loaded.rolling_90d.carrier_lane_winrate == {"MSC.Shanghai": 78.2}


def test_save_is_atomic(tmp_path: Path):
    """save() writes to .tmp then renames — no half-written file should exist."""
    p = tmp_path / "baselines.json"
    baselines.save(baselines.Baselines(updated_at="t"), p)
    # The .tmp is gone after the rename.
    assert not (p.with_suffix(p.suffix + ".tmp")).exists()
    assert p.exists()


# ── compute ────────────────────────────────────────────────────────────


def test_compute_with_no_requests(now: datetime):
    b = baselines.compute([], now=now)
    assert b.rolling_14d.ingest_volume_p50 == 0
    assert b.rolling_14d.biz_hours_response_p50 is None
    assert b.rolling_14d.win_rate_pct is None


def test_compute_volume_percentiles(now: datetime):
    """3 requests today + 1 request yesterday → daily volumes [3, 1, 0, ...0]
    over 14 days; P50 = 0 (median is 0 since most days are quiet),
    P90 should pick up the busy day."""
    requests = [
        _request(request_id=f"r{i}", request_timestamp=now.replace(hour=8),
                 status="WIN") for i in range(3)
    ] + [_request(
        request_id="rY", request_timestamp=now - timedelta(days=1, hours=-2),
        status="Q&L",
    )]
    b = baselines.compute(requests, now=now)
    assert b.rolling_14d.ingest_volume_p50 is not None
    assert b.rolling_14d.ingest_volume_p90 is not None
    assert b.rolling_14d.ingest_volume_p90 >= b.rolling_14d.ingest_volume_p50


def test_compute_win_rate_uses_decided_only(now: datetime):
    """3 WIN, 1 Q&L, 1 NQ, 1 PENDING → win_rate = 75% (over the 4 decided).

    DECISION REVERSED 2026-08-14: "decided" was WIN + Q&L + NQ (set
    2026-04-27 with the four-state classifier); it is now WIN + Q&L.
    The headline win rate is Wins/(Wins+Q&L) and never included NQ, so the
    old set put a LEVEL and a DELTA over different populations in one
    email — and since the 2026-08-17 NQ floor the report states plainly
    that NQ rows are not counted, while this still counted them.
    PENDING stays excluded: the row is still alive.

    THIS TEST WAS GREEN THROUGH A 100% WIN-RATE BUG. Its fixtures use the
    STRICT form ("Q&L"/"NQ"); production writes the LEGACY form —
    scripts/core.decide_status returns "LOSS", enforced by QC-041. The
    predicate under test compared raw status against a STRICT tuple, so on
    real data it matched wins and nothing else and win_rate_pct was 100.0.
    The test exercised a code path production never takes. That is why the
    LEGACY companion below exists — do not delete it."""
    requests = (
        [_request(request_id=f"w{i}", request_timestamp=now.replace(hour=10),
                  status="WIN") for i in range(3)]
        + [_request(request_id="l1", request_timestamp=now.replace(hour=11),
                    status="Q&L")]
        + [_request(request_id="nq1", request_timestamp=now.replace(hour=11, minute=30),
                    status="NQ", quoted=False)]
        + [_request(request_id="p1", request_timestamp=now.replace(hour=12),
                    status="PENDING")]
    )
    b = baselines.compute(requests, now=now)
    assert b.rolling_14d.win_rate_pct == 75.0


def test_compute_win_rate_on_the_form_production_actually_writes(now: datetime):
    """The same book in the LEGACY form, which is what is really stored:
    quoted-and-lost is status="LOSS" + quoted=True, never-quoted is
    status="LOSS" + quoted=False. Must give the same 75%.

    This is the guard the STRICT-fixture test above could not provide. Run
    against the pre-2026-08-14 predicate it returns 100.0, because every
    LOSS row is invisible to a STRICT-tuple membership check."""
    requests = (
        [_request(request_id=f"w{i}", request_timestamp=now.replace(hour=10),
                  status="WIN") for i in range(3)]
        + [_request(request_id="l1", request_timestamp=now.replace(hour=11),
                    status="LOSS", quoted=True)]
        + [_request(request_id="nq1", request_timestamp=now.replace(hour=11, minute=30),
                    status="LOSS", quoted=False)]
        + [_request(request_id="p1", request_timestamp=now.replace(hour=12),
                    status="PENDING")]
    )
    b = baselines.compute(requests, now=now)
    assert b.rolling_14d.win_rate_pct == 75.0, (
        f"win_rate_pct={b.rolling_14d.win_rate_pct} — 100.0 means the "
        "predicate is comparing raw status against STRICT literals again "
        "and cannot see the losses production writes."
    )


def test_compute_biz_hours_percentiles_handles_floats(now: datetime):
    requests = [
        _request(request_id=f"r{i}", request_timestamp=now.replace(hour=10),
                 status="WIN", biz_hours=h) for i, h in enumerate([1.0, 2.0, 3.0, 4.0, 5.0])
    ]
    b = baselines.compute(requests, now=now)
    assert b.rolling_14d.biz_hours_response_p50 == 3.0
    assert b.rolling_14d.biz_hours_response_p90 is not None
    assert b.rolling_14d.biz_hours_response_p90 >= 4.0


def test_compute_parser_miss_rates_zero_when_all_populated(now: datetime):
    requests = [
        _request(request_id=f"r{i}", request_timestamp=now.replace(hour=10),
                 status="WIN", carrier_won="MSC", mdolx_ref="123")
        for i in range(3)
    ]
    b = baselines.compute(requests, now=now)
    rates = b.rolling_14d.parser_miss_rate
    # All rates should be 0 because every applicable row is fully populated.
    for parser, rate in rates.items():
        assert rate == 0.0, f"{parser} miss-rate = {rate}, expected 0.0"


def test_compute_parser_miss_rates_detects_missing_eta_offered(now: datetime):
    """Half the quoted requests are missing eta_offered → 50% miss-rate."""
    requests = []
    for i in range(4):
        requests.append(_request(
            request_id=f"r{i}", request_timestamp=now.replace(hour=10),
            status="Q&L", quoted=True,
            eta_offered=None if i < 2 else "2026-05-15",
        ))
    b = baselines.compute(requests, now=now)
    assert b.rolling_14d.parser_miss_rate.get("eta_offered") == 50.0


def test_compute_carrier_lane_winrate_requires_minimum_decisions(now: datetime):
    """Carrier × lane combos with only 1 decision are filtered out."""
    requests = [
        _request(request_id="a", request_timestamp=now.replace(hour=8),
                 status="WIN", carrier_won="MSC", destination="Shanghai"),
        # Single entry only — should NOT appear in 90d carrier_lane_winrate.
        _request(request_id="b", request_timestamp=now.replace(hour=9),
                 status="Q&L", carrier_quoted="ZIM", destination="Tokyo"),
    ]
    b = baselines.compute(requests, now=now)
    # Tokyo should not appear (only 1 ZIM/Tokyo decision).
    assert "ZIM.Tokyo" not in b.rolling_90d.carrier_lane_winrate
    # MSC/Shanghai with 1 decision also drops out.
    assert "MSC.Shanghai" not in b.rolling_90d.carrier_lane_winrate


def test_compute_carrier_lane_winrate_two_decisions_same_lane(now: datetime):
    """Two MSC/Shanghai decisions, both WIN → 100% rate."""
    requests = [
        _request(request_id="a", request_timestamp=now.replace(hour=8),
                 status="WIN", carrier_won="MSC", destination="Shanghai"),
        _request(request_id="b", request_timestamp=now.replace(hour=9),
                 status="WIN", carrier_won="MSC", destination="Shanghai"),
    ]
    b = baselines.compute(requests, now=now)
    assert b.rolling_90d.carrier_lane_winrate.get("MSC.Shanghai") == 100.0


# ── update + graft ─────────────────────────────────────────────────────


def test_update_writes_baselines_json(tmp_path: Path, now: datetime):
    requests = [
        _request(request_id="x", request_timestamp=now.replace(hour=10),
                 status="WIN")
    ]
    p = tmp_path / "baselines.json"
    out = baselines.update(
        tracking_data={"requests": requests},
        baselines_path=p,
        now=now,
    )
    assert p.exists()
    assert out.updated_at.startswith("2026-04-26")
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["version"] == baselines.BASELINES_VERSION


def test_update_idempotent_values(tmp_path: Path, now: datetime):
    """Two updates with identical input produce identical numeric values
    (timestamps differ but baselines values don't)."""
    requests = [_request(
        request_id="r1", request_timestamp=now.replace(hour=8),
        status="WIN", carrier_won="MSC",
    )]
    p = tmp_path / "baselines.json"
    out1 = baselines.update(
        tracking_data={"requests": requests}, baselines_path=p, now=now,
    )
    out2 = baselines.update(
        tracking_data={"requests": requests}, baselines_path=p, now=now,
    )
    assert out1.rolling_14d.win_rate_pct == out2.rolling_14d.win_rate_pct
    assert out1.rolling_14d.parser_miss_rate == out2.rolling_14d.parser_miss_rate


def test_graft_into_tracking_data_writes_qc_friendly_subset(now: datetime):
    b = baselines.Baselines(
        updated_at=now.isoformat(),
        rolling_14d=baselines.BaselineWindow14d(
            ingest_volume_p50=8.0, win_rate_pct=42.0,
            parser_miss_rate={"eta_offered": 10.0},
        ),
        rolling_90d=baselines.BaselineWindow90d(
            carrier_lane_winrate={"MSC.Shanghai": 60.0},
        ),
    )
    td = {"requests": []}
    out = baselines.graft_into_tracking_data(td, b)
    grafted = out["baselines"]
    assert grafted["ingest_volume_p50"] == 8.0
    assert grafted["win_rate_pct"] == 42.0
    assert grafted["parser_miss_rate"] == {"eta_offered": 10.0}
    assert grafted["carrier_lane_winrate"] == {"MSC.Shanghai": 60.0}


def test_now_utc_returns_aware_utc():
    t = baselines.now_utc()
    assert t.tzinfo == UTC
