"""
hilmar.baselines — rolling stats with memory.

Stats-with-memory, NOT machine learning. Maintains a small JSON file at
``data/baselines.json`` (gitignored runtime state, backed up via
``backup.py``) that tracks rolling distributions of:

  * ingest volume (P50 / P90 of daily request counts)
  * biz-hours response time (P50 / P90)
  * win rate (mean + stddev)
  * parser miss rates (per-parser %)
  * carrier × lane win rates (90-day window)

The values feed two consumers:
  1. :mod:`hilmar.qc` phases 8 + 9 (parser regression, ingest gap).
  2. :mod:`hilmar.insights` for "today vs baseline" delta narrative.

Update once per run, BEFORE insights generation. Idempotent — re-running
on the same tracking-data produces the same baselines (the timestamp
``updated_at`` does change but values don't).
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import core

log = logging.getLogger(__name__)

BASELINES_VERSION = 1
WINDOW_14D_KEY = "rolling_14d"
WINDOW_90D_KEY = "rolling_90d"


# ─────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────


@dataclass
class BaselineWindow14d:
    """14-day rolling stats. None values mean "insufficient data yet"."""
    ingest_volume_p50: float | None = None
    ingest_volume_p90: float | None = None
    biz_hours_response_p50: float | None = None
    biz_hours_response_p90: float | None = None
    win_rate_pct: float | None = None
    win_rate_pct_stddev: float | None = None
    parser_miss_rate: dict[str, float] = field(default_factory=dict)


@dataclass
class BaselineWindow90d:
    """90-day rolling stats. Carrier × lane win rates (sparse)."""
    carrier_lane_winrate: dict[str, float] = field(default_factory=dict)


@dataclass
class Baselines:
    """Persisted state. Round-trips through json via :func:`save` / :func:`load`."""
    version: int = BASELINES_VERSION
    updated_at: str = ""
    rolling_14d: BaselineWindow14d = field(default_factory=BaselineWindow14d)
    rolling_90d: BaselineWindow90d = field(default_factory=BaselineWindow90d)


# ─────────────────────────────────────────────────────────────────────
# IO
# ─────────────────────────────────────────────────────────────────────


def load(path: Path) -> Baselines:
    """Load baselines from ``path``. Returns an empty :class:`Baselines`
    if the file doesn't exist or is unparseable."""
    if not path.exists():
        return Baselines()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("baselines file %s did not parse — starting fresh", path)
        return Baselines()
    win14 = raw.get(WINDOW_14D_KEY) or {}
    win90 = raw.get(WINDOW_90D_KEY) or {}
    return Baselines(
        version=raw.get("version", BASELINES_VERSION),
        updated_at=raw.get("updated_at", ""),
        rolling_14d=BaselineWindow14d(
            ingest_volume_p50=win14.get("ingest_volume_p50"),
            ingest_volume_p90=win14.get("ingest_volume_p90"),
            biz_hours_response_p50=win14.get("biz_hours_response_p50"),
            biz_hours_response_p90=win14.get("biz_hours_response_p90"),
            win_rate_pct=win14.get("win_rate_pct"),
            win_rate_pct_stddev=win14.get("win_rate_pct_stddev"),
            parser_miss_rate=dict(win14.get("parser_miss_rate") or {}),
        ),
        rolling_90d=BaselineWindow90d(
            carrier_lane_winrate=dict(win90.get("carrier_lane_winrate") or {}),
        ),
    )


def save(baselines: Baselines, path: Path) -> None:
    """Persist baselines to ``path`` atomically (write + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialised = {
        "version": baselines.version,
        "updated_at": baselines.updated_at,
        WINDOW_14D_KEY: asdict(baselines.rolling_14d),
        WINDOW_90D_KEY: asdict(baselines.rolling_90d),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(serialised, indent=2), encoding="utf-8")
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────
# Computation
# ─────────────────────────────────────────────────────────────────────


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile over ``values`` (0..100)."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    rank = (pct / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return round(s[lo] + (s[hi] - s[lo]) * frac, 2)


def _safe_stddev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return round(statistics.stdev(values), 2)


def _request_dt(r: dict[str, Any]) -> datetime | None:
    return core.parse_iso(r.get("request_timestamp"))


def _in_last_n_days(r: dict[str, Any], now: datetime, n: int) -> bool:
    ts = _request_dt(r)
    return bool(ts and ts >= now - timedelta(days=n))


def _daily_volumes(requests: Iterable[dict[str, Any]], now: datetime, n: int) -> list[int]:
    """Returns one count per calendar day in the last ``n`` days. Days with
    zero requests are included as 0 so the percentile reflects "typical day"
    not "typical busy day"."""
    counts: dict[str, int] = defaultdict(int)
    for r in requests:
        ts = _request_dt(r)
        if not ts or ts < now - timedelta(days=n):
            continue
        counts[ts.date().isoformat()] += 1
    end_d = now.date()
    out: list[int] = []
    for i in range(n):
        d = (end_d - timedelta(days=i)).isoformat()
        out.append(counts.get(d, 0))
    return out


def _parser_miss_rates(requests: list[dict[str, Any]]) -> dict[str, float]:
    """Mirror :mod:`qc.phase_8_parser_regression` predicates so the baseline
    is comparable. Returns {parser: miss_rate_pct}."""
    has_rate_body = lambda r: r.get("ol_rate") is not None  # noqa: E731
    parser_specs = {
        "rate_table": (lambda r: bool(r.get("quoted")),
                       lambda r: r.get("ol_rate") is None and r.get("carrier_quoted") is None),
        "eta_offered": (has_rate_body,
                        lambda r: not r.get("eta_offered")),
        "vessel_voyage": (has_rate_body,
                          lambda r: not r.get("vessel_voyage")),
        "transshipment": (has_rate_body,
                          lambda r: not r.get("transshipment")),
        "mdolx_ref": (lambda r: r.get("status") == "WIN",
                      lambda r: not r.get("mdolx_ref")),
    }
    rates: dict[str, float] = {}
    for parser, (applicable, missed) in parser_specs.items():
        applicable_rows = [r for r in requests if applicable(r)]
        if not applicable_rows:
            continue
        missed_rows = [r for r in applicable_rows if missed(r)]
        rates[parser] = round(100.0 * len(missed_rows) / len(applicable_rows), 1)
    return rates


def _carrier_lane_winrates(requests: list[dict[str, Any]]) -> dict[str, float]:
    """{<CARRIER>.<LANE>: win_rate_pct} over the input window. ``LANE`` is
    the destination only, lower-cased + stripped, to avoid carrier-pair
    sparsity."""
    bucket_won: dict[str, int] = defaultdict(int)
    bucket_total: dict[str, int] = defaultdict(int)
    for r in requests:
        carrier = (r.get("carrier_won") or r.get("carrier_quoted") or "").strip()
        if not carrier:
            continue
        dest = (r.get("destination") or "").strip()
        if not dest or dest.lower() == "unknown":
            continue
        key = f"{carrier}.{dest}"
        # "Decided" = WIN or any LOSS variant (Q&L / NQ). PENDING is excluded
        # because the row is still alive. Pre 2026-04-27 the LOSS status
        # collapsed Q&L+NQ into one bucket; the four-state classifier
        # split them, so the membership check now lists both explicitly.
        if r.get("status") in (core.STATUS_WIN, core.STATUS_Q_AND_L, core.STATUS_NQ):
            bucket_total[key] += 1
            if r.get("status") == core.STATUS_WIN:
                bucket_won[key] += 1
    out: dict[str, float] = {}
    for key, total in bucket_total.items():
        if total >= 2:  # require at least 2 decisions before reporting
            out[key] = round(100.0 * bucket_won[key] / total, 1)
    return out


def compute(
    requests: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> Baselines:
    """Recompute baselines from the current ``requests`` slice. Pure
    function; no IO. Used by :func:`update`."""
    now = now or core.now_utc()

    recent_14 = [r for r in requests if _in_last_n_days(r, now, 14)]
    recent_90 = [r for r in requests if _in_last_n_days(r, now, 90)]

    daily = _daily_volumes(requests, now, 14)
    biz_hours = [
        float(r["turnaround_biz_hours"])
        for r in recent_14
        if isinstance(r.get("turnaround_biz_hours"), (int, float))
    ]

    # Same "decided" set as :func:`_carrier_lane_winrates` — kept symmetric so
    # the per-carrier rate and the global win-rate stddev are computed over
    # the same denominator.
    decided_statuses = (core.STATUS_WIN, core.STATUS_Q_AND_L, core.STATUS_NQ)
    decided_14 = [r for r in recent_14 if r.get("status") in decided_statuses]
    win_rate = (
        round(100.0 * sum(1 for r in decided_14 if r.get("status") == core.STATUS_WIN) / len(decided_14), 1)
        if decided_14 else None
    )
    # Stddev needs a per-day distribution, not a single point.
    daily_winrates: list[float] = []
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in decided_14:
        d = _request_dt(r)
        if d:
            by_day[d.date().isoformat()].append(r)
    for day_rows in by_day.values():
        if len(day_rows) >= 1:
            wins = sum(1 for r in day_rows if r.get("status") == core.STATUS_WIN)
            daily_winrates.append(100.0 * wins / len(day_rows))

    win_14 = BaselineWindow14d(
        ingest_volume_p50=_percentile([float(v) for v in daily], 50),
        ingest_volume_p90=_percentile([float(v) for v in daily], 90),
        biz_hours_response_p50=_percentile(biz_hours, 50),
        biz_hours_response_p90=_percentile(biz_hours, 90),
        win_rate_pct=win_rate,
        win_rate_pct_stddev=_safe_stddev(daily_winrates),
        parser_miss_rate=_parser_miss_rates(recent_14),
    )

    win_90 = BaselineWindow90d(
        carrier_lane_winrate=_carrier_lane_winrates(recent_90),
    )

    return Baselines(
        version=BASELINES_VERSION,
        updated_at=now.isoformat(),
        rolling_14d=win_14,
        rolling_90d=win_90,
    )


# ─────────────────────────────────────────────────────────────────────
# Public update entry
# ─────────────────────────────────────────────────────────────────────


def update(
    *,
    tracking_data: dict[str, Any],
    baselines_path: Path,
    now: datetime | None = None,
) -> Baselines:
    """Compute fresh baselines from ``tracking_data["requests"]`` and
    persist to ``baselines_path``. Returns the new :class:`Baselines`."""
    requests = tracking_data.get("requests") or []
    baselines = compute(requests, now=now)
    save(baselines, baselines_path)
    log.info("baselines updated → %s (volume P50=%s, win_rate=%s%%)",
             baselines_path,
             baselines.rolling_14d.ingest_volume_p50,
             baselines.rolling_14d.win_rate_pct)
    return baselines


# ─────────────────────────────────────────────────────────────────────
# Adapter — write baselines into a tracking-data dict so qc phases 8/9
# can read it without an extra IO step.
# ─────────────────────────────────────────────────────────────────────


def graft_into_tracking_data(
    tracking_data: dict[str, Any],
    baselines: Baselines,
) -> dict[str, Any]:
    """Write a flattened subset of ``baselines`` into
    ``tracking_data["baselines"]`` so QC phases 8/9 can read directly.

    Mutates and returns ``tracking_data`` (also returns it for chaining).
    """
    tracking_data["baselines"] = {
        "ingest_volume_p50": baselines.rolling_14d.ingest_volume_p50,
        "ingest_volume_p90": baselines.rolling_14d.ingest_volume_p90,
        "biz_hours_response_p50": baselines.rolling_14d.biz_hours_response_p50,
        "biz_hours_response_p90": baselines.rolling_14d.biz_hours_response_p90,
        "win_rate_pct": baselines.rolling_14d.win_rate_pct,
        "win_rate_pct_stddev": baselines.rolling_14d.win_rate_pct_stddev,
        "parser_miss_rate": dict(baselines.rolling_14d.parser_miss_rate),
        "carrier_lane_winrate": dict(baselines.rolling_90d.carrier_lane_winrate),
        "updated_at": baselines.updated_at,
    }
    return tracking_data


def now_utc() -> datetime:
    """Convenience for callers that don't want to import core or datetime."""
    return datetime.now(timezone.utc)
