"""diag_shared_mailbox.py — is the shared mailbox readable, endpoint by endpoint?

2026-08-14. Michael, after the shared-only fire swept zero: "you did get the
authorizaton you needed.. you are wrong." He may be right, and the evidence so
far cannot tell these two stories apart:

  A. The address is not a readable mailbox (no store behind it) — reads can
     never work, the admin consent bought nothing operationally.
  B. The mailbox is real and authorized, and the FAILURE IS OURS: the sweep
     uses the mailbox-wide `/users/{addr}/messages` endpoint, which depends on
     a hidden "AllItems" search folder that many shared mailboxes simply do
     not have. On those mailboxes Graph returns exactly the error we got —
     404 ErrorItemNotFound "Default folder AllItems not found" — while
     FOLDER-SCOPED reads (`/mailFolders/inbox/messages`) work fine.

The observed errors fit B at least as well as A: the sends that operate this
address come from real OL humans (Linda quotes Lonny FROM it), and a 404
about a missing default folder implies a store that answered.

This probes every layer separately so the answer is read, not argued:

    1. the directory object      GET /users/{addr}?$select=...
    2. the folder list           GET /users/{addr}/mailFolders?$top=50
    3. a folder-scoped read      GET .../mailFolders/inbox/messages?$top=3
    4. the sent quotes           GET .../mailFolders/sentitems/messages?$top=3
    5. the failing endpoint      GET /users/{addr}/messages?$top=1  (for the record)
    6. delta on inbox            GET .../mailFolders/inbox/messages/delta?$top=1

If 2-4 PASS while 5 FAILS, story B is proven: authorization is fine, the
mailbox is fine, and refresh_stage has to sweep per-folder for this target.

READ-ONLY. Prints status codes, Graph error codes, folder names with item
counts, and message DATES only — no subjects, no bodies, no addresses beyond
the sender's domain. Never prints a token.
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

GRAPH = "https://graph.microsoft.com/v1.0"


def _get(token: str, url: str) -> tuple[int, dict]:
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {}
    return r.status_code, body


def _err(body: dict) -> str:
    e = (body or {}).get("error") or {}
    return f"{e.get('code')}: {str(e.get('message'))[:140]}"


def main() -> int:
    import refresh_stage as RS
    import state_store

    # Pull into the REPO ROOT, not a temp dir — outlook_send._load_cache reads
    # secrets/token-cache.bin relative to the repo, exactly as auth-refresh.yml
    # does before its own read_targets check. A temp-dir pull here would leave
    # shared_token_silent() staring at an empty cache and this probe would
    # "prove" an access failure that is really a path bug.
    try:
        pulled = state_store.pull()
        print(f"pulled {len(pulled)} file(s) for the token cache")
    except Exception as e:
        print(f"state pull FAILED: {type(e).__name__}: {e}")

    addr = RS.SHARED_MAILBOX
    tok = RS.shared_token_silent()
    print(f"shared mailbox      : {addr}")
    print(f"Mail.Read.Shared tok: {'ACQUIRED' if tok else 'NOT AVAILABLE'}")
    if not tok:
        print("cannot probe without the token — run auth-refresh with "
              "include_shared=true first")
        return 2

    results: dict[str, str] = {}

    # 1 — the directory object
    code, body = _get(tok, f"{GRAPH}/users/{addr}"
                           f"?$select=id,displayName,mail,userType")
    if code == 200:
        results["object"] = "PASS"
        print(f"[1] directory object      : PASS — displayName="
              f"{body.get('displayName')!r} userType={body.get('userType')!r}")
    else:
        results["object"] = "FAIL"
        print(f"[1] directory object      : FAIL {code} — {_err(body)}")

    # 2 — the folder list. If this passes there is a STORE behind the address,
    # which alone disproves "not a mailbox at all".
    code, body = _get(tok, f"{GRAPH}/users/{addr}/mailFolders?$top=50")
    folders = body.get("value") or []
    if code == 200:
        results["folders"] = "PASS"
        print(f"[2] folder list           : PASS — {len(folders)} folder(s)")
        for f in folders:
            print(f"      {f.get('displayName')!r:30} total={f.get('totalItemCount')} "
                  f"unread={f.get('unreadItemCount')}")
    else:
        results["folders"] = "FAIL"
        print(f"[2] folder list           : FAIL {code} — {_err(body)}")

    # 3 — folder-scoped read of the inbox: the endpoint the fixed sweep would use.
    code, body = _get(tok, f"{GRAPH}/users/{addr}/mailFolders/inbox/messages"
                           f"?$top=3&$select=receivedDateTime,from")
    if code == 200:
        msgs = body.get("value") or []
        results["inbox"] = "PASS"
        print(f"[3] inbox folder read     : PASS — {len(msgs)} sampled")
        for m in msgs:
            dom = (((m.get("from") or {}).get("emailAddress") or {})
                   .get("address") or "?").split("@")[-1]
            print(f"      received={m.get('receivedDateTime')}  from=@{dom}")
    else:
        results["inbox"] = "FAIL"
        print(f"[3] inbox folder read     : FAIL {code} — {_err(body)}")

    # 4 — sent items: where the quotes OL sent to Lonny live.
    code, body = _get(tok, f"{GRAPH}/users/{addr}/mailFolders/sentitems/messages"
                           f"?$top=3&$select=sentDateTime")
    if code == 200:
        msgs = body.get("value") or []
        results["sentitems"] = "PASS"
        print(f"[4] sentitems folder read : PASS — {len(msgs)} sampled")
        for m in msgs:
            print(f"      sent={m.get('sentDateTime')}")
    else:
        results["sentitems"] = "FAIL"
        print(f"[4] sentitems folder read : FAIL {code} — {_err(body)}")

    # 5 — the endpoint the sweep uses today, for the record.
    code, body = _get(tok, f"{GRAPH}/users/{addr}/messages?$top=1")
    print(f"[5] mailbox-wide /messages: "
          f"{'PASS' if code == 200 else f'FAIL {code} — ' + _err(body)}")
    results["allitems"] = "PASS" if code == 200 else "FAIL"

    # 6 — delta on the inbox, the incremental path a folder sweep could use.
    code, body = _get(tok, f"{GRAPH}/users/{addr}/mailFolders/inbox/messages/delta?$top=1")
    print(f"[6] inbox delta           : "
          f"{'PASS' if code == 200 else f'FAIL {code} — ' + _err(body)}")

    print()
    if results.get("folders") == "PASS" and results.get("inbox") == "PASS":
        if results.get("allitems") == "FAIL":
            print("VERDICT: the mailbox is REAL and AUTHORIZED. Only the "
                  "mailbox-wide /messages endpoint fails (the AllItems search "
                  "folder does not exist in this mailbox). The sweep must read "
                  "per-folder for this target. Michael was right; the "
                  "authorization was never the problem.")
        else:
            print("VERDICT: everything passes — re-test the sweep as-is.")
        return 0
    if all(v == "FAIL" for v in results.values()):
        print("VERDICT: nothing is readable — access or the address itself. "
              "The admin consent did not reach this mailbox's store.")
        return 1
    print("VERDICT: mixed — read the individual lines above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
