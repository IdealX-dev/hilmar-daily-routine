# Changelog — Hilmar Daily Tracker

Per the working standard (CLAUDE.md): every session logs its decisions here,
by name, so the next session starts current. Newest first.

## 2026-07-26 — Data audit batch 1: TEU corruption, client contradiction, duplicate rows

Full 6-dimension data-integrity audit (42 agents: 6 finders + adversarial
verification of every finding). 36 raw findings -> 25 CONFIRMED, 11 refuted.
This batch ships the three highest-impact confirmed defects; the rest are
logged as a prioritized backlog below.

1. TEU CORRUPTION FROM REFERENCE NUMBERS (core.parse_teu, both trees)
   The pattern was `(\d+)\s*[x-]?\s*(\d{2})`. The greedy `\d+` ate any digit
   run ending in 20/40/45, and ingest feeds it raw email-preview text:
       parse_teu("PO 4451440")  ->  (44514 containers, 89028 TEU)
   ONE such row poisons every volume figure that day — email KPIs, dashboard
   tiles, lane and week rollups, the client PDF. Reproduced live before fixing.
   The mirror failure was a silent UNDER-count: "40'HC x 2" returned 0 TEU on a
   real 2-container booking.
   Fixed: digit-boundary guards on both sides, quantity capped at 3 digits,
   size restricted to the real ISO sizes, and a SEPARATOR now required between
   quantity and size (every real spelling has one: "2-20'", "1x20'DV",
   "2x40'RF", "3 x 40 HC", "2 40'HC") so a bare "quote 10040" cannot read as
   100 x 40'. Added the reverse phrasing ("40'HC x 2", "20' x 3"), consulted
   ONLY when the forward pattern matched nothing so nothing is double counted.

2. CLIENT EMAIL CONTRADICTED ITSELF (gen_client_email._client_sections)
   The bookings bucket was built from a historical -> WIN transition. A row that
   flips to WIN on Lonny's "please send" and is then re-decided to
   PENDING(AWAITING_MDOLX) on the next fire — because OL has not issued the
   MDOLX confirmation yet — rendered in "Bookings confirmed" AND "Awaiting your
   decision" in the SAME email to the client. Fixed: a booking must still be
   status WIN, and the buckets are now disjoint by row identity.

3. ONE SHIPMENT STORED AS TWO ROWS — new QC-069 (ERROR, detect-only)
   The operator-reported defect #2. Catches (a) one mdolx_ref on more than one
   row (the stand_<mdolx> fallback firing when a booking cannot be linked) and
   (b) an open PENDING row shadowed by a WIN row on the same destination +
   equipment. Destination matching is ALIAS-AWARE (HCMC (Cat Lai) = HCMC =
   Cat Lai) because canonical_lane_key is only .lower() — which is exactly how
   OL's "Cat Lai" confirmation fails to link to Lonny's "HCMC" RFQ and a second
   row appears. Detect-only: the correct survivor depends on which row carries
   the real request thread, so the audit names BOTH ids.

Suite 1795 passed (+42), ruff clean. QC-INDEX now QC-001..QC-069.

CONFIRMED BACKLOG — not yet fixed, ranked (full detail in the audit run):
 HIGH  ingest.py:650  booking->request match via In-Reply-To/References takes
       the FIRST row in arbitrary stage order; a recycled thread can hand a NEW
       request an OLD booking. Verifier corrected the proposed fix: prefer the
       container+carrier scorer over in_reply_to, never let stage order decide.
 HIGH  ingest.py:845  lane-alias miss (HCMC vs Cat Lai) CREATES the duplicate
       row QC-069 now only detects. Needs a canonical alias map on both sides.
 HIGH  ingest.py:1509 additive carry-forward can append a second row with the
       same request_id; the tie-break can discard the preserved WIN.
 HIGH  core.py:1061   a same-day quote enriched by patch_carriers flips straight
       to Q&L with zero aging and leaves both pending buckets.
 HIGH  core.py:1019   decide_status returns has_send=False on SEND_NO_BOOKING,
       erasing the record that Lonny accepted.
 HIGH  ingest.py:368  request_date is stored as a UTC calendar date but every
       day bucket is an ET business day — a Friday-evening Pacific RFQ lands in
       the wrong report day (same class as the 2026-07-21 _et_date fix).
 HIGH  gen_email.py:836 Won-tile vs What-Happened contradiction (a further
       instance beyond the 2026-07-21 fix).
 HIGH  qc_selfheal.py:1248 status mutated without record_transition, so
       status_history contradicts the row's actual status.
 MED   9 further findings (aggregation, self-heal ordering, persistence
       atomicity, schema invariants). LOW 3.

## 2026-07-26 — Michael's timers: OL 3-biz-hour SLA + 24/72 win-loss (both sides)

Michael: "no need for track and trace" / "ol response time has to be 3 hours" /
"win loss timer is the 24/72 hours."

These are TWO DIFFERENT clocks, and keeping them separate is the whole point:

  * **3 BUSINESS hours = OL's response SLA.** Past it OL has breached and must
    be chased — but the row stays OPEN (PENDING_OL). It is NOT a loss.
  * **24h (72h when quoted/asked on a Friday ET) = the win/loss timer** that
    actually resolves the deal, on BOTH sides.

Collapsing them would re-bury live business as "lost" — precisely the
2026-07-24 defect. Flagged and kept distinct deliberately.

CHANGES:
- PENDING_HILMAR_LOSS_HOURS 48 → 24 (FRIDAY stays 72). SUPERSEDES the
  2026-07-14 "48 hours most" rule.
- PENDING_OL_LOSS_HOURS 48 → 24 / 72 Friday — the same win-loss timer now
  governs the OL side too.
- NEW PENDING_OL_SLA_BIZ_HOURS = 3 + core.pending_ol_overdue(), measured in
  BUSINESS hours (ET 8:30–17:30 Mon-Fri) via the existing biz_hours_between —
  the SAME function behind the report's "Time to Quote" column, so the SLA and
  the displayed wait can never disagree. Derived from live data, not guessed:
  the Jul-22 HCMC row shows "5.3h" for an 19-hour wall-clock gap, confirming
  the report already measures OL in business hours.
- Daily email "Pending OL Quote" now shows BUSINESS hours and flags red ⚠ on
  any row past the 3h SLA (was wall-clock with 8h/24h colour bands).
- NEW QC-068 (WARN, no heal) names every lane OL owes and its business-hours
  wait, every fire, so a blown SLA cannot sit silently in the dataset. Only OL
  sending the quote clears it.
- Parity test now locks all five timer constants across both trees.
- Tests updated for the 24/72 rule (test_auditfix_pending_hilmar_window,
  test_core); new tests pin that an SLA breach does NOT turn an open RFQ into a
  loss, that the weekend never counts against OL, and that the two timers stay
  independent numbers on independent clocks.

CLOSED: track-and-trace — Michael 2026-07-26 "no need for track and trace".
Removed from the open list; the client email keeps the honest "dates are as
quoted at booking, not live vessel tracking" disclaimer.

Suite 1753 passed, ruff clean.

## 2026-07-24 — ROOT CAUSE: PENDING_OL was unreachable; live RFQs stored as losses

Michael: "three requests mentioned only 2 ol responses … yet mention waiting
hilmar for the hcmc that doesn't show ol responded to or open" and then "your
quality control system is not functioning."

Both correct, and the same root cause. Found by reading the LIVE sent reports
(Jul-22 and Jul-23) against the code, then proving it by execution.

THE DEFECT (scripts/core.py + src/hilmar/core.py, decide_status):
An UNQUOTED row — Lonny sent an RFQ, OL has not answered YET — was classified
LOSS/NO_RESPONSE the instant it was ingested, with NO grace period whatsoever:

    if not quoted:
        if not response_timestamp:
            return StatusDecision("LOSS", False, False, "NO_RESPONSE", ...)

Two consequences, both proven:
1. **PENDING_OL was STRUCTURALLY UNREACHABLE.** Brute-forcing the full input
   space of decide_status produced PENDING_OL in 0 of 96 combinations — every
   PENDING return carried quoted=True, so pending_substate always resolved to
   PENDING_HILMAR. "⏳ PENDING OL (0) — awaiting OL quote / No activity" was
   not a coincidence in the data; the bucket could never be populated.
2. **Live open business was STORED as lost.** age_requests() runs decide_status
   over every row on every fire and writes r["status"], so tracking-data-v2.json
   persisted genuinely-open RFQs as LOSS/NO_RESPONSE. Nobody chased OL for them.

This is exactly the Jul-22 Oakland→HCMC (Cat Lai) 1-40' HC row Michael flagged:
requested Jul 22 3:42 PM PT, OL quoted it Jul 23 1:48 PM ET. At the Jul-22
report build it was genuinely awaiting OL — it should have read PENDING OL (1).
Instead it was filed a loss and appeared in NO bucket. (The "waiting Hilmar"
HCMC row he saw beside it was a DIFFERENT, older request — correctly displayed.)

THE FIX — an unanswered RFQ is open business, not a loss:
- New PENDING_OL_LOSS_HOURS = 48 / PENDING_OL_LOSS_HOURS_FRIDAY = 72 and
  pending_ol_stale(), mirroring the existing Hilmar-side window and anchored on
  Lonny's REQUEST time (there is no response yet, by definition).
- decide_status holds PENDING (quoted=False → PENDING_OL) until the window
  expires, then ages to NQ/NO_RESPONSE exactly as before.
- decide_status gained an optional request_timestamp kwarg, wired at all five
  production callers (ingest, merge_ingest, qc_selfheal, src/hilmar/ingest,
  src/hilmar/qc) with a request_date fallback.
- STRICTLY ADDITIVE: an undateable row keeps the old immediate-NQ behavior, so
  nothing can leak into permanent PENDING.

CROSS-SYSTEM IMPACT (checked before shipping):
- auto_chase_pending requires response_timestamp, which PENDING_OL rows lack —
  it will NOT start nudging Lonny about quotes OL has not sent. Verified.
- Win rate is unchanged: both NQ and PENDING are excluded from the denominator.
- The "No-Response Rate" KPI will DROP — correctly; it was inflated by live RFQs.
- PENDING OL will now show real open work to chase. That is the operational win.
- Both cores changed identically; test_core_parity locks the new constants.

QC — per Michael, "the daily qc needs to test new lines and data and be updated
every time to make a change":
- New QC-067 (ERROR + SELF-HEAL) re-tests that day's REAL rows on every fire:
  any unquoted row filed NO_RESPONSE while still inside the response window is
  restored to PENDING (quoted stays False → PENDING_OL), loss_reason cleared,
  and a status_history entry appended so the audit trail survives. decide_status
  fixes it at the source; QC-067 is the daily proof on live data and the catch
  for stale carry-forward, operator corrections, or any future regression.
- A test asserts QC-067 and decide_status agree exactly, so detector and state
  machine cannot drift apart and re-create a self-contradicting report.
- QC-INDEX updated (QC-001..QC-067); the governance ratchet already fails CI if
  an emitted check is undocumented or untested.

Suite 1744 passed (+23), coverage 91.34%, ruff clean.

## 2026-07-23 — QC-066: impossible request/outcome ordering (HCMC swallowed request)

Michael (Jul-23 report screenshot): 3 new requests, 2 OL responses, 2 status
changes — yet the new Jul-22 HCMC request (1-40' HC, no OL response) appeared in
NO pending bucket: PENDING OL read 0, and the only pending-Hilmar HCMC was the
OLD Jul-21 2-20' row (waiting 44.3h — a row that already WON Monday). Michael:
"your quality control system is not functioning."

He's right — none of the 65 checks validated the causal ordering of a row's
own events. Diagnosis (code-level; production data unreachable from this
session): Lonny re-uses Outlook threads for recurring lanes, and the merge /
carry-forward path can hand a NEW request a stale outcome recorded BEFORE the
ask existed — the row then sits in a terminal status, invisible to every
pending bucket, while the old row zombies in PENDING HILMAR.

New QC-066 (ERROR, detect-only): flags any row whose newest status_history
event (ET) predates its own request_date, and any report-day request in
terminal WIN/LOSS with no same-day-or-later event. Legacy empty-history rows
exempt. No auto-heal yet — the correct heal (split the row: fresh PENDING
request + the prior outcome under its original ask) gets automated after the
first live run confirms the exact shape. QC-INDEX updated; tests cover the
HCMC shape, clean flows, evening-ET boundary, stand_ rows. Suite 1721 passed.

NEXT (after tomorrow's fire): read QC-066's audit output naming the real rows,
then build the split-heal + the ingest-side guard that stops the inheritance at
the source.

## 2026-07-22 — Client "Active shipments" honesty: arrived/blank/degenerate rows out

Michael (Jul-22 client email screenshot): "active shipments is wrong.. you have
shipments that arrived months ago.. you have one with blank information" +
"you aren't tracking and tracing moves so you do not know what's active."

Both correct. _active_shipments filtered only by request recency — never by
arrival — and there is NO track-and-trace feed in this system, so "Active" was
an overclaim built on quoted dates.

Fix (gen_client_email):
- Section renamed "Booked shipments — upcoming and in transit" with an explicit
  disclaimer: dates are as QUOTED at booking, not live vessel tracking.
- Hard rule (Michael, same day: "for current shipments only those with eta's
  that haven't happened yet"): a row shows iff it has a quoted ETA that has not
  passed. No ETA or ETA past → out (also kills the 260928 blank row — no
  carrier/vessel/dates means no ETA). Degenerate origin→origin lanes
  ("Oakland → Oakland" mis-parse past _lane_resolved) also out.
- Regression tests lock all four exclusions + the disclaimer. Suite 1714 passed.

- Post-#114 review round: origin→origin guard MOVED into _lane_resolved (all
  six client sections now exclude it, not just Booked shipments); dashboard
  caption no longer claims Requests = Won+Q&L+NQ+Pending (Won is event-dated);
  dashboard passes a full ET datetime into report_business_day so the wee-hours
  rollback applies same as the email; combined PDF client part carries the
  renamed section + quoted-dates disclaimer. Suite 1716 passed.

Also verified this morning (Michael "it only sent the report for lonny"): the
staff email DID send fresh at 8:33 ET (request-id a97dc9d9, all 9 recipients,
combined PDF) and landed in his idealx.us inbox at 8:35 — 2 minutes behind
Lonny's lighter no-attachment copy. No pipeline defect; delivery lag.

OPEN — real track-and-trace: knowing what's ACTUALLY active needs a carrier
tracking integration (e.g. Terminal49/project44/carrier APIs) keyed off the
booking refs the tracker already holds. Needs Michael's go + provider choice.
Also still open: arrived-months-ago rows carrying RECENT request dates smells
like the known duplicate-row issue in production tracking-data-v2.json.

## 2026-07-21 — #112 review round 2: ET day boundary + dashboard/email KPI parity

Michael: "why are you not seeing these" — honest answer logged: per-diff reviews
can't see cross-file drift, and no parity test compared the email's day tile to
the dashboard's. Both structural gaps closed:

1. ET day boundary (review 🟡): _iso_date sliced the UTC calendar date, but the
   report day is an ET business day — a 9:30 PM EDT win is already "tomorrow"
   in UTC, so evening events fell into the wrong day bucket (and could misfire
   the new "booked a later day" note). New _et_date helper converts timestamps
   to ET before comparing; applied to ALL day comparisons together (_won_on,
   _has_dated_win, won_later, _today_events' requests/responses/status-changes)
   so sections shift as one and can't contradict each other. Date-only strings
   pass through untouched. Client email inherits via shared _today_events.
2. Dashboard divergence (review 🟣): gen_dashboard re-derived tdy_wins by
   request_date + current status — contradicting the email's event-dated Won
   tile in the SAME daily send (email 0 Won / dashboard 1 Won on the won-later
   shape). Dashboard day tiles now consume gen_email._today_summary — ONE
   bucketing source, drift impossible — and surface "booked a later day" too.

Tests: evening-boundary win (Mon 9:30 PM EDT counts as Monday, exactly once),
_et_date semantics, and a guard that gen_dashboard uses _today_summary and the
independent Won bucketing stays deleted. Suite 1712 passed.

## 2026-07-21 — Post-#111 review fixes: won-later orphan + two label/comment nits

Automated review on #111 (post-merge) found one real regression + two nits; all
three verified and fixed:
1. 🔴 A row REQUESTED on the report day whose →WIN transition is dated a
   DIFFERENT day (asked late Monday, confirmed Tuesday morning) vanished from
   every KPI bucket while still counting in total. Fix: surfaced as
   "booked a later day" on the Requests tile (new won_later field). The
   reviewer's own suggested fix (credit it to the request day's Won tile) was
   REJECTED — it double-counts the win across two day tiles and inflates the
   7-day sparkline; attribution stays exactly-once on the win's event day.
   Tests lock exactly-once + no-orphan.
2. Win-rate explainer said "That day: <period totals>" — period numbers labeled
   as one day. Now "This period:".
3. daily.yml env-block comment still described the deleted Friday-4:30 window
   branching. Rewritten: gate always emits previous.

Suite 1709 passed.

## 2026-07-21 — Day-KPI "Won" counts by win-event date (sent-report contradiction)

Michael, on the Jul-21 sent report (reporting Mon Jul 20): "firstly data
missing … NO.. CHECK YOUR REPORT." Verified from the actual sent email: "What
Happened — STATUS CHANGES" showed 2 wins on Jul 20 (Jul-16 request's MDOLX260963
booking confirmed + a same-day win via Lonny reply), while the day KPI tile said
"0 Won — Mon Jul 20 / 0 TEU won" — the report contradicted itself.

Root cause: gen_email._today_summary bucketed wins by request_date == report
day, so a win that HAPPENED on the report day for an older request was invisible
in the day tile (and in the 7-day win sparkline).

Fix: wins (and TEU won) now count →WIN status_history transitions dated the
report day — the same source as the Status Changes section, so the two can no
longer disagree. WIN rows with no dated →WIN transition fall back to
request_date bucketing (legacy rows), attributed exactly once. Requests / Q&L /
NQ / Pending stay request-date-bucketed (that day's intake by current status);
the KPI sub-line states the split. New tests/test_day_kpi_win_event_date.py
reproduces the Jul-20 shape. Suite 1707 passed.

NOTE: the Jul-21 send also still showed the old "today" labels — it fired at
07:22 ET, before #110 (prior-business-day labels) merged. Next fire carries the
new copy.

OPEN: the same sent report lists the won 2-20' HCMC row in "PENDING HILMAR"
while its win shows in Status Changes — suggests a duplicate row (request row
still PENDING + standalone WIN row) in production data. Needs the live
tracking-data-v2.json to confirm; not reproducible from this clone.

## 2026-07-21 — One daily fire at 8 AM for the prior day; label KPIs "prior business day"

Michael: "get rid of the recaps and just do daily at 8am est for the day before
and indicate the stats and kpis are for day before." Also: "throughout the report
you show the word today when it was actually yesterday."

SCHEDULE — collapsed to a SINGLE fire (supersedes the 2026-07-18 morning +
Friday-wrap-up cadence, which left Monday morning structurally empty):
- daily.yml: one fire, Mon-Fri ~8:07 AM ET (`7 12`/`7 13 * * 1-5`), always
  window=previous. The Friday 4:30 PM wrap-up crons + the gate's fire-time
  window branching are removed; the gate now always emits window=previous.
- Coverage is uniform and gap-free: every business day is reported exactly once,
  the next business morning (Mon→Fri, Tue→Mon, Wed→Tue, Thu→Wed, Fri→Thu). No
  more Monday-empty, no double-report.
- liveness.yml: dropped the Friday ~7:30 PM backstop; Mon-Fri ~9:30/11:30 AM
  backstops only; recovery always dispatches window=previous.
- Cloud PC: setup_cloudpc.ps1 back to ONE 8:07 AM Mon-Fri trigger;
  run_daily_laptop.cmd fixes HILMAR_REPORT_WINDOW=previous (no time-of-day
  branch). Sentry monitor unchanged (already `7 8 * * 1-5`).

LABELS — the report now says the KPIs are for the prior day, not "today":
- Staff email (gen_email.py): header sub-line "Reporting <day> — the prior
  business day"; "What Happened" + KPI sub-lines reworded off "today"; stale
  ~6 PM/"today" copy corrected; _report_date docstring rewritten (window=previous).
- Client email (gen_client_email.py): header "Activity for <day> (prior business
  day)"; KPI tiles + section titles dropped the false "today" ("Requests
  received", "Quotes delivered", "Bookings confirmed"); empty-state lines say
  "that day". Verified end-to-end: a Tuesday fire subjects/labels Monday.

Tests: fire-time-consistency rewritten for one fire (asserts the wrap-up is
GONE everywhere); cloud-PC trigger, liveness wiring, client-email label tests
updated. Full suite 1702 passed; ruff clean.

OPEN — flagged to Michael:
1. "Data missing" (screenshot of the client email): could not determine the
   specific missing shipment from the crop + no production data in this clone.
   Most likely cause is the client `_lane_resolved` filter, which silently drops
   any booking whose lane didn't parse (2026-07-14 "client sees only resolved
   shipments"). Need the specific shipment to confirm.
2. Weekly exec summary (Mon 5 AM) timing: with the Friday wrap-up gone, Friday's
   full PT-day activity isn't ingested until Monday's 8 AM fire — AFTER the
   5 AM weekly. The weekly may undercount Friday. Options: move the weekly later
   Monday, or have it refresh before building. Left as-is pending a decision.

## 2026-07-20 — Weekly "Carrier of the Week" bug + remove Caren from distribution

Two report fixes from Michael's review of the Jul 20 weekly summary.

**Carrier of the Week crowned a 0-win carrier.** The Jul 13-17 summary named
CMA CGM "🏆 Carrier of the Week" with 6 quotes / 0 wins / 0.0% win rate — while
the week HAD a win (1 win, 4 TEU) that a different carrier took. Root cause in
`gen_weekly_summary.carrier_of_week`: candidates were filtered to `quotes >= 2`
BEFORE ranking, which benched the actual winner (its only quote that week was
the winning one) and left only 0-win carriers, then crowned the most-active of
those. Fix:
- A carrier that WON any deal always qualifies (a win is the strongest signal),
  so the min-quote floor can never bench the winner.
- Rank by wins, then TEU won, then win rate, then quotes.
- Win attribution now credits `carrier_won` (not `carrier_quoted`) on WIN rows.
- On a genuine no-win week the render relabels the box "📊 Most Active Carrier"
  so a 0-win carrier is never literally called the week's winner.
- Added `tests/test_weekly_carrier_of_week.py` (the function had ZERO coverage —
  which is how this shipped): reproduces the CMA scenario + locks the fix, unit
  and end-to-end. The actual winner for that week is shown in the report's own
  "Top 3 Winning Lanes (by TEU)" table (carrier + lane + 4 TEU).

**Removed Caren Tobel from the reports.** Michael: "remove caren tobel from all
reports." Removed `caren.tobel@ol-usa.com` from `config.json`
`distribution.full_list` (10 → 9 recipients). She REMAINS a sender exclusion in
`ingest_scope.mailboxes_excluded` — that filters her rate-desk emails out of
ingest and is unrelated to who receives the report. QC-022 already allows 8-12
recipients so 9 is valid (its "exactly 10" comment was stale and is corrected).
README + a new `tests/test_distribution_recipients.py` lock the change.

## 2026-07-21 — Integrity gate: tell a fresh send from an idempotency-suppressed no-send

Michael: "no daily tracker went out yesterday." Diagnosed from the run logs:
Monday Jul 20's fire ran clean but shipped NOTHING — it reported Friday Jul 17
(window=previous), found `sent-2026-07-17.flag` (written 2026-07-18 09:45 ET by a
stray weekend fire — the old "8 AM every day" schedule was briefly still live
that Saturday), and idempotency-suppressed the send. Yet
`assert_fire_integrity` printed "✅ fresh report shipped" and the heartbeat
reported success — a silent no-send read as green.

Root cause: the gate only proved the report-day sent-flag EXISTS, not that THIS
fire wrote it. A flag from any earlier fire satisfied it.

Fix (`deploy/assert_fire_integrity.py`):
- New `send_freshness(today)` parses the flag's `Sent <date> …` lines and
  classifies the fire as **fresh** (a send dated today), **suppressed** (flag
  exists but its newest send predates today → shipped nothing new), or
  **absent** (no flag — still a hard violation, unchanged).
- `main()` now prints the honest outcome: a suppressed no-send reads
  "ℹ️ Fire ran clean but shipped NOTHING NEW …", never "fresh report shipped".
- Suppression is NOT failed — it's legitimate (e.g. Monday re-reporting a Friday
  already sent by the wrap-up) — so it doesn't false-alarm; it's just truthful.
- `check_integrity`'s contract is unchanged; new `tests/test_fire_integrity_freshness.py`
  reproduces the Jul-20 case. Suite 1702 passed.

Also: forced a production-fire on 2026-07-21 07:22 ET to ship Monday's tracker
(reported Monday, wrote a fresh `sent-2026-07-20.flag`, full 9-recipient list +
client copy).

OPEN — flagged to Michael, needs a decision (not code-blocked): under the Friday
4:30 PM wrap-up design, Monday's 8 AM fire reports Friday, which the wrap-up
already sent Friday evening — so **Monday morning is structurally a no-send every
week**. If a Monday recap of Friday is wanted, Monday must force past the
wrap-up flag (a deliberate re-send).

## 2026-07-18 — Close the Thursday gap: add a Friday morning fire

Michael, on the coverage note in the prior entry: "no incorrect — run a friday
morning for thursday then friday night a wrap up." The prior schedule dropped
Thursday's own daily and double-reported Friday. This fixes both by making the
MORNING fire run **Mon-Fri** (not Mon-Thu) and keeping the Friday evening
wrap-up.

WHAT CHANGED (supersedes the coverage design in the 2026-07-16 entry below):
- **Morning fire is now Mon-Fri ~8:07 AM ET** → each reports the PRIOR business
  day (Mon→Fri, Tue→Mon, Wed→Tue, Thu→Wed, **Fri→Thu**). The Friday morning fire
  closes the Thursday gap — Thursday now gets its own daily section.
- **Friday ~4:30 PM ET wrap-up** unchanged → reports Friday itself
  (window=current), still feeding the Monday 5 AM weekly.
- **The report window now keys off the fire TIME, not the day-of-week**: morning
  = previous, evening = current. Friday has one of each, so day alone can't
  decide — the gate reads the cron's hour (manual dispatch / the Cloud PC read
  the current ET hour: before noon = previous, noon-on = current).
- Coverage is now gap-free: every business day gets exactly one morning daily
  (Mon→Fri's Friday, Tue→Mon, …, Fri→Thu) and Friday also gets its evening
  wrap-up. Monday's morning fire still reports Friday as its prior business day
  (Friday's numbers land both Friday evening and Monday morning) — intended, per
  "friday night a wrap up."

SURFACES REALIGNED (all guarded by test_fire_time_consistency):
- daily.yml → morning crons `7 12`/`7 13 * * 1-5`; gate window by cron hour.
- Sentry monitor → `7 8 * * 1-5` (Mon-Fri; the morning fire now covers Friday,
  so Sentry can too — only the 4:30 PM wrap-up stays liveness-covered).
- liveness.yml → morning backstops `30 13`/`30 15 * * 1-5`; recovery guard is a
  flat "wait past 10:00 ET" (was Friday-only 17:00), since Friday now has a
  morning fire to recover; the dispatched fire's window is inferred from the ET
  hour so a morning recovery reports the prior day and an evening recovery
  reports Friday.
- Cloud PC (setup_cloudpc.ps1) → morning trigger DaysOfWeek adds Friday;
  run_daily_laptop.cmd picks the window by HOUR, not weekday.
- Tests updated: fire-time consistency, sentry schedule, cloud-PC triggers,
  liveness wiring. Full suite green.

## 2026-07-16 — Final fire cadence: Mon-Thu 8 AM / Fri 4:30 PM + Mon 5 AM weekly

Michael: "monday through thursday at 8am; friday at 430pm est … no weekend
emails" + "exec summary monday 5am … mark for previous week." Supersedes the
earlier same-day "8 AM every day" move.

DAILY (daily.yml) — two fire times, NO weekend fire:
- **Mon-Thu ~8:07 AM ET** → reports the PRIOR business day (window=previous:
  Mon→Fri, Tue→Mon, Wed→Tue, Thu→Wed).
- **Fri ~4:30 PM ET** → reports FRIDAY ITSELF (window=current), so the week's
  last day is captured Friday for the Monday weekly.
- The gate now emits the report window per ET day-of-week (Friday=current,
  else=previous) as a job output, consumed by production-fire's
  HILMAR_REPORT_WINDOW. crons: `7 12`/`7 13 * * 1-4` (8 AM) + `30 20`/`30 21
  * * 5` (4:30 PM), both DST seasons.
- COVERAGE NOTE: with 5 fires and no weekend email, **Thursday's dedicated
  day-over-day callout is the one casualty** — Thu 8 AM reports Wednesday, Fri
  4:30 reports Friday, so Thursday never gets its own daily section (its
  activity is still in the period rollups + the weekly). Flagged to Michael;
  a Friday 8 AM fire (reporting Thu) would close it if wanted.

WEEKLY (new weekly.yml + gen_weekly_summary) — **Monday ~5:07 AM ET**, builds
the exec summary for the PREVIOUS (just-completed) week, labeled "Previous
week: …", emailed to the INTERNAL staff list only (never the client). The
gate moved Friday→Monday; the <6 AM wee-hours rollback was removed (a real
5 AM Monday fire must not roll back to Sunday); week bounds anchor on today-7.

MONITORS + FALLBACK, all realigned + guarded by test_fire_time_consistency
(rewritten for the two-time schedule; FIRE times are data now):
- Sentry monitor → `7 8 * * 1-4` (Mon-Thu 8 AM; Friday covered by liveness,
  since one crontab can't express two daily times).
- liveness.yml → Mon-Thu ~9:30/11:30 AM + Fri ~7:30 PM backstops, weekday-only,
  day-aware recovery gate (Fri waits past 17:00, else 10:00), weekend-skip
  restored.
- Cloud PC (setup_cloudpc.ps1) → two triggers (Mon-Thu 8:07 AM + Fri 4:30 PM);
  run_daily_laptop.cmd picks HILMAR_REPORT_WINDOW by day-of-week.
- Tests: fire-time-consistency (two times + weekly), sentry-schedule,
  cloudpc-trigger, liveness-wiring, weekly-gate all updated. Suite 1687 passed;
  ruff clean; all workflows parse; weekly renders + labels the prior week.

## 2026-07-16 — Fire moved to 8 AM ET, reports the PRIOR business day

Michael: "rework the system to fire off at 8am new york time every day and
report on the day prior — rather than end of day for that day." The fire was a
~6 PM ET same-day evening fire (HILMAR_REPORT_WINDOW=current); it is now an
~8:07 AM ET MORNING fire that reports the prior business day
(HILMAR_REPORT_WINDOW=previous). core.report_business_day already had the
"previous" window (Tue→Mon, Sat/Sun/Mon→Fri) — this flips which one the fire
uses; core.py's default stays "current".

All FOUR fire-time surfaces moved together (test_fire_time_consistency guards
them as one canonical time — now FIRE_ET_HOUR=8):
- **daily.yml**: crons `7 12`/`7 13 * * *` (8:07 AM EDT/EST), gate keys on UTC
  hour 12/13, HILMAR_REPORT_WINDOW=previous. Runs EVERY calendar day now (was
  Mon-Fri): Saturday's fire sends Friday's report, Sun/Mon fires no-op on the
  report-day sent-flag → each business day reported once, the morning after
  (Friday's lands Saturday).
- **sentry_setup._MONITOR_CONFIG**: schedule `7 8 * * *`, tz America/New_York,
  margin unchanged (290 min → ~12:57 PM ET deadline).
- **liveness.yml**: backstop ticks `30 13`/`30 15 * * *` (~9:30/11:30 AM ET),
  daily; weekend-skip removed; recovery gate now "before 10:00 ET → wait".
- **Cloud PC (fallback)**: setup_cloudpc.ps1 trigger `-Daily -At 8:07am`;
  run_daily_laptop.cmd sets HILMAR_REPORT_WINDOW=previous.
- Tests updated (fire-time consistency, sentry schedule, cloudpc trigger,
  liveness wiring) + docs/SENTRY.md + the hilmar-daily-tracker skill. 1685 pass.

OPEN — flagged to Michael (needs a decision, not code-blocked):
1. Weekend cadence: implemented literally ("every day") — Friday's report
   arrives SATURDAY morning. If no weekend emails are wanted, switch daily.yml +
   liveness back to Mon-Fri (then Friday's report arrives Monday).
2. Weekly exec-summary (gen_weekly_summary) still gates on the Friday fire, so
   under the morning cadence it now covers Mon-Thu + an empty Friday. To ride
   the full week it should move to the Saturday fire (gate on report-day ==
   Friday). Left unchanged pending #1's answer.

## 2026-07-16 — Jul-15 staff email vanished in Exchange + Sentry env root cause

**Missing staff report (Michael: "only lonny got a report").** Verified from
the Jul-15 fire job log: the staff email WAS sent — Graph accepted it
(request-id ab7a24da, 10 recipients, combined PDF attached) 2 seconds before
the client email (request-id 9f6c1d4f) that DID deliver. The loss is
DOWNSTREAM of Graph (Exchange transport/quarantine/junk), not the pipeline —
integrity honestly passed because the send really happened. Recovery: forced
production-fire dispatched 2026-07-16 10:36 AM ET; staff email re-sent
(request-id b9cdb5b5, all 10, --force past the flag+guard); client email also
sent (its Jul-16 flag didn't exist yet — Lonny gets his one update early
today). CONSEQUENCE: today's sent-flags now exist, so the 6 PM scheduled fire
would NO-OP both sends — a forced evening dispatch (~6:25 PM ET) covers
tonight; back to normal tomorrow. If the morning re-send ALSO fails to arrive,
the cause is an Exchange-side rule (check quarantine.microsoft.com for the
"Daily Shipment Tracker Update" subject) — not fixable from this repo.

**Sentry HILMAR-DAILY-TRACKER-A — TRUE root cause (26 daily pages).** The
Jul-15 heartbeat check-in verifiably reached Sentry ("OK Sentry cron check-in
sent", 7:11 PM ET) yet the monitor still paged at 10:57 PM ET. Sentry Crons
alerts PER ENVIRONMENT: _detect_environment only recognized the Cloud PC
hostname as 'production', so post-cutover check-ins landed in 'manual' while
the Cloud-PC-era 'production' environment sat check-in-less and paged every
weekday. Fix: GITHUB_ACTIONS=true now maps to 'production' (GH Actions IS the
production host); ensure_monitor_schedule now DETECTS orphaned monitor
environments and prints the manual fix — it deliberately does NOT auto-delete
monitoring config (operator decision; an auto-prune draft was rejected in
review 2026-07-16). OPERATOR ACTION (Michael, one-time): Sentry UI → Crons →
hilmar-daily-pipeline → delete the stale environment(s) (e.g. 'manual') once
the first 'production' check-in lands after this deploys — otherwise the
orphan keeps paging. +6 tests (tests/test_sentry_env_root_fix.py).

**Also observed in the Jul-15/16 fire logs (separate issues):** Anthropic API
credit balance exhausted — insights LLM narrative skipped, rule-based only
(needs billing top-up; feeds task #21); ol-quote-tracker-prod sync hit a
30s ReadTimeout on the Jul-16 morning fire (best-effort, pipeline continued).

## 2026-07-15 — "Cloud PC fired"?? No — misleading hardcoded workflow name

Michael (seeing the run list): "we turned off cloud pc didn't we... what's
going on?????" Verified: the Cloud PC did NOT fire. The 2026-07-14 heartbeat
run (23:09 UTC) was dispatched by **github-actions[bot]** — daily.yml's own
token — i.e. the GitHub Actions production fire. A Cloud PC dispatch would
show actor IdealX-dev (its PAT). The alarm came from heartbeat.yml's
HARDCODED display name "Heartbeat — Cloud PC fired" (pre-cutover relic),
which every heartbeat run displays regardless of host.

- Renamed the workflow to **"Heartbeat — daily fire"** (host is the `host`
  input, echoed in the log; the dispatch actor identifies the host). liveness
  queries by FILENAME (`--workflow=heartbeat.yml`), so nothing breaks.
- Corrected the record in sentry_setup.heartbeat_checkin docstring +
  docs/SENTRY.md: yesterday's HILMAR-DAILY-TRACKER-A diagnosis said "the
  Cloud PC fired" — wrong inference from this same name. The fire host was
  GitHub Actions; the false-page mechanism and the #101 fix stand unchanged
  (the in-process check-in failed to register from ANY host; the heartbeat
  emitter covers all hosts).

## 2026-07-15 — ONE combined daily PDF replaces the 3-email model (staff list)

Michael: "the 3 reports should be made into one and emailed to everyone as
pdfs." Confirmed via question: merge the 3 daily emails; "everyone" = the
10-recipient staff list (distribution.full_list). Lonny's client email is
UNCHANGED and separate — QC-065 boundary holds.

- New **scripts/gen_combined_pdf.py** → reports/hilmar-combined.pdf:
  Part 1 = the 6-page tracker report (gen_pdf builders, imported not copied);
  Part 2 = client service update copy (gen_client_email buckets + the
  _lane_resolved filter, imported — the PDF can never show more than the
  client email does); Part 3 = systems audit (gen_improvements_report
  collectors: red flags / observations / suggestions + QC status line).
- **daily.yml send step**: audit generated FIRST, then the combined PDF; ONE
  staff email attaches dashboard + hilmar-combined.pdf. The separate audit
  email is gone. FALLBACK: if gen_combined_pdf fails, the run degrades to the
  old model (hilmar-report.pdf attached + separate audit email) so a rendering
  bug can never cost the daily deliverable.
- gen_pdf/gen_improvements_report/gen_client_email untouched (source of the
  parts + the fallback path + Lonny's email).
- +6 tests (tests/test_gen_combined_pdf.py): end-to-end build with all three
  part markers, missing-data exit, resolved-lane inheritance, and daily.yml
  contracts (ordering, fallback wiring, client-email step untouched).
- Verified: 9-page sample rendered from the golden fixture; sample sent to
  Michael in-session.
- NOTE: the Cloud PC wrapper (fallback fire host) still sends the old 3-email
  model — updating its .cmd chain needs a Windows validation pass; flagged as
  follow-up.

## 2026-07-15 — Sentry cron "missed check-in" false page (HILMAR-DAILY-TRACKER-A)

Michael forwarded the Sentry alert "Cron failure: hilmar-daily-pipeline —
missed check-in", seen 25×, last 10:57 PM ET (= 6:07 PM + the 290-min margin).
**Diagnosis: false page — the report DID ship** (heartbeat.yml succeeded that
day), but the Sentry cron check-in never reached Sentry.

Root cause: the check-in was emitted ONLY from inside `run_pipeline.py`
(start/finish), which couples the monitor to that code path's Sentry init on
whichever host fires. On a Cloud-PC-fired day whose in-process check-in didn't
land, the monitor paged even though the deliverable shipped — while liveness
(which reads heartbeat.yml) stayed correctly green. The two observability
systems disagreed because they read different signals.

Root fix — tie the cron check-in to the SAME host-agnostic signal liveness
trusts (the heartbeat):
- New `sentry_setup.heartbeat_checkin(success)` — a single terminal `ok`/`error`
  check-in for the `hilmar-daily-pipeline` monitor, reusing MONITOR_SLUG +
  _MONITOR_CONFIG, self-healing the schedule, best-effort (never raises).
- New `scripts/sentry_cron_checkin.py` CLI (always exits 0).
- `heartbeat.yml` now runs it (continue-on-error on all added steps, so a
  Sentry hiccup can never fail the heartbeat job / liveness signal). Every host
  that heartbeats now also checks in → the monitor pages only on a genuine
  no-fire day.
- In-pipeline emitter A kept (belt-and-suspenders + timing detail).
- Docs: docs/SENTRY.md rewritten to document the two emitters. +7 tests
  (tests/test_sentry_cron_checkin.py).

NOT verifiable from this session (no SENTRY_DSN / Sentry access here): the
end-to-end check-in round-trip. Unit-tested with a mocked SDK; the live
round-trip confirms on the next real fire's heartbeat.

## 2026-07-15 — Status-change transition read backwards ("PENDING HILMAR → QUOTED")

Michael (screenshot, two rows): "status is waiting ol quote then after quote
is pending hilmar response — check your steps." The staff-email STATUS CHANGES
table rendered a rate response as **PENDING HILMAR → QUOTED**, which is the
lifecycle inverted. Correct reading: **PENDING OL → PENDING HILMAR** (RFQ was
waiting on OL to quote → OL delivered a rate → ball now in Hilmar's court).

Root cause (display only — the data was fine): `gen_email._sc_pill` resolved
the transition's *from* end from the row's CURRENT substate. A just-quoted row
is `quoted=True`, so `pending_substate` returns PENDING_HILMAR — which describes
where the row is NOW, mislabeling the BEFORE end. And the *to* end printed the
raw internal enum `QUOTED`. The function's own docstring already described the
correct rule ("a move INTO QUOTED means was PENDING_OL"), but the `cur ==
PENDING` branch short-circuited before it ran.

Fix:
- Promoted `_sc_pill` → module-level **`_status_change_pill`** (now unit-
  testable) and rewrote it to resolve each end from the TRANSITION DIRECTION,
  not the row's present state: a QUOTED end → PENDING_HILMAR (post-quote wait);
  a PENDING-into-QUOTED end → PENDING_OL; a PENDING-into-outcome end →
  PENDING_HILMAR if quoted else PENDING_OL (an NQ OL never answered).
- Raw `status_history` unchanged (`to="QUOTED"` still the internal marker
  QC-019 keys on) — this was purely a label bug on one surface.
- New **tests/test_auditfix_status_change_direction.py** (6 tests) pins
  PENDING OL → PENDING HILMAR for a rate response and the correct from-end for
  WIN / NQ-loss / Q&L-loss. Suite 1666 passed; ruff clean.
- Only live surface affected: `gen_email.py` (staff email). Dashboard diff
  records top-level status changes only (a rate response stays PENDING→PENDING,
  so it never appeared there); `gen_email_new.py` renders raw from/to but is
  dead (referenced by nothing). Both left as-is.

## 2026-07-14 — Pending-Hilmar decision window: 48h clock, 72h if Friday

Michael: "pending hilmar is 48 hours most.. then it's lost if we don't win..
except fridays.. it's 72 hours." Two Friday-quoted rows sat at 73–78h still
PENDING because the prior rule (2026-06-04) aged QUOTED rows on 24 BUSINESS
hours with a Friday→Tuesday-18:00-ET carve-out (~101h for a Friday quote).
Superseded for quoted rows:
- **core.pending_hilmar_stale(resp_dt, now)** (new; mirrored byte-consistently
  into src/hilmar/core.py, parity-tested): pure CLOCK hours from the OL quote
  — >=48h → Q&L, >=72h when OL quoted on a Friday (ET) so the weekend lands
  Lonny Monday. New constants PENDING_HILMAR_LOSS_HOURS=48 /
  PENDING_HILMAR_LOSS_HOURS_FRIDAY=72.
- **decide_status** quote-window aging now calls it (was is_business_stale +
  PENDING_WINDOW_HOURS). The SEND-signal aging is deliberately unchanged
  (still is_business_stale; PENDING_WINDOW_HOURS=24 retained for it).
- **qc_selfheal QC-007** + **gen_improvements_report** aging both switched to
  pending_hilmar_stale so audit + QC + state machine agree. QC-INDEX QC-007
  row updated.
- Legacy 24h/Tuesday tests updated to the new rule; +7 new window tests
  (incl. the exact Jul-13 screenshot rows aging out). Suite 1660 passed.

NOTE for Michael: the rule is implemented literally — only FRIDAY quotes get
72h. A Thursday-evening quote still ages at 48h (lands Saturday). Say the word
if Thursday should also carry the weekend.

## 2026-07-14 — Zero unresolved/unmapped lanes (run 29292014093 root cause)

Michael: "your qc doesn't work and isn't working properly" + "there should be
zero unresolved lanes / unmapped." Production log:
`LANE-DIAG stand_260905: unresolved — pod=Unknown; pdf_fields_present=no`.
Root cause: the row carried the LITERAL string "Unknown" in `pod`, persisted
in tracking-data-v2.json from a fire BEFORE the pdf_parser `_clean_port`
source-fix. It re-derived unresolved every fire (the recurring drift), rendered
"Lane unresolved" in the staff email AND the now-live client email to Lonny,
and produced an "Unmapped" trade-region row — while QC-015 printed GREEN
"within tolerance". Four structural fixes, all at the root; gates green
(compileall, ruff, pytest 1652 passed — was 1642 + 10 new tests).

- **FIX 1 — heal the poisoned literal on load** (`scripts/qc_selfheal.py`
  phase_3_entries; mirrored byte-consistently into `src/hilmar/qc.py`
  phase_3_entries per QC-040 spirit). New `_GARBAGE_PLACEHOLDERS` frozenset +
  `_is_placeholder(v)`. For every request, a placeholder literal
  ("unknown"/"n/a"/"na"/"none"/"null"/"tbd"/"-"/"—"/"") in `pod`/`destination`/
  `origin` (case-insensitive) is coerced to None BEFORE lane derivation, so it
  can never display, defeat `patch_carriers._dest_from_row_pod`/`_dest_from_pod`
  (a truthy "Unknown" looked resolved), or bucket the row "Unmapped". Logs a
  `log.fix` naming the row id + field. Kills the drift at the source.
- **FIX 2 — client report never renders an unresolved row**
  (`scripts/gen_client_email.py`). New module-level `_lane_resolved(r)`; every
  section bucket in `_client_sections` and `_active_shipments` filters through
  it. A row with no displayable lane (placeholder destination AND no real lane,
  or lane == "Lane unresolved") is excluded from ALL client sections. Empty
  sections use the existing collapse. Lonny sees only resolved shipments.
- **FIX 3 — QC-015 fails LOUDLY on a rendered unresolved row**
  (`scripts/qc_selfheal.py`). Contract rewritten: ERRORs when ANY unresolved
  row WOULD render client-facing — a WIN inside the client email's 14-day
  active-shipments window (mirrors `gen_client_email._active_shipments`) OR any
  today-dated staff-section row. "within tolerance" GREEN is reachable ONLY
  when every unresolved row is a non-rendered historical-tail row; the count-
  based WARN/ERROR map tiers survive for that tail. Error names the offending
  row ids + pod/dest/subject. QC-INDEX.md row updated.
- **FIX 4 — exclude unresolved-destination rows from the CLIENT trade-region
  rollup** (`scripts/core.py` + `scripts/gen_pdf.py`). New
  `is_unresolved_destination(dest)` + `aggregate_trade_regions(...,
  include_unresolved=True)`. Default keeps rows (STAFF/QC totals reconcile to
  summary exactly as before); gen_pdf (client-facing) passes
  `include_unresolved=False` so a healed None/placeholder destination never
  renders a mystery "Unmapped" region, reconciled by a "+N pending lane
  assignment" footnote. gen_email (staff) and gen_dashboard (internal)
  intentionally keep the reconciling default — staff MAY surface the needs-lane
  count.

DEFERRED BY DESIGN: stand_260905's TRUE lane is genuinely underivable from
in-window data — a separate operator task, out of scope. It correctly remains
unresolved in the STAFF view; it no longer appears in the CLIENT email/PDF, and
QC-015 now ERRORs on it so the operator is flagged to assign the real lane.

## 2026-07-12 — Client report GO-LIVE (operator decision)

Michael Deitchman approved go-live via the session decision prompt
(option "Client report go-live"), after explicitly declining the request
to add lupfold@hilmaringredients.com to the internal staff full_list
(that would have sent OL's negotiation analytics to the client; refused,
QC-065 continues to enforce the separation). One-line reversible config
flip: client_report.enabled false -> true. From the next production fire
Lonny receives the redesigned client service report daily (cc
michael.deitchman@ol-usa.com, own client-sent idempotency flag + mailbox
guard); samples to sample_to stop. Rollback = flip enabled back to false.
Gate-pinning test updated to lock the LIVE state.

## 2026-07-12 — Session: four fixes from the Friday-evening fire (run 29174327034)

Michael's review of the Friday-evening production fire (run 29174327034,
~8:50 PM ET — the runner's UTC clock already into Saturday) surfaced four
defects. All four fixed at the root; gates green (compileall, ruff, pytest
1641 passed — was 1610 + 30 new tests + 1 net in test_client_email.py).

- **Client email subject report-day derivation hardened**
  (`scripts/gen_client_email.py`): the sample's subject dated the wrong
  day. `build_subject` and `build_body` each read the wall clock
  SEPARATELY and neither accepted an injected instant, so the subject
  could drift from the body and the UTC-vs-ET shift around midnight was
  untestable. Both now derive from ONE aware instant (new `_now_et`
  helper, `now=` keyword; `main()` passes a single shared instant)
  through `gen_email._report_date → core.report_business_day` — the exact
  staff-email path (wee-hours rollback + weekend→Friday). Pinned by
  tests: evening-ET/next-day-UTC, 00:40 ET wee-hours, weekend roll, and
  staff-email parity. CALENDAR NOTE, logged honestly: in the proleptic
  Gregorian calendar Python uses, 2026-07-11 is a Saturday (Friday that
  week is 2026-07-10), so the fire instant Michael reported as "Friday
  Jul 11" evaluates as a Saturday-ET run and the weekend roll (Sat→Fri
  Jul 10) explains the "(Jul 10, 2026)" subject; tests pin the intended
  scenario shapes on the real-calendar dates.
- **Weekly summary Friday gate is now ET + injectable**
  (`scripts/gen_weekly_summary.py`): the gate is evaluated on the
  America/New_York fire day derived from one aware instant (new
  `_fire_day_et` + `should_generate`), never the runner's UTC/local
  date, and honors the same midnight–6 AM ET wee-hours rollback as
  `core.report_business_day` (a 1 AM ET Saturday run is Friday's
  very-late fire and now generates instead of skipping). `--force`
  behavior kept; `main(argv, now)` is test-injectable. Tests: Fri-evening
  ET/Sat-UTC must not skip, actual Saturday ET skips, force overrides.
- **POD literal "Unknown" + PASS 2b short-circuit**
  (`scripts/patch_carriers.py`, `scripts/pdf_parser.py`): the run log had
  NO LANE-DIAG line for stand_260905 because `if not parsed: continue`
  skipped the PASS 2b block (and its diagnostic) for every row whose
  bodies parse to nothing. PASS 1/2 now run under `if parsed:` and PASS
  2b + LANE-DIAG run for ALL rows (r["pod"] can already sit on the row
  from a prior fire). `_dest_from_pod` explicitly treats
  "Unknown"/"unknown"/"" as absent; new `_dest_from_row_pod` also tries
  the parse's POD-shaped aliases (port_of_discharge/discharge_port/
  destination_port/dest_port — "pol" excluded, that's the origin). At
  the source, `pdf_parser.parse_booking_pdf` (new `_clean_port`) never
  emits the literal "Unknown" as a POL/POD value — a placeholder
  "UNKNOWN" cell title-cased to exactly that string. Tests: placeholder
  variants → None, no-parsed-still-diagnoses (capsys LANE-DIAG), lane
  recovery from the row's own pod, pdf_parser synthetic-text fixtures.
- **Client reply-speed metric moved to the Pacific business window** —
  trigger: Michael's timezone report, 2026-07-11: "lonny is uswc and we
  are usec". `core.biz_hours_between` (8:30–17:30 ET) is the STAFF desk
  SLA and is unchanged for all callers; the client email's narrative now
  reflects Lonny's experienced wait. New `core.biz_hours_between_pt`
  (8:30–17:30 America/Los_Angeles, Mon–Fri, DST-safe) shares the loop via
  `_biz_hours_between_window(tz, win_start, win_end)`; **paired change
  mirrored byte-for-byte into `src/hilmar/core.py`** (QC-040; a test now
  locks source parity via inspect.getsource). `gen_client_email`
  narrative computes request→response PT-window hours for TODAY's quotes
  (never the stored turnaround_biz_hours) plus the same-PT-calendar-day
  share: "… — all the same business day (average 1.4 business hours,
  Pacific)"; parenthetical omitted when no timestamps.
  `gen_manual.METRIC_DEFINITIONS`: "Time to Quote" now states the ET desk
  window; new "Client reply time (PT)" entry (drift-guard tests green).
  Cross-coast expected values verified against the real constants:
  request 2026-07-08T23:30Z (4:30 PM PT / 7:30 PM ET Wed) → response
  2026-07-09T12:45Z (5:45 AM PT / 8:45 AM ET Thu) gives ET-window
  **0.25 h** (Wed after ET close; Thu 8:30→8:45) and PT-window **1.0 h**
  (Wed 4:30→5:30 PM PT; Thu before PT open); same-day share false for
  that pair, true for a mid-day pair.
- New tests: `tests/test_auditfix_fri_evening_fire_tz.py` (30);
  `tests/test_client_email.py` narrative tests rewritten for the PT
  metric (+1 net). Untouched per scope: outlook_send.py, config.json,
  QC-065 constants, workflows, gen_email.py staff Time-to-Quote.

## 2026-07-11 — Session: client daily email redesigned as a premium service update

Michael reviewed the client-facing daily sample and called it "terrible" —
`scripts/gen_client_email.py` rebuilt as a premium logistics service update
for Lonny Upfold (Hilmar Ingredients). All safety invariants preserved:
same artifacts (`reports/client-email-{body.html,subject.txt}`), same subject
format, same `--data/--out-dir/--config` CLI, QC-065 leak scan clean, CID
logo, inline-styles/solid-header Outlook rules, shared gen_email helpers
(escape-once, Windows-portable strftime, report-day math). gen_email.py,
outlook_send.py, config.json, QC-065 constants, and workflows untouched.

Shipped (working tree, this branch):
- **Hero KPI strip** — 4 tiles (Requests received / Quotes delivered /
  Bookings confirmed / Awaiting your decision, each count + TEU) reusing
  `gen_email._kpi_card`, so the td.hx-kpi display:block full-width mobile
  stacking is shared code, not a copy (never inline-block/50%).
- **Service narrative** — one line under the tiles ("We received 3 rate
  requests and returned 3 quotes (average 1.4 business hours); 1 booking
  confirmed."); the avg comes from today's quoted rows'
  `turnaround_biz_hours` and the parenthetical is omitted when absent.
- **Active shipments** (new section) — WIN rows from the last 14 days
  (request_date, fallback response_timestamp): Lane · Carrier · Booking ref
  (mdolx_ref, else "Confirmation to follow") · Vessel (vessel_voyage) ·
  ETD · ETA · Doc cutoff, sorted by ETD ascending. stand_* bookings included
  here (the honest client-facing event).
- **Upcoming cutoffs callout** (new) — amber box listing active shipments
  with doc_cutoff (fallback: etd_offered → "vessel departs") within the next
  7 days; dates parsed defensively, unparseable skipped, box hidden when
  empty.
- **Empty-section collapse** — zero-row daily sections render one friendly
  line ("No new rate requests today.") instead of an empty table; "Awaiting
  your decision" always tables when it has rows (client's action list). A
  fully quiet day still composes: tiles (zeros) + narrative + footer.
- **Polish** — header subtitle tightened to "Prepared by OL-USA · <day> ·
  Updated <time> ET"; alternating row striping (inline bgcolor, Outlook-safe);
  nowrap navy headers; footer adds "Questions about a specific shipment?
  Reply with the booking reference."
- **Tests**: `tests/test_client_email.py` 20 → 29 (hero tile counts,
  active-shipments window/sort/refs, cutoff in/out of horizon + junk dates,
  quiet-day collapse with leak scan, mobile-CSS regression guard pinning
  display:block and banning inline-block/50%, subject pinned exactly).
  Full suite: 1610 passed.
- NOT done deliberately: production `PROJECT HILMAR/scripts/` mirror copy
  (QC-040) — this session works the repo tree only; mirror on deploy.

## 2026-07-11 — Session: insights engine wired into the production pipeline

Implements the "Insights engine wired" line item announced under 2026-07-10
(the modules existed since 2026-04/05 per docs/INSIGHTS-DESIGN.md but were
only reachable from the dormant src/hilmar/orchestrator.py path — the
scripts/ pipeline that actually fires daily never invoked them).

Shipped (working tree, this branch):
- **`scripts/gen_insights.py`** (new): CLI shim — baselines update
  (`hilmar.baselines.update`, persisted next to tracking-data as
  `baselines.json` + grafted in-memory) → `insights.build_context`
  (+ optional qc-result.json / parser_misses.jsonl enrichment) →
  `insights.generate_narrative` via `ModelRouter` (defaults untouched:
  Opus, env dial-down, 429 cascade). Writes
  `reports/insights/<date>.{json,html}` + the two embed snippets
  `reports/insights-business.html` (staff) / `insights-full.html` (audit).
  Prints daily LLM spend from the router cost log; loud WARN above
  `HILMAR_INSIGHTS_COST_ALERT_CENTS` (default 200¢). EXITS 0 ON EVERY
  PATH — missing ANTHROPIC_API_KEY / API down / crash all degrade to a
  skipped narrative with the rule-based context still written.
- **`scripts/run_pipeline.py`**: new step "Daily insights (baselines + LLM)"
  inserted directly before "Email body HTML" (gen_email embeds its output);
  classified BEST_EFFORT; 480s step timeout (4 sequential Opus calls).
- **`scripts/gen_email.py`**: staff daily embeds insights-business.html as
  "🤖 AI Insights — Business" before the footer — only when the file's
  mtime is from today (stale-yesterday never renders), non-empty, ≤40KB;
  any failure renders nothing. Business-only per Michael 2026-04-28.
- **`scripts/gen_improvements_report.py`**: idealx.us audit embeds
  insights-full.html (all four sections), mirroring the
  rate-intelligence inline pattern + the same mtime-today freshness guard.
- **`scripts/gen_manual.py`**: "AI Insights — Business" added to the email
  section catalog (drift-guard test forces manual coverage); dropped an
  unused import that was failing ruff.
- **Tests**: `tests/test_gen_insights_wiring.py` (16 tests, LLM fully
  mocked — no Anthropic client is ever constructed). Suite: 1599 passed.

Decisions (Claude session, per locked spec — no operator input needed):
- baselines.json + llm-cost-log.jsonl live NEXT TO tracking-data-v2.json
  (repo root in the GitHub-Actions deploy), matching the
  orchestrator.step_baselines convention; HILMAR_LLM_COST_LOG still
  overrides the cost-log path.
- The shim grafts baselines in-memory only — it does NOT write
  tracking-data-v2.json (QC already ran by this pipeline stage; a
  best-effort step must not mutate the canonical data file).
- Missing-key check happens BEFORE any router call: client construction
  without a key raises outside the router's own 429/connection cascade,
  so the shim skips upfront rather than relying on that path.

## 2026-07-10 — Session: client email, zero unresolved lanes, analytics lit up

Operator decisions (Michael Deitchman):
- CLAUDE.md replaced with the Ideal-X working standard (PR #88; pipeline
  manual preserved in git history at main@1857c36).
- Client-facing daily email approved to build, SHIPS GATED OFF — go-live is
  the `client_report.enabled` flip, pending his review of real-data samples.
- "Zero unresolved lanes" set as the standard for the daily email.
- Queued: full cost-efficiency review (Anthropic tokens + Microsoft/Azure).

Shipped (PRs #85–#88 merged to main; analytics PR follows):
- **Client email** (`gen_client_email.py`): service-update-only content,
  QC-065 hard-pins recipients (to=lupfold@hilmaringredients.com,
  cc=michael.deitchman@ol-usa.com) + scans for internal-analytics leaks;
  sample goes to Michael only while gated; own `client-sent` idempotency
  flag + cross-host mailbox guard.
- **Lane resolution**: standalone booking amendments now take their lane
  from the booking-PDF Port of Discharge (`patch_carriers` PASS 2b,
  KNOWN_DESTINATIONS-validated). QC-015's "within tolerance" pass for
  unmapped destinations replaced with a hard ERROR on today-dated rows.
- **QC-057 diagnostics + acknowledgments**: every silently-dropped intake
  email now logs PII-scrubbed lane-hinted body lines (QC-057-DIAG); the
  REEFER NEEDS / REEFERS commercial notes (not RFQs — free-time ask,
  transship-options instruction) recorded in date-scoped
  `scripts/intake_acknowledged.json`.
- **User manual** (`gen_manual.py` → user-manual.html): consumer manual
  rebuilt every fire from live config; drift-guard tests tie its catalogs
  to the real gen_email sections and dashboard tabs; attached to the daily
  staff email.
- **Weekly executive summary restored**: it only fired from the retired
  Cloud PC wrapper, so the GitHub-Actions cutover had silently killed it.
  Now a Friday-self-skipping pipeline step; attached to Friday's email when
  fresh.
- **Historian wiring**: daily.yml now passes HILMAR_HISTORIAN_URL/TOKEN and
  installs the libsql client. Store stays dormant until Michael adds the
  two repo secrets (docs/HISTORIAN.md) — owner action.
- **Insights engine wired** (built 2026-04/05 per docs/INSIGHTS-DESIGN.md,
  never invoked): baselines + rule-based context + LLM narrative now run as
  a best-effort pipeline step; Business section embeds in the staff email,
  all four sections in Michael's audit email; cost telemetry + $2/day alert
  per the locked spec.

Known-open (owner: Michael unless noted):
- Repo is PUBLIC on GitHub — code/docs expose client + business details;
  recommend flipping to private (Settings → Change visibility).
- Turso secrets not yet set — no longitudinal rate/win history accumulates
  until they are.
- Client email go-live flip pending sample review.
- Cost-efficiency review queued (Claude: run after insights cost telemetry
  accumulates a few days).
- Yokohama ×2 rate rows still missing carriers (sibling quotes disagreed);
  QC-056 diagnostics continue to surface them.
