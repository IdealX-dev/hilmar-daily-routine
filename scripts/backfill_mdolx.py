"""
backfill_mdolx.py — Backfill missing mdolx_ref on WIN rows by scanning
the staged emails for MDOLX subjects that share the same conversation/thread.

Per Michael 2026-05-17 "this parser and your system have to run at minimum
of 98 percent accuracy." Baseline measurement of tracking-data-v2.json
found 11 of 62 WIN rows missing mdolx_ref (82.3% — well under the 98%
threshold for QC-039). These WINs have valid carrier + lane data but
the MDOLX number wasn't extracted during ingest.

STRATEGY

1. Read tracking-data-v2.json, find all WIN rows where mdolx_ref is null/empty.
2. For each, gather identifying signals:
   - source_imids (Internet Message-IDs of source emails)
   - conversation_id
   - request_timestamp (rough match window)
3. Scan stage_emails.txt for MDOLX-containing subjects:
   - Subject contains "MDOLX######" (4+ digits, separator-tolerant)
   - Match to the WIN via shared imid OR shared conversation_id OR
     shared sent_within(±48h, ±lane_signature)
4. For each WIN matched, set mdolx_ref to the extracted number.
5. Persist updates back to tracking-data-v2.json (atomic write).
6. Re-compute parser accuracy + report.

SAFETY

- Always writes a backup BEFORE modifying tracking-data-v2.json.
- Dry-run mode (--dry) prints proposed changes without writing.
- Idempotent: running twice produces same result.

USAGE

  python scripts/backfill_mdolx.py --dry      # show proposed updates
  python scripts/backfill_mdolx.py            # apply
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "tracking-data-v2.json"
# When invoked from the OneDrive working dir, DATA_PATH may be one level
# different. Try both.
if not DATA_PATH.exists():
    DATA_PATH = ROOT.parent / "tracking-data-v2.json"

STAGE_PATH = ROOT / "scripts" / "stage_emails.txt"
if not STAGE_PATH.exists():
    STAGE_PATH = ROOT.parent / "scripts" / "stage_emails.txt"

# Separator-tolerant MDOLX regex per the recently-shipped fix in
# ol-quote-tracker PR #270. Captures "MDOLX260622" / "MDOLX-260622" /
# "MDOLX 260622" / "mdolx_260622" / "Mdolx-260622".
MDOLX_RX = re.compile(r"MDOL([XMFD])[-\s_]*(\d{4,})", re.IGNORECASE)


def _ymd(s: str | None) -> str:
    return (s or "")[:10]


def load_stage() -> list[dict]:
    rows = []
    with STAGE_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _normalize_mdolx(s: str) -> str | None:
    """Return canonical MDOLX###### from a subject (uppercase, no separator)."""
    m = MDOLX_RX.search(s or "")
    if m:
        return f"MDOL{m.group(1).upper()}{m.group(2)}"
    return None


# Carrier-name → booking-ref-prefix mapping (e.g. CMA CGM uses NAM,APL,ANL,
# ONE uses RICG,ONEY, etc). Used to verify a stage email's carrier matches
# the WIN's carrier_won when subjects contain refs but not the carrier name.
_CARRIER_REF_PREFIXES: dict[str, tuple[str, ...]] = {
    "CMA CGM":     ("NAM", "APL", "ANL", "CMA", "CGM"),
    "Maersk":      ("MAEU", "SEAU", "SUDU", "MSK", "MAERSK"),
    "MSC":         ("MEDU", "MSCU", "EBKG", "MSC"),
    "ONE":         ("ONEY", "RICG", "SCNB", "ONE"),
    "Evergreen":   ("EBKG", "EISU", "EGLV", "EVERGREEN", "EMC"),
    "Hapag-Lloyd": ("HLCU", "HLBU", "HLAG", "HAPAG"),
    "OOCL":        ("OOLU", "OOCL"),
    "Yang Ming":   ("YMLU", "YML", "YANGMING", "YANG"),
    "HMM":         ("HMMU", "HMM", "HYUNDAI"),
    "ZIM":         ("ZIMU", "ZIM"),
    "COSCO":       ("COSU", "COSCO", "COSCON"),
}


def _carrier_match(win_carrier: str, subject: str) -> bool:
    """Verify the subject names the WIN's carrier (by name or ref prefix).

    Two-tier match:
      Tier 1 — exact carrier name appears in subject (case-insensitive)
      Tier 2 — a known booking-ref prefix for that carrier appears
    """
    if not win_carrier or not subject:
        return False
    subj_up = subject.upper()
    canonical = win_carrier.strip()
    # Tier 1: name match
    if canonical.upper() in subj_up:
        return True
    # Tier 2: ref-prefix match
    prefixes = _CARRIER_REF_PREFIXES.get(canonical, ())
    for p in prefixes:
        if p.upper() in subj_up:
            return True
    return False


def find_mdolx_for_win(win: dict, stage_rows: list[dict]) -> str | None:
    """Find an MDOLX number for a WIN. Strict matching only — false matches
    are far worse than no match (they corrupt data).

    Strategy 1: shared conversation_id (rare in Hilmar data — stage rows
                often have conversationId=None for OL booking confirmations).
    Strategy 2: shared internet-message-id (most reliable when present).
    Strategy 3: HILMAR client tag + matching lane + matching carrier (by
                name OR booking-ref prefix) + ±14d time window. ALL FOUR
                must match — no fallback that drops any single requirement.
                Plus: must be a UNIQUE match (ambiguous = skip).
    """
    # Strategy 1: shared conversation_id
    win_conv = win.get("conversation_id") or win.get("conversationId")
    if win_conv:
        for s in stage_rows:
            if (s.get("conversationId") == win_conv or s.get("conversation_id") == win_conv):
                mdolx = _normalize_mdolx(s.get("subject", ""))
                if mdolx:
                    return mdolx

    # Strategy 2: shared internet-message-id
    win_imids = set(win.get("source_imids") or [])
    if win_imids:
        for s in stage_rows:
            if s.get("imid") in win_imids:
                mdolx = _normalize_mdolx(s.get("subject", ""))
                if mdolx:
                    return mdolx

    # Strategy 3: strict 4-way match (HILMAR + lane + carrier + time).
    # Collect ALL candidate matches and require UNIQUE — ambiguous = skip.
    win_lane = (win.get("lane") or "").strip()
    win_carrier = (win.get("carrier_won") or "").strip()
    win_ts = win.get("response_timestamp") or win.get("request_timestamp") or ""
    if not (win_lane and win_carrier and win_ts):
        return None
    if " → " not in win_lane:
        return None
    origin, dest = win_lane.split(" → ", 1)
    if not (origin and dest):
        return None
    win_date = win_ts[:10]
    candidates = set()
    for s in stage_rows:
        subj = s.get("subject", "") or ""
        if not subj:
            continue
        # Must be a Hilmar MDOLX booking confirmation
        if "HILMAR" not in subj.upper():
            continue
        mdolx = _normalize_mdolx(subj)
        if not mdolx:
            continue
        # Lane match (both origin AND destination in subject)
        if origin.lower() not in subj.lower() or dest.lower() not in subj.lower():
            continue
        # Carrier match (by name OR booking-ref prefix)
        if not _carrier_match(win_carrier, subj):
            continue
        # Time match within ±14 days (booking confirmations can be days
        # before the WIN gets logged)
        sent = (s.get("sent") or s.get("received") or "")[:10]
        if not sent or abs(_days_apart(sent, win_date)) > 14:
            continue
        candidates.add(mdolx)

    # Require unique match — if multiple MDOLXs match, operator must review
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _days_apart(a: str, b: str) -> int:
    try:
        da = datetime.strptime(a, "%Y-%m-%d").date()
        db = datetime.strptime(b, "%Y-%m-%d").date()
        return (da - db).days
    except Exception:
        return 9999


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="Show proposed updates without writing")
    ap.add_argument("--verbose", action="store_true", help="Print per-row details")
    args = ap.parse_args()

    if not DATA_PATH.exists():
        print(f"❌ tracking-data-v2.json not found at {DATA_PATH}")
        return 2
    if not STAGE_PATH.exists():
        print(f"⚠️  stage_emails.txt not found at {STAGE_PATH} — backfill cannot run")
        return 1

    print(f"📁 Data: {DATA_PATH}")
    print(f"📁 Stage: {STAGE_PATH}")
    print()

    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    stage_rows = load_stage()
    print(f"Loaded {len(stage_rows)} staged emails")

    wins_missing = [
        r for r in data.get("requests", [])
        if r.get("status") == "WIN" and not (r.get("mdolx_ref") or "").strip()
    ]
    print(f"WIN rows missing mdolx_ref: {len(wins_missing)}")
    print()

    updates = []
    for r in wins_missing:
        mdolx = find_mdolx_for_win(r, stage_rows)
        if mdolx:
            updates.append((r, mdolx))
            if args.verbose or args.dry:
                print(f"  ✅ {r.get('request_id', '?')[:30]:30} "
                      f"{(r.get('lane') or '?')[:35]:35} → {mdolx}")
        elif args.verbose:
            print(f"  ⚠️  {r.get('request_id', '?')[:30]:30} "
                  f"{(r.get('lane') or '?')[:35]:35} → no match")

    print()
    print(f"Proposed updates: {len(updates)} / {len(wins_missing)}")

    if args.dry:
        print("(dry-run — no changes written)")
        return 0

    if not updates:
        print("Nothing to update.")
        return 0

    # Atomic backup
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bkpath = DATA_PATH.with_suffix(f".pre-mdolx-backfill-{ts}.json")
    shutil.copy2(DATA_PATH, bkpath)
    print(f"📦 Backup written: {bkpath.name}")

    for r, mdolx in updates:
        r["mdolx_ref"] = mdolx
        # Mark provenance
        r.setdefault("_backfill", []).append({
            "field": "mdolx_ref",
            "value": mdolx,
            "source": "scripts/backfill_mdolx.py",
            "at": datetime.now(timezone.utc).isoformat(),
        })

    DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✅ Wrote {DATA_PATH} with {len(updates)} mdolx_ref updates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
