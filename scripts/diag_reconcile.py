"""diag_reconcile.py — the tracker against OL's own system of record.

2026-08-12. Michael forwarded Linda Echevarria's booking recap
("06-01-26 thru 08-12-26.xlsx", 35 Hilmar bookings Jun 1 - Aug 12) with:
"ALSO A REPORT ATTACHED SHOWING ALL OUR WINS". That spreadsheet is pulled
from OL's operational system, so it is the AUTHORITATIVE win list — better
evidence than anything this pipeline derives from email, because it does not
depend on which mailbox a message happened to reach.

Every previous check this session compared the tracker to itself, or to the
mail we managed to stage. Both share the same blind spot: mail that never
arrived cannot be missed by a diagnostic that only reads what arrived. This
one compares against a list produced OUTSIDE the pipeline entirely, so a
booking we never saw shows up as absent rather than as silence.

Pass the refs in DIAG_MDOLX (comma/space separated, with or without the
MDOLX prefix). Output, per ref: WIN / present-but-not-WIN / ABSENT — plus
the reverse direction, tracker WINs in the period that OL's list does not
contain, because invented wins matter as much as missing ones.

READS ONLY. Pulls state and prints. No blob write, no send, no mutation.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _rule(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * max(0, 66 - len(title))}")


def parse_refs(raw: str) -> list[str]:
    """MDOLX refs from free text — tolerant of prefixes, commas, newlines."""
    return [m.group(1) for m in re.finditer(r"(?:MDOLX\s*)?0*(\d{6})", raw or "")]


def row_refs(r: dict) -> set[str]:
    """Every MDOLX a row claims, primary and secondary."""
    out = {str(x) for x in ([r.get("mdolx_ref")] + (r.get("mdolx_refs_all") or [])) if x}
    return {re.sub(r"^MDOLX", "", s, flags=re.I).lstrip("0") or s for s in out}


def main() -> int:
    import core as C  # noqa: F401  (kept: parity with the other diags' imports)

    refs = parse_refs(os.environ.get("DIAG_MDOLX", ""))
    if not refs:
        print("::error::DIAG_MDOLX is empty — pass the MDOLX refs from OL's recap")
        return 2
    wanted = list(dict.fromkeys(refs))          # de-dup, keep order
    print(f"OL recap refs supplied: {len(wanted)}")

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
    print(f"tracking-data: {len(requests)} rows "
          f"(last_updated {data.get('last_updated') or '?'})")

    by_ref: dict[str, list[dict]] = {}
    for r in requests:
        for ref in row_refs(r):
            by_ref.setdefault(ref, []).append(r)

    _rule("OL's recap → what the tracker has for each booking")
    verdicts: Counter = Counter()
    missing: list[str] = []
    for ref in wanted:
        rows = by_ref.get(ref) or []
        if not rows:
            verdicts["ABSENT"] += 1
            missing.append(ref)
            print(f"  MDOLX{ref}  ABSENT — no row in the tracker carries this ref")
            continue
        statuses = {(x.get("status") or "?").upper() for x in rows}
        if "WIN" in statuses:
            verdicts["WIN"] += 1
            continue
        verdicts["present-not-WIN"] += 1
        r0 = rows[0]
        print(f"  MDOLX{ref}  present but {sorted(statuses)} — "
              f"{r0.get('lane') or r0.get('destination') or '?'} "
              f"req={str(r0.get('request_timestamp'))[:10]}")

    print(f"\n  {verdicts['WIN']}/{len(wanted)} recorded as WIN, "
          f"{verdicts['present-not-WIN']} present but not a win, "
          f"{verdicts['ABSENT']} absent entirely.")
    if missing:
        print("\n  ABSENT refs (the tracker never saw these bookings at all):")
        print("    " + ", ".join(f"MDOLX{m}" for m in missing))
        print("  A booking absent from the tracker but present in OL's system is "
              "mail that never reached the mailbox this pipeline reads — not a "
              "parser or matcher fault.")

    # Reverse direction: wins we claim that OL's recap does not list. Scoped to
    # the recap's own ref range so bookings outside its period are not called
    # phantoms — the recap covers Jun 1 - Aug 12 only.
    _rule("tracker WINs inside the recap's ref range that OL does not list")
    lo, hi = min(wanted), max(wanted)
    supplied = set(wanted)
    extra = []
    for r in requests:
        if (r.get("status") or "").upper() != "WIN":
            continue
        for ref in row_refs(r):
            if ref.isdigit() and lo <= ref <= hi and ref not in supplied:
                extra.append((ref, r))
                break
    if extra:
        for ref, r in sorted(extra, key=lambda t: t[0]):
            print(f"  MDOLX{ref}  tracker says WIN — "
                  f"{r.get('lane') or r.get('destination') or '?'} "
                  f"won={C.win_event_date(r)}")
        print(f"\n  {len(extra)} win(s) the recap does not contain. Each is either "
              "a booking outside its scope or one this pipeline invented.")
    else:
        print("  none — every tracker win in that range is in OL's list.")

    print("\nNOTHING WAS WRITTEN. The stored tracking data is untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
