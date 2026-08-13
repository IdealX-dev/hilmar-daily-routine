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
    # Linda's container report spells ports with the country attached.
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
    # OL's transaction report (2026-08-13) drops the country on most ports.
    "YOKOHAMA": "Yokohama",
    "SINGAPORE": "Singapore",
    "SHANGHAI": "Shanghai",
    "XINGANG": "Xingang",
    "DURBAN": "Durban",
    "CAI MEP": "HCMC (Cai Mep)",
    "BUSAN": "Busan",
    "SHEKOU": "Shekou",
    "KEELUNG": "Keelung",
    "LYTTELTON": "Lyttelton",
    "MELBOURNE": "Melbourne",
    "DUBLIN": "Dublin",
    "HAMBURG": "Hamburg",
    "KAOHSIUNG,TAIWAN": "Kaohsiung",
    "LAEM CHABANG": "Laem Chabang",
    "LAEM CHABANG,THAILAND": "Laem Chabang",
    "PASIR GUDANG,MALAYSIA": "Pasir Gudang",
    "PORT KLANG (PELABUHAN KLANG)": "Port Klang",
    # DELIBERATELY ABSENT: bare "MANILA". Manila North and Manila South are
    # different terminals and core.same_port treats them as different ports,
    # which is the rule that stops a booking landing on the wrong lane. An
    # unqualified "MANILA" cannot be resolved to either, so it is reported.
}


def destination_for(pod: str) -> str | None:
    """Tracker destination for an OL POD spelling, or None if unmapped.

    STRICT on purpose: this feeds MATCHING, where a wrong guess puts a real
    booking onto someone else's request. Unmapped is reported, never guessed.
    """
    if not pod:
        return None
    return POD_TO_DESTINATION.get(pod.strip().upper())


def label_for(pod: str) -> str:
    """A display name for a port on a CREATED row.

    Different job from destination_for and deliberately lenient: nothing is
    being matched here, the booking simply needs a readable lane so its TEU
    lands on the right line of the lane rollup. An unmapped port keeps OL's
    own spelling rather than becoming "Unknown", which would collapse
    several real lanes into one meaningless bucket.
    """
    mapped = destination_for(pod)
    if mapped:
        return mapped
    clean = (pod or "").split(",")[0].strip()
    return clean.title() if clean else "Unknown"


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


def _teu(b) -> int:
    """TEU as an int, or 0. The export writes it as a float string."""
    try:
        return int(float(b.get("teu") or 0))
    except (TypeError, ValueError):
        return 0


def creation(ref: str, b: dict) -> dict:
    """A create:true correction for a booking with no request behind it.

    Michael 2026-08-13: "if it's a booking it's a win and yes the 54 that
    predate the tracker should be entered so we can see complete and total
    volumes booked on lanes so better."

    46 of those bookings sailed before this pipeline read any mail, so no
    request exists to amend and none ever will. The evidence is OL's own
    transaction report, and the row says so.

    THE DATE IS A SAILING DATE, NOT A BOOKING DATE — Michael was explicit
    about that. Every date on the created row is therefore the sailing date,
    labelled as one in the note, and the row carries no turnaround figure at
    all: inventing "request to quote" hours from a sail date is exactly the
    fabricated timing the 2026-08-13 clock reset exists to stop.

    The duplicate guard lives in ingest.apply_operator_corrections: a
    created row stands down the moment any row carries the same MDOLX, so a
    confirmation arriving later takes over rather than double-counting.
    """
    sail = b.get("sheet_date") or ""
    dest = label_for(b.get("pod") or "")
    origin = (b.get("pol") or "").split(",")[0].strip().title() or "Oakland"
    teu = _teu(b)
    setter = {
        "status": "WIN",
        "mdolx_ref": ref,
        "destination": dest,
        "lane": f"{origin} → {dest}",
        "origin": origin,
        "teu_requested": teu,
        "teu_won": teu,
    }
    if b.get("carrier"):
        setter["carrier_won"] = b["carrier"]
        setter["carrier_quoted"] = b["carrier"]
    if sail:
        # booking_timestamp is what ingest's create branch stamps the WIN
        # transition with. Without it the stamp defaults to NOW, and
        # core.win_event_date would report all 54 of these as won TODAY —
        # 54 phantom wins on one morning's report. This is the single most
        # important field on the correction.
        setter["booking_timestamp"] = f"{sail}T12:00:00+00:00"
        setter["request_timestamp"] = f"{sail}T12:00:00+00:00"
        setter["request_date"] = sail
    inland = b.get("final_destination")
    return {
        "request_id": f"ol_{ref}",
        "create": True,
        "set": setter,
        "note": (
            f"MDOLX{ref} booked ({b.get('carrier') or 'carrier n/a'}, "
            f"{origin} → {dest}"
            + (f", inland {inland}" if inland else "")
            + f", {teu} TEU). Source: OL transaction report pulled 2026-08-13 "
              "covering all 2026 Hilmar sailings. Michael: \"if it's a "
              "booking it's a win and yes the 54 that predate the tracker "
              "should be entered so we can see complete and total volumes "
              "booked on lanes\". No request row exists to amend — this "
              f"booking sailed {sail or 'date n/a'}, before or outside the "
              "window in which this pipeline read mail. DATE IS A SAILING "
              "DATE, not the date booked, so no turnaround is recorded for "
              "this row."),
        "source": "ol-transaction-report-2026",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    # Default is OL's TRANSACTION report, not Linda's container report.
    # Michael 2026-08-13: "ahhh my transaction report is better" — it is the
    # full year, carries TEU and a cancelled flag, and is OL's own book
    # rather than a filtered view of it.
    ap.add_argument("--recap", default=str(ROOT / "data" /
                                           "ol-transaction-report-2026.json"))
    ap.add_argument("--since", default="2026-01-01",
                    help="Ignore Lonny requests before this date")
    ap.add_argument("--max-age", type=int, default=120,
                    help="Most days a request may predate its booking. The "
                         "transaction report dates are SAILING dates "
                         "(Michael 2026-08-13), not booking dates, so the "
                         "gap between an ask and its sailing is wider than "
                         "the container report's 60 allowed for.")
    ap.add_argument("--create-missing", action="store_true",
                    help="Also record a booking that matches NO request as a "
                         "standalone win (Michael: 'if it's a booking it's a "
                         "win ... the 54 that predate the tracker should be "
                         "entered so we can see complete and total volumes').")
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

    label = ("no request matched — recorded as standalone wins"
             if args.create_missing else "NOT matched — need a human decision")
    _rule(f"{label} ({len(unmatched)})")
    for ref, b, why in unmatched:
        print(f"  MDOLX{ref}  {b.get('pol')} → {b.get('pod')}  ({b.get('sheet_date')}): {why}")
    if unmatched and not args.create_missing:
        print("\n  Operator corrections can only amend an EXISTING request row, so a "
              "booking with no Lonny request cannot be added this way. These are "
              "reported rather than invented. Pass --create-missing to record "
              "them as standalone wins instead.")
    if unmatched and args.create_missing:
        teu = sum(_teu(b) for _, b, _ in unmatched)
        print(f"\n  --create-missing: each becomes a standalone WIN row carrying "
              f"its lane, carrier and TEU ({teu} TEU across {len(unmatched)}). "
              "Dates on these rows are SAILING dates and no turnaround is "
              "recorded for them. Each stands down automatically if a real "
              "confirmation for the same MDOLX ever arrives.")

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

    created = 0
    if args.create_missing:
        for ref, b, _why in unmatched:
            rid = f"ol_{ref}"
            if rid in existing:
                print(f"  SKIP {rid} — already has a correction; not "
                      "overwriting a human verdict")
                continue
            doc.setdefault("corrections", []).append(creation(ref, b))
            created += 1

    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWROTE {added} match correction(s) and {created} created win(s) "
          f"to {path}")
    print("Review the diff, then commit. The next fire applies them and they "
          "survive every rebuild after that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
