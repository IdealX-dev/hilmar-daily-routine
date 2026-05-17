"""
hilmar.feedback_ingest — process insight feedback emails into a log.

Loop:
  * The daily insights HTML embeds three mailto: buttons per insight
    bullet — 👍 / 👎 / 💤. Each pre-fills:
        Subject: INSIGHT-FEEDBACK <id> 👍
        To:      HILMAR_INSIGHTS_FEEDBACK_TO  (defaults to Michael's
                 idealx address; configurable via env)
  * Michael clicks → his mail client sends.
  * The next ingest pass (or a scheduled scan) fetches mail to the
    feedback inbox via :class:`hilmar.graph_client.GraphClient`,
    parses the subject, and appends a record to
    ``data/insights-feedback.json``.

  * The next insights run uses :func:`load_feedback_summary` to
    summarise recent feedback into a string, which is passed to
    :func:`hilmar.insights.generate_narrative` as ``feedback_summary``.

This module is FORMAT-tolerant — the subject regex accepts variations
like "INSIGHT-FEEDBACK abc123 👍" / "INSIGHT FEEDBACK abc123 thumbs up"
/ etc.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)


SUBJECT_RX = re.compile(
    r"INSIGHT[\s_-]*FEEDBACK\s+(?P<id>[A-Za-z0-9_.\-]+)\s+(?P<rating>.+?)\s*$",
    re.IGNORECASE,
)


RATING_NORMALISERS: dict[str, str] = {
    "👍": "up", "thumbs up": "up", "thumbs-up": "up", "up": "up",
    "yes": "up", "good": "up", "helpful": "up", "+1": "up",

    "👎": "down", "thumbs down": "down", "thumbs-down": "down", "down": "down",
    "no": "down", "bad": "down", "not helpful": "down", "-1": "down",

    "💤": "noise", "noise": "noise", "skip": "noise", "ignore": "noise",
    "z": "noise", "zzz": "noise",
}


def normalise_rating(raw: str) -> str | None:
    """Map a free-form rating string to ``up`` / ``down`` / ``noise``.
    Returns ``None`` if it doesn't match any known token."""
    s = (raw or "").strip().lower()
    if s in RATING_NORMALISERS:
        return RATING_NORMALISERS[s]
    # Try first emoji or word.
    head = s.split()[0] if s else ""
    if head in RATING_NORMALISERS:
        return RATING_NORMALISERS[head]
    return None


def parse_subject(subject: str) -> tuple[str, str] | None:
    """Pull (insight_id, rating) from the subject. Returns ``None`` if
    the subject doesn't fit the INSIGHT-FEEDBACK pattern."""
    if not subject:
        return None
    m = SUBJECT_RX.search(subject)
    if not m:
        return None
    insight_id = m.group("id").strip()
    rating = normalise_rating(m.group("rating"))
    if rating is None:
        return None
    return insight_id, rating


# ─────────────────────────────────────────────────────────────────────
# Records + persistence
# ─────────────────────────────────────────────────────────────────────


@dataclass
class FeedbackRecord:
    insight_id: str
    rating: str          # "up" / "down" / "noise"
    received_at: str     # ISO8601 UTC
    section: str | None = None  # "system" / "design" / "data" / "business"
    raw_subject: str | None = None
    raw_from: str | None = None


def load_log(path: Path) -> list[FeedbackRecord]:
    """Read existing records. Empty list if missing/corrupt."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("insights-feedback file %s did not parse — starting fresh", path)
        return []
    out: list[FeedbackRecord] = []
    for item in raw or []:
        try:
            out.append(FeedbackRecord(**item))
        except TypeError:
            continue
    return out


def save_log(records: list[FeedbackRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(r) for r in records], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def upsert(
    records: list[FeedbackRecord],
    new_record: FeedbackRecord,
) -> list[FeedbackRecord]:
    """Idempotent append: replaces an existing (insight_id, raw_subject)
    pair so re-ingesting the same email doesn't duplicate.
    """
    key = (new_record.insight_id, new_record.raw_subject or "")
    out = [r for r in records if (r.insight_id, r.raw_subject or "") != key]
    out.append(new_record)
    return out


# ─────────────────────────────────────────────────────────────────────
# Fetch + ingest from a Graph mailbox
# ─────────────────────────────────────────────────────────────────────


def feedback_to_address() -> str:
    return os.environ.get(
        "HILMAR_INSIGHTS_FEEDBACK_TO", "michael.deitchman@idealx.us",
    )


def ingest_from_graph(
    *,
    client,                         # GraphClient (untyped here to avoid circular import)
    log_path: Path,
    after: datetime | None = None,
) -> int:
    """Fetch INSIGHT-FEEDBACK mail since ``after`` (default: 30 days)
    and merge into ``log_path``. Returns the number of NEW records
    added (idempotent — re-running won't grow the log).

    The recipient filter is the env-configured feedback inbox. The
    fetch itself is delegated to ``client.search_messages``; only the
    parsing is owned here.
    """
    after = after or (datetime.now(timezone.utc) - timedelta(days=30))
    inbox = feedback_to_address()
    metas = client.search_messages(recipient=inbox, after=after)

    existing = load_log(log_path)
    seen_keys = {(r.insight_id, r.raw_subject or "") for r in existing}
    added = 0
    for meta in metas:
        parsed = parse_subject(meta.subject or "")
        if parsed is None:
            continue
        insight_id, rating = parsed
        key = (insight_id, meta.subject or "")
        if key in seen_keys:
            continue
        rec = FeedbackRecord(
            insight_id=insight_id,
            rating=rating,
            received_at=meta.received_at.isoformat() if meta.received_at else "",
            section=infer_section_from_id(insight_id),
            raw_subject=meta.subject,
            raw_from=meta.from_address,
        )
        existing = upsert(existing, rec)
        seen_keys.add(key)
        added += 1
    if added:
        save_log(existing, log_path)
    return added


# ─────────────────────────────────────────────────────────────────────
# Insight ID helpers
# ─────────────────────────────────────────────────────────────────────


def make_insight_id(*, date: str, section: str, idx: int) -> str:
    """Stable ID for a bullet. ``date`` is YYYY-MM-DD; ``section`` is one
    of system/design/data/business; ``idx`` is the bullet's 1-based index.
    """
    return f"{date}.{section}.{idx}"


def infer_section_from_id(insight_id: str) -> str | None:
    parts = insight_id.split(".")
    if len(parts) < 2:
        return None
    candidate = parts[1].lower()
    if candidate in ("system", "design", "data", "business"):
        return candidate
    return None


# ─────────────────────────────────────────────────────────────────────
# Mailto button HTML — used by render.py / insights.py
# ─────────────────────────────────────────────────────────────────────


def feedback_button_html(insight_id: str, rating_label: str, rating_emoji: str) -> str:
    to_addr = feedback_to_address()
    subj = f"INSIGHT-FEEDBACK {insight_id} {rating_emoji}"
    href = f"mailto:{to_addr}?subject={subj.replace(' ', '%20')}"
    return (
        f"<a href='{href}' style='text-decoration: none; "
        f"display: inline-block; padding: 2px 8px; margin-right: 4px; "
        f"font-size: 11px; background: #f1f5f9; border-radius: 10px; "
        f"color: #334155;' title='{rating_label}'>{rating_emoji}</a>"
    )


def insights_feedback_strip(insight_id: str) -> str:
    """Render the three-button strip (👍 / 👎 / 💤) for one insight bullet."""
    return (
        feedback_button_html(insight_id, "Helpful",     "👍")
        + feedback_button_html(insight_id, "Not helpful", "👎")
        + feedback_button_html(insight_id, "Noise",      "💤")
    )


# ─────────────────────────────────────────────────────────────────────
# Summarisation for the next-run prompt
# ─────────────────────────────────────────────────────────────────────


def load_feedback_summary(
    log_path: Path,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> str:
    """Return a SHORT plain-text summary of the last ``days`` of feedback,
    suitable to inject into the next run's LLM prompts.

    Format: counts per (section, rating) + the most recent 5 bullet IDs
    that scored 'down' or 'noise' so the model knows what to avoid.
    """
    records = load_log(log_path)
    if not records:
        return ""

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    recent: list[FeedbackRecord] = []
    for r in records:
        try:
            ts = datetime.fromisoformat(r.received_at.rstrip("Z"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts >= cutoff:
            recent.append(r)
    if not recent:
        return ""

    counter: Counter[tuple[str, str]] = Counter()
    for r in recent:
        counter[(r.section or "unknown", r.rating)] += 1

    sections = sorted({s for (s, _) in counter})
    lines = [f"Last {days} days: {len(recent)} ratings."]
    for sec in sections:
        up = counter.get((sec, "up"), 0)
        down = counter.get((sec, "down"), 0)
        noise = counter.get((sec, "noise"), 0)
        lines.append(f"- {sec}: {up} 👍 / {down} 👎 / {noise} 💤")

    bad_ids = [
        r.insight_id for r in sorted(recent, key=lambda r: r.received_at, reverse=True)
        if r.rating in ("down", "noise")
    ][:5]
    if bad_ids:
        lines.append(f"Recent ids to AVOID similar bullets: {', '.join(bad_ids)}.")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# CLI helper for manual ingestion (dev-time)
# ─────────────────────────────────────────────────────────────────────


def main() -> int:
    """Manual ingest entrypoint — useful for one-off testing on the VM."""
    import argparse

    from .graph_client import GraphClient

    ap = argparse.ArgumentParser(description="Ingest INSIGHT-FEEDBACK emails.")
    ap.add_argument("--log", type=Path, required=True,
                    help="data/insights-feedback.json path")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    client = GraphClient()
    client.authenticate(interactive_ok=False)
    after = datetime.now(timezone.utc) - timedelta(days=args.days)
    n = ingest_from_graph(client=client, log_path=args.log, after=after)
    print(f"Ingested {n} new feedback records → {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def _ingest_records_for_test(records: Iterable[FeedbackRecord]) -> list[FeedbackRecord]:
    """Test helper — used to seed fixtures without going through Graph."""
    return list(records)
