"""Local prose-compression command; no paid API calls."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--low-risk-prose", action="store_true")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--rate", type=float, default=0.7)
    args = parser.parse_args()
    if args.version:
        print("llmlingua " + importlib.metadata.version("llmlingua"))
        return 0
    if not args.low_risk_prose:
        parser.error("--low-risk-prose is required; never compress code, instructions or shipment evidence")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    from optional_api import compress_notes
    text = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
    result = compress_notes(text, low_risk_prose=True, rate=args.rate)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
