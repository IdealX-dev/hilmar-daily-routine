"""
backup_offline.py — Daily backup to TWO secondary locations (defense in depth).

Per Michael 2026-05-14: "onedrive backup is fine.. or offline backup somewhere".

WHY a second copy: OneDrive sync conflicts, file corruption, or accidental
delete to PROJECT HILMAR/ would erase weeks of tracker history. Two
independent backup destinations means we lose data only if BOTH the
primary AND a secondary fail simultaneously.

TARGETS (configured in config.json `backup`):
  1. secondary_onedrive_dir — separate OneDrive folder (cloud-replicated)
     Default: %USERPROFILE%/OneDrive - IdealX/HILMAR_BACKUPS/
     OneDrive corruption of one folder doesn't propagate to a sibling.
  2. local_offline_dir — Windows local folder, NOT OneDrive-synced
     Default: %USERPROFILE%/hilmar-local-backups/
     Survives all OneDrive issues, but lives on one machine only.

WHAT GETS BACKED UP per fire:
  - tracking-data-v2.json
  - reports/qc-result.json
  - reports/drift-result.json
  - reports/rate-intelligence.json
  - reports/hilmar-dashboard.html
  - reports/hilmar-report.pdf
  - reports/email-body.html
  - SHARED/client_intelligence/hilmar/_client_meta.json (shared store stamp)

All compressed into a single .tar.gz per day named `hilmar-YYYY-MM-DD.tar.gz`.

ROTATION: retention_days (default 30) — older backups pruned.

CLI:
  python scripts/backup_offline.py              # run backup
  python scripts/backup_offline.py --dry        # show what would back up
  python scripts/backup_offline.py --restore <YYYY-MM-DD> --target <dir>
                                                 # restore a specific day's
                                                 # snapshot to <dir>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _expand(p: str) -> Path:
    """Expand env vars (%USERPROFILE% on Windows, $HOME on Unix)."""
    expanded = os.path.expandvars(p)
    # Windows: also handle ${USERPROFILE} style and forward slashes
    expanded = os.path.expanduser(expanded)
    return Path(expanded)


def _files_to_backup() -> list[Path]:
    """Resolve all files to include in today's backup."""
    files = [
        ROOT / "tracking-data-v2.json",
        ROOT / "reports" / "qc-result.json",
        ROOT / "reports" / "drift-result.json",
        ROOT / "reports" / "rate-intelligence.json",
        ROOT / "reports" / "hilmar-dashboard.html",
        ROOT / "reports" / "hilmar-report.pdf",
        ROOT / "reports" / "email-body.html",
        ROOT / "reports" / "email-subject.txt",
        ROOT / "reports" / "improvements-report.html",
    ]
    # Add shared-store meta if present
    home = Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))
    for c in [home / "OneDrive - IdealX" / "SHARED" / "client_intelligence" / "hilmar",
              home / "OneDrive" / "SHARED" / "client_intelligence" / "hilmar"]:
        meta = c / "_client_meta.json"
        if meta.exists():
            files.append(meta)
            break
    return [f for f in files if f.exists()]


def _make_archive(files: list[Path], dest: Path) -> int:
    """Create a .tar.gz of `files` at `dest`. Returns size in bytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for f in files:
            # Store with relative-from-ROOT path so restore is portable
            try:
                arcname = str(f.relative_to(ROOT))
            except ValueError:
                arcname = f.name  # fall back to basename for shared store etc.
            tar.add(str(f), arcname=arcname)
    return dest.stat().st_size


def _prune(target_dir: Path, retention_days: int):
    """Remove backups older than retention_days. Returns count removed."""
    if not target_dir.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for f in target_dir.glob("hilmar-*.tar.gz"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def backup(dry: bool = False) -> dict:
    cfg = core.load_config(ROOT / "config.json")
    bkcfg = cfg.get("backup", {}) or {}
    secondary = _expand(bkcfg.get("secondary_onedrive_dir",
                                   "%USERPROFILE%/OneDrive - IdealX/HILMAR_BACKUPS"))
    offline = _expand(bkcfg.get("local_offline_dir",
                                 "%USERPROFILE%/hilmar-local-backups"))
    retention = int(bkcfg.get("retention_days", 30))

    today = datetime.now().strftime("%Y-%m-%d")
    fname = f"hilmar-{today}.tar.gz"
    files = _files_to_backup()

    if not files:
        return {"error": "no files to back up", "files_count": 0}

    result = {
        "today": today,
        "files_count": len(files),
        "secondary": {"path": str(secondary / fname), "size": 0, "ok": False},
        "offline": {"path": str(offline / fname), "size": 0, "ok": False},
        "pruned": {"secondary": 0, "offline": 0},
    }

    if dry:
        result["dry"] = True
        result["files"] = [str(f) for f in files]
        return result

    # Target 1: secondary OneDrive folder
    try:
        sz = _make_archive(files, secondary / fname)
        result["secondary"]["size"] = sz
        result["secondary"]["ok"] = True
    except Exception as e:
        result["secondary"]["error"] = str(e)

    # Target 2: local offline folder
    try:
        sz = _make_archive(files, offline / fname)
        result["offline"]["size"] = sz
        result["offline"]["ok"] = True
    except Exception as e:
        result["offline"]["error"] = str(e)

    # Prune old backups in both targets
    result["pruned"]["secondary"] = _prune(secondary, retention)
    result["pruned"]["offline"] = _prune(offline, retention)

    return result


def restore(date_str: str, target_dir: Path) -> dict:
    """Restore a snapshot. Looks in both backup targets — first hit wins."""
    cfg = core.load_config(ROOT / "config.json")
    bkcfg = cfg.get("backup", {}) or {}
    locations = [
        _expand(bkcfg.get("secondary_onedrive_dir",
                          "%USERPROFILE%/OneDrive - IdealX/HILMAR_BACKUPS")),
        _expand(bkcfg.get("local_offline_dir",
                          "%USERPROFILE%/hilmar-local-backups")),
    ]
    fname = f"hilmar-{date_str}.tar.gz"
    for loc in locations:
        src = loc / fname
        if src.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(src, "r:gz") as tar:
                tar.extractall(target_dir)
            return {"ok": True, "source": str(src), "target": str(target_dir),
                    "files_restored": len(tar.getnames()) if hasattr(tar, "getnames") else None}
    return {"error": f"No backup for {date_str} in any target",
            "searched": [str(loc) for loc in locations]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="Show what would back up")
    ap.add_argument("--restore", help="Date (YYYY-MM-DD) to restore")
    ap.add_argument("--target", help="Restore target directory")
    args = ap.parse_args()

    if args.restore:
        if not args.target:
            print("ERROR: --restore requires --target <dir>", file=sys.stderr)
            return 1
        result = restore(args.restore, Path(args.target))
    else:
        result = backup(dry=args.dry)

    print(json.dumps(result, indent=2, default=str))
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
