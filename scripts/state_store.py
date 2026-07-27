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
import json
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
    # The historian's longitudinal quote DB (2026-07-11, Michael "you handle
    # turso tokens... i cannot read this as it works"): plain sqlite synced
    # through this store instead of a Turso account — pulled before the fire,
    # appended to by historian.py, pushed after. Missing file = first run =
    # clean skip, exactly like every other entry here.
    "data/quote-history.db",
]


def _utc_stamp() -> str:
    """HHMMSS in UTC — the time half of an immutable snapshot's name.

    UTC, not ET: the DATE half is ET (so the retention prune and QC-032's age
    check keep reading an ET day), but the time only has to make names unique
    and sort correctly within that day, and UTC has no DST discontinuity to
    produce a duplicate or out-of-order stamp.
    """
    return datetime.now(ZoneInfo("UTC")).strftime("%H%M%S")


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
        # client-sent = the client-facing email's idempotency flag
        # (outlook_send --flag-name client-sent); it syncs like the staff
        # flags so cross-machine dedupe holds for that send shape too.
        # weekly-sent added 2026-07-27: weekly.yml's push step was already
        # NAMED "Push state back (weekly-sent flag)", but that flag was never
        # in this list — so the step pushed the entire state set (including
        # tracking-data-v2.json, which weekly never writes) and never the one
        # flag it meant to. Its idempotency was therefore machine-local: a
        # re-dispatch on a fresh runner saw no flag and re-sent the weekly
        # exec summary to the full distribution.
        for p in (f"reports/sent-{d}.flag", f"reports/improvements-sent-{d}.flag",
                  f"reports/client-sent-{d}.flag", f"reports/weekly-sent-{d}.flag")
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


def push(root: Path | None = None, *, container=None,
         only: list[str] | None = None) -> list[str]:
    """Upload each locally-present state file to the store, overwriting the
    prior version. Returns the list of relative paths actually pushed.

    `only` restricts the push to paths whose relative name contains one of the
    given substrings. This exists because the WEEKLY workflow used to push the
    FULL state set: it pulls state at the start of its run, and if the daily
    fire wrote new state in between, the weekly push then uploaded its own
    stale snapshot over it — a silent last-writer-wins revert of a whole day's
    ingest. The weekly job never mutates tracking-data-v2.json and has no
    business uploading it; it now pushes only its own flag.
    """
    root = root or ROOT
    cc = container or _container_client()
    pushed: list[str] = []
    for rel in state_paths():
        if only and not any(frag in rel for frag in only):
            continue
        src = root / rel
        if not src.exists():
            continue
        # GATE THE CANONICAL STATE FILE. The daily workflow pushes under
        # `if: always()`, so it runs even when the pipeline died partway
        # through — and before save_data was made atomic that meant a
        # truncated tracking-data-v2.json could be uploaded straight over the
        # good blob, destroying the only copy. Parsing it here is the last
        # check before it leaves the machine; a file that does not parse, or
        # that has no `requests` list, is not state and must not be published.
        if rel.endswith("tracking-data-v2.json"):
            _reason = _tracking_file_unusable(src)
            if _reason:
                # StateStoreError, NOT a bare RuntimeError. StateStoreError
                # is a SUBCLASS of RuntimeError, so `except StateStoreError`
                # in main() does not catch the parent — this guard's exception
                # escaped uncaught and daily.yml's push step got a Python
                # traceback instead of the one-line diagnostic the guard was
                # written to print. The refusal and the non-zero exit were
                # always correct; only the message was. Raised in review of
                # #124. Every other raise in this module already uses it.
                raise StateStoreError(
                    f"REFUSING to push {rel}: {_reason}. The local file is "
                    f"corrupt; pushing it would overwrite the last good blob. "
                    f"Pull a fresh copy or restore a snapshot."
                )
        bc = cc.get_blob_client(rel)
        bc.upload_blob(src.read_bytes(), overwrite=True)
        pushed.append(rel)
    return pushed


def _tracking_file_unusable(src: Path) -> str | None:
    """Return a human reason when the tracking file must not be published."""
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — any parse failure disqualifies it
        return f"does not parse as JSON ({e})"
    if not isinstance(doc, dict):
        return f"top level is {type(doc).__name__}, expected an object"
    if not isinstance(doc.get("requests"), list):
        return "has no `requests` list"
    return None


BACKUP_PREFIX = "backups/tracking-data-v2."
BACKUP_RETENTION_DAYS = 14


def backup(root: Path | None = None, *, container=None) -> str | None:
    """Snapshot tracking-data-v2.json to a dated, gzipped blob.

    Post-cutover replacement for the OneDrive/local backup pair that an
    ephemeral runner cannot reach (QC-032's red flag from the first
    post-flip audit, 2026-06-12). Taken right after state pull — i.e. the
    previous end-state, before this fire mutates it. Prunes snapshots older
    than BACKUP_RETENTION_DAYS (by the date embedded in the blob name, so no
    list-metadata support needed). Returns the blob name written, or None if
    there was nothing to back up.

    SNAPSHOTS ARE IMMUTABLE (2026-07-27, audit finding #21). This used to
    write `{PREFIX}{date}.json.gz` with `overwrite=True` — ONE blob per ET
    day, replaced in place. So the second run of any ET day overwrote the
    first, and the case where that matters is exactly the case backups exist
    for: the fire corrupts or empties tracking-data-v2.json, and the recovery
    dispatch — pulling the bad state, then calling backup() — uploads that bad
    file over the day's only good snapshot. The backup destroyed by the
    attempt to use it, with nothing older than midnight to fall back to.

    The name now carries the UTC time as well, and the upload is
    `overwrite=False`, so no snapshot can ever be replaced — not by a second
    fire, not by a recovery run, not by a future bug. The date still leads the
    name, so the retention prune and `latest_backup_age_days` keep parsing it
    unchanged. Two runs inside the same second is the only collision, and it
    is treated as already-done rather than an error.
    """
    import gzip
    from datetime import timedelta

    root = root or ROOT
    src = root / "tracking-data-v2.json"
    if not src.exists():
        return None
    cc = container or _container_client()
    today = _today_et()
    name = f"{BACKUP_PREFIX}{today}T{_utc_stamp()}.json.gz"
    try:
        cc.get_blob_client(name).upload_blob(
            gzip.compress(src.read_bytes()), overwrite=False)
    except Exception as e:
        # A same-second re-run is a no-op, not a failure — the identical
        # snapshot is already there. Anything else is a real upload error and
        # must not be swallowed: a backup that silently did not happen is
        # worse than one that loudly failed.
        if type(e).__name__ != "ResourceExistsError":
            raise
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


def list_backups(*, container=None) -> list[str]:
    """Every snapshot blob name, oldest first. Names sort chronologically
    because they lead with `YYYY-MM-DDTHHMMSS`."""
    cc = container or _container_client()
    try:
        return sorted(getattr(b, "name", b)
                      for b in cc.list_blobs(name_starts_with=BACKUP_PREFIX))
    except Exception:
        return []


def restore(name: str | None = None, root: Path | None = None, *,
            container=None, confirm: bool = False) -> str | None:
    """Write a snapshot back over tracking-data-v2.json.

    A backup nobody can restore is not a backup — until now the only way back
    was to hand-fetch a blob, gunzip it, and copy it into place under
    pressure, which is when mistakes get made. This is the scripted,
    reversible counterpart the working standard asks for.

    `name` selects the snapshot; None means the newest. Returns the blob name
    restored, or None when there is nothing to restore.

    THIS OVERWRITES LIVE DATA, so it is gated three ways:
      * `confirm=False` (the default, and what the CLI does without --yes)
        resolves and REPORTS the target without writing anything.
      * the current file is snapshotted first, so the restore is itself
        reversible — recovering from a wrong restore is another restore.
      * the payload must parse as JSON carrying a `requests` list before it
        can land, so a truncated or half-uploaded blob cannot replace a good
        file with a broken one (the same gate push() applies on the way out).
    """
    import gzip

    root = root or ROOT
    cc = container or _container_client()
    target = name or (list_backups(container=cc) or [None])[-1]
    if not target:
        return None
    if not confirm:
        return target

    raw = cc.get_blob_client(target).download_blob().readall()
    payload = gzip.decompress(raw)
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except Exception as e:
        raise StateStoreError(
            f"{target} does not contain valid JSON ({e}) — refusing to "
            f"restore it over live data") from e
    if not isinstance(parsed, dict) or not isinstance(parsed.get("requests"), list):
        raise StateStoreError(
            f"{target} has no `requests` list — refusing to restore it over "
            f"live data")

    dst = root / "tracking-data-v2.json"
    if dst.exists():
        # Snapshot what we are about to replace FIRST. If this restore turns
        # out to be the wrong choice, the state it destroyed is still a blob.
        backup(root=root, container=cc)

    # Atomic, for the same reason core.save_data is: a partial write here
    # would destroy the live file AND leave nothing usable in its place.
    tmp = dst.with_suffix(dst.suffix + ".restore-tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["pull", "push", "backup", "restore",
                                    "list-backups"])
    ap.add_argument("--from", dest="from_blob", default=None,
                    help="restore: snapshot blob name to restore "
                         "(default: the newest).")
    ap.add_argument("--yes", action="store_true",
                    help="restore: actually write over tracking-data-v2.json. "
                         "Without it, restore only reports what it WOULD do — "
                         "this overwrites live client booking data.")
    ap.add_argument("--only", default=None,
                    help="Comma-separated path fragments; push ONLY matching "
                         "state files. Use from jobs that must not overwrite "
                         "state they did not produce (e.g. the weekly "
                         "summary, which owns its flag and nothing else).")
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
        if args.cmd == "list-backups":
            names = list_backups()
            print("\n".join(names) if names else "state_store: no snapshots")
            return 0
        if args.cmd == "restore":
            target = restore(args.from_blob, confirm=args.yes)
            if not target:
                print("state_store: no snapshot to restore", file=sys.stderr)
                return 1
            if args.yes:
                print(f"state_store: restored {target} -> tracking-data-v2.json "
                      f"(the replaced file was snapshotted first)")
            else:
                # Dry run by default: this overwrites live client booking
                # data, so the destructive step needs an explicit --yes.
                print(f"state_store: WOULD restore {target} over "
                      f"tracking-data-v2.json. Nothing was written. "
                      f"Re-run with --yes to do it.")
            return 0
        _only = ([f.strip() for f in args.only.split(",") if f.strip()]
                 if args.only else None)
        done = pull() if args.cmd == "pull" else push(only=_only)
    except StateStoreError as e:
        print(f"state_store: {e}", file=sys.stderr)
        return 1
    verb = "pulled" if args.cmd == "pull" else "pushed"
    print(f"state_store: {verb} {len(done)} file(s): {', '.join(done) or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
