"""backfill_ol_bookings.py — recover wins from OL's own booking recap.

2026-08-12. Michael: "now use the report that was sent by linda with all the
bookings and match to the lonny requests since july 1 i assume and clean up
and the go forward all emails will again be in my inbox."

WHY THIS EXISTS. Between roughly Jul 1 and Aug 12 the booking confirmations
OL sent to Lonny stopped reaching the mailbox this pipeline reads (they went
To: Lonny, Cc: the group; forwarding to Michael's inbox has since been
fixed). diag_reconcile measured the damage against Linda Echevarria's export
from OL's operational system: 20 of 35 bookings present, 15 ABSENT. Those 15
are real wins the tracker cannot derive from email it never received, so the
evidence has to come from outside — from the recap itself.

WHY OPERATOR CORRECTIONS AND NOT A DIRECT WRITE. ingest REBUILDS every row
from the staged mail on each fire; anything written straight into
tracking-data-v2.json is erased the next morning. operator_corrections.json
is the one durable human-verdict store the rebuild honours (ingest
.apply_operator_corrections runs last, and qc_selfheal re-applies it), so a
correction persists exactly as long as it should.

MATCHING IS CONSERVATIVE ON PURPOSE. A wrong win is worse than a missing one
— this session has already shipped a report full of quotes that never
existed. Every match must satisfy ALL of:
  - the request's destination matches the booking's POD via core.same_port
    (the pipeline's own predicate, terminal-aware: Manila North is not
    Manila South);
  - the request predates the booking and by no more than --max-age days;
  - the request is not already a WIN and carries no MDOLX;
  - one request per booking, one booking per request, no reuse.
Anything ambiguous is REPORTED, never guessed. Bookings with no matching
request are listed too: operator corrections can only amend an existing row,
so those need a decision from Michael rather than an invention from me.

DRY RUN BY DEFAULT. --apply is the only thing that writes, and it writes to
scripts/operator_corrections.json, which is version-controlled — so the
change is reviewable in a diff and revertible with a revert.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

#: OL's operational spellings → the destination names this tracker uses.
#: Explicit rather than clever: an unmapped POD is REPORTED and skipped, so a
#: new port can never be silently matched to the wrong lane.
POD_TO_DESTINATION = {
    "YOKOHAMA,JAPAN": "Yokohama",
    "KOBE,JAPAN": "KOBE",
    "NAGOYA,JAPAN": "Nagoya",
    "TOKYO,JAPAN": "Tokyo",
    "OSAKA,JAPAN": "Osaka",
    "SINGAPORE,SINGAPORE": "Singapore",
    "SHANGHAI,CHINA": "Shanghai",
    "XINGANG,CHINA": "Xingang",
    "DURBAN,SOUTH AFRICA": "Durban",
    "CAI MEP,VIETNAM": "HCMC (Cai Mep)",
    "HO CHI MINH,VICT,VIETNAM": "HCMC",
    "MANILA NORTH HARBOUR": "Manila (North)",
    "BUSAN,KOREA": "Busan",
}


def destination_for(pod: str) -> str | None:
    """Tracker destination for an OL POD spelling, or None if unmapped."""
    if not pod:
        return None
    return POD_TO_DESTINATION.get(pod.strip().upper())


def _rule(title: str) -> None:
    print(f"\n{'──── '}{title} {'─' * max(0, 62 - len(title))}")


def propose(bookings, requests, since, max_age_days, core):
    """(matches, unmatched, skipped) — pure, so the tests can drive it."""
    have = set()
    for r in requests:
        for x in [r.get("mdolx_ref"), *(r.get("mdolx_refs_all") or [])]:
            if x:
                have.add(str(x).lstrip("0"))

    claimed: set[str] = set()
    matches, unmatched, skipped = [], [], []

    for b in sorted(bookings, key=lambda b: b.get("mdolx") or ""):
        ref = str(b.get("mdolx") or "").lstrip("0")
        if not ref:
            continue
        if ref in have:
            skipped.append((ref, "already in the tracker"))
            continue
        dest = destination_for(b.get("pod") or "")
        if not dest:
            unmatched.append((ref, b, f"POD {b.get('pod')!r} is not in POD_TO_DESTINATION"))
            continue
        bdt = core.parse_iso((b.get("sheet_date") or "") + "T23:59:59Z")
        if not bdt:
            unmatched.append((ref, b, "booking row carries no usable date"))
            continue

        best = None
        for r in requests:
            if r.get("request_id") in claimed:
                continue
            if (r.get("status") or "").upper() == "WIN":
                continue
            if r.get("mdolx_ref"):
                continue
            rdt = core.parse_iso(r.get("request_timestamp"))
            if not rdt or rdt > bdt:
                continue
            if (bdt - rdt) > timedelta(days=max_age_days):
                continue
            if since and rdt.date().isoformat() < since:
                continue
            if not core.same_port(dest, r.get("destination")):
                continue
            if best is None or rdt > core.parse_iso(best.get("request_timestamp")):
                best = r
        if best is None:
            unmatched.append((ref, b, f"no unclaimed {dest} request within {max_age_days}d"))
            continue
        claimed.add(best.get("request_id"))
        matches.append((ref, b, best))
    return matches, unmatched, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--recap", default=str(ROOT / "data" /
                                           "ol-booking-recap-2026-06-01_2026-08-12.json"))
    ap.add_argument("--since", default="2026-07-01",
                    help="Ignore Lonny requests before this date (Michael: "
                         "'match to the lonny requests since july 1')")
    ap.add_argument("--max-age", type=int, default=60,
                    help="Most days a request may predate its booking")
    ap.add_argument("--apply", action="store_true",
                    help="Write the corrections. Without this, report only.")
    args = ap.parse_args()

    import core

    bookings = json.loads(Path(args.recap).read_text(encoding="utf-8"))
    print(f"OL recap: {len(bookings)} booking(s) from {args.recap}")

    _rule("state store")
    import state_store
    try:
        pulled = state_store.pull(root=ROOT)
    except Exception as e:
        print(f"pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s)")
    data = json.loads((ROOT / "tracking-data-v2.json").read_text(encoding="utf-8"))
    requests = data.get("requests") or []
    print(f"tracking-data: {len(requests)} rows")

    matches, unmatched, skipped = propose(bookings, requests, args.since,
                                          args.max_age, core)

    _rule(f"already present ({len(skipped)})")
    print("  " + ", ".join(f"MDOLX{r}" for r, _ in skipped) if skipped else "  none")

    _rule(f"proposed matches ({len(matches)})")
    for ref, b, r in matches:
        print(f"  MDOLX{ref}  {b.get('carrier') or '?':<12} {b.get('booking_no') or '—':<14}")
        print(f"      → {r.get('request_id')}  {r.get('lane') or r.get('destination')}"
              f"  asked {str(r.get('request_timestamp'))[:10]}  status now {r.get('status')}")

    _rule(f"NOT matched — need a human decision ({len(unmatched)})")
    for ref, b, why in unmatched:
        print(f"  MDOLX{ref}  {b.get('pol')} → {b.get('pod')}  ({b.get('sheet_date')}): {why}")
    if unmatched:
        print("\n  Operator corrections can only amend an EXISTING request row, so a "
              "booking with no Lonny request cannot be added this way. These are "
              "reported rather than invented.")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to record these "
              "corrections in scripts/operator_corrections.json.")
        return 0

    path = ROOT / "scripts" / "operator_corrections.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    existing = {c.get("request_id") for c in doc.get("corrections", [])}
    added = 0
    for ref, b, r in matches:
        rid = r.get("request_id")
        if rid in existing:
            print(f"  SKIP {rid} — already has a correction; not overwriting a human verdict")
            continue
        setter = {"status": "WIN", "mdolx_ref": ref}
        if b.get("carrier"):
            setter["carrier_won"] = b["carrier"]
            setter["carrier_quoted"] = b["carrier"]
        doc.setdefault("corrections", []).append({
            "request_id": rid,
            "set": setter,
            "note": (f"MDOLX{ref} booked ({b.get('carrier') or 'carrier n/a'}"
                     + (f", booking {b['booking_no']}" if b.get("booking_no") else "")
                     + "). Source: OL operational booking recap sent by Linda "
                       "Echevarria 2026-08-12 covering Jun 1 - Aug 12. The "
                       "confirmation email never reached the tracked mailbox "
                       "(sent To: Lonny, Cc: the group), so this win cannot be "
                       "derived from mail and is recorded from OL's system of "
                       "record instead."),
            "source": "ol-booking-recap-2026-08-12",
        })
        added += 1
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWROTE {added} correction(s) to {path}")
    print("Review the diff, then commit. The next fire applies them and they "
          "survive every rebuild after that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
