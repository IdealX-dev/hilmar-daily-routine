"""diag_day.py — a named day went out empty. Where did its mail stop?

2026-08-07. The Aug 6 report showed zero activity in every section. Michael,
asked directly whether Lonny genuinely sent nothing that Wednesday: "he did."

So one link in the chain dropped it, and the chain has five:

    Graph returns it  →  classify() buckets it  →  it lands in stage_emails
    →  fetch_bodies gives it a send time  →  it becomes a dated tracking row

Every previous investigation this week inspected ONE link and inferred the
rest, and inferring was wrong twice: I read two identical result counts and
concluded $search was capping (it was not — a dropped message is stamped
2026-08-07T13:37:44Z), and before that I called the heal fixed while it was
reading field names fetch_bodies does not write. This walks all five links
for one day and prints what each one did, so the answer is read rather than
deduced.

READS ONLY. Pulls state into a temp dir, queries Graph with the SAME two KQL
queries refresh_stage uses, and prints. It never writes the blob, never
mutates stage or tracking data, never emails, and never fetches a body.

PII: prints sender addresses, subjects and timestamps for the target day —
the same fields refresh_stage's drop log already prints unconditionally
(2026-08-07). Never prints message bodies. The Actions log for this private
repo is the only sink.

Usage
    DIAG_DAY=2026-08-06 python3 scripts/diag_day.py
    DIAG_DAY_LOOKBACK=21 DIAG_DAY=2026-08-06 python3 scripts/diag_day.py

DIAG_DAY defaults to the last completed business day. The lookback only has
to reach past the target; it does not change what is printed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _rule(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * max(0, 62 - len(title))}")


def _short(s: str | None, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _target_day(core) -> str:
    """The day under investigation, ET."""
    explicit = os.environ.get("DIAG_DAY", "").strip()
    if explicit:
        return explicit
    from zoneinfo import ZoneInfo
    return core.report_business_day(
        datetime.now(ZoneInfo("America/New_York"))).isoformat()


def _load_jsonl(path: Path) -> list[dict]:
    """stage_emails.txt / stage_emails_bodies.txt — JSON Lines, tolerant.

    A malformed line is COUNTED and skipped rather than fatal: this is a
    diagnostic, and refusing to run because line 900 is truncated would hide
    the 899 lines that answer the question.
    """
    if not path.exists():
        return []
    out, bad = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            bad += 1
    if bad:
        print(f"  WARN {path.name}: {bad} unparseable line(s) skipped")
    return out


def main() -> int:
    import core

    day = _target_day(core)
    lookback = int(os.environ.get("DIAG_DAY_LOOKBACK", "21"))
    print(f"diag_day: target ET day = {day}   (lookback {lookback}d)")

    # ── link 0: the stored state ────────────────────────────────────────────
    _rule("state store")
    import state_store

    tmp = Path(tempfile.mkdtemp())
    try:
        pulled = state_store.pull(root=tmp)
    except Exception as e:
        print(f"pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s): {', '.join(pulled)}")

    stage = _load_jsonl(tmp / "scripts" / "stage_emails.txt")
    bodies = _load_jsonl(tmp / "scripts" / "stage_emails_bodies.txt")
    staged_imids = {r.get("internetMessageId") for r in stage if r.get("internetMessageId")}
    staged_ids = {r.get("id") for r in stage if r.get("id")}
    body_imids = {r.get("internetMessageId") for r in bodies if r.get("internetMessageId")}
    print(f"stage_emails: {len(stage)} records ({len(staged_imids)} with an imid)")
    print(f"bodies:       {len(bodies)} records ({len(body_imids)} with an imid)")

    # ── link 1+2: what Graph returns, and what classify() does with it ──────
    _rule(f"Graph — everything dated {day}")
    import refresh_stage as RS

    token = RS.get_token()
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback)
    queries = [
        ("lonny-flow", f"from:{RS.LONNY_EMAIL} OR to:{RS.LONNY_EMAIL}"),
        ("hilmar-bookings", f"from:{RS.MBD_BOOKING_EMAIL} AND subject:HILMAR"),
    ]
    items: dict[str, dict] = {}
    for label, kql in queries:
        got = RS.search_messages(token, kql, max_results=500)
        print(f"query {label!r}: {len(got)} results")
        for it in got:
            items.setdefault(it.get("internetMessageId") or it["id"], it)
    print(f"unique across queries: {len(items)}")

    # Oldest/newest prove whether the window even reaches the target day —
    # the check I skipped when I wrongly blamed $search for capping.
    stamps = sorted(
        s for s in ((it.get("receivedDateTime") or it.get("sentDateTime")) for it in items.values()) if s
    )
    if stamps:
        print(f"received range: {stamps[0]} … {stamps[-1]}")
    in_window = sum(1 for it in items.values()
                    if (RS.parse_iso(it.get("receivedDateTime"))
                        or RS.parse_iso(it.get("sentDateTime")) or cutoff) >= cutoff)
    print(f"inside the {lookback}d cutoff: {in_window}")

    on_day = []
    for it in items.values():
        ts = it.get("receivedDateTime") or it.get("sentDateTime")
        if core.et_date_of(ts) == day:
            on_day.append(it)
    on_day.sort(key=lambda i: i.get("receivedDateTime") or i.get("sentDateTime") or "")

    if not on_day:
        print(f"\n>>> Graph returned NOTHING dated {day}. The loss is at INTAKE.")
    else:
        print(f"\n{len(on_day)} message(s) dated {day}:\n")
        print(f"  {'bucket':<20} {'staged':<7} {'body':<5} sender / subject")
        for it in on_day:
            sender = ((it.get("from") or {}).get("emailAddress") or {}).get("address") or "<none>"
            imid = it.get("internetMessageId")
            bucket = RS.classify(it) or "DROPPED"
            excl = sender.lower() in {s.lower() for s in RS.EXCLUDED_SENDERS}
            if bucket == "DROPPED" and excl:
                bucket = "excluded"
            staged = "yes" if (imid in staged_imids or it.get("id") in staged_ids) else "NO"
            has_body = "yes" if imid in body_imids else "NO"
            ts = it.get("receivedDateTime") or it.get("sentDateTime") or "?"
            print(f"  {bucket:<20} {staged:<7} {has_body:<5} {sender}")
            print(f"  {'':<20} {'':<7} {'':<5} {ts}  {_short(it.get('subject'), 90)}")

    # ── link 4+5: what the tracking data holds for that day ─────────────────
    _rule(f"tracking-data-v2.json — rows dated {day}")
    data_path = tmp / "tracking-data-v2.json"
    if not data_path.exists():
        print("tracking-data-v2.json not in the store")
        return 2
    data = json.loads(data_path.read_text(encoding="utf-8"))
    rows = data.get("requests", [])
    print(f"{len(rows)} requests total")

    req_on_day = [r for r in rows
                  if core.et_date_of(r.get("request_date") or r.get("request_timestamp")) == day]
    resp_on_day = [r for r in rows if core.et_date_of(r.get("response_timestamp")) == day]
    print(f"request_date/timestamp on {day}: {len(req_on_day)}")
    print(f"response_timestamp on {day}:     {len(resp_on_day)}")
    for r in req_on_day[:25]:
        print(f"  REQ  {r.get('request_id', '?'):<22} {r.get('status', '?'):<8} "
              f"{_short(r.get('origin'), 18)} → {_short(r.get('destination'), 24)}")
    for r in resp_on_day[:25]:
        print(f"  RESP {r.get('request_id', '?'):<22} {r.get('status', '?'):<8} "
              f"carrier={_short(r.get('carrier_quoted'), 16)} rate={r.get('ol_rate')}")

    # Undated rows are the failure mode that looks EXACTLY like a quiet day:
    # the row exists, the KPI totals count it, and no day bucket shows it.
    # Reason via qc_selfheal._undated_reason so this reads the SAME verdict
    # QC-077 does — a second opinion here would be a second bug waiting.
    undated = [r for r in rows
               if not core.et_date_of(r.get("request_date") or r.get("request_timestamp"))]
    print(f"\nrows with NO usable request date: {len(undated)}")
    if undated:
        from collections import Counter

        import qc_selfheal as QC
        bodies_idx = {b.get("internetMessageId"): b for b in bodies
                      if b.get("internetMessageId")}
        why = Counter(QC._undated_reason(r, bodies_idx) for r in undated)
        for reason, n in why.most_common():
            print(f"  {reason:<14} {n}")
        for r in undated[:15]:
            print(f"  UNDATED {str(r.get('request_id', '?')):<22} "
                  f"status={str(r.get('status', '?')):<8} "
                  f"{QC._undated_reason(r, bodies_idx)}")

    _rule("reading this")
    print("Graph shows nothing for the day      → intake. Widen or fix the query.")
    print("Shown but bucket=DROPPED             → classify(). Add the sender.")
    print("Bucketed but staged=NO               → the stage write or the dedupe.")
    print("Staged but body=NO                   → fetch_bodies; the row cannot be dated.")
    print("Staged with a body, no row on the day → merge/parse, or the date heal.")
    print("Row exists but appears under UNDATED  → it counts in totals and in no day.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
