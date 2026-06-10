#!/usr/bin/env python3
"""Debug helper: dump every table (headers + first row) found in a recap overflow file."""
import sys

sys.path.insert(0, "/sessions/brave-sharp-davinci/mnt/PROJECT HILMAR/scripts")
from extract_hilmar_recaps import TableParser, find_header_row, load_email_body

for path in sys.argv[1:]:
    msg_id, subj, html = load_email_body(path)
    print(f"\n=== {subj} ===")
    p = TableParser()
    p.feed(html)
    print(f"  tables: {len(p.tables)}")
    for ti, t in enumerate(p.tables):
        if not t:
            continue
        hdr = find_header_row(t)
        print(f"  table[{ti}] rows={len(t)} hdr_idx={hdr}")
        show_rows = t[:min(4, len(t))]
        for ri, r in enumerate(show_rows):
            print(f"    r{ri}: {r}")
