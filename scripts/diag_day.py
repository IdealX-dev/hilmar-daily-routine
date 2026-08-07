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

READ-ONLY WHERE IT COUNTS, and precisely: it never writes the BLOB (no push,
no backup, no restore), never sends mail, never fetches a body, and never
edits stage or tracking data. It DOES write the runner's working tree, twice
and unavoidably — `state_store.pull` lands the state files there, and MSAL
rewrites secrets/token-cache.bin on a silent refresh.

That is why it pulls into the repo root rather than a temp dir, which the
first version did and which failed: this tenant has no app-only Entra app
registered (OL IT declined — see state_store.STATE_FILES), so GRAPH_APP_* is
empty and Graph auth falls back to the delegated MSAL cache at
secrets/token-cache.bin. outlook_send resolves that path from module
constants, so a temp dir is invisible to it. Monkeypatching those constants
from a diagnostic would be a private copy of the pipeline's auth, which is
the one thing this file must not have.

CONSEQUENCE, stated plainly: run this on a checkout you do not mind having
overwritten with the store's copy of state. On a runner that is a fresh
clone. On the Cloud PC it is the same overwrite `state_store.py pull` does
before every fire, but do not run it over uncommitted local edits.

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


def _filter_day(RS, token: str, day: str) -> list[dict]:
    """Every message in the mailbox for one ET day, via $filter — NOT $search.

    This is the control. $search is relevance-ranked and its completeness is
    exactly what is in question; $filter on receivedDateTime is an ordered
    range scan and returns everything in the window. Run both over the same
    day and the difference is the intake gap, measured instead of argued.

    The window is ET midnight to ET midnight converted to UTC, because the
    report's day boundary is ET — using a UTC day would shift the edges by
    four hours and put evening mail on the wrong side.
    """
    from datetime import date as _date
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    d = _date.fromisoformat(day)
    lo = datetime(d.year, d.month, d.day, tzinfo=et).astimezone(timezone.utc)
    hi = lo + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    params = {
        "$filter": (f"receivedDateTime ge {lo.strftime(fmt)} and "
                    f"receivedDateTime lt {hi.strftime(fmt)}"),
        "$select": RS.GRAPH_SELECT,
        "$orderby": "receivedDateTime desc",
        "$top": "50",
    }
    print(f"$filter window (UTC): {lo.strftime(fmt)} … {hi.strftime(fmt)}")

    out: list[dict] = []
    url = f"{RS._mailbox_base}/messages"
    while url and len(out) < 500:
        data = RS.graph_get(token, url, params=params)
        out.extend(data.get("value") or [])
        url = data.get("@odata.nextLink")
        params = None  # nextLink already carries the query
    return out


def main() -> int:
    import core

    day = _target_day(core)
    lookback = int(os.environ.get("DIAG_DAY_LOOKBACK", "21"))
    print(f"diag_day: target ET day = {day}   (lookback {lookback}d)")

    # ── link 0: the stored state ────────────────────────────────────────────
    _rule("state store")
    import state_store

    # Into the REPO ROOT, not a temp dir — the delegated MSAL cache has to
    # land at secrets/token-cache.bin for Graph auth to work at all. See the
    # module docstring; the temp-dir version of this failed on exactly that.
    root = ROOT
    print(f"pulling state into {root} (overwrites local copies)")
    try:
        pulled = state_store.pull(root=root)
    except Exception as e:
        print(f"pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s): {', '.join(pulled)}")

    # Imported AFTER the pull so its module-level STAGE_PATH/_resolve() see the
    # files that were just pulled.
    import qc_selfheal as QC
    import refresh_stage as RS

    # The pipeline's OWN loaders. The first version of this file read
    # "internetMessageId" off the stage records and reported `0 with an imid`
    # across 1273 of them — build_stage_record writes `imid`. A private copy of
    # a field name is the same defect as a private copy of classify(), and it
    # made the staged/body columns read NO for every message on earth.
    staged_ids, staged_imids = RS.load_existing_stage_keys()
    body_imids = RS.load_existing_body_imids()
    bodies_idx = QC._load_bodies_index()
    print(f"stage_emails: {len(staged_ids)} ids, {len(staged_imids)} imids")
    print(f"bodies:       {len(body_imids)} imids")

    # ── link 1+2: what Graph returns, and what classify() does with it ──────
    _rule(f"Graph — everything dated {day}")

    token = RS.get_token()
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback)
    queries = [
        ("lonny-flow", f"from:{RS.LONNY_EMAIL} OR to:{RS.LONNY_EMAIL}"),
        ("hilmar-bookings", f"from:{RS.MBD_BOOKING_EMAIL} AND subject:HILMAR"),
    ]
    items: dict[str, dict] = {}
    per_query: dict[str, set[str]] = {}
    for label, kql in queries:
        got = RS.search_messages(token, kql, max_results=500)
        print(f"query {label!r}: {len(got)} results")
        per_query[label] = {it.get("internetMessageId") or it["id"] for it in got}
        for it in got:
            items.setdefault(it.get("internetMessageId") or it["id"], it)
    print(f"unique across queries: {len(items)}")

    # Two unrelated queries returning the SAME count is the signature of a cap
    # or of $search ignoring the predicate, and the overlap tells them apart:
    # a real from:/to: filter and a real subject: filter should share only the
    # booking mail Lonny is copied on, not most of their results.
    if len(per_query) == 2:
        (la, sa), (lb, sb) = per_query.items()
        both = sa & sb
        print(f"overlap {la} ∩ {lb}: {len(both)}  "
              f"({la}-only {len(sa - sb)}, {lb}-only {len(sb - sa)})")
        if sa == sb:
            print(">>> the two queries returned IDENTICAL result sets — $search "
                  "is not honouring the predicates")

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

    # A per-day histogram, because "1 message on Aug 6" means nothing without
    # the neighbouring days. If every recent weekday is 1-2 and the volume all
    # sits in May, the result set is capped and sorted by relevance rather than
    # by date; if Aug 6 alone is empty, the day really was quiet.
    from collections import Counter

    by_day: Counter = Counter()
    for it in items.values():
        d = core.et_date_of(it.get("receivedDateTime") or it.get("sentDateTime"))
        if d:
            by_day[d] += 1
    print("\nmessages per ET day, most recent 21:")
    for d in sorted(by_day, reverse=True)[:21]:
        mark = "  <- target" if d == day else ""
        print(f"  {d}  {by_day[d]:>3}  {'█' * min(by_day[d], 40)}{mark}")
    if day not in by_day:
        print(f"  {day}    0  <- target (no messages at all)")

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

    # ── the control: the same day via $filter, which is not relevance-ranked ─
    _rule(f"$filter control — the whole mailbox for {day}")
    search_imids = {it.get("internetMessageId") for it in on_day}
    try:
        everything = _filter_day(RS, token, day)
    except Exception as e:
        print(f"$filter FAILED: {type(e).__name__}: {e}")
        everything = []
    print(f"{len(everything)} message(s) in the mailbox that day "
          f"($search found {len(on_day)})")

    lonny_l = RS.LONNY_EMAIL.lower()

    def _touches_lonny(it: dict) -> bool:
        who = [((it.get("from") or {}).get("emailAddress") or {}).get("address") or ""]
        for field in ("toRecipients", "ccRecipients"):
            who += [((r.get("emailAddress") or {}).get("address") or "")
                    for r in (it.get(field) or [])]
        return any(a.lower() == lonny_l for a in who)

    relevant = [it for it in everything if _touches_lonny(it)]
    print(f"of those, {len(relevant)} touch {RS.LONNY_EMAIL}")
    missed = [it for it in relevant
              if it.get("internetMessageId") not in search_imids]
    if missed:
        print(f"\n>>> {len(missed)} Lonny message(s) $filter FOUND and $search MISSED "
              f"— the intake query is the gap:\n")
        for it in missed:
            sender = ((it.get("from") or {}).get("emailAddress") or {}).get("address") or "<none>"
            print(f"  {RS.classify(it) or 'DROPPED':<20} {sender}")
            print(f"  {'':<20} {it.get('receivedDateTime')}  "
                  f"{_short(it.get('subject'), 90)}")
    elif relevant:
        print(">>> $search found every Lonny message $filter did — the query is fine.")
    else:
        print(f">>> No message in the entire mailbox on {day} involves "
              f"{RS.LONNY_EMAIL}. Nothing was dropped; nothing arrived.")

    # ── link 4+5: what the tracking data holds for that day ─────────────────
    _rule(f"tracking-data-v2.json — rows dated {day}")
    data_path = root / "tracking-data-v2.json"
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
