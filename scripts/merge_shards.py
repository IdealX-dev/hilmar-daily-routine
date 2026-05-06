#!/usr/bin/env python3
"""
merge_shards.py - Merge per-agent shard JSONL files into the canonical
stage_emails_bodies.jsonl, deduplicating by imid (last-writer-wins).

Usage:
  python scripts/merge_shards.py scripts/_shards/*.jsonl
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_bodies as FB  # noqa: E402

MAIN = ROOT / "scripts" / "stage_emails_bodies.jsonl"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"  WARN {path.name} line {i}: {e}", file=sys.stderr)
    return rows


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: merge_shards.py <shard.jsonl> [<shard.jsonl> ...]", file=sys.stderr)
        return 2

    shards = [Path(p) for p in sys.argv[1:]]
    main_rows = read_jsonl(MAIN)
    main_n = len(main_rows)
    print(f"Main file:  {main_n} rows in {MAIN}")

    for shard in shards:
        s = read_jsonl(shard)
        print(f"Shard {shard.name}: {len(s)} rows")
        main_rows.extend(s)

    # Dedup by imid; keep last occurrence (latest fetched_at)
    seen: dict[str, int] = {}
    for i, r in enumerate(main_rows):
        imid = r.get("imid")
        if imid:
            seen[imid] = i
    deduped = [main_rows[i] for i in sorted(seen.values())]

    print(f"Total before dedup: {len(main_rows)}")
    print(f"Total after dedup:  {len(deduped)}")
    print(f"Net new since main: {len(deduped) - main_n}")

    # Write back
    with MAIN.open("w", encoding="utf-8") as f:
        for r in deduped:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    s = FB.status()
    print()
    print(f"Post-merge status: {s['total_fetched']}/{s['total_staged']}")
    for b, n in sorted(s["by_bucket"].items()):
        pct = 100.0 * n["fetched"] / n["staged"] if n["staged"] else 0.0
        print(f"  {b:22s}  {n['fetched']:3d}/{n['staged']:3d}  ({pct:5.1f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
