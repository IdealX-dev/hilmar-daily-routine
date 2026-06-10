"""
pdf_llm_rescue.py — Claude-vision fallback for image-only PDFs.

Per Michael 2026-05-19 ("go with task 11 and llm"). pdf_parser.py uses
pdfplumber to extract text; ~3 of every 110 booking PDFs are
image-only scans (no embedded text layer) and pdfplumber returns empty.
This module sends the PDF binary directly to Claude via the messages
API — Claude supports PDF as a `document` content block, no manual
image conversion needed — and asks for the same structured fields
pdf_parser extracts.

WHEN IT FIRES

  Only when pdf_parser._extract_pdf_text(p) returns empty AND the
  caller explicitly opts in via parse_booking_pdf(p, allow_llm=True).
  Daily pipeline (patch_carriers) opts in; one-off test scripts can
  leave it off to avoid surprise API charges.

CACHING

  Results live in data/pdf_llm_cache.json keyed by SHA1 of the PDF
  bytes. Same PDF never costs twice — re-runs are free.

COST

  Each image-only PDF runs through Claude Haiku (cheap model — these
  are structured extractions, not narrative reasoning). At ~$0.001
  per call, the 3 image PDFs we see today cost ~$0.003/day. Even at
  10x bookings/day it's well under $0.10/day.

  HILMAR_PDF_LLM_BUDGET (default 20) caps per-run LLM calls. Beyond
  that the rescue silently no-ops (logs the skip + Sentry metric).

SECURITY

  API key resolution order:
    1. secrets/anthropic-api-key.txt (gitignored)
    2. ANTHROPIC_API_KEY env var
  Never logged. Never echoed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "pdf_llm_cache.json"
DEFAULT_MODEL = "claude-haiku-4-5"
PER_RUN_BUDGET = int(os.environ.get("HILMAR_PDF_LLM_BUDGET", "20"))


def _load_api_key() -> str | None:
    """Resolve ANTHROPIC_API_KEY. Returns None if neither source has it
    — caller treats that as "LLM rescue disabled" and silently no-ops."""
    f = ROOT / "secrets" / "anthropic-api-key.txt"
    if not f.exists():
        f = ROOT.parent / "secrets" / "anthropic-api-key.txt"
    if f.exists():
        try:
            t = f.read_text(encoding="utf-8").strip()
            if t and (t.startswith("sk-ant-") or len(t) > 30):
                return t
        except Exception:
            pass
    return os.environ.get("ANTHROPIC_API_KEY")


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        CACHE_PATH.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("pdf_llm_rescue: cache write failed: %s", e)


_EXTRACTION_PROMPT = """\
You are extracting structured booking-confirmation data from an OL-USA
ocean freight booking PDF. Return ONLY a single JSON object — no prose,
no markdown fences. Use null for fields you cannot determine.

Required fields (use these exact keys):
  mdolx_ref         — the MDOLX booking number (digits only, e.g. "260409"
                       from "BOOKING CONFIRMATION MDOLX260409")
  booking_ref       — the carrier booking reference, often alpha-prefixed
                       (e.g. "RICGH7587500", "NAM8321190", "EBKG16491184")
  carrier_quoted    — steamship carrier name (canonical: "CMA CGM", "MSC",
                       "ONE", "HMM", "Evergreen", "Maersk", "Yang Ming",
                       "Hapag-Lloyd", "OOCL", "COSCO", "ZIM", "Wan Hai")
  vessel_voyage     — vessel name + voyage number (e.g. "ONE OLYMPUS 080W")
  pol               — Port of Loading (city only, title case, e.g. "Oakland")
  pod               — Port of Discharge (city only, e.g. "Tokyo")
  etd_offered       — ETD as ISO date (YYYY-MM-DD)
  eta_offered       — ETA as ISO date (YYYY-MM-DD)
  erd               — Earliest Return Date as ISO date
  doc_cutoff        — Document due date as ISO date
  port_cutoff       — Port terminal closing date as ISO date
  ol_rate           — Per-container OCEAN FREIGHT charge (just the dollar
                       number, no $ or commas, e.g. 285.0)
  container_count   — Total container count (sum if multiple sizes listed)
  containers        — Formatted string like "3-40'HC" or "1-20'DV + 2-40'HC"
  teu_requested     — Total TEU (1 per 20', 2 per 40'/45')
  product           — Commodity from cargo description block (e.g. "Lactose",
                       "Cheese", "Skim Milk Powder", "WPC 80")
  temperature       — Reefer temperature if specified (e.g. "-2C", "34F",
                       "Frozen", "Chilled"); null on dry containers
  origin_free_time  — Free-time text from origin side, if present
                       (e.g. "3 DETENTION + 4 DEMURRAGE FREE DAYS")
  dest_free_time    — Free-time text from destination side
                       (e.g. "14 DETENTION + 14 DEMURRAGE FREE DAYS")
  transshipment     — "DIRECT" / "DIRECT VIA <PORT>" / null if not stated

Output exactly one JSON object. No code fences. Nothing else.\
"""


def _sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def extract_from_pdf(pdf_path: Path, *, model: str | None = None) -> dict | None:
    """Send PDF to Claude vision API and return the extracted dict.

    Returns None when:
      - No API key configured (rescue disabled — silent no-op)
      - anthropic SDK not installed
      - API call fails (logged, doesn't raise)
      - Per-run budget exceeded
      - Response isn't parseable JSON
    """
    p = Path(pdf_path)
    if not p.exists():
        return None
    api_key = _load_api_key()
    if not api_key:
        log.info("pdf_llm_rescue: no ANTHROPIC_API_KEY — silent no-op")
        return None

    try:
        pdf_bytes = p.read_bytes()
    except Exception as e:
        log.warning("pdf_llm_rescue: read failed for %s: %s", p.name, e)
        return None
    digest = _sha1_bytes(pdf_bytes)

    # Cache hit — free
    cache = _load_cache()
    if digest in cache:
        log.info("pdf_llm_rescue: cache hit for %s", p.name[:40])
        return cache[digest].get("data") or None

    # Budget gate
    if not _budget_take():
        log.warning("pdf_llm_rescue: per-run budget exhausted (HILMAR_PDF_LLM_BUDGET="
                    "%d), skipping %s", PER_RUN_BUDGET, p.name[:40])
        return None

    try:
        import anthropic  # type: ignore
    except ImportError:
        log.warning("pdf_llm_rescue: anthropic SDK not installed")
        return None

    client = anthropic.Anthropic(api_key=api_key)
    model = model or DEFAULT_MODEL

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.b64encode(pdf_bytes).decode("ascii"),
                        },
                    },
                    {
                        "type": "text",
                        "text": _EXTRACTION_PROMPT,
                    },
                ],
            }],
        )
    except Exception as e:
        log.warning("pdf_llm_rescue: API call failed for %s: %s", p.name[:40], e)
        return None

    # Parse the response — should be a single JSON object
    text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    if not text_blocks:
        log.warning("pdf_llm_rescue: empty response for %s", p.name[:40])
        return None
    raw = "\n".join(text_blocks).strip()
    # Strip code fences if Claude returned any
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("pdf_llm_rescue: response not JSON for %s: %s\n  raw: %s",
                    p.name[:40], e, raw[:200])
        return None
    if not isinstance(data, dict):
        log.warning("pdf_llm_rescue: response not a dict for %s", p.name[:40])
        return None

    # Drop null values so downstream merge `.get()` behaves
    cleaned = {k: v for k, v in data.items() if v not in (None, "", [])}

    # Cache
    cache[digest] = {
        "data": cleaned,
        "pdf_name": p.name,
        "model": model,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "in_tokens": getattr(resp.usage, "input_tokens", None),
        "out_tokens": getattr(resp.usage, "output_tokens", None),
    }
    _save_cache(cache)
    log.info("pdf_llm_rescue: extracted %d fields from %s (model=%s, in=%d, out=%d)",
             len(cleaned), p.name[:40], model,
             getattr(resp.usage, "input_tokens", 0) or 0,
             getattr(resp.usage, "output_tokens", 0) or 0)
    return cleaned


# Per-run budget state — process-local so daily pipeline gets fresh counter
_budget_remaining = PER_RUN_BUDGET


def _budget_take() -> bool:
    """Decrement remaining budget by 1; return False when exhausted."""
    global _budget_remaining
    if _budget_remaining <= 0:
        return False
    _budget_remaining -= 1
    return True


def reset_budget() -> None:
    """Reset per-run budget. Called at the start of patch_carriers."""
    global _budget_remaining
    _budget_remaining = PER_RUN_BUDGET


if __name__ == "__main__":
    # CLI for manual rescue testing: python pdf_llm_rescue.py <pdf_path>
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: pdf_llm_rescue.py <pdf_path>")
        sys.exit(2)
    out = extract_from_pdf(Path(sys.argv[1]))
    print(json.dumps(out, indent=2) if out else "(no extraction)")
