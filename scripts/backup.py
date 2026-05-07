"""
backup.py - Rotating snapshots for tracking-data-v2.json.

Every run creates a timestamped copy in data-backups/. Keeps the N most recent
(configured via rules.backup_retention_count). Oldest beyond that are pruned.

Also supports rollback:
  python3 scripts/backup.py                   # create snapshot
  python3 scripts/backup.py --list            # list available snapshots
  python3 scripts/backup.py --rollback LATEST # restore most recent
  python3 scripts/backup.py --rollback 2026-04-19T14-22-07Z   # restore specific
  python3 scripts/backup.py --prune           # just prune old (no new snap)
"""
from __future__ import annotations
import sys, json, argparse, shutil
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402


def _load_cfg(path):
    return core.load_config(path)


def _snapshot_name():
    return "tracking-data-v2_" + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ") + ".json"


def _list_snapshots(backup_dir: Path):
    """List ALL backup naming formats — backup.py uses 'tracking-data-v2_T...Z'
    but qc_selfheal.py Phase 1 creates 'tracking-data-v2.YYYY-MM-DD-HHMM'.
    Both formats need to participate in retention pruning, otherwise the
    qc_selfheal-format files accumulate unbounded.
    Fixed 2026-05-07 per Michael 'handle all suggestions' (was: prune only
    counted underscore-format files, period-format grew indefinitely).
    """
    seen = set()
    snaps = []
    # Underscore format: tracking-data-v2_2026-05-07T14-32-52Z.json
    for p in backup_dir.glob("tracking-data-v2_*.json"):
        if p.name not in seen:
            seen.add(p.name); snaps.append(p)
    # Period format: tracking-data-v2.2026-05-07-1032.json
    for p in backup_dir.glob("tracking-data-v2.*.json"):
        if p.name not in seen:
            seen.add(p.name); snaps.append(p)
    # Sort by mtime so prune removes oldest by actual creation time
    snaps.sort(key=lambda p: p.stat().st_mtime)
    return snaps


def create_snapshot(cfg):
    data_path = Path(cfg["paths"]["data"])
    backup_dir = Path(cfg["paths"]["backups"])
    backup_dir.mkdir(parents=True, exist_ok=True)

    if not data_path.exists():
        print(f"WARN: Nothing to back up - data file missing: {data_path}")
        return None

    dst = backup_dir / _snapshot_name()
    shutil.copy2(data_path, dst)
    size = dst.stat().st_size
    print(f"OK: Snapshot {size:,} bytes -> {dst.name}")
    prune(cfg, quiet=False)
    return dst


def prune(cfg, quiet=False):
    backup_dir = Path(cfg["paths"]["backups"])
    keep = int(cfg.get("rules", {}).get("backup_retention_count", 14))
    snaps = _list_snapshots(backup_dir)
    if len(snaps) <= keep:
        if not quiet:
            print(f"  (retain {len(snaps)}/{keep} - no prune needed)")
        return 0
    to_remove = snaps[:-keep]
    removed = 0
    skipped = 0
    for f in to_remove:
        try:
            f.unlink()
            removed += 1
        except (PermissionError, OSError) as e:
            # Session-mount constraint: files from prior sessions can fail
            # to unlink even when owned. Rename with .stale- prefix so next
            # pass ignores them (glob won't match).
            try:
                f.rename(f.parent / f".stale-{f.name}")
                skipped += 1
                if not quiet:
                    print(f"  WARN prune: unlink blocked on {f.name} ({e}); renamed to .stale-")
            except Exception as e2:
                skipped += 1
                if not quiet:
                    print(f"  WARN prune: could not remove or rename {f.name} ({e2}); skipping")
    if not quiet:
        print(f"  Pruned {removed} old snapshot(s). Retained {keep}. Skipped {skipped}.")
    return removed


def list_snapshots(cfg):
    backup_dir = Path(cfg["paths"]["backups"])
    snaps = _list_snapshots(backup_dir)
    if not snaps:
        print(f"(no snapshots in {backup_dir})")
        return
    print(f"Available snapshots in {backup_dir}:")
    for s in snaps:
        size = s.stat().st_size
        mtime = datetime.fromtimestamp(s.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"  {s.name}  ({size:,} bytes, {mtime})")


def rollback(cfg, which: str):
    data_path = Path(cfg["paths"]["data"])
    backup_dir = Path(cfg["paths"]["backups"])
    snaps = _list_snapshots(backup_dir)
    if not snaps:
        print("ERROR: No snapshots available to roll back to.")
        sys.exit(1)

    if which.upper() == "LATEST":
        chosen = snaps[-1]
    else:
        matches = [s for s in snaps if which in s.name]
        if not matches:
            print(f"ERROR: No snapshot matching '{which}'. Use --list to see options.")
            sys.exit(1)
        if len(matches) > 1:
            print(f"ERROR: Ambiguous - '{which}' matches {len(matches)} snapshots:")
            for m in matches: print(f"  {m.name}")
            sys.exit(1)
        chosen = matches[0]

    if data_path.exists():
        pre_rollback = backup_dir / ("prerollback_" + _snapshot_name())
        shutil.copy2(data_path, pre_rollback)
        print(f"  Safety snapshot of current state: {pre_rollback.name}")

    shutil.copy2(chosen, data_path)
    print(f"OK: Restored {chosen.name} -> {data_path.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--list", action="store_true", help="list snapshots")
    ap.add_argument("--rollback", metavar="WHICH", help="LATEST or timestamp substring")
    ap.add_argument("--prune", action="store_true", help="prune old snapshots (no new snap)")
    args = ap.parse_args()

    cfg = _load_cfg(args.config)

    if args.list:
        list_snapshots(cfg); return
    if args.rollback:
        rollback(cfg, args.rollback); return
    if args.prune:
        prune(cfg, quiet=False); return

    create_snapshot(cfg)


if __name__ == "__main__":
    main()
