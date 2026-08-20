#!/usr/bin/env python3
"""Why is the OL signer blank, and is the rate the number OL actually wrote?

READ-ONLY. Writes nothing, sends nothing.

Michael, 2026-08-20, on the OL-USA RESPONSES table: "why are signors missing
nothing should be missing.. also the numbers are wrong for $".

Two questions, and neither can be answered from the rendered table — the
report shows what was PARSED, and the question is how that compares to what
the email SAID. So this pairs every recent quoted row with the body it was
parsed from and prints them side by side:

  SIGNER  — the stored value, whether a body is even linked, and if one is,
            the last non-empty lines of the top message (a signature, if it
            has one). ol_responder_signer is set ONLY from the body parse
            (ingest.py:1507-1509, and the booking fallback at :1062); there
            is no completeness gate on it — QC-027 tracks ETD/ETA/Vessel/
            Rate/Carrier/POL/POD and not this — so a blank is silent.

  RATE    — the stored ol_rate against every currency-shaped number in the
            body, so a mis-pick (a per-TEU figure read as per-container, a
            surcharge read as the base, the second lane's rate on a
            multi-lane quote) is visible rather than inferred.

Usage:  python3 scripts/diag_signer_and_rate.py [--days 7] [--limit 12]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import core  # noqa: E402

MONEY = re.compile(r"(?:USD\s*)?\$\s?([0-9][0-9,]{1,7}(?:\.\d{2})?)", re.I)
OL_EMAIL = re.compile(r"[A-Za-z][A-Za-z.'\-]*@ol-usa\.com", re.I)
BAR = "=" * 78


def _bodies(root: Path) -> dict:
    """internetMessageId -> cached body record."""
    out = {}
    p = root / "scripts" / "stage_emails_bodies.txt"
    if not p.exists():
        p = root / "stage_emails_bodies.txt"
    if not p.exists():
        return out
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        imid = rec.get("imid") or rec.get("internetMessageId")
        if imid:
            out[imid] = rec
    return out


def _text(rec) -> str:
    for k in ("text_body", "body_text", "body", "preview", "snippet"):
        v = (rec or {}).get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _tail(text: str, n: int = 14) -> list[str]:
    """Last non-empty lines of the TOP message — where a signature lives."""
    try:
        top = core._strip_chain(text)
    except Exception:
        top = text
    lines = [ln.strip() for ln in top.splitlines() if ln.strip()]
    return lines[-n:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=12)
    a = ap.parse_args()

    # PULL THE STATE. Nothing lives in the checkout — tracking-data-v2.json
    # and the body cache are in the blob store, and every other diag in this
    # workflow pulls them itself. The first version of this script read the
    # repo root, died with FileNotFoundError on its first line, and the
    # step's `|| true` swallowed it: run 32380990788 went GREEN having
    # printed nothing. A diagnostic that cannot say it failed is worse than
    # no diagnostic, so the pull failure below is loud and the exit is
    # non-zero.
    tmp = Path(tempfile.mkdtemp())
    try:
        import state_store
        pulled = state_store.pull(root=tmp)
    except Exception as e:
        print(f"::error::diag_signer_and_rate: state pull FAILED: "
              f"{type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s)")

    data_path = tmp / "tracking-data-v2.json"
    if not data_path.exists():
        print("::error::diag_signer_and_rate: tracking-data-v2.json not in "
              "the store — nothing to inspect")
        return 2
    data = json.loads(data_path.read_text(encoding="utf-8"))
    rows = data.get("requests", []) or []
    bodies = _bodies(tmp)
    cutoff = datetime.now(timezone.utc) - timedelta(days=a.days)

    recent = []
    for r in rows:
        if not (core.is_real_rate(r.get("ol_rate")) or r.get("carrier_quoted")):
            continue
        dt = core.parse_iso(r.get("response_timestamp")
                            or r.get("request_timestamp"))
        if dt and dt >= cutoff:
            recent.append((dt, r))
    recent.sort(key=lambda p: p[0], reverse=True)
    recent = [r for _dt, r in recent[:a.limit]]

    print(BAR)
    print(f"SIGNER + RATE PROVENANCE — {len(recent)} quoted row(s), last "
          f"{a.days} days")
    print(f"body cache: {len(bodies)} message(s)")
    print(BAR)

    no_signer = no_body = rate_absent = 0

    for r in recent:
        lane = r.get("lane") or f"{r.get('origin')} → {r.get('destination')}"
        signer = r.get("ol_responder_signer")
        rate = r.get("ol_rate")
        imids = r.get("source_imids") or []
        print(f"\n  {r.get('request_id')}  {lane}")
        print(f"    equipment      : {r.get('containers')!r}  "
              f"teu={r.get('teu_requested')!r}")
        print(f"    carrier_quoted : {r.get('carrier_quoted')!r}")
        print(f"    ol_rate        : {rate!r}")
        print(f"    signer         : {signer!r}")
        print(f"    source_imids   : {len(imids)}")

        if not signer:
            no_signer += 1

        linked = [bodies[i] for i in imids if i in bodies]
        if not linked:
            no_body += 1
            print("    body           : NONE CACHED — signer and rate cannot "
                  "be re-derived or checked from here")
            continue

        # PER ROW, not per body. A row links Lonny's ASK and OL's REPLY; only
        # the reply carries money. The first version counted each body, so a
        # perfectly-parsed dataset reported "stored rate NOT in body: 12" —
        # a scary number that was purely an artefact of counting the asks.
        rate_seen_somewhere = False
        for rec in linked[:2]:
            txt = _text(rec)
            subj = (rec.get("subject") or "")[:70]
            print(f"    body subject   : {subj!r}  ({len(txt)} chars)")
            if not txt:
                continue
            found = [m.group(1) for m in MONEY.finditer(txt)]
            uniq = sorted({f.replace(",", "") for f in found},
                          key=lambda s: float(s), reverse=True)
            print(f"    $ in body      : {uniq[:10]}")
            if rate is not None:
                as_str = f"{float(rate):.2f}".rstrip("0").rstrip(".")
                hit = any(abs(float(u) - float(rate)) < 0.01 for u in uniq)
                rate_seen_somewhere = rate_seen_somewhere or hit
                print(f"    stored rate {as_str} in THIS body: "
                      f"{'YES' if hit else 'no'}")
                if len(uniq) > 1:
                    print(f"    *** {len(uniq)} different $ figures in one "
                          f"body — the parser chose {as_str}; confirm it "
                          f"picked the right one ***")
                    # WHICH option did it skip? parse_rate_table reads
                    # _table_cells(rows[0], rows[1]) — the header and the
                    # FIRST data row only. Michael 2026-08-20: "could be
                    # different rates for different steamship lines." If the
                    # extra rows name a different carrier, the parser is
                    # silently picking one carrier's price; if they name the
                    # same carrier on a later vessel, it is picking the first
                    # sailing. Those need opposite fixes, so print the rows.
                    for ln in txt.splitlines():
                        if ln.count("|") >= 3 and any(
                                c.isdigit() for c in ln):
                            print(f"        ROW | {ln.strip()[:150]}")
            ols = sorted(set(OL_EMAIL.findall(txt)))
            if ols:
                print(f"    ol-usa addresses in body: {ols[:4]}")
            print("    signature tail :")
            for ln in _tail(txt):
                print(f"        | {ln[:90]}")
        if rate is not None and not rate_seen_somewhere:
            rate_absent += 1
            print("    *** stored rate appears in NO linked body — mis-pick ***")

    print("\n" + BAR)
    print("SUMMARY")
    print(BAR)
    print(f"  rows examined            : {len(recent)}")
    print(f"  blank signer             : {no_signer}")
    print(f"  no cached body at all    : {no_body}")
    print(f"  stored rate in NO linked body : {rate_absent}   "
          f"(0 = every rate traced to an OL email)")
    print("\n  A blank signer with a cached body means the signature pattern "
          "missed.\n  A blank signer with NO body means the evidence is gone "
          "(90-day retention)\n  and nothing can recover it from here.")
    print("  A stored rate absent from its own body is a mis-pick — the "
          "number\n  shown to the CEO is not the number OL wrote.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
