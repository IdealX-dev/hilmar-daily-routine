"""diag_matching.py — why do OL's rate responses not attach to Lonny's asks?

2026-08-12, Michael: "ol responded to everything." The weekly table says
otherwise — W31: 13 requests, 0 quoted; W32: 12 requests, 1 quoted — while
455 mbd_rate_response records sit in stage, 28 of them on Jul 28 alone. Both
cannot be true, and Michael's statement is about reality, so the defect is in
MATCHING, not in OL's behaviour or in intake.

This replays the REAL matcher — ingest.apply_rate_responses with its `trace`
hook — over freshly rebuilt requests, and reports the fate of every rate
response by category. It never re-implements the matching rules: this repo has
already been burned by a diagnostic that modelled the pipeline in a different
order and confidently answered questions about a system that did not exist.
Whatever this prints is what production did.

Output, in order:
  1. INPUT REALITY — how many rate responses reach the matcher, and how many
     carry each field the matcher needs (destination, sent, conversation_id,
     body_parsed). A field missing at scale IS the answer.
  2. FATE OF EVERY RATE RESPONSE — matched / no_destination / no_send_time /
     terminal_filter_emptied / no_candidate_matched, with the per-row reason
     tally inside the last one (already_quoted, ask_after_reply, outside_14d,
     request_undated).
  3. SAMPLES — real subjects per failure category, so the fix is written
     against the actual mail OL sends, not an imagined shape.
  4. THE UNQUOTED ASKS — every request in the recent weeks with no quote, and
     the rate responses that name the same lane within 14 days. If those
     exist, the pairing is provably recoverable and the matcher is at fault.

READS ONLY. Pulls state, rebuilds in memory, prints. No blob write, no send,
no mutation of stored data — the rebuilt rows are discarded.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _rule(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * max(0, 66 - len(title))}")


def _short(s, n: int) -> str:
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    import core as C
    import ingest as IN
    import refresh_stage as RS

    sample_n = int(os.environ.get("DIAG_SAMPLES", "8") or 8)
    days = int(os.environ.get("DIAG_DAYS", "21") or 21)

    _rule("state store")
    import state_store
    print(f"pulling state into {ROOT} (overwrites local copies)")
    try:
        pulled = state_store.pull(root=ROOT)
    except Exception as e:
        print(f"pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s)")

    rows = []
    for line in RS.STAGE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    print(f"stage_emails: {len(rows)} records")

    # Attach bodies exactly as ingest.main does — a diagnostic that skips the
    # join answers a question about a pipeline nobody runs (2026-08-10 lesson,
    # diag_bookings).
    attached = IN.attach_bodies(rows)
    print(f"bodies attached to stage rows: {attached}/{len(rows)} "
          "(this is also the only source of conversation_id)")

    # The client gate, replayed with the REAL predicate — main() drops
    # out-of-scope rows BEFORE the bucket split, which is why the fire log says
    # 378 rate responses and the stage file holds 455. A diagnostic that skips
    # it reports on a superset production never matched, and every count here
    # would fail to reconcile with the fire's own line.
    _kept = [r for r in rows if not IN.out_of_scope_reason(r)]
    print(f"client gate: {len(rows) - len(_kept)} row(s) dropped as out-of-scope "
          f"(same predicate main() uses)")

    lonny_out = [r for r in _kept if r.get("bucket") == "lonny_outbound"]
    rate_rsps = [r for r in _kept if IN.counts_as_rate_response(r)]
    requests = IN.build_requests(lonny_out)
    print(f"lonny_outbound rows: {len(lonny_out)} → built requests: {len(requests)}")
    print(f"rate responses reaching the matcher: {len(rate_rsps)}")
    print("  (both counts must reconcile with the fire log's "
          "'Built N rate_requests' / 'Rate-response matches: X/Y')")

    # ── 1. input reality ───────────────────────────────────────────────────
    _rule("what the matcher receives — field presence on rate responses")
    have = Counter()
    for rr in rate_rsps:
        have["destination (row field)"] += bool(rr.get("destination"))
        have["destination (from subject)"] += bool(
            IN.clean_destination(rr.get("subject", "")))
        have["sent"] += bool(rr.get("sent"))
        have["conversation_id"] += bool(rr.get("conversation_id"))
        have["body_parsed"] += bool(rr.get("body_parsed"))
        have["body_parsed.rate_table"] += bool(
            (rr.get("body_parsed") or {}).get("rate_table"))
    total = len(rate_rsps) or 1
    for k, n in have.items():
        print(f"  {k:<28} {n:>5}/{total}  ({100 * n / total:.0f}%)")
    req_conv = sum(1 for r in requests if r.get("conversation_id"))
    print(f"  {'requests w/ conversation_id':<28} {req_conv:>5}/{len(requests)}")

    # ── 2. replay the real matcher, traced ─────────────────────────────────
    _rule("fate of every rate response (the REAL matcher, traced)")
    events: list[tuple[dict, str, dict]] = []
    quoted = IN.apply_rate_responses(
        requests, rate_rsps,
        trace=lambda rr, outcome, detail: events.append((rr, outcome, detail)))
    fates = Counter(o for _, o, _ in events)
    traced = sum(fates.values())
    print(f"  matches reported by the function: {quoted}/{len(rate_rsps)}")
    for outcome, n in fates.most_common():
        print(f"  {outcome:<26} {n:>5}")
    if traced < len(rate_rsps):
        print(f"  {'(untraced — see note)':<26} {len(rate_rsps) - traced:>5}")

    why_totals = Counter()
    for _, outcome, d in events:
        if outcome == "no_candidate_matched":
            for k, n in (d.get("reasons") or {}).items():
                why_totals[k] += n
            if not d.get("candidates"):
                why_totals["zero_candidates_on_lane"] += 1
    if why_totals:
        _rule("inside no_candidate_matched — why each candidate was rejected")
        for k, n in why_totals.most_common():
            print(f"  {k:<26} {n:>5}")

    # ── 3. samples per failure category ────────────────────────────────────
    _rule(f"samples — up to {sample_n} real subjects per failure category")
    by_outcome: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for rr, outcome, d in events:
        if outcome != "matched":
            by_outcome[outcome].append((rr, d))
    for outcome, items in by_outcome.items():
        print(f"\n  [{outcome}] {len(items)} total")
        for rr, d in items[:sample_n]:
            print(f"    {_short(rr.get('sent'), 20):<20} "
                  f"dest={_short(d.get('dest') or '—', 22):<22} "
                  f"{_short(rr.get('subject'), 74)}")
            if d.get("reasons"):
                print(f"        candidates={d.get('candidates')} "
                      f"conv={d.get('conv')} reasons={d['reasons']}")

    # ── 4. the unquoted asks, and the replies that name their lane ─────────
    _rule(f"unquoted asks in the last {days}d + rate responses on the same lane")
    cutoff = C.now_utc() - timedelta(days=days)
    recoverable = 0
    examined = 0
    for r in sorted(requests, key=lambda r: r.get("request_timestamp") or ""):
        req_dt = C.parse_iso(r.get("request_timestamp"))
        if not req_dt or req_dt < cutoff:
            continue
        if r.get("quoted"):
            continue
        examined += 1
        lane_key = IN.canonical_lane_key(r.get("destination"))
        near: list[tuple] = []
        for rr in rate_rsps:
            sent_dt = C.parse_iso(rr.get("sent"))
            if not sent_dt or sent_dt < req_dt:
                continue
            if (sent_dt - req_dt) > timedelta(days=14):
                continue
            rr_dest = rr.get("destination") or IN.clean_destination(
                rr.get("subject", ""))
            same_key = rr_dest and IN.canonical_lane_key(rr_dest) == lane_key
            same_port = rr_dest and C.same_port(rr_dest, r.get("destination"))
            if same_key or same_port:
                near.append((sent_dt, rr, same_key, same_port))
        flag = ""
        if near:
            recoverable += 1
            flag = f"  <-- {len(near)} same-lane repl(y|ies) WITHIN 14d"
        print(f"\n  {str(r.get('request_timestamp'))[:16]:<17} "
              f"{_short(r.get('lane') or r.get('destination'), 34):<34} "
              f"{r.get('request_id')}{flag}")
        for _sent_dt, rr, same_key, same_port in sorted(near, key=lambda t: t[0])[:3]:
            print(f"      reply {str(rr.get('sent'))[:16]:<17} "
                  f"key={'Y' if same_key else 'n'} port={'Y' if same_port else 'n'} "
                  f"conv={'Y' if rr.get('conversation_id') else 'n'} "
                  f"{_short(rr.get('subject'), 60)}")
    print(f"\n  {examined} unquoted ask(s) examined; {recoverable} have at least one "
          f"same-lane OL reply inside the 14-day window.")
    if recoverable:
        print("  Those pairings are provably recoverable from data already in "
              "stage — the matcher is dropping them, OL is not silent.")
    else:
        print("  NONE are recoverable by lane. Either the reply never reached "
              "this mailbox, or it is staged under a shape the lane test "
              "cannot see — the thread walk below tells them apart.")

    # ── 5. the thread walk — the question lane-matching cannot answer ──────
    # "OL responded to everything" (Michael) vs "no same-lane reply exists"
    # (above) can BOTH be true if the reply is staged under another bucket,
    # another subject, or reached a mailbox we do not read. conversation_id
    # is the one identifier that survives all three, so walk the ask's own
    # thread and print EVERY staged message after it, whatever its bucket.
    # If the thread is empty after the ask, the reply is not in our data at
    # all and no matcher change can conjure it — that is an ACCESS finding,
    # not a parsing one, and it belongs to OL IT rather than to this repo.
    _rule(f"thread walk — every staged message after each unquoted ask ({days}d)")
    by_conv: dict[str, list[dict]] = defaultdict(list)
    for r in rows:                      # ALL staged rows, pre-gate, any bucket
        conv = r.get("conversation_id")
        if conv:
            by_conv[conv].append(r)
    silent_threads = 0
    with_traffic = 0
    for r in sorted(requests, key=lambda r: r.get("request_timestamp") or ""):
        req_dt = C.parse_iso(r.get("request_timestamp"))
        if not req_dt or req_dt < cutoff or r.get("quoted"):
            continue
        conv = r.get("conversation_id")
        after = []
        for m in by_conv.get(conv or "", []):
            m_dt = C.parse_iso(m.get("sent") or m.get("received"))
            if m_dt and m_dt > req_dt:
                after.append((m_dt, m))
        if after:
            with_traffic += 1
        else:
            silent_threads += 1
        print(f"\n  {str(r.get('request_timestamp'))[:16]:<17} "
              f"{_short(r.get('lane') or r.get('destination'), 30):<30} "
              f"{'NO REPLY IN THREAD' if not after else str(len(after)) + ' msg(s) after the ask'}"
              f"{'' if conv else '   [ask has no conversation_id]'}")
        for _m_dt, m in sorted(after, key=lambda t: t[0])[:4]:
            print(f"      {str(m.get('sent') or m.get('received'))[:16]:<17} "
                  f"{_short(m.get('bucket'), 18):<18} "
                  f"{_short(m.get('sender_email') or m.get('sender'), 34):<34} "
                  f"{_short(m.get('subject'), 46)}")
    print(f"\n  {silent_threads} ask thread(s) have NO staged message after the ask; "
          f"{with_traffic} do.")
    if silent_threads and not with_traffic:
        print("  VERDICT: the replies are not in this mailbox in ANY bucket. "
              "This is an intake/access finding (which mailboxes refresh_stage "
              "can read), NOT a matcher or parser defect.")

    print("\nNOTHING WAS WRITTEN. The stored tracking data is untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
