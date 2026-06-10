#!/usr/bin/env python3
"""
Collect MBD_OceanExportBookingShared "RE: Oakland to X" rate-response emails
from cached MCP tool-results, parse summary table fields, and append to
stage_emails.jsonl as bucket 'mbd_rate_response'.

Idempotent: dedupes by id before appending.
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path

TOOL_RESULTS_DIR = Path(
    '/sessions/awesome-sharp-faraday/mnt/.claude/projects/'
    '-sessions-awesome-sharp-faraday/639015f7-4bb4-4284-8e7a-920e9733f338/tool-results'
)
STAGE_PATH = Path(__file__).resolve().parent / 'stage_emails.jsonl'

RATE_RX = re.compile(r'^\s*(re|fw|fwd):\s*oakland\s+to\s+(.+?)\s*$', re.IGNORECASE)

# Table summary pattern examples:
# "Oakland Manila (North) 1x20'DV EVER LEGION 0TBNEW1MA 17-Apr-26 21-Apr-26 22-Apr-26 25-Apr-26 30-May-26 $2040 CMA SHANGHAI 4 DETENTION + 5 DEMURRAGE FREE"
# "Oakland HCMC 2 X 20'DV WAN HAI A01 W017 21-Apr-26 24-Apr-26 27-Apr-26 30-Apr-26 3-Jun-26 $450.00 ONE LINE DIRECT VIA CAI MEP 14 DETENTION + 14..."
# "Oakland Haiphong 1x20'DV WAN HAI A11 0016W 14-Apr-26 16-Apr-26 17-Apr-26 21-Apr-26 20-May-26 $508 HMM DIRECT 14 DETENTION + 5 DEMURRAGE FREE"

CONTAINER_RX = re.compile(
    r'\b(\d+)\s*[xX×]\s*(\d{2})(HC|HCRF|RF|DV|GP|OT|FR)?\b|'
    r'\b(\d+)\s*[-–]\s*(\d{2})[\'\u2019]?\s*(HC|RF|DV|GP|HC\s*Reefer)?\b',
    re.IGNORECASE,
)

# Rate price pattern — "$2040" or "$3,500" or "$450.00"
PRICE_RX = re.compile(r'\$([\d,]+(?:\.\d+)?)')

# Date pattern "DD-Mon-YY"
DATE_RX = re.compile(r'(\d{1,2})-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-(\d{2,4})', re.IGNORECASE)

# Carrier short-codes visible in summaries
CARRIER_KEYWORDS = [
    'EVERGREEN', 'EVER LEADER', 'EVER LEGION', 'ONE LINE', 'ONE ORPHEUS',
    'CMA CGM', 'CMA ', 'WAN HAI', 'HMM', 'MAERSK', 'MSC', 'COSCO', 'ZIM',
    'PRESIDENT JQ ADAMS', 'PRESIDENT', 'OOCL', 'YANG MING', 'HAPAG', 'HYUNDAI'
]

def pick_carrier(text: str) -> str | None:
    U = text.upper()
    # Prefer carrier-line token after vessel: patterns like "$2040 CMA SHANGHAI" or "$450.00 ONE LINE DIRECT"
    # Step 1: find the $price index, carrier is typically right after it.
    m = PRICE_RX.search(text)
    if m:
        after = text[m.end():m.end()+60].upper()
        for kw in ['CMA CGM', 'CMA ', 'ONE LINE', 'EVERGREEN', 'WAN HAI', 'HMM ',
                  'MAERSK', 'MSC ', 'COSCO', 'ZIM', 'OOCL', 'YANG MING', 'HAPAG']:
            if kw in after:
                return kw.strip()
    # Fallback: scan whole string
    for kw in ['CMA CGM', 'CMA ', 'ONE LINE', 'EVERGREEN', 'WAN HAI', 'HMM ',
              'MAERSK', 'MSC ', 'COSCO', 'ZIM', 'OOCL', 'YANG MING', 'HAPAG']:
        if kw in U:
            return kw.strip()
    return None

def parse_rate_table(summary: str) -> dict:
    """Parse the condensed rate row visible in email summary."""
    out = {
        'containers_raw': None,
        'container_count': None,
        'container_size': None,
        'container_equip': None,
        'vessel_voyage': None,
        'erd': None,
        'port_cut': None,
        'etd': None,
        'eta': None,
        'rate_expiry': None,
        'ol_rate': None,
        'carrier_quoted': None,
        'routing_note': None,
        'detention_free': None,
        'demurrage_free': None,
    }
    s = summary or ''

    cm = CONTAINER_RX.search(s)
    if cm:
        if cm.group(1):
            out['container_count'] = int(cm.group(1))
            out['container_size'] = cm.group(2)
            out['container_equip'] = (cm.group(3) or '').strip() or None
        else:
            out['container_count'] = int(cm.group(4))
            out['container_size'] = cm.group(5)
            out['container_equip'] = (cm.group(6) or '').strip() or None
        out['containers_raw'] = cm.group(0)

    # Grab prices
    prices = PRICE_RX.findall(s)
    if prices:
        # Clean comma
        p = prices[0].replace(',', '')
        with contextlib.suppress(ValueError):
            out['ol_rate'] = float(p)

    # Dates — the rate row has ERD, Port Cut, ETD, ETA, Rate Expiry as sequential DD-Mon-YY
    dates = DATE_RX.findall(s)
    if len(dates) >= 5:
        # ERD, PortCut, ETD, ETA, Expiry
        def norm(d):
            return f"{int(d[0]):02d}-{d[1].title()}-20{d[2][-2:]}"
        out['erd']         = norm(dates[0])
        out['port_cut']    = norm(dates[1])
        out['etd']         = norm(dates[2])
        out['eta']         = norm(dates[3])
        out['rate_expiry'] = norm(dates[4])

    out['carrier_quoted'] = pick_carrier(s)

    # Detention / Demurrage free days
    dmd = re.search(r'(\d+)\s*DETENTION\s*\+\s*(\d+)\s*DEMURRAGE', s, re.IGNORECASE)
    if dmd:
        out['detention_free'] = int(dmd.group(1))
        out['demurrage_free'] = int(dmd.group(2))

    # Try to extract vessel name (between container spec and ERD date)
    # Heuristic: capture chars after container to first date
    if out['containers_raw'] and out['erd']:
        after_c = s.split(out['containers_raw'], 1)
        if len(after_c) > 1:
            before_date = DATE_RX.split(after_c[1], 1)[0]
            vessel = before_date.strip().rstrip('-').strip()
            # Trim trailing junk
            if vessel and len(vessel) < 80:
                out['vessel_voyage'] = vessel

    return out


def main() -> int:
    # 1. Collect all unique records from cached tool results
    files = sorted(TOOL_RESULTS_DIR.glob(
        'mcp-1dadfca3-*-outlook_email_search-*.txt'
    ))
    all_records: dict[str, dict] = {}
    for f in files:
        try:
            data = json.loads(f.read_text())
            if isinstance(data, list):
                for d in data:
                    if 'text' in d:
                        rec = json.loads(d['text'])
                        rid = rec.get('id')
                        if rid and rid not in all_records:
                            all_records[rid] = rec
        except Exception as e:
            print(f"[skip] {f.name}: {e}", file=sys.stderr)

    print(f"Loaded {len(all_records)} unique records from {len(files)} cached MCP files")

    # 2. Filter for rate-response pattern
    rate_rsps = []
    for rid, rec in all_records.items():
        subj = rec.get('subject', '') or ''
        m = RATE_RX.match(subj)
        if not m:
            continue
        # Skip if sender isn't the MBD shared mailbox
        sender = (rec.get('sender') or '').lower()
        if 'mbd_oceanexportbookingshared' not in sender:
            continue
        destination = m.group(2).strip()
        # Clean trailing dupe-counters like " (2)"
        destination = re.sub(r'\s*\(\d+\)\s*$', '', destination).strip()
        # Parse table
        parsed = parse_rate_table(rec.get('summary', '') or '')
        rate_rsps.append({
            'bucket': 'mbd_rate_response',
            'id': rid,
            'uri': rec.get('uri'),
            'subject': subj,
            'sender': rec.get('sender'),
            'sent': rec.get('sentDateTime'),
            'received': rec.get('receivedDateTime'),
            'summary_preview': rec.get('summary'),
            'imid': rec.get('internetMessageId', '').strip('<>'),
            'destination': destination,
            'rate_table': parsed,
        })

    rate_rsps.sort(key=lambda r: r.get('sent') or '')
    print(f"Matched {len(rate_rsps)} rate-response rows")

    # 3. Load existing stage, dedupe, append new rows
    existing_ids: set[str] = set()
    existing_rows: list[dict] = []
    if STAGE_PATH.exists():
        for line in STAGE_PATH.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            existing_rows.append(row)
            if row.get('id'):
                existing_ids.add(row['id'])
    print(f"Existing stage rows: {len(existing_rows)} "
          f"(buckets: {dict((b, sum(1 for r in existing_rows if r.get('bucket')==b)) for b in set(r.get('bucket') for r in existing_rows))})")

    new_rows = [r for r in rate_rsps if r['id'] not in existing_ids]
    print(f"New rate-response rows to append: {len(new_rows)}")

    # Write back
    with STAGE_PATH.open('a') as f:
        for r in new_rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    # Diagnostic: print appended rows
    for r in new_rows:
        t = r['rate_table']
        print(f"  {r['sent']}  {r['destination']:<22}  "
              f"{t.get('container_count','?')}x{t.get('container_size','')}{t.get('container_equip') or ''}  "
              f"carrier={t.get('carrier_quoted')}  rate=${t.get('ol_rate')}  etd={t.get('etd')}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
