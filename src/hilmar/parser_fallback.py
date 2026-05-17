"""LLM-fallback parser layer — "infallible and learning" per Michael 2026-04-28.

Architecture:
  1. Regex parsers in :mod:`hilmar.body_parser` run first as today.
  2. When a regex returns ``None`` on a field this module knows about,
     :func:`extract_with_fallback` calls the LLM via
     :class:`hilmar.model_router.ModelRouter` (task_type
     ``parser_extraction``, defaults to Haiku for cost) to extract the
     field from the email body. Returns a JSON-shaped result with a
     confidence label.
  3. Results are cached in ``data/parser_cache.json`` keyed by
     ``sha1(field + body[:1000])`` so the same email body never costs
     twice — re-runs are free.
  4. Every fallback call (including budget-skipped ones) appends to
     ``data/parser_misses.jsonl`` — append-only NDJSON. This is the
     test-fixture queue: each line is a future regex pattern + test
     case waiting to be promoted from "LLM crutch" to "regex covered".
  5. Per-run budget cap (``HILMAR_PARSER_FALLBACK_BUDGET``, default 20)
     prevents runaway LLM cost on a bad-data day. Beyond budget, the
     fallback is a no-op (returns regex result, logs the skip).

The "learning" half is the miss log + cache: every miss is captured
permanently with the email body excerpt and the LLM's extraction.
That corpus seeds future regex improvements, test fixtures, and (when
the user 👍s an LLM extraction via the existing feedback loop) becomes
training context for next-run prompts.

Wiring: callers (e.g. ``ingest.apply_rate_responses``) pass the regex
result + body + a ``ParserFallbackContext`` so the fallback can share
one cache file + one budget across a daily run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model_router import ModelRouter, ModelRouterError

log = logging.getLogger(__name__)

# Fields where LLM fallback is enabled. Adding a field here is the
# concrete way to "extend" the fallback layer — no code changes needed
# beyond the prompt template below. Conservative starting set: the
# fields that visibly degrade the daily output when missed.
FALLBACK_FIELDS = frozenset({
    "ol_rate",        # rate value (e.g. "$540" or "$3,400/40RF")
    "carrier_quoted", # carrier name (e.g. "MSC", "CMA CGM")
    "etd_offered",    # offered ETD date (ISO date string)
    "eta_offered",    # offered ETA date
    "vessel_voyage",  # vessel + voyage (e.g. "MSC OSCAR / 012E")
    "transshipment",  # T/S port or "Direct"
    # Individual person at MBD shared mailbox who composed the rate
    # response. ``from_name`` carries this when Outlook send-as
    # populates it; body-signature parser handles the rest; this LLM
    # fallback covers the long tail of email signature formats.
    "ol_responder_signer",
})

# Field-specific extraction prompts. Kept compact so the LLM has a
# fixed shape to fill in and we can parse the response cheaply.
_FIELD_PROMPTS: dict[str, str] = {
    "ol_rate": (
        "Extract the ocean-freight rate quoted by OL-USA in the message below. "
        "Look for dollar amounts in the body, often labeled as 'rate', 'all-in', "
        "or appearing next to a container size (e.g. '$540 per 20DV'). Return "
        "the numeric value (no '$' or units) or null if no rate is present."
    ),
    "carrier_quoted": (
        "Extract the steamship carrier name OL-USA is quoting in the message "
        "below (e.g. 'MSC', 'CMA CGM', 'Maersk', 'ONE', 'ZIM'). Return the "
        "carrier name as a string, or null if no carrier is mentioned."
    ),
    "etd_offered": (
        "Extract the ETD (estimated time of departure) date OL-USA is offering "
        "in the message below. Return ISO 8601 date format (YYYY-MM-DD), or "
        "null if no ETD is present."
    ),
    "eta_offered": (
        "Extract the ETA (estimated time of arrival) date OL-USA is offering "
        "in the message below. Return ISO 8601 date format (YYYY-MM-DD), or "
        "null if no ETA is present."
    ),
    "vessel_voyage": (
        "Extract the vessel name and voyage number from the message below "
        "(e.g. 'MSC OSCAR / 012E', 'CMA CGM MARCO POLO 0HXC1W1MA'). Return "
        "the combined string, or null if no vessel is identified."
    ),
    "transshipment": (
        "Extract the transshipment information from the message below — "
        "either a T/S port name (e.g. 'via Singapore') or 'Direct' if the "
        "voyage is direct. Return the value as a string, or null if not "
        "specified."
    ),
    "ol_responder_signer": (
        "Extract the name of the individual person at OL-USA who composed "
        "this rate-response email — the human signer, NOT the shared "
        "mailbox name 'MBD Ocean Export Booking'. Look at the closing "
        "block (e.g. 'Best, Caren Tobel'). Return the person's full name "
        "as 'First Last', or null if no individual signature is present "
        "(e.g. unsigned auto-generated mail)."
    ),
}

_SYSTEM_PROMPT = (
    "You are a strict information-extraction system for shipping rate-desk "
    "emails. Always respond with a JSON object exactly matching the schema:\n"
    '  {"value": <extracted-or-null>, "confidence": "high"|"medium"|"low"}\n'
    'Use "high" when the field is unambiguous, "medium" when you had to '
    'choose between candidates, "low" when you guessed. Never invent values '
    "not present in the email. If the field is genuinely absent, return "
    'value: null.'
)


@dataclass
class ParserFallbackContext:
    """Per-run state shared across all fallback calls.

    One ``ParserFallbackContext`` per daily run; pass it to every
    :func:`extract_with_fallback` invocation. Tracks the cache, the
    miss log, and the remaining budget so a single bad-data day can't
    spend the LLM budget into next week.
    """
    cache_path: Path
    miss_log_path: Path
    budget: int = 20
    router: ModelRouter | None = None
    calls_made: int = 0
    skips: int = 0
    cache: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_data_dir(cls, data_dir: Path, *, router: ModelRouter | None = None) -> ParserFallbackContext:
        """Default wiring for production: cache + miss log live alongside
        tracking-data-v2.json. Budget honors HILMAR_PARSER_FALLBACK_BUDGET
        (default 20)."""
        budget = int(os.environ.get("HILMAR_PARSER_FALLBACK_BUDGET", "20"))
        cache_path = data_dir / "parser_cache.json"
        miss_log_path = data_dir / "parser_misses.jsonl"
        cache: dict[str, Any] = {}
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log.warning("parser_cache.json unreadable (%s) — starting fresh", e)
        return cls(
            cache_path=cache_path,
            miss_log_path=miss_log_path,
            budget=budget,
            router=router,
            cache=cache,
        )

    def persist_cache(self) -> None:
        """Write the in-memory cache back to disk. Safe to call multiple
        times per run; idempotent if no new entries."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.cache, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def append_miss(self, record: dict[str, Any]) -> None:
        self.miss_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.miss_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _cache_key(field: str, body: str) -> str:
    """Stable cache key — field + first 1000 chars of body. Different
    bodies produce different keys; same body, same key (so re-runs are
    free)."""
    h = hashlib.sha1()
    h.update(field.encode("utf-8"))
    h.update(b"\x00")
    h.update(body[:1000].encode("utf-8", errors="replace"))
    return h.hexdigest()


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    """LLMs occasionally wrap JSON in code fences or prose. Strip those
    and try to find the first {...} block. Returns None on any failure
    so the caller can fall through cleanly."""
    if not text:
        return None
    # Strip markdown code fences if present.
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    # Find the first {...} block — LLMs sometimes prepend a sentence.
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            return None
        return parsed
    except json.JSONDecodeError:
        return None


def extract_with_fallback(
    field: str,
    body: str,
    regex_value: Any,
    *,
    ctx: ParserFallbackContext,
) -> Any:
    """Return ``regex_value`` if non-empty; otherwise consult the LLM
    fallback (cache → router → miss log).

    Contract:
      * If ``regex_value`` is non-empty (truthy + not the string "null"),
        returned as-is. No LLM call, no cache hit, free.
      * If ``field`` is not in :data:`FALLBACK_FIELDS`, regex_value
        returned even when None. Avoids accidental LLM use on fields
        we haven't vetted prompts for.
      * If a cache entry exists for (field, body[:1000]), use it.
        Cache entries from prior runs persist forever.
      * Else if budget remains, call the LLM, cache the result, log to
        misses, and return.
      * Else log the skip and return regex_value (None).

    Failures inside the LLM call NEVER raise — the function falls back
    to the regex value. Daily run must not die on a parser fallback
    error.
    """
    if regex_value not in (None, "", "null"):
        return regex_value
    if field not in FALLBACK_FIELDS:
        return regex_value
    if not body:
        return regex_value

    key = _cache_key(field, body)
    cached = ctx.cache.get(key)
    if cached is not None:
        # Cache hit — still log to misses so the corpus reflects every
        # miss (deduped in fixture-promotion downstream).
        return cached.get("value")

    if ctx.calls_made >= ctx.budget:
        ctx.skips += 1
        ctx.append_miss({
            "ts": datetime.now(timezone.utc).isoformat(),
            "field": field,
            "body_excerpt": body[:500],
            "result": "skipped_budget",
            "budget": ctx.budget,
        })
        return regex_value

    if ctx.router is None:
        try:
            ctx.router = ModelRouter()
        except Exception as e:  # noqa: BLE001
            log.warning("ModelRouter init failed in parser fallback (%s) — skipping", e)
            return regex_value

    prompt = (
        f"{_FIELD_PROMPTS[field]}\n\n"
        f"=== EMAIL BODY ===\n{body[:2500]}\n=== END ===\n\n"
        'Respond with JSON only: {"value": ..., "confidence": ...}'
    )

    try:
        ctx.calls_made += 1
        resp = ctx.router.call(
            task_type="parser_extraction",
            prompt=prompt,
            system=_SYSTEM_PROMPT,
            max_tokens=200,
        )
    except (ModelRouterError, Exception) as e:  # noqa: BLE001
        log.warning("parser_extraction LLM call failed (%s) — falling back to regex", e)
        ctx.append_miss({
            "ts": datetime.now(timezone.utc).isoformat(),
            "field": field,
            "body_excerpt": body[:500],
            "result": "llm_error",
            "error": str(e),
        })
        return regex_value

    parsed = _parse_llm_json(resp.text or "")
    extracted = (parsed or {}).get("value")
    confidence = (parsed or {}).get("confidence", "low")

    # Don't cache "low" confidence — gives the next run a chance to
    # extract more strongly when context shifts.
    if confidence in ("high", "medium"):
        ctx.cache[key] = {
            "value": extracted,
            "confidence": confidence,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }

    ctx.append_miss({
        "ts": datetime.now(timezone.utc).isoformat(),
        "field": field,
        "body_excerpt": body[:500],
        "result": "llm_extracted",
        "value": extracted,
        "confidence": confidence,
        "model": resp.model,
        "cost_cents": resp.cost_cents,
    })
    return extracted if extracted is not None else regex_value
