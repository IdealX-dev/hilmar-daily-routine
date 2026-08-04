# Changelog — Hilmar Daily Tracker

Per the working standard (CLAUDE.md): every session logs its decisions here,
by name, so the next session starts current. Newest first.

## 2026-08-04 (2) — "i like it all" finally means all three

Michael, after asking for the staff send and the crons: "but first the
formatting changes i wanted." Fair — he said "i like it all" on 2026-07-22
about the OL air-freight comparison doc, meaning dashboard AND PDF AND email.
#138 shipped only the dashboard. This is the other two.

1. ONE SET OF TOKENS, NOT THREE COPIES
   The palette #138 put inline in gen_dashboard.py moved to branding.DOC_*
   and all three renderers now read it. Every hex was read out of the
   reference document's :root block. THREE COPIES OF A PALETTE IS NOT A
   DESIGN LANGUAGE — a test asserts the dashboard, the email and the PDF
   resolve the same paper token, so re-hardcoding one is caught.
     DOC_PAPER #f4f3ef  DOC_CARD #ffffff  DOC_INK  #1f2328
     DOC_MUTED #5f6670  DOC_LINE #e3e1da  DOC_TH_BG #fbfaf7
     DOC_GOOD #1f7a4d   DOC_WARN #b9740f  DOC_BAD  #b03030

2. WHAT ACTUALLY CHANGED (visual language only — no data path touched)
   - warm paper ground; white cards held by a hairline
   - table heads: muted uppercase over ONE ink rule, replacing three
     different saturated bars (navy #1e3a5f, green #059669, dark-red
     #7f1d1d) with white text. The data is the loud part now.
   - PDF: the full 0.3pt GRID cage is gone; 0.25pt hairline below rows,
     0.9pt rule under the head. Eight tables repeated the same six inline
     commands — they now share gen_pdf.table_style().
   - every figure in mono (Courier in the PDF, DOC_MONO_STACK in HTML) so
     decimals align down the column
   - email KPI tiles: were saturated blocks with white text, now white
     cards with the colour demoted to a 3px top rule
   - email header: the navy→blue gradient is gone. QC-045's rule is now
     satisfied by construction rather than by a solid-colour fallback.

3. EMAIL IS NOT THE WEB — AND IT BIT, EXACTLY ONCE
   Desktop Outlook renders with Word's engine: no CSS custom properties, no
   flex, no grid. So the tokens are resolved to literal hex in Python before
   the message is built. First pass, THREE styles went into plain '' strings
   instead of f'' strings and rendered the literal text "{TH_STYLE}" into the
   body. Caught by rendering the email, not by reading the source.
   tests/test_document_restyle.py now fails on ANY unrendered placeholder in
   the output — that is the only place this class of bug is visible.
   Also dropped the fonts.googleapis.com link from the email, same call as
   the dashboard in #138: remote content trips Outlook's "download pictures?"
   bar and OL's proxy, and the same report should render the same on every
   desk.

4. A #138 TEST THAT WAS TESTING THE WRONG THING
   test_the_dashboard_sets_a_mono_stack_for_figures scanned
   gen_dashboard.py's SOURCE for "ui-monospace". Centralizing the stack into
   branding turned it red while the emitted CSS was byte-identical. A
   source-substring test fails on a refactor that changes nothing a reader
   sees, and passes on a definition that is never emitted. Rewritten to
   assert on the RENDERED dashboard. (Same family as the QC-ID substring
   scanners — an ID in prose is indistinguishable from an ID emitted.)

   VERIFIED THIS SESSION: full suite 2216 passed, 0 failed; ruff clean across
   scripts/ and tests/. The PDF's paper ground, its 0.25/0.9pt rules and its
   Courier figures are asserted by decoding the generated PDF's own content
   stream — there is no rasterizer on this host, so nobody has LOOKED at the
   PDF. Previews were sent to Michael for that.

STILL OPEN — CARRIED FROM THE ENTRY BELOW
- The staff send is BLOCKED by a real defect, not by a decision. The mailbox
  guard dedupes on subject, and the 21:04 verification send used the SAME
  subject the staff send would. A plain send_to=full will refuse, send
  nothing, and write a weekly-sent flag claiming it shipped. Same foot-gun as
  2026-07-30. Fix before sending: tag verification subjects "[VERIFY]" so a
  test send can never consume a real send's guard, plus a deliberate one-run
  override for today's collision.
- Crons back on: Michael said yes; not done yet.

## 2026-08-04 — The backfill loaded the data and then reported one day of it

Michael, on the backfill run: "you failed in your backfill.. lots more work
happened this week including today" -> "and this is only the lonny report".
He was right, and the data was never the problem.

1. WHAT THE BACKFILL ACTUALLY DID
   Run 30949044542 (mode=production-fire, send_to=test, force_resend=true)
   completed in 248.9s and pushed 6 files — tracking-data-v2.json,
   scripts/stage_emails.txt, scripts/stage_emails_bodies.txt,
   secrets/token-cache.bin, secrets/token-cache.json, data/quote-history.db.
   First successful push since 2026-07-27 18:29. new_quotes_appended 335,
   new_wins_appended 74; the store went 315 -> 335 rows.
   THE INGEST WORKED. The reporting did not.

2. WHY THE REPORT SHOWED ONE DAY
   The daily is hard-wired to the prior business day, so it rendered Aug 3 and
   nothing else. The weekly declined outright — "Fire day is Tuesday ET, not
   Monday". Six business days of recovered work (Jul 28, 29, 30, 31, Aug 3,
   Aug 4) had no report that covered them.
   Michael chose option 1, the weekly summary. Verified before building it:
   `--force` alone does NOT cover the outage. main() anchors on
   `today - 7 days`, so dispatched Tuesday Aug 4 it reports Jul 27-31 and drops
   Aug 3 and Aug 4 — including the day he was looking at. A recovery run that
   silently reports less than the gap is the same failure again, quieter.

3. FIX — REPORT THE WINDOW THAT WAS MISSED (#143)
   scripts/gen_weekly_summary.py gains `--start` / `--end` (ISO, inclusive,
   both-or-neither). An explicit period implies --force, because otherwise a
   wrong weekday could drop a run that was asked for BY DATE. Bad or reversed
   dates exit 2 and write nothing — and the send step reads files the build
   step must write, so a typo'd date sends nothing rather than the wrong
   window.
   The comparison baseline is the SAME-LENGTH window immediately before the
   period, not the previous calendar week: a 9-day catch-up measured against a
   5-day Mon-Fri prints a delta manufactured by the window length.
   The header says "Reporting period", not "Previous week", and the deltas
   state what they are measured against. The default path renders exactly as
   before — that is the run nobody watches.
   weekly.yml gains matching `start` / `end` dispatch inputs and passes them
   through; with both blank it takes the unchanged `--force` path.

4. THE SUBJECT NOW COMES FROM THE GENERATOR, NOT FROM SHELL DATE MATH
   weekly.yml built the subject with `date -u -d 'last monday -7 days'`,
   duplicating the anchor arithmetic the Python already does. Two clocks, one
   header — and the subject is what the cross-host mailbox guard dedupes on,
   so a drift between them silently suppresses a real send or doubles a sent
   one. `_subject_for` is now the single source; the send step fails closed if
   the file is missing or empty. A test pins the default string byte-for-byte
   against what the shell produced, so this refactor cannot move it.
   A catch-up gets its OWN subject ("Catch-Up Executive Summary"), so it can
   never be deduped against a real weekly.

5. A BUG THE TESTS CAUGHT BEFORE MICHAEL DID
   The week label was one line that dropped the end month unconditionally —
   correct for a Mon-Fri week, which never leaves its month. The first
   cross-month period rendered "Jul 27-4, 2026". Not a date range, and it sits
   directly above the numbers it describes. `_range_label` now handles
   same-month, cross-month and cross-year; all three are parametrized.

   VERIFIED THIS SESSION: full suite 2175 passed, 0 failed; ruff clean on both
   changed files; weekly.yml parses as YAML. CI run 30950446416 green on all
   14 steps, including the production-fire env-parity suite. Merged as #144.

6. THE CATCH-UP WENT OUT — TO MICHAEL ONLY
   Run 30950686216, dispatched on the merge commit a9131b3f with
   send_to=test, start=2026-07-27, end=2026-08-04:
     Explicit period: '2026-07-27' -> '2026-08-04'
     Period:    2026-07-27 -> 2026-08-04 (9 days)
     Baseline:  2026-07-18 -> 2026-07-26
     Subject:   Hilmar - Catch-Up Executive Summary (Jul 27-Aug 4, 2026)
     This week: 20 req / 9 W / 34 TEU / 33.3% win rate
     Prev week: 12 req / 3 W /  8 TEU / 23.1% win rate
     Carrier of week: Evergreen (100.0%)
     TO (1): ['michael.deitchman@idealx.us']  BODY: 18,004 bytes
     Sent. request-id=b3a32828-6d3e-4c8b-9b53-0e25dd6361a4
   20 requests against the ONE day the backfill report showed. "Push state
   back" correctly skipped (SEND_TO=test), so tracking-data-v2.json was not
   touched. Nothing reached the staff list; the weekly never goes to the
   client, so Lonny received nothing.
   READ WITH IT: 9 wins + 18 Q&L against 20 requests does not add up, and that
   is the DESIGN, not a defect. Wins are dated by when the booking landed;
   Q&L and the request total are dated by when the RFQ came in, so a win here
   can belong to an RFQ from before Jul 27. That mix is the identical formula
   the daily's KPI block uses, and matching it is what stopped the weekly and
   the daily contradicting each other (finding #19).

DECISIONS
- Michael: recover the week via the weekly exec summary (option 1), not via
  six replayed dailies.
- Claude: build the period override rather than dispatch a bare --force that
  would have reported Jul 27-31 and left Aug 3-4 dark — the exact shortfall
  he had just called out.
- Claude: first dispatch goes to send_to=test (Michael only) for review before
  anything reaches the staff list. The weekly never goes to the client.

STILL OPEN — AWAITING MICHAEL
- Whether the Jul 27-Aug 4 catch-up goes to the STAFF list. Same dispatch,
  send_to=full. Not sent; needs his word.
- Whether to turn the crons back on.

STILL OPEN
- Crons remain OFF in daily.yml and weekly.yml (`# PAUSED 2026-08-03`).
  Re-enable when Michael says so; nothing fires on a schedule until then.
- PDF and email-body restyle ("i like it all") — only the dashboard shipped
  (#138).
- TEAMS_WEBHOOK_URL still unset.
- QC-073: stand_260928 degenerate lane Oakland -> Oakland.

## 2026-07-30 — The report was down three days for a reason nobody had looked at

Michael: "this report hasn't run in days" -> "storage account. no clue.. that's
for you to figure out" -> on the Jul 29 report, "lots of data missing all broken".

FIRST, A CORRECTION TO THIS FILE. The 2026-07-28 entry recorded that Friday
2026-07-24 "never got its own daily email". WRONG. Run 30099554766 shows the
daily send, the client send and the integrity gate all green. Friday shipped.
The outage was Mon 27, Tue 28, Wed 29 — three business days, not two.

1. THE ACTUAL BLOCKER WAS A BACKUP, NOT THE PARSER
   Mon 27 was runner allocation + the QC-039 gate. Tue and Wed were something
   else: the blob store began refusing WRITES, and the snapshot-backup step —
   under the default `bash -e` — exited 1 and took the whole job with it.
   Steps 9-13 (validate, run pipeline, SEND THE DAILY EMAIL, client email,
   integrity gate) all skipped.
   A dated gzip snapshot is a SAFETY NET. The daily report is the PRODUCT. Two
   further days of reports were lost to a backup that could not be written,
   which is strictly worse than having no backup for two days.
   FIX (#131): snapshot is continue-on-error and raises an out-of-band alert;
   the alert step is itself continue-on-error, because an alarm must never be
   the thing that kills the fire. tests/test_audit_batch8.py asserts the
   GENERAL rule: no step before the send may abort the fire unless it is named
   in ESSENTIAL_BEFORE_SEND with the reason it earns that power.

2. THE STORAGE ACCOUNT — MEASURED, NOT GUESSED
   Michael handed this back, so it was diagnosed from the runner (the only
   place the credential exists). scripts/diag_blob.py + a manual-dispatch
   workflow (#130, #132), run three times ~75 min apart with identical results:
     account rgidealxautomation9439, StorageV2, Standard_LRS
     auth = ACCOUNT KEY (not SAS)      -> not a permission-scope problem
     is_hns_enabled = False            -> not a directory problem
     immutability/legal hold = False, lease unlocked, tier Hot
     ALL reads, lists, properties      -> OK
     ALL writes                        -> 404 ResourceNotFound
     create_container on the EXISTING container -> 404 (not the healthy 409)
     create_container on a NEW container       -> 404
     get_service_properties                    -> OK
   create_container returning 404 is the tell: no blob is involved and the
   container demonstrably exists. Every read path works; every write path at
   every level 404s. That is account-scope Azure state — not our code, not the
   container, not the credential's validity. WHY is control-plane and needs
   Portal/ARM access nobody in this session has. Last successful write
   2026-07-27 18:29:06, exactly when the reports stopped.
   NOTE: airprofits-state shares that account. If anything else writes there it
   has been failing silently since Monday too.
   DO NOT re-paste AZURE_STORAGE_CONNECTION_STRING. It is proven-good (it
   authenticates; every read passes) and write-only in GitHub, so overwriting
   destroys the only copy of the one input nobody can inspect.

3. VERIFIED THE FIX BY FIRING, WITH MICHAEL'S APPROVAL
   Dispatched mode=production-fire send_to=test (Michael only, --force
   --no-flag, Lonny receives nothing). First report to leave the pipeline
   since Friday: PIPELINE COMPLETE in 61.9s, two sends with request-ids, fire
   integrity OK. Both new alerts fired as designed, and the push-failure alert
   returned {'stderr': true, 'queue': true, 'github': true, 'teams': false} —
   the GitHub channel wired on 07-27 reaching a human for the first time.

4. THE NEXT FATAL BLOB CALL, FOUND BY AUDIT (#133)
   verify_fire_prereqs.check_storage pinged with svc.get_service_properties(),
   and that check is FATAL — three steps before the send. No fire had reached
   that line since Jul 24, so it was untested for three days while sitting
   directly in the path of the fix just shipped. Now pings with
   get_account_information() + container.exists(), both MEASURED working.
   HONESTY NOTE: it was later probed and it PASSES. It was an untested fatal
   dependency, not an averted outage; saying otherwise overstated it.

5. QC-077 — A REAL QUOTE THE REPORT CAN NEVER SHOW (#136, #137)
   Michael's "data missing" was real. OL-USA RESPONSES buckets by EVENT DATE
   (gen_email.py:186-199, off response_timestamp); PENDING HILMAR is CURRENT
   STATE and not windowed (gen_email.py:800-801). A row with an ol_rate or
   carrier_quoted but no response_timestamp matches no day and is invisible to
   that section FOREVER, while PENDING HILMAR keeps showing its quote — which
   is why the report looked self-contradictory rather than broken.
   Measured: 29 of 315 rows (9.2%), and the newest response_timestamp anywhere
   is 2026-07-23. The section had been silently empty since Jul 24.
   ingest.py:1200 is the only place a matched rate response sets it; the
   sibling-lane fallback (ingest.py:1345) and the carrier backfill do not.
   NO HEAL, deliberately — synthesising a timestamp would fabricate turnaround
   timing. QC-077 errors, and the report now prints how many quotes it cannot
   date instead of rendering "No activity" over real OL work.
   STILL OPEN: the ingest fix itself. QC-077 makes it visible; it does not
   populate the field.

6. DASHBOARD RESTYLED (#138)
   Michael shared an OL air-freight comparison and called the formatting
   gorgeous. Took the craft: warm paper ground, hairline rules instead of drop
   shadows, mono for every figure so decimals align, quiet uppercase table
   headers, a .basis class for the derivation text that makes a number
   auditable. Class names untouched, so no HTML generation was restructured.
   Also removed the CDN font fetch: the dashboard ships as an email attachment
   opened offline or behind OL's proxy, and rendering should be deterministic.
   HONESTY NOTE: the old link had a fallback stack and degraded gracefully. It
   was called a defect here first; that was overstated. gen_email.py's link is
   LEFT ALONE — it is MSO-guarded progressive enhancement, a different case.
   Michael wants all three surfaces. PDF and email body are NOT done; the
   email needs the design rebuilt table-based to survive Outlook's Word
   renderer, not ported.

MISTAKES MADE TODAY, ON THE RECORD
   - Reasoned for a long stretch believing the date was 07-28 when it was
     07-30, so the outage was one day worse than stated until caught.
   - TWO mutation probes were INERT and read as passes: one argument-order bug
     in the harness (`mut A "desc"` against `desc="$1"; shift`), one bad anchor
     string. A third destroyed an uncommitted daily.yml fix via
     `git checkout --` and the resulting failures were misread as the
     mutation's. Harness now aborts if a mutation does not apply, and work is
     committed before mutating.
   - QC-077's first version flagged standalone bookings, which legitimately
     have no response_timestamp (ingest.py:887 sets None deliberately). 5 of
     the 29. It would have cried wolf on every run.
   - QC-077's message quoted "QC-056" in prose, which broke two QC-056 tests:
     helpers and the ratchet both scan fired messages by substring, so an ID in
     prose is indistinguishable from that check firing. Same shape as the
     ratchet defect fixed on 07-28.
   - Widening _today_events' return arity broke 34 tests for no benefit.

STILL OPEN
   - The storage account write path. Nothing in the repo can fix it.
   - The ingest response_timestamp fix (see 5).
   - The PDF and email-body restyle (see 6).
   - TEAMS_WEBHOOK_URL unset — still the only channel that survives GitHub
     itself failing.
   - Scheduled runs land ~2h late: ~14:05-14:30 UTC against a `7 12` cron, so
     the report arrives ~10:00-10:30 ET, not 8 AM.
   - The automated PR reviewer has skipped SIX consecutive PRs on the org
     overage spend limit. Everything today merged on CI + self-review.

## 2026-07-28 — Reviewing yesterday's fix found it half-done, and its test blind

Michael raised the org's code-review spend limit after the automated reviewer
skipped PR #126 ("overage spend limit reached") — #126 had already merged on CI
and self-review alone. Spent the raised limit reviewing what had just shipped.
It found a defect in my own fix and a test that certified safety that wasn't
there. Both are corrections to claims I made to Michael yesterday.

1. THE GATE WAS STILL AHEAD OF A HEAL — IN THE DIRECTION THAT SHIPS BAD DATA
   Yesterday's fix moved QC-039 past QC-056. It was still ahead of QC-064,
   which NULLS garbage out of client-visible cells. Six of QC064_DISPLAY_FIELDS
   are graded by the gate and five are CRITICAL, so the gate counted a field
   populated, QC-064 blanked it, and the email would ship below threshold with
   the gate reading green. Measured on an 11-row probe: gate saw 100.0%, the
   shipped rows were 90.9%.
   Yesterday's instance WITHHELD a good report. This one SHIPS a bad one, which
   is worse. Not a regression — the old position was also ahead of QC-064 — but
   the same bug class, left open while I described it as closed.
   FIX: QC-039 now runs at the END of phase_6_rules, after every row mutation.

2. THE TEST THAT WAS SUPPOSED TO PREVENT #1 COULD NOT SEE IT
   I told Michael the ordering rule was "enforced by a test that walks the
   function's AST". It parsed the AST only to slice out the function TEXT, then
   ran str.find for `["field"] = ` over three of eight CRITICAL fields. Blind to
   variable-key writes (`r[_f] = None` — exactly QC-064), .update()/.setdefault(),
   and writes inside helpers the phase calls. One of its three fields
   (`ol_rate`) has ZERO occurrences in phase_6_rules, so a third of it
   constrained nothing while reading as thorough.
   FIX: a real ast.walk over every field in FIELD_REQUIREMENTS, following one
   level into called helpers, treating computed keys as unsafe; plus a test that
   the walk cannot go inert, and one asserting the same rule in main() — moving
   phase_3_entries after phase_6_rules reintroduced yesterday's bug with all
   2093 tests green.

3. QC-076 CHECKED THAT A STRING WAS NON-EMPTY
   It read `GH_TOKEN or GITHUB_TOKEN` inline. A junk token passed; a box with
   `gh auth login` and no token was called dead. The 2026-07-27 outage had TWO
   causes (no token AND no `issues: write`) and QC-076 covered neither properly.
   FIX: both halves delegate to fire_alert (one definition of "configured"), the
   log says "configured" not "deliverable", and the `issues: write` half stays a
   static assertion against daily.yml. Predicate changed from GITHUB_ACTIONS to
   HILMAR_NONINTERACTIVE — set on every unattended host, and it stops the ERROR
   firing on CI runs.

4. THE GITHUB CHANNEL WENT LIVE YESTERDAY, MAKING TWO LATENT BUGS REAL
   - No dedupe: QC-063 fires on every fire until the failing step is fixed, so a
     step dead a week would file five identical issues. Now comments on the open
     issue instead (liveness.yml already did this; fire_alert never did, because
     the channel was a permanent no-op).
   - send_alert defaulted to the `cloud-pc-down` label, and liveness.yml closes
     EVERY open `cloud-pc-down` issue on a fresh heartbeat. A critical alert —
     assert_fire_integrity's "no verified report shipped" — could be filed and
     auto-closed within hours by an unrelated watchdog while still true. Default
     is now `fire-alert` alone.
   - The UNDELIVERABLE banner could RAISE out of send_alert on a closed or
     non-UTF-8 stderr, turning "the alarm could not deliver" into "the caller
     crashed". Wrapped, ASCII-only.
   - The fire_alert CLI exited 0 for an alert that reached nobody
     (`any(res.values())` counts stderr and queue). Now uses undeliverable().

5. THE GOVERNANCE RATCHET WAS DEFEATED BY A COMMENT
   emitted_checks() regexed the whole file, so deleting QC-076's entire emission
   block left the ratchet green on one surviving docstring mention. Now AST-based
   over the string arguments of log.ok/warn/error/fix only.

VERIFIED THIS SESSION
   2105 passed, coverage 91.28%, ruff clean. All NINE mutations caught — gate
   moved back before the heals (3 tests), post-gate variable-key write (3),
   phase reorder in main() (1), deleted banner call site (1), teams-delegation
   drift (1), predicate reverted (2), QC-076 emission deleted with a comment
   left (3, incl. the ratchet), default label reverted (1), dedupe removed (1).
   Two of my first mutation probes were themselves inert (a bad anchor string,
   and one that broke the file) and reported green — re-run correctly before
   being believed.
   Tests are now hermetic w.r.t. the alarm env vars: test.yml sets
   HILMAR_NONINTERACTIVE for the pytest step, so these would have been
   host-dependent (the 2026-06-15 "green in CI / red in production" class).

TOMORROW'S FIRE — verified on merged main, not assumed
   QC-056 heal -> QC-039 gate ordering confirmed by AST; production-fire has
   `issues: write` + job-level GH_TOKEN; exactly one cron proceeds (12:07 UTC,
   ET offset -0400, the 13:07 tick gated out); SEND_TO resolves to 'full' on a
   schedule; no same-day sent-flag exists for 2026-07-29 to suppress it.

STILL OPEN
   - TEAMS_WEBHOOK_URL unset. The durable fix for CORRELATED failure: on
     2026-07-27 the fire and its watchdog died together because both run on
     GitHub runners. A GitHub-independent channel is the only thing that
     survives that. Needs the URL from Michael.
   - QC-073: stand_260928 has a degenerate lane Oakland -> Oakland. Real error,
     untouched.
   - QC-070's TEU heal also writes graded fields; harmless today because
     core.parse_teu never returns None and _is_populated counts 0 as populated,
     but it is the same ordering shape and is now covered by the AST walk.

## 2026-07-27 — Why the report didn't go out, and the two fixes so it can't happen this way again

Michael: "todays reports didn't fire" -> "YOU HAVE TO FIX THIS SO IT DOESN'T
HAPPEN AGAIN. FIX THE QC AND THE DAILY AUDITS."

THREE THINGS HAPPENED. Only the first was GitHub's.

1. THE SCHEDULED FIRES NEVER GOT A MACHINE (GitHub, transient)
   Both 08:07-ET cron runs and two Liveness ticks were created and never
   assigned a runner: runner_id 0, runner_name "", NO steps array at all,
   failed 2-3s later, logs 404. Proved by control — the same repo's successful
   jobs carry runner_id 1000016xxx and a full steps list. Cleared by 18:19; a
   dispatch then got runners immediately on the identical commit. Not code:
   daily.yml is byte-identical between Friday's passing run and today's
   failures, and no step ever executed.
   I first hypothesised an Actions spending cap. Michael checked: wrong. Logged
   because I asserted it before ruling it out.

2. THE RE-FIRE WAS BLOCKED BY A GATE THAT MEASURED TOO EARLY (ours — the real
   defect)
   QC-039 read carrier_quoted 291/313 (93.0%) and blocked the client ship.
   QC-056 then backfilled 10 carriers ON THE SAME RUN. 301/313 = 96.2% — over
   the 95% gate. A whole business day's report was withheld because the gate
   ran ~880 lines BEFORE the heals it was grading, inside the same
   phase_6_rules call.
   THIRD INSTANCE OF THIS EXACT SHAPE: batch-5 #15 persisted aggregates before
   the heals ran; the QC-075 stale-summary bug false-fired for the same reason;
   now this. So the rule is no longer a habit, it is a TEST: every write to a
   gate-graded field (carrier_quoted, carrier_won, ol_rate) must land before
   compute_accuracy runs, enforced by an AST walk over phase_6_rules. Moving
   one check would have fixed today; the test is what stops the fourth
   instance.
   Verified: 3 tests fail with the old ordering restored, including the
   behavioural one (a row QC-056 CAN heal must not trip the gate). The gate
   keeps its teeth — a row with no carrier anywhere in its text still blocks.

3. THE ALARM COULD NOT REACH ANYONE (ours — why nobody was told)
   When the fire failed it raised a FIRE-ALERT. That alert returned
   {'github': False, 'teams': False}. daily.yml gave the pipeline and
   integrity-gate steps no GH_TOKEN, and the production-fire job no
   `issues: write`; no Teams webhook is configured. So the alarm existed only
   as a stderr banner inside a failed job's log and a queue file on an
   ephemeral runner that was then destroyed. Michael found out because the
   report never arrived.
   Fixed at source: `issues: write` on the job, GH_TOKEN at JOB level (not one
   step — both alert-raising steps need it, and so will the next one). A
   failing fire now opens a labeled issue immediately instead of depending
   entirely on the liveness watchdog, which runs on the same runners and
   therefore failed the same way today.
   `fire_alert.send_alert` now also prints an unmissable ALERT UNDELIVERABLE
   banner when no remote channel took it. An alarm that cannot deliver is a
   silent failure of the one thing whose job is not being silent.

NEW QC-076 (ERROR) — "can the alarm actually reach anyone?"
   Checks on EVERY fire, while everything is fine, that at least one remote
   channel (GitHub issue / Teams webhook) is available. The worst time to learn
   the alarm is dead is the moment you need it — same rationale as QC-032
   checking backup freshness instead of waiting for a restore to fail. Scoped
   to unattended runs: on a dev box stderr IS a human channel, and a check that
   cries wolf there trains the operator to ignore it.

WHAT I GOT WRONG TODAY, ON THE RECORD
   * Asserted an Actions billing cap before ruling it out. Wrong.
   * Said the weekly summary succeeded "on the merged code" — it ran on the
     PRE-merge SHA, so my weekly.yml change still has not run in production.
     First real exercise is next Monday.
   * Nearly diagnosed this whole thing against a STALE local main (f921f2d):
     `git checkout main` silently landed on the old clone, not the merged
     cc7cd79. Caught before drawing conclusions from it.
   * Investigated the liveness watchdog on a hunch it was blind to failed
     fires. It is not — heartbeat.yml deliberately FAILS the job on a
     non-success fire (2026-06-25) so liveness's --status=success query cannot
     see it. That design is sound; the watchdog simply never got a runner.

STILL OPEN
   * [CORRECTED 2026-07-30 — THIS WAS WRONG. Run 30099554766 shows Friday's
     daily send, client send and integrity gate all green. Friday SHIPPED.
     Leaving the original line below so the error is visible, not erased.]
   * Friday 2026-07-24 never got its own daily email. The DATA is intact and
     the weekly covers it; the one daily send is gone. Tomorrow's fire reports
     Monday.
   * TEAMS_WEBHOOK_URL is still unset. That is the durable fix for CORRELATED
     failure: today the fire and its watchdog died together because both run
     on GitHub runners. A GitHub-independent channel is the only thing that
     survives that. Needs Michael to supply the URL.
   * QC-073 flagged a real error not yet fixed: stand_260928 has a degenerate
     lane Oakland -> Oakland.
   * The automated reviewer is limit-blocked, so this batch merges on CI +
     self-review only.

Suite 2093 passed (2075 -> +18), coverage 91.28%, ruff clean on the CI-gated
paths. QC governance ratchet green (it caught QC-076 undocumented on the first
run — working as designed).

## 2026-07-27 — Self-review of the one commit that shipped unreviewed

#124 merged with 85da7e9 never seen by the automated reviewer — IDEALX's
Claude Code overage limit was reached mid-PR, so the third pass never ran.
That commit is the one that fixed two regressions I had introduced, i.e. the
least-scrutinised part of the whole batch. Reviewed it myself instead.

WHAT I CHECKED, AND WHAT HELD
   * `pol` added to _PLACEHOLDER_FIELDS — the only change in that commit that
     alters LIVE row data. Verified the FULL loop by execution, not just the
     pop: phase_3 removes the garbage literal, and QC-027's heal in phase_6
     re-derives the real POL from the lane endpoints. "TBD"/"N/A" ends up as
     "Oakland"/"Busan" — strictly better data, not a hole. The risk was that
     popping a field the completeness gate MEASURES would make QC-027 start
     reporting a gap this heal created; it does not, because the heal refills
     it, and because both the scrub and the gate scope to the same rows.
   * QC-075 keeps its teeth after the pre-check rebuild. The rebuild removes
     STALENESS; it cannot mask a genuine disagreement, because the two sides
     run different aggregators over the same rows. Confirmed by simulating
     finding #17's actual predicate split — reconciled correctly reads False.

WHAT DID NOT HOLD — MY OWN TEST
   `test_qc075_still_fires_on_a_genuine_aggregator_disagreement` poisoned
   `data["summary"]` directly and asserted the check caught it. But main()
   rebuilds the summary immediately before reconciling, so that poisoning is
   overwritten — the test asserted against a state production can never reach.
   Green, and proving nothing. That is the SAME "test looks green over an
   untested path" shape that produced two findings in this batch already
   (the mock-Log QC-075 test, and the phase-sequence test that survived
   reverting its own fix).
   Rewritten to drive the real mechanism: monkeypatch aggregate_trade_regions
   back to the pre-fix loss_reason predicate so the two aggregators genuinely
   disagree about one RESPONSE_NO_RATE row. Mutation-checked by forcing
   `reconciled = True` — the new test fails, the old one would not have.

Suite 2075 passed (2074 -> +1), coverage 91.28%, ruff clean on the CI-gated
paths. No production behaviour changed by this commit — it is one test
replaced and one added.

## 2026-07-27 — Data audit batch 5: the report contradicting itself, + durability

Five confirmed findings. Four are the SAME defect in four places — two code
paths computing "the same" number by different rules, with nothing comparing
them. That is the shape behind every "CHECK YOUR REPORT" so far.

1. TWO DEFINITIONS OF "NOT QUOTED" — actually THREE (finding #17)
   `gen_email` bucketed NQ by `loss_reason == "NO_RESPONSE"`; `core.
   aggregate_summary` used `core.is_not_quoted` (a LOSS that was never
   quoted). A RESPONSE_NO_RATE row — OL acknowledged the RFQ but sent no rate,
   so quoted=False — satisfies the second and not the first. ONE row then split
   across five contradicting numbers in a SINGLE email:
     "Not Quoted: 1 · 4 TEU"  in the KPI tile
     "NQ 0 / Q&L 1"           in the 8-week rollup
     "NQ 0 / Q&L 1"           in Volume by Trade Region — printed directly
                              under the words "reconciles to summary"
     0 rows                   in the NOT-QUOTED detail section
     ONE charged 1 Q&L loss + 4 TEU lost with 0 quotes (win-rate denominator 0)
   Fixed by routing all three `gen_email` call sites through
   `core.is_not_quoted`. loss_reason is now purely the WHY column; it never
   decides the bucket.
   THE TEST FOUND A THIRD SITE I HAD MISSED: `core.aggregate_trade_regions`
   used the same wrong `loss_reason` test, which is precisely the Volume by
   Trade Region line above. Fixed there too. (No mirror in src/hilmar — that
   function only exists in the production tree.)

2. QC-075 (ERROR) — trade-region rollup vs summary
   `_trade_region_reconciliation` has computed a `reconciled` boolean since
   2026-05, but it was only ever `print()`ed to stdout — never routed to
   log.error — so every divergence shipped silently. It now escalates. The
   check detects; the shared predicate above prevents.

3. AGGREGATES WERE PERSISTED BEFORE THE HEALS RAN (finding #15)
   phase_5 built summary/lane_summary/carrier_summary, phase_6 then ran the
   MUTATING heals (QC-064 nulls a leaked responder mailbox out of
   carrier_quoted, QC-067 restores a row to PENDING, QC-056 backfills a
   carrier), and phase_7 saved phase_5's stale output. The shipped file's
   aggregates contradicted its own rows: the audit read clean because the ROW
   was fixed, while carrier_summary still keyed a carrier named
   "MBD_OceanExport@ol-usa.com" and the client PDF printed it.
   Fixed: phase_7_save recomputes first. It is a pure recompute from rows, so
   it is cheap and idempotent.

4. THE PLACEHOLDER HEAL SHIPPED THE LITERAL STRING "None" (finding #16)
   Setting origin/destination/pod to None left the key PRESENT-but-null, so
   every downstream `r.get("origin", "Oakland")` default was bypassed —
   `.get` only substitutes when the key is ABSENT. The client PDF's Lane
   Performance table shipped a row labelled "None → Tokyo", strictly worse
   than the "Unknown → Tokyo" the heal was replacing. Now `r.pop(field, None)`.

5. "PENDING WATCHLIST — N OPEN" OVER AN EMPTY TABLE (finding #18)
   The header counted len(pending) while any row whose substate timestamp
   would not parse was `continue`d out of the table — the dashboard
   contradicting itself, and an open RFQ nobody chases because it is
   invisible. Now falls through response_timestamp -> request_timestamp ->
   booking_timestamp -> request_date, and a row that still cannot be dated is
   shown with an em-dash age at lowest severity rather than dropped (matching
   gen_email's PENDING HILMAR table). Sort and render both handle None.

6. NON-ATOMIC SAVE + AN UNCONDITIONAL PUSH OVER IT (finding #20)
   `open(path, "w")` truncates the destination the instant it is called, so a
   crash, OOM kill or cancelled job mid-write left tracking-data-v2.json
   truncated — and daily.yml pushes under `if: always()`, so that half-written
   file was then uploaded over the canonical blob. The backup could not help:
   it snapshots the same corrupt file.
   `core.save_data` now writes to a temp file, flushes, fsyncs, and
   `os.replace`s — atomic on POSIX, so a reader sees the whole old file or the
   whole new one. The fsync is what makes that survive a machine crash rather
   than just a process one. A failed write cleans up its temp file.
   `state_store.push` independently refuses to upload a tracking file that
   does not parse or has no `requests` list — the last gate before it leaves
   the machine.

Tests: tests/test_audit_batch5.py (18 cases). The atomic-save test asserts the
PREVIOUS file survives a mid-write failure; the watchlist test drives the real
gen_dashboard.render with the production config and asserts every row appears;
the push guard is exercised against five corruption shapes (truncated, empty,
non-JSON, no requests list, top-level array) each asserting the good blob is
untouched. Suite 1965 passed (1940 -> +25), coverage 91.23%, ruff clean.

REMAINING BACKLOG: 7 findings — #13 merge_thread_dupes swallowing a distinct
request, #14 lane substring match writing a rate onto the wrong terminal,
#19 weekly vs daily crediting a booking to different weeks, #21 same-day
backup overwrite, #23 QC-067 heal bypassing manual_locked, #24 weekly.yml
concurrency group, #25 undeclared schema fields + typo'd date_range key.

## 2026-07-27 — Audit batch 5b: wrong-row writes, an overwritten operator, a reverted day, and a three-month-stale date window

Four more of the same seven. These are WRITE-SIDE defects: each one puts a
correct value on the wrong row, the wrong day, or under a key nobody reads.

14. A RATE LANDED ON THE WRONG TERMINAL (finding #14)
   `apply_rate_responses` fell back to a bare substring test when the exact
   lane key missed: `dest_canon in k or k in dest_canon`. Neither "manila
   (north)" nor "manila (south)" is a substring of the other — but BOTH
   canonicalise to "manila" via `canonical_port_key`, which is exactly what
   that alias map is for — so an exact-key hit ALSO pooled the two terminals.
   A reply on a thread titled "RE: Oakland to Manila" could then write
   ol_rate / carrier_quoted / etd_offered / vessel_voyage onto the WRONG
   terminal's row. The client saw a South-terminal rate reported as the North
   lane's quote, while the correct request stayed unquoted and aged out NQ.
   Two wrong numbers from one match.
   New `core.same_port(a, b)`: same canonical city AND, when BOTH sides name
   a terminal, the same terminal. A terminal-less side still matches either,
   so the "HCMC" → "HCMC (Cat Lai)" widening that the fallback existed for
   keeps working. Narrowing is UNCONDITIONAL on both branches.
   MY FIRST VERSION HAD A HOLE MY OWN TEST CAUGHT: `if _narrowed: candidates
   = _narrowed` kept the INCOMPATIBLE list when nothing matched, so a Manila
   (South) rate still landed on the Manila (North) row — the whole check
   defeated in the one case it existed for. If no candidate is compatible
   there is no match: leaving the row unquoted is correct, because a wrong
   rate on a client quote is worse than a missing one.
   Mirrored into src/hilmar/core.py with 21 new parity cases in
   test_core_parity.py. scripts/ runs the fire, src/hilmar/ is what coverage
   targets; a drift here writes a wrong rate in production while CI stays
   green — the exact PR #13 failure mode that test file exists to catch.

15. QC-067's HEAL OVERWROTE THE OPERATOR (finding #23)
   Every other re-decide path in qc_selfheal skips `manual_locked`. QC-067's
   did not, so a human correction filing a row LOSS/NO_RESPONSE was silently
   flipped back to PENDING on the next fire — and the operator had no signal
   it had happened. Now it skips and WARNs.
   Same heal also hand-rolled its status_history append with a hardcoded
   `"from": "LOSS"` — a FABRICATED prior state whenever the row was in any
   other status, i.e. a corrupt audit trail written by the thing whose job is
   keeping the audit trail. Now `core.record_transition`, which reads the
   real prior status and no-ops when it already matches.

16. THE WEEKLY JOB REVERTED A DAY OF INGEST (finding #24)
   weekly.yml ran under `concurrency: hilmar-weekly`; daily.yml runs under
   `hilmar-daily-fire`. DIFFERENT groups means no serialization, and both
   jobs share one blob store. Weekly pulls state at the start of its run; if
   the daily fire wrote new state in between, weekly's push then uploaded its
   own stale snapshot over it — silent last-writer-wins, a whole day's ingest
   gone with no error anywhere. Both now use `hilmar-daily-fire`.
   Compounding it: the push step was NAMED "Push state back (weekly-sent
   flag)" but ran a bare `push`, which uploads the ENTIRE state set including
   tracking-data-v2.json — a file the weekly job never writes and has no
   business uploading. And `reports/weekly-sent-{d}.flag` was never in
   `state_paths()` at all, so the one file it meant to sync was the one file
   it did not: weekly idempotency was machine-LOCAL, and a re-dispatch on a
   fresh runner saw no flag and re-sent the exec summary to the full
   distribution list.
   Fixed on both sides — flag added to `state_paths()`, and `push(only=...)`
   / `--only weekly-sent` so the job pushes its own flag and nothing else.

17. THE DATE WINDOW WAS A HARDCODED LITERAL UNDER A TYPO'D KEY (finding #25)
   `"data_range": {"start": "2026-04-01", "end": "2026-04-19"}`. Two defects
   in one line. (a) The key is `date_range` — that is what gen_email,
   gen_email_new, render.data_window, restructure_two_table and insights all
   read — so the key was silently ABSENT and gen_email fell back to
   `cfg["data_range"]["start"]`, printing the CONFIG's start date instead of
   the data's. (b) Even a reader that found it got a hardcoded window three
   months stale. Now `_computed_date_range(all_requests)`, computed from the
   rows actually being written and bucketed by `core.et_date_of` so the
   window agrees with the day tiles rather than describing a different clock.
   (The src/hilmar tree's qc.py heals data_range→date_range; the scripts/
   pipeline that actually runs in production has no such heal.)

18. FOUR FIELDS PRODUCTION WRITES THAT THE SCHEMA NEVER DECLARED
   Same finding, root cause: nothing compared schema.json to what ingest
   writes, which is how `data_range` survived three months. Auditing the
   whole contract found `pol`, `pod`, `free_time_requested` and —
   importantly — `manual_locked`, the operator override flag that item 15
   above is entirely about. The flag every heal must honour was invisible to
   anything built off the schema. All four now declared, with descriptions
   stating which is the raw parsed value vs the canonical one.
   MIGRATION NOTE (CLAUDE.md rule 3): additive only. The request definition
   does not set `additionalProperties`, so it was already permissive — these
   rows validated before and validate now. No stored row changes, nothing to
   rewrite, reversible by deleting the four property entries. Logged here.
   The lasting fix is the comparison itself: two new tests read ingest's
   `output` dict and its request-row literals FROM THE AST and assert every
   key is declared. AST, not a substring scan — an earlier source-scraping
   test in this same file failed because my own explanatory COMMENT matched
   the string it was asserting absent. The next undeclared field, or the next
   `data_range`, fails CI instead of shipping.

Tests: tests/test_audit_batch5.py 18 -> 43 cases, tests/test_core_parity.py
+21 (same_port / port_terminal / canonical_port_key across both trees). The
schema-conformance tests were mutation-checked — deleting `pol` and
`manual_locked` from schema.json fails 3 tests; restoring them passes 43.
Suite 2011 passed (1965 -> +46), coverage 91.27%, ruff clean on the CI-gated
paths (scripts/ src/ tests/ deploy/).

KNOWN, NOT FIXED: `plugin-build/build_plugin.py:70` has one ruff UP031.
Pre-existing, and outside the paths test.yml lints — not a regression from
this work, not silently fixed inside an unrelated commit.

REMAINING BACKLOG: 3 findings — #13 merge_thread_dupes swallowing a distinct
request when the equipment line falls outside the Outlook bodyPreview, #19
weekly summary vs daily email crediting the same booking to different weeks
(wants a shared `core.win_event_date(r)`), #21 same-ET-day blob backup
overwritten by a recovery run (wants timestamped immutable snapshots,
`overwrite=False`, and a `restore` command).

## 2026-07-27 — Audit batch 6: the last three findings, plus three the reviewer caught in batch 5

BACKLOG NOW EMPTY. The three findings that were left, plus three real defects
the automated review of #124 found in batch 5's own work — I confirmed all
three by execution before touching anything, and the reviewer was right on
all three.

19. ONE BOOKING, TWO DIFFERENT WEEKS (finding #19)
   Michael directed on 2026-07-21 ("a win belongs to the day Lonny booked it")
   that the daily email count wins by EVENT date, and gen_email._today_summary
   was changed to do exactly that. gen_weekly_summary was never changed: it
   filters every row — wins included — by request_date. So an RFQ received
   Friday 2026-07-24 and booking-confirmed Monday 2026-07-27 is a win in
   Monday's daily email (week of the 27th) AND a win in the PREVIOUS week's
   summary (week of the 20th). The same booking, credited to two different
   weeks, in two reports read side by side. Not a wrong number — two
   right-looking numbers that cannot both be true.
   New `core.win_event_date(r)` — ONE definition, in both core trees, called
   by both reports. ET date of the LAST →WIN transition (last, not any: a row
   reversed and re-won has two, and "any transition on this day" credited it
   to both days), falling back to request_date for legacy rows so no win
   vanishes, and None once the row is no longer a WIN so a reversed booking
   is never credited anywhere.
   The weekly now filters wins through `_filter_wins` while intake / Q&L / NQ
   / pending stay request_date-bucketed — the daily's documented split,
   matched exactly. carrier_of_week, the top-winning-lanes table and the
   4-week trend all move with it. win_rate keeps mixing the two clocks ON
   PURPOSE: it is the identical formula and identical mix the daily's KPI
   block uses, and a second "cleaner" rule here would just recreate the
   disagreement this fix removes.

20. THE SAME CLOCK WAS WRONG IN teams_alert, TWICE (found while fixing #19)
   (a) `at[:10]` sliced a UTC calendar date and compared it to an ET
   `today_iso`. A booking confirmed 20:30 ET Friday is 2026-07-25 in UTC and
   2026-07-24 in ET, so the two never matched and NO win alert fired for any
   booking confirmed after 8 PM ET — silently, every time. Proved by
   execution before changing it.
   (b) The alert never checked the row's CURRENT status, only that a →WIN
   transition existed — so a booking won and then reversed the same day still
   fired "🎉 WIN", celebrating a cancellation. gen_client_email already guards
   exactly this ("a →WIN transition is not the same as a confirmed booking");
   the alert path did not. Found by a test I wrote expecting it to pass.
   (c) The big-day TEU sum read `status_history[-1]` — the most recent
   transition of ANY kind — so a row won in the morning and touched by any
   later transition stopped counting toward the day's total.
   All three are gone: `core.win_event_date` answers exactly the question
   being asked, and the status guard is structural rather than remembered.

21. A TRUNCATED PREVIEW IS NOT EVIDENCE OF ABSENCE (finding #13)
   `_merge_thread_dupes` collapses Lonny's "header" email into the sibling
   carrying the container line, deciding which is which by teu_requested==0.
   But teu_requested comes from `guess_teu_from_preview`, which reads ONLY
   summary_preview — and the stagers cut that at 300 chars
   (refresh_stage.py:548). Lonny's RFQs open with routing and dates, so on a
   longer ask the equipment line falls PAST the cut. The row then looks thin
   while being a completely ordinary second RFQ, and the merge DELETED it: a
   real rate request that never reached intake, never got chased, never
   counted in any total, and left no trace but a merge_note on another row.
   When nothing parsed, the row's `containers` field still holds that raw
   preview, so we can tell whether we saw the whole email. A preview whose
   length lands exactly on a stager cap (200 or 300) was truncated → keep BOTH
   rows and record why. The genuine header-only case (a short, complete
   preview) still merges, so Issue #5 stays fixed.
   The bias is deliberate and asymmetric: a wrong "truncated" verdict costs
   one duplicate row, which is visible and which QC's dupe checks catch. A
   wrong "complete" verdict silently deletes a real RFQ, which nothing
   catches.

22. THE BACKUP WAS DESTROYED BY THE ATTEMPT TO USE IT (finding #21)
   `backup()` wrote `{PREFIX}{ET-date}.json.gz` with `overwrite=True` — ONE
   blob per day, replaced in place. The second run of any ET day overwrote
   the first, and the case where that matters is exactly the case backups
   exist for: the fire corrupts tracking-data-v2.json, and the recovery
   dispatch pulls the bad state and calls backup(), uploading it over the
   day's only good snapshot. Nothing older than midnight to fall back to.
   Snapshots are now IMMUTABLE: the name carries a UTC time as well, and the
   upload is `overwrite=False`, so no snapshot can be replaced — not by a
   second fire, not by a recovery run, not by a future bug. The ET date still
   LEADS the name, so the retention prune and QC-032's `latest_backup_age_days`
   keep parsing it unchanged. A same-second re-run is treated as already-done;
   any other upload error is raised, because a backup that silently did not
   happen is worse than one that loudly failed.
   Added `restore` and `list-backups`. A backup nobody can restore is not a
   backup — the only way back was hand-fetching a blob and gunzipping it into
   place under pressure. `restore` is gated three ways, per CLAUDE.md's
   destructive-action rule: it is a DRY RUN without `--yes` (reports the
   target, writes nothing); it snapshots the file it is about to replace
   first, so a wrong restore is itself reversible; and the payload must parse
   as JSON with a `requests` list before it can land, the same gate push()
   applies on the way out. The write is atomic.

REVIEW OF #124 — THREE REAL DEFECTS IN BATCH 5's OWN WORK
   All three confirmed by execution first. Two are sites I claimed to have
   fixed and had not.

23. THE 8-WEEK ROLLUP AND THE LANE TABLES STILL USED loss_reason
   Batch 5 said "all three gen_email NQ sites now use core.is_not_quoted".
   There were five. `_week_rows` — which builds the literal "NQ 0 / Q&L 1"
   line quoted as evidence in the finding-#17 writeup — still tested
   `loss_reason == "NO_RESPONSE"`, and so did `_build_lane_buckets`, which
   feeds the Winning/Losing lane tables and additionally charged a
   never-quoted row's TEU to that lane's `teu_lost`. Proved: one
   RESPONSE_NO_RATE row gave aggregate_summary NQ=1/Q&L=0 and _week_rows
   NQ=0/Q&L=1. The exact contradiction the batch existed to remove, still
   shipping in the surface the writeup pointed at.

24. apply_send_signals COULD PROMOTE THE WRONG TERMINAL TO WIN
   Batch 5 fixed the terminal collapse in `apply_rate_responses` and left the
   identical pattern in `apply_send_signals` — same `canonical_lane_key`
   pooling, same bare-substring fallback, no `same_port` narrowing. Since
   that loop tie-breaks purely on the latest request_timestamp, a "Send"
   reply on the Manila (North) thread could flip the Manila (South) row to
   WIN, inheriting its carrier, while the row Lonny actually confirmed stayed
   open and aged out as a loss. Worse than the rate case: a wrong WIN is a
   stronger, more client-facing claim. Same unconditional narrowing applied.

25. QC-075 ESCALATED AFTER THE ARTIFACTS WERE ALREADY WRITTEN
   `phase_7_save` serializes log.errors into BOTH persisted artifacts —
   `data["qc"]["error_log"]` in tracking-data-v2.json and `error_details` /
   `status` in reports/qc-result.json. QC-075 fired AFTER that call, so it
   appended to a list nothing re-serialized. gen_dashboard's QC tab and
   gen_improvements_report's red-flags section read those files, so a
   reconciliation failure was invisible on every surface anyone consumes —
   behaviourally identical to the `print()` QC-075 was created to replace.
   Moved ahead of phase_7_save. The new test drives the REAL phase_7_save and
   reads both artifacts back off disk; batch 5's test had only re-implemented
   the `if` against a mock Log, which is why it stayed green.

SECOND REVIEW PASS ON #124 — FOUR MORE, TWO OF THEM MINE
   All four confirmed by execution before touching anything. The 🔴 was
   caused by my own fix in item 25 above.

26. QC-075 FALSE-FIRED ON EVERY HEALED FIRE (my regression)
   Moving the check ahead of phase_7_save (item 25) put it after phase_6's
   MUTATING heals but before anything rebuilt `data["summary"]`.
   `_trade_region_reconciliation` recomputes the regions fresh from the rows
   while reading `data["summary"]` as-is — so it compared post-heal regions
   against a summary built back in phase 5, and failed on ORDERING rather
   than on any real disagreement. Reproduced with a single QC-067 row: heals
   LOSS/NO_RESPONSE → PENDING, fresh NQ=0 vs stale NQ=1, reconciled=False.
   That is a FALSE QC-075 ERROR persisted into both artifacts on essentially
   every fire that heals a status — and phase_7_save's own recompute a few
   lines later then wrote `reconciled: True` into the SAME qc-result.json.
   The report contradicting itself, produced by the check written to stop
   that. Fixed by rebuilding the aggregates immediately before the check:
   QC-075's job is catching two AGGREGATORS that disagree, never two points
   in time.

27. THE REBUILD FIX WAS COUNTED TWICE (my regression)
   Item 15's fix had phase_7_save call `phase_5_summaries` a second time, and
   that function logs `log.fix("...rebuilt...")` unconditionally — the drift
   flag only changes the message text, it never gates the call. So every run
   recorded the rebuild twice, inflating `data["qc"]["fixes_applied"]`, which
   gen_dashboard renders verbatim as "N fixes", and printing the same line
   twice in the Fixes Applied list. Adding the item-26 rebuild would have
   made it three.
   Split the pure rebuild out as `_recompute_aggregates(data) -> bool`, which
   touches no Log. phase_5_summaries wraps it and logs; the phase-7 and
   pre-QC-075 rebuilds call it silently. One rebuild fix per run, whatever
   the call count.

28. `pol` WAS NEVER PLACEHOLDER-SCRUBBED, AND MY SCHEMA DOC SAID IT WAS
   `_PLACEHOLDER_FIELDS` was ("pod", "destination", "origin"). `pol` was the
   one asymmetry: written the same way as `pod` from free-text OL body
   parsing (adjacent lines in ingest), already listed among QC-064's display
   fields, and exported to durable external surfaces by historian.py and
   share_intel.py — but swept nowhere. A literal "TBD" in OL's POL cell
   survived every scrub and shipped as a port name.
   Pre-existing; what item 18 added was a schema description asserting a
   guarantee the code did not provide. Fixed the CODE, not the doc — there
   was no reason for the exclusion, and papering over it with wording would
   have left the export poisoned. `pol` is now swept with the other three.

29. THE PUSH GUARD'S EXCEPTION ESCAPED ITS OWN HANDLER
   `StateStoreError` is a SUBCLASS of RuntimeError, so main()'s
   `except StateStoreError` never caught the bare `RuntimeError` item 6's
   corruption guard raised. daily.yml's push step got a Python traceback
   instead of the one-line "state_store: REFUSING to push ..." diagnostic the
   guard exists to print. The refusal and the non-zero exit were always
   correct — only the message was lost. Every other raise in the module
   already used StateStoreError; this one now does too, pinned by a
   structural test that fails on any bare RuntimeError/Exception raise
   anywhere in the file.

Tests: tests/test_audit_batch6.py (63 cases), tests/test_state_store.py
updated for the immutable snapshot name. Every fix mutation-checked — the
old code reintroduced, the relevant tests confirmed failing, the fix
restored: 5 fail for the weekly clock, 2 for the merge guard, 5 for the
first review pass, 6 for the second. Suite 2074 passed (2011 -> +63),
coverage 91.28%, ruff clean on the CI-gated paths.

ON THE SECOND PASS'S TESTS: my first behavioural QC-075 test survived
reverting the fix, because it drove the phase sequence itself instead of
reading main()'s. That is the SAME weakness the reviewer had just flagged in
the previous QC-075 test. Both are now pinned structurally from the AST —
main() must rebuild before it reconciles and reconcile before it saves, and
phase_7_save must call the silent rebuild — so drift in main() fails here
rather than in production.

DECISIONS FOR MICHAEL
   * `DEFAULT_PRICING_CPM["claude-opus-4-6"]` is still ~3x overstated. Left
     deliberately — a retroactive correction breaks comparability with every
     historical cost figure already reported. Say the word and I will change
     it forward-dated instead.
   * The 45% prompt-cache estimate in Anthropic's notice is org-wide direct
     API traffic. This repo's own saving is cents; whichever IDEALX system
     generates that spend is not this one, and I cannot identify it from here.

## 2026-07-27 — LLM layer: Opus 4.6 -> Claude Opus 5, + prompt-cache breakpoint

Michael forwarded Anthropic's "prompt cache hit rate is low" notice (est. up
to 45% of IDEALX direct API spend) and asked to migrate then add caching.

WHAT I MEASURED FIRST (before changing anything)
   Four LLM call sites in this repo. Prompt caching is a PREFIX match with a
   model-specific minimum, and below that minimum `cache_control` is a SILENT
   no-op — no error, no cache, just the ~1.25x write premium. Measured
   against a deliberately-padded InsightsContext:

     insights.py (4 calls/day)  ~8,300 chars (~2,100-2,800 tok)  opus-4-6, min 4096  NO
     parser_fallback.py         250-char system                  haiku-4-5, min 4096  NO
     qc_actions_from_sentry.py  no system prompt at all          haiku-4-5, min 4096  NO
     pdf_llm_rescue.py          base64 PDF FIRST, differs/call    -                    NO

   So caching on the OLD model would have done nothing. The minimum is not
   monotonic across generations — 4096 on Opus 4.6, 512 on Opus 5 — which is
   why the migration is what makes caching viable, not the marker.

1. DEFAULT_MODEL: claude-opus-4-6 -> claude-opus-5 (three Opus generations)
   TWO breaking changes had to be handled; both fail silently, not loudly.

   (a) THINKING IS ON BY DEFAULT. On Opus 4.6/4.7/4.8 a request that omits
       the `thinking` parameter ran WITHOUT thinking. On Opus 5 the same
       request runs adaptive thinking — and `max_tokens` caps thinking PLUS
       response text together. `_invoke` sends no `thinking` field, so every
       narrative call would have started spending its 4096-token budget on
       reasoning and truncating the answer mid-sentence, with no error and
       just `stop_reason: "max_tokens"`. Fixed with MAX_TOKENS_FLOOR (16000)
       applied to models in THINKING_BY_DEFAULT.
       NOT fixed by disabling thinking: that is capped at effort<=high on
       Opus 5 and carries two documented failure modes (tool calls emitted as
       plain TEXT so the call silently never runs; <thinking> tags leaking
       into the response). Thinking on with a real budget is the safe config.

   (b) Sampling params (`temperature`/`top_p`/`top_k`) and `budget_tokens`
       return 400 on Opus 4.7+. This router never sent any — verified, and
       now pinned by a test so it stays that way.

   parser_extraction stays on claude-haiku-4-5. That is a deliberate cost
   choice for a high-volume, structurally simple task and was not in scope.

2. PROMPT-CACHE BREAKPOINT (ModelRouter._system_param)
   Marks the system block with `cache_control` when it clears the model's
   minimum. Caches tools+system together (render order tools -> system ->
   messages). BEHAVIOUR-NEUTRAL: it adds a field and reorders nothing, so the
   model is asked exactly the same thing — pinned by a test.

   HONEST CEILING, measured not assumed: the four insights tasks each send a
   DIFFERENT system prompt, so they share no prefix with each other, and the
   pipeline runs once every 24h against a 5-minute TTL (1h max) — so there is
   no cross-task and no cross-run hit. What this covers is the retry and
   Sonnet-cascade paths, which re-send an identical prefix seconds apart.
   Real saving in THIS repo: cents. The 45% in Anthropic's notice is org-wide
   and cannot be coming from here — see the open question below.

   The bigger win needs the shared ctx_json moved AHEAD of the per-task
   instruction so all four calls share a prefix. That inverts the
   data/instruction ordering and would change the output of all four
   narrative sections, so it is a separate decision, not folded in silently.

   Kill switch: HILMAR_LLM_CACHE=0 reverts to plain-string system prompts.

3. FOUND WHILE MIGRATING — the cost table is ~3x high on Opus 4.6
   DEFAULT_PRICING_CPM lists claude-opus-4-6 at 1500/7500 cents per MTok
   ($15/$75). Anthropic's published price is $5.00/$25.00 — the same as
   Opus 5. Every cost_cents figure logged against Opus 4.6 has therefore been
   overstated ~3x, which also means the HILMAR_INSIGHTS_COST_ALERT_CENTS
   banner has been firing early. LEFT AS-IS deliberately: correcting it
   retroactively makes historical llm-cost-log entries incomparable, and that
   is Michael's call. The new Opus 5 row (500/2500) is correct.

OPEN QUESTION FOR MICHAEL: Anthropic's notice covers IDEALX org-wide direct
API traffic and explicitly EXCLUDES Claude Code. This repo's direct traffic is
4 Opus calls/day plus budgeted Haiku parser calls — 45% of that is pennies.
The traffic driving that estimate is another IDEALX system (the OL-USA quote
tracker on the Cloud PC is the obvious candidate). I cannot see it from this
container. Point me at it and I will run the same measurement there.

Tests: tests/test_model_router_opus5.py (15 cases) — the max_tokens floor and
that it only applies to thinking models, no removed params sent, thinking
never disabled, the breakpoint on/off either side of each model's minimum,
Haiku's far-higher minimum, behaviour-neutrality, the kill switch, and a guard
that every routable model has a declared minimum. Four existing tests pinning
the old default/pricing updated.
Suite 1940 passed (1925 -> +15), coverage 91.23%, ruff clean.

## 2026-07-27 — Data audit batch 4: the last four priority findings

Each defect REPRODUCED on main before it was touched, re-verified after.
One new QC check (QC-074) and one schema change.

1. BOOKING->REQUEST MATCHING WAS DECIDED BY STAGE-FILE ORDER (ingest.py)
   The header chain (In-Reply-To/References) picked the FIRST row it
   encountered whose imid appeared anywhere in the chain. When Lonny REUSED a
   thread, that made row ordering decide a business outcome:

       stage holds NEW first -> booking landed on req_new
       stage holds OLD first -> booking landed on req_old

   Same inputs, same day, opposite result. The new still-unanswered 1x20'DV
   RFQ got stamped WIN carrying a 2x40'HC booking it never asked for and
   vanished from PENDING OL, while the genuinely quoted 2x40'HC row sat open —
   the operator's 2026-07-22 Oakland->HCMC report. QC-066 could not catch it:
   the stolen booking is same-day, so both its clauses pass.
   Fixed: the chain is now a candidate FILTER, not a decision. Every chain
   member goes through the same evidence scoring the lane fallback already
   used (container count + carrier from the booking subject), candidates Lonny
   sent AFTER the booking are dropped outright, ties break to the latest ask
   before the booking, and a candidate whose container count CONTRADICTS the
   booking subject is penalised. Ambiguity is recorded in
   `_booking_match_via` ("chose 1 of N in-thread by evidence") rather than
   resolved silently. Extracted to a module-level `_pick_best_request` after
   ruff flagged the closure's late-bound loop variables (B023) — a real
   hazard, not a style nit.

2. NEVER AGE ON ABSENCE (core.decide_status, both trees)
   A quoted row whose response_timestamp was missing or unparseable returned
   LOSS/OTHER instantly — "assumed aged". That is exactly what patch_carriers
   produces when it recovers a rate from a sibling thread or a booking PDF
   carrying no usable timestamp. Proved: an RFQ sent 2 HOURS AGO returned
   LOSS/OTHER. It was reported to staff AND to the client as a loss, counted
   against win rate, dropped from auto_chase_pending, and absent from every
   pending bucket — so PENDING OL read 0 and nobody followed up on live
   business the system had already buried.
   A missing timestamp is missing EVIDENCE, not elapsed time. The row now
   falls back to Lonny's request clock and holds PENDING until THAT window
   expires; only then is it a loss. Tagged NO_RESPONSE_TS (new loss reason,
   both trees + schema.json enum) so the cause is legible instead of hidden
   inside the "OTHER" catch-all. Three existing tests pinned the old OTHER
   label — updated, intent preserved; the schema enum test caught the missing
   enum value, which is the governance ratchet doing its job.

3. CARRY-FORWARD APPENDED A SECOND ROW UNDER THE SAME request_id (ingest.py)
   The RFQ email is still inside the 90-day stage window, so the fresh build
   rebuilds the row as PENDING with no knowledge of the booking; the additive
   carry-forward then APPENDED the old WIN beside it. tracking-data-v2.json
   reported 2 entries and 8 TEU for one 4-TEU shipment, the same id
   simultaneously PENDING/NQ and WIN — and phase_4 collapsed them by counting
   non-empty FIELDS, in one observed run discarding the very win the
   carry-forward exists to protect (MDOLX260500 Oakland->Yokohama).
   Fixed: reconcile by request_id FIRST. A prior WIN whose id already exists
   MERGES its evidence into the rebuilt row (`_merge_prior_win_into`) instead
   of appending — booking ref, carrier, booked TEU, refs unioned — and records
   the transition so QC-072's invariant holds. Evidence fills gaps only, so a
   fresher carrier signal is not clobbered. A status contradiction is never
   resolved by counting fields: an mdolx-backed WIN is evidence, a rebuilt
   PENDING is the absence of it.

4. THE REPORT ARGUED WITH ITSELF ABOUT WINS (gen_email.py)
   The "Won — <day>" KPI tile required `status == "WIN"`; the What-Happened
   block counted every ->WIN transition dated that day regardless of where the
   row ended up. A row promoted on a send-signal and later re-decided away
   (aged to SEND_NO_BOOKING, or held MDOLX_NO_SEND) satisfied one and not the
   other — so ONE email read "Won — Wed Jul 22: 0" in the KPI strip and
   "· 1 wins ·" eight inches below, with a green PENDING -> WIN pill under it.
   Michael has flagged this shape repeatedly ("CHECK YOUR REPORT").
   Fixed: `_win_landed(r, h)` is now THE single rule both surfaces use — a
   transition that was subsequently reversed is not a win. The STATUS CHANGES
   table applies it too: the event is still shown (it happened), but labelled
   "REVERSED, now LOSS (SEND_NO_BOOKING)" instead of rendering as a win.

QC-074 (ERROR/WARN) — win evidence vs outcome. ERROR on a duplicate
request_id (one shipment stored twice, TEU counted twice) and on a row
carrying an mdolx_ref while reported as a loss; WARN on a WIN with neither
mdolx_ref nor has_send. MDOLX_NO_SEND is exempt — that state is DEFINED as
holding a booking ref without a win. Detect-only; shape (a) is prevented at
the source by fix 3.

SCHEMA CHANGE: loss_reason enum gains "NO_RESPONSE_TS" (schema.json +
LOSS_REASONS in both core trees). Additive only — no existing value changed,
so prior data stays valid.

Tests: tests/test_audit_batch4.py (27 cases) — order-independence proved by
running the same inputs both ways, the after-the-booking guard, chain-still-
beats-lane, ambiguity flagging, all four never-age-on-absence outcomes,
cross-tree parity, the schema enum, merge idempotency and no-clobber, and both
win surfaces agreeing on a reversed win AND on a real one.
Suite 1925 passed (1898 -> +27), coverage 91.17%, ruff clean.

Backlog after this batch: 11 confirmed findings remain, all MEDIUM/LOW — the
last HIGH-severity finding is closed.

## 2026-07-26 — Data audit batch 3: the standalone-WIN cluster

Findings 2, 11 and 22 turned out to be ONE root cause, reproduced on main
before anything was touched.

THE DEFECT
   The only lane key in the system was `.strip().lower()`, so "HCMC" and
   "Cat Lai" were different lanes. Lonny's RFQ says "Oakland to HCMC"; OL's
   booking confirmation says "Oakland to Cat Lai". The booking could not be
   linked, so link_bookings_to_requests fabricated a stand_<mdolx> WIN row
   beside the real request:

       req_abc       lane=Oakland → HCMC     status=PENDING  teu_requested=2
       stand_260999  lane=Oakland → Cat Lai  status=WIN      teu_won=2

   One shipment, two rows. TEU double counted, a phantom lane in Lane
   Performance, and 24h later the orphaned PENDING copy ages into a LOSS
   reporting that OL never quoted a move OL had actually booked. This is the
   operator's reported defect #2.

Three layers, because preventing it and detecting it are different jobs:

1. PREVENT — core.canonical_port_key (both trees), one key per physical
   destination, used on BOTH sides of every destination comparison so the two
   sides cannot disagree about what counts as the same place. HCMC / Cat Lai /
   Cai Mep / Ho Chi Minh / Saigon collapse; so do Manila North/South, Busan /
   Port Busan / Pusan, and the three Lat Krabang spellings. Resolution is
   whole-string, then the head before a parenthetical, then the parenthetical
   itself, so "HCMC (Cat Lai)" and "Vietnam (Cat Lai)" both land on hcmc.
   DELIBERATELY CONSERVATIVE: Bangkok/Laem Chabang and Tokyo/Yokohama are NOT
   merged — collapsing distinct ports would cross-match real separate
   business, a worse failure than the one being fixed. Unknown names fall
   through to their own lowercased head, so this is a strict refinement of the
   old behaviour: it can only merge names the map lists, never split ones that
   used to match. Verified: the booking now links to the real row, TEU counts
   once, an unrelated Hamburg RFQ is not consumed, and a booking with no
   matching request still becomes a standalone (that path is legitimate).

2. DETECT — QC-069 had TWO gaps that made it miss the exact pair it was
   written for. Verified by execution: it returned [] on that pair.
     (a) its alias set only split a PARENTHETICAL, so two rows saying plain
         "HCMC" and plain "Cat Lai" produced disjoint sets. Now anchored on
         canonical_port_key — the same key ingest matches with, so detection
         and prevention cannot disagree.
     (b) it compared container specs as raw strings: Lonny writes "1x40HC",
         OL writes "1X40'HC", and case-folding leaves those different. Now
         compares parsed (count, TEU) — that is what double-counts in the
         rollups, and it is spelling-proof.

3. CONTAIN — standalone rows no longer carry invented values.
     * A destination resolving to the SAME port as the origin is now treated
       as unresolved instead of emitting "Oakland → Oakland", which had been
       rendering in Lane Performance as a real trade lane (defect #3). Happens
       on re-forwarded / return-leg confirmations naming only one port.
     * response_timestamp stays None, the SAME rule the MATCHED path spells
       out 100 lines up ("we never saw a rate response — the booking arrived
       directly"). The standalone path contradicted it and wrote the booking
       time, making the row claim an OL rate quote at a moment OL only sent a
       booking confirmation — the identical 171-hour turnaround defect fixed
       on the matched path in 2026-05-19 and left live on this one.
       booking_timestamp now carries the chronology.

QC-073 (ERROR/WARN) — standalone booking row hygiene. ERROR on a degenerate
lane and on a fabricated rate response; WARN listing standalone WINs with no
carrier_won (an unattributable win the operator must chase). Detect-only:
these are real bookings, and the fix is to link them or correct the source
subject. Both ERROR shapes are now prevented at the source, so reaching the
check is a regression.

Tests: tests/test_audit_batch3.py (39 cases) — the alias table, the
NOT-merged guard on distinct ports, cross-tree parity, the end-to-end link,
TEU counted once, over-matching guards, both QC-069 gaps, and all three QC-073
shapes. Suite 1898 passed (1859 -> +39), coverage 91.17%, ruff clean.

Backlog after this batch: 15 confirmed findings remain (2 HIGH) — booking-to-
request match decided by arbitrary stage-file order, additive carry-forward
appending a second row under the same request_id, quoted rows with an
unparseable response_timestamp aging with zero grace, and the remaining
Won-tile vs What-Happened contradiction.

## 2026-07-26 — Data audit batch 2: send evidence, day-bucket clock, audit trail

Second half of Michael's "DO BOTH". Four defects, each REPRODUCED on current
main before it was touched, each re-verified after. Two new QC checks
(QC-071, QC-072) so a regression is caught on live data the next morning.

1. has_send WAS ERASED ON SEND_NO_BOOKING (core.decide_status, both trees)
   The branch returned has_send=False on the one loss reason that MEANS the
   send happened. has_send is an EVIDENCE field ("did Lonny accept?"), not a
   state field. Because qc_selfheal writes the decision back onto the row
   (r["has_send"] = decision.has_send), the NEXT pass re-read has_send=False,
   fell through to the quote-aging branch and relabelled the row
   UNDIFFERENTIATED — "we lost, cause unknown". Unrecoverable. The
   OL-dropped-the-ball signal vanished from the loss mix, the carrier
   scorecards (_OL_SILENT) and the improvement report.
   Proved: pass1 SEND_NO_BOOKING/has_send=False -> pass2 UNDIFFERENTIATED.
   A SECOND site did the same thing — qc_selfheal cleared has_send on EVERY
   LOSS, which would have wiped the flag straight back out. Now exempted for
   SEND_NO_BOOKING only; clearing stays for every other reason, where
   has_send genuinely is contradictory. Invariant now tested both ways.

2. NEW DEFECT FOUND WHILE FIXING (1): MDOLX_NO_SEND SELF-PROMOTED TO WIN
   Not on the audit list — found by testing the adjacent branch. The branch
   is literally `has_mdolx and not has_send`, yet scripts/core.py returned
   has_send=TRUE, asserting the opposite of the condition that reached it.
   Same feedback loop: pass 2 re-read has_send=True alongside the MDOLX and
   took the WIN branch. An anomaly explicitly HELD for ops review silently
   became a WIN one fire later, with no send signal and nobody looking.
   src/hilmar/core.py already returned False — so this was PRODUCTION-ONLY,
   the exact "green in CI, wrong on the box" split the parity tests exist to
   catch. Proved both trees side by side, fixed scripts/ to match.

3. request_date RAN ON THREE DIFFERENT CLOCKS (ingest, merge_ingest, qc heal)
   ingest wrote the UTC calendar date, merge_ingest took a raw ts[:10] UTC
   slice, and qc_selfheal's heal wrote PT — while every reader buckets by the
   ET business day (core.report_business_day). An RFQ Lonny sent Friday 5:30
   PM PT is 2026-07-25 in UTC and Friday 2026-07-24 in ET; since no fire ever
   reports a Saturday, that row appeared in NO day's New Requests, KPI tile or
   day reconciliation — on any day, ever — while still counting in the period
   totals. So the day tiles and period tiles disagreed by exactly the rows the
   clocks disagreed about, and the day reconciliation still balanced because
   the row was never in the denominator.
   Fixed with ONE canonical helper, core.et_date_of (both trees), called by
   all three producers — a fourth convention can't quietly appear. Date-only
   strings pass through untouched (re-reading them as midnight UTC would shift
   them a day, in the exact direction this prevents); unparseable input
   returns None so callers fall back rather than invent a date.
   The phase_3 heal now RECOMPUTES request_date every pass instead of only
   filling it when missing — which is also the MIGRATION, since every row
   already stored on the wrong clock would otherwise have kept its wrong day
   forever. The legacy `date` mirror is kept in step (readers fall back to it).
   Verified: the Friday-evening RFQ now lands on Friday's report, and on no
   other day.

4. ROWS CONTRADICTED THEIR OWN AUDIT TRAIL (ingest.age_requests, merge)
   status was assigned directly instead of through core.record_transition, and
   merge_idempotent recomputed `status` (in _RECOMPUTED_FIELDS) while
   PRESERVING the old status_history (not in it). A send-signal WIN that never
   booked read status="LOSS"/SEND_NO_BOOKING with history still ending at
   {"to": "WIN"} — and status_history is the field schema.json declares as THE
   transition record, so audits, the dashboard timeline and Sentry triage all
   reported the row as WON, with no entry anywhere explaining the regression.
   It also kept teu_won=2 for a shipment that was never booked.
   Fixed: both ingest mutators route through record_transition; a hand-rolled
   history append that hardcoded "from": "PENDING" (wrong from any other
   state) and fired even when the row was ALREADY WIN was deleted; leaving WIN
   clears teu_won via _clear_win_evidence_on_exit — deliberately narrow, only
   the volume, never the has_send/mdolx_ref evidence (that is defect 1).
   merge_idempotent now UNIONS status_history rather than keeping the stale
   copy. First attempt deduped on `at` and an EXISTING ingest test caught it:
   a fresh run re-derives the same transition with a new timestamp, so every
   daily fire would have appended another copy — unbounded growth. Dedup is
   now on CONSECUTIVE (from, to, reason), keeping the earliest, so a genuine
   WIN -> LOSS -> WIN keeps both WIN entries and [-1] stays the true state.

QC-071 (ERROR) — request_date != its own timestamp in ET. Heals in phase_3
(recompute + migrate); reaching the check means the heal missed the row.
QC-072 (ERROR) — status_history[-1]["to"] != status, or teu_won > 0 on a
non-WIN row. Detect-only: rewriting history is never a safe automatic act.

Tests: tests/test_audit_batch2.py (30 cases). Suite 1859 passed (1829 -> +30),
coverage 91.16%, ruff clean, QC governance ratchet green.

Backlog after this batch: 18 confirmed findings remain (5 HIGH) — booking-to-
request match decided by arbitrary stage-file order, the HCMC/Cat Lai lane
alias gap that CREATES the duplicate rows QC-069 only detects, additive
carry-forward appending a second row under the same request_id, quoted rows
with an unparseable response_timestamp aging with zero grace, and the
remaining Won-tile vs What-Happened contradiction.

## 2026-07-26 — TEU sanity ceiling: QC-070 (ERROR + self-heal)

Michael: "DO BOTH" — this is the first half (the sanity cap); the confirmed
backlog is the second and continues in the next batch.

WHY, WHEN THE REGEX WAS ALREADY FIXED THIS MORNING: batch 1 hardened
`core.parse_teu` so "PO 4451440" stops parsing as 44,514 x 40' = 89,028 TEU.
That fix is correct, but it left the whole defence resting on ONE regex. Every
volume figure in the daily email, dashboard, PDF and lane rollup is a SUM over
rows, so a single bad row rewrites the day's numbers, and a wrong-but-huge
number is invisible until a human reads the report and disbelieves it. This
adds the second line of defence, so a future regex regression costs a zero
instead of an 89,028.

1. PER-ROW CEILING (core.MAX_ROW_TEU = 100, MAX_ROW_CONTAINERS = 60; both trees)
   New `core.teu_implausible(count, teu)` returns a human reason, or None.
   `parse_teu` now REFUSES a parse above the ceiling — returns (0, 0) rather
   than the poisoned figure. The failure mode is chosen deliberately: zero is
   visibly wrong and QC-070 flags it; 89,028 silently rewrites every rollup.
   Calibration: the largest real Hilmar sample on record is 6x40'RF = 12 TEU,
   so 100 TEU (50 forty-foots in ONE request line) is ~8x beyond anything the
   business has ever asked for. Verified: every real spelling on record still
   parses identically ("1x40HC", "2-20'", "4X40'RF", "3x20'DV + 1x40'HC",
   "40'HC x 2"), while "999 x 40'HC" and "40'HC x 800" — well-formed parses
   that the regex reads CORRECTLY, i.e. exactly the case a regression lands in
   — are refused.

2. QC-070 (ERROR) — TEU that cannot be real
   (a) OVER-COUNT, SELF-HEALED: a stored `teu_requested` / `teu_won` /
       `container_count` above the ceiling is recomputed from the row's OWN
       `containers` text via `parse_teu`, so a heal can never write a value
       ingest would itself refuse. `teu_won` clears to 0 on a non-WIN row —
       it is win evidence, not a volume. This catches numbers ALREADY in the
       dataset: written by an older build, a carry-forward, or a hand edit.
   (b) 0-TEU SHAPE, DETECT-ONLY: a row whose `containers` text plainly names
       equipment yet recomputes to 0 TEU — either a parser gap or a parse
       refused as implausible. Not healed: healing it would mean INVENTING a
       volume, which is the exact class of guess that caused the defect.
   Verified end-to-end through the live `phase_6_rules` path on a poisoned row
   (89,028 TEU / 44,514 containers): both fields errored and healed to 4 TEU /
   2 containers from the row's own text; the clean row alongside it was
   untouched.

3. WHY QC-006 WAS NOT ENOUGH — recorded, because it looks like a duplicate
   QC-006 (WARN, >30 TEU) DID fire correctly on the 89,028 row on 2026-07-26,
   and the report shipped anyway: a WARN neither gates nor heals, and QC-006
   only ever inspected `teu_requested`. Both checks stay. QC-006 is the
   advisory band ("unusually large, eyeball it"); QC-070 is the hard ceiling
   ("impossible, stop"). Different questions, different answers. QC-006's row
   in QC-INDEX.md and its call site now say so, so nobody re-litigates it.

Tests: `tests/test_teu_sanity_cap.py` (34 cases) — the ceiling constants, both
known real defects (89,028 and the 200 TEU "quote 10040" misread), every real
spelling, cross-tree parity of the constants AND the refusal, all four QC-070
branches, heal=False non-mutation, and junk field types (booleans are ints in
Python and must not read as volumes). One test guards the CALIBRATION itself:
it fails if the ceiling is ever tuned down toward real volumes.
Full suite 1829 passed (1795 -> +34), coverage 91.17%, ruff clean.

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
