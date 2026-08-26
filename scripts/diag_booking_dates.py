#!/usr/bin/env python3
"""Find the REAL booked date for the back-entered MDOLX bookings.

READ-ONLY. Writes nothing, sends nothing.

Michael, 2026-08-24, on 18 corrections that carry no booking date: "then read
them again". The dates are not missing from the world — they are missing from
operator_corrections.json. Their source is named in each correction's own note:

    "Source: OL operational booking recap sent by Linda Echevarria 2026-08-12
     covering Jun 1 - Aug 12"

That email is in the body cache. This locates it, dumps every table row that
mentions one of the target MDOLX numbers, and prints every date-shaped token on
that row, so a booked date can be read off OL's own record instead of inferred
from the row's request time.

scripts/extract_hilmar_recaps.py documents that the WEEKLY recaps carry no
per-booking dates. This is a different email — an operational booking recap
covering ten weeks — so whether it carries them is an open question, and the
point of running this is to answer it rather than assume either way.

Usage:  python3 scripts/diag_booking_dates.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

MDOLX_RE = re.compile(r"MDOLX\s*0*(\d{5,8})", re.I)
BARE_REF_RE = re.compile(r"\b(2[0-9]{5})\b")
DATE_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
    r"|\d{1,2}[-\s](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-\s]?\d{0,4}"
    r"|\d{4}-\d{2}-\d{2})\b", re.I)


def _targets() -> set[str]:
    """The MDOLX refs whose booking date we do not have."""
    doc = json.loads((ROOT / "scripts" / "operator_corrections.json")
                     .read_text(encoding="utf-8"))
    out = set()
    for c in doc.get("corrections", []):
        s = c.get("set") or {}
        if s.get("status") == "WIN" and not s.get("booking_timestamp"):
            ref = str(s.get("mdolx_ref") or "").strip()
            if ref:
                out.add(ref.upper().replace("MDOLX", "").lstrip("0"))
    return out


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    try:
        import state_store
        pulled = state_store.pull(root=tmp)
    except Exception as e:
        print(f"::error::diag_booking_dates: state pull FAILED: "
              f"{type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s)")

    targets = _targets()
    print(f"MDOLX refs with NO booking date: {len(targets)}")
    print(f"  {sorted(targets)}")

    bodies = tmp / "scripts" / "stage_emails_bodies.txt"
    if not bodies.exists():
        bodies = tmp / "stage_emails_bodies.txt"
    if not bodies.exists():
        print("::error::diag_booking_dates: no cached bodies in the store")
        return 2

    recs = []
    for ln in bodies.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            recs.append(json.loads(ln))
        except Exception:
            continue
    print(f"cached bodies: {len(recs)}")

    def _text(rec):
        for k in ("text_body", "body_text", "body", "preview", "snippet"):
            v = (rec or {}).get(k)
            if isinstance(v, str) and v.strip():
                return v
        return ""

    # 1. WHICH BODIES MENTION THESE REFS AT ALL — not just the named recap.
    #    The note points at Linda's 2026-08-12 email, but a booking
    #    confirmation for the same MDOLX may also be cached and would carry a
    #    send date that IS the booked date.
    hits = {}
    for rec in recs:
        txt = _text(rec)
        if not txt:
            continue
        found = {m.group(1).lstrip("0") for m in MDOLX_RE.finditer(txt)}
        found |= {m.group(1) for m in BARE_REF_RE.finditer(txt)}
        overlap = found & targets
        for ref in overlap:
            hits.setdefault(ref, []).append(rec)

    print(f"\nrefs found in at least one cached body: {len(hits)} / {len(targets)}")
    missing = sorted(targets - set(hits))
    if missing:
        print(f"  NOT FOUND ANYWHERE: {missing}")

    for ref in sorted(hits):
        print("\n" + "=" * 74)
        print(f"MDOLX{ref}  — {len(hits[ref])} body(ies)")
        for rec in hits[ref][:3]:
            subj = (rec.get("subject") or "")[:78]
            # THE SCHEMA'S OWN KEY NAMES (2026-08-26). This read "sent" /
            # "received" / "from" / "sender" — none of which exist on a
            # stage_emails_bodies row. fetch_bodies.py:27 defines the schema
            # as sent_ts / received_ts / sender_email, so every one of the
            # 3,437 cached bodies printed `sent=?  from=?` and the run
            # concluded the send times were missing from the cache. They
            # were never missing; this was looking under the wrong names.
            # `_text` worked only because "text_body" happens to head its
            # own fallback list. The old names stay last as a fallback in
            # case an older row used them.
            sent = (rec.get("sent_ts") or rec.get("received_ts")
                    or rec.get("sent") or rec.get("received") or "?")
            frm = (rec.get("sender_email") or rec.get("from")
                   or rec.get("sender") or "?")
            print(f"  · sent={sent}  from={frm}")
            print(f"    subject: {subj!r}")
            txt = _text(rec)
            # the LINE carrying the ref, plus every date token on it
            for line in txt.splitlines():
                if ref in line.replace(" ", ""):
                    dates = DATE_RE.findall(line)
                    print(f"    ROW: {line.strip()[:190]}")
                    if dates:
                        print(f"       dates on this row: {dates}")
                    break

    print("\n" + "=" * 74)
    print("HOW TO READ THIS")
    print("=" * 74)
    print("  A ref whose only body is Linda's recap: the booked date must come")
    print("  off that row, if the recap carries one at all.")
    print("  A ref that ALSO appears in a booking confirmation: that email's")
    print("  `sent` IS the booked date — use it, no inference needed.")
    print("  A ref found NOWHERE: the evidence is not in the mailbox and the")
    print("  date has to come from Michael or OL, not from this pipeline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
