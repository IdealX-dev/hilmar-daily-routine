"""
patch_carriers.py — Enrichment: fill carrier_won + lane on WIN rows that landed
without carrier attribution.

Two-stage strategy:
  1. AUTO-DISCOVERY (added 2026-05-07 per Michael 'handle all suggestions'):
     For each WIN missing carrier, scan stage_emails.txt for ALL emails
     matching the MDOLX ref. Try parse_subject_carrier on each subject —
     a single MDOLX often has multiple emails (PLEASE UPDATE, NEW BOOKING
     CONFIRMATION, REVISED BOOKING, etc.) and only one of them may carry
     the carrier name. Same trick for lane (Origin → Destination).
  2. MANUAL FALLBACK: CARRIER_BY_MDOLX dict for MDOLX refs whose subjects
     never carry a carrier signal (Lonny 'covered' off-channel, draft-only
     emails, etc.). Maintained by hand when auto-discovery returns nothing.

Idempotent — only writes where carrier_won is currently missing/empty.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import core as C  # noqa: E402
import body_parser as BP  # noqa: E402

# Manual fallback — only for MDOLX refs whose stage subjects truly have
# no carrier signal. Auto-discovery handles the common case.
CARRIER_BY_MDOLX: dict[str, str] = {
    "260062": "MSC",            # subject: "MSC BKG # EBKG14800694"
    "260211": "CMA CGM",        # body verified: 6x "CMA CGM"; booking NAM832...
    "260240": "Evergreen",      # subject: "EVERGREEN BKG # 404640177726"
    "260357": "MSC",            # subject: "MSC BKG # EBKG16245253"
    "260367": "Evergreen",      # subject: "EVERGREEN BKG # 404640284442"
    "260388": "Evergreen",      # subject: "EVERGREEN BKG # 404640301435"
    "260407": "Evergreen",      # subject: "EVERGREEN BKG # 404640318320"
    "260408": "Evergreen",      # subject: "EVERGREEN BKG # 404640318371"
    "260420": "ONE",            # body & subject: "// ONE: RICGE7217600"
    "260426": "CMA CGM",        # subject: "// CMA: NAM8321190"
    "260460": "CMA CGM",        # subject: "// CMA: NAM8400958" (Oakland->Tokyo 4x40RF)
    "260482": "ONE",            # subject: "// ONE LINE BKG # RICGH7587500"
    "260486": "Evergreen",      # subject: "EVERGREEN BKG # 404640376320"
    "260491": "OOCL",           # subject: "// OOCL BKG #"
}


def _load_stage_subjects_by_mdolx() -> dict[str, list[str]]:
    """Read stage_emails.txt and group all subjects by the MDOLX ref(s) they mention."""
    out: dict[str, list[str]] = {}
    stage_path = ROOT / "scripts" / "stage_emails.txt"
    if not stage_path.exists():
        return out
    for line in stage_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        subj = d.get("subject") or ""
        if not subj:
            continue
        for m in re.finditer(r"MDOLX\s*0*(\d{4,})", subj, flags=re.IGNORECASE):
            ref = m.group(1)
            out.setdefault(ref, []).append(subj)
    return out


def _load_bodies_by_imid() -> dict[str, str]:
    """Read stage_emails_bodies.txt and index by message-id (imid).

    Q&L rows carry `source_imids` linking to the OL rate-response messages.
    When the table parser fails (prose-format quotes), we scan the body
    text directly for carrier name near a rate amount.
    """
    out: dict[str, str] = {}
    bodies_path = ROOT / "scripts" / "stage_emails_bodies.txt"
    if not bodies_path.exists():
        return out
    for line in bodies_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        imid = (d.get("imid") or "").strip("<>").strip()
        if not imid:
            continue
        # Field name is `text_body` in the current schema (was `body` /
        # `body_text` in legacy refresh_stage versions). Try all three so
        # this works across stage_emails_bodies.txt versions.
        body = d.get("text_body") or d.get("body") or d.get("body_text") or d.get("summary_preview") or ""
        if body:
            out[imid] = body
    return out


# Carrier-name patterns in body text. Keyed by canonical name; values are
# regex patterns that match the carrier in prose. Designed for false-negative
# avoidance: must be paired with a rate-dollar amount in the same body to
# claim the row.
_BODY_CARRIER_PATTERNS = [
    ("CMA CGM",   re.compile(r"\b(?:CMA\s*CGM|CMA-?CGM|\bCMA\b)\b", re.I)),
    ("MSC",       re.compile(r"\bMSC\b", re.I)),
    ("Maersk",    re.compile(r"\bMaersk\b", re.I)),
    ("ONE",       re.compile(r"\b(?:ONE|Ocean Network Express)\b")),  # case-sensitive ONE
    ("OOCL",      re.compile(r"\bOOCL\b", re.I)),
    ("Evergreen", re.compile(r"\b(?:Evergreen|EMC)\b", re.I)),
    ("HMM",       re.compile(r"\bHMM\b", re.I)),
    ("Yang Ming", re.compile(r"\b(?:Yang\s*Ming|YML)\b", re.I)),
    # Match Hapag, Hapag-Lloyd, HAPAG, or HLAG (alpha codes vary in OL prose)
    ("Hapag-Lloyd", re.compile(r"\b(?:Hapag(?:[\s\-]?Lloyd)?|HLAG)\b", re.I)),
    ("ZIM",       re.compile(r"\bZIM\b")),
    ("COSCO",     re.compile(r"\bCOSCO\b", re.I)),
]

_BODY_RATE_PATTERN = re.compile(r"\$\s*([\d,]{3,}(?:\.\d{2})?)")


def _discover_carrier_from_bodies(imids: list[str], bodies_by_imid: dict[str, str]) -> tuple[str | None, float | None]:
    """For a Q&L row's source_imids, scan body texts for carrier+rate signal.

    Returns (carrier, rate) — either may be None. Carrier is claimed only when
    a carrier name AND a $-rate appear in the same body (avoids false positives
    from carrier names mentioned in prose without a quote).
    """
    for imid in imids or []:
        key = imid.strip("<>").strip()
        body = bodies_by_imid.get(key)
        if not body:
            continue
        rate_m = _BODY_RATE_PATTERN.search(body)
        if not rate_m:
            continue  # No rate = no quote = don't claim a carrier
        for canonical, pat in _BODY_CARRIER_PATTERNS:
            if pat.search(body):
                rate_val = None
                try:
                    rate_val = float(rate_m.group(1).replace(",", ""))
                except ValueError:
                    pass
                return canonical, rate_val
    return None, None


def _discover_carrier_from_subjects(subjects: list[str]) -> str | None:
    """Try parse_subject_carrier on each subject — return first non-None hit."""
    for s in subjects:
        c = BP.parse_subject_carrier(s)
        if c:
            return C.normalize_carrier(c)
    return None


def _discover_lane_from_subjects(subjects: list[str]) -> tuple[str | None, str | None]:
    """Find an 'Origin to Destination' phrase in any of the MDOLX's subjects.

    Booking-confirmation subjects look like:
      MDOLX260587_ *NEW BOOKING CONFIRMATION // HILMAR - Oakland to Osaka - 2X40'RF // EVERGREEN ...
    """
    for s in subjects:
        try:
            o, d = BP.parse_subject_lane(s)
            if o and d and o != "Unknown" and d != "Unknown":
                return o, d
        except Exception:
            pass
    return None, None


def main():
    cfg = C.load_config()
    data_path = Path(cfg["paths"]["data"])
    data = json.loads(data_path.read_text())

    requests = data.get("requests", [])
    patched_carrier = 0
    patched_lane = 0
    patched_ql_carrier = 0
    patched_rate = 0
    auto_hits: list[str] = []
    manual_hits: list[str] = []
    body_hits: list[str] = []

    stage_by_mdolx = _load_stage_subjects_by_mdolx()
    bodies_by_imid = _load_bodies_by_imid()
    normalized_manual = {mdolx: C.normalize_carrier(c) for mdolx, c in CARRIER_BY_MDOLX.items()}

    def _row_mdolx_candidates(row):
        out = []
        if row.get("mdolx_ref"):
            out.append(row["mdolx_ref"])
        for m in row.get("mdolx_refs_all", []) or []:
            out.append(m)
        if row.get("booking_id"):
            out.append(row["booking_id"])
        if row.get("mdolx"):
            out.append(row["mdolx"])
        return out

    for r in requests:
        if r.get("status") != "WIN":
            continue

        # CARRIER attribution
        if not (r.get("carrier_won") or r.get("carrier_quoted")):
            canon = None
            mdolx_used = None
            # 1. Auto-discovery from stage subjects
            for cand in _row_mdolx_candidates(r):
                subjects = stage_by_mdolx.get(cand, [])
                if subjects:
                    canon = _discover_carrier_from_subjects(subjects)
                    if canon:
                        mdolx_used = cand
                        auto_hits.append(f"{cand}->{canon}")
                        break
            # 2. Manual fallback
            if not canon:
                for cand in _row_mdolx_candidates(r):
                    if cand in normalized_manual:
                        canon = normalized_manual[cand]
                        mdolx_used = cand
                        manual_hits.append(f"{cand}->{canon}")
                        break
            if canon:
                r["carrier_won"] = canon
                if not r.get("carrier_quoted"):
                    r["carrier_quoted"] = canon
                patched_carrier += 1
                print(f"  PATCH carrier {mdolx_used} -> {canon} (dest={r.get('destination')})")

        # LANE attribution — only when current lane is unresolved/empty
        lane_now = r.get("lane") or ""
        dest_now = r.get("destination") or ""
        if lane_now in ("", "Lane unresolved") or dest_now in ("", "Unknown"):
            for cand in _row_mdolx_candidates(r):
                subjects = stage_by_mdolx.get(cand, [])
                o, d = _discover_lane_from_subjects(subjects)
                if o and d:
                    if not r.get("origin") or r.get("origin") in ("", "Unknown"):
                        r["origin"] = o
                    if not r.get("destination") or r.get("destination") in ("", "Unknown"):
                        r["destination"] = d
                    r["lane"] = f"{r.get('origin', o)} → {r.get('destination', d)}"
                    patched_lane += 1
                    print(f"  PATCH lane    {cand} -> {r['lane']}")
                    break

    # Q&L body-text carrier fallback — added 2026-05-07 per Michael "did
    # you fix the drifts and all from the 1233pm report". The table-format
    # parser caught ~48% of Q&L. The body-scan looks at the actual rate-
    # response message body for carrier + rate co-occurrence.
    for r in requests:
        if r.get("status") != "LOSS" or not r.get("quoted"):
            continue
        if r.get("carrier_quoted"):
            continue
        imids = r.get("source_imids") or []
        if not imids:
            continue
        canon, rate = _discover_carrier_from_bodies(imids, bodies_by_imid)
        if canon:
            r["carrier_quoted"] = C.normalize_carrier(canon) or canon
            patched_ql_carrier += 1
            body_hits.append(f"{r.get('request_id')}->{canon}")
            if rate is not None and not r.get("ol_rate"):
                r["ol_rate"] = rate
                patched_rate += 1
            print(f"  PATCH Q&L  {r.get('request_id')[:16]} -> {canon}"
                  + (f" @ ${rate:.0f}" if rate else ""))

    if patched_carrier == 0 and patched_lane == 0 and patched_ql_carrier == 0:
        print("Nothing to patch - all target rows already have carrier and lane.")
        return

    print(f"\nSummary: {patched_carrier} WIN-carrier patches "
          f"({len(auto_hits)} auto / {len(manual_hits)} manual), "
          f"{patched_lane} lane patches, "
          f"{patched_ql_carrier} Q&L-carrier patches "
          f"(via body scan), {patched_rate} rate patches")

    meta = data.setdefault("meta", {})
    rev = int(meta.get("revision", 0)) + 1
    meta["revision"] = rev
    meta["patched_by"] = "patch_carriers.py"

    from datetime import datetime, timezone
    data["last_updated"] = datetime.now(timezone.utc).isoformat()

    data_path.write_text(json.dumps(data, indent=2))
    print(f"OK Patched -> revision {rev}")


if __name__ == "__main__":
    main()
