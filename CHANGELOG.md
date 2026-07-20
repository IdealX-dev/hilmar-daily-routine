# Changelog — Hilmar Daily Tracker

Per the working standard (CLAUDE.md): every session logs its decisions here,
by name, so the next session starts current. Newest first.

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
