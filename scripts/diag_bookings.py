"""diag_bookings.py — OL confirmed a booking. Where did the win go?

2026-08-10, Michael: "if ol confirmed bookings with mdolx numbers it's a win.
what are you talking about."

That is the rule, and it is simpler than the code I was reasoning about. I had
been treating a booking as a win only once it MATCHED an RFQ, and theorising
about why the match failed. Wrong frame. ingest already agrees with Michael:
an unmatched booking becomes a standalone `stand_<mdolx>` WIN row. So an MDOLX
confirmation produces a win either way — matched or standalone — UNLESS one of
three gates drops it before it ever reaches that code:

    out_of_scope_reason()      numidia / agridairy / trucking / recalled
    is_operational_subject()   FREE-TIME ISSUE, LOADING APPT, DRAFT RATED, …
    no MDOLX parsed            extract_mdolx() found nothing

Each gate was added to stop a real false positive, and each is a plausible
place for a real booking to be eaten. The 2026-08-10 fire reported
`requests=12 bookings=0` for Aug 3-7 while QC-009 counted 4 staged
mbd_inbound messages that week. Four confirmations in, zero wins out.

This does not guess which gate. It runs the REAL gates, in the real order,
over the REAL staged rows, and prints the verdict per booking — then checks
whether tracking-data actually holds a win for that MDOLX.

READS ONLY. Pulls state (see diag_day for why into the repo root), runs pure
predicates, prints. No writes to the blob, no send, no mutation of stage or
tracking data.

Usage
    DIAG_SINCE=2026-08-03 DIAG_UNTIL=2026-08-07 python3 scripts/diag_bookings.py
    DIAG_SINCE=2026-07-01 python3 scripts/diag_bookings.py     # until = today
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _rule(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * max(0, 62 - len(title))}")


def _short(s, n: int) -> str:
    s = (str(s) or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


_TAG_RX = re.compile(r"<[^>]+>")
_WS_RX = re.compile(r"[ \t\r\f\v]+")


def _plain(html: str, limit: int = 1400) -> str:
    """Readable text out of an HTML body — enough to JUDGE a booking by.

    Michael 2026-08-10: "read the emails.. that's your job to decide if it's a
    problem with the file or a new win." Subjects were not enough: an
    "UPDATED ETA BOOKING CONFIRMATION" could be a brand-new booking or a
    schedule change on an old one, and only the body says which.
    """
    if not html:
        return ""
    txt = html.replace("</p>", "\n").replace("<br>", "\n").replace("<br/>", "\n")
    txt = _TAG_RX.sub(" ", txt)
    for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
        txt = txt.replace(ent, ch)
    lines = [_WS_RX.sub(" ", ln).strip() for ln in txt.splitlines()]
    txt = "\n".join(ln for ln in lines if ln)
    return txt[:limit]


def trace_one_mdolx(mdolx: str, rows, bodies_full, rows_td, core, IN) -> None:
    """Everything the system holds about ONE booking, in one place."""
    _rule(f"MDOLX{mdolx}")
    hits = []
    for r in rows:
        blob = " ".join(str(r.get(k) or "") for k in
                        ("subject", "summary_preview", "mdolx"))
        body = bodies_full.get(r.get("imid")) or ""
        if mdolx in blob or mdolx in body:
            hits.append(r)
    hits.sort(key=lambda r: str(r.get("received") or r.get("sent") or ""))
    print(f"{len(hits)} staged email(s) mention it\n")

    for r in hits:
        subj = str(r.get("subject") or "")
        reason = IN.out_of_scope_reason(r)
        gate = (f"out_of_scope:{reason}" if reason
                else "operational" if IN.is_operational_subject(subj)
                else "admitted" if (IN.extract_mdolx(subj)
                                    or IN.extract_mdolx(str(r.get("summary_preview") or "")))
                else "no-mdolx")
        print(f"  {str(r.get('received') or r.get('sent'))[:19]}  "
              f"{str(r.get('bucket')):<18} gate={gate}")
        print(f"      {_short(subj, 108)}")

    # The body of the EARLIEST mention — that is the one that says whether
    # this booking was created here or merely updated.
    if hits:
        first = hits[0]
        body = bodies_full.get(first.get("imid")) or ""
        print(f"\n  ── body of the earliest mention "
              f"({str(first.get('received'))[:10]}, {first.get('bucket')}) ──")
        print("  " + (_plain(body) or "(no body on disk)").replace("\n", "\n  "))

    print(f"\n  ── tracking-data rows for MDOLX{mdolx} ──")
    found = False
    for t in rows_td:
        refs = [str(t.get("mdolx_ref") or "")] + [str(x) for x in (t.get("mdolx_refs_all") or [])]
        if mdolx in refs or t.get("request_id") == f"stand_{mdolx}":
            found = True
            print(f"  {t.get('request_id')}  status={t.get('status')} "
                  f"request_date={t.get('request_date')} "
                  f"lane={_short(t.get('lane'), 28)}")
            print(f"      carrier_won={t.get('carrier_won')} "
                  f"teu_won={t.get('teu_won')} etd={t.get('etd_offered')} "
                  f"eta={t.get('eta_offered')} "
                  f"resp_ts={str(t.get('response_timestamp'))[:10]}")
    if not found:
        print(f"  >>> NO ROW anywhere for MDOLX{mdolx}.")


def main() -> int:
    import core

    since = os.environ.get("DIAG_SINCE", "").strip()
    until = os.environ.get("DIAG_UNTIL", "").strip() or "9999-12-31"
    if not since:
        print("::error::DIAG_SINCE is required (YYYY-MM-DD)")
        return 2
    print(f"diag_bookings: window {since} … {until} (ET dates)")

    _rule("state store")
    import state_store
    print(f"pulling state into {ROOT} (overwrites local copies)")
    try:
        pulled = state_store.pull(root=ROOT)
    except Exception as e:
        print(f"pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s)")

    # The pipeline's OWN gates and loaders. No private copies — a diagnostic
    # with its own idea of the rules clears a booking the pipeline still eats.
    import ingest as IN
    import refresh_stage as RS

    stage_path = RS.STAGE_PATH
    rows = []
    for line in stage_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    print(f"stage_emails: {len(rows)} records")

    # Bodies, keyed the way qc_selfheal keys them — so a no-mdolx drop can be
    # told apart from "the number was on disk and nobody looked".
    import qc_selfheal as QC
    bodies_by_imid = {
        k: str((v or {}).get("html_body") or (v or {}).get("text_body") or "")
        for k, v in QC._load_bodies_index().items()
    }
    print(f"bodies:       {len(bodies_by_imid)} records")

    # ATTACH THE BODIES, exactly as ingest.main does before it filters.
    # 2026-08-10: without this, out_of_scope_reason() runs against an empty
    # text_body here and returns None where production returns "agridairy" —
    # which is precisely how this tool reported MDOLX260821 as "admitted by
    # every gate" for a thread the pipeline had been correctly excluding since
    # July. A diagnostic that models the gates with less evidence than the
    # pipeline gets does not model the gates.
    _attached = 0
    for r in rows:
        body = bodies_by_imid.get(r.get("imid")) or ""
        if body:
            r["text_body"] = body
            _attached += 1
    print(f"bodies attached to stage rows: {_attached}/{len(rows)} "
          "(out_of_scope_reason reads text_body — see ingest.main)")

    # Per-MDOLX deep trace. Michael 2026-08-10: "read the emails.. that's your
    # job to decide if it's a problem with the file or a new win." This is that
    # read — every staged mention, the earliest body in full, and every
    # tracking row, for one booking at a time.
    only = [m.strip() for m in os.environ.get("DIAG_MDOLX", "").split(",") if m.strip()]
    if only:
        data_path = ROOT / "tracking-data-v2.json"
        rows_td = (json.loads(data_path.read_text(encoding="utf-8")).get("requests") or []
                   if data_path.exists() else [])
        print(f"tracking-data: {len(rows_td)} requests")
        import ingest as IN0
        for m in only:
            trace_one_mdolx(m, rows, bodies_by_imid, rows_td, core, IN0)
        return 0

    # Booking confirmations only, in window. Dated by the same fields the
    # pipeline uses, through core's canonical ET clock.
    bookings = []
    for r in rows:
        if r.get("bucket") != "mbd_inbound":
            continue
        d = core.et_date_of(r.get("received") or r.get("sent"))
        if not d or not (since <= d <= until):
            continue
        bookings.append((d, r))
    bookings.sort(key=lambda t: t[0])

    _rule(f"staged booking confirmations in window: {len(bookings)}")
    if not bookings:
        print(">>> No mbd_inbound records staged in this window at all. The "
              "loss is at INTAKE, not at these gates — run diag-day.yml.")

    admitted, dropped = [], []
    for d, r in bookings:
        subject = str(r.get("subject") or "")
        preview = str(r.get("summary_preview") or "")
        # EXACT order ingest applies them in.
        reason = IN.out_of_scope_reason(r)
        if reason:
            verdict = f"DROPPED out_of_scope:{reason}"
        elif IN.is_operational_subject(subject):
            hits = [h for h in IN._OPERATIONAL_SUBJECT_HINTS
                    if h in subject.upper()]
            verdict = f"DROPPED operational:{'/'.join(hits) or '?'}"
        else:
            mdolx = r.get("mdolx") or IN.extract_mdolx(subject) or IN.extract_mdolx(preview)
            if mdolx:
                verdict = f"ADMITTED mdolx={mdolx}"
            else:
                # ingest reads the SUBJECT and the 300-char preview only. If the
                # number is in the body, the booking is dropped while the
                # confirmation is sitting on disk with the reference in it.
                # That is a silent loss of a real win, so name it distinctly.
                body = (bodies_by_imid.get(r.get("imid")) or "")
                in_body = IN.extract_mdolx(body)
                verdict = (f"DROPPED no-mdolx-in-subject BUT body has "
                           f"MDOLX{in_body}" if in_body else "DROPPED no-mdolx")
        (admitted if verdict.startswith("ADMITTED") else dropped).append(
            (d, r, verdict))
        print(f"\n  {d}  {verdict}")
        print(f"      {_short(subject, 100)}")

    # ── does tracking-data actually hold a win for each admitted booking? ───
    _rule("tracking-data — is there a WIN for each admitted booking?")
    data_path = ROOT / "tracking-data-v2.json"
    if not data_path.exists():
        print("tracking-data-v2.json not in the store")
        return 2
    data = json.loads(data_path.read_text(encoding="utf-8"))
    rows_td = data.get("requests") or []
    print(f"{len(rows_td)} requests total; "
          f"{sum(1 for r in rows_td if core.is_win(r))} WIN")

    by_ref: dict[str, list] = {}
    for r in rows_td:
        for ref in ([r.get("mdolx_ref")] + list(r.get("mdolx_refs_all") or [])):
            if ref:
                by_ref.setdefault(str(ref), []).append(r)

    missing = []
    for d, r, verdict in admitted:
        mdolx = verdict.split("mdolx=", 1)[-1]
        hit = by_ref.get(mdolx) or [
            t for t in rows_td if t.get("request_id") == f"stand_{mdolx}"]
        if hit:
            t = hit[0]
            print(f"  {mdolx}: WIN present — {t.get('request_id')} "
                  f"status={t.get('status')} lane={_short(t.get('lane'), 30)}")
        else:
            missing.append((d, mdolx, r))
            print(f"  {mdolx}: >>> NO ROW. Admitted by every gate and still "
                  f"produced neither a matched win nor stand_{mdolx}.")

    # ── the mailbox itself: what MDOLX mail exists, and what would we do? ───
    #
    # 2026-08-10, Michael: "yes they wind up in my mail because i'm part of the
    # mbd ocean export group emails." So the confirmations ARE here. The gap is
    # between arriving and being staged, and the pipeline's two queries are:
    #
    #   from:lonny OR to:lonny
    #   from:MBD_OceanExportBookingShared AND subject:HILMAR
    #
    # The second requires the literal token HILMAR in the subject. Note which
    # booking DID survive it above: a NUMIDIA move, whose subject carries
    # "HILMAR" only because Hilmar is the ORIGIN. A genuine Hilmar-client
    # booking whose subject names the lane instead would never match — the
    # filter would be selecting FOR exactly the moves the numidia gate then
    # discards. That is a hypothesis; this section is how it gets tested.
    if os.environ.get("DIAG_SKIP_GRAPH"):
        print("\n(DIAG_SKIP_GRAPH set — mailbox scan skipped)")
    else:
        _rule("mailbox — every MDOLX message, and what the pipeline does with it")
        try:
            token = RS.get_token()
            print(f"reading: {RS._mailbox_base}")
            found = RS.search_messages(token, "MDOLX", max_results=200)
        except Exception as e:
            print(f"mailbox scan FAILED: {type(e).__name__}: {e}")
            found = []

        staged_imids = {r.get("imid") for r in rows if r.get("imid")}
        in_window = []
        for it in found:
            d = core.et_date_of(it.get("receivedDateTime") or it.get("sentDateTime"))
            if d and since <= d <= until:
                in_window.append((d, it))
        in_window.sort(key=lambda t: t[0])
        print(f"{len(found)} MDOLX message(s) returned; {len(in_window)} in window\n")

        by_verdict: dict[str, int] = {}
        for d, it in in_window:
            sender = ((it.get("from") or {}).get("emailAddress") or {}).get("address") or "?"
            subj = str(it.get("subject") or "")
            bucket = RS.classify(it) or "DROPPED-by-classify"
            staged = "staged" if it.get("internetMessageId") in staged_imids else "NOT-staged"
            # why the bookings query would or would not have found it
            q2 = ("q2-match" if sender.lower() == RS.MBD_BOOKING_EMAIL.lower()
                  and "HILMAR" in subj.upper() else "q2-MISS")
            key = f"{bucket} | {staged} | {q2}"
            by_verdict[key] = by_verdict.get(key, 0) + 1
            print(f"  {d}  {bucket:<22} {staged:<11} {q2}")
            print(f"      {sender}")
            print(f"      {_short(subj, 96)}")

        if by_verdict:
            print("\n  summary:")
            for k, n in sorted(by_verdict.items(), key=lambda kv: -kv[1]):
                print(f"    {n:>4}  {k}")
            print("\n  q2-MISS + NOT-staged = a confirmation sitting in the "
                  "mailbox that the bookings query never asked for.")

    # COUNT BOOKINGS, NOT EMAILS. 2026-08-10: this block reported "admitted
    # but NO win row: 4" and the next line told the reader the loss was in
    # ingest. Both halves were wrong. The four were FOUR MESSAGES OF ONE
    # THREAD — a single MDOLX, 260821 — and the pipeline dedupes to one
    # booking per MDOLX (Michael's rule: 1 MDOLX = 1 win). A count in emails
    # against a pipeline that counts in bookings inflates every number here
    # and turns one correctly-excluded thread into a four-booking crisis.
    def _mdolx_of(verdict: str) -> str:
        return verdict.split("mdolx=", 1)[-1]

    admitted_mdolx = {_mdolx_of(v) for _d, _r, v in admitted}
    missing_mdolx: dict[str, tuple] = {}
    for d, mdolx, r in missing:
        missing_mdolx.setdefault(mdolx, (d, r))

    # ── blast radius of the 2026-08-10 client-gate tightening ──────────────
    #
    # Michael asked for full tightening. I flagged the failure mode: a stricter
    # client gate fails by making a REAL WIN quietly stop existing. So the
    # change ships with the diff measured against real staged mail rather than
    # asserted safe — run both gates over the same rows and name every booking
    # whose verdict moved.
    # MIRROR main()'s ORDER OR THE NUMBER IS WRONG. ingest.main removes
    # out-of-scope rows at line ~1713 and only THEN calls collect_bookings, so
    # a row whose own subject says NUMIDIA never reaches the booking collector
    # in production at all. Run 7 compared against unfiltered rows and reported
    # 4 lost bookings, two of which (260874, 260991) say "NUMIDIA" in their own
    # subject — they were never in production's "before", so counting them as
    # newly-dropped overstates what this change actually does. The delta that
    # matters is the one the pipeline would see: kept rows, thread rule on/off.
    _rule("client-gate tightening — what changed")
    _dropped_rows = [r for r in rows if IN.out_of_scope_reason(r)]
    _kept_rows = [r for r in rows if not IN.out_of_scope_reason(r)]
    _excluded = IN.out_of_scope_mdolx(_dropped_rows)
    before = set(IN.collect_bookings(_kept_rows))
    after = set(IN.collect_bookings(_kept_rows, excluded_mdolx=_excluded))
    lost, gained = sorted(before - after), sorted(after - before)
    print(f"  staged rows                        : {len(rows)}"
          f"  (kept {len(_kept_rows)} / out-of-scope {len(_dropped_rows)})")
    print(f"  MDOLX with an out-of-scope sibling : {len(_excluded)}")
    print(f"  bookings BEFORE tightening         : {len(before)}")
    print(f"  bookings AFTER  tightening         : {len(after)}")
    print(f"    no longer bookings               : {len(lost)}")
    print(f"    newly bookings                   : {len(gained)}")
    if lost:
        print("\n  DROPPED — read every one of these. Each is either another "
              "customer's cargo correctly removed, or a Hilmar win lost:")
        for m in lost:
            subj = next((str(r.get("subject") or "") for r in rows
                         if m in str(r.get("subject") or "")), "")
            print(f"    MDOLX{m}  ({_excluded.get(m, '?')})")
            print(f"        {_short(subj, 100)}")
            print(f"        hilmar_signal={IN.hilmar_signal(subj)!r} "
                  "(a 'tag' here would NOT have been dropped)")
    if gained:
        print("\n  NEWLY ADMITTED (unexpected — tightening should not add):")
        for m in gained:
            print(f"    MDOLX{m}")
    if not lost and not gained:
        print("\n  No booking changed verdict on this window's staged mail. "
              "The tightening is a no-op here — which is the outcome to want: "
              "it closes a hole without touching live business.")

    _rule("verdict")
    print(f"staged booking messages      : {len(bookings)}")
    print(f"  dropped by a gate          : {len(dropped)} message(s)")
    print(f"  admitted                   : {len(admitted)} message(s) "
          f"= {len(admitted_mdolx)} booking(s)")
    print(f"  admitted but NO win row    : {len(missing)} message(s) "
          f"= {len(missing_mdolx)} booking(s)")
    if dropped:
        print("\nIf any DROPPED line above is a real Hilmar booking, the gate "
              "that names it is too tight — that is the bug, and the gate name "
              "tells you which one.")
    if missing:
        # NAME THEM HERE, not only where they were found. 2026-08-10: this
        # count read "4" for a whole session while the four MDOLX numbers sat
        # ~300 lines up, above a 200-line mailbox scan, off the end of every
        # log fetch. A finding that scrolls out of reach is a finding nobody
        # has. The verdict block is the part that gets read — so the verdict
        # block carries the evidence.
        print("\n  the ones with NO row:")
        for mdolx, (d, r) in sorted(missing_mdolx.items()):
            n_msgs = sum(1 for _d, m, _r in missing if m == mdolx)
            print(f"    MDOLX{mdolx}  first staged {d}  ({n_msgs} message(s))")
            print(f"        {_short(r.get('subject'), 100)}")
            # BEFORE calling this an ingest bug, check whether the pipeline
            # already knows this thread belongs to someone else. MDOLX260821
            # read as "admitted" on ten messages titled "Hilmar, CA to La
            # Guaira" — but "Hilmar" there is the ORIGIN CITY, and a sibling
            # message in the same thread says "Agri Dairy Vendor Reference
            # PO00-26002163". is_hilmar is `"HILMAR" in subject`, so an
            # origin-city match is indistinguishable from a customer tag.
            # No row is the CORRECT outcome for another customer's cargo that
            # happens to load in the town of Hilmar.
            siblings = [(dd, vv) for dd, rr, vv in dropped
                        if mdolx in str(rr.get("subject") or "")
                        or mdolx in str(rr.get("summary_preview") or "")]
            if siblings:
                reasons = sorted({v.split(":", 1)[-1] for _dd, v in siblings
                                  if v.startswith("DROPPED out_of_scope")})
                if reasons:
                    print(f"        ^ {len(siblings)} sibling message(s) in this "
                          f"thread WERE gated out_of_scope: {', '.join(reasons)}")
                    print("          → almost certainly another customer's move. "
                          "No row is correct; this is not a lost win.")
        print("\nA rowless MDOLX with NO out-of-scope sibling is the real "
              "signal: the loss is AFTER the gates, and "
              "link_bookings_to_requests built neither a match nor a "
              "standalone. That is ingest, not intake.")
    if bookings and not dropped and not missing:
        print("\nEvery staged booking became a win. The gap is upstream — "
              "the confirmations are not reaching stage. Run diag-day.yml.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
