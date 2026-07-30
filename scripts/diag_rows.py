"""diag_rows.py — why does a row show a quote while OL-USA RESPONSES is empty?

2026-07-30: the Jul 29 report rendered

    NEW REQUESTS FROM LONNY (3)   populated
    OL-USA RESPONSES (0)          "No activity"
    STATUS CHANGES (0)            "No activity"
    PENDING OL (0)                "No activity"
    PENDING HILMAR (3)            populated, WITH carrier + rate (CMA CGM $3,150)

Those cannot all be right. gen_email buckets by EVENT DATE:

    scripts/gen_email.py:186-199
        if req_d  == today_date: new_requests.append(r)
        if resp_d == today_date: ol_responses.append(r)   # response_timestamp

while PENDING HILMAR is CURRENT STATE and not windowed at all
(scripts/gen_email.py:800-801). So a row with a carrier and a rate but no
usable `response_timestamp` silently vanishes from OL-USA RESPONSES while
still showing its quote under PENDING HILMAR — the report would under-report
real OL activity and nothing would say so.

This prints, for the report window, exactly the fields that decide which
bucket a row lands in, so the question is answered from data instead of
inference.

READ-ONLY. Pulls state from the blob store (reads still work; it is writes
that 404 — see scripts/diag_blob.py) into a temp dir and prints. It never
writes to the blob, never mutates the working tree, and never emails.

PII: prints lane/carrier/rate/timestamps and request ids only. No email
bodies, no addresses, no subjects.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _et_date(ts):
    """Mirror gen_email._et_date closely enough to reproduce its bucketing."""
    if not ts:
        return None
    try:
        import core
        return core.et_date_of(ts) if hasattr(core, "et_date_of") else None
    except Exception:
        return None


def main() -> int:
    window = os.environ.get("DIAG_WINDOW_DATE", "").strip()

    import state_store

    tmp = Path(tempfile.mkdtemp())
    try:
        pulled = state_store.pull(root=tmp)
    except Exception as e:
        print(f"pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s) into a temp dir: {', '.join(pulled)}")

    data_path = tmp / "tracking-data-v2.json"
    if not data_path.exists():
        print("tracking-data-v2.json not in the store — nothing to inspect")
        return 2
    data = json.loads(data_path.read_text(encoding="utf-8"))
    rows = data.get("requests", [])
    print(f"tracking-data-v2.json: {len(rows)} requests")
    print("NOTE: this is the STORED state. Writes have failed since "
          "2026-07-27 18:29 UTC, so it is FROZEN at that point and does NOT "
          "contain anything the last three runs ingested.\n")

    if not window:
        # Default to the most recent request_date present.
        dates = sorted({str(r.get("request_date") or "")[:10] for r in rows} - {""})
        window = dates[-1] if dates else ""
        print(f"DIAG_WINDOW_DATE unset — using latest request_date present: {window}")

    print(f"\n{'=' * 78}\nROWS WITH request_date == {window}\n{'=' * 78}")
    hit = 0
    for r in rows:
        rd = str(r.get("request_date") or "")[:10]
        if rd != window:
            continue
        hit += 1
        rts = r.get("request_timestamp")
        resp = r.get("response_timestamp")
        print(f"\n  request_id            : {r.get('request_id')}")
        print(f"  lane                  : {r.get('lane') or (str(r.get('origin')) + ' -> ' + str(r.get('destination')))}")
        print(f"  status                : {r.get('status')}   quoted={r.get('quoted')}")
        print(f"  request_timestamp     : {rts}   (ET date {_et_date(rts)})")
        print(f"  response_timestamp    : {resp!r}   (ET date {_et_date(resp)})")
        print(f"  carrier_quoted        : {r.get('carrier_quoted')!r}")
        print(f"  ol_rate               : {r.get('ol_rate')!r}")
        print(f"  ol_responder_signer   : {r.get('ol_responder_signer')!r}")
        print(f"  status_history entries: {len(r.get('status_history') or [])}")
        for h in (r.get("status_history") or [])[-3:]:
            print(f"      {h.get('at')}  {h.get('from')} -> {h.get('to')}")
        # THE POINT: a row quoted but with no response_timestamp is invisible
        # to OL-USA RESPONSES while still rendering under PENDING HILMAR.
        if (r.get("carrier_quoted") or r.get("ol_rate")) and not resp:
            print("  *** QUOTED BUT NO response_timestamp — this row can never "
                  "appear in OL-USA RESPONSES ***")
    if not hit:
        print("  (no rows with that request_date in the stored state)")

    # Account-wide sweep for the same shape, so this is not judged on 3 rows.
    print(f"\n{'=' * 78}\nWHOLE-DATASET SWEEP\n{'=' * 78}")
    quoted_no_ts = [r for r in rows
                    if (r.get("carrier_quoted") or r.get("ol_rate"))
                    and not r.get("response_timestamp")]
    print(f"  rows with a carrier or rate but NO response_timestamp: {len(quoted_no_ts)}")
    for r in quoted_no_ts[:25]:
        print(f"    {r.get('request_id')}  {r.get('lane')}  "
              f"carrier={r.get('carrier_quoted')!r} rate={r.get('ol_rate')!r} "
              f"status={r.get('status')}")
    if len(quoted_no_ts) > 25:
        print(f"    … and {len(quoted_no_ts) - 25} more")

    print("\n  Every one of those is a real OL quote that the daily report's")
    print("  OL-USA RESPONSES section can never show, on any day, because the")
    print("  bucket is keyed on response_timestamp.")

    resp_dates: dict[str, int] = {}
    for r in rows:
        d = _et_date(r.get("response_timestamp"))
        if d:
            resp_dates[str(d)] = resp_dates.get(str(d), 0) + 1
    print("\n  response_timestamp ET-date histogram (last 10 dates present):")
    for d in sorted(resp_dates)[-10:]:
        print(f"    {d}  {resp_dates[d]:4d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
