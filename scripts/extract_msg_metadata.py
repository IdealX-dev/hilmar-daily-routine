#!/usr/bin/env python3
"""
extract_msg_metadata.py — parse raw read_resource output files into compact metadata.

Input:  A raw JSON file from Outlook MCP `read_resource` call (either inline dict or [{"type":"text","text":"{...}"}])
Output: Compact {conversationId, id, subject, sender, receivedDateTime, sentDateTime,
                 body_first_500, bodyPreview, internetMessageId, body_html}

Usage:
    python3 scripts/extract_msg_metadata.py <raw.json>       # prints compact JSON
    python3 scripts/extract_msg_metadata.py <raw.json> --save <out.json>

Designed for batch pipeline in build_ops_flow_v2.py — extracts only the fields
needed for pairing/classification, discards the heavyweight body HTML.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path
from html.parser import HTMLParser


class TextStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out = []
    def handle_data(self, data):
        self.out.append(data)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    p = TextStripper()
    try:
        p.feed(html)
    except Exception:
        pass
    txt = "".join(p.out)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def extract(path: str | Path) -> dict:
    p = Path(path)
    raw = p.read_text()
    try:
        obj = json.loads(raw)
    except Exception as e:
        return {"error": f"JSON parse fail: {e}", "path": str(p)}
    # overflow files are [{"type":"text","text":"{...}"}, ...] — take first
    if isinstance(obj, list):
        obj = json.loads(obj[0].get("text", "{}"))
    body_html = (obj.get("body") or {}).get("content", "") or ""
    body_text = html_to_text(body_html)
    return {
        "id": obj.get("id"),
        "conversationId": obj.get("conversationId"),
        "internetMessageId": obj.get("internetMessageId"),
        "subject": obj.get("subject", "") or "",
        "sender": (obj.get("sender") or {}).get("address", ""),
        "from_address": ((obj.get("from") or {}).get("emailAddress") or {}).get("address", "")
            if isinstance(obj.get("from"), dict) else "",
        "toRecipients": [
            ((r.get("emailAddress") or {}).get("address", ""))
            for r in (obj.get("toRecipients") or [])
        ],
        "receivedDateTime": obj.get("receivedDateTime"),
        "sentDateTime": obj.get("sentDateTime"),
        "bodyPreview": obj.get("bodyPreview", ""),
        "body_first_500": body_text[:500],
        "body_text_len": len(body_text),
        "body_html_len": len(body_html),
        # Keep body_html if caller wants to run OL options parser later — optional
    }


def main():
    if len(sys.argv) < 2:
        print("usage: extract_msg_metadata.py <raw.json> [--save <out.json>]", file=sys.stderr)
        sys.exit(1)
    path = sys.argv[1]
    meta = extract(path)
    if "--save" in sys.argv:
        out = sys.argv[sys.argv.index("--save") + 1]
        Path(out).write_text(json.dumps(meta, indent=2, default=str))
        print(f"[saved] {out}")
    else:
        print(json.dumps(meta, indent=2, default=str))


if __name__ == "__main__":
    main()
