#!/usr/bin/env python3
"""
reprocess_bodies.py - Re-run html_to_text + _parse_all on every row in
stage_emails_bodies.jsonl, writing the refreshed text_body / parsed back.

Use after changing body_parser or core.parse_signer so existing fetches
benefit from the new logic without re-fetching.

Idempotent — safe to re-run.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import body_parser as BP  # noqa: E402
import fetch_bodies as FB  # noqa: E402

# 2026-05-19: stage files renamed .jsonl → .txt 2026-05-06 (so SharePoint
# indexes them). Resolve to the .txt file when present, fall back to the
# legacy .jsonl for back-compat with older boxes.
def _resolve(p: Path) -> Path:
    txt = p.with_suffix(".txt")
    legacy = p.with_suffix(".jsonl")
    return txt if txt.exists() or not legacy.exists() else legacy
BODIES = _resolve(ROOT / "scripts" / "stage_emails_bodies")


def main() -> int:
    if not BODIES.exists():
        print(f"ERR: {BODIES} not found", file=sys.stderr)
        return 1

    rows = []
    delta_signer = 0
    delta_vessel = 0
    delta_carrier = 0
    delta_rate = 0
    for line in BODIES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        old_parsed = rec.get("parsed") or {}
        html = rec.get("html_body") or ""
        text = BP.html_to_text(html)
        parsed = FB._parse_all(text, rec.get("subject", ""), rec.get("bucket", ""))

        # Track gains/changes
        if not old_parsed.get("ol_responder_signer") and parsed.get("ol_responder_signer"):
            delta_signer += 1
        if not old_parsed.get("vessel_voyage") and parsed.get("vessel_voyage"):
            delta_vessel += 1
        old_rt = old_parsed.get("rate_table") or {}
        new_rt = parsed.get("rate_table") or {}
        if not old_rt.get("carrier_quoted") and new_rt.get("carrier_quoted"):
            delta_carrier += 1
        if not old_rt.get("ol_rate") and new_rt.get("ol_rate"):
            delta_rate += 1

        rec["text_body"] = text
        rec["parsed"] = parsed
        rows.append(rec)

    # Write back
    with BODIES.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Coverage report
    n = len(rows)
    n_signer = sum(1 for r in rows if (r.get("parsed") or {}).get("ol_responder_signer"))
    n_vessel = sum(1 for r in rows if (r.get("parsed") or {}).get("vessel_voyage"))
    n_rt_carrier = sum(1 for r in rows if ((r.get("parsed") or {}).get("rate_table") or {}).get("carrier_quoted"))
    n_rt_rate = sum(1 for r in rows if ((r.get("parsed") or {}).get("rate_table") or {}).get("ol_rate"))
    ol_only = [r for r in rows if r.get("bucket") in ("mbd_rate_response", "mbd_inbound")]
    n_signer_ol = sum(1 for r in ol_only if (r.get("parsed") or {}).get("ol_responder_signer"))
    n_vessel_ol = sum(1 for r in ol_only if (r.get("parsed") or {}).get("vessel_voyage"))

    print(f"Reprocessed {n} bodies")
    print(f"  Newly populated signer:  +{delta_signer}")
    print(f"  Newly populated vessel:  +{delta_vessel}")
    print(f"  Newly populated rate.carrier: +{delta_carrier}")
    print(f"  Newly populated rate.rate:    +{delta_rate}")
    print()
    print(f"Total coverage:")
    print(f"  ol_responder_signer (OL-only):  {n_signer_ol}/{len(ol_only)}")
    print(f"  vessel_voyage (OL-only):        {n_vessel_ol}/{len(ol_only)}")
    print(f"  rate_table.carrier_quoted:      {n_rt_carrier}/{n}")
    print(f"  rate_table.ol_rate:             {n_rt_rate}/{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
