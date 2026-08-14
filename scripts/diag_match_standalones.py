"""diag_match_standalones.py — can the 49 standalone bookings be tied to a
real RFQ thread, on evidence?

Michael 2026-08-13: "the 49 you should be able to match up the vessel,
container quantities and lanes by the emails from lonny and the team
suggestions.. you should be able to close it out and match up so you have
extra proof".

WHAT THE 49 ARE. Rows created from OL's own 2026 transaction report
(data/ol-transaction-report-2026.json) for bookings that predate the tracker
or whose RFQ thread was never staged. They carry a booking and a lane and
nothing else — `core.has_no_rfq_chain` is true for them, they have no
source_imids at all (diag-blob 31731525694 measured exactly that: "49 no
source_imids"), and so they are excluded from every quote-side check.

WHY MATCHING THEM IS WORTH DOING. A standalone row states "this shipped". A
matched row states "Lonny asked on this date, OL quoted this rate, and it
booked on this vessel" — which is the difference between a volume number and
a defensible win. It also retires 49 rows' worth of permanent blanks.

WHY THIS IS A DIAGNOSTIC AND NOT A HEAL. Linking a booking to the wrong RFQ
manufactures a win against a request that was really lost, and this repo has
already shipped a phantom-Q&L chain built exactly that way (2026-08-11, the
resp==req same-day quotes). So this prints candidates and the evidence behind
each, and writes nothing. A link gets applied only after it is read.

SCORING — every signal is stated, never summed into a single opaque number:
    lane        origin+destination agree after canonicalisation   (required)
    containers  container_count / TEU agree                       (strong)
    vessel      vessel_voyage token appears in the RFQ row or its
                cached body                                       (strongest)
    time        booking date is AFTER the ask, within 120 days    (required)
A candidate is reported CONFIDENT only with lane + time + (vessel OR
containers). Everything else is reported as a possible and left alone.

READ-ONLY. Pulls state into a temp dir and prints. No blob write, no mutation,
no email.

PII: lanes, vessels, container counts, request ids and timestamps only. No
email bodies are printed, no addresses, no subjects.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

#: A vessel name is only evidence if it is distinctive. Two-letter carrier
#: prefixes and bare voyage numbers appear in hundreds of unrelated rows, so a
#: token has to be long enough to mean something before it is allowed to
#: confirm a match.
MIN_VESSEL_TOKEN = 5

#: A booking cannot precede its own RFQ, and an RFQ from four months earlier is
#: a different negotiation. Both bounds are required, not preferences.
MAX_ASK_TO_BOOKING_DAYS = 120


def _canon_place(s) -> str:
    """Lower, strip punctuation and any parenthetical terminal.

    'HCMC (Cat Lai)' and 'HCMC' are the same port for this purpose — the
    terminal is which berth, not which lane.
    """
    s = str(s or "").split("(")[0]
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _lane_key(r: dict) -> tuple[str, str]:
    return (_canon_place(r.get("origin")), _canon_place(r.get("destination")))


def _vessel_tokens(s) -> set[str]:
    """Distinctive words from a vessel/voyage string."""
    out = set()
    for tok in re.split(r"[^A-Za-z0-9]+", str(s or "")):
        if len(tok) >= MIN_VESSEL_TOKEN and not tok.isdigit():
            out.add(tok.upper())
    return out


def _row_text(r: dict) -> str:
    """Every stored free-text field a vessel name could be hiding in."""
    return " | ".join(str(r.get(k) or "") for k in (
        "vessel_voyage", "transshipment", "reason_detail", "pol", "pod",
        "subject", "containers", "notes"))


def main() -> int:
    import core as C

    tmp = Path(tempfile.mkdtemp())
    try:
        import state_store
        pulled = state_store.pull(root=tmp)
    except Exception as e:
        print(f"pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s): {', '.join(pulled)}")

    data_path = tmp / "tracking-data-v2.json"
    if not data_path.exists():
        print("tracking-data-v2.json not in the store — nothing to inspect")
        return 2
    rows = json.loads(data_path.read_text(encoding="utf-8")).get("requests", [])
    print(f"tracking-data-v2.json: {len(rows)} requests\n")

    # Body cache, so a vessel named only in the email still counts.
    bodies: dict = {}
    for name in ("stage_emails_bodies.txt", "stage_emails_bodies.jsonl"):
        p = tmp / name
        if not p.exists():
            p = tmp / "scripts" / name
        if p.exists():
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("imid"):
                        bodies[rec["imid"]] = rec
            break
    print(f"body cache: {len(bodies)} message(s)")

    standalones = [r for r in rows if C.has_no_rfq_chain(r)]
    chained = [r for r in rows if not C.has_no_rfq_chain(r)]
    print(f"standalone booking rows: {len(standalones)}")
    print(f"rows WITH an RFQ chain to match against: {len(chained)}\n")

    # Index the chained rows by lane so each standalone is compared against a
    # short list rather than all 380.
    by_lane: dict[tuple[str, str], list] = {}
    for r in chained:
        by_lane.setdefault(_lane_key(r), []).append(r)

    confident, possible, orphan = [], [], []

    for s in standalones:
        lane = _lane_key(s)
        book_dt = (C.parse_iso(s.get("booking_timestamp"))
                   or C.parse_iso(s.get("request_timestamp"))
                   or C.parse_iso(s.get("response_timestamp")))
        s_tokens = _vessel_tokens(s.get("vessel_voyage"))
        s_cc = s.get("container_count")
        s_teu = s.get("teu_won") or s.get("teu_requested")

        cands = []
        for c in by_lane.get(lane, []):
            ask = C.parse_iso(c.get("request_timestamp"))
            # TIME is a gate, not a score. A booking cannot precede its RFQ.
            if not (ask and book_dt):
                continue
            days = (book_dt - ask).total_seconds() / 86400.0
            if days < 0 or days > MAX_ASK_TO_BOOKING_DAYS:
                continue
            # VESSEL — the strongest signal, checked against the row's stored
            # text AND the cached bodies of its own source messages.
            hay = _row_text(c)
            for imid in (c.get("source_imids") or []):
                rec = bodies.get(imid) or {}
                hay += " | " + str(rec.get("text_body") or "")[:4000]
            hay_tokens = _vessel_tokens(hay)
            vessel_hit = sorted(s_tokens & hay_tokens)
            # CONTAINERS
            c_cc = c.get("container_count")
            c_teu = c.get("teu_requested") or c.get("teu_won")
            cc_hit = bool(s_cc and c_cc and int(s_cc) == int(c_cc))
            teu_hit = bool(s_teu and c_teu and abs(float(s_teu) - float(c_teu)) < 0.01)
            cands.append({
                "row": c, "days": days, "vessel_hit": vessel_hit,
                "cc_hit": cc_hit, "teu_hit": teu_hit,
                "strength": (2 if vessel_hit else 0) + (1 if cc_hit else 0)
                            + (1 if teu_hit else 0),
            })

        cands.sort(key=lambda d: (-d["strength"], d["days"]))
        if not cands:
            orphan.append((s, "no same-lane RFQ inside the window"))
        elif cands[0]["strength"] >= 2:
            confident.append((s, cands))
        else:
            possible.append((s, cands))

    def _show(title, group, limit_c=3):
        print(f"\n{'=' * 78}\n{title} — {len(group)}\n{'=' * 78}")
        for s, cands in group:
            print(f"\n  {s.get('request_id')}  {s.get('lane')}  "
                  f"MDOLX{s.get('mdolx_ref') or '?'}  "
                  f"vessel={s.get('vessel_voyage')!r}  "
                  f"containers={s.get('containers')!r} "
                  f"cc={s.get('container_count')} teu={s.get('teu_won')}")
            for d in cands[:limit_c]:
                c = d["row"]
                why = []
                if d["vessel_hit"]:
                    why.append(f"VESSEL {'+'.join(d['vessel_hit'])}")
                if d["cc_hit"]:
                    why.append(f"containers={c.get('container_count')}")
                if d["teu_hit"]:
                    why.append("teu")
                print(f"      -> {c.get('request_id')}  status={c.get('status')} "
                      f"ask={str(c.get('request_timestamp'))[:10]} "
                      f"(+{d['days']:.0f}d)  rate={c.get('ol_rate')!r} "
                      f"carrier={c.get('carrier_quoted')!r}  "
                      f"[{', '.join(why) or 'lane+time only'}]")

    _show("CONFIDENT — lane + time + (vessel or containers)", confident)
    _show("POSSIBLE — lane + time only, needs a human", possible)

    print(f"\n{'=' * 78}\nORPHANS — nothing to match — {len(orphan)}\n{'=' * 78}")
    for s, why in orphan:
        print(f"  {s.get('request_id')}  {s.get('lane')}  "
              f"MDOLX{s.get('mdolx_ref') or '?'}  vessel={s.get('vessel_voyage')!r}  — {why}")

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    print(f"  standalone rows      : {len(standalones)}")
    print(f"  CONFIDENT candidates : {len(confident)}")
    print(f"  POSSIBLE candidates  : {len(possible)}")
    print(f"  ORPHANS              : {len(orphan)}")
    print("\n  Nothing was written. Applying a link is a separate, reviewed step —"
          "\n  a booking tied to the wrong RFQ manufactures a win against a"
          "\n  request that was really lost.")
    if os.environ.get("DIAG_JSON"):
        out = {
            "confident": [s.get("request_id") for s, _ in confident],
            "possible": [s.get("request_id") for s, _ in possible],
            "orphan": [s.get("request_id") for s, _ in orphan],
        }
        print("\nDIAG_JSON " + json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
