"""
patch_carriers.py — One-shot enrichment: fill carrier_won on WIN rows that landed
without a rate-response match (standalones + MDOLX_MATCH_NO_QUOTE).

Carriers were extracted from the MDOLX booking subject pattern `// CARRIER: BKG#`
(with one body-verified case: 260211 = CMA CGM, confirmed via 6x "CMA CGM" mentions
in the email body).

This is idempotent — only writes where carrier_won is currently missing/empty.
"""
from __future__ import annotations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import core as C  # noqa: E402

# Subject- and body-derived mapping
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


def main():
    cfg = C.load_config()
    data_path = Path(cfg["paths"]["data"])
    data = json.loads(data_path.read_text())

    requests = data.get("requests", [])
    patched = 0
    normalized_map = {mdolx: C.normalize_carrier(c) for mdolx, c in CARRIER_BY_MDOLX.items()}

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
        if r.get("carrier_won") or r.get("carrier_quoted"):
            continue
        matched_mdolx = None
        for cand in _row_mdolx_candidates(r):
            if cand in normalized_map:
                matched_mdolx = cand
                break
        if not matched_mdolx:
            continue
        canon = normalized_map[matched_mdolx]
        r["carrier_won"] = canon
        if not r.get("carrier_quoted"):
            r["carrier_quoted"] = canon
        patched += 1
        print(f"  PATCH {matched_mdolx} -> {canon} (dest={r.get('destination')})")

    if patched == 0:
        print("Nothing to patch - all target rows already have a carrier.")
        return

    meta = data.setdefault("meta", {})
    rev = int(meta.get("revision", 0)) + 1
    meta["revision"] = rev
    meta["patched_by"] = "patch_carriers.py"

    from datetime import datetime, timezone
    data["last_updated"] = datetime.now(timezone.utc).isoformat()

    data_path.write_text(json.dumps(data, indent=2))
    print(f"OK Patched {patched} carrier_won values. Revision -> {rev}")


if __name__ == "__main__":
    main()
