"""state_store.py — persist the pipeline's mutable state across runs.

On the Cloud PC the daily fire's state (tracking-data-v2.json + the staged
email cache) lives in the OneDrive folder and survives between runs. On
GitHub Actions the runner is ephemeral — every fire starts from a clean
checkout — so that state must be pulled at the start and pushed at the end.

The state files are gitignored (they hold client booking data), so they
CANNOT live in the repo. This adapter syncs them to Azure Blob Storage
instead — a private container keyed by the AZURE_STORAGE_CONNECTION_STRING
secret. This is the state-location prerequisite from
docs/MOVE-OFF-CLOUDPC.md.

Usage (in the GH Actions production-fire job):
    python scripts/state_store.py pull    # before run_pipeline.py
    python scripts/state_store.py push     # after the fire (always)

Both are no-ops with a clear message when the connection string isn't set,
so the script is safe to call on the Cloud PC too (where state is local and
this isn't needed).

Design for testability: pull()/push() accept an injected `container` client
so tests pass a fake (no Azure account, no azure-sdk import needed). The
real client is built lazily only when none is injected.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent

#: Mutable state synced between runs. Paths are relative to ROOT. These are
#: exactly the files the pipeline reads at start + rewrites during a fire
#: that must NOT reset to empty on an ephemeral runner.
STATE_FILES: list[str] = [
    "tracking-data-v2.json",
    "scripts/stage_emails.txt",
    "scripts/stage_emails_bodies.txt",
    # The delegated MSAL token cache — added 2026-06-10 when OL IT declined
    # to register an app-only Entra app, making device-code the only auth
    # that works off the Cloud PC. The runner pulls this, silently refreshes
    # (which keeps the ~90d refresh-token alive day after day), and pushes
    # the rotated cache back. The blob container is private and keyed by a
    # repo secret. Seed once from the Cloud PC: state_store.py push.
    # SECURITY (2026-06-26): canonical is the non-indexed .bin (outlook_send
    # writes .bin now; see its TOKEN_CACHE_PATH note). Both names are synced
    # during the migration so a mid-transition host keeps working; absent files
    # are skipped (push/pull both no-op on a missing path). Once every host is
    # on .bin, drop the .json entry AND delete the .json blob + ROTATE the token.
    "secrets/token-cache.bin",
    "secrets/token-cache.json",
]


def _today_et() -> str:
    """Flag files are dated in ET — the operational day of the 6 PM ET fire.
    Must match outlook_send.py's flag naming or the idempotency sync breaks."""
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def state_paths(today: str | None = None) -> list[str]:
    """STATE_FILES plus today's send-idempotency flags.

    The flags ride along so idempotency is machine-independent: without them,
    a second production-fire dispatch on the same day (or a re-run after a
    partial failure) starts from a clean runner, sees no flag, and re-sends
    to the full distribution. Only TODAY's flags sync — older ones are dead
    state and pruning them out keeps the container from accreting.
    """
    today = today or _today_et()
    days = [today]
    # Flags are keyed to the REPORT business day (outlook_send._flag_date —
    # core's wee-hours rule), which differs from the calendar day off-hours:
    # a 00:40 fire writes the PREVIOUS evening's flag. Sync both names so
    # cross-machine idempotency holds no matter when a fire lands.
    try:
        import core as _core
        _rd = _core.report_business_day(
            datetime.now(ZoneInfo("America/New_York"))).isoformat()
        if _rd not in days:
            days.append(_rd)
    except Exception as _e:
        # Loud, not silent: omitting the report-day flag from the sync is
        # exactly the cross-runner double-send hole this exists to close.
        print(f"WARN state_store: report-day flag name unavailable "
              f"({type(_e).__name__}: {_e}) — syncing calendar-day flags only")
    return STATE_FILES + [
        p for d in days
        for p in (f"reports/sent-{d}.flag", f"reports/improvements-sent-{d}.flag")
    ]

ENV_CONN = "AZURE_STORAGE_CONNECTION_STRING"
ENV_CONTAINER = "HILMAR_STATE_CONTAINER"
DEFAULT_CONTAINER = "hilmar-state"


class StateStoreError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.environ.get(ENV_CONN))


def _container_client():
    """Build the real Azure Blob container client from the connection-string
    env var. Lazy-imports the SDK so this module loads anywhere (the SDK is
    only needed when actually syncing on a runner)."""
    conn = os.environ.get(ENV_CONN)
    if not conn:
        raise StateStoreError(
            f"{ENV_CONN} not set — cannot reach the state store. "
            "Set it in the GH Actions secrets, or run on the Cloud PC where "
            "state is local."
        )
    try:
        from azure.storage.blob import BlobServiceClient
    except Exception as e:  # pragma: no cover - import guard
        raise StateStoreError(
            f"azure-storage-blob not installed: {e}. "
            "pip install azure-storage-blob"
        ) from e
    name = os.environ.get(ENV_CONTAINER, DEFAULT_CONTAINER)
    try:
        svc = BlobServiceClient.from_connection_string(conn)
    except ValueError as e:
        # The 2026-06-10 verification-fire failure: the secret held the bare
        # access KEY, not the connection string (adjacent fields in Azure
        # Portal). Say exactly what to paste instead of the SDK's terse error.
        raise StateStoreError(
            f"{ENV_CONN} is not a valid connection string ({e}). It must be "
            "the FULL 'Connection string' field from Azure Portal > Storage "
            "account > Access keys (starts with "
            "'DefaultEndpointsProtocol=https;AccountName='), not the bare Key."
        ) from e
    cc = svc.get_container_client(name)
    with contextlib.suppress(Exception):
        cc.create_container()  # already exists — fine
    return cc


def pull(root: Path | None = None, *, container=None) -> list[str]:
    """Download each state file that exists in the store into the local
    tree. Missing blobs are skipped (first-ever run). Returns the list of
    relative paths actually pulled."""
    root = root or ROOT
    cc = container or _container_client()
    pulled: list[str] = []
    for rel in state_paths():
        bc = cc.get_blob_client(rel)
        if not bc.exists():
            continue
        data = bc.download_blob().readall()
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        pulled.append(rel)
    return pulled


def push(root: Path | None = None, *, container=None) -> list[str]:
    """Upload each locally-present state file to the store, overwriting the
    prior version. Returns the list of relative paths actually pushed."""
    root = root or ROOT
    cc = container or _container_client()
    pushed: list[str] = []
    for rel in state_paths():
        src = root / rel
        if not src.exists():
            continue
        bc = cc.get_blob_client(rel)
        bc.upload_blob(src.read_bytes(), overwrite=True)
        pushed.append(rel)
    return pushed


BACKUP_PREFIX = "backups/tracking-data-v2."
BACKUP_RETENTION_DAYS = 14


def backup(root: Path | None = None, *, container=None) -> str | None:
    """Snapshot tracking-data-v2.json to a dated, gzipped blob.

    Post-cutover replacement for the OneDrive/local backup pair that an
    ephemeral runner cannot reach (QC-032's red flag from the first
    post-flip audit, 2026-06-12). One snapshot per ET day, taken right
    after state pull — i.e. yesterday's end-state, before today's fire
    mutates it. Prunes snapshots older than BACKUP_RETENTION_DAYS (by the
    date embedded in the blob name, so no list-metadata support needed).
    Returns the blob name written, or None if there was nothing to back up.
    """
    import gzip
    from datetime import timedelta

    root = root or ROOT
    src = root / "tracking-data-v2.json"
    if not src.exists():
        return None
    cc = container or _container_client()
    today = _today_et()
    name = f"{BACKUP_PREFIX}{today}.json.gz"
    cc.get_blob_client(name).upload_blob(
        gzip.compress(src.read_bytes()), overwrite=True)
    # Prune: blob names carry their date — delete anything older than the
    # retention window. list_blobs may be unsupported on exotic clients;
    # pruning is best-effort.
    with contextlib.suppress(Exception):
        # Anchor the prune cutoff to the SAME "today" that named this
        # snapshot (_today_et), not a fresh wall-clock read — otherwise the
        # naming and pruning disagree whenever the two are evaluated against
        # different clocks (e.g. a mocked _today_et in tests, or a run that
        # straddles midnight ET). Both now derive from one date.
        cutoff = (datetime.strptime(today, "%Y-%m-%d")
                  - timedelta(days=BACKUP_RETENTION_DAYS)).strftime("%Y-%m-%d")
        for b in cc.list_blobs(name_starts_with=BACKUP_PREFIX):
            bname = getattr(b, "name", b)
            day = bname[len(BACKUP_PREFIX):len(BACKUP_PREFIX) + 10]
            if day < cutoff:
                cc.delete_blob(bname)
    return name


def latest_backup_age_days(*, container=None) -> float | None:
    """Days since the newest dated backup blob, by embedded date (ET).
    None = no backups exist (or listing unsupported). Used by QC-032 on
    blob-store hosts."""
    cc = container or _container_client()
    try:
        days = sorted(
            getattr(b, "name", b)[len(BACKUP_PREFIX):len(BACKUP_PREFIX) + 10]
            for b in cc.list_blobs(name_starts_with=BACKUP_PREFIX)
        )
    except Exception:
        return None
    if not days:
        return None
    newest = datetime.strptime(days[-1], "%Y-%m-%d").replace(
        tzinfo=ZoneInfo("America/New_York"))
    now = datetime.now(ZoneInfo("America/New_York"))
    return (now - newest).total_seconds() / 86400.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["pull", "push", "backup"])
    args = ap.parse_args(argv)

    if not is_configured():
        # Safe no-op on the Cloud PC / anywhere the store isn't configured.
        print(f"state_store: {ENV_CONN} not set — {args.cmd} skipped (state is local).")
        return 0

    try:
        if args.cmd == "backup":
            written = backup()
            print(f"state_store: backup {'-> ' + written if written else 'skipped (no data file)'}")
            return 0
        done = pull() if args.cmd == "pull" else push()
    except StateStoreError as e:
        print(f"state_store: {e}", file=sys.stderr)
        return 1
    verb = "pulled" if args.cmd == "pull" else "pushed"
    print(f"state_store: {verb} {len(done)} file(s): {', '.join(done) or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
