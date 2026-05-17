"""``hilmar-backfill`` — synthesize daily_snapshots/{YYYY-MM-DD}.json
files from the historical signal in tracking-data-v2.json.

The orchestrator persists ONE snapshot per real run (PR #10). On a
fresh deploy the WoW/MoM/YTD trend section honestly says
"collecting — 1/7 days" until natural history accumulates. This
script fills in synthetic snapshots for any prior date by walking
each row's ``status_history`` to determine its as-of-D status, then
running ``core.aggregate_summary`` on the filtered+rewritten rows.

Synthetic snapshots are tagged ``_synthesized: true`` so anything
inspecting the file can tell them apart from real ones. They never
overwrite an existing snapshot file unless ``--overwrite`` is passed.

Usage:
    hilmar-backfill --start 2026-04-01 --end 2026-04-27
    hilmar-backfill --month 2026-04
    hilmar-backfill --start 2026-04-01            # ends at yesterday

The orchestrator's daily run will continue to write the day's REAL
snapshot at end-of-pipeline; backfill only fills in the gaps.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from . import core, paths


def _parse_month(s: str) -> tuple[date, date]:
    """'2026-04' → (2026-04-01, 2026-04-30)."""
    y, m = s.split("-")
    yi, mi = int(y), int(m)
    start = date(yi, mi, 1)
    if mi == 12:
        end = date(yi + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(yi, mi + 1, 1) - timedelta(days=1)
    return start, end


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill daily_snapshots/{date}.json from tracking-data-v2.json's "
            "status_history. Synthetic snapshots are tagged _synthesized=true."
        ),
    )
    parser.add_argument(
        "--data", type=Path, default=None,
        help="Path to tracking-data-v2.json (default: $HILMAR_DATA_DIR/tracking-data-v2.json)",
    )
    parser.add_argument(
        "--snapshots", type=Path, default=None,
        help="Output dir for daily snapshots (default: $HILMAR_DATA_DIR/daily_snapshots)",
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="Start date inclusive (YYYY-MM-DD). Required unless --month is given.",
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="End date inclusive (YYYY-MM-DD). Defaults to yesterday.",
    )
    parser.add_argument(
        "--month", type=str, default=None,
        help="Convenience alias: --month 2026-04 → start=2026-04-01, end=2026-04-30.",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing snapshot files. Default is to skip them so "
             "today's real snapshot isn't replaced by a synthetic approximation.",
    )
    args = parser.parse_args()

    if args.month:
        start_d, end_d = _parse_month(args.month)
    else:
        if not args.start:
            parser.error("--start is required (or use --month)")
        start_d = date.fromisoformat(args.start)
        end_d = (
            date.fromisoformat(args.end) if args.end
            else core.now_utc().date() - timedelta(days=1)
        )

    data_path = args.data or (paths.data_dir() / "tracking-data-v2.json")
    snapshots_dir = args.snapshots or (paths.data_dir() / "daily_snapshots")

    tracking = json.loads(data_path.read_text(encoding="utf-8"))
    requests = tracking.get("requests") or []
    print(f"backfilling {start_d}..{end_d} ({(end_d - start_d).days + 1} days)")
    print(f"  source: {data_path} ({len(requests)} requests)")
    print(f"  target: {snapshots_dir}")
    print(f"  overwrite: {args.overwrite}")

    written = core.backfill_daily_snapshots(
        requests, snapshots_dir,
        start_date=start_d, end_date=end_d,
        overwrite=args.overwrite,
    )
    print(f"wrote {written} snapshot file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
