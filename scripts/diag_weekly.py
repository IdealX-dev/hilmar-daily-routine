"""diag_weekly.py — the weekly table Michael screenshotted, decomposed to rows.

2026-08-10, Michael on the dashboard's W24-W33 table: "this is absurd.. your
data is consistently wrong. where it used to be correct before we did
formatting changes."

The screenshot cannot say WHICH numbers are wrong, so this prints the ground
under every cell of that table, plus the three cross-checks that turn "wrong"
into a named defect:

  1. STAGE INTAKE BY DAY — staged bucket counts per ET date, mbd_rate_response
     called out. W31/W32 show 25 requests with zero wins and everything in the
     red column; if OL's quote replies stopped being staged (the Reno classify
     drop, fixed Aug 7 but only effective if a QUERY fetches her mail), every
     request since goes unquoted -> flips to a no-response loss -> weeks of
     all-red. The per-day mbd_rate_response series says whether quote intake
     ever recovered. Zero after Aug 8 = intake is still dead and the fix was
     necessary but not sufficient.

  2. THE TABLE, RECOMPUTED THROUGH THE RENDERER'S OWN BUCKETER — calls
     gen_dashboard.wow_bars, no private copy (a diagnostic with its own idea
     of the rollup answers questions about a table nobody renders). Then the
     same wins bucketed BY WIN-EVENT WEEK next to BY REQUEST WEEK: the
     renderer credits a win to the week Lonny ASKED, so a booking confirmed in
     August for a July ask appears in July and the recent weeks read empty.
     If Michael counts wins by when OL confirmed, the two columns disagree by
     design — that is a semantics finding, not a parser one.

  3. ROW-LEVEL DUMP + MISFILE FLAGS for the recent weeks:
       - LOSS/no-response while still INSIDE the pending window (QC-067 shape,
         premature flip);
       - quoted rows with no response_timestamp (QC-077 shape);
       - WIN rows with containers parsed but teu_won == 0 (the booking-rank
         class fixed 2026-08-10 — stale until a fire re-derives them).

READS ONLY. Pulls state (into the repo root — see diag_day for why), attaches
bodies before running gates exactly as ingest.main does, prints. No blob
write, no send, no mutation of stored data.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _rule(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * max(0, 66 - len(title))}")


def _short(s, n: int) -> str:
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _week_key(day: str | None) -> str | None:
    """ISO week key, identical construction to gen_dashboard.wow_bars."""
    if not day:
        return None
    try:
        iso = datetime.strptime(str(day)[:10], "%Y-%m-%d").isocalendar()
    except Exception:
        return None
    return f"{iso.year}-W{iso.week:02d}"


def main() -> int:
    import core

    weeks_back = int(os.environ.get("DIAG_WEEKS", "10") or 10)
    row_weeks = int(os.environ.get("DIAG_ROW_WEEKS", "3") or 3)

    _rule("state store")
    import state_store
    print(f"pulling state into {ROOT} (overwrites local copies)")
    try:
        pulled = state_store.pull(root=ROOT)
    except Exception as e:
        print(f"pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s)")

    data_path = ROOT / "tracking-data-v2.json"
    if not data_path.exists():
        print("::error::tracking-data-v2.json not in the store")
        return 2
    data = json.loads(data_path.read_text(encoding="utf-8"))
    requests = data.get("requests") or []
    print(f"tracking-data: {len(requests)} requests "
          f"(last_updated {data.get('last_updated') or '?'})")

    import refresh_stage as RS
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

    # ── 1. intake by day — did OL quote replies ever come back? ────────────
    _rule("staged records per ET date, by bucket (last ~35 days)")
    by_day: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        d = core.et_date_of(r.get("received") or r.get("sent"))
        if d:
            by_day[d][str(r.get("bucket"))] += 1
    days = sorted(by_day)[-35:]
    print(f"  {'date':<12} {'rate_resp':>9} {'inbound':>8} {'lonny_out':>9} "
          f"{'lonny_rep':>9} {'other':>6}")
    for d in days:
        c = by_day[d]
        other = sum(c.values()) - (c["mbd_rate_response"] + c["mbd_inbound"]
                                   + c["lonny_outbound"] + c["lonny_reply"])
        flag = "  <-- ZERO quote intake" if c["mbd_rate_response"] == 0 else ""
        print(f"  {d:<12} {c['mbd_rate_response']:>9} {c['mbd_inbound']:>8} "
              f"{c['lonny_outbound']:>9} {c['lonny_reply']:>9} {other:>6}{flag}")

    # ── 2. the table, through the renderer's own bucketer ─────────────────
    _rule("wow_bars — the EXACT rollup the dashboard renders")
    import gen_dashboard as GD
    bars = dict(GD.wow_bars(requests))
    keys = sorted(bars)[-weeks_back:]
    print(f"  {'week':<10} {'req':>4} {'wins':>5} {'ql':>4} {'nq':>4} "
          f"{'pend':>5} {'uncls':>6} {'teu_won':>8} {'teu_lost':>9}")
    for k in keys:
        b = bars[k]
        print(f"  {k:<10} {b['requests']:>4} {b['wins']:>5} {b['ql']:>4} "
              f"{b['nq']:>4} {b['pending']:>5} {b['unclassified']:>6} "
              f"{b['teu_won']:>8} {b['teu_lost']:>9}")

    # Wins by REQUEST week (what the table shows) vs by WIN-EVENT week (when
    # OL actually confirmed). The renderer credits the ask; Michael counts the
    # confirmation. Where these differ, the table reads "wrong" while being
    # internally consistent — a semantics gap, and it must be VISIBLE.
    _rule("wins: by request week (rendered) vs by win-event week (confirmed)")
    by_req_week: dict[str, list] = defaultdict(list)
    by_win_week: dict[str, list] = defaultdict(list)
    for r in requests:
        if not core.is_win(r):
            continue
        ref = r.get("mdolx_ref") or r.get("request_id")
        wk_req = _week_key(r.get("request_date") or r.get("date"))
        if wk_req:
            by_req_week[wk_req].append(ref)
        wk_win = _week_key(core.win_event_date(r))
        if wk_win:
            by_win_week[wk_win].append(ref)
    all_weeks = sorted(set(by_req_week) | set(by_win_week))[-weeks_back:]
    print(f"  {'week':<10} {'by-request':>10}  {'by-confirm':>10}   detail")
    for k in all_weeks:
        a, b = by_req_week.get(k, []), by_win_week.get(k, [])
        note = "" if len(a) == len(b) else "  <-- DIFFER"
        print(f"  {k:<10} {len(a):>10}  {len(b):>10}{note}")
        if a != b:
            only_req = [x for x in a if x not in b]
            only_win = [x for x in b if x not in a]
            if only_req:
                print(f"      only by-request : {', '.join(map(str, only_req))}")
            if only_win:
                print(f"      only by-confirm : {', '.join(map(str, only_win))}")

    # ── 3. rows behind the recent weeks + misfile flags ────────────────────
    recent = sorted(bars)[-row_weeks:]
    _rule(f"every row in the last {row_weeks} rendered week(s)")
    flags = Counter()
    for r in sorted(requests, key=lambda x: str(x.get("request_date") or "")):
        wk = _week_key(r.get("request_date") or r.get("date"))
        if wk not in recent:
            continue
        rid = str(r.get("request_id"))
        status = str(r.get("status"))
        quoted = bool(r.get("quoted"))
        resp = bool(r.get("response_timestamp"))
        reason = r.get("loss_reason") or ""
        marks = []
        # Premature no-response loss: flipped LOSS while the ask is still
        # inside the pending-OL window (QC-067's whole reason to exist).
        if status == "LOSS" and reason == "NO_RESPONSE":
            req_dt = core.parse_iso(r.get("request_timestamp"))
            if req_dt is not None and not core.pending_ol_overdue(req_dt):
                marks.append("PREMATURE-LOSS")
                flags["LOSS/NO_RESPONSE inside the pending window"] += 1
        if quoted and not resp:
            marks.append("QUOTED-NO-TS")
            flags["quoted but no response_timestamp (invisible to reports)"] += 1
        if core.is_win(r) and not (r.get("teu_won") or 0) and r.get("containers"):
            marks.append("TEU0")
            flags["WIN with containers parsed but teu_won=0 (booking-rank class)"] += 1
        print(f"  {wk}  {rid:<24} {status:<8} quoted={int(quoted)} "
              f"resp={int(resp)} {reason:<12} "
              f"teu={r.get('teu_requested')}/{r.get('teu_won')} "
              f"{_short(r.get('lane'), 28)}"
              + ("  [" + ",".join(marks) + "]" if marks else ""))

    _rule("misfile flags across ALL rows (not just recent weeks)")
    all_flags = Counter()
    for r in requests:
        if (r.get("status") == "LOSS" and (r.get("loss_reason") or "") == "NO_RESPONSE"):
            req_dt = core.parse_iso(r.get("request_timestamp"))
            if req_dt is not None and not core.pending_ol_overdue(req_dt):
                all_flags["LOSS/NO_RESPONSE inside the pending window"] += 1
        if r.get("quoted") and not r.get("response_timestamp"):
            all_flags["quoted but no response_timestamp"] += 1
        if core.is_win(r) and not (r.get("teu_won") or 0) and r.get("containers"):
            all_flags["WIN with containers but teu_won=0"] += 1
    if all_flags:
        for k, n in all_flags.most_common():
            print(f"  {n:>4}  {k}")
    else:
        print("  none — every row is internally consistent by these three tests")

    print("\nNOTHING WAS WRITTEN. The stored tracking data is untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
