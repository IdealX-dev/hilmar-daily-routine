# Changelog — Hilmar Daily Tracker

Per the working standard (CLAUDE.md): every session logs its decisions here,
by name, so the next session starts current. Newest first.

### 2026-09-04 (latest) — the fire had no transcript, so four defects hid behind one blind channel

Michael: **"just bloody fix this all"** — the four items carried as open at the
end of the prior session. Measuring them turned up a fifth that explains why
the other four survived so long.

**THE ARTIFACT UPLOAD WAS EMPTY. NOT SPARSE — EMPTY.** Production fire
33864188808 (2026-09-04), the "Upload run artifacts" step, verbatim:

    ##[warning]No files were found with the provided path: reports/run-log.txt
    reports/qc-result.json reports/test-result.json reports/coverage.json.
    No artifacts will be uploaded.

`list_workflow_run_artifacts` on that run returns `total_count: 0`, and the
step still went green on `if-no-files-found: warn`. Every file each of the
four items would have been diagnosed from was missing, for four separate
reasons:

| file | why it was absent |
|---|---|
| `run-log.txt` | only the Cloud-PC wrappers ever wrote it (`>> "%LOG%"`). `grep -n run-log scripts/run_pipeline.py` returns nothing |
| `qc-result.json` | **BOTH** qc_selfheal passes were SIGKILLed at their 180s cap, before PHASE 7 PERSIST. Not just post-patch — the pre-patch pass too |
| `test-result.json`, `coverage.json` | `HILMAR_SKIP_PIPELINE_TESTS=1`; test.yml is the authoritative gate |

So the fire shipped to nine staff recipients AND to Lonny with **no completed
QC pass behind it**, and `assert_fire_integrity` printed "Fire integrity OK".

**A KILLED STEP ALSO THREW AWAY ITS OWN EVIDENCE.** `subprocess.run` SIGKILLs
on timeout and SIGKILL discards the child's stdio buffer; Python block-buffers
stdout whenever it is not a TTY, which on a runner it never is. Measured with
one variable changed — a child that prints, sleeps past the timeout, and is
killed:

    buffered           -> line LOST
    PYTHONUNBUFFERED=1 -> line SURVIVES

That is why QC-057's body snippets, written expressly so a parser fix could be
scoped from them, have never once been readable: the pass computed them and
died holding them. (A first run of that measurement was INVALID — the shell
exported `PYTHONUNBUFFERED=1`, so both arms were unbuffered. Re-run with a
scrubbed env.)

**Shipped:**

1. `run_pipeline` writes `reports/run-log.txt` on every host, via an
   fd-level tee — `dup2` onto fds 1 and 2, not a `sys.stdout` wrapper, because
   `run_step` calls `subprocess.run` WITHOUT capture and every step's real
   output comes from a child writing to the inherited fd. Gated off on the
   Cloud PC, where the wrapper already redirects into the same file.
   Children now run `PYTHONUNBUFFERED=1`. Per-step `--- name ---` markers,
   the two date spellings and a `PY:` line, so the existing parsers read one
   format on both hosts. THE FIRST DRAFT'S TEARDOWN ORDER WAS WRONG — it
   closed the saved console fd before joining the pump, so the last line
   reached the file and not the console; caught by measuring, and the test
   for it fails against that ordering.

2. **QC-021 was not merely skipped on Actions — it emitted NOTHING.** Its body
   sat inside `if _log_path.exists():` with no else. A check with a QC-INDEX
   row, making zero Log calls on every production fire, is indistinguishable
   from a passing one. Now host-aware: on the ephemeral runner it asserts the
   transcript opened, and does NOT hunt for `Sent. request-id=`, which cannot
   be there — `outlook_send` is a separate workflow step that runs after
   `run_pipeline` exits, outside the tee. Writing the file without this would
   have converted silence into a daily false alarm. Cloud-PC path untouched.

3. **QC-019 is an ordering bug, and Seer
   was wrong.** Seer: "body_parser.py fails to extract the carrier from the
   pipe-table or prose." Reproduced instead, driving the real `phase_6_rules`
   on the production row shape:

       ERROR: QC-019: ... have no carrier — parser missed extraction
       FIX:   QC-056: backfilled carrier from row text — Oakland → Busan=HMM
       carrier_quoted after the pass: HMM

   One pass, both lines: QC-019 sat ~166 lines above QC-056 and reported a
   defect the same run repaired. Moved below the heals, with a structural test
   pinning the order. Severity UNCHANGED — Michael set it to ERROR on
   2026-05-13 and a row still blank after every recovery is still an error.
   The message now names the `request_id` (the 2026-08-27 event read only
   "16:59:21 Lane unresolved") and says "no carrier in ANY source". That
   re-keys the Sentry grouping: TRACKER-3 will fall quiet, and the quiet is
   the false positives stopping, not the check being switched off. Second Seer
   misdiagnosis this month.

4. **QC-057's diagnostic was blind.** Its hint list contained `" to "`, which
   matches the standard confidentiality footer ("...entity **to** whom they
   are addressed..."), and a hinted line SUPPRESSED the head-line fallback —
   so for a body whose only hinted line was the disclaimer, the operator saw
   the disclaimer and none of the email. Measured. Head lines of the top
   message (after `core._strip_chain`) are now always included and get first
   claim on the budget, boilerplate is dropped, scrubbing stays last. An
   existing test asserted the old behaviour (`"Hi team" not in snippet`); it
   was updated deliberately, with the reason recorded in place — that filter
   is what dropped the real content.

5. **A booking ref is a booking ref, whichever field holds it.**
   `scripts/core.decide_status` read `mdolx_ref` only; the library has always
   read the union with `mdolx_refs_all`. Same row, two verdicts — production
   LOSS/`SEND_NO_BOOKING` "booking never confirmed", library WIN. The parity
   suite could not catch it: production's signature did not accept the
   argument, so the call raised rather than disagreed. Production was wrong on
   three counts — Reading B asks for a booking confirmation, not one in a
   particular field; `booking_count` and `is_confirmed_win` already read the
   union; and the row was calling itself a loss while holding the booking. It
   also erased its own evidence: `booking_count` gates on the stored status,
   so once the QC pass wrote LOSS the count fell 1 → 0 and the contradiction
   vanished. `qc_selfheal`'s decide loop is the one that flipped them — unlike
   `ingest.age_requests` it has no union-aware skip guard, and it runs twice
   per fire. Both call sites now pass the union, and
   `_merge_prior_win_into` holds the invariant at the writer.

**The adversarial pass caught two defects in this change, before merge:**

6. **The tee would have made QC-055 LIE.** QC-055 reads `[-50000:]` of
   run-log.txt looking for `Sentry cron start failed (pipeline continues)` —
   a window sized when that file held only the wrapper's terse step echoes.
   The sentinel prints at the START of the fire; QC-055 runs two pipeline
   steps later. Measured against the pre-fix block, sentinel present each time:

       10,052 bytes -> ERROR (correct)
       45,026 bytes -> ERROR (correct)
       60,048 bytes -> SILENT OK   ** false pass **
       90,034 bytes -> SILENT OK   ** false pass **

   Fire 33864188808 carried ~89 KB. So the tee would have replaced an honest
   "skipped — ephemeral runner" with an invisible false pass every fire —
   through `Log.ok`, which never reaches `qc-result.json`. Strictly worse than
   the gap being closed. QC-055 now scopes to TODAY's fire header instead of a
   byte tail, warns (never `ok`) when it cannot find its evidence, and no
   longer re-raises a failure that predates today's fire.

7. **The transcript is not the non-PII debug artifact that upload list was
   written for.** `daily.yml`'s own comment forbids uploading plaintext
   carrying "client name, lanes, carriers, OL rates, the distribution list,
   MDOLX booking refs". Fire 33864188808's pipeline output carries 29 distinct
   email addresses (Lonny's included), 95 MDOLX refs, OL rates and lanes, and
   `sentry_setup._scrub_string` redacts addresses and refs but NOT lanes or
   rates — so scrubbing would not satisfy the rule. `reports/run-log.txt` was
   therefore REMOVED from the artifact path in the same change that started
   writing it. The transcript stays on the runner where the QC checks read it;
   the per-step detail is already in the job log, the channel this evidence
   has always used, whose exposure is unchanged. A scrubbed upload is
   available if Michael wants it — that is his call, not an engineering one.

8. **QC-021 credited a fire with the PREVIOUS fire's send.** Pre-existing,
   surfaced by the same adversarial pass. It located today's marker with
   `.find` — the FIRST occurrence — so on the accumulating Cloud-PC log, on a
   day with two scheduled fires, `_after` spanned from fire #1's header and
   matched fire #1's `Sent. request-id=`. MEASURED: a second fire, mid-flight
   and having sent nothing, printed "today's wrapper completed send step" —
   a send monitor reporting a send that did not happen. Now `.rfind`. Not
   introduced by the tee (run-log.txt is not in `state_store`'s synced set, so
   each Actions fire starts a fresh file), but live on the wrapper path.

9. **QC-021 still read a byte tail, and at the real transcript size that was
   a false alarm every fire.** Copilot's review on PR #254 caught it, and it
   reproduced immediately: the fire HEADER is written at the START of a
   transcript, QC-021 sliced `[-40000:]`, and a production fire carries ~89 KB.

       5,087 bytes -> ok
      39,053 bytes -> ok
      89,084 bytes -> FALSE WARN "carries no fire header"

   So the host-aware branch added in item 2 would have warned "the transcript
   tee did not open" on every fire in which it *had* opened — the exact daily
   false alarm that branch exists to prevent. Every QC-021 test written for
   item 2 used a fixture of a few hundred bytes, so none of them could see it.
   Same defect class as QC-055's 50 KB window, in the same PR, and I fixed one
   and left the other. Both branches now read a bounded window off the END of
   the file (2 MB — the Cloud-PC log appends for months) and locate today's
   LATEST fire header inside it. Regression tests are sized like a real fire.

10. **The QC-019 move was wrong, and I shipped it before the review caught
    it.** Landing QC-019 after QC-056 put it 716 lines BEFORE QC-064, which
    NULLS a garbage carrier out of the client-visible fields. Reproduced on a
    status-change WIN, both of QC-064's real garbage classes:

        carrier_quoted='209-656'                         (phone fragment)
        carrier_quoted='OL Ocean Export Booking mailbox' (mailbox name)
          QC-019 said: NOTHING
          carrier after the pass: None / None

    A blank carrier cell shipped and the check that exists to catch it was
    silent — a noisy false POSITIVE traded for a silent false NEGATIVE, which
    is worse, and QC-019's clean path is `log.ok`, which never reaches
    `qc-result.json`. My own comment and the QC-INDEX row both claimed "Runs
    AFTER QC-056/QC-064", which was false by line number. The rule is not
    "after the heals" but AFTER EVERY WRITER THAT CAN CHANGE A CARRIER:
    QC-056 fills them, QC-064 empties them. Now placed after QC-064, and the
    structural guard pins it against BOTH — the first guard asserted only
    `qc019 > qc056` and passed with the hole wide open.

11. **"75 Sentry events" was wrong — it is 4.** I read the occurrence count
    off issue HILMAR-DAILY-TRACKER-3 and attributed it to QC-019.
    `capture_qc_error` does not set a per-check fingerprint, so eight checks
    group into that one issue; an issue-level total is not a check's count.
    Measured on the `qc_check:QC-019` tag: **4 events in 90 days** —
    2026-09-04, 2026-08-28, 2026-07-20, 2026-07-18. Corrected in the source
    comment, the test docstring, QC-INDEX and here. Related and recorded, not
    fixed: the missing fingerprint is why QC-019 shared an issue, an
    occurrence count and a Seer analysis with seven other checks.

    SCOPE, honestly: the ordering mechanism is proven on the 2026-09-04 row
    shape only. The other three events are NOT established as the same cause —
    2026-08-27 read "Lane unresolved", and the two 2026-07-17 events read
    "Oakland → Oakland", a degenerate lane that is QC-073's territory and may
    be a different defect wearing QC-019's message.

14. **The transcript now contains the checks' OWN output, and that broke the
    anchor a third time.** Reported by an automated reviewer on PR #254.
    QC-021 and QC-055 both located "where today's fire starts" with `rfind`
    on a bare date string — sound while run-log.txt held only the wrapper's
    terse step echoes, unsound the moment run_pipeline teed the whole
    transcript into it. QC-021 prints
    `✅ QC-021: transcript open for 2026-09-04 ...`, which carries today's ISO
    date and runs BEFORE QC-055 in the same `phase_6_rules` pass. MEASURED on
    a transcript shaped exactly as the tee writes one:

        sentinel IS in the file: True
        QC-055 recorded: (NOTHING)
        QC-055 printed : ✅ QC-055: Sentry cron heartbeat registered on this fire

    A real Sentry cron-start failure reported as registered, on every normal
    fire. Both checks now anchor on the fire HEADER via a line-anchored regex
    covering both host forms (`HILMAR FIRE <iso> ...` and
    `Hilmar daily on <BOX> — <us> ...`), in one shared helper.

    THIS IS THE THIRD INSTANCE OF ONE MISTAKE IN THIS PR — QC-055's 50 KB byte
    tail, QC-021's 40 KB byte tail, and now a bare date substring. The rule
    the three share: **a byte offset or a bare date is the wrong anchor for a
    file whose content this change controls.** Each was found by review, not
    by me, and each was found only after the previous one was fixed.

12. **A SCHEDULED FIRE IS ALWAYS `send_to=full`, so merging IS the send
    decision.** Verified in `.github/workflows/daily.yml:381`:

        SEND_TO: ${{ github.event_name == 'schedule' && 'full' || github.event.inputs.send_to }}

    The `send_to=test` default guards MANUAL dispatch only, and
    `HILMAR_REPORTS_PAUSED` is `"false"` (live). So merging PR #254 puts the
    changed win numbers in front of the nine-person staff list and Lonny on
    the next weekday cron (`30 10 * * 1-5`) with no further human step. A send
    is irreversible; CLAUDE.md requires written approval first. The PR is
    therefore green, mergeable and DELIBERATELY NOT MERGED.

13. **`scripts/diag_refs_all_only.py` — so the decision has a number.** The
    impact was `[ASSUMPTION]` and an assumption is not a basis for a client
    send. The diagnostic counts rows in the hazard shape (a booking ref
    present only in `mdolx_refs_all`), splits them by stored status, reports
    how many are `preserved_from_prior`, and — the part that matters — runs
    production's classifier BOTH ways to report the actual delta in bookings,
    win rate and TEU rather than estimating it. Read-only: pulls to a temp
    dir, computes every decision on copies, writes no blob, sends nothing,
    and prints request_ids and refs but not lanes, rates or addresses (item 7
    established the run log is not a PII-clean channel). Fails loudly —
    `::error::` and exit 2 — with no `|| true`.

**THIS MOVES CLIENT-FACING NUMBERS, UPWARD, AND THE SIZE IS UNMEASURED UNTIL THAT DIAG RUNS.**
Item 5 flips `refs_all`-only rows to WIN: wins up, win rate up, TEU moving
from quoted-lost to won, and rows appearing under "Your confirmed bookings" in
Lonny's report that were invisible yesterday. That is the CORRECT outcome when
the ref is real — a ref only reaches `mdolx_refs_all` by parsing an actual OL
booking email — but the live incidence is **[ASSUMPTION]**: it needs a
diag-blob run over the 58 preserved-from-prior WINs before a `full` send, not
after. Michael's call whether Monday goes out `test` first.

**Corrections made this session, by name:**
- I told Michael #253 fixed "the post-patch pass". Both passes were timing
  out. Same root cause, so #253 should clear both — unverified until a fire.
- I concluded `_merge_prior_win_into` could not create the no-primary-ref
  shape because `mdolx_ref` is in `_PRIOR_WIN_EVIDENCE`. True only for a
  healthy prior win: the fill loop skips a FALSY value, so a prior ref of `""`
  originates it, and an already-shaped row propagates. Both measured.
- My first buffering measurement was invalid (ambient `PYTHONUNBUFFERED=1`).
- My first version of the killed-step test asserted on a string that also
  appears in `run_step`'s `cmd:` banner, so it passed with the fix removed —
  it certified nothing. Caught by mutation-testing every new test.

**Not shipped, deliberately, and why:**
- No parser extension for QC-057's three subjects. "Rates to a few
  destinations for a study" is a genuinely multi-destination RFQ (Rotterdam,
  Shanghai, Nhava Sheva, Tokyo — OL quoted it) that the one-destination-
  per-row schema cannot hold, and a body scan recovers only 3 of 4. The other
  two are unmeasurable until the fixed diagnostic runs once. Inventing a lane
  is the failure QC-057 exists to catch.
- No `intake_acknowledged.json` entry. That is an operator's signature.
- No QC-019 severity downgrade, and no widening of `patch_carriers` to write
  carriers onto WIN rows — that is the machinery that twice manufactured
  attribution (the 2026-08-31 `NAM` substring match, the 2026-08-12 phantom
  quotes).
- `qc_selfheal` still does not pass `send_signal_events` while `ingest` does.
  It is inert (nothing in `scripts/` writes the field) and line ~1587 writes
  `r["has_send"] = decision.has_send` back onto the row, so passing it would
  stamp an evidence field from a derived one. Recorded, not fixed.

**Found in passing, not in scope, not fixed:** `refresh_stage` spends 22m41s
of the 35-minute fire sweeping 9,569 messages to keep 186; `sentry_seer.py`
gets a hard `400 Invalid stats_period` every fire and triggers nothing; the
shared mailbox still emits `::error::` after #249 declared it closed.

### 2026-09-04 — the daily "Cron failure" was a QC check costing more than the work it checked

Michael forwarded the Sentry mail again (HILMAR-DAILY-TRACKER-H, "Cron
failure: hilmar-daily-pipeline", seen 16 times) and said: **"you need to work
with sentry and get the alerts yourself."** Done — and the alert was real, but
it was not about the cron.

**The chain, measured end to end:**

| | |
|---|---|
| HILMAR-DAILY-TRACKER-H | "Cron failure", last 11:14:53 UTC. Reason: *"An error check-in was detected"* — NOT a missed check-in, and not a runtime timeout (`max_runtime` 60m vs 35m actual) |
| HILMAR-DAILY-TRACKER-6 | `QC self-heal (post-patch) TIMEOUT @ 180s` at 11:12:07, same run, release `aa9924e`. **31 occurrences, substatus regressed**, first seen 2026-05-17 |

`run_pipeline.py` sent the cron check-in with `success=not failures` — the FULL
failure list — while its exit code excluded anything named "QC self-heal". So
the timeout paged a pipeline failure for a pipeline that succeeded and shipped,
and the next fire's ok check-in auto-resolved it. A daily alarm that named the
wrong thing and trained its reader to skip it.

Worse than the alarm: on timeout `run_pipeline` KILLS the subprocess
(rc=124), so the entire post-patch pass — heals, recomputed aggregates,
`qc-result.json` — was discarded every fire, and the report shipped from
pre-patch state. That pass is the one that runs AFTER `patch_carriers`, so it
sees enrichment the first pass cannot.

**ROOT CAUSE, PROFILED — and Sentry Seer had it wrong.** Seer said
`qc_selfheal` reprocesses every historical ROW with no retention window so
runtime grows linearly. Measured on a production-scale fixture:

```
427 rows, no mail cache ...................  2s
+ 4,510 cached bodies + 7,146 stage rows ... 41s

cProfile:  phase_6_rules ................... 50.4s of 50.9s
             reprocess_bodies.reprocess() .. 49.4s
               body_parser.html_to_text x4510  30.2s
               fetch_bodies._parse_all  x4510  18.8s
```

The rows are 2s. **QC-059 was calling `reprocess(write=False)` — re-parsing
every cached body — purely to ASK whether the pre-ingest backfill step had
run.** A check costing as much as the work it checks, in BOTH `qc_selfheal`
passes, on top of the real backfill: three full re-parses of the mail cache
per fire. The cost tracks the MAILBOX, not the row count, which is why it grew
into the timeout.

**THE FIX.** `reprocess()` now stamps every record it writes with
`parser_fingerprint()` — a sha1 of `body_parser.py` + `fetch_bodies.py`, the
two modules whose output IS the cached parse. `cache_staleness()` compares
that stamp: one string per record, no parsing. QC-059 asks the cheap question
and falls through to the unchanged, expensive backfill only when the answer is
"stale".

**Measured, production sequence (backfill step, then QC): 41s → 2s**, all 7
phases and 65 checks intact. No migration needed — the backfill step runs
BEFORE both QC passes, so the first fire stamps the whole cache and QC-059
sees zero stale in the same run.

Source bytes, not a version constant: a constant is one more thing to forget
to bump, and forgetting it is indistinguishable from a fresh cache. Any parser
edit re-stamps on the next fire — over-refresh costs a minute, under-refresh is
the stale-parse break QC-059 exists to catch.

**And the check-in now reports the same verdict as the exit code**, computed
from one list so they cannot drift apart again. The best-effort failure is not
silenced — it still raises `pipeline.step_failure` to Sentry and still prints
in the summary. Only the claim the CRON makes about the run as a whole changed.

Deliberately NOT shipped alone: fixing the check-in without fixing QC-059
would have silenced the alert while the post-patch pass was still being killed
and discarded every fire — removing the signal without fixing the defect.

**One hypothesis I tested and threw away.** Before profiling I guessed the cost
was `_load_bodies_index` being called at four sites without memoisation.
Implemented it, measured 30s → 30s, and reverted it. Shipping a no-op with a
docstring claiming it fixed the timeout is exactly the unverified claim this
repo keeps paying for. The profiler found the real answer in one run.

Also caught while measuring: a cleanup `rm` executed in the wrong directory
deleted my fixture and produced a false "1s — fixed!" result. Spotted because
the check count dropped 65 → 63.

Still open, recorded not fixed: `reports/run-log.txt` is never written on the
Actions path, so the run-artifact upload has been empty every fire ("No files
were found") — which is why this went 31 fires undiagnosed. And two live QC
issues: HILMAR-DAILY-TRACKER-C (QC-057, 3/387 staged Lonny RFQs silently
dropped by ingest) and HILMAR-DAILY-TRACKER-3 (QC-019, a status change with no
carrier).

3720 passed, 1 skipped; ruff clean; src/hilmar coverage 91.11% (gate 90%).

### 2026-09-03 — the QC-069 heal: 4b and 4c, approved and shipped

Michael, on the measurement: **"fix 4b and 4c both approved"**.

`ingest.claim_corrected_mdolx_refs` — a booking ref an operator correction
NAMES belongs to that row alone. For every other row holding it, the ref is
DEMOTED into a new `mdolx_refs_seen` field, and then:

  4a  the row keeps a ref of its own          → nothing else changes
  4b  the row is chain-less (`stand_`/`ol_`)  → absorbed into the owner and
                                                REMOVED; it existed only for
                                                this booking
  4c  the row has a real RFQ thread           → kept, and `age_requests`
                                                re-derives its status off WIN

**Demoted, not deleted, and that is the whole design.** `mdolx_refs_all` has
two kinds of reader that want opposite things: `core.booking_count` COUNTS it
(so a ref on two rows is counted twice) and `patch_carriers` JOINS on it to
find the booking PDF (:701), the rate response (:209) and the carrier (:533).
That PDF supplies `pod`, and PASS 2b recovers `destination`/`lane` from it — so
deleting the ref outright fixes the count and severs the join, the row falls to
"Lane unresolved", and `gen_client_email._lane_resolved` drops it from every
client bucket. Lonny told one FEWER booking for a shipment OL confirmed, with
`is_confirmed_win` still True, QC-049 silent and QC-069 quiet. `mdolx_refs_seen`
is read by the three enrichment lookups and by nothing that counts.

Rehearsed over a fixture reproducing all eleven live findings:

```
                          BEFORE   AFTER
  rows                        17      12
  QC-069 duplicates           11       0
  bookings counted            22      11
  teu_won total               27      15
  WIN rows                    17      11
```

The −12 TEU is two effects, both corrections: −8 is `req_f942b9672ff756ab`
leaving WIN (a booking it never owned; `_clear_win_evidence_on_exit` zeroes
volume on that edge), and −4 is two shipments that were stored at 2 TEU on
BOTH their rows and are now stored once.

**TWO DEFECTS FOUND IN MY OWN CHANGE BY REHEARSING IT, not by a test:**

1. `teu_won` was missing from the absorbable set. Several owner rows carry
   `teu_won=None` while the orphan beside them holds the volume — the orphan
   was built FROM the confirmation, which names the containers. Absorbing
   without it meant REMOVING the orphan silently deleted booked TEU: the
   client-report under-count the design exists to avoid, committed by the fix
   for it.
2. Adding `teu_won` then introduced a worse one. Absorption ran from EVERY
   loser, so the 4c owner took `req_f942b9672ff756ab`'s own 8 TEU —
   fabricating volume on a confirmed win. A `stand_`/`ol_` row IS the booking
   and its evidence belongs to the owner; an ordinary `req_` row is a
   DIFFERENT ask the matcher merely stamped. Absorption is now gated on
   `core.has_no_rfq_chain`; only the ref moves off a real request row.

Neither was caught by the 25 tests then passing. Both now have tests, verified
to fail against the defective version.

**THEN AN ADVERSARIAL PASS OVER THE REAL DIFF FOUND THREE MORE**, two of them
blocking, all three confirmed by executing the code rather than reading it:

3. **The re-derive reversed operator verdicts.** The first version called
   `age_requests(all_requests)` after the claim. `age_requests` has NO
   `manual_locked` guard and `apply_operator_corrections` is documented as
   "applied LAST so they win over every automatic classification" — so an
   unrestricted call after the operator layer undoes human verdicts. Measured:
   a correction setting LOSS/SEND_NO_BOOKING on a fresh send-signal came back
   PENDING/AWAITING_MDOLX. `age_requests` now takes `only=`, and the claim
   returns the rows it emptied so exactly those are re-derived. Medians stay
   computed from the FULL list — a subset would skew the PRICE classifier.
4. **Demoting one ref stranded a row's OTHER real booking.** `_demote_ref`
   cleared `mdolx_ref` without promoting a survivor out of `mdolx_refs_all`.
   A row holding 261031 (disputed) and 261099 (real) came out
   `mdolx_ref=None`, and `decide_status` — which reads only the primary, as
   does qc_selfheal's decide loop at :1540 — returned LOSS/SEND_NO_BOOKING.
   The surviving booking silently stops being a win while `is_confirmed_win`
   (which reads the union) stays True, so nothing notices.
5. **The QC backstop path had neither logging nor the re-derive.** It called a
   function that REMOVES WIN rows and assigned the count to a variable nothing
   read. `run_pipeline.py --skip-ingest` makes that path the ONLY one that
   runs. Now `log.fix` + a Sentry metric + the same `only=` re-derive —
   `log.fix`, never `log.ok`, because `Log.ok()` only prints and never reaches
   `qc-result.json`.

And where two corrections disagree — one assigning a booking to row A, another
locking row B's verdict — the claim does NOT pick. It leaves the human's row
alone and emits `::warning::` naming both, because the contradiction is in
`operator_corrections.json` and only a human can settle it.

Five defects, none caught by the tests passing at the time each was found.
Every one now has a test verified to fail against the defective version.

Guards, each earned: the owner must actually HOLD the ref (a correction naming
a row that does not carry the number is not this collision); the owner is never
emptied; a ref no correction names is untouched (QC-069 case 3, genuinely
ambiguous, no verdict to defer to); `exclude` corrections are not claims;
`_demote_ref` always writes lists, because `ingest.py:1054` does
`set(.get(k, []) + [mdolx])` and TypeErrors on None. Idempotent — a demoted ref
no longer counts as a claim, which matters because this runs at intake and
twice more inside `qc_selfheal`, every fire, while the matcher recreates the
duplicate every fire. The 4a idempotence test is the load-bearing one: 4b's
loser is removed, so a second pass there is trivially safe even when the
predicate is wrong.

`main()` re-runs `age_requests` after the claim: corrections apply AFTER aging,
so a 4c row would otherwise publish as a ref-less WIN for a whole fire — QC-049
errors on it, and it is the "worse than the duplicate it replaced" state.

QC-INDEX's QC-069 row updated from "Detect-only" to name the exception, the
demotion, and why the PDF join must survive; `tests/test_qc_governance.py`
binds the pair.

3707 passed, 1 skipped; ruff clean; src/hilmar coverage 91.11% (gate 90%).

### 2026-09-03 — QC-069's 11 duplicates are one bug, and it is not the one I named

I reported to Michael last session that the MDOLX261026-261046 duplicates
came from eight near-identical booking confirmations tying in
`_pick_best_request`'s carrier+container scoring, and asked him whether the
emails carried a stronger disambiguating signal. He said: *"check your mcp
connector to my ol email and look yourself."*

**The connector cannot reach that mailbox** — it is signed into
`michael.deitchman@idealx.us`, and `ol-usa.com` is a different tenant
(`ErrorInvalidUser`, Graph 404). That mailbox holds the tracker's own output,
not OL's confirmations. Read the staged mail instead, which is the better
source anyway: the matcher can only use what is staged.

**My root cause was wrong.** MEASURED (diag-bookings `33783443620`,
diag-blob `33784550128`), all ELEVEN live findings are one mechanism:

```
 5  (4a) OPERATOR CORRECTION vs MATCHER, stale list entry
 5  (4b) OPERATOR CORRECTION vs MATCHER, orphan standalone
 1  (4c) OPERATOR CORRECTION vs MATCHER, rival mdolx_ref
```

Zero carry-forward. Zero lane-mismatch standalone — the shape QC-069's own
docstring leads with, and the one a heal would have been written against.

Eight corrections back-entered the batch from Linda Echevarria's Aug-12
recap, each noting *"The confirmation never reached [this mailbox]"*. True
when written, false now: the confirmations arrived **2026-08-13 20:04-20:21**,
one day after the recap. So every fire runs both writers over the same refs —
`link_bookings_to_requests` writes `mdolx_ref` AND appends to
`mdolx_refs_all` on the row IT scored best; `apply_operator_corrections` does
`row.update(changes)`, overwriting `mdolx_ref` on the row the OPERATOR named
and touching `mdolx_refs_all` not at all. They do not name the same row.
Nothing un-stamps the matcher's copy: the standing corollary, in one field.

**Shipped the measurement; the heal was designed, adversarially verified, and
NOT shipped** (#251). Four independent passes over the proposed 4a heal — "a
ref an operator correction names may appear on exactly one row; strip the
stale copy where the row retains another ref" — and all four refuted it. Two
were decisive, and both were verified here by execution rather than on report:

1. **The instrument that scoped it could not tell the shapes apart.**
   `_classify` was a priority chain: it computed the standalone and rival
   rows, returned on the 4a branch BEFORE testing either, and the label named
   neither. Ran it on three materially different row-sets — bare 4a; 4a WITH
   a `stand_` row also claiming the ref; 4a WITH a rival `req_` row — and all
   three returned one identical string. So "5 cases of 4a", reported this
   morning, was not a claim the tool could support. That is the exact failure
   CLAUDE.md's measure rule exists to prevent, committed by the instrument
   built to prevent it.

2. **`mdolx_refs_all` is a PDF join key, not only a counter.**
   `patch_carriers.py:701-704` joins on it to find a booking PDF; the PDF
   supplies `pod`, and PASS 2b recovers `destination`/`lane`. A row whose lane
   comes that way loses it when the entry is cleared, and
   `gen_client_email._lane_resolved` then drops the row from every client
   bucket — Lonny told one FEWER booking for a shipment OL confirmed, with
   every guard the heal leaned on still green. Same class as 2026-08-10 ("you
   sent lonny we won no shipment last week"), against QC-065-locked
   renderers.

Also measured: `core.booking_count` counts the UNION of both fields and is
numerator AND denominator of every win and request count, so the heal is not
count-neutral; TEU (summed per row) does not move.

`_classify` now reports the SET of shapes and marks each finding
`[EXCLUSIVELY 4a]` or `[NOT exclusively 4a]` — the only thing a heal may gate
on. AUTHORITATIVE COUNT, diag-blob run 33788252407:

```
 5  (4) 4a EXCLUSIVE — a field clear resolves it
 5  (4) 4b NOT exclusive — needs a row removed or a ref blanked
 1  (4) 4c NOT exclusive — needs a row removed or a ref blanked
```

**5 of 11, not the 10 the first tally implied.** 4b removes a WIN row; 4c
blanks `req_f942b9672ff756ab`'s only booking ref (`teu_won=8`). Both are
destructive and wait on Michael.

Four things this session got wrong and corrected on the record:

- The first live run put MDOLX261031 in bucket `(3) genuinely ambiguous; no
  heal should touch it`. That is a licence to leave a real defect alone, and
  it was a mislabel — a correction names one of the two rows. It became 4c.
- The first draft of the classifier used a bare `startswith("stand_")`.
  `test_no_rfq_chain_predicate.py` failed it, correctly: the 49 rows
  backfilled from OL's export are `ol_`-prefixed and equally chain-less, so
  the bare check would have reported that shape as UNCLASSIFIED. Now asks
  `core.has_no_rfq_chain`, with a test for the `ol_` shape.
- `.github/workflows/diag-blob.yml` ran the step as `... || true` — shipped
  in #250 two days ago, and the exact pattern CLAUDE.md names as the
  2026-08-20 defect. The script already emits `::error::` and returns 2;
  `|| true` threw the exit code away. Removed; `if: always()` kept.
- Reported that step as hanging for 13 minutes. It ran in 68 seconds.
  GitHub's jobs API held it at `in_progress` for ~12 minutes after it
  finished and the log endpoint 404'd over the same window. Nothing was
  broken, and the conclusion was drawn from a stale API rather than measured.

Recorded, not fixed, and needs its own change: `scripts/core.decide_status`
and `src/hilmar/core.decide_status` DISAGREE on a row whose only ref is in
`mdolx_refs_all` — production LOSS, library WIN (`src/hilmar/core.py:1381`
has `or bool(mdolx_refs_all)`; the production signature has no such
parameter). Both library callers pass it; production's three cannot. No case
in `test_core_parity.py` passes `mdolx_refs_all`, so the parity suite is
green over the drift, and `_LEGACY_SRC_CONTRACT` is scoped to `body_parser`
and says nothing about it. Undeclared drift is what that block exists to
catch.

Recorded, not fixed: MDOLX261031's thread carries an 08-27 *"Export Invoice
available"* message that `collect_bookings` admits alongside the 08-13
confirmation, so the booking `_pick_best_request` scores against can be dated
LATER than the booking — which is how a row dated 2026-08-26 got past the
`req_ts > bk_ts` guard. Separate change, separate blast radius.

3673 passed, 1 skipped; ruff clean. No production behaviour changed.

### 2026-09-03 — the "we are losing mail" finding was wrong; the alarm was

Yesterday's entry led with *"we are losing booking mail, quietly, every
fire"* and built a whole folder-enumeration fallback on top of one log line:

```
##[error] refresh_stage: [MBD_OceanExportBookingShared@ol-usa.com] date sweep
FAILED: ... Default folder AllItems not found. — falling back to $search
only, which is known to drop recent mail
```

**It was not a data gap.** `CHANGELOG.md`, 2026-08-14: *"THE SHARED MAILBOX
IS CLOSED, PERMANENTLY. Full Access was the only route... Michael: 'ol won't
grant more access.'"* `refresh_stage.SHARED_MAILBOX`'s own comment says the
same and names why coverage still holds: Michael is on the ops distribution,
so Hilmar mail reaches `/me` regardless. Both were already in the file the
folder-sweep PR edited.

Ran `scripts/diag_shared_mailbox.py` against the live mailbox before merging
- CLAUDE.md's own rule, "measure before you write it up" - and it settled
the question the code review had already raised: every endpoint 404s, not
only the one being replaced.

```
[1] directory object      : PASS
[2] folder list           : FAIL 404 — Default folder Root not found.
[3] inbox folder read     : FAIL 404 — Default folder Inbox not found.
[5] mailbox-wide /messages: FAIL 404 — Default folder AllItems not found.
```

`Root not found` is not survivable by any Graph endpoint - a folder-scoped
read fails exactly like the mailbox-wide one. The reviewed, mutation-tested,
17-test folder-enumeration fix (recursive folders, `internetMessageId`
dedupe, date-budgeted truncation, per-folder `::error::`) was correct and
untriggerable on the only mailbox that needed it. **Deleted rather than
merged** - untriggerable code claiming completeness is how this file got
into trouble in the first place.

#### WHAT ACTUALLY SHIPPED

One line. The `::error::` this session mistook for an open bug now fires
only for a genuinely new failure - a different mailbox, or this one failing
a way that is not the confirmed-dead signature. The known signature (this
mailbox, `AllItems` 404) gets a plain line naming it as known and permanent,
pointing at `SHARED_MAILBOX` for the history, with the fallback and the
`continue` unchanged. `::error::` on a dead end that cannot be fixed trains
the next reader - a future session, or this one on a later day - to go
looking for a fix that does not exist. That is what happened here.

Four tests pin the split: the known signature is quiet and names where the
story lives; anything else stays exactly as loud as before, verified by
slicing the source into its `if`/`else` branches rather than trusting a
single substring search (which is what let the first test-suite's coverage
gap through). The pre-existing `test_a_failed_sweep_is_loud_rather_than_a_
quiet_fallback` and the zero-yield-mailbox tests needed no changes - the
general path is untouched.

3649 passed, 1 skipped; ruff clean; coverage gate green.

**QC-069's 11 duplicate-MDOLX rows are still next.** They are a real,
measured defect (`_restore_prior_win` unions `mdolx_refs_all` with nothing
that ever narrows it, so a booking mismatched in an earlier fire is
immortal, and `core.booking_count` then counts one shipment as two wins) -
unlike this one, which was never a defect.

---

### 2026-08-31 — the blocker was one row, and NO PREAMBLE finally landed

Michael, on the entry directly below this one: *"what do you need with the
secret? what does this have to do with hilmar?"*

**He was right and it was overstated.** The measurement nobody had taken:

| table | entries |
|---|---|
| `core.PORT_LOCODES` | **1** — `{"JPYOK": "Yokohama"}` |
| `core.CARRIER_ALIASES` | 42 |
| `core._TRADE_REGION_MAP` | 81 |
| `body_parser.KNOWN_ORIGINS` | 22 |

`PORT_LOCODES` is **one row**. Consuming `rate_blaster.geo` would delete a
one-line dict in exchange for a private cross-repo dependency on the daily
fire. `REFERENCE_DATA_PROMPT.md` called that an OPEN VIOLATION and led three
PRs (#245-#247) with a `RATE_BLASTER_TOKEN` ask, and `core.py` had been
arguing the other way the whole time, right above the dict:

> SEEDED FROM EVIDENCE, NOT FROM MEMORY. JPYOK is the only entry because it is
> the only code this book has actually produced and the only one the operator
> has confirmed.

with `test_every_locode_value_is_a_real_corpus_port` refusing anything
unconfirmed. The rule exists to stop a 12,000-row port list being duplicated
and drifting; one operator-confirmed row behind a test is not that.

**Michael's call: do not create the secret.** Revisit when `PORT_LOCODES`
starts growing a row per fire, or QC-015 fires on ports the local tables do not
carry. The credential mechanics stay recorded for that day - cross-repo
`actions/checkout`, never a token in a pip URL - because they are right, just
not yet needed. `_TRADE_REGION_MAP` is not replaceable upstream at all: trade
region is a classification `geo_master` does not carry.

#### THE FAILURE WAS NOT THE CONCLUSION, IT WAS THE METHOD

Nobody ran `len()`. The finding was inherited from an audit page and repeated
across three PRs as fact. **Second time in one week** - the KOBE lane split,
which `title_case_destination` already merged, was the first. So it is now a
rule rather than a resolution, in `CLAUDE.md`:

> MEASURE THE THING BEFORE YOU WRITE IT UP. [...] An inherited claim is not a
> verified one - run it, or label it [ASSUMPTION].

#### NO PREAMBLE, LANDED AT LAST

Michael confirmed the instruction was his (*"oh yes.. all into .md and you
handle"*). It had been written down on 2026-08-24 and pushed to a branch whose
PR had merged two days earlier, so it never reached `main` and no session
reading `CLAUDE.md` ever saw it. Now in HOW TO TALK TO ME, with the confidence
tags explicitly surviving it: a tag is precision, not hedging, and brevity
never buys an unverified claim.

**Upstream is still owed these three bullets.** The working standard is
duplicated into each repo from `IdealX-dev/idealx-claude-standards` ->
`user-claude-md/CLAUDE.md`; they landed here only, so the next sync would
overwrite them.

`REFERENCE_DATA_PROMPT.md` and `HANDOFF.md` both rewritten to lead with the
measurement instead of the ask.

3645 passed, 1 skipped; ruff clean.

---

### 2026-08-31 — the reference-data blocker is one missing secret, not a design question

`REFERENCE_DATA_PROMPT.md` carried a probe snapshot from 2026-08-28 that is no
longer true, and it was stale in the direction that keeps work from happening:
it recorded `carrier_registry` as LOCAL ONLY and the probe itself as living on
a branch. **Re-ran the probe** against `rate-blaster` `main` @ `6b8f8b57`:

```
[SHAREABLE] geo_master.db        seaports 11928  airports 7883
                                 icds 12949      rail_yards 9177
                                 is_major: seaports 136, rail_yards 112, icds 117
[SHAREABLE] carrier_registry     53 carriers (28 ocean, 25 air)
[SHAREABLE] turso carrier_codes  publisher present
EXIT=0
```

`EXIT=0`. Nothing upstream is held back any more. The block recorded against
`core.CARRIER_ALIASES` — *"staying that way until `carrier_registry` reaches
`rate-blaster` `main`"* — has cleared.

#### THE ACTUAL BLOCKER, MEASURED

`IdealX-dev/rate-blaster` is **private** (`"private": true`, GitHub API), and
this repo's runner cannot read it. Every workflow checks out with
`actions/checkout@v5` and no `repository:` override; the only GitHub token in
play is `${{ github.token }}`, scoped to `IdealX-dev/hilmar-daily-routine`
alone. The nine `secrets.*` this repo uses are Graph, Sentry, Azure, Anthropic,
Teams and QT credentials — **none is a GitHub PAT.**

So `pip install "git+https://github.com/IdealX-dev/rate-blaster.git@main"`
installs fine in a session with the repo attached and **fails on the runner
that fires the daily report**. That is the entire gap.

Unblocked by one fine-grained PAT (read-only Contents on rate-blaster) stored
as `RATE_BLASTER_TOKEN`. Creating it is an access change, so it is Michael's.

Size is not a reason to hesitate — pip's own docs (topics/vcs-support):
*"Pip defaults to partial clones for Git 2.17 or later."* It fetches the tree
at the named ref, where `geo_master.db` is 6.2 MB; it does not pull
rate-blaster's ~176 MB of history.

#### WHY NOTHING WAS WRITTEN AGAINST IT YET

`core.PORT_LOCODES` / `resolve_locode()` stay an open violation for now, on
purpose. A consumption path that CI cannot install is worse than the violation
it replaces: it would ship code that never executes in production and tests
that skip. The design decision is settled (consume the package; do not vendor
an extract — Michael, 2026-08-30); only the credential is outstanding.

Also recorded: rule 5 moved from NOT YET ASSESSED to assessed, in #241.
### 2026-08-31 — QC-083 stops reporting and starts absorbing

It shipped **DETECT-ONLY on 2026-08-28** and the days since are the whole
justification. From that entry, verbatim: *"the heal gets written
against a real list rather than a hypothesis."* The list arrived.

**The 2026-08-31 fire named exactly two pairs**, both HCMC:

```
req_9e919aa59f6f6bfe  <-  req_913dc883fba91890  (MDOLX260712)
req_34213cc401395756  <-  req_e54685b379d8c950  (MDOLX261072)
```

Two, not twenty. That is what a heal can be sized against, and it is why the
absorb is now written.

#### WHY IT WAITED

Absorbing a superseded re-ask **deletes a LOSS**. A detector wrong in that
direction manufactures a win rate — the exact failure this repo has already
shipped once (the STRICT-vs-LEGACY filter that produced a 100% win rate and
survived because both sides of the comparison were equally wrong). Nothing
local could measure the blast radius: the data is in blob and nothing
meaningful runs locally. So it named rows and waited for a fire to answer.

#### WHAT THE ABSORB DOES

The duplicate's `source_imids` and `source_ids` fold into the booked row
FIRST, and a `merge_notes` line records what went and why, so the deletion is
reversible by hand. The `FIX` line names both request_ids and the MDOLX — for
a deleted row that log line is the only trace left, so it has to carry enough
to undo by hand.

Three guards, each with a test that fails when it is removed:

- **A human verdict outranks the heal.** A `manual_locked` row — one an
  operator correction already pinned — is NEVER absorbed. The conflict is
  WARNed so it is visible, rather than silently resolved in the code's favour.
  `operator_corrections.json` is the only durable human state in a system that
  rebuilds every row each fire; a row Michael pinned is a row he looked at.
- **It scans what phase 4 left standing**, not the original `data["requests"]`.
  This was a live bug the moment the check stopped being read-only: pass 1
  collapses exact `request_id` collisions and the discarded twin stayed in the
  scanned list, so it could be elected as the surviving "booked" row — and the
  absorbed row's evidence would have been folded into a dict that phase 4 then
  filtered out. One row gone, its thread gone, nothing to show for it.
  `test_the_survivor_is_a_row_that_survives_phase_4` pins it.
- **Idempotent across the two `qc_selfheal` passes per fire.** The second pass
  sees a group of one and does nothing — no duplicate `merge_notes`, no second
  FIX line.

Everything QC-083 must NOT fire on is unchanged and still tested: two genuine
sailings in one thread, a stale row carrying its own `has_send`, two distinct
MDOLX refs, a missing `etd_requested`, a different thread, an unresolved
(`"unknown"`) lane, a same-day pair (pass 2 owns those), different container
lines, a lone row.

Prose corrected in the same commit, because both said the opposite: the
QC-083 header comment in `scripts/qc_selfheal.py` (*"DETECT-ONLY, ON
PURPOSE"* / *"WHY IT ONLY REPORTS"*), the `reports/QC-INDEX.md` row
(*"DETECT-ONLY, deliberately"*), and the test module docstring (*"THIS CHECK
ONLY REPORTS"*). A doc that describes the previous behaviour is worse than no
doc — the next session reads it and trusts it.

`3645 passed, 1 skipped`; ruff clean; coverage 91.11% against the 90% gate.

---

### 2026-08-31 (later still) — the KPI tiles and STATUS CHANGES mean two different days

Michael, on the delivered Aug 28 report: *"how are there zero losses or
changes in the friday kpi cards if you show four as status changes to lost"*.

**Both numbers were right.** The row of five KPI tiles mixes two definitions of
"that day", and `_today_summary`'s own docstring already says so:

| tile | dated by |
|---|---|
| WON | **event** — booked that day, any request date |
| REQUESTS / Q&L / NQ / PENDING | **intake** — current status of the requests that came in that day |

STATUS CHANGES is event-dated like WON. On Aug 28 it listed four rows aging
`PENDING HILMAR → Q&L`, **every one requested Aug 27**, while Friday's intake
was zero rows — so all four intake tiles correctly read 0.

The losses are not missing. They sit in **Thursday's** tiles, because Thursday
is when they were requested, and because the tile shows CURRENT status
Thursday's `PENDING` reads 4 in Thursday's report while its `Q&L` would read 4
if regenerated now.

#### THIS IS #232 SURFACING, NOT BREAKING

Before aging transitions were stamped with the deadline they crossed, they
carried the pipeline clock and never landed in the report-day window at all.
STATUS CHANGES would have been EMPTY here and the zeros would have looked
unremarkable. The transitions now appear on the day they happened, which put
them next to intake-dated tiles that disagree.

#### THE OBVIOUS FIX IS THE WRONG ONE

Event-dating Q&L / NQ / PENDING to match WON would break the reconciliation
Michael asked for — *"it should all tie out to requests"*. Those four tiles are
built to sum against the REQUESTS tile; a row requested Thursday and lost
Friday would land in Friday's losses but not Friday's requests, and the bucket
sum would exceed the total again. That identity is what the tiles are FOR.

So the section states its own semantics instead — exactly what
`_pending_as_of_note` already does for the live PENDING lists, which was the
remedy for the same defect class on 2026-08-21 (*"pending hilmar two sections
different number of open"*). A section describing a different moment from the
box around it has to say so, or the reader reconciles it by hand and files a
bug against arithmetic that is correct.

`_status_change_daynote` renders under the STATUS CHANGES header:

> Moves that happened on the report day — **All 4** were requested on an
> earlier day. The tiles below bucket by REQUEST date, so they count in the
> tiles for the day they were requested, not for today.

Two Copilot findings were fixed before merge, both verified by execution.
**The note dated a row by `request_date`/`date` only** — while `_today_events`
resolves `request_date or request_timestamp` — so a row requested ON the
report day carrying just a timestamp printed *"It was requested on an earlier
day"*, which is false. It now resolves the same way and stays silent when any
row cannot be dated at all. **And the wording carried curly apostrophes**,
which the Word/Outlook HTML engine mojibakes; straight quotes were not the fix
either, since they close the single-quoted f-strings the note is built from.
Reworded past the possessive entirely, and a test asserts neither kind of
apostrophe survives.

It discriminates rather than decorating: a same-day move gets the opposite
note, a mixed day reports "2 of 3", and it stays silent when there is nothing
to say or when the caller cannot supply a report day — saying nothing beats
saying something unverifiable.

13 tests, including singular/plural agreement at every count (a report that
says "All 1 were" gets trusted less than it should) and a wiring test that
fails if the helper is computed but never rendered — the half-fix that would
otherwise pass every unit test in the file.

### 2026-08-31 (Monday fire) — one port is one lane, however it was spelled

`aggregate_lanes` and `compute_lane_winning_medians` keyed on the raw
"Oakland → X" DISPLAY string. The note above `core.PORT_LOCODES` named that as
the reason the Yokohama split starved its own winning median — and #230 fixed
the JPYOK spelling at PARSE time and left the keying alone. **The cause
outlived the symptom.**

#### THE LIVE PAIR, FROM QC-083's FIRST REAL FIRE

Monday's fire — the first scheduled one since 08-27, restored by #239 — gave
QC-083 its first real output. Two pairs, and the second is a lane-spelling
split in production data:

```
req_34213cc401395756  superseded re-ask of  req_e54685b379d8c950 (MDOLX261072)
    Oakland → HCMC (Cat Lai)        vs        Oakland → HCMC
```

#### THE MERGE IS AN OPERATOR RULING, NOT AN INFERENCE

I stopped this change once, mid-build, and said why: `canonical_port_key`'s own
docstring calls itself *"a MATCHING key, not a display value"*, built for
booking→request linking. Reusing it to bucket a REPORTING aggregate merges Cat
Lai and Cai Mep's rates, and those terminals are ~50km apart — a pricing
decision, not a code decision.

Michael, 2026-08-31: *"no they are all hcmc with two different terminal
requests in ho chi minh"*. One lane. `_PORT_ALIASES` already said so for
matching (*"Lonny asks for 'HCMC'; OL confirms whichever terminal the vessel
calls"*); this confirms it for pricing.

**And the example I first justified this with was wrong.** I told Michael six
operator corrections pinning `KOBE` were splitting the Kobe lane. They are
not: `title_case_destination('Kobe')` returns `'KOBE'`, so the parser already
merges that pair — I had repeated the audit's claim without running it. The
real divergences are the seven `canonical_port_key` merges that
`title_case_destination` does not make: HCMC / Cat Lai / Cai Mep / Port Busan
/ the Lat Krabang spellings / Manila N-S.

#### WHAT SHIPPED

`core.canonical_lane_id` routes both lane ends through `canonical_port_key`.
`aggregate_lanes` buckets on it and displays the most common spelling the rows
actually carried — ties broken alphabetically, because deterministic beats
pretty: a display that flipped between fires would make the dashboard and the
PDF disagree about one lane on one day. The dict is still keyed by a display
string, so **no consumer changed**; two entries simply become one.

`compute_lane_winning_medians` buckets canonically and emits the merged median
**under every spelling that fed the bucket**, so a raw lookup still hits and no
caller had to change. The canonical key is deliberately NOT emitted — it is an
internal bucket id, and putting it in a returned dict would change an
observable contract for no caller that needs it. `decide_status` looks up raw
first, canonical as a fallback.

That asymmetry is the trap this change had to avoid: bucketing one side
canonically and looking the other up raw returns `None` silently, which reads
as "no lane history" and drops every Q&L on the lane from PRICE to
UNDIFFERENTIATED — the same wrong answer by a new route. A test drives a real
`decide_status` call end to end rather than trusting the two sides agree.

#### WHY IT MATTERS BEYOND TIDINESS

`PRICE_GAP_MIN_LANE_WINS` is 3. Four wins split 2/2 across two spellings
produce **no median at all**, and every Q&L on that lane falls to
UNDIFFERENTIATED — "we lost, the data doesn't tell us why" — on the lane group
Hilmar ships most.

#### THE OLD TEST, REWRITTEN RATHER THAN DELETED

`test_locode_split_would_fragment_the_lane_rollup` asserted `len(split) == 2`
— it existed to DEMONSTRATE the damage. Both layers now merge that pair, so
the pre-condition can no longer be constructed with it. That is the
improvement, not a lost guard: it now asserts the raw un-normalised spelling
merges too (defence in depth), keeps the median assertion, and adds a
Yokohama-vs-Tokyo case so the bucketing cannot pass by collapsing everything.

41 tests across both trees. **Half assert things must stay SEPARATE** — a
bucketer that merged everything would satisfy every merge test ever written.
Reverting the bucketing fails 11.

### 2026-08-31 (later) — rule 5 was not nil here: re-LAX-ed, and an incoterm read as a port

I recorded rule 5 as **"NOT ASSESSED — ocean-only, exposure likely nil"** on
2026-08-30. That was wrong, and "likely" was doing the work. The compliance
audit found 11 items; I reproduced the two that matter by executing the
production module.

#### THE ORIGIN SCAN WAS AN UNANCHORED `str.find()`

`_KNOWN_ORIGINS` carries the bare three-letter forms `"SLC"`, `"OAK"` and
`"LAX"`, and `_scan_for_origin` looked for them as substrings:

```
parse_subject_lane('Relaxed cutoff to Tokyo')     -> ('LAX', 'Tokyo')
parse_subject_lane('Flaxseed shipment to Busan')  -> ('LAX', 'Busan')
```

re-**LAX**-ed. f-**LAX**-seed. The origin is a lane endpoint, so a bogus one
splits the lane bucket and mis-labels the carrier scoreboard — the *exact*
damage that got `"HILMAR"` removed from this same list, recorded in the
comment directly above it.

Word boundaries now. Note this does **not** cost us `Oakland`: the long form
sits ahead of the short one in the list and still matches at its own index,
while bare `OAK` correctly stops matching inside it.

#### AN INCOTERM SITS EXACTLY WHERE A PORT SITS

```
parse_subject_lane('Updated Rates FOB Korea from Dalhart') -> ('Dalhart', 'FOB')
parse_subject_lane('Rates CPT Japan from Tulare')          -> ('Tulare', 'CPT')
```

The `<DEST> <region> from <ORIGIN>` branch pops a trailing region word and
takes whatever capitalised token sits behind it. For `"FOB Korea"` the pop
makes it **worse**: it strips `Korea` and hands back the incoterm.

`_INCOTERMS` (15) and `_UNIT_TOKENS` (16) now join `_LANE_STOPWORDS` at both
endpoint-acceptance sites. The contract's framing is about POSITION — a
3-letter token after a number is a unit, an incoterm before a place name is
the incoterm — and refusing both classes outright is the conservative reading:
this repo has no lane whose endpoint is legitimately spelled `FOB` or `CBM`.

A refused pair falls through rather than returning a half-lane. That is the
honest outcome — we do not know the destination, and `Korea` is a country, not
the port. The row is dropped and QC surfaces the gap, instead of a fabricated
lane key entering the aggregates.

#### MOSTLY POSITIVE TESTS, ON PURPOSE

35 tests across BOTH trees. Half assert the parser still works — a parser that
returned `(None, None)` for everything would pass every negative test in the
file. The 2026-06-24 Busan/Korea recovery shape, the ordinary `X to Y` lane,
and the QC-057 destination recovery are all pinned. Both fixes reverted in the
production tree: **11 tests fail**.

### 2026-08-31 — the daily fire was assigning CMA CGM to every Vietnam and Panama lane

A 244-agent compliance audit against the shared reference-data contract
(6 dimensions, 3 independent refuters per finding, 20 upheld of 79). The worst
finding was live in production, and every claim below was confirmed by
EXECUTING the code, not by reading the report.

#### VIET-NAM. PA-NAM-A.

`patch_carriers.py` PASS 4 — the carrier-enrichment step in `run_pipeline` —
carried its own table:

```python
"CMA CGM": ("NAM", "APL", "ANL", "CMA", "CGM"),
```

...matched with a bare `p in up` against the SUBJECT LINE. `NAM` is a real CMA
CGM booking-ref prefix. It is also inside VIET**NAM** and PA**NAM**A. Run on
real Hilmar subject shapes, none of which names a carrier:

```
CMA CGM  <- MDOLX261145_ HILMAR Oakland to Cat Lai, VIETNAM 2x40RF
CMA CGM  <- MDOLX260502_ HILMAR Oakland to Cai Mep, VIETNAM 1x40HC
CMA CGM  <- HILMAR Oakland to Manzanillo, PANAMA 2x40RF
```

**Every Vietnam and Panama lane.** Cai Mep alone was 16 of 134 bookings in
OL's 2026 export — the second-largest lane in the book.

Scope, honestly: the branch only fills a WIN whose `carrier_won` is already
blank, so it never overwrote a known carrier. It **invented** one where the
honest answer was None — precisely the failure the contract names: *a wrong
carrier on a priced row misleads a human in a way a blank never does.*

**And it did not stay in this repo.** `share_intel` exports `carrier_summary`;
`sync_to_quote_tracker` runs in the same fire and upserts those names into the
Turso `client_intelligence` registry — as canonical vendor entities, with
aliases — the registry rate-blaster also writes.

#### TWO MORE, SAME CLASS, BOTH REPRODUCED

- `backfill_mdolx._carrier_match` — same private table, same substring match.
  `_carrier_match('CMA CGM', '...VIETNAM...')` returned **True**. It gates
  whether an MDOLX is bound to a WIN, and `find_mdolx_for_win`'s own docstring
  says *"false matches are far worse than no match — they corrupt data."*
- `src/hilmar/body_parser._find_carrier` — 14 tokens, bare substring, fed
  `f"{subject}\n{preview}"` by `ingest.py:597`:

```
'Please call my phone for details' -> 'ONE'     # ph-ONE
'stone container'                  -> 'ONE'     # st-ONE
'Booking done, no money yet'       -> 'ONE'     # d-ONE
'ZIMBABWE inland move'             -> 'ZIM'
```

  `qc.py:416` already carried a comment noting this matched by substring and
  hand-rolled its own word-boundary regex to avoid it. The workaround was
  right; the defect belonged here.

#### THE FIX IS THIS REPO'S OWN EXISTING ANSWER

Nothing new invented. `body_parser.detect_carrier_token` (2026-06-15) already
scanned on WORD BOUNDARIES and refused `_AMBIGUOUS_CARRIER_TOKENS` outside a
known carrier cell; `parse_subject_carrier`'s Pattern D already anchored a ref
prefix to its DIGITS. Both were correct. The two older sites never adopted
them.

So that matcher was lifted into `body_parser` and made importable —
`CARRIER_REF_PREFIXES`, `carrier_from_booking_ref`, `carrier_named_in` — and
**both private tables were deleted rather than fixed.** One table, one place.

The anchor is the whole guard: a prefix counts only when DIGITS follow it.
`NAM8322223` is a CMA CGM booking; `VIETNAM` is a country. NAME tokens are now
deliberately absent from the ref table — mixing names into it is what made
`NAM` searchable as prose — and a test asserts they stay out.
`carrier_named_in` asks about ONE carrier rather than returning the first of
many, so a subject naming two carriers cannot confirm the wrong one.

33 tests, most POSITIVE assertions on purpose: a matcher that matches nothing
passes every negative test ever written. All three fixes were reverted in turn
and 11 tests fail against the old behaviour.

#### STILL OPEN FROM THE SAME AUDIT — NOT IN THIS CHANGE

Rule 5 is **not** nil here, contrary to the earlier "not assessed" note:
`parse_subject_lane('Relaxed cutoff to Tokyo')` returns origin `LAX`
(re-**LAX**-ed, via an unanchored `find()` over `_KNOWN_ORIGINS`), and
`'Rates CPT Japan from Tulare'` returns destination `CPT` — an incoterm read
as a port. Separately, `aggregate_lanes` still keys on the raw display string,
so `KOBE` and `Kobe` are two lanes while `same_port` says they are one, and
six operator corrections pin the uppercase form.

### 2026-08-30 — two sessions wrote the same contract; only one copy ships

Michael sent the bare filename `REFERENCE_DATA_PROMPT.md`. It existed nowhere
— not here, not in rate-blaster, not on any reachable ref — so this session
wrote it: the shared reference-data contract, plus a pointer section in
CLAUDE.md.

**Another session had already landed the same contract in CLAUDE.md** (#236,
#237) while this one was writing. Caught before merge, on a `mergeable_state:
behind` from the base moving underneath the branch.

Shipping it as written would have put THREE copies of one rule in this repo:
main's CLAUDE.md block, a full restatement in the new file, and a third
partial restatement in the pointer section — a contract about not keeping two
copies of a thing, kept in three.

So the file was rewritten to hold **only what CLAUDE.md cannot**, and the
duplicate CLAUDE.md section was dropped entirely:

- **What the probe actually returned**, with the date and the commit it was
  run from. CLAUDE.md states the claims; this records the measurement —
  including that `carrier_registry` and `reference_data_status` itself are
  LOCAL ONLY, on `claude/rate-blaster-geo-fetch-0cckio` @ `b527194`, so no
  consumer can use them yet. Labelled as going stale the moment that branch
  merges, with the instruction to re-run rather than trust the page.
- **This repo's compliance status**, a dated finding rather than policy: the
  OPEN violation (`core.PORT_LOCODES` / `resolve_locode`, shipped by this
  session in #230), what is correctly blocked, what is clean on rule 4, what
  is not yet migrated, and — recorded rather than assumed — that rule 5 has
  not been audited here at all.

Corroborated independently against `geo_master.db` on rate-blaster `main`
(`531bd27`): `JPYOK` is name='Port of Yokohama' / city='Yokohama' (rule 1 in
one row), and city='Lagos' returns `NGAPP`, `NGTIN` and `PTLOS` (rule 2 in
three).

The rule now lives in exactly one place. The evidence lives beside it.

### 2026-08-30 — the scheduled fire has not fired since 08-27, and every run reported success

Found while checking what QC-083 turned up in its first real fire. It had not
had one.

#### THE OUTAGE

`#228` moved the crons to 10:30 / 11:30 UTC (6:30 AM ET) and **left the
schedule gate's DST matcher on the old 12 / 13**. The two numbers are the same
fact stored twice, and only one of them moved. Neither cron could then open
the gate:

```
cron='30 11 * * 1-5' hour=11 ET-offset=-0400 → proceed=false
```

Both of Friday 2026-08-28's scheduled runs gated themselves off. `Production
fire` — **skipped**. The whole workflow reported **success**, in seconds,
having sent nothing. The report reached the distribution only because someone
dispatched it by hand at 17:31 UTC.

Green, silent, and not firing — the same shape as the 2026-08-20 `|| true`
diagnostic that died on its first line and passed. This one is worse: it
looked like three healthy runs.

Mine. #228 shipped 2026-08-27; the gate has been closed to every scheduled
fire since.

#### THE FIX, AND THE REAL FIX

The matcher moves to 10 / 11. That is the symptom.

The DEFECT is that a cron that fires and a gate that opens are two facts and
`daily.yml` keeps them in two places. Four tests now DERIVE the matcher from
the cron lines instead of restating either number:

- every scheduled cron hour must be one the gate opens on (the regression —
  fails on the shipped state with `assert not {12, 13}`)
- every matcher hour must have a cron behind it (dead code that makes the gate
  look correct while covering nothing)
- each DST season opens on exactly one hour, and EST is exactly one UTC hour
  after EDT — two openings would fire the pipeline twice a day, zero is this
  outage
- the hour the gate opens on must convert to the intended ET wall-clock in its
  own season, and clear the wee-hours cutoff, so the two halves of this file
  cannot disagree

Both regression tests were re-run against the shipped 12/13 and both fail
there.

#### WHY THE EXISTING CRON TEST DID NOT CATCH IT

`test_cron_respects_the_wee_hours_cutoff` checked that the crons convert to a
safe ET hour. They do — 6:30 AM ET is correct and was never the problem. It
never asked whether anything downstream would act on them. A guard on the
schedule that ignores the gate is half a guard.

### 2026-08-28 (blob dry run) — QC-082 would have paged an ERROR on a healthy row at the next fire

The first real dry run of `migrate_locode_rekey.py` against blob, dispatched
today. It is read-only (`--dry-run` returns above both `write_text` calls),
and it came back with three things worth having.

```
Read 414 rows from tracking-data-v2.json and 84 corrections
  ROW  stand_261031  'Jpyok' -> 'Yokohama'  id -> stand_261031
##[error]ALREADY STALE before this migration: stand_260905 (...)
rows renamed=1 corrections to re-key=0 unverifiable=0 pre-existing stale=1
```

#### 1. THE MIGRATION IS A CONFIRMED NO-OP

**0 corrections to re-key, 0 unverifiable.** Exactly one live row carries the
`Jpyok` spelling — `stand_261031` — and its `request_id` does not move, because
a `stand_*` id is derived from the MDOLX ref rather than from
`core.request_id`'s destination hash. So the merge re-keys nothing and
`--apply` has nothing to do. That is a MEASUREMENT, replacing the
`[ASSUMPTION]` #230 shipped with.

**A correction to #230's framing while I am here:** the "44 of 134 rows /
largest lane by 3x" figure was measured on `data/ol-transaction-report-2026.json`,
OL's export. In the LIVE tracking data it is one row. Both numbers are real
and they are different datasets — the lane-median starvation was demonstrated
on the OL corpus, the production blast radius today is a single row. The
parser fix still matters going forward; the retroactive damage was one row.

#### 2. AND THE THING THE RUN ACTUALLY EARNED ITS KEEP FOR

`stand_260905` was reported as pre-existing staleness — and it is **not
stale**. It carries BOTH of Michael's verdicts:

- a `set` fixing the lane (2026-07-14): *"Oakland → Tokyo ... so it resolves
  permanently every fire"*
- a later `exclude` (2026-08-13): *"260905 260192 260963 were bookings hilmar
  cancelled"*

The exclude drops the row, so the `set` matches nothing **by design**. But
QC-082's exemption asked only whether THAT correction carried the flag, never
whether a SIBLING correction on the same `request_id` excluded the row. So the
`set` read as an orphaned human verdict and **QC-082 — which is
`log.error` — would have paged, on healthy data, every fire, forever.**

That is the QC-081 failure mode exactly, the one held back from #230 for
precisely this reason. QC-082 shipped 2026-08-27 and the next daily fire had
not yet run when the dry run found it, so **it never reached production.**

Fixed in QC-082 and in the migration's `already_stale` bucket together — the
same bug in both, and QC-082's own remediation message tells a human to run
that migration, so the two disagreeing about one row would send someone to a
tool that contradicts the alarm that sent them. Pinned by a test that runs
both against the real `stand_260905` shape.

The exemption is keyed on the **id**, not applied file-wide: a third test
asserts a genuine orphan is still caught when a DIFFERENT id is excluded.
Widening it to "any exclude anywhere" would silence the check entirely.

#### 3. WHY THE STEP SHOWS RED

`--dry-run` exits non-zero when it finds anything to report, so the step goes
RED rather than green-in-zero-seconds. Deliberate, from the 2026-08-20 lesson
where a diagnostic died on its first line and reported success because of a
trailing `|| true`. Red here means "there is something to read", not "it
broke".

### 2026-08-28 (later still) — QC-083: find the duplicate pairs already on disk, and do not delete them

#231 stopped NEW phantom losses. The pairs written by earlier fires are still
in `tracking-data-v2.json`. QC-083 finds them and **reports them** — it does
not absorb them, and that is the decision, not a deferral.

#### WHY REPORT AND NOT HEAL

Absorbing one of these rows deletes a **LOSS**. A detector wrong in that
direction manufactures a win rate, which is a failure this repo has already
shipped once. And nothing here can measure how many real rows match: the data
is in blob and nothing meaningful runs locally. So the check names them, with
their ids, and the heal gets written against a real list instead of a
hypothesis.

#### PHASE 4'S EXISTING PASSES CANNOT SEE THEM — TWICE OVER

Pass 2 keys on `request_date`, and a re-ask is by definition a DIFFERENT day,
so the pair lands in two separate groups. It also fires only on an unconfirmed
**WIN**, and post-#231 the stale copy is a **LOSS**. Neither half matches.

#### THE DISCRIMINATOR IS THE REQUESTED SAILING

Same thread + same lane + same containers **also describes two genuine moves**
Lonny asked for in one thread, one of which lost. Collapsing that erases a
real loss. What separates them is `etd_requested`: one shipment asked twice
names the SAME sailing, two shipments name two. A row missing that field is
skipped entirely — absence is not evidence, and this is the branch where
guessing costs a loss.

Never fires when: the group holds 2+ distinct MDOLX refs (two real bookings,
Michael's 2026-08-24 Tokyo question); the stale row carries its own `has_send`
(an ask Lonny accepted in its own right, so its loss is real); the lane is
unresolved (`canonical_port_key` → `"unknown"`, the same sentinel trap guarded
in `ingest._prior_win_captured`); or the pair is same-day (pass 2 owns that,
and two checks racing to collapse one row is how a heal double-counts).

Ten of the fourteen tests are negative cases, deliberately.

#### A LIMIT WORTH KNOWING

The `has_send` guard depends on the rebuild having run. Pairs already on disk
were promoted by the PRE-#231 matcher and still carry `has_send=True` until a
fire rebuilds them from staged mail. So QC-083 **under-reports** on the first
fire after #231, and on any pair whose RFQ has aged out of the 14-day stage
window. That is the safe direction: a false negative reports nothing; a false
positive nominates a real loss for deletion.

### 2026-08-28 (later) — the reversal that could never arrive: stamp the deadline, not the fire clock

Michael, told that a WIN→LOSS reversal cannot reach any report: *"why not?
what is best way.. figure it out and manage it"*. So: decided and shipped, not
asked.

#### MEASURED FIRST, ON THE PRODUCTION SHAPE

One row — a Monday 14:00 ET Send that OL never books — through four
consecutive fires at 06:30 ET with `window=previous`:

```
fire 08-25 -> reports 08-24 : WIN->PENDING stamped 08-25   (outside the window)
fire 08-26 -> reports 08-25 : WIN->LOSS    stamped 08-26   (outside)
fire 08-27 -> reports 08-26 : WIN->LOSS    stamped 08-27   (outside)
fire 08-28 -> reports 08-27 : WIN->LOSS    stamped 08-28   (outside)
   STATUS CHANGES TODAY renders: NOTHING, every single day
```

**Exactly one day ahead of the window, on every fire, forever.** The promotion
is stamped from LONNY'S EMAIL (`ingest.py:1763`, `at=sent_dt`); the reversal
was stamped from the PIPELINE'S CLOCK (`age_requests`, `at=now`). Production
reports the PRIOR business day, so the fire day is always one day after the
day being reported. And it never catches up, because every fire rebuilds
`status_history` from empty and re-creates the reversal at that morning's
`now`. It is not late. It never arrives.

#### THE FIX — THE ONE THIS REPO ALREADY MADE, IN THE OTHER DIRECTION

`ingest.py`, 2026-08-11, on the prior-build WIN restore: *"DATE THE RESTORE
FROM THE PRIOR EVIDENCE, NEVER FROM NOW."* Same defect, same fix.

Each staleness predicate already computed a deadline internally and threw it
away, returning a bool. Now they return it:

- `core.business_stale_deadline(dt, hours)` ← `is_business_stale`
- `core.pending_hilmar_deadline(resp_dt, *, request_dt=None)` ← `pending_hilmar_stale`
- `core.pending_ol_deadline(request_dt)` ← `pending_ol_stale`

Each predicate is rewritten to CONSUME its own deadline function, so a
deadline and the bool it came from cannot drift. `StatusDecision` gains
`stale_at`, set on every aging LOSS branch — `SEND_NO_BOOKING`,
`NO_RESPONSE`, `NO_RESPONSE_TS`, and the whole Q&L tail (`ETD_MISS`, `PRICE`,
`UNDIFFERENTIATED`, `QUOTED_NOT_BOOKED`), which is one aging event wearing
four labels and so carries one deadline bound once above them all.
`RESPONSE_NO_RATE` is deliberately excluded: it fires on evidence, not on a
clock.

`age_requests` and `qc_selfheal` (both trees) stamp `at=decision.stale_at`.
After the fix, the same row:

```
fire 08-26 -> reports 08-25 : WIN->LOSS stamped 08-25   <- IN the window, renders
fire 08-27 -> reports 08-26 : WIN->LOSS stamped 08-25   (stable; correctly silent)
fire 08-28 -> reports 08-27 : WIN->LOSS stamped 08-25   (stable; correctly silent)
```

Reported once, on the day it happened, then quiet. The stamp is in the past by
construction (stale *means* now is past the deadline), identical on every
later fire, and lands on the business day the window actually closed.

#### THE DST TRAP, LOOKED UP RATHER THAN RECALLED

Per the CPython docs (Context7, this session): adding a timedelta to an aware
datetime *"adjusts the date and time while preserving the original tzinfo
attribute without performing timezone adjustments"*, while subtracting two
aware datetimes normalises both to UTC. So `is_business_stale`, which converts
to ET **before** adding hours, measures WALL-clock time; `pending_hilmar_stale`
and `pending_ol_stale`, which subtract, measure ABSOLUTE time. **The two
already disagree by an hour across a DST change.** That divergence predates
this work and is NOT silently unified here — each deadline function is lifted
from the predicate it belongs to, verbatim.

Proven, not asserted: **108,864 predicate evaluations × both trees**, 1,008
anchors spanning both 2026 DST transitions — **zero** behaviour drift against
the pre-refactor implementations, and zero deadline-vs-predicate disagreements
one second either side of every boundary.

#### WHAT THIS DOES NOT DO

`_is_current_status_change` (2026-08-13) stays. It filters a genuinely LATE
record — the April aging only written down today — and honest dating does not
make it redundant; it makes the two agree instead of fight. Pinned: a same-day
aging renders, an April one does not.

**A second, separate defect surfaced while measuring this and is NOT fixed
here.** `apply_send_signals` promotes a send-signal row to `WIN`
unconditionally, and `age_requests` immediately re-decides it to
`PENDING(AWAITING_MDOLX)` — because `decide_status` (Reading B, Michael
2026-04-27) says a Send with no MDOLX is not a win. So every such row gets a
spurious `PENDING→WIN→PENDING` pair inside a single fire, and that transient
WIN is what wore the green pill. Changing it means changing what a "WIN"
means in the status model, which earns its own PR and its own scrutiny.

### 2026-08-28 — one move asked twice, one booking, and an invented loss on the other copy

Michael, on the Oakland → Tokyo pair: *"that's your job by parsing emails
properly"* — rejecting a "this needs a blob diagnostic" answer. He was right;
the evidence to match on was already staged and the matcher was throwing it
away.

#### THE DEFECT

`apply_send_signals` matched Lonny's "Send" replies to a request by **lane and
recency only**. It never read `in_reply_to`, `references` or
`conversation_id` — which `refresh_stage.build_stage_record` puts on EVERY
staged message (`refresh_stage.py:1105-1110`), and which the other two matchers
already use: `link_bookings_to_requests` scores a header-chain pool
(`ingest.py:983-1027`), `apply_rate_responses` prefers a `conversation_id` hit
(`ingest.py:1374-1404`). Send-signal was the only one of the three that was
blind.

Worse, it **skipped every row already WIN**. So when Lonny asked for one move
on two days, the Send that belonged to the booked row was refused by that row
and fell through to the next-latest open row on the lane — promoting the
duplicate. That row then had no booking, so it aged out
`LOSS / SEND_NO_BOOKING`.

Net result on the live pair: one move, quoted once (Wan Hai $2,884), booked
once on MDOLX261145 — reported as **a win and a loss**, with the loss reason
accusing OL of never confirming a booking OL had confirmed.

Reproduced on `main` before touching anything, on the real row shape:

```
=== A' — the SAME shape on main (the bug) ===
  older req_0825: status=WIN has_send=True
  -> cascaded on main: True   (True = the bug reproduces)
```

#### THE FIX — evidence, in order, spent once

`send_thread_anchors` / `send_reply_is_in_thread`, then resolution order:

1. **thread pool** — rows the reply is provably anchored to. When non-empty it
   is the WHOLE candidate set; a Send that names its thread never lands
   outside it.
2. **booking evidence** — a row whose MDOLX landed at or after the Send
   outranks an open row. That booking IS what the Send bought, so a booked row
   is an eligible target, not a skipped one.
3. **recency** — the old rule, kept, as the tie-break.

Two guards on the obvious over-correction:

- **`_send_consumed`** — a row absorbs at most one Send per fire. Without it
  rule 2 would hand every Send on the lane to the same booked row and a
  genuine double-ask would collapse to one win. With it, a second Send falls
  through to the next-best open row. Proven both ways:

  ```
  === B — two genuine asks must BOTH promote ===
    promotions: 2   req_A has_send=True   req_B has_send=True
  ```

- **chronological iteration** — the loop now sorts by `sent`, not stage-file
  order. `_pick_best_request` was made deterministic by construction for
  exactly this reason (`ingest.py:153-171`); a matcher whose outcome depends
  on the order rows happened to be written in is the same defect wearing a
  different hat.

`conversation_id` is trusted here and nowhere else on purpose. `core.request_id`
declines it as an identity key because Outlook reuses it across identical
subjects — but here that reuse can only ADD the sibling ask to the pool, and
the scoring then picks between them on booking evidence, which is the wanted
behaviour.

#### WHAT IS DELIBERATELY NOT IN THIS CHANGE

**The heal.** This stops NEW phantom losses; it does not absorb the duplicate
rows already in `tracking-data-v2.json`. That is a `qc_selfheal` pass which
REMOVES A ROW FROM LIVE DATA, so it earns its own PR and its own scrutiny
rather than riding along with a parser fix.

**The reversal (Michael's item 1)** — why a WIN→LOSS never reaches any report.
Still open, and to be REWRITTEN rather than ported: the agent-authored version
pages an ERROR-class Sentry alert on healthy rows every fire (reproduced twice
by its own reviewers) and leaves a superseded `PENDING→LOSS` on the report day
when a row recovers.

#### ALSO

`test_two_lane_less_rows_are_not_evidence_of_each_other`, held back from #230
because the `canonical_port_key` `"unknown"`-sentinel routing belongs to this
work, is restored here — with an added positive assertion that a real lane
still matches, so it cannot pass by matching nothing.

### 2026-08-27 (later) — JPYOK is Yokohama, and it was costing the biggest lane in the book

Michael, supplying the fact the code could not: *"JPYOK and Yokohama are samy
JPYOK is the UN LOC code for Yokohama"* — *"makes no sense and for you to fix"*.

Earlier today this was deliberately left Unmapped. That was right at the time:
the code could prove `body_parser._norm` had INVENTED the name (it Title-Cases
any all-caps token over three characters, so `JPYOK` → `Jpyok`) but could not
prove what the code stood for — nothing in the repo knew what a UN/LOCODE was.
Mapping it blind would have split Yokohama forever. With the identity
confirmed by the operator, the merge is the fix.

#### IT WAS NOT COSMETIC — MEASURED, NOT ARGUED

`data/ol-transaction-report-2026.json`: **YOKOHAMA is 44 of 134 bookings**, the
largest lane in the book by 3× (next is CAI MEP at 16). And the split did more
than untidy a table — `compute_lane_winning_medians` needs
`PRICE_GAP_MIN_LANE_WINS = 3` wins on a lane:

    four wins, split 2/2 across the spellings -> {}                     (no median)
    the same four merged                      -> 3150.0

With no winning median, a Q&L loss on that lane cannot be attributed to PRICE
and falls to UNDIFFERENTIATED. So the fake port name was mislabelling **why we
lost on the lane we ship most**, which is the number carrier negotiations run
on.

#### THE FIX

`core.PORT_LOCODES` + `core.resolve_locode`, **table-gated**, seeded with
`JPYOK` alone. A shape rule ("five caps letters is a LOCODE") would eat BUSAN,
OSAKA, TOKYO, GENOA, HAIFA and LAGOS — every one a real port in this corpus,
verified surviving in the tests. An unrecognised code stays raw and surfaces as
an unmapped destination rather than being renamed to a guess.

No other codes are pre-seeded. They could not be verified against the UNECE
list from this runner (egress blocked), and CLAUDE.md forbids guessing into
production. Each is a one-line PR when a fire surfaces it.

Patched at **all three** entry points, because `_norm` was not the only
producer: `body_parser._norm`, `body_parser._rate_table_from_cells` (the POD
cell path, which never called `_norm`), and `ingest.title_case_destination`
(an independent second producer — a subject reaching it as `JPYOK` was renamed
even when the parser had not touched it). Case-insensitive, so rows already on
disk carrying the damaged `Jpyok` spelling resolve too.

#### QC-082 AND THE MIGRATION

`core.request_id` hashes the destination, so renaming one **re-keys the row and
orphans its operator correction** — and `apply_operator_corrections` handles a
miss with a `print(...)` and carries on. A print in a runner log is not an
alarm: the row silently reverts to whatever the parser decided.

- **QC-082** (ERROR, detect-only) catches exactly that: a `set` correction
  matching no row. `create` and `exclude` corrections are exempt — absence is
  their normal state. Deliberately wider than this one migration.
- **`scripts/migrate_locode_rekey.py`** is the scripted, reversible repair:
  `--dry-run` by default, `--apply` records `superseded_request_id` as the
  reverse map, `--revert` undoes it. It refuses to apply while any row's id is
  unreproducible. `.github/workflows/migrate-locode.yml` runs it, defaulting to
  dry-run.

#### PROVENANCE — THIS CODE CAME FROM AGENTS AND WAS NOT TAKEN ON TRUST

A workflow asked for DESIGNS wrote 3,308 lines of implementation into the
working tree instead, covering this plus two unrelated fixes. Its own review
found the other half fatally flawed. So: nothing was merged wholesale. This
branch was rebuilt from `main` taking only the LOCODE pieces —
`body_parser` (both trees) and the new files wholesale, three named hunks of
`core.py`, one hand-written hunk of `ingest.py`, and QC-082 with its wiring —
and every claim above was re-verified by running it. The rest is checkpointed
and unshipped.

Two things were HELD BACK rather than dropped, and both are recorded where the
next session will find them:
- `test_two_lane_less_rows_are_not_evidence_of_each_other` guards
  `_prior_win_captured` against `canonical_port_key`'s `"unknown"` sentinel.
  That routing change belongs to the duplicate-row work, not here; the note
  sits at the foot of `tests/test_locode_migration_and_qc082.py`.
- QC-081 (a derived transition stamped with the fire's clock) is NOT shipped.
  Its implementation pages an ERROR-class Sentry alert on healthy rows every
  fire, reproduced twice by its own reviewers.

Also updated: `test_dashboard_buttons_do_what_they_say` asserted Jpyok must
STAY Unmapped — this morning's deliberate decision, now overruled by the
operator. It asserts the merge instead, and still pins that the six five-letter
real ports survive.

Suite: 3,466 passed / 1 skipped. Coverage 91.18% (gate 90%). ruff clean.
Isolated-import check: 0 failures.

### 2026-08-27 — a green WIN badge on a loss, thirteen buttons that never filtered, and a cron with a cliff under it

Four things Michael found in the live artifacts, all real, none of them the
defect they first looked like.

#### 1. A REVERSED WIN WAS STILL WEARING A GREEN "WIN" BADGE

Michael, on an Oakland → Tokyo row reading "PENDING HILMAR → WIN" beside the
reason "Lonny replied Send — REVERSED, now LOSS (SEND_NO_BOOKING)": *"why is
it still win with no further change to loss"*.

Every other surface already called it a loss — the KPI tile, the footer's
"1 wins", the carrier table, both lane tables, the weekly. The pill was the
only thing disagreeing, because it was built from the TRANSITION's target and
never from the row. `h["to"] == "WIN"` fell past `_pill_colour_key` and
`_pill_text` (which remap only LOSS/Q&L/NQ) and took the good-green palette.
Measured on his row shape: footer "0 wins", `display_status` "Q&L",
`_win_landed` False — and a green WIN badge shipped anyway.

`_win_landed` knew all along. It gates the reason string and the win count,
and was never asked about the pill. Now it is: the TO end renders the row's
real status, so his row reads **PENDING HILMAR → Q&L** in red.

**AND THE SECOND HALF OF HIS QUESTION HAS A WORSE ANSWER.** The reversal did
happen — it just cannot ever appear. Two clocks: the promotion is stamped
with Lonny's send email (`ingest.py`, `at=sent_dt`, the report day), the
reversal with the pipeline's own clock (`at=now`, the fire day).
`_today_events` keeps only report-day history, so WIN→LOSS is outside the
window by construction. And it never catches up: every fire rebuilds
`status_history: []` and re-creates the reversal at that morning's `now`, so
it walks forward with the window forever. It is not late — it never arrives.
Not fixed here; it needs a decision about which clock a derived reversal
should carry, and that is Michael's call, not a silent change to how history
is stamped.

**A COMMENT IS WHY THIS LOOKED CLOSED.** Above `wins_in_day`, the code said
the STATUS CHANGES table "applies it too, so a transition the KPI refuses to
count is never rendered as a win." False about its own file — the table
applied `_win_landed` to the REASON STRING only. Corrected, and now true.

**AND THE TEST PASSED GREEN THROUGHOUT.**
`test_a_reversed_win_is_not_rendered_as_a_win` asserted only that the strings
"REVERSED" and "SEND_NO_BOOKING" appear in the block. Checking the prose says
nothing about the pill, which is the thing a reader looks at. Two tests added
that assert the badge itself; both fail on the old code (verified by planting
it back) and pass on the new.

#### 2. WERE THE TWO TOKYO ROWS ONE SHIPMENT? THE CODE CANNOT SAY — AND HIS TWO BEST CLUES ARE NOT USABLE

Same lane, equipment, TEU, carrier and rate, one day apart. **Same carrier
and same rate is not independent evidence**: the pipeline manufactures that
agreement across days — `ingest.py` backfills `carrier_won` from a same-lane
sibling within 30 days, and `qc_selfheal` copies a carrier from a same-lane,
same-rate-to-the-cent sibling within ±45 days and a response timestamp under
the same fingerprint. Row A's "Wan Hai / $2,884" may be row D's, copied.

[Likely] one shipment, by a named mechanism: booking linking runs before
send-signal matching; the send matcher skips rows already WIN and takes the
latest remaining same-lane candidate within 7 days. D won on MDOLX261145, so
one Tokyo "Send" cascades onto row A, manufacturing its `has_send` and hence
SEND_NO_BOOKING. `ingest.py`'s own comment names this failure verbatim: "the
row Lonny actually confirmed stayed open and aged out as a loss."

The audit is structurally blind to it: QC-069 pairs WINs against PENDINGs
(row A is LOSS), QC-074 needs an MDOLX (row A has none), QC-051 needs
matching request dates (08-25 ≠ 08-26). Settling it needs the blob — the
diagnostic and its decision rule are specified but NOT yet run.

#### 3. THIRTEEN DASHBOARD BUTTONS, AND THE FILTER WORKED ON NONE OF THEM

Michael: *"in portal if you notice filter active.. it still lists every move
ever won"*, then *"so all buttons need checking"*. All thirteen were.

- **No date existed anywhere.** Tiles carried only a status string and NO row
  carried a date attribute at all, so "Wins — Wed Aug 26" showed every win
  since January. The day filter was not broken, it was absent.
- **The selector was document-wide.** The Pending tile opens the Pending TAB,
  yet its filter reached across and dimmed every row of the Confirmed Wins
  table on the Summary tab — invisibly, persisting until Clear Filter. The
  only tile that did damage rather than nothing.
- **QL, NQ and `quoted` had no branch at all**, so three tiles lit a banner
  naming a scope and filtered nothing.
- Clicking Wins dimmed exactly one thing: the table's own HEADER row, because
  the table opened with no `<thead>` so the header landed in the implicit
  tbody and faded to 25%. That faint header was the entire visible effect.

**THE TRAP IN THE OBVIOUS FIX**, and the reason this took a real
investigation: the day "Won" tile counts bookings CONFIRMED that day whatever
day the RFQ came in — the dashboard's own header says so — while the table
DISPLAYS Req Date. Filtering on the displayed column would have dimmed the
very row booked that day and shown an empty table under a tile reading 2.
The attribute is `win_event_date`.

Fixed: `data-win-date` from the booking date, `data-filter-date` on the day
Wins tile, the selector scoped to the clicked section, `<thead>` around the
header, and every label that named a scope nothing could enforce rewritten to
describe what you actually land on.

#### 4. "STILL SHOWS THINGS UNMAPPED" — ONE ROW, TWO DIFFERENT BUGS

`Unmapped | 2 requests | Huangpu, Jpyok`. Two destinations, two requests, so
one row each — one is the win, the other the Q&L.

**Huangpu is a genuine map gap**: a real port near Guangzhou, simply absent
from `_TRADE_REGION_MAP` and from `KNOWN_DESTINATIONS`. Added to both, and to
`src/hilmar`'s corpus twin (the parity test requires them byte-identical).

**Jpyok is our own parser.** `body_parser._norm` title-cases any all-caps
token longer than three characters, so the UN/LOCODE `JPYOK` becomes the
port name `Jpyok`. Nothing in this repo knows what a LOCODE is — zero hits
for the word anywhere. Adding `jpyok` to the map would turn the row green and
split Yokohama across two spellings **forever**: separate lane keys, separate
win-rate denominators, separate `request_id`, and `same_port('Yokohama',
'Jpyok')` is False so an OL booking naming JPYOK would never link to Lonny's
Yokohama RFQ. It would also delete the only detector currently pointing at
it. **Deliberately NOT mapped**, with a test pinning that it stays Unmapped.

The real fix — a table-gated LOCODE normalizer at the parse boundary — is
NOT done here: it changes stored destinations, hence `request_id`, hence any
`operator_corrections.json` entry keyed to those rows goes stale silently.
That is a migration and needs approval. It also needs the source subject line
read out of blob first: JPYOK=Yokohama is an external reading, not something
this code proves.

Latent, same family, measured: `trade_region_for` never routes through
`canonical_port_key`, so nine spellings the alias table already resolves are
Unmapped today — pusan, hongkong, saigon, cat lai port, cai mep port, manila
north, manila south, lad krabang, ho chi minh city. The next pink row is
already loaded.

#### 5. THE CRON MOVED — AND THERE IS A CLIFF 90 MINUTES BELOW IT

Michael: *"blast did not go out today"*, then *"move cron earlier"*.

It HAD gone out. GitHub dropped the 12:07 UTC tick and started the run at
15:54 UTC — 3h47m late — so the email landed 12:12 PM ET instead of ~8 AM.
Nothing failed; the scheduler slipped, which `daily.yml` already documented
as a 2-4h possibility. (A manual dispatch at 01:31 UTC had also sent, keyed
to report-day Aug 26; today's keyed Aug 27, so no double-send.)

**MOVING IT EARLIER IS NOT A ONE-LINE CHANGE.**
`core.report_business_day` treats any fire before **6:00 AM ET** as belonging
to the prior business day. With `window=previous`, a 5 AM Thursday fire would
report TUESDAY and skip Wednesday — every day, silently, with nothing red.
Verified before moving: 05:07 ET → business day 08-26; 06:07 ET → 08-27.

Moved to **6:30 AM ET**, the earliest slot with a real cushion above the
cliff (GitHub fires late, never early). `tests/test_cron_respects_the_wee_
hours_cutoff.py` now fails if any report-sending cron is ever set below it,
reading the cutoff out of `core.py` rather than hardcoding it.

**The fire time is pinned in SEVEN places** and the suite caught every one:
`daily.yml` crons, `RUNBOOK.md` prose, `deploy/setup_cloudpc.ps1`'s scheduled
task, `scripts/sentry_setup.py`'s cron monitor, and three tests. All moved
together — leaving the Sentry monitor at 8:07 would have paged every weekday
for a check-in arriving 97 minutes early.

NOTE: `setup_cloudpc.ps1` is the SETUP script. The live Cloud-PC task keeps
firing at 8:07 until someone re-runs it. Michael's standing instruction is
not to touch that box, so this is flagged, not done.

**And the guard found a pre-existing surprise**: `weekly.yml` fires 5:07 AM
ET, below the cutoff. That one is CORRECT — `gen_weekly_summary._fire_day_et`
has its own rule with no wee-hours rollback ("5 AM Monday IS Monday") and
never calls `report_business_day`. The exemption is now asserted rather than
assumed: a test fails if the weekly is ever routed through the rollback while
keeping that cron.

Suite: 3,422 passed / 1 skipped. Coverage 91.15% (gate 90%). ruff clean.
Isolated-import check: 0 failures.

### 2026-08-26 (later) — the diagnostic nobody had read, read

Michael approved dispatching diag-blob after PR #226 flagged an assumption it
could settle. Run `33018399015` on `main`, all ten steps green. Three answers,
none of them guesses.

#### THE SHARED-MAILBOX 404 — LOOKED UP AT LAST

CLAUDE.md names this as the standing case: "it has been guessed at twice and
looked up zero times." Re-probed today, twelve days after 2026-08-14, and the
five lines come back byte-for-byte identical:

    directory object : PASS — 'MBD Ocean Export Booking (Shared)' Member
    folder list      : 404 ErrorItemNotFound  Default folder Root not found
    inbox read       : 404 ErrorItemNotFound  Default folder Inbox not found
    sentitems read   : 404 ErrorItemNotFound  Default folder SentItems not found
    /messages        : 404 ErrorItemNotFound  Default folder AllItems not found
    inbox delta      : 404 ErrorItemNotFound  Default folder Inbox not found

Mail.Read.Shared token ACQUIRED. Nothing has changed; no grant has been made.

The theory standing in `daily.yml` — Exchange answers 404 rather than 403 for a
mailbox the caller has no Full Access on — turns out to be RIGHT, and is now
[Likely] on documentary evidence rather than on reasoning. The developer index
carries this exact string as the standard symptom of missing delegation, and
Microsoft's own "About shared mailboxes in Microsoft 365" states that "Delegate
access must be done through the delegate's own mailbox", which is that Full
Access grant. No correction needed to what was written on 2026-08-14; the
confidence marker is upgraded and the source recorded.

ONE RIVAL READING SURVIVES and is now written down, because it points at a
different owner: the identical error is what Graph returns for a mailbox with
NO STORE behind it — a user mailbox Exchange disconnected after its licence was
removed. Nothing reachable from this repo separates the two. One command in
OL's Exchange admin does: `Get-Mailbox` on the address — no result means no
store, a result means the grant is missing. That question goes to OL alongside
the OWA "Open another mailbox" test already documented.

OWNER: OL IT. Not a code defect and not fixable from here.

#### THE MULTI-BOOKING ASSUMPTION BEHIND #226

[Likely] the 18 back-entered bookings are ONE MDOLX PER ROW. Every daily-tracker
row recovered from the cached bodies names a single ref —
`Oakland → Yokohama | CMA CGM | 261026 | PRESIDENT LB JOHNSON 0DBP2W1MA` — and
261027, 261030 and 261032 each appear as their own row despite sharing one
vessel, voyage, lane and carrier.

So Michael's scenario is REAL — several bookings against one vessel and lane do
exist in this data — but they are held as separate rows, not folded onto one.
`shipment_count` therefore returns 1 for each and #226 changes no number on
today's dataset. It is a guard against a shape the data can take, not a
correction to a number now being printed. Stated plainly because the PR said
the fix was correct either way, and this is which way it turned out.

Not proven for the whole dataset: the diagnostic lists refs, not rows.

#### THE #225 FIX, ON REAL DATA

`stored rate in NO linked body: 0` across 12 rows — every rate traces to an OL
email. The Osaka row reads `ol_rate 3210.0` with the live parse agreeing, and
the grid it came from carries the free-time columns that used to be misread:

    header: [... 'RATE', 'CARRIER', 'TRANSSHIPMENT', 'ORIGIN FREE TIME', 'DESTINATION FREE TIME']
    data  : [... '$3210', 'CMA', 'Via KOBE', '5 DETENTION + 4 DEMURRAGE FREE DAYS', ...]

Blank signers: 0. Maria Machado is named on every row — the closed-roster fix
holding.

#### STILL OPEN

- 8 of the 18 refs still carry no booked date, and are meant to.
  `261031` has only a CMA CGM carrier notification cached, not an OL booking;
  `260469` is a 'DRAFT RATED FOR HILMAR' rating email; `261072` and `260433`
  appear ONLY inside our own daily-tracker emails, which is circular — dating
  a booking from our own report proves nothing. And `260358`, `260370`,
  `260896`, `261068` are in no cached body at all: that evidence is not in the
  mailbox and must come from Michael or OL, not from this pipeline.
  A date for any of the eight would be fabricated.

#### AND THE REASON THE OTHER 14 LOOKED HOPELESS WAS A TYPO

The first draft of this entry said the remaining 14 "carry no send time, so a
booking confirmation's own timestamp cannot be used as the booked date". That
was WRONG, and wrong in the same shape as everything else fixed today: a claim
read off a symptom without checking the thing underneath it.

`diag_booking_dates` printed `sent=?  from=?` on all 3,437 cached bodies
because it read `rec["sent"]`, `rec["received"]`, `rec["from"]` and
`rec["sender"]` — and `fetch_bodies.py:27` defines the schema as **`sent_ts`,
`received_ts`, `sender_email`**. None of the four names it looked under exist.
Its sibling `_text()` worked only because `"text_body"` happens to head its own
fallback list.

The send times were in the cache the whole time. Measured on a record built to
the documented schema:

    BEFORE : sent=?  from=?
    AFTER  : sent=2026-08-03T21:51:00Z  from=MBD_OceanExportBookingShared@ol-usa.com

Fixed; the real schema keys go first and the old names stay last as a fallback
for any older row.

#### AND THE RE-RUN IMMEDIATELY SHOWED THE FIX WAS NOT ENOUGH

Dispatched again with the key fix (run `33019186551`). The metadata prints —
and it says something the first reading of it got wrong.

`sent_ts` is NOT the booked date for these rows. It is when THAT message was
sent, and most of these are FORWARDS. Measured, verbatim off the run:

    ref      cached sent_ts (the FORWARD)   quoted Sent: (the BOOKING)
    261025   2026-08-13T20:23Z              Tuesday, August 4, 2026 9:23 AM
    261026   2026-08-13T20:12Z              Monday, August 3, 2026 5:43 PM
    261027   2026-08-13T20:05Z              Monday, August 3, 2026 5:51 PM
    261028   2026-08-13T20:14:12Z           Monday, August 3, 2026 5:57 PM
    261029   2026-08-13T20:11:15Z           Monday, August 3, 2026 6:03 PM
    261030   2026-08-13T20:09:24Z           Monday, August 3, 2026 6:09 PM
    261032   2026-08-13T19:59:04Z           Monday, August 3, 2026 6:19 PM
    261033   2026-08-13T20:16:16Z           Monday, August 3, 2026 6:24 PM
    261046   2026-08-13T20:18:20Z           Wednesday, August 5, 2026 6:12 PM
    261047   2026-08-13T20:01:53Z           Wednesday, August 5, 2026 6:11 PM

Ten confirmations inside a 24-minute window on 2026-08-13 — one batch forward.
Using `sent_ts` would have dated **all ten bookings to 2026-08-13**. The real
spread is Aug 3 (x7), Aug 4 (x1), Aug 5 (x2).

That is exactly the failure QC-080 (win-date clustering, ≥30% on one day) was
added this session to catch, and it would have caught it — but the right answer
is not to trip a check, it is to read the correct field.

`_quoted_sent_date` now reads the original off the quoted Outlook header, and
the run prints BOTH, labelled, flagging any body whose quoted date differs from
its own stamp. The script's own "HOW TO READ THIS" footer said *"that email's
`sent` IS the booked date — use it, no inference needed"*; that instruction was
wrong and is replaced, because it is what a future session would have followed.

Verified against all five real shapes from the run, including the CMA CGM
notification (genuinely 2026-08-13) and a daily-tracker row with no quoted
header at all (correctly returns None).

MDOLX261027 was booked **2026-08-03**. That figure was right in the first draft
of this entry, but for the wrong reason — it came from a fabricated test record
whose `sent_ts` happened to be the booking date. On the real body `sent_ts` is
2026-08-13. Right number, wrong mechanism, and the mechanism is what a reader
would have reused.

#### AND THEN THE DATES WERE APPLIED

That paragraph originally ended "Replacing the inferred dates in
`operator_corrections.json` is a data change and needs Michael's approval, so
it is NOT done here." Michael then said: "YOU JUST DO IT.. I'M BUSY". So it was
done here, and the note above would have been false left standing — flagged by
Copilot on the PR, which was right.

The ten evidenced refs now carry a `booking_timestamp` read from the QUOTED
Outlook header and converted ET→UTC:

    261025  2026-08-04T13:23:00Z     261030  2026-08-03T22:09:00Z
    261026  2026-08-03T21:43:00Z     261032  2026-08-03T22:19:00Z
    261027  2026-08-03T21:51:00Z     261033  2026-08-03T22:24:00Z
    261028  2026-08-03T21:57:00Z     261046  2026-08-05T22:12:00Z
    261029  2026-08-03T22:03:00Z     261047  2026-08-05T22:11:00Z

The DST conversion is ASSERTED, not assumed: August in America/New_York is EDT
(UTC-4) and the offset is checked before each stamp is written, so a zone
surprise fails loudly rather than shifting a booking across midnight. The
`replace(tzinfo=ZoneInfo)` → `astimezone(utc)` pattern was confirmed against
the CPython docs rather than recalled, per the LOOK IT UP rule.

`core.win_event_date` prefers `booking_timestamp`, so each win is now credited
to the day it was BOOKED rather than the day the tracker was told — the rule
already documented in that function.

`scripts/operator_corrections.json` records its own provenance in
`_booked_dates_source`, naming which field was read and which was rejected.
`tests/test_booked_dates_are_sourced.py` holds both halves shut: the ten must
carry their exact stamps and route through `win_event_date` to the right day,
the eight must stay empty, and NO stored `booking_timestamp` may fall on
2026-08-13 — so a future re-derivation from `sent_ts` fails loudly instead of
silently clustering ten wins onto one day.

### 2026-08-26 — one booking, one number; and a manual describing an email nobody got

Review findings on #224, verified against `main` before acting on any of them.
All four were real. One was worse than reported, and chasing it turned up a
section of the report that had stopped being produced by anything at all.

#### 1. THE COUNTING RULE WAS IMPLEMENTED IN ONE PLACE OUT OF SEVEN

Michael's rule from 2026-08-24 — count shipments, not emails; "no it would be
three requests to three wins"; "there are no bookings without rfqs" — shipped
in #223 wired into exactly ONE function, `gen_weekly_summary.analyze_week`.

Measured on `main` at 8d53fc9, one row carrying three MDOLX refs read:

    weekly KPI tile ................ 3 wins    core.booking_count
    weekly Top Winning Lanes ....... 1 win     by_lane[lane]["wins"] += 1
    weekly Carrier of the Week ..... 1 win     by_c[c]["wins"] += 1
    daily email win tile ........... 1 win     len(day_wins)
    period-to-date summary ......... 1 win     len(wins)
    dashboard "Confirmed Wins" ..... 1 booking len(wins)
    trade region / lane / carrier .. 1 win     += 1

One booking, six numbers, in reports read side by side — the same
self-contradiction that produced the 175% quote rate, one level down.

FIX: `core.shipment_count(r)` is now THE rule, in one place, in both trees. A
win is worth its distinct MDOLX refs; every other row is worth 1. Every counter
above routes through it — `aggregate_summary`, `aggregate_lanes`,
`aggregate_carriers`, `aggregate_trade_regions`, both weekly rollups, the daily
tile, both dashboard bucket loops, and the dashboard win tiles.

THE DENOMINATOR EXPANDS WITH THE NUMERATOR. Requests are counted with the same
function, not with `len()`. Counting wins by shipment against requests by row
is precisely how a rate above 100% gets printed. `aggregate_carriers` expands
`quotes` alongside `wins` for the same reason.

`total_entries` counts every row by shipment rather than summing the four
buckets: a floored NQ row (NQ_VALID_FROM) is excluded from `not_quoted` but has
to stay in the total, or QC-075's trade-region reconciliation fires on healthy
data.

Verified on a three-booking row: PTD summary, weekly, lanes, carriers and trade
regions all now read 4 requests / 3 wins / 1 Q&L / 75% win rate.

#### 2. THE MANUAL DESCRIBED SIX SECTIONS NOBODY COULD FIND

#224 moved six analysis sections out of the daily email. `gen_manual.py`'s
`EMAIL_SECTIONS` still catalogued all six, and that manual is attached to every
daily email — so staff received a guide to sections that were in nobody's inbox.

The drift guard did not catch it because it asserted the renderer still
EXISTED. All six functions still exist; they are simply never called. Existence
was never the property worth guarding.

FIX: the catalog now describes the email that actually ships, plus a new
`MOVED_TO_DASHBOARD` list printed in the manual so a reader who misses a
section is told where it went. The guard now walks gen_email's call graph and
fails if a catalogued renderer is UNREACHABLE from `build_body` — and a second
test fails if a section listed as living in the dashboard is not in it.

#### 3. A COMMENT I WROTE IN #224 WAS FALSE, AND ONE SECTION HAD NO HOME

That comment said the seven moved sections "already exist in the attached
dashboard HTML and the 6-page PDF" and that "gen_dashboard and gen_pdf import
several of them". Neither was checked before it was written. No file outside
gen_email.py references those functions at all.

Checked afterwards, section by section: Week over Week, Carrier Performance,
Volume by Trade Region, Top Winning Lanes and Top Losing Lanes are all in the
dashboard — built from its own code, not by importing these. **Loss-Reason Mix
was in neither the dashboard nor the PDF.** Removing it from the email deleted
the "why we lost" breakdown from every artifact this system produces.

FIX: restored to the dashboard's Summary tab, rendered by calling
`gen_email._loss_reason_mix_html` — the same function, so the two cannot give
different answers. The comment now states what was verified, including that it
was asserted rather than checked. Two tests pin the caller and the render.

#### 4. EVERY MONEY COLUMN IN THE EMAIL WAS MIS-ALIGNED, AND NOT BOLD

Found while confirming a reported duplicate-`style` nit. Two defects in the
cell helper, both silent, both predating #224:

  - `_TD_STYLE.replace("text-align:left", "text-align:right")` — used 11 times
    — was a NO-OP. `_TD_STYLE` never contained `text-align:left`; only
    `_TH_STYLE` did. Every rate, TEU, wait-hours and Time-to-Quote cell has been
    left-aligned under a centered or right-aligned header for as long as the
    helper has existed.
  - `<td {_TD_STYLE};font-weight:600;font-size:14px>` appended declarations
    AFTER the attribute's closing quote. `html.parser` reads that as
    `[('style', 'padding:...'), (';font-weight:600;font-size:14px', None)]` — a
    garbage attribute NAME, not CSS. The rate column was neither bold nor sized,
    in the email whose stated design goal that week was that the number be
    findable in ten seconds on a phone.

The reported nit was real too: the edge column's `<th>` carried two `style`
attributes, of which only the first is honored, so the header kept full padding
while every body row's edge cell was a 6px sliver.

FIX: `_cell(*extra, align=...)` builds every data cell with all declarations
inside one attribute; `_edge_th()` merges the edge header instead of stacking.
`tests/test_email_cells_are_valid_html.py` parses the rendered email and fails
on an unrecognised attribute, a doubled `style`, or a money cell that is not
right-aligned and bold — plus two source-shape guards so the pattern cannot
return in a table the fixture does not happen to render.

#### 5. `_collapsed_from` WAS WRITTEN AND NEVER READ

Its own comment said it existed "so the reason line can still say OL quoted
before it booked". Nothing read it, so the reason line said no such thing. A
quote that booked the same day rendered "PENDING HILMAR → WIN", hiding that OL
had quoted it that morning — the one thing that section exists to record.

FIX: the status-change pill now renders the day's ARC, `PENDING OL → WIN`,
using the collapsed origin when present.

#### 6. COPILOT ON THE PR — FOUR MORE, ALL MINE, ALL VERIFIED

- `src/hilmar/core.aggregate_carriers` was left at `+= 1` while the library's
  `aggregate_summary` moved to shipments. `src/hilmar/qc.py` reads both, so a
  multi-MDOLX row would have made the carrier stats disagree with the summary
  printed beside them — the same defect, in the tree I had just claimed to
  mirror. Both `quotes` and `wins` now expand together.
- The dashboard's Confirmed Wins heading counts bookings, but the table under
  it rendered only `mdolx_ref`. A three-booking row showed ONE number beneath a
  heading claiming three; a reader who scrolled down to check the tile found it
  contradicted. The cell now names every distinct ref, de-duped the same way
  `booking_count` counts them.
- A comment I wrote pointed at `tests/test_report_design.py` for the cell-HTML
  guard. That guard is in `tests/test_email_cells_are_valid_html.py`. A comment
  naming the wrong file is a claim that fails silently.
- The money-alignment test built a `money` list that could never match (it
  searched attribute VALUES for a `$` that is in the cell text) and ended on
  `assert money is not None`, which is true of every list. Removed, and the
  real assertions tightened to also reject a doubled `style` and trailing
  declarations.

#### WHAT WAS NOT DONE

- `_pending_html`, `_pending_ol_html`, `_week_block_html`,
  `_carrier_block_html`, `_trade_region_html`, `_winning_lanes_html` and
  `_losing_lanes_html` are defined and called by nothing. Left in place, not
  deleted — deleting seven renderers is a destructive change nobody asked for,
  and the reachability guard now makes their status visible rather than silent.
- No live-data check that any row actually carries multiple MDOLX refs. State
  is in blob and nothing meaningful runs locally; `[ASSUMPTION]` that such rows
  exist, based on Michael's 2026-08-24 question about two bookings on one
  vessel. The fix is correct either way — with no multi-ref rows every count
  above is unchanged.

Suite: 3,400 passed / 1 skipped. `src/hilmar` coverage 91.15% (gate 90%).
ruff clean across scripts/, src/, tests/, deploy/.

### 2026-08-24 — the weekly numbers did not add up, and one of them was a bad parse

Michael, on the Aug 17-21 executive summary: "how are there 16 requests with 9
wins and 10 losses   that would be 19 requests". And on the 4-week trend, where
Aug 10-14 read 12 requests / 17 wins / 175% quote rate: "how more wins then
requests" ... "that's unusual and sounds like bad parse".

Both correct. Two separate defects wearing one symptom.

#### 1. TWO POPULATIONS DIVIDED BY EACH OTHER

analyze_week counted Requests/Q&L/NQ from INTAKE (rows whose request_date fell
in the week) and Wins from EVENT (bookings that landed in the week, whenever
the RFQ came in). Then it divided one by the other:

    win_rate   = 9 / (9 + 10)  = 47.4%     19 outcomes against 16 requests
    quote_rate = (19 + 1) / 16 = 125.0%

The function's own docstring asserted the rate "cannot exceed 100%" because
"every win in win_rows is also in the denominator". That was false, and stating
it is what stopped anyone checking.

MICHAEL'S RULE, in his words: count shipments, not emails. Every booking is one
request and one win. A quote that lost is one request and one loss. So total is
now DERIVED — wins + Q&L + NQ + pending — and cannot be smaller than what it
contains. Both rates sit inside one population and are arithmetically incapable
of exceeding 100%.

A row carrying several MDOLX refs is several bookings ("no it would be three
requests to three wins"), so core.booking_count expands wins and total
together. Until now multiple bookings lived on ONE row and every count was
row-based, so a three-shipment RFQ reported as a single win — the opposite
error, understating wins and TEU.

COST, STATED RATHER THAN HIDDEN: a week's "Requests" is now what RESOLVED or is
still open that week, not what arrived in the post that week. A Friday RFQ
booked Monday counts once, in Monday's week. The old contract had a test
pinning the opposite; it was rewritten with the reasoning, not deleted.

I ALSO GOT THE CAUSE WRONG FIRST. I told Michael one driver was "bookings with
no RFQ" — the stand_*/ol_* rows from OL's transaction report. He corrected it:
"there are no bookings without rfqs ... each one had a quote so that's fine".
They count as requests too. Recorded because the wrong version was said out
loud.

#### 2. THE BAD PARSE HE SPOTTED — 17 WINS IN ONE WEEK

apply_operator_corrections stamped every status flip with C.now_utc(). So a
correction back-entering a REAL booking dated it "today". On 2026-08-13 that
put 18 bookings — MDOLX 260896 and the sequential 261025-261047 batch out of
OL's transaction report — into the week of Aug 10-14. Worse, 49 more
corrections carried a genuine booking_timestamp spanning January to April, and
win_event_date ignored it in favour of the same fire-time transition stamp.

Jan-Apr bookings, all credited to one week in August. The bookings are real;
the DATE was manufactured by the applier.

  - the applier now stamps the booking's own clock: booking_timestamp → the
    correction's `at` → the row's booking/response/request time → now() only
    when the row carries no clock at all. That is the same preference order
    _restore_prior_win has always used; this path just never learned it.
  - core.win_event_date prefers booking_timestamp over the transition. The
    transition was only ever a proxy, and for a back-entered booking the proxy
    is the day the tracker was TOLD.

QC-080 (new) watches this from the outside, because the fix is in two writers
and a unit test only pins today's two: real bookings do not all land on one
calendar day, so any single day holding >=30% of the dataset's wins is an
ERROR, reported with how many of them lack a booking_timestamp.

#### STILL OPEN

The 18 corrections in the 261025-261047 batch carry no booking date at all, so
their wins now fall back to the row's own request/response time rather than
Aug 13. That is closer to true but still inferred. If Michael can supply the
real booked dates from OL's transaction report, they should go into
operator_corrections.json as booking_timestamp and the inference disappears.

3,349 passed / 1 skipped, ruff clean, src/hilmar coverage 91.07%.

### 2026-08-21 (second pass) — the time system: which clock, and which leg

Michael, on the delivered Aug 20 report: "pending hilmar two sections
different number of open   fix time system with proper tools".

#### FIRST, THE NON-DEFECT — because it nearly cost an operator decision

PENDING_HILMAR_LOSS_HOURS is 24 because MICHAEL SET IT TO 24 (0c73c4b,
2026-07-26, explicitly superseding the 48 he asked for twelve days earlier).
The three Aug-20 quotes were 26-29h old and were aged to Quoted & Lost
CORRECTLY. A stale comment directly above that branch still said "48 CLOCK
hours", which made the constant look like the bug — one edit from reverting
his own call. The constant's own docblock had warned in writing that reading
that block "invites someone to fix an operator decision back to a value he
had already rejected". It nearly did. A test now pins the constant WITH the
reason, so the next session reads why before it reads the number.

Also worth recording: the normal 8:07 AM fire would never have shown this.
A prior-day quote cannot be 24h old at 8 AM. The contradiction surfaced
because THIS session re-fired at 5:45 PM, off-cadence, to deliver the signer
and rate fixes.

#### WHAT WAS ACTUALLY WRONG

1. A THRESHOLD THE CODE HAS NOT USED SINCE 2026-07-26 WAS PRINTED IN THE
   REPORT. decide_status returned "no MDOLX within the 48h (biz-hours)
   cutoff" as a reason_detail — stored in status_history, rendered into the
   Reason column the CEO reads. Now interpolated from the constant.

2. THE TIMER-DRIFT GUARD WAS GREEN OVER BOTH SITES. It matched line by line,
   so a phrase wrapping across a comment break was on neither line; and it
   scanned only comments and docstrings, never string literals — prose the
   program SHOWS A HUMAN was exempt. It now joins comment blocks, reads
   string and f-string literals, tolerates punctuation, ignores histogram
   ranges ("0-48h"), and takes a sentence-scoped [historic] marker for prose
   that deliberately names a superseded value.

3. ETD_MISS WAS MEASURING THE OCEAN CROSSING. _ETA_REQ_ANCHORS matched
   departure language — cutoff, ship by, load by, need to sail — and filed it
   as a requested ARRIVAL, then differenced it against OL's offered ARRIVAL.
   "Cutoff 8/28" vs OL ETA 30-Sep-26 = 33 days; "Need to sail by 8/25" = 36.
   A month of ocean freight to Asia clears the 5-day gate every time, so
   every cutoff-style RFQ that lost was stamped "missed the requested ETD" —
   feeding loss analytics, avg_etd_fit_days and the carrier scoreboard.

   MICHAEL'S RULING: "compare like with like only" — and where the legs do
   not match, record no miss rather than a fabricated one. core.
   requested_fit_days compares arrival-to-arrival or departure-to-departure,
   records which leg in etd_fit_basis, and returns (None, None) otherwise.
   NOT every ETD_MISS was wrong: Lonny asked "ETA 9/15" for Shanghai and OL
   offered 10-Oct. That one is real and still reports as a 25-day miss.

4. A YEAR-LESS DATE TOOK ITS YEAR FROM THE RUN CLOCK, so Lonny's December
   "ETA 1/15" resolved into the year already ending and changed meaning at
   midnight on 1 Jan (reprocess_bodies re-derives every body every fire). The
   year now comes from the message's own send date, and a year-less date
   already well past rolls forward — but ONLY in the top message, because a
   reply quotes the whole thread and a re-ping must not re-date an ask
   written months earlier.

5. THE REPORT NOW SAYS WHICH MOMENT EACH SECTION DESCRIBES. STATUS CHANGES is
   the report day's history; PENDING OL/HILMAR are current state at render
   time — deliberately, since their job is "what is open right now". Michael
   chose labelling over freezing the report, so the pending list stays
   actionable: an "Open right now — as of <time>" stamp on both, plus a
   reconciliation line naming what left the list and WHERE IT WENT (lost,
   booked, moved on).

#### DECISIONS MADE, BY NAME

- Michael: keep the pending sections LIVE and label them, rather than
  freezing the whole report to the report day.
- Michael: like-for-like only on ETD_MISS; no miss beats a fabricated one.
- Michael: Context7 for library behaviour and the developer index for
  third-party runtime failures, as a STANDING rule — now in CLAUDE.md.
  Context7 settled one question in this change: timedelta arithmetic on an
  aware datetime is wall-clock, not absolute, which is why is_business_stale
  and pending_hilmar_stale differ by an hour across a DST boundary.

#### WHAT /code-review CAUGHT THAT I HAD SHIPPED

Two passes, fourteen findings. Three were regressions from this same session:
the status pill rendered the ROW'S CURRENT state at BOTH ends of a change
("WIN → WIN"), and since "Q&L"/"NQ" are display words rather than palette
keys it turned every loss pill grey — a fix for greyness that made everything
grey. The reconciliation line called bookings losses. And making the
etd_fit_days recompute unconditional put it ABOVE the manual_locked guard,
where it would have silently rewritten rows a human had locked.

Fixing one leg of the year roll-forward also made the other worse: before,
both sides shared the same wrong year and cancelled; after, a December ask
measured against a prose-parsed offer came out ~360 days early. Both legs now
take the year from the message.

core.offered_date was mirrored into src/hilmar so both trees read an OL cell
identically — the library tree was returning None for "9-30-26", a form OL
really sends.

#### SCHEMA

etd_fit_basis added to schema.json (additive; "arrival" | "departure" | null).
No migration needed — rebuild-not-merge recomputes it every fire, and it is
recomputed rather than write-once so a value from the old cross-leg rule
cannot persist.

3,330 passed / 1 skipped, ruff clean, src/hilmar coverage 91.08%.

### 2026-08-21 — a signer is whoever signed, and OL offers more than one rate

Michael, on the OL-USA RESPONSES table: "why are signors missing nothing
should be missing.. also the numbers are wrong for $". Two separate defects,
both confirmed against real message bodies pulled from the blob store (diag
runs 32382040208 and 32413391384), neither inferred.

#### 1. THE SIGNER ROSTER WAS A GATE, AND IT WAS SHORT

core.parse_signer required every matched name to appear in
_OL_INDIVIDUALS_FULL (14 entries) or _OL_FIRST_NAMES (14). Maria Machado signs
nine of the twelve recent quotes and is on neither list, so her name was
discarded and the cell rendered "—". Linda Echevarria survived only because
her block carries an "email: First.Last@ol-usa.com" line matched by a separate
rule; Maria's block has no email line.

MICHAEL REJECTED THE DESIGN, not just the symptom: "a signor is a signor if
new staff comes, new staff comes. if they change they change.. maria machado
is staff then." A closed roster is a maintenance debt that fails silently on
every new hire — which is exactly what happened.

The roster is no longer a gate. Any person-shaped name in the signature block
is accepted; the roster survives only as a preference when several candidates
appear. The safe discriminator is the one Michael supplied — "lonny doesn't
sign from an ol email address" — and it is structural, not a name blocklist:
parse_signer only ever runs on the mbd_inbound / mbd_rate_response buckets
(fetch_bodies.py:231), and refresh_stage.classify() only assigns those when
the sender ends in @ol-usa.com. Verified, not assumed.

#### 2. ONLY THE FIRST ROW OF OL'S RATE TABLE WAS EVER READ

_find_table_rows returned [header, first_data_row] and stopped. An OL reply
offering a CHOICE was stored as whichever sailing was typed first, and the
rest was discarded before anything downstream could see it existed. The week
of 2026-08-17, verbatim:

  Oakland -> Xingang     $810 ONE via Pusan  |  $675 CMA direct    stored 810
  Oakland -> Algeciras   $4,938 CMA          |  $4,201 Hapag       stored 4938
  Oakland -> Shanghai    $430 YML            |  $566 OOCL          stored 430
  Oakland -> Yokohama    two sailings, one carrier, one price
  Oakland -> Haiphong    $555 ONE Haiphong   |  $740 CMA SHANGHAI  stored 555

In two of the four multi-option bodies the discarded option was cheaper AND
arrived sooner, so reading row one was not buying service quality. Michael
diagnosed it before I did: "could be different rates for different steamship
lines."

THE HAIPHONG BODY IS THE ONE NOBODY EXPECTED — a row for a DIFFERENT
DESTINATION pasted into the same reply. Today the Haiphong row happens to come
first. Nothing in the parser guaranteed that, and if the order had flipped, a
Shanghai price would have been reported as a Haiphong quote with no warning
anywhere in the pipeline.

  _find_table_block   reads every option row under the header, bounded so
                      OL's pipe-shaped NRA footer ("… AMENDMENT. | | |") and
                      rule rows cannot become options
  _same_lane_options  drops rows quoting a different POD than the first and
                      records them in other_lane_pods instead of dropping
                      them silently
  _pick_headline      the LOWEST rate offered on the lane — with carrier,
                      vessel, voyage, ETD, ETA and free time all read from
                      THAT SAME ROW, so a quote can never pair one sailing's
                      price with another sailing's schedule

MICHAEL RULED THE SAME DAY, and the assumption is retired. Asked which rate
the report should call "the rate" on a multi-option reply, he said: "the
booked one when there is a booking." So the ladder is evidence-first, and it
is now two rungs:

  1. THE BOOKED OPTION, when a booking confirmation exists. Once Hilmar has
     booked, guessing is over — the option booked IS the transaction, and
     reporting a cheaper one it declined would be as wrong as the row-order
     rule both of these replaced. "There is a booking" means
     core.is_confirmed_win: a WIN with an MDOLX reference, the same bar every
     client-facing claim clears (QC-049). A send-signal WIN with no
     confirmation is NOT a booking and will not move a row.
  2. THE BEST RATE OFFERED on the lane, while the decision is still open.
     That is the honest answer to "what did OL quote" before anyone has acted
     on it, and it is what the parser picks.

core.snap_quote_to_booked_option does rung 1, called from phase_3_entries so
it runs in BOTH qc_selfheal passes — the post-patch pass sees carrier_won
after patch_carriers has enriched it, and a row that only becomes bookable
later does not wait a day to move. It is idempotent, which matters because
phase 3 runs twice per fire.

THE SCHEDULE MOVES WITH THE RATE, OR NOT AT ALL — a quote may never pair one
sailing's price with another sailing's schedule. But a WIN's ETD/ETA may
already have come from the booking PDF, which is better evidence than the rate
sheet, so a field is rewritten only when it still holds the value of the
option the row is LEAVING. Anything else was written by a stronger source and
is left alone. Tested both ways.

CROSS-SYSTEM IMPACT, STATED BEFORE THE CHANGE: parse_rate_table is read by
fetch_bodies, patch_carriers (both passes), qc_selfheal and
build_ops_flow_v2. Because the pipeline is rebuild-not-merge, historic rows
with multi-option bodies WILL re-derive to the cheaper number on the next
fire — win/loss economics, the carrier scoreboard, savings and the insights
baseline all move with them. That is a correction, not damage, but it is a
visible one and nobody should be surprised by it. rate_options and
other_lane_pods are additive row keys; no migration script is required
because nothing reads them yet that could break on their absence.

NOTHING IS THROWN AWAY. rate_options rides on the row, and the daily report
renders it in OL-USA RESPONSES and PENDING HILMAR as "$675 / also $810 ONE".
A hidden choice can never again look like a single number.

QC-079 (new) re-asserts the invariant on the real dataset, because the
selection lives in the parser while the row is written by ingest,
patch_carriers PASS 2, the PDF fallback and the heals — five writers, one
rule, and a unit test only pins today's five. Its second half WARNs when a
quote came from a reply that also priced another destination.

#### ALSO FIXED

_msgs in the QC-078 test read log.oks. Log has no such attribute (log.ok only
prints — the lesson from 2026-08-19), and the helper only runs inside a
failing assert's message. It would have replaced a real QC-078 failure with an
AttributeError at the exact moment the failure mattered.

#### 3. LINDA'S COLUMNS HAD NO NAMES, AND THE ETA FELL BACK TO LONNY'S ASK

"important data still missing" (2026-08-20, on a row with ETA Offered "—").
Diag run 32493969967 printed her header verbatim:

  Port of loading | Port of discharge | Container Size | Vessel | Voyage |
  ERD | Doc Cutoff | Cutoff | Sail | Arrive | RATE | CARRIER | ...

"Doc Cutoff", "Cutoff" and "Arrive" mapped to NOTHING. Header-to-cell
alignment is only as good as the header dictionary, and a column the parser
cannot name has its value dropped without a word — so on every quote Linda
sends, the doc cutoff, the port cutoff and the ETA were discarded.

WORSE THAN A BLANK. With the grid's ETA gone, fetch_bodies fell back to the
prose date parsers — which it ran over the WHOLE body, and an OL reply carries
Lonny's RFQ quoted underneath it. On req_720044de494c2b58 (Oakland→Algeciras)
OL's grid says Arrive 24-Oct-26; the stored row said 2026-10-21; and 10/21 is
the date LONNY asked for in the ask below the chain marker. The report handed
the CEO Hilmar's own request back as OL's answer.

Both halves fixed, because either alone leaves it possible: the aliases stop
the grid being missed, and the prose fallback now sees only the top message —
OL's own words — with the grid winning outright. The decoy guard is unchanged
and tested ("Terminal Operator", "Service Line" still map to nothing).

Also closed a dead term in ingest: `best["etd_offered"]` read `rt.get("etd")`,
a key production's parse_rate_table never emits (it is the src/hilmar
mirror's), so every production ETD arrived via the prose path. It now hedges
both spellings, exactly as the ETA line beside it already did.

#### STILL OPEN

None from this report. The multi-option and ETA fixes are verified against the
real bodies in diag run 32493969967 — live re-parse beside the stored row —
but they are verified in the PARSER, not yet in a delivered email: the fire
has not run since. Michael's next daily report is the proof, and if any of
these three still shows a gap, the diagnostic prints stored value beside live
parse for every row and will say which.

3,308 passed / 1 skipped, ruff clean, src/hilmar coverage 91.07%.

### 2026-08-19 (fifth pass) — the undated-quote banner is off the report

Michael, on "⚠️ 1 recent quote has a rate or carrier but no response time, so
it is missing from the dated responses above. It is still counted in the
win/loss totals": "this error shouldn't exist / just clear it."

SECOND TIME HE HAS ASKED. On 2026-08-13 the same instruction — "all that truly
matters at end of days is the wins and losses. turnaround is secondary for the
past moves.. so clear this error" — bought a 14-day recency filter instead of
the removal he wanted. Reading it as "make it smaller" rather than "remove it"
cost a week and a second complaint about the same line.

He is right on the merits, and the banner said so itself: the row IS counted
in wins, losses, TEU and every lane rollup. The only missing field is WHEN OL
sent the quote — turnaround detail, on a report whose stated job is wins and
losses. It alarmed without being actionable by its reader.

REMOVED from the report body. The FUNCTION and QC-077 both stay: a silent
detector is how this count reached 41 unnoticed, and re-rendering it is one
line at the call site.

QC-077 SEVERITY LOWERED, ERROR -> WARNING, for the same reason: an error every
fire over a known, accepted gap trains the reader to skip the audit, and that
is worse than the gap.

warn() AND NOT ok(), deliberately — and this is the part that needed checking
rather than assuming. Log.ok only PRINTS; it is not recorded on the Log and
never reaches qc-result.json. Downgrading that far would have deleted the
count from the audit entirely while I described it as "kept", which is the
failure this project exists not to commit. The historical bucket has been
log.ok since 2026-08-13 and is silent in qc-result.json for the same reason —
pre-existing, noted here rather than fixed in the same breath.

Five tests moved from asserting log.errors to log.warnings: severity changed,
substance did not. Two new tests pin the removal — one that the banner is not
rendered, one that the detector behind it still exists, so "clear the banner"
cannot quietly become "delete the check".

3,268 passed / 1 skipped, coverage 91.04%, ruff clean.

### 2026-08-19 (fourth pass) — QC-078, and a borrowed date could have HALTED
### the fire through drift phase 2

Michael: "make sure drift and qc checks are up to date". Two real gaps, one of
them capable of blocking a send.

DRIFT PHASE 2 COUNTED BORROWED ROWS AS MATCHER DRIFT. It asks "is there a
closer same-destination NQ record than the one this OL reply is attached to?"
and answers from |response_timestamp - request_timestamp|. On a borrowed row
that interval measures nothing — no reply was ever attached to it, the date
came off a different row's quote. And it is not cosmetic: THREE drift
candidates trip MATCHER_DRIFT_FAIL_FLOOR and halt the whole fire, so borrowed
rows on a busy standing-rate lane could black out the daily send on evidence
that does not exist. That is the HILMAR-DAILY-TRACKER-6 failure mode, which
already cost days once. Phase 2 now skips non-evidenced rows; a test asserts
it still flags REAL drift, so the fix cannot be mistaken for disabling it.

QC-078 ADDED — nothing may be derived from a borrowed response time. The
invariant existed only in unit tests, which pin TODAY'S writers. The three
derived fields (turnaround_biz_hours, turnaround_hours, olusa_time_et) have
20+ readers and one write gate, so only a check over the REAL dataset catches
tomorrow's writer: a new heal, a backfill script, a snapshot restored from
before the fix. It runs after phase_3_entries has both written and scrubbed,
so any survivor means a writer the check does not know about. A clean borrowed
row is REPORTED, not errored — the date itself is legitimate evidence about
which quote covered the lane, and crying wolf on correct behaviour every fire
is how QC-077's count reached 41 unnoticed.

QC-078 is deliberately SEPARATE from QC-048, with a test saying so. QC-048
clears only >40 biz-hours; the measured fabrication was 6.95. Merging them
later would reopen exactly the gap that let it reach the KPI.

The QC governance ratchet did its job unprompted: adding QC-078 failed
test_every_emitted_check_has_a_test_or_is_known_untested until it had real
tests, which is the rule QC-INDEX states ("every new code pattern ships with
QC + self-heal in the same commit") being enforced rather than remembered.

3,266 passed / 1 skipped, coverage 91.04%, ruff clean. The drift guard was
verified by reverting it — test_a_borrowed_row_is_not_matcher_drift fails
without it.

### 2026-08-19 (third pass) — the marker reached 1 of 5 readers, a fabricated
### 6.95 biz-hours was in the KPI, and "rebuild-not-merge handles it" was false

A 33-agent enumeration (four sweeps — by grep, by audience, by statistic, by
writer — each blind to the others, every hit then verified) found 17 confirmed
sites. Three were things I had not seen at all.

FABRICATED STATISTICS IN THE KPI. The heal kept turnaround_biz_hours whenever
the borrowed gap fell under 40 biz-hours. That window is not a safety check —
it is the band where a made-up number stays plausible enough that QC-048
(which clears only >40) never looks at it. Measured: 6.95 biz-hours on a row
whose date was copied off another quote, feeding
summary.turnaround_avg_biz_hours, the carrier scoreboard gen_pdf SORTS by, the
dashboard's "use in your 1:1 line meetings" table, and the insights baseline
that future fires are compared against. No turnaround is derived from a
borrowed minute now, at any magnitude.

A THIRD FABRICATED FIELD I MISSED ENTIRELY: olusa_time_et. phase_3_entries
stored a pre-rendered "OL sent at (ET)" clock string off the borrowed minute.
Because it is a CACHED STRING, a reader that correctly guards
response_timestamp still prints it — gen_email, gen_dashboard, core.compute_dod
and restructure_two_table all read it raw. Now guarded at the writer.

"REBUILD-NOT-MERGE MEANS YESTERDAY'S STAMPS DISAPPEAR" WAS FALSE, and I wrote
it twice today. Nothing ever un-stamps a borrowed row: the heal skips any row
that already carries a response_timestamp, so the marker and every value
derived from it persist across fires. The 6.95 would have sat in
tracking-data-v2.json indefinitely behind a skip-only gate. phase_3_entries now
CLEARS all three derived fields on any non-evidenced row — a migration, not
belt-and-braces — and it runs immediately before QC-048 so nothing re-derived
reaches phase_7_save. The earlier claim is retracted in place above.

WHY THE FIX IS AT THE WRITERS. The three derived fields have 20+ readers
across scripts/, src/hilmar/ and two Jinja templates. Guarding each is the
"fix one reader, ship two numbers" failure this repo has now paid for four
times in a week. One write gate plus one unconditional scrub, both in
phase_3_entries — the only code that runs on every pass of every fire over the
persisted file. Read-side guards are a closed list: only the sites that touch
response_timestamp directly, which a writer fix cannot reach (notably
gen_dashboard, which RE-DERIVES the clock string when the stored one is empty
— exactly the state the scrub leaves).

ALSO CLOSED: the marker was being tested by inline string literal in
gen_email; the two report sections outside the day list (PENDING HILMAR and
the full detail table) still printed the borrowed minute, its time-to-quote
and its hours-since to the 9-address staff list; and hilmar/core's
aggregate_summary had no guard at all while test_timing_reset pairs it
directly against scripts/core's.

DECISION REVERSED: "inside 40 biz-hours the turnaround is real and kept"
(shipped 2026-08-14) is gone, with its test rewritten to say why.

TEST FIXTURE CORRECTED, and it is the same disease: test_timing_reset's _req
built rows with turnaround_biz_hours and NO response_timestamp — a shape
production cannot produce (ingest._t sets response_timestamp at :1461 and
derives the turnaround at :1516, same function). Verified before touching it,
because changing a test to make new code pass is exactly the wrong move.

QC-048 now has real tests and left KNOWN_UNTESTED (ceiling 17 -> 16),
including one asserting it is NOT what protects against a borrowed date —
6.95 is not > 40, and nobody should merge the two checks later.

3,259 passed / 1 skipped, coverage 91.04%, ruff clean. The phase-3 guard was
verified by reverting it: 4 tests fail without it.

### 2026-08-19 (second pass) — my own fan-out fix was wrong, and a 36-agent
### adversarial review caught it before it shipped

The first fix (80686e1) claimed the invariant "ONE source, ONE row". It did
not hold, and it failed hardest on exactly the lanes that caused the
incident. Before merging I ran an adversarial review — five independent
lenses, every finding then attacked by a skeptic told to refute it. 30
findings, 19 confirmed, 11 refuted. Four blocked the merge. I reproduced the
two worst myself rather than take the reviewers' word.

BLOCKER 1 — THE HEADLINE CLAIM WAS FALSE. `best` is chosen per row as the
earliest quote covering THAT row's own ask, so several old asks on one
standing-rate lane pick DIFFERENT quotes, land in different groups, and every
one is stamped. Reproduced on the exact $3,289 Oakland→Singapore shape:

    stamped: 3   warnings: 0   turnaround samples fabricated: 2

Silent — worse than the loud bug it replaced. The guard fired only in the
degenerate single-quote case, i.e. never on a standing-rate lane. FIXED by
judging ambiguity on the FINGERPRINT the heal actually trusts (lane + rate to
the cent): more than one undated contender, or more than one dated quote,
refuses the lot and says why. Now: 0 stamped, 1 warning naming the rows.

BLOCKER 2 — THE HEAL RUNS TWICE PER FIRE. run_pipeline.py:78 and :82, with
patch_carriers between, over the file core.save_data persists. Pass 1's stamp
became pass 2's evidence. Excluding borrowed rows from the source pool was
necessary and NOT sufficient: on pass 2 the row stamped in pass 1 drops out of
both the candidate pool and the contender count, leaving the next ask looking
unambiguous. Rows already holding a borrowed date on a fingerprint are now
counted as prior claimants. The test runs the heal TWICE, which is the shape
production runs and the shape no previous test used.

BLOCKER 3 — I INTRODUCED A NEW COUNT CONTRADICTION ON THE SAME REPORT.
Excluding booking-confirmed WINs from QC-077 without excluding them from
gen_email.undated_quotes meant the banner would say 8 while QC-077 — which
the banner tells the reader to go consult — said 0. Verified on a
Yokohama-shaped row. This is the #148 bug (two numbers off one dataset) for
the third time. FIXED by one predicate, core.is_undated_quote, called by
both. The 2026-08-19 review also mutation-proved that
test_qc077_and_the_note_count_the_same_rows — written to prevent exactly
this — RE-TYPED the predicate inline instead of calling it, so deleting the
exclusion from production changed nothing in the suite. It calls the shared
predicate now.

BLOCKER 4 — THE MARKER REACHED ONE OF FIVE READERS. response_time_source was
added and then read only by gen_email. Three CLIENT-FACING sites still stated
a borrowed minute as fact to Lonny: gen_client_email's "Quoted at (ET)" in
both Quotes provided and Awaiting your decision, gen_client_weekly's "Quoted"
column (Mondays), and auto_chase_pending, where a borrowed date licenses
"quote from N days ago" in a chase email — the fabrication that module's own
docstring forbids. All four now route through core.response_time_is_evidenced.
The row keeps its place in the table; only the minute is withheld.

DECISION REVERSED: "the earliest covering quote wins" (a tie-break shipped
2026-08-14) is gone. Two dated rows at one rate on one lane are either two
quote events or one quote captured twice, and nothing distinguishes them —
picking the earlier one is a guess, and on standing-rate lanes it is usually
wrong. More than one quote on a fingerprint now refuses.

ALSO FIXED, and it is the same disease: two tests failed on this change
because they SCAN SOURCE TEXT rather than call the code —
test_qc077_no_longer_counts_a_backfilled_booking grepped the QC-077 block for
a literal that had moved into core, and test_the_promise_is_gone caught my own
new docstring quoting a forbidden client phrase. The first is now
behavioural. The second was right and I reworded the docstring.

3,248 passed / 1 skipped, coverage 91.03%, ruff clean. Both client guards and
the renderer guard were verified by REMOVING them and watching the tests fail.

### 2026-08-19 — One quote was dating a dozen asks. The report counted them
### all as replies.

Michael, on the Aug-18 report showing NEW REQUESTS FROM LONNY (4) above
OL-USA RESPONSES (11): "there is data missing and the request count and reply
count vary greatly as well as container count."

THE TELL WAS IN HIS SCREENSHOT, before any data was pulled. Four Singapore
rows all read "OL Quoted Aug 18 1:44 PM ET" and both Xingang rows "4:42 PM
ET". One real email, fanned across every old same-lane row. Those rows also
rendered with no signer, no time-to-quote and container counts belonging to
a different ask — because no email sits behind them.

CONFIRMED ON THAT FIRE'S OWN LOG (run 32255989336): 17 rows stamped by
_stamp_response_from_dated_sibling, grouped by the source quote they took
their time from:

    2026-08-13T19:59:04   x8   one Yokohama quote -> eight booking WINs
    2026-08-18T17:44:45   x3   one Singapore quote -> three July/Aug asks
    2026-08-12T20:57:02   x2
    four others           x1   each

ROOT CAUSE. The heal's fingerprint was "same lane + rate to the cent", and
that is not a fingerprint on lanes with STANDING rates. $3,289
Oakland->Singapore matches every Singapore row for weeks; $745 Xingang the
same. So an August quote dated July asks, and each stamped row then entered
that day's OL-USA RESPONSES, which buckets purely on response_timestamp.
Eleven replies against four requests, exactly as he read it. One stamp even
produced a fabricated 29.2 biz-hour turnaround sample on a row whose real
resolving event was a booking.

THIS IS MY OWN 2026-08-14 CHANGE. It was built for a single half-copied row
(the Algeciras case) and shipped with tests that only ever exercised one
undated row against one sibling — so nothing in the suite could see fan-out.
Same shape as the two bugs found on 08-15: a test that passes because it
never poses the question production poses.

THREE GUARDS ADDED.
  - A booking-confirmed WIN is never stamped. Its rate arrived by the
    rate/carrier sibling copies; the event that resolved it was the BOOKING,
    and dating that copied rate manufactures a phantom "OL quoted today".
  - Ask SHAPE must not conflict: teu_requested and container_count, when
    present on both sides, must match. Michael named this one directly. A
    quote for 8 boxes does not date an ask for 15. Absent is not
    conflicting, or the heal would disable itself on the many rows that
    carry no container_count.
  - ONE source, ONE row. A response claimed by several rows is ambiguous
    evidence, and ambiguity is a human's call. Refusals are WARNed by
    request_id rather than dropped silently.

SECOND LINE OF DEFENCE, in the renderer. Stamped rows now carry
response_time_source="sibling_quote", and _today_events excludes those from
the day's OL-USA RESPONSES. A borrowed date is evidence about which quote
covered a lane; it is not proof OL sent something that day. The date stays
on the row — the win/loss ledger and QC-077 read it — but it no longer
inflates a reply count.

COST, MEASURED NOT ASSUMED. req_0818ca58087a1cc8 — the very Algeciras row
this heal was written for — now shares its source with another ask, so it is
refused and returns to undated. It does not reopen the 08-14 complaint: the
ask is 2026-08-04, past UNDATED_QUOTE_RECENT_DAYS=14, so QC-077 files it as
accepted backlog (log.ok), not the error banner Michael was reading. Written
into the code comment so nobody "fixes" it back.

QC-077 also now excludes booking-confirmed WINs outright. Without that, the
eight Yokohama rows the heal stops stamping would re-enter the banner as
undateable quotes the moment the stamp was refused. Booked is booked.

Rebuild-not-merge re-decides the STAMPING each fire. It does NOT un-stamp a
row already carrying a borrowed date — see the 2026-08-19 (third pass) entry,
which retracts this and adds the migration.

3,230 passed / 1 skipped, ruff clean. The renderer guard was verified by
removing it — two tests fail without it.

### 2026-08-16 — Unmapped came back because the DETECTOR hid it, not because
### the lookup broke. And the manual report did send.

Michael: "i ran manually and got no report   unmapped.. we fixed this at
root before and it's back"

THE REPORT DID SEND. Run 31966507635 (workflow_dispatch, 19:04 UTC) was
green end to end, and the email is in his IdealX inbox — verified by reading
the delivered message, not the job log: "[VERIFY] Hilmar Ingredients — Daily
Shipment Tracker Update (Aug 14, 2026)", received 19:15 UTC, isRead false,
toRecipients michael.deitchman@idealx.us, four attachments. Under
verify_only every send is forced to the IdealX address; watching the OL
mailbox is why it looked like nothing fired. Worth noting for next time
rather than re-diagnosing.

UNMAPPED — HE IS RIGHT THAT IT IS BACK, AND RIGHT THAT WE FIXED IT AT ROOT.
The 2026-08-05 fix was the comma-qualified LOOKUP ("Shanghai, CN" finding
"shanghai") and it is intact; every lane rendered in his report resolves
correctly, checked one by one. What was broken is QC-015's tiering:

    >10  -> log.error
    >5   -> log.warn
    else -> log.ok        # green, and the port names NEVER printed

One to five unmapped destinations were green and silent. His row was 3
requests / 18 TEU / 2 wins, so QC said OK while a pink row went to the CEO.

The silent branch also named the WRONG THING. `_urows` is rows with no
destination at all (Unknown / "Lane unresolved"); `_unmapped` is real ports
missing from the map. They are different conditions and `_urows` was empty
here, so the message read "zero unresolved rows" while three real ports sat
unclassified.

COST OF THAT SILENCE, measured: identifying the ports took reading the
delivered email out of Michael's mailbox through the M365 connector,
extracting every lane from the HTML, and running each back through
trade_region_for. The report showed a count; the count is not actionable.

FIXED IN TWO PLACES, because one was not enough last time. QC-015 now
reports ANY non-empty unmapped list and always names the ports (error above
10, warn otherwise — never OK). The email's Unmapped row renders the
destination names underneath the label, because the audit is not what
Michael reads and the lane tables show a top-N these rows fall outside of.

MAP EXTENDED with the two ports confirmed present in the real book, both
found sitting Unmapped in the diag orphan list: shekou (ol_260291, Oakland →
Shekou) → Far East, with shenzhen/yantian alongside it; lyttelton
(ol_260140, Oakland → Lyttelton) → Oceania.

DECISION: the tier thresholds stay for SEVERITY but no longer gate
VISIBILITY. A detector that hides small counts is how a fixed symptom comes
back looking like a regression — which is exactly what happened here, and
what cost the investigation.

### 2026-08-15 — I was wrong about the blindness. Michael was right, and the
### stored data proves it.

Michael: "i am in the group email so i see them."

WHAT I CLAIMED, twice, in the entry below and in SKILL.md: that OL's replies
live only in the unreadable shared mailbox, that the tracker therefore
cannot see OL's outbound quotes, that the NQ count would re-inflate after
the 2026-08-17 restart, and that only a process change (OL CCing an address
we can read) could fix it. I put that to Michael as fact and told him to
raise it with OL.

IT IS FALSE. diag-blob run 31877434357, against real stored state:

    response_timestamp by ET day … 2026-07-15: 1 … 2026-08-12: 5, 08-13: 5
    turnaround, 294 dated rows     254 in 0-4h, 17 in 4-24h, 0 negative
    rate responses in transitions  07-22: 2, 07-23: 1, 08-04: 1, 08-12: 3,
                                   08-13: 1

OL's replies are arriving and being captured, usually inside four hours.
Michael is on the distribution, the group copy lands in his OL mailbox, and
`/me` is precisely what intake reads. The mechanism has been in `classify()`
all along: OL-domain sender with Lonny on the message, plus `LonnyThreads`
for forwards that strip him. There was never a permanent gap to route
around.

HOW I GOT IT WRONG. The shared mailbox 404s, which is true and unchanged
(probe re-run this session: all six endpoints still fail, directory object
still resolves). I generalised from that one fact to "we cannot see OL's
replies" WITHOUT checking whether the replies were arriving by another
path — which the stored response timestamps would have answered in one
diagnostic run. I had the tool, I had written the tool, and I reasoned
instead of running it. Same failure mode as the 2026-08-13 "shared mailbox
is live" claim: a precondition mistaken for an observation. The rule this
repo already encodes — a capability claim requires observed OUTPUT, never a
passed precondition — applies equally to claims of INCAPACITY. Absence of
evidence got reported as evidence of absence.

THE 2026-08-14 SPIKE, correctly read. 255 transitions in one day (33 "OL-USA
never responded", 33 "Send but no MDOLX") is not mass blindness: the loss
reasons carry their own age, and "Quoted 2952.7h ago" is 123 days. It is the
aging sweep catching up on APRIL rows. The NQ floor is still the right call
for that backlog — but "the count will re-inflate" was unsupported and is
retracted.

THE REAL UNDATED POPULATION is 60 rows: 49 with no source_imids at all
(entered from OL's transaction report, never as mail, so nothing can date
them), 10 linked only to Lonny's own ask, 1 unexplained and worth chasing.
A backlog, not a live leak.

NO CODE CHANGED. Nothing was broken; what was broken was my description of
it. SKILL.md rewritten so the next session does not inherit the false
premise and go asking OL for a process change it does not need.

### 2026-08-14 — The insights engine was reporting a 100% win rate, and the
### delta was hiding it. Shared mailbox closed permanently.

Michael: "ol won't grant more access." + "do whatever is best for you."

THE BUG, and it is the real one. `baselines._carrier_lane_winrates`,
`baselines.compute` and `insights.build_context` each decided which rows
counted as resolved with

    r["status"] in ("WIN", "Q&L", "NQ")

Production stores the LEGACY form. `scripts/core.decide_status` returns
"LOSS" for both quoted-and-lost and never-quoted rows — verified this
session by running it, not inferred — and QC-041 enforces LEGACY as what
gets written. So that tuple matched WINS AND NOTHING ELSE. Every loss was
invisible to the insights engine, the decided set was all-wins, and the win
rate computed wins/wins = **100.0%**.

WHY NOTHING CAUGHT IT. `win_rate_delta_pp = today_win_rate -
baselines.win_rate_pct`, and BOTH sides used the same broken filter. Both
returned 100.0, so the delta was a flat, healthy-looking 0.0. Two alert
classes were disabled as a side effect: `win_rate_shift` and
`carrier_lane_drop` compare today against baseline and cannot fire when
both are pinned at 100%. Carriers that lost every quote vanished from the
lane view entirely rather than showing 0%.

WHERE IT LANDED. `insights.context_to_dict` feeds the Opus narrative
prompt, so the business advice embedded in the daily email was written from
a fabricated 0.0pp delta. `ctx.win_rate_pct` reads the stored summary and
was always correct — so the email carried a right headline next to a wrong
delta, which is why it read as plausible.

WHY THE TESTS WERE GREEN — worth reading twice. `test_baselines.py::
test_compute_win_rate_uses_decided_only` built its fixtures in the STRICT
form (`status="Q&L"`, `status="NQ"`). Production writes LEGACY. The test
exercised a code path production never takes and passed while the
production path returned 100%. A LEGACY companion test now sits beside it
and must not be deleted. `core.display_status`' own docstring had warned
about exactly this: "Never compare r['status'] == 'Q&L' directly — it'll
silently miss legacy rows." The trap was documented, then walked into in
two files.

DECISION REVERSED, by name. "Decided = WIN + Q&L + NQ" (set 2026-04-27 with
the four-state classifier) is now "decided = WIN + Q&L". The headline win
rate is Wins/(Wins+Q&L) and never included NQ, so the old set put a LEVEL
and a DELTA over different populations in one email — and since the
2026-08-17 floor the report states that NQ rows are not counted while this
still counted them. Both fixes landed together on purpose: correcting the
storage form alone would have swung the delta by whatever NQ happened to
be; correcting the denominator alone would have left it pinned at 100%.
One predicate now, `baselines.is_decided`, which insights aliases rather
than restating — three longhand copies is how all three drifted
identically.

No migration: `baselines.compute` is pure and `update` persists its result,
so the next fire recomputes both sides on the new definition.

THE SHARED MAILBOX IS CLOSED, PERMANENTLY. Full Access was the only route
to reading `MBD_OceanExportBookingShared` (Graph 404s every folder without
it). Michael: "ol won't grant more access." The OWA self-test and the IT
request are both dead leads and the docs now say so.

FIXED A LANDMINE THIS EXPOSED. `HILMAR_READ_SHARED_ONLY` DEFAULTED to
`true` in code — the one configuration that cannot return mail — held off
production by a single env var in daily.yml. Delete that var, or import the
module anywhere it is unset, and the fire reads a 404-for-everything
mailbox, stages nothing and exits GREEN. Not hypothetical: the 10:41 fire
on 2026-08-14 did exactly that. The default is now `false`. The flag is
kept, not deleted, so a future grant is a one-variable change.

STATED PLAINLY, because it is the thing that actually matters: the NQ floor
does NOT fix the underlying blindness. OL's staff reply to Lonny *from* the
shared mailbox, and sent mail is not delivered to the distribution, so
those replies exist only in a store we can never read. Restarting the count
on 2026-08-17 gives a clean slate that re-dirties. The only fix available
without OL IT is a PROCESS one — OL CCs an address Michael controls on
quote replies, or Lonny (who receives every one) copies it. Owner: Michael;
next step: one ask to the OL team. No code change can substitute.

CORRECTION TO MY OWN PRIOR ENTRY. The entry below calls the win-rate split
"pre-existing, changes neither number, just a frame clash". That was wrong
— it changes the number, from a true ~25-38% to a reported 100%. Corrected
here rather than edited there.

### 2026-08-14 — The Not-Quoted reset landed on main, and verifying it
### surfaced a parity blind spot the suite could not see

THE MERGE. PR #209 squash-merged to main as 9054644 — `NQ_VALID_FROM =
"2026-08-17"`. Michael, on the report's "Not Quoted — Last 14 Days (9 listed
• 25 total • 104 TEU)" section: "get rid of thjs as all quoted / and restart
the count monday." Verified on main after the merge rather than trusting the
API's "merged: true": both `scripts/core.py` carries the constant and the
fire's import path resolves to it (`import core` in gen_email.py and
qc_selfheal.py → `/scripts/core.py`, `NQ_VALID_FROM = '2026-08-17'`,
executed this session). Monday's 8:07 AM ET fire carries the reset.
Distribution unchanged: `HILMAR_REPORTS_PAUSED: "verify_only"`, Michael only.

FINDING 1 — THE PARITY TEST HAS AN INTERSECTION BLIND SPOT. The merge shipped
`NQ_VALID_FROM`, `nq_is_valid`, `counts_as_not_quoted` and `nq_reset_note` to
`scripts/` and to NEITHER of them in `src/hilmar/` — with a 3,185-test green
suite. Root cause, in `tests/test_core_parity.py::test_no_undocumented_
constants_drift`:

    common = set(sc) & set(hc) - set(ALLOWED_CROSS_FOLDER_DRIFT)

INTERSECTION. It compares values of constants present in BOTH trees, so a
constant added to only ONE tree is structurally invisible to it. The test
built to stop PR #13-class drift could not see this drift at all.

Not a production defect, and I checked rather than assumed: production
renders NQ exclusively through `scripts/core`, and
`src/hilmar/core.is_not_quoted` has ZERO callers anywhere under src/hilmar.
The stale mirror renders nothing. Monday's numbers are correct.

FIXED by checking the `*_VALID_FROM` policy-floor family by UNION instead —
three tests: floors must exist in both trees or carry a written exemption;
shared floors must hold equal values; and an exemption that no longer
describes a real divergence fails, so the allowlist cannot go stale.
`NQ_VALID_FROM` is entered as a documented scripts-only exemption with the
reason (mirroring it would add dead code, not safety). The next undocumented
one-tree floor fails CI.

DECIDED, NOT DONE: the two cores are 46 symbols apart (24 scripts-only,
22 hilmar-only — `aggregate_trade_regions`, `is_win`, `is_real_rate`,
`trade_region_for` exist only in scripts; the snapshot/insights functions
only in hilmar). "Mirror" is aspirational; they are two partly-overlapping
libraries. Reconciling them is an architectural call on what `src/hilmar`
is FOR, it is a second job, and I did not start it. Owner: Michael to
decide; next step: pick one tree as the library or formally scope the split.

FINDING 2 — TWO WIN-RATE DENOMINATORS, FLAGGED AND DELIBERATELY NOT CHANGED.
`src/hilmar/insights.py:366` builds `decided_14 = WIN + Q&L + NQ` from RAW
stored status and derives `today_win_rate` from it, which then feeds
`win_rate_delta_pp` (line 404) and the `win_rate_shift` alert (line 185).
The headline is Wins/(Wins+Q&L) — NQ has never been in it. So the insights
block can print a delta and fire an alert computed on a denominator the
headline does not use.

PRE-EXISTING — the floor changes neither number (`ctx.win_rate_pct` reads the
floored `summary`; `today_win_rate` reads raw status, which floored rows keep
because nothing is deleted). What the floor changes is the FRAME: the report
now states plainly that NQ is excluded and restarts Monday, while insights
still bakes those same rows into a win-rate delta. That is the "report
argues with itself" failure Michael has already caught once. Not touched
this session because it changes a reported business metric — his number, his
call. Owner: Michael; next step: confirm the insights delta should use
Wins/(Wins+Q&L) to match the headline, then it is a one-line change.

### 2026-08-14 — The shared mailbox: two of my claims corrected on evidence,
### the blind-mailbox alarm, and where the truth actually landed

THE SEQUENCE, honestly. (1) I reported the shared mailbox as live on 08-13
without verifying a single message came back — the "proof" only showed the
token carried Mail.Read.Shared. Every actual read had been failing (403).
(2) I then made the unread mailbox the ONLY intake (HILMAR_READ_SHARED_ONLY)
and the 10:41 fire swept ZERO messages, staged zero, and went green — the
tracker was blind for that run. (3) I reverted to /me and diagnosed the
mailbox as "probably a distribution group". Michael: "you did get the
authorizaton you needed.. you are wrong." (4) The endpoint-by-endpoint probe
(diag_shared_mailbox, run 31806028826) settled it:

    directory object : PASS — 'MBD Ocean Export Booking (Shared)', userType Member
    folder list      : 404 "Default folder Root not found"
    inbox read       : 404 "Default folder Inbox not found"
    sentitems read   : 404 "Default folder SentItems not found"
    /messages        : 404 "Default folder AllItems not found"
    inbox delta      : 404 "Default folder Inbox not found"

WHAT THAT MEANS. Michael was RIGHT about the authorization: the token is
granted, the object resolves, and the errors are STORE-level, not
access-level (a permission failure is 403 ErrorAccessDenied — we are past
that layer). The distribution-group theory was WRONG: the object is a named
shared mailbox. And the reads still cannot work: Graph finds no folder
store behind the address — not even Root. Since OL staff demonstrably use
the mailbox in Outlook every day, the store exists somewhere Graph cannot
see. I first read that as the mailbox being homed on-prem. Michael, same
day: "they use exchange online... we do not run on our own servers at ol"
— that theory is DEAD too, the second wrong one on this mailbox. The
surviving explanation [Likely, unverified]: Exchange Online returns 404
"not found" rather than 403 for a mailbox the signed-in user lacks FULL
ACCESS to — it hides the mailbox rather than admit it exists. Distribution
membership (why /me receives the group's mail) grants no mailbox
permission; Full Access is a separate per-mailbox Exchange grant, on a
different admin surface from the Entra consent that WAS approved, and
Mail.Read.Shared only unlocks what the user could already open. This also
explains the 403/404 mix across runs. Decisive self-test, no IT: OWA →
"Open another mailbox" → the shared address. If refused, the ask is one
grant — Full Access (read) for michael.deitchman@ol-usa.com on
MBD_OceanExportBookingShared — then re-run diag_shared_mailbox BEFORE
believing anything.

OPERATIONAL POSITION, unchanged and healthy: /me is the intake, as it always
really was. Michael is on the group's distribution, so the group's mail
lands in his cloud mailbox. Post-revert fire 31805501343: 5,122 messages
swept, 11 new staged, report sent 13:46 to Michael only.

WHAT THE INCIDENT BOUGHT: a mailbox that yields zero messages for a whole
window is now a per-mailbox ERROR at the point of the read (it fired
correctly on the 13:36 run), and a sweep that returns nothing at all says
the report reflects only previously-staged mail. An empty report and a
quiet day are no longer indistinguishable — the exact shape that cost a
week in July.

verify_only throughout. Suite 3158 passed / 1 skipped, ruff clean.

### 2026-08-13 (7) — Shared mailbox live, turnaround clock back on, and
### what the 49 standalone bookings turned out to be

SHARED MAILBOX. OL granted admin consent for Microsoft Graph Command Line
Tools (= outlook_send.CLIENT_ID 14d82eec-...). A re-auth with --shared minted
a token carrying Mail.Read.Shared and refresh_stage now reads two mailboxes:

    will read: MBD_OceanExportBookingShared@ol-usa.com
    will read: me

The constraint recorded since 2026-06-10 is retired. The read path needed no
edit, exactly as refresh_stage.SHARED_MAILBOX predicted.

CORRECTION, ON THE RECORD. I told Michael reading that mailbox was "the root
fix" for the 13 undated quotes. IT WAS NOT. Measured on the 60-day sweep:

    NEW staged records: 4        already-staged: 3,636

3,636 messages came back with imids we ALREADY had — the shared mailbox is
largely a duplicate of Michael's own, which is what he said weeks ago and
what the code already recorded ("i'm already included in the group emails
from ops, nothing has changed"). The Jun-Aug gap was never an access problem:
where OL replied to Lonny without copying the group, NEITHER mailbox has the
message and no access recovers it. Of the 13, the shared mailbox dated 6; the
booking-derived-carrier fix handled the other 9. Access is still worth having
— it is the authoritative copy and catches anything OL sends only to the
group from here — but it is not a historical trove.

QC-077: 22 -> 7, and pre-patch is clean ("every quoted row has a
response_timestamp"). The 7 survivors appear only AFTER carrier enrichment.
NOT diagnosed — do not read 7 as finished.

TURNAROUND CLOCK BACK ON. core.TIMING_VALID_FROM = "" in both trees. Measured
first (diag-blob 31736160870, 288 rows with both timestamps): ZERO responses
predate their own ask, 8 (2.8%) exceed 30 days, and those 8 are April asks
paired to June/July replies that QC-021 already clears at >40 biz-hours. The
fire confirmed it — 10 implausible turnarounds cleared, not averaged.

THE 49 CANNOT BE CLOSED FROM EMAIL, and the reason is not a weak matcher.
diag_match_standalones over 51 standalone rows: 1 CONFIDENT, 0 POSSIBLE, 50
ORPHANS, every orphan "no same-lane RFQ inside the window". They are
ol_2520xx / ol_2600xx — bookings for Jan-Mar sailings, quoted in late 2025 /
early 2026. The tracker's RFQ history starts in April, so the asks do not
exist in the data at all, and they are far past Graph's ~90-day body
retention. Michael's transaction report is the only evidence for them and it
is already in. The single CONFIDENT match is AMBIGUOUS and was left alone:
stand_260842 (Oakland -> Yokohama, PRESIDENT LB JOHNSON) has THREE candidate
RFQs, all CMA CGM at $3,076, asks 6-8 days apart.

OPEN, FRAGILE, NOT FIXED — ol_260192 is CREATED then EXCLUDED on every fire.
Both entries are mine: created while reconciling the transaction report, then
excluded when Michael said it was cancelled. Net result is correct (133 wins,
matching OL's book) but ONLY because the exclude runs after the create in the
same pass. If that ordering ever shifts, a cancelled booking becomes a
phantom win. Recommended fix: delete the create, keep the exclude (it carries
the reason). NOT applied — operator_corrections.json is authoritative human
state and the numbers are right today, so it is Michael's call.

DISTRIBUTION UNCHANGED. Michael was asked directly whether to go full and
answered "me only". HILMAR_REPORTS_PAUSED stays "verify_only": crons run, mail
is scanned, every send goes to michael.deitchman@idealx.us alone. Lonny and
the 9-recipient staff list receive nothing. Do not flip this without an
explicit, unambiguous go — "great send" was read as approval and was not.

Suite 3129 passed / 1 skipped, ruff clean.

### 2026-08-13 (6) — STATUS CHANGES holds only what happened; the shared
### mailbox becomes reachable, and the QC-077 banner is measured not guessed

STATUS CHANGES. Michael: "clean up the massive status changes asap to just
what's current last two days.. we don't need to see all that you fixed".
Measured (diag-blob 31731525694): Aug 12 = 16 transitions, of which 2 were
real OL answers and 11 were "Operator correction: MDOLX2610xx booked" — his
.xls folded in, none booked that day. Aug 13 already = 249: 35 "OL-USA never
responded", 32 "Send received but no MDOLX", ~180 quotes aged out at up to
2926h. Cause: record_transition stamps `at` = now, so a backlog flush lands
on one day.

  DECISION: judge a derived loss on LATENESS, not age. Aging never fires
  before its window closes (48h/72h-Friday), so the obvious "newer than two
  days" rule would have silenced EVERY genuine aging, not just the backlog —
  a permanently empty section reading as a quiet week. Bookings and OL
  answers are kept unconditionally; the booking IS the news. Reconciliation
  reasons are dropped outright. Feed, not ledger: KPIs and totals unchanged.

QC-077, MEASURED (diag-blob 31732181146). The banner's 22 split:
   10  LOSS, rate present, no booking ref      <- REAL undated quotes
    8  WIN, NO rate, booking ref, operator-corrected
    3  WIN, rate present, booking ref
    1  WIN, NO rate, booking ref
So 9 of 22 carry NO rate at all — only a carrier, and that carrier was
written by the booking reconciliation. A carrier from a booking is BOOKING
evidence; QC-077 counts `rate or carrier` and therefore calls it a quote we
failed to date. It is not a quote at all. NOT YET FIXED — logged so the next
session does not re-derive it.

  Also visible: Oakland → Algeciras $4938 CMA CGM appears as TWO undated
  LOSS rows, while the Aug-12 row with the same lane/rate/carrier IS dated
  (20:57:02Z). Same OL quote. A same-lane/same-rate/same-carrier sibling that
  is dated is a real recovery route for some of the 10 — with a window guard,
  since an identical price months apart is a re-quote, not the same event.

SHARED MAILBOX — the constraint changed today. OL approved admin consent for
"Microsoft Graph Command Line Tools", which IS this app
(outlook_send.CLIENT_ID 14d82eec-204b-4c2f-b7e8-296a70dab67e). Since
2026-06-10 the repo has recorded that Mail.Read.Shared needs ol-usa admin
consent and that OL IT declined; refresh_stage.SHARED_MAILBOX says the read
path "starts working with no edit" if that ever changes. It has.

  Consent alone changes NOTHING: a scope has to be requested, and the cached
  token in the blob was minted without it — acquire_token_silent cannot
  invent a scope. auth_notify can now request it behind --shared, exposed as
  the auth-refresh input include_shared.

  DEFAULT FALSE, deliberately. The approval email did not enumerate scopes,
  so whether it covers Mail.Read.Shared is UNVERIFIED. If it does not, AAD
  refuses at REDEMPTION — after the human has signed in — and the run stores
  no token at all. Defaulting it on would put the one lever that recovers a
  dead credential behind an unproven permission. The run reports which way it
  went, in both directions, rather than leaving it to be read off a scope
  string.

  Updated test_every_device_flow_requests_only_the_consentable_set, which
  pinned `initiate_device_flow(scopes=OS.SCOPES)` in auth_notify because
  consent had been declined. That fact expired; the invariant it protected
  (never widen by default) is now pinned directly, plus the workflow input
  defaulting false.

  WHY IT MATTERS BEYOND ACCESS: the 13 genuinely undated quotes are undated
  because the only message linked to them is Lonny's ask — OL's reply went to
  the shared mailbox and never reached the one we read. Reading that mailbox
  is the root fix for the QC-077 banner, not just a wider net.

Suite 3115 passed / 1 skipped, ruff clean.

### 2026-08-13 (5) — Live fire 31728462371: the forwards landed, and the
### two checks that were only right while the bug existed

VERIFIED ON REAL MAIL, not fixtures. Dispatched daily.yml at
mode=production-fire, send_to=test, days_back=21 on f3ea2b8. Both emails went
to michael.deitchman@idealx.us alone (verify_only forced SEND_TO=test in both
send steps; log: "Lonny receives NOTHING").

WHAT THE INTAKE FIX ACTUALLY DID

  refresh_stage: Lonny thread anchors: 85 conversation(s), 529 message-id(s)
  refresh_stage: ADMITTED by Lonny-thread linkage: 4
      Linda.Echevarria@ol-usa.com | 'RE: Oakland to HCMC (Cat Lai)'  x2
      Linda.Echevarria@ol-usa.com | 'FW: Oakland to Algeciras'
      Linda.Echevarria@ol-usa.com | 'FW: Oakland to HCMC (Cat Lai)'

Exactly the two lanes Michael named, and NOTHING else — Hoogwegt's 87
messages, the 1003 from our own mailbox, and every other OL sender stayed
dropped. Both rows reached QUOTED, at 2026-08-12T20:46:10Z and 20:57:02Z.
mbd_rate_response over 7d went to 49. OL-USA RESPONSES is no longer (0).

The aging fix also held its line: both quotes are ~21h old against a 48h
window, so both are correctly still PENDING. A quote given yesterday did not
become a loss. Final tally 380 entries: 133W | 220 Q&L | 25 NQ | 2 P.

THEN THE FIRE FOUND TWO MORE, both the same species — a check that was only
correct while the defect was present.

  QC-072 called both new rows red errors: "status=PENDING but status_history
  ends at QUOTED". Nothing is wrong with those rows. "QUOTED" is not a status
  at all (VALID_STATUSES is {WIN, LOSS, PENDING}); it is the sub-state
  ingest.py:1526 records when OL answers, with decide_status finalizing later.
  So "we quoted it, Lonny has not decided" is spelled exactly this way BY
  DESIGN, and QC-072 compared the two strings literally. It never fired before
  because it needs a row that is quoted AND still pending, and until today
  there were none. Exempted that one pair, narrowly; the history-says-WIN /
  status-says-LOSS shape it was built for still fires
  (test_qc072_still_catches_the_shape_it_was_built_for).

  The undated-quotes banner ended "They appear under PENDING HILMAR" as a flat
  claim. True only while an undated quote could never age — decide_status had
  no clock on such a row, so it held PENDING at any age. Now that they age off
  Lonny's request, most are Quoted & Lost, and the banner had become a
  confident pointer to the wrong section: the exact failure it exists to
  prevent, committed by the banner itself. It now READS the statuses, through
  core.display_status so LEGACY (LOSS+quoted) and STRICT (Q&L) rows both
  bucket correctly rather than falling into "elsewhere".

  Updated test_audit_batch8.py::test_the_report_says_how_many_quotes_it_cannot
  _show, which required the literal "PENDING HILMAR" on every note. Its
  fixture is a Q&L row, so that assertion demanded the wrong pointer. The
  test's INTENT — "the note must tell the reader where the quote DID go" — is
  unchanged and now actually enforced.

STILL OPEN, NOT FIXED, DO NOT READ AS DONE

  QC-077 is 22 (was 21; QC-056 backfilled a carrier onto one more row). It did
  NOT go to zero and the aging fix was never going to take it there — QC-077
  counts rows with a rate but no response TIME, which is a data gap, not a
  status. The split says all 22 link to a CACHED message that carries no send
  time or could not be classified, i.e. the only linked message is Lonny's own
  ask. Stamping the ask's send time is what manufactured the phantom same-day
  quotes in W31/W32, so quote_evidence_ok refuses it and the row stays
  undated. That refusal is correct; the remaining work is recovering the real
  OL message link at ingest, not loosening the guard.

  QC-057: 3 staged Lonny RFQs still silently dropped (no destination parsed).

  Suite 3105 passed / 1 skipped, ruff clean.

### 2026-08-13 (4) — OL forwards enter the tracker on thread identity;
### an undated quote with a Send and no booking finally ages to a loss

Two defects, both reported by Michael against the 2026-08-12 report, both
fixed here with tests driven by the two committed OL quote emails.

## A — "i sent you the two fucking emails five times"

The report listed two NEW REQUESTS FROM LONNY (Oakland->HCMC Cat Lai,
Oakland->Algeciras) and, for the same two lanes, OL-USA RESPONSES (0). OL had
quoted both. `tests/fixtures/ol_quote_algeciras.eml` and
`ol_quote_hcmc_cat_lai.eml` are those emails.

MEASURED: both are FORWARDS — `From: Linda.Echevarria@ol-usa.com
To: Michael.Deitchman@ol-usa.com`, no Cc, Lonny nowhere on the header line.
`classify()`'s OL branch requires `LONNY_EMAIL in _addresses(item)`, so both
returned None and were DROPPED AT INTAKE. Lonny's address IS in the body, at
byte offset 8678 and 4303 — but bodies are fetched AFTER staging and Graph's
bodyPreview is ~255 chars, so no body test could ever have been the gate.

TWO GATES, NOT ONE. `BP.RATE_RESPONSE_SUBJECT_RX` is anchored on a literal
"re:", so "FW: Oakland to Algeciras" fails it, and
`ingest.counts_as_rate_response` re-derives that regex over `mbd_inbound`
rows. Opening intake alone would have parked both in `mbd_inbound` and left
OL-USA RESPONSES at (0) — the same bug one layer down, with a fix in front of
it. `tests/test_ol_forward_intake.py::test_an_intake_only_fix_would_not_have_
been_enough` proves it rather than asserting it.

DECISIONS

  Identity comes from the THREAD, not the header line. `LonnyThreads` collects
  conversation ids + message ids of Lonny-SENT staged mail; a forward is
  admitted only when it is OL-sent, not from us, carries a lane-shaped
  subject, AND links to one of those threads. REJECTED: matching on subject
  alone — Lonny's subjects name no customer ("Oakland to Algeciras"), and
  NUMIDIA / Agri Dairy / Hoogwegt / Erno Laszlo / Brisar load out of the same
  plant on the same lanes, so a subject rule admits their freight verbatim.
  REJECTED: a second body-fetch pass — one Graph GET per candidate per fire,
  and "lupfold appears somewhere in the body" fires on anything quoting a
  thread he was ever on.

  conversation_id is load-bearing; In-Reply-To/References are an OR, never an
  AND. conversation_id is in GRAPH_SELECT and has been persisted by
  build_stage_record since 2026-06-25. Whether Graph returns
  internetMessageHeaders on a COLLECTION $select has never been measured in
  this repo, so the fix does not depend on it.

  Bucketed straight to `mbd_rate_response`, which short-circuits
  `counts_as_rate_response`. `BP.RATE_RESPONSE_SUBJECT_RX` and its src mirror
  are UNCHANGED — widening the shared regex would silently reclassify every
  historical `mbd_inbound` row in both trees with no migration. The new
  `LANE_SUBJECT_RX` is deliberately local to refresh_stage.

  Intake is now TWO passes. The date sweep is newest-first, so Linda's 20:57
  forward is visited BEFORE Lonny's 13:05 request; a single pass tests the
  forward against an anchor set that does not yet contain its own thread. One
  extra pass suffices — anchors come only from Lonny-SENDER rows, decided on
  the From address alone and therefore order-independently. Pass 2 re-decides
  only rows pass 1 dropped, so no sender rule can be overridden.

  The fire now logs the anchor count and names every thread-admitted message,
  unconditionally (the daily fire passes no --verbose). This branch admits
  mail that does not mention Lonny anywhere a human can see; if it ever starts
  admitting the wrong customer, the log must say so without a re-run.

## B — "if you have the quotes and you do not see a booking, it is a loss"

The report banner: "21 further quotes are recorded with a rate or carrier but
no response time... They appear under PENDING HILMAR."

THE STATED DIAGNOSIS WAS WRONG, and shipping it would have been a no-op that
looked like a fix. `pending_hilmar_stale`'s `if resp_dt is None: return False`
is unreachable — every call site already guards the argument. `decide_status`'s
QUOTE-aging branch ALSO already falls back to Lonny's request. Measured: a
3-week-old quoted row with no send and no MDOLX returned LOSS/NO_RESPONSE_TS
before this change and after it.

THE REAL HOLE was one branch earlier. On `has_send and not has_mdolx`,
`send_at` came only from `response_timestamp` and `send_signal_events`, and
`is_business_stale` returns False on None — so a row with neither (exactly
what patch_carriers produces when it recovers a rate from a sibling thread or
a booking PDF) had NO CLOCK AT ALL. Measured before the fix: identical
PENDING/AWAITING_MDOLX at +1d, +30d, +365d and +3650d. `pending_substate`
keys off `quoted`, so it rendered under PENDING HILMAR — the banner's
population.

DECISIONS

  `decide_status` falls back to `request_timestamp` on the send branch, in
  BOTH trees. NOT a change to `is_business_stale`: it must keep returning
  False on None so a row with no clock at all stays PENDING and surfaces as a
  DATA defect, rather than being aged on a timestamp nobody can evidence.

  `pending_hilmar_stale` gains a KEYWORD-ONLY `request_dt` fallback, both
  trees, byte-identical. Keyword-only so a future caller cannot slide it
  positionally into `now` — the same class of error that put a hardcoded 24h
  in QC-007 while decide_status ran 24h/72h. Every existing 2-arg call is
  bit-for-bit unchanged.

  All three detectors were BLIND, which is why nobody saw it: QC-007
  (`if rt and`), gen_improvements_report (`if resp_dt is None: continue`) and
  auto_chase_pending (`if not response_timestamp: continue`) each skipped
  undated rows, so a stuck row raised nothing and got no chase. All three now
  anchor on the request when the quote is undated.

  Removing `if rt` from QC-007 also removed the scoping it was doing by
  accident — `pending` is EVERY PENDING row. QC-007 is now explicitly scoped
  to PENDING_HILMAR and skips AWAITING_MDOLX / MDOLX_NO_SEND, which
  decide_status holds on purpose.

  NO REPORT MAY CLAIM A QUOTE TIME IT CANNOT EVIDENCE. gen_improvements_report
  now says "requested Xh ago (quote undated)" on a request anchor, and
  auto_chase_pending — which emails LONNY — says "request from N days ago"
  instead of "quote from N days ago". Fabricated timing shipped from this repo
  once already (core.TIMING_VALID_FROM); it is not going out over Michael's
  signature a second time. Also fixed a pre-existing mislabel: that flag cited
  PENDING_WINDOW_HOURS while the predicate has always used
  PENDING_HILMAR_LOSS_HOURS.

UNFIXED — needs Michael

  `has_mdolx and not has_send` returns PENDING/MDOLX_NO_SEND with no clock
  consulted, at any age. Unbounded, and real. But there IS a booking, so under
  Michael's verbatim rule it is not a loss — it needs an ops-review SLA, not a
  loss rule. Pinned by test so nobody "fixes" it by accident.

  When a quote is undated, the fallback anchors Friday-ness on LONNY'S
  REQUEST, not OL's quote. A request anchor is always EARLIER than the quote,
  so the window expires sooner than a quote-anchored one would. Mitigated by
  qc_selfheal._heal_undated_quote running BEFORE decide_status in the same
  loop (now pinned by test), which recovers a real response_timestamp from the
  cached body first. Michael still needs to rule on whether an undated quote
  should age off the request at all, or hold PENDING and raise a QC instead.

  How many of the 21 rows are shape A vs the MDOLX_NO_SEND shape is UNKNOWN —
  production tracking-data-v2.json is not in this repo or on this box.

VERIFICATION (this session)
  Baseline before:  3020 passed, 1 skipped; ruff All checks passed!
  After:            3096 passed, 1 skipped; ruff All checks passed!
  New tests verified to FAIL against the pre-fix tree: 30 of 44 (intake),
  17 of 36 (aging). The remainder are regression guards that must hold in
  both states.

  Fixture classification:  BEFORE None -> AFTER mbd_rate_response, both
  fixtures, via conversationId and via the real References chain
  independently; ingest.counts_as_rate_response True on both staged records.

  Synthetic quoted row, request 3 weeks old, no response_timestamp:
    no send, no MDOLX     LOSS/NO_RESPONSE_TS    -> unchanged (already correct)
    Lonny SEND, no MDOLX  PENDING/AWAITING_MDOLX -> LOSS/SEND_NO_BOOKING
    MDOLX, no send        PENDING/MDOLX_NO_SEND  -> unchanged (out of scope)
  Same three shapes with the request 2h old: all PENDING before and after.

### 2026-08-13 (3) — OL quote tables are read by header-to-cell alignment;
### the Dummy-SI footer and "vessel diversion" are now unreachable

Michael supplied two real OL quote emails. Measured this session, both were
misparsed by `src/hilmar/body_parser.parse_rate_table`, which had no table
parser at all and regex-scanned the entire flattened body:

  ALGECIRAS  carrier "MSC"  <- the standing footer "Maersk, Sealand, MSC, ONE,
                               CMA and Cosco do not accept Dummy SI"
             vessel  "dive" <- the standing disclaimer "... routing changes,
                               vessel diversion, or alternate discharge ..."
             eta 2026-10-19 <- Lonny's OWN requested "ETA 10/19", quoted at
                               the bottom of the forwarded chain
             transshipment "Direct" <- Lonny's "direct service if possible"
  HCMC       carrier "MSC", vessel "dive", NO RATE AT ALL (a `500 <= val`
             gate on the prose fallback dropped the real $475.00), no POL,
             no POD

THE BLOCKER, AND IT REVERSES THE BRIEF: `scripts/body_parser.py` — the tree
production actually binds — was ALREADY correct on both emails, 15/15. The two
"mirrored" files had diverged into completely different algorithms and NOTHING
in the suite compared them. So this defect never reached the daily fire; the
client-report symptoms it was blamed for (QC-077, QC-039 at 92.8%, empty
OL-USA RESPONSES) have a different root cause and still need one. The
consumers' two dead keys are the better candidates — see UNFIXED below.

DECISIONS

  Both trees now share ONE rate-table core, copied verbatim, 14,868 bytes,
  and `tests/test_body_parser_parity.py` fails if the copies drift. That guard
  is the actual fix for how this shipped.

  Every field comes from a CELL of the data row aligned under its own header.
  parse_rate_table no longer scans body prose for carrier, vessel or rate at
  all. Boilerplate is unreachable by construction, not by blocklist.

  Header cells are matched WHOLE-CELL, then by word token — never by
  substring. OL's NRA footer ("ACCEPTANCE OF THE RATES AND TERMS OF THIS NRA
  OR NRA AMENDMENT.") scored a "rate" hint under the old substring scan;
  "RATES" is not the token "rate", so it is now rejected, while OL's qualified
  labels ("RATE (USD)", "Ocean Rate", "ETD (POL)") still map.

  The one surviving prose path is the carrier for a grid with NO carrier
  column (the 2026-06-15 Manila fix, which a green test requires). It is
  double-guarded: OL's standing disclaimer lines are stripped first, and a
  line naming TWO OR MORE carriers is rejected outright — a LIST of carriers
  can never identify THE quoted carrier, which is exactly what the Dummy-SI
  line is.

  No sanity gate on the RATE cell. The old `500 <= val` gate is what threw
  away HCMC's real $475.00, so a numeric floor cannot be the defence.

  CORRECTED 2026-08-13, same day: the original wording here claimed
  "alignment already rules out a stray date landing there". An adversarial
  review DISPROVED that in the same commit — the token header fallback
  shipped alongside it mapped an "Inland Rate" decoy column to `rate`, and
  31 landed in ol_rate. The claim was false when written. The real defence
  is the HEADER: a header carrying any word the parser does not recognise
  now maps to nothing at all (see _HEADER_QUALIFIERS), so a decoy column
  cannot supply a rate or a carrier. tests/test_decoy_columns.py pins it in
  both trees.

  `parse_vessel` was hardened too. Its blanket `re.IGNORECASE` defeated the
  `[A-Z]` doing the work and its lazy `{3,40}?` stopped at the 4-char minimum
  — together that is where "dive" came from. src/hilmar/ingest.py:350 calls it
  on the raw body for EVERY bucket, so a clean table parser alone would not
  have cleared the field.

  `vessel` and `voyage` are now emitted as their own keys, and `vessel_voyage`
  joins them in the house form "NYK METEOR 0CLNCE1MA" (matching
  scripts/pdf_parser). src/hilmar previously joined with " / ".
  scripts/build_ops_flow_v2.py recovered the voyage by splitting on "/", so it
  was updated to read the split keys — it had no test at all before; it has
  two now.

  DELIBERATELY NOT CHANGED — both are persisted-data migrations, not parser
  fixes, and need Michael's sign-off:
  - Production keeps RAW table dates ("7-Sep-26"). src/hilmar keeps ISO plus
    the legacy `etd`/`eta` keys its consumers read. The divergence is declared
    once, as `_LEGACY_SRC_CONTRACT`, instead of hiding in two parsers.
  - `detention_free`/`demurrage_free` stay out of the production tree.
    schema.json documents them as "origin-side"/"destination-side" free time,
    but OL's cells read "4 DETENTION + 5 DEMURRAGE" — different meanings, and
    I have no ground truth to pick one. Guessing writes bad data into a
    schema field. src/hilmar keeps them (nothing reads them there).
    "7 COMBINED FREE DAYS" yields neither, on purpose.

  Removed `_carrier_from_cells` / `_CARRIER_HEADER_ALIASES` from production
  and `_TABLE_HEADER_HINTS` from both: three lists of OL column names that
  could disagree, replaced by the single `_TABLE_CELL_ALIASES` map.

TESTS. `tests/test_ol_quote_table_alignment.py`, 35 cases driven by the two
real emails, now committed as `tests/fixtures/ol_quote_algeciras.eml` and
`tests/fixtures/ol_quote_hcmc_cat_lai.eml` so they are self-contained.
Asserts the full field set for each in both trees, that the Dummy-SI line
alone yields NO carrier, that "vessel diversion" prose yields NO vessel, that
appending OL's whole footer to a real table changes not one field, and that
an absent column stays absent. Verified they FAIL on the pre-fix code: 21 of
the 29 that existed at that point were red. Plus 3 drift guards in
`tests/test_body_parser_parity.py`. Suite 2931 -> 2968 passed, 1 skipped
(the day-count case, which production deliberately does not emit).
`ruff check scripts/ src/ tests/ deploy/` clean.

UNFIXED, FOUND WHILE READING THE CONSUMERS — not touched, no approval to
change persisted behavior, and each needs its own tests:
  - scripts/ingest.py:1463 reads `rt.get("etd")`, which production has never
    emitted. Dead. So the `reason_detail` string is ALWAYS "ETD ?".
  - scripts/ingest.py:1503-1504 write `detention_free`/`demurrage_free`
    UNCONDITIONALLY, clobbering any earlier value with None. Both fields are
    structurally always null in production.
  - scripts/ingest.py:82 `_etd_fit_days` parses with `datetime.fromisoformat`,
    but `eta_offered` holds a raw "24-Oct-26" from the table. `etd_fit_days`
    is dead for every table-parsed row.

### 2026-08-13 (2) — OL's own 2026 book is now the authority; 12 phantom
### wins removed, 54 real ones recovered, the response clock switched off

Michael sent OL's transaction report, then the richer customer transaction
report, and ruled: "THE REPORT I UPLOADED EARLIER IS THE REPORT TO VERIFY
AND USE."

THE RECONCILIATION (diag-reconcile 31701602704, backfill dry-run
31702992685, diag-find 31703011175 / 31703548619 / 31705817226 — all
read-only, against state written 2026-08-12 23:05:59 UTC):

  OL's book: 134 Hilmar bookings, Jan 3 - Sep 5 2026 sailings, 533 TEU.
   80 already recorded
   +4 matched to requests recorded LOSS — 260358 260370 260433 260469.
      OL booked cargo the tracker had written off.
  +50 backfilled as standalone wins, 189 TEU, sailed Jan-Apr, before this
      pipeline read any mail.
  -12 excluded.

THE 12, and they were three faults wearing one face:
  NUMIDIA (6) — 260387 260388 260407 260486 260487 260928. A different
    customer whose cargo loads at the Hilmar plant. Michael: "NUMIDIA IS
    NOT HILMAR.. THAT'S WHEN HILMAR IS USED AS A LOCATION." Hilmar Cheese
    is in Hilmar, California, and this pipeline could not tell the client
    from the town.
  CANCELLED (6) — 260772 260895 260963 260192 260426, and 261071.
    Michael: "260905 260192 260963 were bookings hilmar cancelled",
    "260772 was also cancelled", "260426 cancelled".

CANCELLATION EXPLAINS THE EXPORT'S SHAPE, and it strengthens it: OL DROPS
cancelled bookings rather than flagging them, which is why the cancelled
column reads No on all 134 rows. Absence from the export IS the
cancellation signal.

THE PARSER DEFECT WAS ONE STRING. The operational-subject gate listed
"LOADING APPT"; OL wrote "LOAD APPTS". "LOADING" is not a prefix of
"LOAD ", so nothing matched, and a drayage leg from the town of Hilmar to
the Port of Oakland became a WIN on the lane "Oakland → Oakland". Fixed by
ADDING a string, since OL writes both, with a test asserting the
non-containment so nobody merges them back.

TWO OF MY OWN ERRORS, both caught by Michael and both corrected here:
 (a) I called MDOLX260928 drayage with no booking behind it, reading a
     subject line instead of the booking record. His MOVE screenshot shows
     a real ocean export — NUMIDIA BV-LZ, Oakland to Penang. Real booking,
     wrong customer.
 (b) I created MDOLX261071 as a win on 2026-08-12 from a row that was
     EMPTY — carrier null, pol "", pod "", booking_no "". "Everything she
     sent as a booking is a win" presumes the row IS a booking. Withdrawn.

AND A BLIND SPOT IN MY OWN CHECK. diag_reconcile's reverse direction was
scoped `if lo <= ref <= hi and ...` with no else, so anything outside the
export's range fell through to nothing. 261071 and 261072 sit one and two
above its highest ref (261070) and were never examined: it reported "10
wins the recap does not contain" when the true number was 12. Michael
found one by hand. Out-of-range wins are now bucketed in the same branch
and printed under their own heading.

TWO RULES ARE NOW CODE RATHER THAN VIGILANCE:
  - An empty row is not a booking. Twice a row with no port and no carrier
    became a win. is_evidence_of_a_booking refuses and prints REFUSED.
  - One carrier, one name. OL names carriers as legal entities, so ONE
    would have appeared twice — 38 as "ONE", 19 as "OCEAN NETWORK EXPRESS
    PTE, LTD" — splitting one carrier across every rollup and defeating the
    point of the backfill. Six aliases in BOTH cores, plus a test that
    fails if any spelling in the export has no canonical form.

THE RESPONSE CLOCK IS OFF. Michael: "JUST INDICATE THE TURN AROUND CLOCK
AND SUCH IS OFF AND START RUNNING IT AGAIN STARTING TODAY AND INDICATE
THAT ON THE REPORTS." core.TIMING_VALID_FROM = "2026-08-13"; pre-floor
samples are excluded from every turnaround aggregate AND counted. The
averages return None, not 0.0 — a suppressed average rendering as "0.0h"
is not a missing number, it is a FLATTERING one claiming OL replied
instantly. Email, dashboard and PDF print OFF with the date, the live
count, the excluded count and the cause. Clearing the constant removes the
banner with it.

MIGRATION: schema.json widens the two averages to accept null and declares
turnaround_valid_from and turnaround_excluded. Additive, no stored data
rewritten (ingest rebuilds every row each fire), reversible by revert. QC
Phase 10 caught the drift before it shipped.

TOOLING: extract_ol_recap.py reads .xls and .xlsx, routes on magic bytes,
binds columns by HEADER never position and prints the binding, and
confirms the MDOLX column by its VALUES. backfill_ol_bookings gained
--create-missing. diag_reconcile takes a committed export path.
diag_find prints request_id.

operator_corrections.json 19 → 86: 51 created wins, 21 amendments, 14
exclusions. Suite 2917 passed, ruff clean. PR #205 merged (3816d20).

STILL OPEN: reports remain hard-stopped pending Michael's review of the
verification fire.

### 2026-08-13 (1) — the 15 unlisted wins are ANSWERED; the next export gets
### read by machine, not by hand

Michael: "linda only ran a partial report... i'll have a run a year long
report.. and get it to you shortly."

THE OPEN QUESTION FROM (12) AND (14) IS CLOSED, and it closes in the
tracker's favour. 15 tracker WINs sat inside the Jun 1 - Aug 12 recap's date
range while the recap did not list them (260716, 260718-260723, 260748,
260770, 260809, 260811, 260833, 260842, 260928, 260963). I refused to call
that either way, because the two readings — OL's export is narrower than it
looks, or this pipeline is over-counting — have opposite fixes. It was the
first: the export was partial. Absence from a partial list is not evidence
against a win, so those 15 stand and the period's count is the higher one.

Nothing in the pipeline had to change for that. The recap is read only by
backfill_ol_bookings and the diagnostics — no QC rule, report section or
gate keys on it — so those 15 were never at risk of being deleted; what was
at risk was me "fixing" a non-defect. Recorded here so the next session does
not reopen it.

NEW: scripts/extract_ol_recap.py — Linda's .xlsx to the recap JSON that
backfill_ol_bookings and diag_reconcile already consume. The Jun-Aug file
was transcribed by hand at 35 rows; a year cannot be, and a mistyped MDOLX
in that file becomes a WIN in the tracker for a booking that never happened
— the precise failure the last three days were spent removing.

  - stdlib only. openpyxl is not installed in the runner and this cannot
    depend on a network install; .xlsx is a zip of XML.
  - Columns bind by HEADER, never by position, and the binding is PRINTED
    each run. Column order is the thing most likely to differ between a
    two-month export and a year-long one, and a wrong guess belongs in the
    run log, not silently in the data.
  - A missing MDOLX column is a hard error that dumps every header it saw.
    Emitting zero bookings would read as "OL booked nothing", which is a
    worse lie than crashing.
  - One booking per MDOLX. The report is
    Container_Report_With_TEU_By_Container_Size — one booking split across
    container sizes is several lines, and counting each would inflate wins.
  - Unparseable reference, unmapped date: REPORTED and dropped, never
    guessed. --customer filters to HILMAR if the year-long pull spans OL's
    whole book.
  - Excel's 1899-12-30 epoch is pinned by a test against a known serial; an
    off-by-one there silently changes which request a booking can match.

26 tests, including a round-trip of the hand-transcribed Jun-Aug file: all
35 bookings come back identical, which is the only ground truth available
since the original .xlsx is not in the repo. Suite 2846 passed, ruff clean. Reports remain hard-stopped
(verify_only, crons removed).

### 2026-08-12 (14) — 35/35: corrections can now record a win with no email

Michael: "everything she sent as a booking is a win... assume that each win
was a quote request so just use the wins if you cannot find the emails from
lonny in my ol emails which you should as they are there."

Two things, and the second is new capability rather than a fix.

FIRST — MDOLX261072, matched. diag_find showed why the automatic matcher
refused it: OL booked CAI MEP while Lonny's ask names CAT LAI, and
core.same_port demands terminal equality (the rule that keeps Manila North
off Manila South). Michael: "they are wins and replaced cat lai if lonny
approved which he did" — so the terminal swap is normal business, not a
mismatch. Matched to the Aug-11 Cat Lai ask; both that and the choice of
the older of two open asks are written into the correction note, each
reversible with a one-line edit.

SECOND — `create: true` on an operator correction. operator_corrections
could only AMEND an existing row, which cannot express a booking that has
no row at all, and MDOLX261071 has none: no confirmation in the mailbox,
and OL's own recap row carries NO POD, carrier or booking number. It is now
recorded as a WIN with the lane left UNRESOLVED rather than guessed —
QC-015 keeps flagging it until someone supplies the lane.

THE GUARD THAT MAKES THIS SAFE: forwarding is fixed, so the real
confirmation will start arriving and ingest will build its own row for that
MDOLX. A created row stands down whenever any row already carries the
number (primary or secondary ref) — the derived row wins because it has the
email behind it. Without that, this feature would double-count every
booking it healed, which is the same error in the opposite direction.
7 tests, including the duplicate guard, the re-apply idempotency
(qc_selfheal re-runs the applier after ingest), and create being opt-in so
a typo'd request_id still warns instead of inventing a row.

RESULT: 34 matched to real Lonny requests, 1 recorded from the recap alone
= 35/35. Started the day at 20/35.

STILL OPEN and unchanged: the 15 tracker wins the recap does not list
(260716, 260718-260723, 260748, 260770, 260809, 260811, 260833, 260842,
260928, 260963). One question to Linda decides whether the win count is
~74 or ~59; I am not guessing it.

Suite 2820 passed, ruff clean.

### 2026-08-12 (13) — 13 wins recovered from OL's recap; 2 need Michael

Michael: "now use the report that was sent by linda with all the bookings
and match to the lonny requests since july 1 i assume and clean up and the
go forward all emails will again be in my inbox."

Backfill dry run (backfill-ol run 1, --since 2026-07-01 --max-age 60):
  already present   20
  proposed matches  13  — every one flips a real LOSS row to WIN
  not matched        2
Applied all 13 to scripts/operator_corrections.json (17 total), each
verified against the stored recap before writing and each carrying a note
naming its source, carrier and booking number. Version-controlled, so the
change is a reviewable diff and reverts with a revert.

  260896 Nagoya (Jul 9 ask)      261025 Singapore (Aug 3)
  261026 Yokohama (Aug 10)       261027 Yokohama (Aug 7)
  261028 Yokohama (Aug 5)        261029 Yokohama (Aug 3)
  261030 Yokohama (Aug 3)        261031 Yokohama (Jul 30)
  261032 Yokohama (Jul 30)       261033 Yokohama (Jul 29)
  261046 Yokohama (Jul 17)       261047 Yokohama (Jul 10)
  261068 HCMC     (Jul 7)

NEEDS A DECISION, not invented:
  MDOLX261071 — the recap row carries no POD at all (it is the sparse row
                with "No" in the carrier column).
  MDOLX261072 — Cai Mep, ETD Sep 1; the one open HCMC (Cai Mep) ask was
                already claimed by 261068. Either it answers an older ask
                outside the window or Lonny never sent a request for it.

Nine of these (261026-261033, 261046) are the Yokohama cluster that had
been rendering as W31/W32 losses. They were wins the whole time.

WHY CORRECTIONS AND NOT A DATA WRITE: ingest rebuilds every row from staged
mail each fire, so a direct write is erased by morning. operator_corrections
is the one durable human-verdict store the rebuild honours.

Suite 2813 passed, ruff clean.

### 2026-08-12 (12) — RECONCILED against OL's recap: 20 of 35, not all

Michael: "you mean you found all of these emails and more??" No. Measured,
diag-reconcile run 1 against OL's own 35-booking recap:

  20/35 recorded as WIN, 0 present-but-not-won, 15 ABSENT entirely.
  Absent: 261072 261071 261068 261047 261046 261033 261032 261031 261030
          261029 261028 261027 261026 261025 260896

The body-signal fix (11) recovered 3 bookings, 66 -> 69, NOT 15. One of
them is MDOLX261070 — the message Michael forwarded, which is why it was
in his mailbox to be found. That is the fix working, and it is also the
proof of what limits it: the other 14 bookings' messages are not in the
staged corpus at all, because they went To: Lonny, Cc: the group, and only
the forwarded ones reached the mailbox we read.

REVERSE DIRECTION, and it is not settled: 15 tracker WINs inside the
recap's own range that the recap does not list — 260716, 260718-260723,
260748, 260770, 260809, 260811, 260833, 260842, 260928, 260963 (Jun 8 -
Jul 20, mostly Oakland->Yokohama). Either OL's export is narrower than it
appears (its filename is Container_Report_With_TEU_By_Container_Size, so
it may only include rows with container data) or this pipeline is
over-counting. I am NOT claiming either. It needs one question to Linda:
does that report include every booking, or only those with container/TEU
detail?

The Aug-12 test email still showed nothing for the day: the report covers
the PRIOR business day by design (window=previous), and OL's quote
responses still do not match (286/379 unchanged). Bookings and quotes are
separate paths; (11) fixed part of the bookings path only.

Reports remain hard-stopped.

### 2026-08-12 (11) — FOUND: OL changed the booking subject, our gate reads
### only the subject, 15 wins were thrown away

Michael sent Linda Echevarria's reply with two real bookings attached and
OL's own recap (35 Hilmar bookings Jun 1 - Aug 12): "FIGURE OUT WHY YOU
AREN'T SEEING THESE EMAILS".

MEASURED on the two .eml files:
  "MDOLX260963_NEW BOOKING CONFIRMATION// HILMAR 2X20'DV Oakland to HCMC"
      subject HILMAR: True    body HILMAR: True
  "RE: Oakland to Manila (North) / MDOLX261070 / ONE BKG # RICGAZ641400"
      subject HILMAR: FALSE   body HILMAR: True (7x "HILMAR INGREDIENTS")

Same desk, same customer, new subject convention. collect_bookings gated on
hilmar_signal(SUBJECT) alone — the "full tightening" of 2026-08-10 — so
every new-format booking was discarded on arrival. 15 of the 35 bookings in
OL's recap (MDOLX261025 through 261072) never reached the tracker; the
highest win it held was 260980.

diag_find is what settled it, and it is worth keeping for the next time:
the Manila message was STAGED, bucket=mbd_inbound, body fetched (9224
chars), and "261070: NO tracking row carries this ref". Present, read, and
thrown away by us — not a delivery problem, which is what I had concluded
twice and been wrong about twice.

FIX: when the subject carries no signal, read the body — which is where the
booking names its shipper, and which attach_bodies has already populated by
the time this gate runs. The body is deliberately the WEAKER signal: only a
SUBJECT tag overrides the thread-level exclusion, so a forwarded digest that
merely mentions Hilmar still cannot claim another customer's MDOLX. 7 tests
from the real subjects; non-vacuity proven (revert -> 2 fail).

WHAT THIS DOES NOT EXPLAIN, and is still open: the quote/rate responses
(OL-USA RESPONSES stays empty, matches 286/379). Bookings and quotes are
separate paths; this fixes the wins. Suite 2801 passed, ruff clean.

### 2026-08-12 (10) — MEASURED: the mailbox is now read whole, and the
### recent replies are genuinely not in it

The fire on f7b32b19 (all fixes in, --days-back 21), verbatim:

  sweep read 5666 message(s); oldest reached 2026-07-22T18:21:52Z
    (window floor requested 2026-07-22T18:21:15)   → the WHOLE window
  total unique: 5789 (5421 from the sweep, 368 added by $search outside it)
  NEW staged records: 18 — mbd_inbound: 18      ← zero new rate responses
  Rate-response matches: 286/379                ← unchanged
  QC-077: 13

So with complete visibility into every message in Michael's OL mailbox for
21 days, there are NO OL quote replies to the Jul 23 - Aug 12 asks. Not a
scanning gap: we now read all 5666. The dropped senders are other clients
(TTS 493, Hoogwegt 83, Numidia 62, Hilldrup 64, dewittmove, 2ship) plus OL
staff on other clients' work — correctly dropped, because Lonny is not on
those messages.

This is exactly what Michael diagnosed himself: "the team stopped copying
the group email address for bookings sent to lonny and then lonny's
approvals are not going to group but to the individual email that sent
them". Those replies live between an OL individual and Lonny and never
reach this mailbox at all. He is fixing that at OL. The participant rule
(9) means the pipeline will pick them up the moment either the group or
Michael is back on the thread — no code change needed then.

Corrections to my own record, both directions: entry (6) said "not in the
mailbox we read" and was retracted by (7) as wrong — it was PARTLY right
about this window but for the wrong mechanism (I blamed mailbox access;
the cause is a dropped CC). And (7) stands on its own merits regardless:
the $search ceiling was a real, separate defect that was hiding recent mail
independently of the CC break. Both were true at once, which is why fixing
either alone changed nothing visible.

ALSO FIXED: the coverage warning I added in (7) fired on a 37-SECOND gap
during a complete 5666-message read. Pagination ending on its own means
Graph had nothing older; the guard is the only real incompleteness signal.
Coverage is still printed every run. A warning that fires on success is the
same crying-wolf that let the ceiling hide for weeks.

Suite 2784 passed, ruff clean.

### 2026-08-12 (9) — the real cause, from Michael, and the durable fix

Michael, after correcting my model of who does what: "export pricig doesn't
book cargo.. mbd oceanbooking shared books cargo, they send the options and
the pricing to the client normally then lonny books". Noted and wrong on my
part — entry (8) below reasoned from the assumption that the export pricing
desk answers rate requests. It does not.

THE ACTUAL CAUSE, found by Michael, not by this pipeline: "the team stopped
copying the group email address for bookings sent to lonny and then lonny's
approvals are not going to group but to the individual email that sent them".
So the options-and-pricing mail that used to arrive from
MBD_OceanExportBookingShared now arrives from whichever OL person sent it,
and classify() keyed on a three-address whitelist — group mailbox, Lonny,
Reno. Everything else was dropped. The ask then aged out as Not Quoted while
OL was answering normally. He is fixing the copying process at OL.

FIX — stop identifying OL correspondence by a roster of individuals. An
@ol-usa.com sender WITH LONNY ON THE MESSAGE (to or cc) is OL↔Hilmar
correspondence, bucketed exactly as the shared mailbox is: lane-shaped
subject → mbd_rate_response, anything else → mbd_inbound. Identity comes
from the tenant plus a participant, neither of which a staffing change can
drift out of. Requiring Lonny is also the guardrail that keeps every other OL
client out — he is Hilmar's buyer and nobody else's, so Numidia/Hoogwegt/TTS
mail (3537 correctly-dropped messages on this fire) still cannot enter.
The report no longer silently depends on OL's copying discipline holding.

REVERTED from (8): MBD_Export_Pricing and caren.tobel are NOT unconditional
quote senders — that came from my incorrect model. They are now covered by
the participant rule only when Lonny is actually on the message. What stands
from (8) is EXCLUDED_SENDERS being empty: a mailbox-scan rule
(config.json ingest_scope.mailboxes_excluded, "stop searching idealx, ignore
MBD_Export_Pricing" — about which MAILBOXES TO READ) had been applied as a
sender filter, discarding OL mail arriving into the mailbox we do scan.

10 tests: the direct-to-Lonny quote, cc-only, non-lane subjects, and three
negative cases (OL mail without Lonny, non-OL sender with Lonny, Lonny's own
mail still classifying as Lonny). Suite 2784 passed, ruff clean.

### 2026-08-12 (8) — WE WERE DELETING OL'S QUOTES ON ARRIVAL
### PARTIALLY SUPERSEDED by (9): the pricing-desk-quotes premise was wrong.

Michael: "same bullshit ... ol responded to everything ... they are in my
mailbox where they always have been since day one". All true, and this is
the cause. Not the matcher, not access, not $search alone.

refresh_stage.classify() held EXCLUDED_SENDERS = {MBD_Export_Pricing@ol-usa.com,
caren.tobel@ol-usa.com} — "never stage from these senders even if they appear
in the search". That is the OL EXPORT PRICING DESK: the people who answer
Lonny's rate requests, an address on this report's own distribution list, and
the sender used in this repo's own OL-body test fixtures. Every quote they
sent into the mailbox we scan was discarded before classification. The ask
then aged out as Not Quoted, which is what W31/W32 have been showing.

PROVENANCE — a scope rule applied one layer off. config.json
`ingest_scope.mailboxes_excluded`, from Michael 2026-04-30: "stop searching
idealx, ignore MBD_Export_Pricing". That instruction is about WHICH MAILBOXES
TO SCAN AS A SOURCE — the key is literally named mailboxes_excluded. The code
turned it into a sender filter. Same shape as every other defect found today:
the rule was right, the layer was wrong.

FIX: EXCLUDED_SENDERS is now empty; both addresses move to
OL_QUOTE_ONLY_SENDERS (unconditional, same reasoning as Reno — they quote
rather than book, and their subjects do not follow the shared mailbox's
"Re: <origin> to <dest>" shape). q3 derives from that set, so the fetch side
follows automatically. Cross-client bleed is handled where it already is:
ingest's out_of_scope gate (325 numidia / 26 agridairy / 64 other_client
dropped on the 2026-08-12 fire) and the lane+thread matcher. config.json
keeps the mailbox list for its real purpose plus a note that it is NOT a
sender filter. 7 tests, including one asserting the two lists can never again
contradict each other.

'
### 2026-08-12 (7) — I WAS WRONG. It is $search, and it is a code defect

Michael: "they are in my mailbox ... where they always have been since day
one and toh have access to my ol emakl". Correct on both counts. Entry (6)
below concluded the replies were not in the mailbox we read; that
conclusion was WRONG and is retracted. What it actually proved is narrower:
the replies are not in our STAGED data. I extrapolated from "not staged" to
"not in the mailbox" and sent Michael to OL for a process fix he does not
need.

THE EVIDENCE WAS IN THE FIRE LOG THE WHOLE TIME:
    query 'lonny-flow':       got 275 results
    query 'hilmar-bookings':  got 275 results
    query 'ol-quote-senders': got 275 results
Three semantically unrelated queries cannot each match exactly 275
messages. Graph stops paginating $search at a service-side ceiling that
sits BELOW our own 500 cap — so `truncated` stayed False and
_warn_search_cap never fired. And $search ranks by RELEVANCE and cannot be
combined with $orderby (documented in _warn_search_cap since June), so the
275 kept were an arbitrary slice: 357 of the 599 unique results were
PRE-CUTOFF. The ranker handed back mostly old mail and dropped the current
week.

WHY IT "WORKED WEEKS AGO AND THEN DIDN'T" (Michael's question, and the
right one): nothing changed in the mechanism — $search + the 500 cap
predate the squashed base (2026-06-25). What changed is the DATA. While
fewer than ~275 messages matched a query, the slice covered everything
including today. As the mailbox's matching history grew past the ceiling,
coverage rotted from the NEWEST end, invisibly, because no count ever
looked wrong and QC never had a rule for "the ranker is lying". The
Aug-10 fire already showed the symptom — QC-008: "stage_emails latest
received is 71.4h old". Adding q3 on Aug 11 could not help: each query
independently hits the same ceiling.

FIX — stop asking a ranker for the truth. list_messages_since() sweeps the
window with $filter=receivedDateTime ge <cutoff> + $orderby
receivedDateTime desc: date-ordered, deterministic, complete — pagination
ends because the WINDOW ends. It runs FIRST as the primary intake in every
mailbox; the $search queries remain only as a supplement for pre-cutoff
mail a thread may need, deduped on imid. A failed sweep is ::error::, not
a quiet fallback to the defective path. And the detector that failed is
replaced: identical result counts across unrelated queries are now
reported as the service-ceiling fingerprint they are.

7 tests incl. non-vacuity (drop $orderby → the date-order test fails) and
a request-level assertion that no $search appears in the sweep. One
pre-existing test (test_multi_mailbox_read) legitimately caught the new
loop and was updated to assert its stated intent — failure SURFACED, other
mailbox still runs — rather than the severity of one call site.

Suite 2771 passed, ruff clean.

### 2026-08-12 (6) — the NQ rows are an ACCESS finding, not a code defect
### RETRACTED by entry (7) above — the replies WERE in the mailbox.

Michael: "ol responded to everything." Measured, twice, read-only
(diag-matching runs 1 and 2, both reconciling exactly with the verification
fire's own line "Built 339 rate_requests" / "Rate-response matches:
286/378"):

RUN 1 — the matcher is NOT dropping them. Fate of all 378 rate responses:
286 matched, 56 no_destination, 36 no_candidate_matched. And the decisive
negative: 35 unquoted asks examined, ZERO with a same-lane OL reply
anywhere in stage inside 14 days. The 36 no_candidate_matched are all
April-June duplicates on threads already quoted (already_quoted 335,
ask_after_reply 262 across candidates) — normal, not the recent weeks. The
56 no_destination are CARRIER NEWSLETTERS swept in by the broad from:
sender query: "Maersk: Flash sale", "*GRI ALERT* EVERGREEN EUROPE", "YML
TAEB New Bunker", "MSC Middle East Rates", "CMA Week 30 TP Newsletter",
"CARRIER EQUIPMENT REPORT". Not quotes at all.

RUN 2 — the thread walk. For each of the 35 unquoted asks, every staged
message in its own conversation AFTER the ask, any bucket, pre-gate.
Result: 12 threads have NO message after the ask; the other 23 contain
ONLY lonny_outbound — Lonny asking again days later. Not one OL message
in any of the 35 threads.

VERDICT: OL's replies to those asks are not in the mailbox this pipeline
reads, in any form. Not misfiled, not misparsed — absent. refresh_stage
authenticates as /me (Michael's mailbox; OL IT declined the app-only Entra
registration 2026-06-10, see daily.yml), and it warns on every run that it
cannot read MBD_OceanExportBookingShared. Lonny puts Michael on the ask,
so the ask is visible; OL replies to Lonny WITHOUT Michael, so the reply
is invisible. Both "OL responded to everything" and "no reply exists in
our data" are true at once. No matcher, parser, or query change can
conjure mail this account never received — the fix is access or process at
OL, and it is named for Michael rather than papered over here.

CORRECTION to entry (2): "the intake fix WORKS — 107 mbd_rate_response
staged on its first fire" overstated it. Those 107 include the newsletter
noise above; the fix genuinely closed the fetch/keep drift for Reno's
address, but it did not restore quote coverage for the recent asks,
because those replies never reach this mailbox. Stated plainly because the
earlier line reads as a win it did not earn.

### 2026-08-12 (5) — verification results + the last rolling win

VERIFICATION FIRE (run 31611357523, send_to=test, days_back=21) — measured
by diag-weekly on the state it pushed:
  - W31: 13 requests → 0 Q&L, 13 NQ (was 13 phantom Q&L). Every row
    honestly q=0/no-response.
  - W32: 12 requests → 1 GENUINE Q&L (req Aug-3, real OL resp Aug-4),
    11 NQ (was 12 phantom Q&L).
  - The three Aug-11 asks: PENDING, no carrier, no rate (were fabricated
    Yang Ming $797 / CMA CGM $725 / ONE $505 in the CEO email).
  - QC-077: 49 → 13. Zero "PATCH PND/Q&L" fabrication lines in the log.
  - QC-066 clean; wins render in their April weeks.
The [VERIFY] email is in Michael's inbox; reports remain off pending his
approval to restore crons.

FOUND BY THE SAME DIAG, FIXED: the last rolling win. The operator-
corrections applier re-runs every fire (rebuild wipes its fields) and
appended a fire-time WIN→WIN entry each time; core.win_event_date reads
the last →WIN entry, so stand_260905's Jul-9 booking re-dated to "today"
daily. Two-sided fix: win_event_date ignores self-transitions (from==WIN
to==WIN is a touch, not a win event), and the applier only writes a
history entry when the status actually changes. 2 tests.

NEW WATCH ITEM from the fire: QC-009 now warns lonny_reply zero-in-7d
(Lonny simply hasn't replied this week — watch, not fix). QC-057's 3
dropped RFQs unchanged (pre-existing; DIAG lines name the three subjects).

### 2026-08-12 (4) — one unfetchable message must not kill the fire

The first verification fire (run 31609735248) died in refresh_stage: 46 of
47 bodies fetched, ONE Graph GET failed (an Evergreen GRI ALERT blast), and
`return 0 if body_failures == 0 else 1` exited 1 under bash -e — pipeline
never ran, no email. The rule was also a permanent trap: a fetch-failed
message never lands in the bodies file, so it retries every run — a message
deleted from the mailbox would fail every fire forever. New rule
(body_fetch_exit_code, tested): non-zero ONLY when fetching is dead
(failures with zero successes — the broken-auth signature); partial
failures warn per-message and proceed, the staged record keeps its retry.
Same principle as the 2026-07-30 snapshot-backup lesson: a safety net must
never hold the client report hostage. Verification fire re-dispatched after
merge.

### 2026-08-12 (3) — verify_only: the resume path (Michael: "go")

Third gate state between hard-stopped and live, in daily + weekly +
liveness (tests keep the three agreeing): schedule and send_to=full stay
blocked exactly as under "true"; the ONLY opening is a manual dispatch with
send_to=test — Michael alone — so the gated pipeline proves itself on a
real fire before anything client-facing resumes. Crons stay out (the
pairing test now treats verify_only as a no-triggers state). Also:
daily.yml gains a days_back dispatch input (default 14) so the
verification fire can run --days-back 21 and pull W31's real OL replies
(Jul 27-31 asks — their responses predate the 14-day window) back into
stage. 4 test changes/additions. Resume remains: flag "false" + crons
restored in one PR, only after Michael approves the verification email.

### 2026-08-12 (2) — the rest of the phantom machine: carrier/rate mining gated

Root cause of the 49, from the Aug-12 fire's own log (run 31602529593, on
aa39f16): at 13:42:41 pre-patch QC says "✅ QC-077 every quoted row has a
response_timestamp"; at 13:42:43 patch_carriers PASS 1 prints "PATCH PND
req_73be1541f11b -> Yang Ming @ $797" (+ CMA CGM $725, ONE $505 — the exact
three "quotes" in the email the CEO read, on requests OL had not yet
answered); at 13:43:10 post-patch QC errors "QC-077: 49 rows". The aa39f16
fix guarded the TIMESTAMP stamps but not the CARRIER/RATE mining — so the
fabrication kept running, now undated, and QC-077 surfaced it to the staff
list.

Two vectors, both in patch_carriers, both now gated by the same
core.quote_evidence_ok predicate as the stamps (OL-authored AND post-ask):
(1) _discover_full_quote_from_bodies parsed ANY source_imids body — on a
rebuilt row that is Lonny's ask, carrying the previous rate sheet quoted
below it; (2) _find_related_rate_response's conversation-id join — Lonny
re-uses threads, so "the rate response in this conversation" is routinely
LAST cycle's sheet, sent before this ask existed. Also deleted
_discover_carrier_from_bodies: zero callers, duplicated the mining loop
ungated. Audited and left alone: ingest's rate-response matcher (already
enforces req≤resp on classified OL responses — the legitimate path),
the booking/WIN paths (MDOLX evidence), qc_selfheal's quoted-reconcile and
QC-056 carrier heals (read the row's own fields — honest once the writers
are). 6 new tests incl. non-vacuity (gates removed → 3 fail).

ALSO MEASURED, same log: the intake fix WORKS — the ol-quote-senders query
staged 107 mbd_rate_response records on its first fire (7-day bucket ZERO →
63). And the win re-dating fix held: the emailed table shows the April wins
back in April weeks. What remains wrong until a fire runs THIS fix: the 49
undated phantom quotes and W31/W32's fabricated Q&L rows. W32 asks (Aug 3-7)
have their real OL replies in stage now; W31 (Jul 27-31) replies predate the
14-day window — a --days-back 21 refresh would recover them if wanted.

Noted, not yet fixed: QC-057 (3/343 Lonny RFQs dropped, no destination
parse) — pre-existing, unrelated to the fabrication.

### 2026-08-12 — HARD STOP: all reports off (Michael, after the CEO reply)

The Aug-12 staff email (first fire on the aa39f16 fixes) surfaced QC-077's
warning banner — "49 further quotes are recorded with a rate or carrier but
no response time" — to the full distribution. Carrie Murphy King (OL USA
CEO) replied asking why 49 quotes "are not being recorded" and how the
analysis can be trusted. Michael: "turn off all reports going out until you
finally figure out where you made system changes in the last month that the
report is all wrong still."

DONE, both halves per the 2026-08-03 lesson (a flag alone lets an
already-spawned run send): cron triggers removed from daily.yml and
weekly.yml, HILMAR_REPORTS_PAUSED="true" in daily.yml + weekly.yml +
liveness.yml (tests enforce the three agree and fail any half-pause). The
hard stop blocks manual dispatch too — zero goes out by ANY trigger until
the flag flips back. Also fixed en route: liveness's auto-recover step now
stands down on reason=paused instead of dispatching a no-op daily run every
paused weekday ≥10 AM ET.

WHAT THE 49 ARE (for Carrie's question): rows whose carrier/rate were mined
out of Lonny's OWN emails by the recovery heals (the phantom-quote machine,
see 2026-08-11 below). The aa39f16 fix stopped the fabricated response
TIMESTAMPS — so those rows became "quote with no date" and QC-077 flagged
them honestly, straight into the staff email. The half-fix made the rot
visible before removing it: carrier/rate/quoted fabrication from ask bodies
was NOT yet gated, only the timestamp/rate stamp sites were. Diagnosis of
the remaining fabrication sites is the open work item; reports stay off
until the rows are honest.

To resume when fixed: restore the cron lines in daily.yml + weekly.yml and
flip the three flags back to "false" in the same PR.

Michael, on the staff email's W24-W33 table: "this is absurd.. your data is
consistently wrong. where it used to be correct before we did formatting
changes." Root-caused with measurements (diag-weekly runs 1-3, the Aug 7-11
fire logs verbatim, and git diffs), then fixed. Three defects, one story.

FIRST, THE ATTRIBUTION, settled with diffs: NOT the formatting changes. The
aggregation code that built the Aug 10 artifact is byte-identical to HEAD
(`git diff 344eb1c..HEAD -- gen_email.py gen_dashboard.py` is EMPTY); the
Aug 4-7 commits touching the weekly renderer are provably styling-only, and
the one predicate commit in the window is a pure widening. The wrongness is
in the ROWS. Also corrected en route: the screenshotted table is
gen_email.py's "This Week vs Last Week" (staff email), not the dashboard —
red = Q&L, orange = NQ, purple = Pending, wins credited to the REQUEST week.

DEFECT 1 — OL QUOTE INTAKE STRUCTURALLY DEAD SINCE ~JUL 24. Measured:
mbd_rate_response staged per ET day is ZERO Aug 3 through Aug 11, across TWO
fires that ran WITH the Aug-7 Reno classify fix and a 14-day window. Cause:
classify can only keep what a query FETCHED, and both queries miss her —
q1 needs Lonny ON the message, q2 needs the shared mailbox as sender. The
Aug 10 fire's own log confirms: its only 3 new rate responses were Jul 30-31
replies that happened to carry Lonny. FIX: graph_queries() gains q3, built
FROM OL_QUOTE_ONLY_SENDERS (`from:reno.gurusinghe@ol-usa.com`), so the fetch
side and the classify side share one sender list and cannot drift.

DEFECT 2 — THE PHANTOM-Q&L MACHINE. All 25 W31/W32 requests read quoted=1
LOSS (PRICE / ETD_MISS / UNDIFFERENTIATED) with response_timestamp == request
date, SAME DAY, in a window with zero staged replies. The chain, every step
individually defensible as "recovery": Lonny re-uses Outlook threads, so his
new ask quotes the PREVIOUS rate sheet below it; the heals read bodies by
source_imids — the ask itself, on a rebuilt row — and mined a carrier, then
reconciled quoted=True (qc_selfheal:1066), then recovered last cycle's rate
(_heal_missing_rate), then stamped the ask's own send time as the response
(_stamp_response_time_from_bodies; patch_carriers._stamp_response_time is the
mirror). Real OL replies used to arrive first and overwrite all of it — "it
used to be correct" — until defect 1 removed them and the fabrications became
the only quotes. FIX: core.quote_evidence_ok, ONE rule at all three sites: a
message may evidence an OL quote only if OL WROTE it (@ol-usa.com; missing
sender fails CLOSED — an undated quote QC-077 flags honestly beats a dated
fabrication) and it POSTDATES the ask (resp <= req is QC-066's impossible
ordering). Guarded at both stamps and at the rate-mining body pick.

DEFECT 3 — ROLLING WIN DATES. Nine wins carried win_event=2026-08-11; eight
are APRIL bookings. ingest's prior-WIN restore (_merge_prior_win_into) called
record_transition with no `at`, which defaults to NOW — and the restore runs
EVERY fire for a win whose booking never re-enters the stage window, so those
wins re-dated to "this week" daily, forever. NOT the booking-rank change, as
was feared and said aloud; the history reasons name the restore. FIX: the
restore carries the prior row's ORIGINAL →WIN transition verbatim; lacking
one (including priors already poisoned by the old behaviour, excluded by
reason prefix), it dates from booking → response → ask evidence. now is the
last resort, once, not daily.

WHAT REMAINS WRONG UNTIL A FIRE RUNS THE FIXED CODE: the stored rows. Ingest
rebuilds everything from stage each fire (verified: prior tracking-data is
read ONLY to preserve WINs), so the next fire re-derives W31/W32 honestly
(unquoted rows show as NQ/pending — or genuinely quoted, if q3 finds real
Aug replies within the 14-day window) and re-dates the eight wins to April.
Numbers cannot be promised in advance; the diag re-run after that fire is
the evidence.

Also fixed en route: diag-weekly run 2 died one line after "9 row(s)" — bare
sorted() on (date, dict) tuples compares dicts on tied dates. A diagnostic
that dies on the rows it exists to explain has negative value.

Guard: tests/test_phantom_quotes.py (15 tests, verbatim production shapes).
Non-vacuous: with all three fixes neutered, 11 of 15 fail. Older fixtures
that built body records without sender_email were updated to match the real
writer (fetch_bodies always records it); the guard fails closed on sender-
less records BY DESIGN.

Suite 2736 passed, 0 failed. ruff clean.

### 2026-08-10 (9) — the tightening's blast radius on real mail: ZERO

diag-bookings run 8 (`4f56570`, window 2026-04-01 → today, the whole store):

  staged rows                        : 1277  (kept 1008 / out-of-scope 269)
  MDOLX with an out-of-scope sibling : 45
  bookings BEFORE tightening         : 66
  bookings AFTER  tightening         : 66
    no longer bookings               : 0
    newly bookings                   : 0

NOT ONE BOOKING CHANGES VERDICT across four months of real staged mail. The
risk I raised — a stricter client gate silently deleting a real win — does not
materialise on any data we hold. That is the outcome to want from a tightening:
it closes the hole without touching live business.

WHY ZERO, stated honestly rather than sold as a win: 45 MDOLX numbers DO have
an out-of-scope sibling, and the thread rule would have removed any booking
they produced. None produced one, because the existing per-row body check
already catches every such thread in THIS dataset — the Agri Dairy / Numidia
text is quoted into the fetched bodies. The thread-level rule is therefore
insurance for the case the body check cannot cover: a thread where no body was
fetched for the message that names the other customer. It is not currently
doing work; it is standing where the July leak came through.

A CORRECTION TO RUN 7, caught before it was reported as final. Run 7 said FOUR
bookings were lost — MDOLX260741, 260821, 260874, 260991, all Numidia/Agri
Dairy moves loading at the Hilmar plant, all classified 'origin_city' and none
'tag'. The qualitative read held (no tagged Hilmar win was touched) but the
COUNT was wrong: two of the four say "NUMIDIA" in their own subject, so
ingest.main's per-row filter removes them at line ~1713 and they never reach
collect_bookings in production at all. They were never in production's
"before". The comparison had run the old gate over UNFILTERED rows.

Same class of error as the diagnostic that judged the gates without attaching
bodies, and the second time in one session: a tool that models the pipeline in
a different ORDER than the pipeline runs reports a number about a system that
does not exist. Fixed, guarded by a test, and re-run — which is where the zero
came from.

Suite 2708 passed, 0 failed. ruff clean.

### 2026-08-10 (8) — full tightening of the client gate, per Michael

REVERSED FROM ENTRY (7). I recommended leaving the gate loose; Michael: "i want
full tightening." His call. The risk I raised does not evaporate by being
overruled, so the work is built to avoid it rather than to accept it: a
stricter client gate fails by making a REAL WIN quietly stop existing.

FIRST, A CORRECTION TO MY OWN EARLIER FINDING. Entry (5) reported MDOLX260821
as "admitted by every gate, no win row" and called it correct-by-accident. It
was correct BY DESIGN: ingest.py:295-301 already carries an Agri Dairy rule
added on 2026-07-01 for this exact leak, Michael's words at the time being
"only moves booked by Lonny are Hilmar the client". The pipeline had been
excluding it via the BODY check ever since. My diagnostic reported "admitted"
because IT NEVER ATTACHED BODIES — out_of_scope_reason reads text_body, and
ingest.main attaches bodies before filtering while diag_bookings did not. A
tool that models the gates with less evidence than the pipeline gets does not
model the gates. Fixed; guarded.

WHAT TIGHTENING IS NOT: requiring a "// HILMAR" tag. Hilmar Ingredients is
physically in Hilmar, California, so a genuine move can name the lane and never
the customer. That rule would drop real wins, which is the concern from entry
(7) and the reason it is not the design.

WHAT SHIPPED, three parts:

  1. hilmar_signal() — the substring test is now a CLASSIFIER returning
     'tag' | 'origin_city' | None. Any tag-shaped mention wins outright;
     "Hilmar, CA" alone is recognised as a place.
  2. out_of_scope_mdolx() — the check is now THREAD-LEVEL. One MDOLX is one
     shipment, so it has one paying customer; if any message carrying that
     number names a different customer, the number is theirs. Per-row
     filtering could never see this — it only caught the Agri Dairy sibling
     when that sibling's text happened to be quoted into a fetched body.
     Derived from the DROPPED rows at the filter, because two lines later they
     are gone and the signal would be permanently empty (guarded by test).
  3. The customer list now covers who is actually in this mailbox — Hoogwegt,
     Erno Laszlo, Brisar — sourced from senders observed in the live stage on
     2026-08-10, not invented. Word-bounded so none can fire on a port or
     carrier substring. `la[sz]{1,2}lo` because BOTH spellings are live
     (ernolaszlo.com and ERNO-LAZLO-SHIPMENT-REPORT); my first regex matched
     only one and my own test caught it. Deliberately NOT added: "Solis" (a
     report feed, not a counterparty) and tts-worldwide / Quality Forms
     (internal commission mail, no MDOLX).

THE ESCAPE HATCH THAT KEEPS IT SAFE: an explicit "// HILMAR" tag OVERRIDES the
thread verdict. Hilmar and another customer can share a thread — same plant,
same week — and a stated customer tag is not discarded on a sibling's say-so.
Only ambiguous origin-city-only rows defer to the thread.

NEVER SILENT. Every thread-level drop prints its MDOLX, reason and subject, and
main() logs the count. A booking count that falls with no line explaining why is
indistinguishable from the pipeline breaking.

MEASURED, NOT ASSERTED SAFE. diag_bookings now runs BOTH gates over the same
staged rows and names every booking whose verdict moved, with hilmar_signal for
each drop so "tag or city?" — the fact that says whether a drop was safe — is
on the page. Blast radius on live mail is not yet run; that is the next step
and it gates nothing else.

Guard: tests/test_client_gate_tightened.py (16 tests), real production
subjects. Verified non-vacuous by disabling the thread-level check — 2 failed,
including the stand_260821 reproduction. The must-NOT-regress half is tested
too: the same lane-only subject IS admitted when no sibling condemns it, and a
tagged row survives a condemned thread.

Suite 2707 passed, 0 failed. ruff clean.

### 2026-08-10 (7) — the client gate stays loose, and now it is actually tested

DECISION, recorded so a later session does not "fix" it: the Hilmar client gate
`is_hilmar = "HILMAR" in subject.upper()` (ingest.py:677-679) is NOT tightened.

Michael asked whether it should be, after MDOLX260821 — Agri Dairy cargo whose
subject reads "Hilmar, CA to La Guaira, Venezuela" — passed it. Two measured
facts decide it:

  1. Hilmar Ingredients is IN Hilmar, California. A real Hilmar booking can
     name the lane and never the customer, so requiring a "// HILMAR" tag would
     drop real wins. The loose gate is deliberate.
  2. The specificity already lives in the NEGATIVE gate. out_of_scope_reason()
     names the other customers Hilmar ships for and returns "agridairy" on the
     sibling message that gave MDOLX260821 away. Positive test loose, negative
     test specific — tightening the positive side attacks the wrong half and
     buys nothing the negative side does not already do.

WHAT WAS ACTUALLY BROKEN — the test coverage, and it was mine. AST-verified
over every scripts/*.py: NOTHING in the live tree ever writes `is_hilmar`, so
`row.get("is_hilmar")` is always None and the substring test always runs. But
tests/test_booking_email_choice.py — which I wrote earlier today — hard-coded
`"is_hilmar": True` in its fixture, short-circuiting ingest.py:679 on all seven
of its tests. The gate that decides which customer's bookings enter Hilmar's
data had NO test exercising it. Any tightening would have shipped untested.

Fixed: the fixture no longer sets the key, so those seven now run the real
predicate. tests/test_hilmar_client_gate.py (8 tests) pins the gate itself with
the verbatim production subjects, including the decision above as an assertion
rather than prose. Verified non-vacuous by re-planting `is_hilmar: True` — the
guard fires.

RE-LEARNED THE REPO'S OLDEST LESSON, again. The first draft of that guard was a
substring scan for `"is_hilmar": True`, and it matched the docstring EXPLAINING
the fix — failing on a file that was already correct. An identifier in prose is
indistinguishable from an identifier in code. Now an AST walk over dict keys.
That is at least the seventh time this session.

ALSO — QC-030's comment said "≥80% of WIN/Q&L rows". The code thresholds at
ERROR <70 / WARN <85 and selects WIN/LOSS. Neither number nor status set
matched. Corrected; a comment that misstates the gate it sits on is how an
operator reads a passing 82% as a failure.

Suite 2691 passed, 0 failed. ruff clean.

### 2026-08-10 (6) — OL writes a sail date two ways; the client report knew one

Chasing QC-027's ETA at 93.3% (307/329) — the only field left under 95% once
Carrier was resolved. What it turned up is worse than a completeness number,
and it was reaching the customer.

MEASURED FIRST (diag-qc027 runs 2 and 3, live data):

  rows missing eta_offered: 22
    2026-04  1/83   2026-05  4/82   2026-06  16/83 (19%)   2026-07  1/69
  why:  21  every OTHER graded field parsed — one cell missed
         1  partial parse
  bodies on disk for the first 8: 0

All 22 carry ETD + vessel + rate, so the rate table WAS read. And not one of
them has a cached body left, so the parser cannot be re-run over them — which
killed both of my standing hypotheses and sent me to the write and read paths
instead.

THE CLIENT-FACING DEFECT. Both date forms are in the live data, weeks apart:

  stand_260769   etd=22-Apr-26   eta=26-May-26      <- d-Mmm-yy
  req_5d2685f3…  etd=1-Jul-26    eta=2026-07-25     <- ISO

and THREE parsers were reading that one field:

  share_intel.py:255        _parse_loose_date   internal feed   loose
  gen_client_email.py:326   _iso_date           LONNY'S EMAIL   STRICT
  gen_client_weekly.py:184  _iso_date           LONNY'S WEEKLY  STRICT

`_iso_date` is `strptime(s[:10], "%Y-%m-%d")`. So a `26-May-26` ETA is truthy
for QC-027 — it counts toward the 93.3% as PRESENT — and invisible to
"Currently in transit", which drops any row whose ETA will not parse. The
internal intel feed saw those shipments; the customer's report did not. One
fact, three readers, and the one that reached Lonny held the wrong one. This is
the fourth instance of that shape this session (sent_ts/sent, imid/
internetMessageId, LOSS/quoted).

DECISION — one predicate: `core.offered_date()`. Every reader of an OL-offered
ETD/ETA goes through it; share_intel's private format table is deleted and its
extra fallbacks (non-zero-padded M/D/YY) folded in, so nothing any of the three
could parse is lost. Verified against the real strings before writing a line of
it. It returns None for a yearless "Jul 25" rather than strptime's 1900 default
— a fabricated sail date sorts to the FRONT of the client's transit table, and
None is the honest answer. I caught that one by running the function, not by
reading it; my first docstring claimed the behaviour the code did not have.

TWO WRITE-SIDE LOSSES, both fixed:
  * ingest.py:1255 assigned eta_offered UNCONDITIONALLY. Lonny re-uses Outlook
    threads, so a second rate response with no ETA nulled a good one. A stated
    ETA still wins; the fallback only fires when the new email says nothing.
    The correct shape was already two lines below —
        best["pol"] = rt.get("pol") or best.get("pol") or _dpol
    ports preserved, table fields not. Deliberately NOT extended to
    carrier/rate/etd: a re-quote replacing those is correct behaviour, and ETD
    is at 100% so nothing there is measured as broken.
  * patch_carriers.py:633 — BACKFILL_KEYS could always WRITE eta_offered, but
    the gate deciding whether to GO LOOKING named only etd/vessel/rate. Every
    one of the 22 rows carries all three, so every one failed the gate and
    never triggered the sibling lookup. Gradeable, but unreachable.

ALSO: `rt.get("eta")` in ingest is dead in production — scripts/body_parser
emits `eta_offered` and never `eta` (that key belongs to the src/hilmar
mirror). fetch_bodies.py:208 already hedges both; ingest now matches it rather
than relying on `parsed` having bubbled the value up.

WHAT THIS DOES NOT CLAIM. Whether these three fixes move the 93.3% is unknown
and unknowable from here: the 22 rows have no bodies left to re-parse, so they
cannot be repaired retroactively. The fixes stop NEW rows from being lost the
same three ways. Said plainly rather than implied.

Guard: tests/test_offered_dates.py (13 tests), built from the verbatim
production strings. Verified non-vacuous by reverting the client renderer to
_iso_date — 2 failed, including the end-to-end one asserting a future
`15-Sep-26` ETA reaches the client's table.

Suite 2683 passed, 0 failed. ruff clean.

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
