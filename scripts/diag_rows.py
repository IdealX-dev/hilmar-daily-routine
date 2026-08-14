"""diag_rows.py — why does a row show a quote while OL-USA RESPONSES is empty?

2026-07-30: the Jul 29 report rendered

    NEW REQUESTS FROM LONNY (3)   populated
    OL-USA RESPONSES (0)          "No activity"
    STATUS CHANGES (0)            "No activity"
    PENDING OL (0)                "No activity"
    PENDING HILMAR (3)            populated, WITH carrier + rate (CMA CGM $3,150)

Those cannot all be right. gen_email buckets by EVENT DATE:

    scripts/gen_email.py:186-199
        if req_d  == today_date: new_requests.append(r)
        if resp_d == today_date: ol_responses.append(r)   # response_timestamp

while PENDING HILMAR is CURRENT STATE and not windowed at all
(scripts/gen_email.py:800-801). So a row with a carrier and a rate but no
usable `response_timestamp` silently vanishes from OL-USA RESPONSES while
still showing its quote under PENDING HILMAR — the report would under-report
real OL activity and nothing would say so.

This prints, for the report window, exactly the fields that decide which
bucket a row lands in, so the question is answered from data instead of
inference.

READ-ONLY. Pulls state from the blob store (reads still work; it is writes
that 404 — see scripts/diag_blob.py) into a temp dir and prints. It never
writes to the blob, never mutates the working tree, and never emails.

PII: prints lane/carrier/rate/timestamps and request ids only. No email
bodies, no addresses, no subjects.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _et_date(ts):
    """Mirror gen_email._et_date closely enough to reproduce its bucketing."""
    if not ts:
        return None
    try:
        import core
        return core.et_date_of(ts) if hasattr(core, "et_date_of") else None
    except Exception:
        return None


def main() -> int:
    window = os.environ.get("DIAG_WINDOW_DATE", "").strip()

    import state_store

    tmp = Path(tempfile.mkdtemp())
    try:
        pulled = state_store.pull(root=tmp)
    except Exception as e:
        print(f"pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s) into a temp dir: {', '.join(pulled)}")

    data_path = tmp / "tracking-data-v2.json"
    if not data_path.exists():
        print("tracking-data-v2.json not in the store — nothing to inspect")
        return 2
    data = json.loads(data_path.read_text(encoding="utf-8"))
    rows = data.get("requests", [])
    print(f"tracking-data-v2.json: {len(rows)} requests")
    # The "state is FROZEN, writes have failed since 2026-07-27" warning that
    # used to print here was true when this script was written and is false
    # now — blob writes were restored and daily.yml's "Push pipeline state
    # back to blob store" step has run green since. Leaving it would have had
    # this diagnostic tell its reader to distrust the very numbers it prints.
    print("NOTE: this is the STORED state as of the last successful push.\n")

    if not window:
        # Default to the most recent request_date present.
        dates = sorted({str(r.get("request_date") or "")[:10] for r in rows} - {""})
        window = dates[-1] if dates else ""
        print(f"DIAG_WINDOW_DATE unset — using latest request_date present: {window}")

    print(f"\n{'=' * 78}\nROWS WITH request_date == {window}\n{'=' * 78}")
    hit = 0
    for r in rows:
        rd = str(r.get("request_date") or "")[:10]
        if rd != window:
            continue
        hit += 1
        rts = r.get("request_timestamp")
        resp = r.get("response_timestamp")
        print(f"\n  request_id            : {r.get('request_id')}")
        print(f"  lane                  : {r.get('lane') or (str(r.get('origin')) + ' -> ' + str(r.get('destination')))}")
        print(f"  status                : {r.get('status')}   quoted={r.get('quoted')}")
        print(f"  request_timestamp     : {rts}   (ET date {_et_date(rts)})")
        print(f"  response_timestamp    : {resp!r}   (ET date {_et_date(resp)})")
        print(f"  carrier_quoted        : {r.get('carrier_quoted')!r}")
        print(f"  ol_rate               : {r.get('ol_rate')!r}")
        print(f"  ol_responder_signer   : {r.get('ol_responder_signer')!r}")
        print(f"  status_history entries: {len(r.get('status_history') or [])}")
        for h in (r.get("status_history") or [])[-3:]:
            print(f"      {h.get('at')}  {h.get('from')} -> {h.get('to')}")
        # THE POINT: a row quoted but with no response_timestamp is invisible
        # to OL-USA RESPONSES while still rendering under PENDING HILMAR.
        if (r.get("carrier_quoted") or r.get("ol_rate")) and not resp:
            print("  *** QUOTED BUT NO response_timestamp — this row can never "
                  "appear in OL-USA RESPONSES ***")
    if not hit:
        print("  (no rows with that request_date in the stored state)")

    # Account-wide sweep for the same shape, so this is not judged on 3 rows.
    print(f"\n{'=' * 78}\nWHOLE-DATASET SWEEP\n{'=' * 78}")
    quoted_no_ts = [r for r in rows
                    if (r.get("carrier_quoted") or r.get("ol_rate"))
                    and not r.get("response_timestamp")]
    print(f"  rows with a carrier or rate but NO response_timestamp: {len(quoted_no_ts)}")
    for r in quoted_no_ts[:25]:
        print(f"    {r.get('request_id')}  {r.get('lane')}  "
              f"carrier={r.get('carrier_quoted')!r} rate={r.get('ol_rate')!r} "
              f"status={r.get('status')}")
    if len(quoted_no_ts) > 25:
        print(f"    … and {len(quoted_no_ts) - 25} more")

    print("\n  Every one of those is a real OL quote that the daily report's")
    print("  OL-USA RESPONSES section can never show, on any day, because the")
    print("  bucket is keyed on response_timestamp.")

    resp_dates: dict[str, int] = {}
    for r in rows:
        d = _et_date(r.get("response_timestamp"))
        if d:
            resp_dates[str(d)] = resp_dates.get(str(d), 0) + 1
    print("\n  response_timestamp ET-date histogram (last 10 dates present):")
    for d in sorted(resp_dates)[-10:]:
        print(f"    {d}  {resp_dates[d]:4d}")

    # ── WHY each undated row could not be dated ──────────────────────
    # 2026-08-13, Michael on the banner: "still shouldn't exist". QC-077
    # reports these as "link to a cached message that carries no send time or
    # could not be classified", which is two very different problems collapsed
    # into one bucket. Print the actual linkage so the fix is chosen from data:
    # a row linked ONLY to Lonny's own ask cannot be dated without fabricating
    # turnaround (quote_evidence_ok refuses it, correctly), whereas a row
    # linked to an OL message that simply lost its send time is recoverable.
    print(f"\n{'=' * 78}\nWHY THE UNDATED ROWS ARE UNDATED\n{'=' * 78}")
    bodies: dict = {}
    for name in ("stage_emails_bodies.txt", "stage_emails_bodies.jsonl"):
        bpath = tmp / name
        if not bpath.exists():
            bpath = tmp / "scripts" / name
        if bpath.exists():
            with open(bpath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("imid"):
                        bodies[rec["imid"]] = rec
            break
    print(f"  body cache: {len(bodies)} message(s)")
    try:
        import core as C
    except Exception as e:  # pragma: no cover - diagnostic only
        print(f"  core import failed: {e}")
        C = None
    tally: dict[str, int] = {}
    for r in quoted_no_ts:
        imids = r.get("source_imids") or []
        if not imids:
            label = "no source_imids at all"
        else:
            recs = [(i, bodies.get(i)) for i in imids]
            if not any(rec for _, rec in recs):
                label = "linked message(s) not in the body cache"
            else:
                senders, sends, refused = [], 0, 0
                for _i, rec in recs:
                    if not rec:
                        continue
                    snd = (rec.get("sender_email") or "?").lower()
                    senders.append(snd)
                    st = C.body_send_time(rec) if C else None
                    if st:
                        sends += 1
                        if C and not C.quote_evidence_ok(
                                rec.get("sender_email"), st,
                                r.get("request_timestamp")):
                            refused += 1
                if not sends:
                    label = "linked message(s) cached but carry NO send time"
                elif refused >= sends:
                    ol = any(s.endswith("@ol-usa.com") for s in senders)
                    label = ("send time REFUSED — linked only to non-OL mail "
                             "(Lonny's own ask)" if not ol else
                             "send time REFUSED — OL mail, but not after the ask")
                else:
                    label = "should have been datable — unexplained"
        tally[label] = tally.get(label, 0) + 1
    for label, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"    {n:4d}  {label}")

    # ── The EXACT QC-077 population, and where its evidence came from ─
    # The banner counts a row as an undated QUOTE on `rate or carrier`. But a
    # carrier written by the operator correction that folded in Michael's
    # transaction report is BOOKING evidence, not evidence that a quote email
    # ever arrived — and for Jun-Aug it demonstrably did not (OL replied to
    # Lonny, cc the group, never this mailbox). Print the split so the fix is
    # chosen on what these rows actually are.
    print(f"\n{'=' * 78}\nTHE QC-077 POPULATION (banner's own filter)\n{'=' * 78}")
    if C is None:
        print("  core unavailable — skipped")
    else:
        pop = [r for r in quoted_no_ts if not C.has_no_rfq_chain(r)]
        print(f"  rows QC-077 counts: {len(pop)}")
        split: dict[str, int] = {}
        for r in pop:
            has_rate = C.has_quote_evidence(r) and r.get("ol_rate") is not None
            booking = bool(r.get("mdolx_ref") or r.get("booking_no")
                           or r.get("booking_timestamp"))
            reasons = " ".join(str((h or {}).get("reason") or "")
                               for h in (r.get("status_history") or []))
            corrected = "Operator correction" in reasons
            key = (f"status={r.get('status')} rate={'yes' if has_rate else 'NO'} "
                   f"booking_ref={'yes' if booking else 'no'} "
                   f"operator_corrected={'yes' if corrected else 'no'}")
            split[key] = split.get(key, 0) + 1
        for key, n in sorted(split.items(), key=lambda kv: -kv[1]):
            print(f"    {n:4d}  {key}")
        print("\n  first 25, with the evidence fields that decide this:")
        for r in pop[:25]:
            reasons = " ".join(str((h or {}).get("reason") or "")
                               for h in (r.get("status_history") or []))
            print(f"    {r.get('request_id')}  {r.get('lane')}  "
                  f"status={r.get('status')} rate={r.get('ol_rate')!r} "
                  f"carrier={r.get('carrier_quoted')!r} "
                  f"mdolx={r.get('mdolx_ref')!r} "
                  f"corrected={'Operator correction' in reasons}")

    # ── THE REPORT CONTRADICTED ITSELF (2026-08-13 PM) ───────────────
    # Michael, on the Aug 12 email: "in status shows nothing pending hilmar in
    # the chart but then in words says yes, we had 2 new requests but three
    # open etc etc.. it's all wrong".
    #
    # STATUS CHANGES listed three rows moving INTO PENDING HILMAR, and the
    # PENDING HILMAR section immediately below read (0) / No activity. Both
    # sections are fed by the same list and pending_substate only ever returns
    # PENDING_OL or PENDING_HILMAR for a PENDING row, so (0) means there were
    # no PENDING rows at render time at all. Print every one, with the fields
    # that decide which bucket it lands in.
    #
    # The undated-quote note and QC-077 also disagreed — 16 against 7 — and
    # they are supposed to count the SAME rows (they drifted once before,
    # #148). Both predicates are evaluated here side by side so the difference
    # is read rather than argued about.
    print(f"\n{'=' * 78}\nPENDING ROWS AND THE TWO UNDATED COUNTS\n{'=' * 78}")
    if C is None:
        print("  core unavailable — skipped")
    else:
        pend = [r for r in rows if (r.get("status") or "").upper() == "PENDING"]
        print(f"  rows with status == PENDING: {len(pend)}")
        for r in pend:
            print(f"    {r.get('request_id')}  {r.get('lane')}  "
                  f"quoted={r.get('quoted')!r} "
                  f"substate={C.pending_substate(r)!r} "
                  f"loss_reason={r.get('loss_reason')!r} "
                  f"resp={str(r.get('response_timestamp'))[:19]}")
        if not pend:
            print("    (none — this is why both PENDING sections render 0)")

        # QC-077's predicate, spelled out exactly as qc_selfheal applies it.
        qc = [r for r in rows
              if (C.is_real_rate(r.get("ol_rate")) or r.get("carrier_quoted"))
              and not r.get("response_timestamp")
              and not C.has_no_rfq_chain(r)
              and not C.quote_evidence_is_booking_derived(r)]
        # The report note's predicate, via the real function.
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import gen_email as _GE
            note = _GE.undated_quotes({"requests": rows})
        except Exception as e:  # pragma: no cover - diagnostic only
            note = []
            print(f"  gen_email.undated_quotes unavailable: {e}")
        print(f"\n  QC-077 predicate counts : {len(qc)}")
        print(f"  report-note counts      : {len(note)}")
        qc_ids = {r.get("request_id") for r in qc}
        note_ids = {r.get("request_id") for r in note}
        only_note = sorted(note_ids - qc_ids)
        only_qc = sorted(qc_ids - note_ids)
        print(f"  in the NOTE but not QC-077: {len(only_note)}")
        for rid in only_note[:15]:
            r = next((x for x in rows if x.get("request_id") == rid), {})
            print(f"      {rid}  status={r.get('status')} "
                  f"rate={r.get('ol_rate')!r} carrier={r.get('carrier_quoted')!r} "
                  f"mdolx={r.get('mdolx_ref')!r} "
                  f"no_rfq_chain={C.has_no_rfq_chain(r)} "
                  f"booking_derived={C.quote_evidence_is_booking_derived(r)}")
        print(f"  in QC-077 but not the NOTE: {len(only_qc)}")
        for rid in only_qc[:15]:
            print(f"      {rid}")

    # ── THE BANNER ROW, AND EVERY MESSAGE THAT COULD HAVE DATED IT ───
    # 2026-08-14. The report said "1 recent quote has a rate or carrier but
    # no response time". Michael: "untrue again as i gave this to you before
    # and emailed you a copy." If he emailed it, the evidence EXISTS in the
    # mailbox we read, and "cannot be dated" is a pipeline failure, not a
    # data gap. So: name the row, name its linked messages, then scan the
    # whole stage for every message mentioning that lane — with its bucket
    # and sender — so we can see exactly where his copy went.
    print(f"\n{'=' * 78}\nTHE CURRENT UNDATED ROW(S), AND THE STAGE SCAN FOR THEIR LANES\n{'=' * 78}")
    stage_recs = []
    for name in ("stage_emails.txt",):
        spath = tmp / name
        if not spath.exists():
            spath = tmp / "scripts" / name
        if spath.exists():
            with open(spath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        stage_recs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    print(f"  stage records loaded: {len(stage_recs)}")
    try:
        import gen_email as _GE2
        current_undated = _GE2.undated_quotes({"requests": rows})
    except Exception as e:  # pragma: no cover - diagnostic only
        current_undated = []
        print(f"  gen_email unavailable: {e}")
    print(f"  rows the banner counts RIGHT NOW: {len(current_undated)}")
    for r in current_undated:
        print(f"\n  {r.get('request_id')}  {r.get('lane')}")
        print(f"    status={r.get('status')} quoted={r.get('quoted')} "
              f"loss_reason={r.get('loss_reason')!r}")
        print(f"    carrier={r.get('carrier_quoted')!r} rate={r.get('ol_rate')!r} "
              f"mdolx={r.get('mdolx_ref')!r}")
        print(f"    request_ts={r.get('request_timestamp')} "
              f"resp_ts={r.get('response_timestamp')!r}")
        print(f"    source_imids={r.get('source_imids')!r}")
        # Every staged message that mentions this destination, bucket and all.
        dest = str(r.get("destination") or "").split("(")[0].strip().lower()
        if not dest:
            continue
        hits = [s for s in stage_recs
                if dest in str(s.get("subject") or "").lower()]
        print(f"    stage messages whose subject mentions {dest!r}: {len(hits)}")
        for s in hits[-12:]:
            print(f"      [{s.get('bucket')}] {s.get('sent') or s.get('received')} "
                  f"from={s.get('sender_email')!r} imid={str(s.get('imid'))[:40]!r}")
            print(f"        subj={str(s.get('subject'))[:90]!r}")

    # ── IS THE TURNAROUND CLOCK TRUSTWORTHY YET? ─────────────────────
    # Michael 2026-08-13, after the shared mailbox came online: "turnaround
    # clock should be fine now that you see the shard box yourself".
    # core.TIMING_VALID_FROM exists because fabricated timing shipped from
    # this repo once, so the flag comes off on EVIDENCE, not on expectation.
    #
    # The question is not "do we have timestamps" but "are they OL's real send
    # times". Two things would say no: a response BEFORE the ask (mis-paired,
    # the shape QC clears at >40 biz-hrs), and a turnaround so large it can
    # only be a wrong pairing. Printed as a distribution so the decision is
    # read off the data.
    print(f"\n{'=' * 78}\nTURNAROUND PLAUSIBILITY\n{'=' * 78}")
    dated = [r for r in rows if r.get("response_timestamp")
             and r.get("request_timestamp")]
    print(f"  rows with BOTH a request and a response time: {len(dated)} "
          f"of {len(rows)}")
    if C is not None and dated:
        buckets = {"NEGATIVE (response before ask)": 0, "0-4h": 0, "4-24h": 0,
                   "24-48h": 0, "48h-7d": 0, "7-30d": 0, ">30d": 0}
        worst = []
        for r in dated:
            a = C.parse_iso(r.get("request_timestamp"))
            b = C.parse_iso(r.get("response_timestamp"))
            if not (a and b):
                continue
            h = (b - a).total_seconds() / 3600.0
            worst.append((h, r))
            if h < 0:
                buckets["NEGATIVE (response before ask)"] += 1
            elif h <= 4:
                buckets["0-4h"] += 1
            elif h <= 24:
                buckets["4-24h"] += 1
            elif h <= 48:
                buckets["24-48h"] += 1
            elif h <= 168:
                buckets["48h-7d"] += 1
            elif h <= 720:
                buckets["7-30d"] += 1
            else:
                buckets[">30d"] += 1
        for k, n in buckets.items():
            print(f"    {n:5d}  {k}")
        worst.sort(key=lambda t: -abs(t[0]))
        print("\n  the 12 least plausible (largest |gap|):")
        for h, r in worst[:12]:
            print(f"    {h:10.1f}h  {r.get('request_id')}  {r.get('lane')}  "
                  f"req={r.get('request_timestamp')} resp={r.get('response_timestamp')}")
        # The verdict the flag turns on. Anything negative is a mis-pairing;
        # anything past 30d is not a quote turnaround by any reading.
        bad = buckets["NEGATIVE (response before ask)"] + buckets[">30d"]
        print(f"\n  IMPLAUSIBLE (negative or >30d): {bad} of {len(worst)} "
              f"({100.0 * bad / max(1, len(worst)):.1f}%)")
        print("  TIMING_VALID_FROM currently: "
              f"{C.TIMING_VALID_FROM!r}")

    # ── STATUS CHANGES, by day and by reason ─────────────────────────
    # Michael 2026-08-13: "clean up the massive status changes asap to just
    # what's current last two days.. we don't need to see all that you fixed".
    # gen_email windows this section on the report day already, so the question
    # is WHICH transitions land on that day and which of them are real business
    # events rather than the tracker correcting its own past. Printed with the
    # reason text because that is the only field that tells them apart.
    print(f"\n{'=' * 78}\nSTATUS-HISTORY TRANSITIONS BY ET DAY (last 6 days present)\n{'=' * 78}")
    by_day: dict[str, list] = {}
    for r in rows:
        for h in (r.get("status_history") or []):
            at = h.get("at")
            if not (at and h.get("from") and h.get("to")
                    and h["from"] != h["to"]):
                continue
            d = str(_et_date(at) or "")
            if d:
                by_day.setdefault(d, []).append((r, h))
    for d in sorted(by_day)[-6:]:
        entries = by_day[d]
        print(f"\n  {d} — {len(entries)} transition(s)")
        reasons: dict[str, int] = {}
        for _r, h in entries:
            key = (h.get("reason") or "(no reason)")[:72]
            reasons[key] = reasons.get(key, 0) + 1
        for key, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:12]:
            print(f"      {n:3d}  {key}")
        for _r, h in entries[:8]:
            print(f"        {_r.get('request_id')}  {h.get('from')} -> "
                  f"{h.get('to')}  | {(h.get('reason') or '')[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
