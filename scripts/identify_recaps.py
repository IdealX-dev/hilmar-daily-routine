#!/usr/bin/env python3
"""Identify each overflow file by subject."""
import json
import os
import sys

for p in sys.argv[1:]:
    try:
        with open(p, encoding="utf-8") as f:
            arr = json.load(f)
        obj = json.loads(arr[0]["text"])
        subj = obj.get("subject", "?")
        size = os.path.getsize(p)
        print(f"{size:>8}  {subj}  ::  {p}")
    except Exception as e:
        print(f"ERR {p}: {e}")
