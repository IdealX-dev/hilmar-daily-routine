"""diag_blob.py — why can the state store be READ but not WRITTEN?

2026-07-28: the daily fire died before the pipeline ever ran. In the same job,
seconds apart:

    state_store: pulled 6 file(s): tracking-data-v2.json, ...     <- GET 200
    python3 scripts/state_store.py backup
    azure.core.exceptions.ResourceNotFoundError: ... ErrorCode:ResourceNotFound

`push` then failed the same way on its FIRST upload, which iteration order makes
`tracking-data-v2.json` at the container root — the very blob `pull` had just
read. So this is not a missing container and not a missing parent directory:
GET and PUT disagree about the same name.

Reasoning cannot separate the remaining candidates (SAS scope, immutability
policy, tier, account kind, endpoint). Measurement can. This prints the facts
that distinguish them.

SAFETY
  - Read-only except for probe blobs under a `_diag/` prefix, each with a
    unique name and uploaded with overwrite=False, so no probe can ever
    clobber real state.
  - Every probe it manages to create, it deletes on the way out.
  - NEVER prints the connection string, the account key, or a SAS signature.
    SAS *parameters* (permissions, expiry, resource types) are printed because
    they are the likely answer and are not themselves credentials.

Usage:  python3 scripts/diag_blob.py
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

ENV_CONN = "AZURE_STORAGE_CONNECTION_STRING"
ENV_CONTAINER = "HILMAR_STATE_CONTAINER"
DEFAULT_CONTAINER = "hilmar-state"

#: SAS query keys that are safe to print — everything else is redacted, and
#: `sig` (the signature) is never printed under any circumstance.
_SAS_SAFE = ("sp", "se", "st", "srt", "ss", "sr", "sv", "spr", "sip")


def _line(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * max(0, 60 - len(title))}")


def _describe_error(e: Exception) -> str:
    """Everything that distinguishes one Azure 404 from another."""
    parts = [type(e).__name__]
    for attr in ("status_code", "error_code", "reason"):
        val = getattr(e, attr, None)
        if val is not None:
            parts.append(f"{attr}={val}")
    resp = getattr(e, "response", None)
    if resp is not None:
        hdrs = getattr(resp, "headers", None) or {}
        for h in ("x-ms-error-code", "x-ms-request-id", "x-ms-version"):
            if h in hdrs:
                parts.append(f"{h}={hdrs[h]}")
    return "  ".join(parts)


def _parse_conn(conn: str) -> dict:
    """Split a connection string into parts WITHOUT exposing secrets."""
    out: dict[str, str] = {}
    for chunk in conn.split(";"):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    conn = os.environ.get(ENV_CONN, "")
    if not conn:
        print(f"{ENV_CONN} is not set — nothing to diagnose.")
        return 2

    parsed = _parse_conn(conn)
    container_name = os.environ.get(ENV_CONTAINER, DEFAULT_CONTAINER)

    _line("connection string shape (redacted)")
    print(f"  AccountName        : {parsed.get('AccountName', '(absent)')}")
    print(f"  EndpointSuffix     : {parsed.get('EndpointSuffix', '(absent)')}")
    print(f"  DefaultEndpoints   : {parsed.get('DefaultEndpointsProtocol', '(absent)')}")
    print(f"  BlobEndpoint       : {parsed.get('BlobEndpoint', '(absent — derived)')}")
    print(f"  has AccountKey     : {'AccountKey' in parsed}")
    print(f"  has SharedAccessSig: {'SharedAccessSignature' in parsed}")
    print(f"  container          : {container_name}")

    # If auth is a SAS, its scope is the single likeliest explanation for
    # "reads fine, writes 404" — print the parameters, never the signature.
    sas = parsed.get("SharedAccessSignature")
    if sas:
        _line("SAS parameters (signature withheld)")
        q = parse_qs(sas)
        for k in _SAS_SAFE:
            if k in q:
                print(f"  {k:4} = {q[k][0]}")
        unknown = sorted(set(q) - set(_SAS_SAFE) - {"sig"})
        if unknown:
            print(f"  (other keys present, values withheld: {', '.join(unknown)})")
        if "sp" in q:
            perms = q["sp"][0]
            print(f"\n  write permission ('w') present : {'w' in perms}")
            print(f"  create permission ('c') present: {'c' in perms}")
            print(f"  delete permission ('d') present: {'d' in perms}")
        if "se" in q:
            print(f"  expires at {q['se'][0]} — now is "
                  f"{datetime.now(timezone.utc).isoformat()}")

    try:
        from azure.storage.blob import BlobServiceClient
    except Exception as e:
        print(f"\nazure-storage-blob not importable: {e}")
        return 2

    svc = BlobServiceClient.from_connection_string(conn)
    print(f"\n  resolved blob endpoint: {svc.url}")

    _line("account information")
    try:
        info = svc.get_account_information()
        for k, v in sorted(info.items()):
            print(f"  {k} = {v}")
    except Exception as e:
        print(f"  get_account_information FAILED: {_describe_error(e)}")

    cc = svc.get_container_client(container_name)

    _line("container")
    try:
        print(f"  exists() = {cc.exists()}")
    except Exception as e:
        print(f"  exists() FAILED: {_describe_error(e)}")
    try:
        props = cc.get_container_properties()
        print(f"  last_modified       = {props.last_modified}")
        print(f"  public_access       = {props.public_access}")
        print(f"  has_immutability    = {getattr(props, 'has_immutability_policy', None)}")
        print(f"  has_legal_hold      = {getattr(props, 'has_legal_hold', None)}")
        iop = getattr(props, "immutable_storage_with_versioning_enabled", None)
        print(f"  immutable_versioning= {iop}")
    except Exception as e:
        print(f"  get_container_properties FAILED: {_describe_error(e)}")

    _line("blobs present (names only, first 25)")
    try:
        names = []
        for i, b in enumerate(cc.list_blobs()):
            if i >= 25:
                names.append("… (truncated)")
                break
            names.append(getattr(b, "name", str(b)))
        print(f"  {len(names)} listed")
        for n in names:
            print(f"    {n}")
    except Exception as e:
        print(f"  list_blobs FAILED: {_describe_error(e)}")

    _line("READ probe — the blob push died trying to overwrite")
    target = "tracking-data-v2.json"
    try:
        bc = cc.get_blob_client(target)
        print(f"  exists()  = {bc.exists()}")
        p = bc.get_blob_properties()
        print(f"  size      = {p.size}")
        print(f"  tier      = {getattr(p, 'blob_tier', None)}")
        print(f"  lease     = {getattr(getattr(p, 'lease', None), 'status', None)}"
              f" / {getattr(getattr(p, 'lease', None), 'state', None)}")
        print(f"  modified  = {p.last_modified}")
        print(f"  immutability_policy = {getattr(p, 'immutability_policy', None)}")
        print(f"  legal_hold          = {getattr(p, 'has_legal_hold', None)}")
    except Exception as e:
        print(f"  READ FAILED: {_describe_error(e)}")

    # ---- write probes -----------------------------------------------------
    # Unique names under a dedicated prefix, overwrite=False: these cannot
    # collide with or destroy real state even if something goes wrong.
    stamp = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    probes = [
        ("root",           f"_diag/probe-root-{stamp}.txt"),
        ("backups prefix", f"_diag/backups/probe-{stamp}.txt"),
        ("reports prefix", f"_diag/reports/probe-{stamp}.txt"),
    ]
    created: list[str] = []

    _line("WRITE probes (new blobs, overwrite=False, cleaned up after)")
    for label, name in probes:
        try:
            cc.get_blob_client(name).upload_blob(b"diag", overwrite=False)
            created.append(name)
            print(f"  {label:15} -> OK      ({name})")
        except Exception as e:
            print(f"  {label:15} -> FAILED  {_describe_error(e)}")

    _line("cleanup")
    for name in created:
        try:
            cc.get_blob_client(name).delete_blob()
            print(f"  deleted {name}")
        except Exception as e:
            print(f"  could NOT delete {name}: {_describe_error(e)}")
    if not created:
        print("  nothing to clean up (no probe was created)")

    # ---- overwrite=True, the exact call push() makes -----------------------
    # The probes above used overwrite=False (If-None-Match: *). push() uses
    # overwrite=True and got the same 404, but prove it rather than assume.
    _line("WRITE probe with overwrite=True (push()'s exact call shape)")
    ow_name = f"_diag/probe-overwrite-{stamp}.txt"
    try:
        cc.get_blob_client(ow_name).upload_blob(b"diag", overwrite=True)
        created.append(ow_name)
        print(f"  overwrite=True  -> OK      ({ow_name})")
        try:
            cc.get_blob_client(ow_name).delete_blob()
            created.remove(ow_name)
            print("  cleaned up")
        except Exception as e:
            print(f"  could NOT delete {ow_name}: {_describe_error(e)}")
    except Exception as e:
        print(f"  overwrite=True  -> FAILED  {_describe_error(e)}")

    # ---- is the denial CONTAINER-scoped or ACCOUNT-scoped? -----------------
    # This is the question the first probe could not answer. If a brand-new
    # container also refuses writes, nothing about `hilmar-state` is at fault
    # and the problem is the account or the credential's power over it.
    # The exact call verify_fire_prereqs.check_storage used to make, and which
    # is FATAL to the fire. No fire reached it between 2026-07-27 and 07-30 —
    # they all died at the snapshot step first — so whether it still answers
    # was never measured. Measure it.
    _line("READ probe — get_service_properties (the old fatal prereq check)")
    try:
        svc.get_service_properties()
        print("  get_service_properties -> OK")
    except Exception as e:
        print(f"  get_service_properties -> FAILED  {_describe_error(e)}")
        print("     (this would have killed the fire at step 9, three steps")
        print("      before the send, even with the snapshot step non-fatal)")

    _line("container-level operations")
    print("  create_container() on the EXISTING container:")
    print("    (state_store._container_client swallows this exception with")
    print("     contextlib.suppress at state_store.py:172 — it may have been")
    print("     saying something useful all along)")
    try:
        cc.create_container()
        print("    -> OK (it did not exist and was just created?!)")
    except Exception as e:
        print(f"    -> {_describe_error(e)}")
        print("       ContainerAlreadyExists/409 here is NORMAL and healthy.")

    probe_container = f"hilmar-diag-{stamp}".lower()[:63]
    print(f"\n  create a NEW container ({probe_container}):")
    made_container = False
    try:
        svc.create_container(probe_container)
        made_container = True
        print("    -> OK — the credential CAN create containers")
        try:
            svc.get_container_client(probe_container).get_blob_client(
                "probe.txt").upload_blob(b"diag", overwrite=False)
            print("    -> write into the new container OK")
            print("       => the denial is scoped to the hilmar-state container")
        except Exception as e:
            print(f"    -> write into the new container FAILED {_describe_error(e)}")
            print("       => the denial is ACCOUNT-WIDE, not container-specific")
    except Exception as e:
        print(f"    -> {_describe_error(e)}")
        print("       => the credential cannot create containers either")
    finally:
        if made_container:
            try:
                svc.delete_container(probe_container)
                print(f"    cleaned up container {probe_container}")
            except Exception as e:
                print(f"    could NOT delete container {probe_container}: "
                      f"{_describe_error(e)}")

    _line("containers visible to this credential")
    try:
        names = [c.name for c in svc.list_containers()]
        print(f"  {len(names)}: {', '.join(names)}")
    except Exception as e:
        print(f"  list_containers FAILED: {_describe_error(e)}")

    _line("verdict hint")
    print("  writes OK  -> the 404 was transient or specific to the real paths")
    print("  all writes 404/403 -> credential scope or account policy, not code")
    print("  root OK but prefixed 404 -> hierarchical-namespace directory issue")
    print("  new container writes OK -> the hilmar-state container is the problem")
    print("  new container writes 404 too -> account-wide write denial")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
