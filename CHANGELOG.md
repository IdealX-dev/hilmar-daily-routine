# Changelog — Hilmar Daily Tracker

Per the working standard (CLAUDE.md): every session logs its decisions here,
by name, so the next session starts current. Newest first.

### 2026-08-10 (5) — the "4 missing bookings" were 1 thread, correctly excluded

CLOSED, and it was never an ingest bug. `admitted but NO win row: 4` had been
printed on three separate runs across this session and never resolved, because
the MDOLX numbers were logged ~300 lines up, above a 200-line mailbox scan, off
the end of every log fetch. Fixed that first (see below), then read them:

  MDOLX260821  staged 2026-07-13, -14, -20, -20
      RE: Hilmar, CA to La Guaira, Venezuela - S38083 / MDOLX260821 -
      Puerto Cabello. / EBKG17621387

ALL FOUR ARE THE SAME MDOLX. Not four bookings — four messages of one thread.
The pipeline dedupes to one booking per MDOLX (Michael: "1 MDOLX = 1 win"), so
the verdict block was counting in emails against a pipeline that counts in
bookings, and inflating every number in it.

AND THE ONE BOOKING IS NOT A LOST WIN. From the deep trace, a sibling message
in the same thread:

  2026-07-22  gate=out_of_scope:numidia
      RE: MDOLX260821_Load appointment needed for 1 x 40' HC /
      Agri Dairy Vendor Reference PO00-26002163 / 93348…

MDOLX260821 is AGRI DAIRY cargo. The subject says "Hilmar, CA" because Hilmar,
California is the ORIGIN CITY — not because Hilmar Ingredients is the customer.
No Hilmar win row is the CORRECT outcome. [Certain, from the emails.]

WHY IT READ AS ADMITTED. ingest's client test is
`is_hilmar = "HILMAR" in subject.upper()` (ingest.py:679). An origin-city match
is indistinguishable from the "// HILMAR" customer tag, so ten messages of
another customer's thread passed that gate. The comment on that line already
records the same failure in 2026-05-17 with NUMIDIA. NOT CHANGED HERE — the
gate is doing the job it was narrowed to do, the out-of-scope rule catches the
thread elsewhere, and the outcome is right. Recorded so the next session knows
the signal is weak rather than rediscovering it.

DECISION — fix the diagnostic, not the pipeline. Two changes to diag_bookings:
  - the verdict counts MESSAGES **and** BOOKINGS, so a ten-message thread stops
    reading as ten findings
  - before blaming ingest, it checks whether a rowless MDOLX has siblings gated
    out_of_scope, and says "another customer's move, this is not a lost win"
    instead of "the loss is AFTER the gates… that is ingest, not intake"
That second line is what sent me chasing a defect that does not exist. A
diagnostic that states one cause unconditionally is not a diagnostic.

Also fixed en route: the rowless MDOLX numbers are now printed in the VERDICT
block, not only where they are found. A finding that scrolls out of reach is a
finding nobody has — this one survived three measurements that way.

Suite 2661 passed, 0 failed. ruff clean.

### 2026-08-10 (4) — the QC-027 numbers, measured on the live data

diag-qc027 run 1 (`1dc1b26`, 340 requests / 329 reachable — the SAME row count
the Sentry alert reported, so this is the same dataset):

  field           BEFORE (old ruler)        AFTER (fixed ruler)
  ETD             329/329  100.0%           329/329  100.0%
  ETA             307/329   93.3%  WARN     307/329   93.3%  WARN
  Vessel/Voyage   328/329   99.7%           328/329   99.7%
  Rate            329/329  100.0%           329/329  100.0%
  Carrier         319/329   97.0%           319/329   97.0%
  POL             329/329  100.0%           329/329  100.0%
  POD             329/329  100.0%           329/329  100.0%

NO FIELD IS BELOW 90%. QC-027 would not fire an ERROR on today's data at all.
Carrier is 97.0%, not 87%. ETA at 93.3% matches the alert's "ETA=93% (WARN)"
exactly — same dataset, and the only field still under 95%.

CORRECTION TO MY OWN ACCOUNT. I expected the diag to show the heals lifting
Carrier from ~87% to something higher. It does not: BEFORE == AFTER on every
field, and the run applied 1 fix, 0 of them touching a QC-027 field. The reason
is that the STORED state is already healed — QC-056's backfills persist to
tracking-data, so a later run has nothing left to repair. The diag cannot
reproduce the alert-day reading, because the data it would have read no longer
exists. That is a real limitation of this diagnostic and is now written into
the script rather than left for the next reader to trip over.

What the ordering defect actually cost is therefore this, and only this: on a
day when unhealed rows ARRIVE, QC-027 reported the pre-heal number and paged
Michael for a shortfall the same run was already fixing. The page was real; the
shortfall was transient. [Likely, not proven] the 87% was one such reading —
33 carriers separate 87% from 97% on 329 rows, and nothing in the stored state
records when they were filled.

"IT USED TO WORK" — ANSWERED FROM THE DATA. The 10 rows still missing a carrier
are 9 from April 2026 and 1 from May. NONE from June, July or August. Nothing
recent is failing; the misses are old rows that predate the current parser, and
every month since is clean. There is no new regression to find.

A PREDICTION OF MINE THAT THE DATA REFUTED: I wrote that rows reachable by
etd_offered or vessel_voyage alone would sit inside QC-027's denominator and
outside QC-056's reach, since QC-056 only looks at rows WITH a rate. That
bucket is EMPTY — all 10 stragglers have a rate and are squarely QC-056's
target; the parser simply finds no carrier token in them. PDF-only rows are
also 0. The gap I named in advance does not exist in this dataset.

REMAINING, not urgent and not new: ETA at 93.3% is a WARN (307/329, 22 rows).
It was a WARN in the alert too. Not touched this session.

### 2026-08-10 (3) — QC-027 graded the rows before the heals repaired them

Michael, on the daily Sentry page: "you have to fix this.. it used to work..
don't know what you did."

MEASURED, NOT ARGUED. Nothing broke the carriers. QC-027's measurement sat
~1200 lines up inside phase_6_rules, ahead of two heals that write the very
fields it grades — both in the same function, same pass, top to bottom:

  QC-027  measures carrier_quoted, pol, pod, vessel_voyage, …   line ~3900
  QC-056  BACKFILLS carrier_quoted from row text, then from a
          same-lane same-rate sibling                           line ~4290
  QC-064  NULLS garbage out of carrier_quoted / pol / pod /
          vessel_voyage and five other client-visible fields    line ~4717

Four of QC-027's seven graded fields are written by QC-064; one is written by
QC-056. So "Carrier=87% (ERROR <90%)" described a state that did not survive
its own run — it counted as MISSING every carrier QC-056 was about to restore,
and as PRESENT every value QC-064 was about to blank. Wrong in both directions,
every day.

FOURTH INSTANCE OF THIS SHAPE in this one phase: QC-039 (2026-07-27, which
withheld a business day's client report), batch-5 #15's persisted aggregates,
QC-075's stale summary, now QC-027. QC-039's banner already states the rule —
A GATE MEASURES THE FINAL STATE OF THE ROWS, AFTER EVERY MUTATING HEAL —
and QC-027 was simply never brought under it.

DECISION — split the check rather than move it whole. The MEASUREMENT moved to
the end of phase_6_rules, beside QC-039. QC-027's own POL/POD derivation stays
where it was, on purpose: it WRITES pol/pod, and QC-064 scrubs pol/pod later in
the same phase, so a heal dragged down with the measurement would put a derived
value in the client email with nothing checking it. Heals early, measurement
last.

DECISION — the denominator now has exactly one definition. `qc027_active_rows`,
`qc027_is_reachable` and `QC027_FIELDS` are named in qc_selfheal and called by
both the check and the diagnostic. A completeness number is a ratio, and a
ratio moves when the DENOMINATOR moves; a re-typed comprehension in a diag
script answers questions about a set nobody is measuring. Side effect: the
reachable/PDF-only split was `r not in _reachable`, an equality scan over
dicts — two identical rows collapsed, and the halves could overlap. Both sides
now call the same predicate, so the partition is disjoint and exhaustive by
construction (and O(n), not O(n²)).

GUARD — tests/test_qc027_measures_final_state.py (11 tests). The structural one
is an AST walk over every write to a graded field inside phase_6_rules,
INCLUDING variable-key writes (`r[_f] = None`, which is exactly how QC-064
nulls). A substring scan for `r["carrier_quoted"]` cannot see that write — the
same blind spot that let the first QC-039 fix ship half-done. Verified
non-vacuous by mechanically moving the measurement back above QC-056: 4 of 11
failed, including both behavioural directions —

  test_a_row_qc056_can_heal_is_not_counted_as_a_missing_carrier   FAILED
  test_a_carrier_qc064_nulls_is_counted_as_missing                FAILED

WHAT IS NOT YET KNOWN, stated plainly: whether the post-heal number clears 90%
on the real 329 rows is a fact about the data, not the code, and no unit test
can answer it. scripts/diag_qc027.py + .github/workflows/diag-qc027.yml print
both readings on the live dataset — the one the old code sent to Sentry and the
one the fixed code sends — plus every row still missing a carrier, bucketed by
request month (does "it used to work" mean new rows or old ones?) and by why it
is unhealed. One bucket is named in advance because the code says so: QC-056
only ever looks at rows WITH a rate, so a row reachable by etd_offered or
vessel_voyage alone is inside QC-027's denominator and outside every healer's
reach. Read-only — pulls state, runs the phase on a deepcopy, writes nothing.

Suite 2656 passed, 0 failed. ruff clean.

### 2026-08-10 (2) — the booking was built from the wrong email in the thread

Michael: "read the emails.. that's your job to decide if it's a problem with
the file or a new win." Read them. Both answers, in order.

NOT A MISSING WIN. MDOLX260769 and MDOLX260797 are already WIN rows, created
in June from real confirmations. The August 5 messages are "UPDATED ETA
BOOKING CONFIRMATION" — revisions of those June bookings, correctly not new
wins. bookings=0 for Aug 3-7 is right about these two.

BUT ONE ROW IS BADLY WRONG, and that IS the file problem:

  stand_260769   carrier_won=CMA CGM  teu_won=0  etd=22-Apr-26  eta=26-May-26
  req_5d2685f3…  carrier_won=CMA CGM  teu_won=8  etd=1-Jul-26   eta=2026-07-25

Zero TEU on a 3X40'RF — should be 6 — and sailing dates two months BEFORE the
16 June booking existed. Impossible on their face.

THE CAUSE, visible once the thread is read. Two emails, three minutes apart:

  17:14:37  MDOLX260769_ *NEED UPDATE TO BOOKING # NAM8482648 // HILMAR
  17:17:34  MDOLX260769_ *NEW BOOKING CONFIRMATION // HILMAR -
            Oakland to Osaka - 3X40'RF // CMA BKG # NAM8482648

collect_bookings kept "the earliest sighting of this MDOLX = booking creation
time" — so it chose the 17:14 email, which is OL asking CMA to CHANGE a
booking ("Can you please update this booking per below: -Reduce to 3 x 40'RF").
That subject carries no lane and no container spec, and the row parses lane,
carrier, containers and TEU FROM THE SUBJECT of whichever email is chosen. So
every one of those fields came out empty or garbage, and the real confirmation
three minutes later was discarded.

FIXED by ranking instead of racing: NEW BOOKING CONFIRMATION > any other
BOOKING CONFIRMATION > everything else, ties broken on earliest. The original
creation therefore still beats a later "UPDATED ETA" revision — trading this
bug for a re-dated win would have been worse than leaving it.

BLAST RADIUS: which EMAIL represents a booking, never WHETHER an MDOLX becomes
one. The gates above are untouched and a test pins the booking set.

Tests use the verbatim production subjects from the trace, and assert the
consequence rather than the choice: the chosen subject must parse to 6 TEU,
and the ops message must parse to 0 — the exact number that shipped. Verified
by restoring earliest-wins and watching four of seven go red.

WHAT THIS DOES NOT EXPLAIN: the 4 bookings that passed every gate and produced
no win row at all. Still open, still unidentified.

Suite 2635 passed, 0 failed. ruff clean.

### 2026-08-10 — we told Lonny a shipment was booked when it was not

Michael: "data missing.. you sent lonny we won no shipment last week."

He is right, and the claim went out under headings that promised precisely
what we could not support:

  gen_client_email   "Booked shipments — upcoming and in transit"
                     "Your confirmed bookings that have not yet reached…"
  gen_client_email   "Bookings confirmed" / "Shipments confirmed on <day>"
  gen_client_weekly  bookings, teu_booked, active_shipments

Every one selected on status == "WIN" alone. A row flips to WIN on a
SEND-SIGNAL — Lonny saying "please send" — and only becomes a real booking
when OL issues an MDOLX confirmation. Between those two moments it is a WIN
with nothing behind it. Both templates then rendered the reference cell as
`mdolx_ref or "Confirmation to follow"`, which tells the customer a
confirmation is coming when nothing says one is.

WE ALREADY KNEW, WHICH IS THE WORST PART. QC-049 has flagged exactly these
rows at ERROR severity since 2026-05: "UNCONFIRMED — flipped to WIN on a
send-signal with no MDOLX booking confirmation linked." The internal audit
reported them by request_id. The client-facing renderers never asked. One
fact, two readers, and the one talking to the customer held the wrong half —
the same shape as sent_ts/sent and LOSS/quoted, except this one left the
building.

FIXED with one predicate in one place: core.is_confirmed_win — WIN AND a
booking reference — defined identically to QC-049's test, with a guard that
fails if the two ever drift. Both client modules now select through it, and
"Confirmation to follow" is gone from both.

THE ASYMMETRY IS DELIBERATE and is itself pinned by a test: internal
reporting KEEPS is_win. A send-signal win is a real business signal and the
staff email and KPIs should count it. Only what leaves the building for
Hilmar needs the confirmation. Narrowing the internal number to match would
understate the desk.

A TEST WAS DEFENDING THE BUG. test_active_shipments_lists_recent_wins_sorted_
by_etd built a row with mdolx_ref=None and asserted "Booked shipments (3)"
and that "Confirmation to follow" appeared — it encoded an unsupportable
customer claim as expected behaviour. That is worse than having no test: it
defends the defect during exactly the review that would otherwise catch it.
Rewritten to assert (2) and the absence of the promise. Third test this week
found pinning a wrong decision rather than an invariant.

All four new guards verified by restoring the original selectors and watching
them fail.

STILL OPEN: Sentry QC-027 regression on 344eb1c — completeness on 329
reachable rows, ETA 93% (WARN), Carrier 87% (ERROR, <90%). Not the same
defect and not yet investigated. Filtering the client view to confirmed wins
will incidentally drop some carrier-less rows from what Lonny sees, but it
does nothing about the underlying 13%.

Suite 2617 passed, 0 failed. ruff clean.

### 2026-08-07 (13) — Aug 6 was quiet. The report was right.

Michael, closing the last open item: "you're right, sixth was quiet."

So the Aug 6 report showing zero activity was CORRECT. There was nothing to
show. The open question from entry (12) is closed, and the flag in
refresh_stage is updated rather than left to rot one commit after a whole PR
spent removing stale text.

WHAT THIS MEANS FOR THE DAY'S DIAGNOSIS, accounted for honestly:

  REAL, and fixed — classify() silently dropping OL quote-only senders.
  Reno's rate replies were discarded on arrival, which is what took
  mbd_rate_response to 0 over seven days against 299 historically. That was
  the actual intake defect.

  REAL, and fixed — the drop log recording twelve thrown-away messages as the
  single word "unclassified", with the sender printed only under --verbose,
  which the daily fire does not pass. That is why it ran for a week.

  REAL, but NOT a data gap — the pipeline reads /me (Michael's mailbox) and
  not MBD_OceanExportBookingShared, despite READ_MAILBOX documenting the
  latter. Measured, true, and it does not lose data, because Michael is on
  the ops distribution and the traffic reaches him anyway. I spent hours
  treating this as the root cause. It is a latent surprise for whoever next
  wonders which mailbox is being read, not a bug.

  NOT REAL — the $search cap (withdrawn on evidence), the missing Aug 6 mail
  (there was none), and the redirect rule (nothing to reroute).

THE PATTERN WORTH KEEPING: every one of the four things that turned out real
was found by MEASURING — the drop log, the imid check, the $filter control,
the /me identity print. Every one of the three that turned out false was
found by REASONING from a number I had not looked behind. Six theories were
floated today and the instrument contradicted all but the measured ones.

Suite 2610 passed, 0 failed. ruff clean.

### 2026-08-07 (12) — the runbook told you to RDP into a machine that is gone

Michael: "the drift from when it worked to now [is] absurd." He is right, and
the worst instance was in the worst possible place. RUNBOOK.md — the file you
open BECAUSE something is already broken — opened with:

    ## Daily fire (6:07 PM ET weekdays)
    **Trigger**: Cloud PC CPC-micha-E552L Windows Task Scheduler
    **If no emails by 6:30 PM ET**:
    1. RDP into Cloud PC via windows.cloud.microsoft

Every line false since the 2026-06 cutover. The fire is GitHub Actions at
8:07 AM ET Mon-Fri, reporting the prior business day. An operator following
that page would wait until 6:30 PM to worry about a report that failed at
8 AM — most of a business day late — and then try to log into a retired
machine.

WORSE, AND THE ONE THAT NEARLY BIT: the documented fix for an expired Graph
token was "Open Cloud PC RDP … python scripts/outlook_send.py auth". The
machine is gone AND the cache was originally seeded from it, so the recovery
path for this pipeline's single most critical credential was impossible to
perform. Nobody would have discovered that until the day it expired. It now
points at auth-refresh.yml, which needs a browser and nothing else.

FIXED: RUNBOOK head (schedule, trigger, expected outcome, what to check when
nothing arrives, how to fire manually), the MSAL recovery, README's intro,
daily-flow table and "run it remotely" section, and docs/MOVE-OFF-CLOUDPC.md —
a COMPLETED cutover plan still written as a numbered to-do list, whose step 2
("Seed state once — on the Cloud PC") is the instruction that sent me hunting
for that machine in the first place. It now declares itself done in a banner,
and points at the live constraint it still holds: the auth path is the no-IT
path because OL IT declined.

KEPT DELIBERATELY: every Cloud-PC NARRATIVE. Comments explaining why
state_store exists, why the wrapper is shaped as it is, why the scope list is
short — that is the reasoning record, and deleting it is how the next person
rebuilds a bad idea. Windows-era failure modes are relabelled [HISTORY] with a
one-line note on why they cannot occur, not deleted; the S4U silent-miss is
the best-documented failure in this project and still generalises.

GUARDED — tests/test_docs_not_stale.py. The distinction it enforces is STEPS,
not mentions: a numbered or bulleted list item telling you to RDP in, open
Task Scheduler or run the wrapper is a trap; a paragraph describing that
history is fine. It also pins the runbook's stated fire time against daily.yml's
actual cron, so the next schedule change fails a test instead of rotting.

MY FIRST VERSION OF THAT GUARD flagged the prose added in this very commit
explaining that the old step was removed. Correct docs, red test — the seventh
time this session that text in prose was indistinguishable from the thing it
described. Narrowed to list items only, then verified by planting a live step
under a non-history heading.

ALSO CORRECTED: the redirect-rule recommendation from entry (11). Michael:
"remember i'm already included in the group emails from ops, nothing has
changed." He is on the ops distribution, so there is nothing to reroute. That
leaves an open question — 373 messages in his mailbox on Aug 6, only 3
involving Lonny, all ours — and refresh_stage now records it as OPEN with the
instruction to run diag-day against a known-active day rather than theorise.
Six theories were floated today; the measurement contradicted every one that
was not measured first.

Suite 2610 passed, 0 failed. ruff clean.

### 2026-08-07 (11) — Mail.Read.Shared needs OL IT. It was already written down.

Michael: "these requires ol's it department to approve.. you have had these
details before.. why recreate the wheel here."

He is right, and it was in the repo in three places I did not read:

  docs/MOVE-OFF-CLOUDPC.md (2026-06-10)
    "No Hilmar app registration exists anywhere, Michael is not an admin in
     the ol-usa.com tenant, and OL IT declined to create the app."
    "The live design is the no-IT path."
  scripts/outlook_send.py, line 23
    "Scopes: Mail.Send Mail.Read Files.ReadWrite (delegated; no admin consent)."
  scripts/verify_fire_prereqs.py
    "The no-IT auth path (OL declined to register an app-only Entra app...)"

The scope set is that short ON PURPOSE. I added Mail.Read.Shared to it,
built a consent workflow, and dispatched a re-consent nobody in this project
can approve. CLAUDE.md's second rule is "Read before you write. Inventory
what exists before changing it." I inventoried the code and not the docs.

REVERTED: AUTH_SCOPES is gone, both device flows and auth_notify are back on
outlook_send.SCOPES, and the pending run is cancelled.

THE CONSTRAINT NOW LIVES WHERE THE TEMPTATION IS — next to
refresh_stage.SHARED_MAILBOX and outlook_send.SCOPES, not only in a doc
nobody opens. A guard fails on any of Mail.Read.Shared, Mail.ReadWrite.Shared,
Mail.Send.Shared, MailboxSettings.ReadWrite, Mail.Read.All, Mail.ReadWrite.All
appearing in ANY scope list in outlook_send — AST, so a second list cannot
smuggle one past it. Verified by planting exactly that.

THE ROUTE THAT WORKS WITHOUT IT — move the mail, do not widen the token. An
inbox rule on MBD_OceanExportBookingShared that REDIRECTS mail from/to
lupfold@hilmaringredients.com to michael.deitchman@ol-usa.com. Michael can set
it himself; no admin, no scope, no code change.

REDIRECT, NOT FORWARD, and this is not a preference. A redirect preserves the
original From header and internetMessageId, so classify() still returns
lonny_outbound and the two-key dedup still recognises the message. A forward
rewrites the sender to the shared mailbox, which classify() reads as
mbd_inbound — every RFQ would be filed as a booking confirmation. That
distinction is now in the code comment, because it is the difference between
this fix working and it silently corrupting the data.

THE WARNING TEXT WAS ALSO WRONG. read_targets said "re-run the auth workflow
to consent", which is impossible. It now names the admin-consent constraint
and the redirect route. A recurring warning with unactionable advice is just
noise that trains people to ignore warnings.

KEPT, because the detour found a real gap: auth_notify.py + the re-seed
workflow, now on the EXISTING scopes. MOVE-OFF-CLOUDPC.md says the token cache
was seeded once from the Cloud PC, and the Cloud PC is decommissioned — if the
~90d refresh token ever lapses there was no way back. Now there is, and it
needs no IT. It also verifies the granted scopes cover OS.SCOPES and errors if
they do not, because a green run with a token that cannot send would surface
as nine people not getting a report.

Suite 2603 passed, 0 failed. ruff clean.

### 2026-08-07 (10) — the code comes to him; the sign-in still cannot

Michael, on being told to fetch a device code out of an Actions log: "what am
i doing from my phone and where.. you do it.. use a chrome extension."

WHAT I CANNOT DO, and no browser automation changes it: the sign-in. Device-
code flow exists so that only the holder of the credentials can complete it.
Driving it in a headless Chrome would mean handling his OL password, and MFA
would stop it regardless. Checked two escape hatches before saying so rather
than after:
  - the Microsoft 365 connector authenticates as michael.deitchman@IDEALX.US,
    a different tenant from @ol-usa.com — so it cannot read that mailbox or
    set a forwarding rule on it either.
  - Microsoft returns no `verification_uri_complete` for this client. Verified
    against the live endpoint: the flow dict is
    ['_correlation_id','device_code','expires_at','expires_in','interval',
    'message','user_code','verification_uri'] — no pre-filled variant. The
    code has to be typed somewhere.

WHAT I CAN DO is delete the scavenger hunt. New scripts/auth_notify.py:

    initiate flow  →  EMAIL THE CODE  →  block until approved  →  save cache

The email goes out BEFORE the blocking call, so the 15-minute clock starts
with the code already in his inbox. Order is the feature and is tested by
AST position, not by hope: emailing after the wait would deliver the code once
it was already expired.

Four failure modes handled because each one looked like success:
  - cannot acquire a token to SEND with → refuse BEFORE starting the flow,
    rather than stranding him with a code nothing will deliver
  - send fails → warn, keep going; the code is printed and still valid
  - consent completes WITHOUT Mail.Read.Shared → ::error:: and exit 1. Green
    run, unreadable mailbox, is the worst outcome available here
  - Outlook rendering → no var(), no flex/grid, no <style>, and every ground
    doubled background-color + background

AND THE GUARD BIT ME AGAIN. My first version of the Outlook test sliced the
source for "var(" and matched _body's own DOCSTRING, which explains the rule
using the words var()/flex/grid. Correct code, red test. Sixth time this
session that an identifier in prose was indistinguishable from one in code,
and the fix is the same as the other five: AST. It now reads the strings the
function EMITS, docstring excluded.

auth-refresh.yml takes a `notify` address and runs auth_notify instead of
auth-bg. Michael's part is now: open the email, tap, sign in. About 30
seconds, on whatever device the mail is already on.

Suite 2603 passed, 0 failed. ruff clean.

### 2026-08-07 (9) — ROOT CAUSE: we were reading the wrong mailbox

diag_day run 6, against production, one line:

    reading: https://graph.microsoft.com/v1.0/me
      /me resolves to: Michael.Deitchman@ol-usa.com
      >>> NOT the intended read target (MBD_OceanExportBookingShared@ol-usa.com)

READ_MAILBOX has documented the shared booking mailbox as the thread endpoint
since it was written — "Lonny's RFQs are addressed to it and OL replies from
it". But _mailbox_base only becomes that when GRAPH_APP_* is configured, and
OL IT declined to register the app-only Entra app, so those three secrets are
empty and the delegated path reads /me instead.

It never errored. It read a real mailbox with real mail in it, just not the
one the RFQs go to. That is the week: Aug 6 at zero, Jul 27-28 returning
nothing, mbd_rate_response at 0 for seven days against 299 historically, and
Michael counting 12 requests where the tracker saw a handful. We only see a
thread when he is personally on it.

MICHAEL, asked to choose: "1 and 3" — read the shared mailbox AND keep his own
as a second source, merged. Both, because mail that reaches only him (an OL
colleague replying direct, a forward) is real data we already have.

BUILT:
  - refresh_stage reads N mailboxes. read_targets() returns
    [(label, base, token)], shared FIRST so a thread present in both dedupes
    to the authoritative copy.
  - search_messages / get_message_body / fetch_pdf_attachments take a `base`.
    A Graph message id is MAILBOX-SCOPED: fetching a shared-mailbox message
    from /me is a 404, not a fallback to the right one. Each item carries
    `_src` and its body and PDF are fetched from that same mailbox.
  - one unreadable mailbox does not cost us the other — the query is wrapped,
    warns, and continues.
  - `_src` cannot reach the stage file (build_stage_record writes an explicit
    dict), asserted rather than assumed.

THE SCOPE, and the trap avoided. Delegated reads of another mailbox need
Mail.Read.Shared, which the cached token never consented to. Adding it to
outlook_send.SCOPES would have been the obvious move and would have BROKEN
THE FIRE: SCOPES is what every silent refresh asks for, and requesting an
unconsented scope there fails the refresh and stops the email. So SCOPES stays
narrow, a new AUTH_SCOPES goes wide, and refresh_stage.shared_token_silent
asks for the wide set and returns None — not an exception — when the cache
cannot supply it. Until the re-consent, the fire runs on /me exactly as it
does today, with a ::warning:: that names what it is missing rather than the
silence we had for a week.

THE CLOUD PC IS NOT NEEDED, and I said otherwise an hour ago. Michael:
"i don't want the cloud pc remember, we purposely turned it off!" Correct, and
device-code auth never needed that machine — it needs a BROWSER. New
auth-refresh.yml prints DEVICE_CODE=… in the Actions log; you enter it at
microsoft.com/devicelogin from a phone. Confirm-gated on typing REAUTH,
pushes ONLY the token cache (state_store.push(only=…), so it cannot revert a
day's ingest the way the weekly job once did), pushes only on success, and
ends by printing the mailboxes refresh_stage would then read — proof the scope
landed rather than an assumption that it did.

NOT DISPATCHED. Re-consenting a live credential is the operator's action.

Tests: 12 new in test_multi_mailbox_read.py. The three that matter were
verified by planting the regression: widening SCOPES itself, dropping the /me
fallback, and losing the shared-first ordering.

Suite 2597 passed, 0 failed. ruff clean.

### 2026-08-07 (8) — no Lonny mail on Aug 6, and a question about WHICH mailbox

Run 5, with the evidence printed before the verdict, and the verdict now
trustworthy because the rows behind it are visible:

    DROPPED  in $search  Michael.Deitchman@ol-usa.com
                         2026-08-06T14:36:41Z  OL-USA — Daily Shipment Update…
    DROPPED  in $search  Michael.Deitchman@ol-usa.com
                         2026-08-06T14:36:41Z  OL-USA — Daily Shipment Update…
    DROPPED  in $search  michael.deitchman@ol-usa.com
                         2026-08-06T14:36:39Z  OL-USA — Daily Shipment Update…
    >>> $search found every Lonny message $filter did — the query is fine.

All three "Lonny-touching" messages are ONE message in three folder copies:
our own daily report TO him. $search returned it. So $search is not the gap
on Aug 6, and this verdict is believable where run 4's was not, because the
three rows are on screen.

ESTABLISHED, with proof rather than inference: of 373 messages in the mailbox
on Aug 6 ET, exactly three involve lupfold@hilmaringredients.com and all three
are our own outbound. NOTHING from Lonny arrived. The intake query is fine;
the mail is not in the mailbox being read.

WHICH RAISES THE QUESTION THE TRACER NEVER ASKED: which mailbox IS it reading?

  refresh_stage.READ_MAILBOX defaults to MBD_OceanExportBookingShared@ol-usa.com
  — documented in its own comment as "the thread endpoint: Lonny's RFQs are
  addressed to it and OL replies from it."

  But get_token() only points _mailbox_base at READ_MAILBOX when GRAPH_APP_*
  is configured. On this tenant those three secrets are EMPTY (visible in
  every run log), so the delegated path leaves _mailbox_base at /me — whoever
  seeded the token cache. The evidence is consistent with that being Michael's
  own mailbox: the only Lonny-adjacent mail on Aug 6 is his own Sent Items
  copies.

  If that holds, Lonny's RFQs to the shared mailbox are invisible to this
  pipeline unless Michael is personally on them — permanently, not
  intermittently. It would also explain Mon Jul 27 and Tue Jul 28 returning
  zero, and the general thinness of recent days against mid-July.

NOT ASSERTED — INSTRUMENTED. The tracer now prints _mailbox_base, resolves
/me to a real address, and says loudly when that address is not READ_MAILBOX.
Proving a mailbox empty is worthless if you cannot say which mailbox, and I
spent five runs not saying it. The next run answers it in one line.

Suite 2585 passed, 0 failed. ruff clean.

### 2026-08-07 (7) — the control reported all-clear on three missing messages

Run 4, the first with the $filter control:

    373 message(s) in the mailbox that day ($search found 1)
    of those, 3 touch lupfold@hilmaringredients.com
    >>> $search found every Lonny message $filter did — the query is fine.

Those three lines contradict each other. $search returned ONE message for
Aug 6 — our own daily report — and $filter found THREE touching Lonny. The
verdict is impossible and it is mine.

THE MECHANISM. The comparison keyed on internetMessageId. Every row on both
sides had it as None, so search_keys was {None}, and `None not in {None}` is
False — three missing messages matched nothing to nothing and the control
reported all-clear on the exact question it exists to answer. "A missing key
is not an error" for the third time this session; here the missing key was an
alibi.

FIXED: _key_of returns imid, else Graph id, else None — and None is DISCARDED
from the comparison set, so an unidentifiable message counts as MISSED rather
than as universally matched. Verified by restoring the old one-line version
and watching the test go red.

AND THE DEEPER FIX — evidence before verdict, unconditionally. Run 4 printed
a conclusion and never printed the rows it came from, so the log looked clean
while being wrong. Every Lonny-touching message the control finds is now
listed with sender, time, subject, its classify() bucket and whether $search
saw it, BEFORE any verdict. A wrong verdict over visible rows is recoverable;
a wrong verdict over nothing is not. Guarded by a test that asserts the
listing precedes the verdict and is not nested under the "something was
missed" branch.

WHAT THE NUMBERS ALREADY SAY, pending the re-run: 373 messages in the mailbox
on Aug 6, 3 of them involving Lonny, and $search returned 1 for that day. So
Lonny's Aug 6 mail is very likely real and very likely missing from intake —
which is where the evidence pointed before my broken comparison talked me out
of it. Stated as the strong reading it is, not as a result: the re-run prints
the three messages and settles it.

Suite 2584 passed, 0 failed. ruff clean.

### 2026-08-07 (6) — the cap theory is dead; a $filter control settles it

Run 3, with the corrected imid lookup and the two new views:

    overlap lonny-flow ∩ hilmar-bookings: 15  (lonny-only 118, bookings-only 197)

THE CAP THEORY IS DEAD, and this time on evidence rather than on a second
guess. Both queries return 275 raw results, which is what made me suspect a
cap twice — but they share only 15 messages. They are genuinely different
result sets; the 275s are a coincidence of pagination, not one relevance set
returned twice. (The raw 275 dedupes to 133 and 212 unique imids — Graph
returns the same message once per folder copy.)

THE HISTOGRAM, which is the first real look at intake completeness:

    2026-08-07  3     2026-07-31  9     2026-07-22 21
    2026-08-06  1 <-  2026-07-30 14     2026-07-21  8
    2026-08-05  6     2026-07-29  3     2026-07-20 17
    2026-08-04  6     2026-07-24  4     2026-07-17 15
    2026-08-03  6     2026-07-23  6     2026-07-16 24

Aug 6 has ONE message where its neighbours have six. The window is populated,
so this is not a dead query — Aug 6 specifically is near-empty. Also missing
entirely: Mon Jul 27 and Tue Jul 28, two business days with zero messages in
the set. Weekends are absent as expected (Sat Jul 18 has 1, so weekend mail
does come through when it exists).

The single Aug 6 message is our own daily report from
michael.deitchman@ol-usa.com, correctly dropped. Nothing from Lonny.

THE CONTROL, added rather than concluded: _filter_day queries the same ET day
with $filter on receivedDateTime and no $search at all. $search is
relevance-ranked and its completeness is the open question; $filter is an
ordered range scan. Run both over one day and the difference IS the intake
gap, measured. Three outcomes, each printed as a verdict:
  - $filter finds Lonny mail $search missed  → the query is the gap
  - $filter finds the same set               → the query is fine
  - no message that day involves Lonny at all → nothing arrived to drop

The window is ET midnight to ET midnight converted to UTC, not a UTC day — a
UTC day shifts both edges four hours and moves evening mail across the
boundary, the exact bug core.et_date_of exists to prevent. Tested on the
emitted $filter string for both an EDT and an EST date, so a hardcoded
4-hour offset fails in January instead of in silence. Pagination tested too:
one page is 50, and a control that stopped there would manufacture a
false "nothing missing".

Suite 2582 passed, 0 failed. ruff clean.

### 2026-08-07 (5) — the tracer ran, and the first thing it caught was itself

Run 2 of diag-day succeeded and printed, in its first three lines:

    stage_emails: 1273 records (0 with an imid)
    bodies:       1259 records (0 with an imid)

Zero of 1273. My tracer indexed the stage records on "internetMessageId" —
the GRAPH field name — while build_stage_record writes `imid`. So the STAGED
and BODY columns read NO for every message in existence, which is
indistinguishable from "nothing was ever staged" and would have pointed the
next investigation at the wrong link entirely.

This is the session's dominant bug shape for the fifth time: one fact, two
spellings, and the reader holding the other one. It is also exactly what the
file's own docstring warns against — a private copy of the pipeline's
knowledge — one level below where I was looking. I guarded classify() and the
mailbox addresses and the ET clock, and then hand-rolled a field name three
lines above them.

FIXED by deleting the private copies: RS.load_existing_stage_keys,
RS.load_existing_body_imids and QC._load_bodies_index now do the reading. The
local _load_jsonl helper is gone.

GUARDED with a BINDING test, not a source grep: build a record with the real
build_stage_record, read it back with the real load_existing_stage_keys
through a redirected STAGE_PATH, assert the imid survives the round trip.
Verified by renaming the field in build_stage_record and watching it go red —
`assert "imid" in source` would have passed through that rename happily.

TWO NEW VIEWS, because the run raised questions it could not answer:
  - a per-day histogram of the Graph result set. "1 message on Aug 6" means
    nothing without the neighbouring days; if every recent weekday is thin and
    the volume sits in May, the set is relevance-ranked rather than complete.
  - the overlap between the two queries. Both returned exactly 275 for a union
    of 330, so they share 220 — implausibly high for "mail from/to Lonny"
    versus "from the booking mailbox with HILMAR in the subject". Identical
    counts from unrelated predicates is the signature of a cap or of $search
    not honouring the query, and the overlap distinguishes them. Printing it
    rather than concluding from it, having already concluded wrongly once.

WHAT THE RUN DID ESTABLISH: Graph returned exactly ONE message dated Aug 6 —
our own daily report, from michael.deitchman@ol-usa.com, correctly dropped.
Nothing from Lonny. And tracking-data has 0 rows dated Aug 6 with 0 undated
rows anywhere, so the dating heal is not hiding anything. The loss is at or
before intake, not downstream.

Suite 2579 passed, 0 failed. ruff clean.

### 2026-08-07 (4) — "he did": a tracer for the five links of the intake chain

Michael, asked directly whether Lonny genuinely sent nothing on Wednesday
Aug 6, which is what the report claimed: "he did." So the Aug 6 zero is a
second, separate gap, and Reno does not explain it — she quotes, she is not
Lonny.

WHY A TOOL AND NOT A GUESS. The chain from an email to a report row has five
links: Graph returns it, classify() buckets it, it lands in stage_emails,
fetch_bodies gives it a send time, it becomes a dated tracking row. Every
investigation this week inspected ONE link and inferred the rest, and
inferring was wrong twice in five days — the $search cap that was not
(withdrawn in the entry below) and the heal I called fixed while it read
field names fetch_bodies does not write. Guessing a sixth time is not
cheaper than measuring once.

NEW scripts/diag_day.py + .github/workflows/diag-day.yml (manual dispatch,
read-only). For one ET day it prints, per message: the classify() bucket,
whether the imid is in stage, whether it has a body — then the tracking rows
dated that day, then every undated row with QC-077's own reason label. The
first line that says NO is the broken link. It also prints the oldest and
newest receivedDateTime in the result set, which is precisely the check I
skipped when I blamed $search.

BORROWED, NOT REIMPLEMENTED: the mailbox addresses, the two KQL queries,
classify(), core.et_date_of and qc_selfheal._undated_reason all come from the
pipeline. A tracer with its own copy of any of them would clear a day the
pipeline still drops, which is worse than having no tracer. Tests pin that.

READ-ONLY, PINNED BY AST: no state_store.push/backup/restore, no import of
outlook_send / fetch_bodies / ingest / merge_ingest, no write_text, no open()
for writing. AST rather than grep because "state_store.push" appears in the
prose of both the script and its test file, and an identifier in prose is
indistinguishable from an identifier in code to a regex — the fourth time
that has bitten this repo. All five guards verified by planting the
violation and watching them fail, then restoring.

THE FIRST RUN DIED, TWICE OVER, AND BOTH WERE MINE:

  1. It installed requirements.txt, which deliberately does NOT carry
     azure-storage-blob (see its header) — every workflow that touches the
     state store names it explicitly, and I did not. `No module named
     'azure'` before a single useful line. Now installs the same set
     daily.yml's fire job does.
  2. It pulled into a temp dir. GRAPH_APP_TENANT_ID / CLIENT_ID /
     CLIENT_SECRET are all EMPTY in this repo — this tenant has no app-only
     Entra app, OL IT declined to register one — so Graph auth falls back to
     the delegated MSAL cache at secrets/token-cache.bin, which outlook_send
     resolves from module constants. A temp dir is invisible to it. Pulls
     into the repo root now, like the fire does. Monkeypatching those
     constants was the alternative and would have given the tracer a private
     copy of the pipeline's auth — the one thing it must not have.

So the read-only claim is narrowed to what is true: it never writes the BLOB,
never sends, never fetches a body, never edits stage or tracking data. It DOES
overwrite the working tree — the pulled state, plus MSAL rewriting the token
cache on refresh. Documented at the top of the file, because on the Cloud PC
that overwrite is real.

THE SECOND FIX IS A GENERAL GUARD, not a patch to one file: any workflow
running a script that imports state_store must install azure-storage-blob.
Checked across all workflows, so the next one to be added cannot repeat this.
Both new guards verified by planting the regression.

NOT YET AN ANSWER. This ships the instrument, not the diagnosis. The Aug 6
trace runs next.

Suite 2577 passed, 0 failed. ruff clean.

### 2026-08-07 (3) — Reno's quotes get staged; the intake rewrite is withdrawn

Michael, answering the product question the previous entry left open: "reno
only quotes hilmar so she doesn't book." And: "why rewrite intake ?"

FIXED — quote-only OL senders. New OL_QUOTE_ONLY_SENDERS in refresh_stage,
holding reno.gurusinghe@ol-usa.com. Every message from a name on that list
classifies as mbd_rate_response, unconditionally, before the drop. Two reasons
it is not subject-matched:

  1. No booking role means no mbd_inbound case to fall through to. A non-quote
     from her is not a booking confirmation.
  2. Her subjects do not have the shared mailbox's "Re: <origin> to <dest>"
     shape. The message that surfaced this was "Re: Rates to a few
     destinations for a study" — RATE_RESPONSE_SUBJECT_RX does not match it
     and never will, because "Rates" is not a known origin. Verified, not
     assumed. Subject-gating her would drop the quote a second time while
     looking like it was handled.

An ALLOWLIST OF ADDRESSES, deliberately, not "any @ol-usa.com". The domain
would swallow our own outbound — michael.deitchman@ol-usa.com is on it, and
nine of the twelve drops were the tracker's own client emails coming back
through `to:lupfold`. A test pins that the list stays addresses.

I WAS WRONG ABOUT THE INTAKE, and the previous entry says so in ink: "pending
is understated because the intake is missing mail... the $search rewrite is
not started." Withdrawn. I built that from two Graph queries both returning
275 results and concluded $search was returning a relevance-capped set that
excluded recent mail. My own diagnostic refutes it — one of the dropped
messages is stamped 2026-08-07T13:37:44Z, i.e. today. Graph is returning
current mail; the loss was entirely at classify(). I had the timestamps in
front of me and reasoned from the two result counts instead of reading them.
No intake rewrite. Nothing to do there.

AND THE 12 RECONCILE. Michael: "there were a minimum of 12 requests this week
so far." QC-009's own 7d counts say lonny_outbound 12. The data was staged all
along; W32 showed 10-11 because the ISO week boundary splits those twelve
across W31 and W32. Not a gap — a window.

STILL UNANSWERED, and only the operator can: whether Aug 6 was genuinely a
zero-activity day for Lonny, or still masks mail we are not seeing.

Suite 2563 passed, 0 failed. ruff clean.

### 2026-08-07 (2) — the navy bar back, and a day-by-day week

Michael, twice: 2026-08-06 "go back to the older format" and 2026-08-07 "still
using the new formatting which i told you to go back to". Plus: "you aren't
doing the current week in review as well for each day... there should be a
weekly tally as well for current week."

THE TABLE HEADER. Reverted to the solid navy bar with white text (#1e3a5f)
that shipped before the 08-04 restyle, kept as the literal rather than
remapped to a token — the point is the exact look he approved, and pointing it
at DOC_INK or HILMAR_NAVY would silently change the colour again.

On the FIRST ask I restored the section rule and left the table headers flat.
That is why there was a second ask. The restyle's reasoning was sound in the
abstract — three saturated bars did shout over the data — but it was not my
call to keep making after the operator had reversed it.

A TEST OF MINE ASSERTED THE OPPOSITE. test_the_old_header_bars_are_gone_from_
the_email pinned "#1e3a5f must not appear", so it would have gone red on his
own instruction. Same defect as the cron test that pinned "reports are paused"
and the client-weekly tests that pinned "enabled is False": a test encoding a
DECISION the operator is entitled to reverse. Replaced with the part that IS
an invariant — every table-header ground must be the navy bar or a document
status hue, never a fourth colour nobody chose. The dark-red losing-lanes bar
stays retired; nobody asked for that one back.

THIS WEEK, DAY BY DAY. New section above Week-over-Week: one row per weekday
of the current week, with a WEEK TO DATE total. The rollup below it shows the
current week as ONE line, which answers "how is the trend" and not "what
happened Tuesday". Days the week has not reached are omitted — a zero for
Friday on a Wednesday reads as a bad week rather than an unfinished one.
Buckets through core.is_win/is_pending/is_quoted_and_lost/is_not_quoted, so a
STRICT-form row counts the same as a LEGACY one; the rollup directly below
still uses raw status comparisons for its loss split and would drop them.

gen_manual's section catalog required the new block to be documented before
the suite would go green — the guard worked.

STILL BLOCKED ON THE OPERATOR: pending is understated because the intake is
missing mail. See the entry above; the $search rewrite is not started.

Suite 2558 passed, 0 failed. ruff clean.

## 2026-08-07 — 12 emails thrown away, logged as one word

Michael: "data is missing and completely incomplete... there were a minimum of
12 requests this week so far." The Aug 7 fire's log, in full:

  refresh_stage: total unique results across queries: 330
  refresh_stage: NEW staged records: 0
  refresh_stage: skipped 281 pre-cutoff, 0 excluded, 12 unclassified, 37 already-staged
  Nothing new to stage.

Graph returned the mail. The classifier discarded twelve messages and the only
trace was the word "unclassified". Nothing has been staged since Aug 5, which
is why the header's window ends Aug 5 while the report claims to cover Aug 6,
and why every "today" section is zero.

WHAT THE FIRE ALREADY KNEW AND DID NOT SAY LOUDLY ENOUGH:
  QC-008  latest staged record is 41.9h old on a business day
  QC-009  classifier may be dropping a sender: ['mbd_rate_response']
          7d counts: lonny_outbound 12, lonny_reply 5, mbd_inbound 4,
          mbd_rate_response 0
mbd_rate_response is ZERO over seven days against 299 historically. OL's rate
replies stopped being classifiable a week ago — that is the empty OL-USA
RESPONSES section and the zero pendings, both of which follow mechanically.
Both checks are WARN, so the pipeline exited 0, the integrity gate said "fresh
report shipped", and an empty report went to nine people.

MECHANISM. classify() returns None for any sender that is not Lonny or the
shared booking mailbox, and the 'lonny-flow' query is `from:lonny OR to:lonny`
— so an OL reply sent from an individual's mailbox comes back from Graph and
is dropped on arrival. That rule is defensible. Dropping SILENTLY is not.

FIXED — the log now names them. Dropped senders are counted and printed with
up to eight whole examples (sender | received | subject), WITHOUT --verbose,
because the daily fire does not pass --verbose and diagnosing this otherwise
needs a hand re-run with a different flag. That is precisely why it ran a week.
Staging zero new records while dropping any also emits a ::error:: annotation
naming the senders, so it lands in the run summary instead of 400 lines up in
stdout. Deliberately an annotation, not an exit code: refresh_stage is
best-effort in the daily fire and failing the step would suppress the staff
email that still carries the cumulative KPIs.

NOT YET FIXED, AND IT NEEDS THE OPERATOR: whether those senders SHOULD be
staged is a product decision, not mine. The next fire's log will name them.

The QC governance ratchet did its job on the way through — the new test made
QC-009 "tested", so it came out of KNOWN_UNTESTED and the ceiling dropped 18
to 17.

Suite 2554 passed, 0 failed. ruff clean.

### 2026-08-06 (3) — "bad format": nothing moved, the landmarks went out

Michael: "go back to the older format that shows pending hilmar pending ol
then what changed etc."

THE FORMAT DID NOT CHANGE. Section ORDER, heading TEXT, heading LEVEL and
empty-state rendering in the staff email are byte-for-byte unchanged across
this repo's entire history. Nothing was reordered, nothing demoted, nothing
that used to be a table now collapses to a one-liner. Verified across every
commit touching gen_email.py, not assumed — and NOT restyled on a guess,
which was the tempting move.

WHAT CHANGED WAS PROMINENCE, and it was mine. The 08-04/08-05 restyle
replaced the solid navy table-header bars and the saturated KPI tiles with
near-white, and set the section rule to a 1px hairline in DOC_LINE — the
colour of the card ground it sits on. Killing the shouting bars was right;
replacing them with nothing was not. A reader scanning for "where is my
pending list" needs an anchor, and a hairline in the background colour is an
absent one rather than a quiet one.

Then Defect 2 emptied three of the five sections in that same block and put an
amber self-apology banner between them. Flat + empty + apologising reads as
"the format broke."

FIXED, without reverting the palette he approved:
  - section rule 1px DOC_LINE → 2px DOC_INK, the same rule the table heads
    already use, so a section head and a table head are one landmark at two
    scales
  - TWO headings drew their 2px rule in DOC_WARN_BG — the pale TINT — under
    DOC_WARN text. A rule in the background tint of its own colour is a rule
    you cannot see. I found one by eye and the new scanner found the second,
    which is the entire argument for scanners over eyes.
  - guards pin WEIGHT, not hexes: the section rule must be 2px ink and must
    not use DOC_LINE, and no 2px rule may be drawn in a *_BG token. The
    palette can keep moving; the landmark cannot vanish again.

ALSO: gen_email_new.py carried the identical dead-fallback date_range bug.
Dormant (not in run_pipeline) but it writes the SAME reports/email-body.html,
so anyone who runs it ships the repr. Routed through core.format_date_range.

SEQUENCING, deliberately: the restyle would not have put one missing row
back. The rows were missing, not hidden by CSS — the same template renders
every row when the data is there. Data first, then landmarks.

Suite 2520 passed, 0 failed. ruff clean.

### 2026-08-06 (2) — the 08-05 heal never dated a single row

Michael: "bad data bad format.. also missing a ton of data". The undated-quote
count went 29 (07-30) → 41 (08-05) → 43 (08-06) — THROUGH the heal shipped on
08-05 specifically to shrink it, which I reported as fixed.

IT COULD NEVER HAVE WORKED.
  fetch_bodies.upsert_body writes    "sent_ts" / "received_ts"
  qc_selfheal._body_send_time read   "sent" / "sentDateTime" / "received"
  patch_carriers read the same wrong three

stage_emails.txt genuinely uses sent/received; stage_emails_bodies.txt — the
file BOTH healers actually open — uses sent_ts/received_ts. Two file schemas
for one concept, and both healers reached for the other file's spelling. Every
lookup returned None, silently, because a missing key is not an error.

That makes the QC-077 set MONOTONIC: rate recovery keeps ADDING rows that carry
a rate with no response_timestamp, and nothing could ever remove one. 29 → 41 →
43 is exactly that shape. It also IS the "missing a ton of data" — an undated
quote is invisible to OL-USA RESPONSES on every day forever, because that
section buckets on response_timestamp. Defects 2 and 3 were one defect.

refresh_stage.py:254 already read BOTH spellings. The split was known to
someone and never shared, which is the whole lesson: one reader now
(core.body_send_time), both healers through it, send preferred over received.

THE TEST BINDS READER TO WRITER, not to a second copy of the list. It builds a
record through fetch_bodies and asserts core.body_send_time finds its
timestamp, plus a scan asserting every timestamp key fetch_bodies writes is a
key the reader knows. A test that lists the spellings itself proves only that
someone wrote the same list twice — which is precisely how this shipped.

I told Michael this was fixed on 08-05. It was not, and the number he was
already calling unacceptable grew for two more days while I said otherwise.

ALSO FIXED — stale timer prose. PENDING_HILMAR_LOSS_HOURS and
PENDING_OL_LOSS_HOURS were set 48→24 by Michael himself in 0c73c4b
(2026-07-26, "supersedes 2026-07-14"). Four places in core.py still said 48.
Chasing "PENDING OL (0)" I nearly "fixed" the CONSTANT back to 48 — silently
reverting an operator decision inside the timer that decides whether live
business gets called lost. The commit message is what stopped me. Comments
corrected in both trees; test_timer_docs_match_constants now fails on any hour
literal in timer prose that no timer constant holds, scanning comments and
docstrings only (via tokenize + AST) because `weekday() == 4` is not an hour.

Suite 2518 passed, 0 failed. ruff clean.

## 2026-08-06 — a Python dict repr in the header nine people read

Michael, on the production email: "bad data bad format... also missing a ton of
data". The header read, verbatim:

  Reporting Wednesday August 5, 2026 — the prior business day ·
  {'start': '2026-04-02', 'end': '2026-08-05'} | Updated: August 6, 2026 ...

ONE FACT, TWO STORAGES — AGAIN, AND THE FIXTURE HELD THE OTHER ONE.
  scripts/ingest.py:1823    writes date_range as a DICT {"start","end"}
  scripts/merge_ingest.py   writes it as a STRING
  tests/fixtures/golden_day writes it as a STRING
schema.json permits both (oneOf [string, object]), so no writer is wrong. The
READERS were. Three of them did `data.get("date_range") or <fallback>` and
interpolated the result — and a DICT IS TRUTHY, so the fallback branch was
unreachable in production while every golden test rendered the string form and
passed. This is structurally identical to the status-vocabulary bug fixed
yesterday: the fixture exercises one shape, production carries the other, and
the renderer only handles one. Second instance in two days.

AFFECTED, and gen_pdf is the one that stings:
  gen_email.py               staff header — what Michael saw
  gen_pdf.py                 CLIENT PDF cover — Lonny would have seen it
  gen_carrier_scorecard_pdf  carrier negotiation pack
All three now read core.format_date_range, which accepts either shape and
renders "Apr 2, 2026 – Aug 5, 2026". Unparseable dates pass through rather
than being dropped: a date we cannot read is still information.

A SHADOWED IMPORT, FOUND BY THE FIX. gen_carrier_scorecard_pdf had a redundant
function-local `import core` two hundred lines below the module-level one,
which made `core` local to all of build_scorecard — so the new call earlier in
the same function raised UnboundLocalError. The local import is gone. It had
been latent since the day it was written; nothing had needed `core` earlier in
that function before.

GUARD: every artifact a human receives is rendered FROM THE PRODUCTION SHAPE
and scanned for Python reprs ({'k': ', dict_keys(, <obj at 0x). Rendering the
fixture is precisely what missed this, so the test overrides date_range to the
dict form rather than trusting the fixture. Detector proven against the exact
header that shipped, and proven not to fire on CSS braces.

Suite 2500 passed, 0 failed. ruff clean.

STILL OPEN — under investigation, not fixed by this commit:
  - the undated-quote count is GROWING: 29 (07-30) → 41 (08-05) → 43 (08-06),
    after the 08-05 heal that was supposed to shrink it
  - OL-USA RESPONSES (0), STATUS CHANGES (0), PENDING OL (0) on a full
    business day — "missing a ton of data"
  - Michael wants the older format back: "pending hilmar pending ol then what
    changed"

## 2026-08-05 (5) — client weekly LIVE (Michael: "flip client weekly")

FLIPPING THE FLAG ALONE WOULD HAVE DONE NOTHING, which is worth recording
because it was by design and the design nearly hid the work. gen_client_weekly
shipped with client_weekly.enabled=false AND no send path anywhere — a
deliberate two-way gate, with a test asserting both. So "flip it on" meant
building the other half:

  - weekly.yml had NO build step for the client rollup. Only run_pipeline
    built it, and run_pipeline runs in daily.yml. Enabling a send without
    adding the build would have shipped whatever stale file the runner
    happened to have, or nothing at all. Added, taking the SAME explicit
    start/end as the staff summary so a catch-up dispatch produces a client
    rollup covering the same window rather than a different one.
  - the send, mirroring the daily's client block on purpose: reaching Lonny
    requires client_weekly.enabled=true AND send_to=full. Anything else is a
    labeled sample to sample_to. Own flag namespace (client-weekly-sent) so it
    can never consume or be consumed by the staff weekly's guard — the exact
    collision that blocked 2026-07-30.
  - --only weekly-sent,client-weekly-sent. COMMA-joined: --only takes one
    string, and a second bare word lands as a positional and is silently
    dropped, so the flag would never persist and every Monday would look like
    the first. Caught reading the arg parser, not in production.
  - config.json client_weekly.enabled=true, recipients unchanged.

QC-065 NOW COVERS BOTH CLIENT ARTIFACTS, VIA ONE DEFINITION. The check was
inline and client_report-only. Extracted to qc065_check_client_block(cfg, key,
body_path) and called for both. Writing a QC-078 for the weekly would have
been a second definition of "safe client artifact" — the precise mistake this
repo spent today undoing (five spellings of one rate predicate, two
vocabularies for one status). Recipients are validated even while DISABLED,
because a wrong address is harmless right up until the flag flips.

TWO TESTS WENT RED, CORRECTLY, AND WERE THE WRONG SHAPE
  test_send_is_disabled_in_shipped_config and
  test_no_pipeline_step_sends_the_client_weekly asserted enabled is False and
  that no send path existed. Both true and useful for exactly one day. They
  pinned an OPERATIONAL STATE the operator is entitled to change — the same
  defect as the cron test that went red the morning Michael said resume.
  Rewritten as invariants that hold in BOTH states: the send requires two
  conditions, the disabled path reaches only the labeled sample, the rollup is
  built before it is sent, it owns its own flag, and recipients equal the
  approved pair whenever enabled.

WHAT I RECOMMENDED AND HE OVERRODE: I asked that a real rendered week be read
before flipping. Michael flipped without one; that is his call and it is
recorded here rather than argued. The first live send is Monday ~5 AM ET. A
send_to=test dispatch produces a labeled sample from real data at any time
before then, which is the review I was asking for, available without delaying
anything.

Suite 2494 passed, 0 failed. ruff clean. weekly.yml parses.

## 2026-08-05 (4) — "do it all asap": the send, the palette, Lonny's weekly

1. THE JUL 27–AUG 4 CATCH-UP WENT, AND THE LOG SAYS SO
   Dispatched weekly.yml on main with start/end/force_send and send_to=full.
   Verified from the job log rather than from a green check — the send step
   ran in ONE SECOND, which is not what a real Graph send looks like, and a
   workflow can succeed while the mailbox guard silently no-ops:
     SUBJECT: Hilmar — Catch-Up Executive Summary (Jul 27–Aug 4, 2026)
     → TO (9): the full staff distribution
     → BODY: 17,389 bytes
     ✅ Sent. request-id=710dd7aa-289d-4e50-9eb6-04b717be8318
   force_send was required and expected: the 21:04 preview had already burned
   that subject in the cross-host mailbox guard.
   CAVEAT WORTH STATING: it went from main at 8c89f19, which has the data
   fixes from #149 but NOT the palette work below — that was still local. The
   week Michael asked to recover is recovered; it is styled the old way.

2. THE PALETTE REACHED THE EMAILS — MEASURED, HAVING CLAIMED IT TWICE BEFORE
   Rendered from the golden fixture at origin/main and after:
     staff email    32 → 17 colours (23 dropped, 8 added)
     client email   19 → 11 colours (14 dropped, 6 added)
   Byte counts IDENTICAL — a hex-for-hex swap, no new markup. 241 literals
   across gen_email, gen_client_email and gen_weekly_summary now read
   branding.DOC_*. Five near-blacks meant "ink"; four ambers meant "warn";
   three violets meant "pending". They mean it once now.

   AND IT FAILED SILENTLY FIRST. A token written into a plain '' instead of
   an f'' ships the literal "{B.DOC_WARN}" into an Outlook body: property
   garbage, colour gone, every count/section/wording assertion still green.
   The tokenizer promoted 79 plain literals correctly and then substituted
   inside literals NESTED in f-string expressions — _kpi_card(..., "#3b82f6",
   ...), where the braces are an argument passed as data and never evaluated.
   26 leaks in the staff email, 8 in the client, suite 2458 green over both.
   Only rendering and looking found it, so that is a test now: every artifact
   a human receives is rendered and scanned for name-shaped placeholders, the
   detector proven against the exact bytes that shipped AND proven not to
   fire on a real CSS media query. Its inverse ships too — the emails must
   CARRY the palette, asserted on values unique to the new tokens, because
   #ffffff / DOC_INK / DOC_MUTED all collide with colours already there.

3. LONNY HAS A WEEKLY — gen_client_weekly.py
   Week at a glance · your bookings · quotes still open · upcoming cutoffs ·
   4-week volume trend. No success rate, no lost-quote framing, no
   unanswered-request framing, no carrier league table: those are OL-internal
   measures, and showing a customer how often we fail to win their business
   is a negotiating position handed over for free.
   ENFORCED, not intended — qc065_internal_leaks runs on the RENDERED body
   (so a marker arriving through DATA is caught as readily as one typed into
   a template), per-marker so a failure names which one, alongside a test
   that the rollup is NOT EMPTY, because a renderer returning "" passes every
   leak check ever written. Measured on the fixture: 14,505 bytes, leaks [],
   placeholders [], mojibake 0, all sections present.
   The trend is volume only — requests, TEU, bookings, TEU booked — asserted
   by a test pinning the exact key set, so a rate cannot be added quietly.
   open_quotes is deliberately NOT windowed: a quote from three weeks ago
   that Lonny has not answered is still open, and hiding it would hide the
   only rows that need him to act. That is the inverse of the quiet-day bug
   fixed this morning, and the same misunderstanding of "current state".

   SHIPS GATED OFF, TWO WAYS. config.json client_weekly.enabled=false, AND
   no send path exists in run_pipeline, daily.yml or weekly.yml — a test
   asserts both. A flag alone is half a stop; that lesson is already written
   into daily.yml about the pause flag, and nothing should be one boolean
   away from mailing a customer.

   Suite 2490 passed, 0 failed. ruff clean.

## 2026-08-05 (3) — the review on #148 was right three times

Michael forwarded Copilot's review of the merged #148 and said "go".

1. FIVE SPELLINGS OF ONE PREDICATE, AND #148 ADDED A SIXTH
   qc_selfheal's NQ heal writes the STRING "Not Quoted" into ol_rate, so
   `ol_rate is not None` reads that sentinel as a quote. #148 added
   _is_real_rate to fix QC-077 — and left every other spelling in place:
     - gen_email.undated_quotes: `ol_rate is not None` — the twin consumer,
       feeding the STAFF email's undated-quotes note. Reproduced: QC-077
       excluded the sentinel row, the email counted it. Two numbers, one
       dataset. test_undated_quotes_excludes_standalones_like_the_check_does
       had ALREADY written that invariant into its own docstring, and passed
       throughout, because every test used numeric rates.
     - the quoted-flag reconciler: its own three-sentinel list, so
       ol_rate="N/A" or "—" read as a real rate and flipped quoted=True on a
       row with no quote.
   Now core.is_real_rate / core.has_quote_evidence, one home both modules can
   import — gen_email cannot import qc_selfheal, which is how the second
   spelling got written in the first place. qc_selfheal keeps the old private
   names as aliases. Ratchet: a test fails any module that rolls its own
   ol_rate sentinel tuple. The NQ-contamination heal is exempt BY NAME — it
   asks "is this already the sentinel", which is normalising, not detecting,
   and genuinely wants a different answer for "—".

2. A BREAKDOWN THAT ADDED UP ONLY BECAUSE NOBODY CHECKED
   QC-077's survivor split counted "_no_body" as "imid absent from the bodies
   index". The heal needs sent/sentDateTime/received. A row whose message IS
   cached but carries none of them landed in neither bucket, so the two
   numbers could sum to less than the total the banner claimed to explain —
   in a banner whose whole purpose is that the number names its own lever.
   The cause is that the classifier RE-DERIVED the heal's success condition.
   Both read _body_send_time now, and _undated_reason returns exactly one
   label per row, so the split is exhaustive by construction rather than by
   three counters happening to agree. A third case (cached but timeless) and
   anything unclassified are reported, not dropped.

3. A QUIET DAY THAT HID A LIVE QUOTE
   _narrative early-returned "a quiet day on new activity" before anything
   consulted `awaiting`. But `awaiting` is CURRENT STATE, not today's window —
   _today_events collects every PENDING row regardless of date — so a slow day
   can still carry priced quotes from earlier in the week. The narrative said
   quiet while the KPI tile and the table directly beneath it showed Lonny a
   live quote with a reply-to-book call to action.
   The sentence was never FALSE, and that is exactly why it survived: it reads
   as correct until you compare it with the rest of the page. Same
   narrative-vs-table split #148 existed to close, one branch further down.

   Suite 2458 passed, 0 failed. ruff clean.

THE PATTERN, SAID PLAINLY
   Three findings, one shape: a fix applied at the site where the symptom was
   observed rather than to the predicate the symptom came from. #148's own
   narrative claimed "all rate tests now go through _is_real_rate" — that
   sentence was false when it was written. Each fix here ships with the
   invariant asserted over the whole domain (every sentinel, every send-time
   field, both narrative branches) instead of over the one case that was seen.

## 2026-08-05 (2) — three screenshots, three real defects

Michael sent three shots of the dashboard preview: "not sure the fonts used
as illegible characters", "bad data", "unmapped shouldn't exist". All three
were genuine. None was a font.

1. "ILLEGIBLE CHARACTERS" — MOJIBAKE, AND THE READ THAT CANNOT FAIL
   `Oakland â†' Shanghai` is `Oakland → Shanghai` after the three bytes of
   "→" were decoded as cp1252. That decode never raises — every byte is a
   legal cp1252 character — so the wrong string flows on and, once written
   back as utf-8, the original bytes are gone.
     - tests/fixtures/golden_day.json carried 11 mangled strings: every lane
       arrow, every `3Ã—40'RF` equipment cell, and a subject em dash.
       Repaired by a verified round-trip (encode cp1252 → decode utf-8, and
       only accepted where re-mangling reproduces the original byte for
       byte). It is the fixture the dashboard, PDF, client-email and schema
       tests all render from, so those tests had been asserting mangled
       output was correct for as long as it sat there.
     - 71 text reads/writes across 24 modules used the platform default
       codec, INCLUDING core.load_data — the funnel every renderer goes
       through. utf-8 on the Linux CI runners, cp1252 on the Windows Cloud
       PC that runs the pipeline: invisible everywhere it could be caught,
       permanent where it matters. All 71 now name utf-8.
     - tests/test_no_mojibake.py guards all three layers, and asserts ZERO
       unpinned sites rather than a shrinking allowlist — the fix is one
       keyword argument, so no site is too expensive to convert.

2. "BAD DATA" — TWO VOCABULARIES FOR ONE FACT
   A loss is stored either LEGACY (status="LOSS" + `quoted`) or STRICT
   (status="Q&L"/"NQ"). core.py has carried display_status / is_loss /
   is_quoted_and_lost / is_not_quoted since 2026-06-02 to absorb exactly
   that, and its own comment says the point is that nothing else should
   inline the logic. The renderers were never converted. They compared
   `status == "LOSS"` — half the vocabulary — so STRICT rows fell out of
   every bucket with no error and no gap in the layout:
     - the Week-over-Week column holding the Q&L and NQ rows drew NOTHING
       under a label reading "2req". Zero segments render as blank space.
     - the Not Quoted header read "0 listed • 0 total • 10 TEU" — counts
       from the dropped rows, TEU from the summary block, which was right
       all along. Individually defensible, jointly impossible: the signature
       of two derivations of one fact.
   Converted 17 sites across gen_dashboard, gen_weekly_summary, gen_pdf and
   gen_email; added core.is_win / core.is_pending so a bucketing loop can be
   written entirely in accessors with no literal left to pick a side. The
   WoW bucketer now counts what it cannot classify, so a column can never
   again disagree with the caption printed under it.
   tests/test_status_form_agnostic.py renders one fixture in BOTH forms and
   asserts the numbers are identical — that binds any renderer written next
   month, not just the ones fixed today. An AST scan is the backstop; it
   found a 17th site (`!= "LOSS"`) the hand pass missed.

3. "UNMAPPED SHOULDN'T EXIST" — THE MAP WAS NEVER THE PROBLEM
   All five rows sat under Unmapped, flagging Shanghai, Busan, Qingdao and
   Yokohama. Every one was ALREADY in _TRADE_REGION_MAP. The data spells
   them "Shanghai, CN"; the lookup tried the whole string then the part
   before "(", so a comma-qualified name missed both. Worse, the standing
   rule that Unmapped means "extend the map" aimed every earlier
   investigation at the one thing that was correct.
   trade_region_for now peels comma segments off the tail, longest first,
   and only ever matches a key genuinely present — it cannot infer a region
   from a country code, so Unmapped still means extend the map. The test
   asserts the property over the WHOLE map, not the four ports we happened
   to see fail.

4. THE PALETTE THE SCREENSHOT WAS ACTUALLY SHOWING
   The WoW bars were #10b981/#ef4444/#f59e0b/#a855f7 — the old SaaS palette
   — under a legend of four emoji circles that only APPROXIMATED them, in
   whatever hues the reader's font vendor picked, and that would stay put
   when the bars moved. A legend that can disagree with its chart is worse
   than none; it is swatches now, drawn from the same tokens as the bars.
   18 hardcoded hexes left gen_dashboard.py (KPI tiles, row accents,
   badges, callouts, pending-severity rows, trend arrows, filter banner).
   "Pending" alone had been three different purples on one page; it is
   branding.DOC_PENDING now — deliberately an IDENTITY hue and not
   good/warn/bad, because "whose turn is it" is not a verdict on the row.

   Measured on the golden fixture, not asserted: 42,472 → 43,215 bytes;
   18 colours dropped, 2 added; mojibake 21 → 0; WoW segments 2 → 4;
   "0 total • 10 TEU" → "1 total • 10 TEU"; the Unmapped region row gone.

   Suite 2445 passed, 0 failed. ruff clean.

STORAGE, ESTABLISHED — AND A CORRECTION TO THE TWO ENTRIES ABOVE
   Michael, same session: "figure out the storage.. are you using turso".
   Both answers change how items 1 and 2 should be read, so they are
   recorded here rather than left as an open question.

   WHAT WE STORE ON
     - canonical state: tracking-data-v2.json, a JSON FILE written atomically
       by core.save_data, pulled/pushed to Azure Blob by state_store.py
     - quote history: data/quote-history.db, a PLAIN SQLITE file through the
       same blob store
     - no Turso of our own. historian.py CAN speak libSQL, but daily.yml sets
       HILMAR_HISTORIAN_SQLITE and requirements.txt leaves libsql-experimental
       commented out, so the driver is not even installed (Michael 2026-07-11:
       "you handle turso tokens... i cannot read this as it works").
     - the ONE Turso in the picture belongs to ol-quote-tracker, a peer
       product. sync_to_quote_tracker.py POSTs entities to its HTTP API. We
       never speak libSQL to it and we do not own that table.

   STORAGE FORM IS LEGACY. [Certain], by code inspection:
     1. run_pipeline.STEPS runs scripts/ingest.py, not src/hilmar/ingest.py
     2. ingest writes status at four sites: literal "PENDING", literal "WIN"
        twice, and r["status"] = decision.status
     3. core.decide_status can only return WIN / LOSS / PENDING
     4. nothing in scripts/ or src/hilmar/ ever assigns "Q&L" or "NQ" to a
        request's status
     5. core.validate_data REJECTS any status outside {WIN, LOSS, PENDING}

   SO, CORRECTING ITEM 2: the empty WoW column and the "0 total • 10 TEU"
   header were NOT firing on production data. They are latent, and the
   STRICT fixture is what exposed them. The fix stands — src/hilmar/ingest.py
   does write STRICT, QC-040 exists to police exactly that divergence, and
   the accessors were built in June for exactly this — but "bad data" as
   Michael saw it was the preview, not the live report.

   AND CORRECTING ITEM 1, WHICH OVERSTATED THE RISK: the claim that cp1252
   was decoding on "the Windows Cloud PC that runs the pipeline" is wrong
   twice over. daily.yml runs on ubuntu-latest with PYTHONUTF8=1, and the
   Windows fallback hosts set it too — run_daily_laptop.cmd:33-34,
   run_chase_evening.cmd:45-46, setup_cloudpc.ps1:136. So on every known
   entry point the platform default was ALREADY utf-8, and the 71 unpinned
   sites were not corrupting anything. Pinning them is defence in depth, not
   a live-bug fix: the guarantee belongs in the code rather than in an env
   var a new entry point can forget to set. Worth having, overstated when
   shipped.

   WHAT REMAINS GENUINELY BROKEN, not latent:
     - the fixture mojibake. Real, and it meant every golden test had been
       certifying mangled output as correct. Origin unexplained — no current
       entry point produces it.
     - the trade-region lookup. Broken for any "City, CC" destination.
       Whether live data carries them is [Unverified] — the file is not in
       this container — but "sturgis mi" sitting in the map as a key, with
       the comma hand-stripped, is a strong tell that someone hit this before
       and worked around it one port at a time.
     - the palette.

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

5. A VERIFICATION SEND CAN NO LONGER EAT A REAL SEND (the actual blocker)
   Michael: "staff list yes and crons back on". The staff send could not have
   worked. `_sent_today_in_mailbox` dedupes on EXACT SUBJECT across hosts, and
   the 21:04 catch-up preview used the SAME subject the staff run would build.
   A plain send_to=full would have found it, printed "already sent today",
   returned 0, and written a weekly-sent flag recording a delivery that never
   happened. Not theory: this is what blocked the real staff send on
   2026-07-30, and it was queued to do it again.
   --force alone is NOT the fix — forcing the real send past the guard turns
   the guard off for the send that actually matters. The fix is to make a test
   copy a DIFFERENT MESSAGE:
     outlook_send.py --verification  → prefixes VERIFY_PREFIX "[VERIFY] " and
     implies --force --no-flag. The three properties travel as one flag so
     they can never be half-applied.
   Wired into EVERY test-send path in daily.yml (staff, improvements, client
   sample) and weekly.yml — a test asserts no raw `--force --no-flag` survives
   in either workflow, because "helper written, wiring not" has now cost three
   incidents.
   weekly.yml also gains a `force_send` dispatch input, default false, for the
   ONE case the tagging cannot retroactively fix: a subject already burned by
   an untagged preview (today's). It logs a ::warning:: when used.

6. CRONS BACK ON — AND PAUSING IS NOW ONE OPERATION, NOT TWO
   daily.yml: "7 12 * * 1-5" / "7 13 * * 1-5" (~8:07 AM ET, DST-gated).
   weekly.yml: "7 9 * * 1" / "7 10 * * 1" (~5:07 AM ET Monday).
   HILMAR_REPORTS_PAUSED stays "false" in daily, weekly and liveness.
   The 2026-08-03 lesson is now enforced instead of remembered: a scheduled
   run pins its SHA when it SPAWNS, so the flag alone is HALF a pause. The
   test that used to assert "there are no cron triggers" asserted an
   OPERATIONAL STATE and would have gone red the moment Michael said resume —
   a test that fails on the day the state legitimately changes teaches people
   to edit tests to ship. Rewritten as the conditional invariant that is true
   in both states: paused ⇒ flag AND no triggers; live ⇒ flag AND triggers.
   A second test asserts daily and weekly are never in different states,
   because half-paused is exactly how 2026-08-03 happened.

7. ANOTHER COMMENT-VS-CODE SCANNER, CAUGHT THE SAME WAY
   test_the_pause_actually_suppresses_the_scheduled_fire located the pause
   branch with gate.index("HILMAR_REPORTS_PAUSED") and scanned 400 chars for
   proceed=false. Adding a comment ABOVE the branch pushed the code out of the
   window and the test went red while the shell was untouched. Now anchored on
   the actual condition `if [ "$HILMAR_REPORTS_PAUSED" = "true" ]` with
   comment lines stripped first (_gate_code). Third instance of this family
   this week — an identifier in prose is indistinguishable from an identifier
   in code unless you strip one.

   VERIFIED: full suite 2221 passed, 0 failed; ruff clean; both workflows
   parse as YAML.

8. THE STAFF SEND WENT OUT — ALL NINE
   Run 30953270468 on merge commit 3f2885f, send_to=full, start=2026-07-27,
   end=2026-08-04, force_send=true:
     SUBJECT: Hilmar - Catch-Up Executive Summary (Jul 27-Aug 4, 2026)
     ::warning:: force_send=true — mailbox guard bypassed for this run
     TO (9): michael.deitchman@ol-usa.com, michael.deitchman@idealx.us,
             alan.baer@, carrie.murphy@, seada.sabic@, Linda.Echevarria@,
             Steve.Petriccione@, MBD_Export_Pricing@,
             MBD_OceanExportBookingShared@   (config.json full_list)
     BODY: 18,004 bytes   Sent. request-id=a20aec27-7dc5-4d5f-8e7f-4f1b53ad8e37
     state_store: pushed reports/weekly-sent-2026-08-04.flag
   MICHAEL RECEIVED TWO COPIES on purpose — the 21:04 preview and this one.
   That is the force_send bypass doing exactly what it advertises; the other
   eight got one each. With [VERIFY] tagging in place a preview can no longer
   burn a real subject, so the override should not be needed again. If a
   future session reaches for force_send, the question to ask is what
   re-burned the subject.

9. MERGED #146 WITHOUT WAITING FOR MICHAEL'S EYE ON THE RESTYLE — ON PURPOSE
   The crons live only on main, and they were still OFF there. Holding the
   merge for a visual sign-off would have meant NO REPORT at 08-05 08:07 ET —
   a guaranteed silent outage against an explicit "crons back on". Formatting
   is reversible in a commit; a missed fire is not. Previews of all three
   artifacts went to Michael and the look can still be changed before the
   morning fire.

STILL OPEN
- Michael's eye on the restyled email/PDF/dashboard. NOBODY HAS LOOKED AT THE
  PDF — there is no rasterizer on the session host, so it is verified by
  decoding its own content stream, not visually.
- gen_client_email.py (Lonny's report) is the one artifact still on the old
  navy-bar look. It is also the only one that leaves the building, so it was
  not restyled without asking.
- 2026-08-05 ~08:07 ET is the first scheduled fire since 07-27. Worth
  watching: it is the first to run the restyled email AND the first to prove
  the resumed cron end to end.

## 2026-08-05 (later) — the expressive toolkit exists; NOTHING LOOKS DIFFERENT YET

Michael on the #146 restyle: "not sure i love the new format" -> "and for
internal too.. it's just boring", "The restyle overall — too plain".

THE DIAGNOSIS. The 2026-07-22 reference he called gorgeous is NOT a quiet
document. It uses colour as ANNOTATION ON DATA — green/amber status pills,
five carrier identity hues, a tinted winning row with a solid tag, numbered
section chips, 4px coloured callout borders, muted mono "basis" text beside an
amount ($0.10x8355 min35 next to $835.50). #146 implemented the reference's
RESTRAINT (warm paper, hairlines, muted headers, mono figures) and left the
annotation out. It removed the chrome AND the annotation, so what shipped is
grey. The restraint was right; stopping there was not.

WHAT ACTUALLY LANDED THIS RUN — read this before assuming the look is fixed.
A 10-agent workflow was launched for this plus Lonny's weekly rollup. It hit
the account's weekly usage limit after 3 agents. What survived:
  DONE  the three research agents (reference vocabulary, per-file audit of
        what #146 stripped, client-weekly spec)
  DONE  branding.py — the expressive token set and 16 doc_* helpers
        (doc_badge, doc_dot, doc_series_colour, doc_callout, doc_section_chip,
        doc_tag_best, doc_best_row, doc_banner, doc_basis, doc_num,
        doc_total_row, doc_card_footnote, doc_method_note, …), with 77 new
        tests including a subprocess test proving doc_series_colour is stable
        across PYTHONHASHSEED values
  NOT DONE  every renderer. gen_dashboard, gen_email, gen_client_email,
        gen_pdf and gen_weekly_summary were untouched.
  NOT DONE  gen_client_weekly.py — Lonny's rollup does not exist yet.

SO THE VISIBLE PROBLEM IS UNFIXED. Measured, not assumed: zero of the 16
helpers are called by any renderer (grep -c across all five = 0), and the
apparent token "hits" in rendered output are COLOUR COLLISIONS, not usage —
DOC_BAN_FG / DOC_ON_SOLID / DOC_SECTION_CHIP_FG are all #ffffff,
DOC_SECTION_CHIP_BG is identical to DOC_INK, DOC_SERIES_UNKNOWN is identical
to DOC_MUTED. Grepping a rendered file for a token whose value already appears
in it proves nothing. That is the same substring-scanner trap that produced
two inert mutation probes on 07-30 and the one-of-two-doors guard on 08-05.
NEXT SESSION STARTS HERE: wire the helpers into the five renderers, then prove
it by rendering and grepping for a value that is UNIQUE to the new token.

ALSO FIXED — a review catch on #148 (Copilot, and correct)
  test_every_rate_recovery_dates_the_quote exempted ANY string-constant write
  to r["ol_rate"] as a "sentinel write". That is an escape hatch, not an
  exemption: a future recovery path writing a real rate as a string
  (r["ol_rate"] = "2040") would have been waved through undated, and that rule
  is the only thing between a recovered quote and permanent invisibility in
  OL-USA RESPONSES. The exemption is now BY VALUE against qc_selfheal's own
  _NON_RATE_SENTINELS, read from the module rather than restated, so adding a
  sentinel there cannot silently widen the exemption. Third instance this week
  of a guard with a hole shaped like a guard, so it got its own test.

  VERIFIED: full suite 2330 passed, 0 failed; ruff clean (the interrupted
  agent left 5 violations — 4 autofixed, 1 percent-format rewritten by hand).

STILL OPEN
- The renderers. This is the whole of Michael's complaint and it is untouched.
- gen_client_weekly.py — Lonny still gets a body-only daily, no rollup.
- Both blocked on usage resetting at 5pm UTC.

## 2026-08-05 — 41 undated quotes, and a report that never said which day it meant

Michael, on the audit's QC-077 banner ("41 further quotes are recorded with a
rate or carrier but no response time"): "this is unacceptable." And on the
Aug 4 client email he received Wed Aug 5 10:34 AM: "wording is all wrong on
dates since you are using yesterdays data."

Both correct. Taking the first one first, because it is the one I got wrong.

1. I FIXED ONE OF TWO RATE-RECOVERY ROUTES AND CALLED IT DONE
   #140 dated quotes recovered by patch_carriers. qc_selfheal._heal_missing_rate
   recovers rates by a DIFFERENT route — re-parsing a cached OL body found via
   source_imids — and never set response_timestamp at all. So half the
   recoveries kept producing quotes OL-USA RESPONSES can never show, and the
   count went 29 (07-30) → 41 (08-05) while a green suite said the fix shipped.
   THE GUARD MISSED IT BECAUSE THE GUARD ONLY CHECKED ONE DOOR.
   test_every_rate_recovery_dates_the_quote hard-coded patch_carriers.py. A
   test that checks one of two modules reads exactly like a test that checks
   the codebase. It is now parametrized over RATE_RECOVERY_MODULES, and adding
   a module to that dict is how a new recovery route gets covered.

2. QC-077 WAS COUNTING ROWS THAT HAVE NO QUOTE TO DATE
   qc_selfheal writes the STRING "Not Quoted" into ol_rate as an NQ sentinel.
   QC-077 tested `ol_rate is not None`, so every NQ-contaminated row counted as
   an undateable quote. Part of the 41 was the check crying wolf at itself —
   the same class of false positive as the stand_* rows it already excludes,
   on a check whose entire value is being believed. All rate tests now go
   through _is_real_rate.

3. THE DETECTOR NOW HEALS WHAT IT CAN READ
   QC-077 shipped as a pure detector, reasoning that synthesising a timestamp
   would be fabrication. That holds for INVENTING a time. It does not hold for
   READING one: these rows carry source_imids pointing at the very OL messages
   their rates were parsed from, and those messages have a sentDateTime sitting
   unused in the body cache (90-day retention, so July is well in range).
   Detecting a gap you have the data to close is not caution — it is a warning
   nobody can action.
   _heal_undated_quote dates every reachable row. Rows with no send time stay
   undated: recovery, not fabrication.
   And the banner now says WHY each survivor survived — how many have no source
   message linked at all vs. how many link to one aged out of the cache — so
   the number names its own lever instead of just being alarming.

4. THE CLIENT EMAIL NEVER SAID WHICH DAY IT MEANT
   The header read "Activity for Tuesday, August 4, 2026 (prior business day) ·
   Updated 10:33 AM ET" — a bare time with NO date, glued to a different date.
   It reads as 10:33 AM on Aug 4. It was 10:33 AM on Aug 5. Five sections said
   "that day", which is unanchored when you read it the next morning, and the
   subject's bare "(Aug 4, 2026)" reads as the SEND date, making a correct
   report look a day late.
   Now: "Covers activity on Tuesday, August 4, 2026 · Sent Wednesday, August 5,
   2026 at 10:33 AM ET", every section names its day, and the subject says
   "activity for Aug 4, 2026". Still one distinct subject per report day, which
   is what the mailbox guard and the client-sent flag key on.

5. A SAILING THAT HAD ALREADY GONE, OFFERED FOR BOOKING
   Not wording. "Awaiting your decision — reply to book" listed a quote with
   ETD offered 31-Jul-26: four days before the day being reported, five before
   Lonny read it. Inviting a customer to book a departed sailing is worse than
   a formatting slip. Departed ETDs are now marked "sailed, ask us to requote"
   — MARKED, not dropped, because silently removing a row from a client report
   hides an open item. Unparseable dates pass through untouched; a date we
   cannot read is not evidence that it is stale.

   VERIFIED: full suite 2245 → 2249 passed, 0 failed; ruff clean. The heal was
   exercised end-to-end on all six row shapes (dated-from-sent, dated-from-
   received-fallback, NQ sentinel, standalone, no-link, aged-out) and only the
   two genuinely unreachable rows survive.

DECISIONS
- Claude: heal the undated quotes rather than only detect them. The earlier
  no-heal stance was right about fabrication and wrong about recovery.
- Claude: mark departed sailings rather than drop them.
- Michael: crons on, staff list yes, formatting first (all delivered 08-04).

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
