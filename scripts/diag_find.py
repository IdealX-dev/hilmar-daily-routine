"""diag_find.py — is THIS specific message in the mailbox we read?

2026-08-12. Michael: "but i am in the group email and you can see from her
email to me was from the shared box so it does come to me". Linda's mail to
him did come from the shared box — but it was addressed TO him. The two
examples she attached were addressed To: Lonny with the group on Cc, and
whether those reach Michael's own mailbox depends on something no amount of
reasoning settles: whether MBD_OceanExportBookingShared fans out to members'
inboxes (a distribution group) or holds mail in its own store (a shared
mailbox you open).

So stop arguing about it and look. Given a subject fragment, an MDOLX ref or
an internetMessageId, this reports whether that message is in the staged
corpus — which, since 2026-08-12, is a complete date-ordered sweep of the
mailbox — and if so, what we did with it: bucket, sender, recipients.

Three outcomes, three different owners:
  - STAGED           → it reached us; if the report still misses it, the bug
                       is downstream (classification, matching, rendering).
  - IN STAGE, DROPPED → it reached us and classify() threw it away — ours.
  - NOT PRESENT      → it never arrived in this mailbox. Delivery, not code.

READS ONLY. Pulls state and prints. No blob write, no send, no mutation.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _rule(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * max(0, 66 - len(title))}")


def _short(s, n: int) -> str:
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def needles() -> list[str]:
    raw = os.environ.get("DIAG_FIND", "")
    return [p.strip().lower() for p in raw.split("||") if p.strip()]


def matches(rec: dict, needle: str) -> bool:
    """Search the fields that identify a message, not the whole blob — a body
    that quotes the subject would otherwise look like the message itself."""
    hay = " | ".join(str(rec.get(k) or "") for k in
                     ("subject", "imid", "id", "sender_email", "sender",
                      "conversation_id", "to", "cc"))
    return needle in hay.lower()


def main() -> int:
    pats = needles()
    if not pats:
        print("::error::DIAG_FIND is empty — pass a subject fragment, MDOLX ref "
              "or Message-ID (separate several with ||)")
        return 2

    _rule("state store")
    import state_store
    try:
        pulled = state_store.pull(root=ROOT)
    except Exception as e:
        print(f"pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s)")

    import refresh_stage as RS
    rows = []
    for line in RS.STAGE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    print(f"stage_emails: {len(rows)} records")

    bodies = {}
    if RS.BODIES_PATH.exists():
        for line in RS.BODIES_PATH.read_text(encoding="utf-8",
                                             errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("imid"):
                bodies[d["imid"]] = d
    print(f"bodies: {len(bodies)} records")

    for needle in pats:
        _rule(f"searching for: {_short(needle, 58)}")
        hits = [r for r in rows if matches(r, needle)]
        if not hits:
            print("  NOT PRESENT in the staged corpus.")
            print("  Since the stage is now a complete date-ordered sweep of the "
                  "window, absence here means the message never arrived in this "
                  "mailbox — a DELIVERY question (who is on the To/Cc, and "
                  "whether the group fans out to members), not a code one.")
            continue
        print(f"  {len(hits)} record(s) STAGED:")
        for r in hits:
            b = bodies.get(r.get("imid")) or {}
            print(f"    bucket={_short(r.get('bucket'), 20):<20} "
                  f"{str(r.get('received') or r.get('sent'))[:19]}")
            print(f"      subject : {_short(r.get('subject'), 88)}")
            print(f"      from    : {_short(r.get('sender_email') or r.get('sender'), 60)}")
            for k in ("to", "cc"):
                if r.get(k):
                    print(f"      {k:<8}: {_short(r.get(k), 88)}")
            print(f"      imid    : {_short(r.get('imid'), 70)}")
            print(f"      body    : {'fetched' if b else 'NOT fetched'}"
                  + (f" ({len(b.get('text_body') or '')} chars)" if b else ""))
            rt = (b.get("body_parsed") or {}).get("rate_table") or {}
            if rt:
                print(f"      parsed  : carrier={rt.get('carrier_quoted')} "
                      f"rate={rt.get('ol_rate')} etd={rt.get('etd')}")

    # MDOLX cross-check: a booking ref should also appear on a tracking row.
    _rule("do these refs reach the tracking data?")
    data_path = ROOT / "tracking-data-v2.json"
    if data_path.exists():
        data = json.loads(data_path.read_text(encoding="utf-8"))
        for needle in pats:
            m = re.search(r"(\d{6})", needle)
            if not m:
                continue
            ref = m.group(1)
            found = [r for r in data.get("requests", [])
                     if ref in " ".join(str(x) for x in
                                        [r.get("mdolx_ref")] + (r.get("mdolx_refs_all") or []))]
            if found:
                r0 = found[0]
                print(f"  {ref}: {len(found)} row(s) — status={r0.get('status')} "
                      f"lane={_short(r0.get('lane') or r0.get('destination'), 34)}")
            else:
                print(f"  {ref}: NO tracking row carries this ref")

    print("\nNOTHING WAS WRITTEN. The stored tracking data is untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
