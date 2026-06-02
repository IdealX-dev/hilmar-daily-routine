# Revenue / Product / Strategy — Refreshed Audit

*Tuesday 2026-06-02, written for Michael's Sunday-night-on-iPhone read*

The 2026-05-31 revenue audit was right about Tuesday Brief being the highest-leverage feature. Two days of shipping and one day of QC archaeology have changed the picture enough that the next 10 days look different than the 12-week plan implied. This is the refresh.

---

## 1. What changed since 2026-05-31

The original audit named four open gaps. Status check:

| Gap (2026-05-31) | Status (2026-06-02) | Notes |
|---|---|---|
| Auto-chase orphaned (scheduler not wired) | **Fixed** — PR #15 (auto-chase schedule), wrapper now fires the chase loop | Live but unobserved; need a week of data before judging |
| Loss-reason chart missing from dashboard | **Fixed** — PR #16, with one big caveat | Chart shipped, but #21 reveals the 94% PRICE bucket was an extraction artifact, not signal |
| Parser drift on carrier extraction | **Partially fixed** — wrapper heartbeat (PR #18) + step classification (PR #19) catch silent failures; pre-vs-post-patch QC split (PR #20) stops false alarms | The accuracy gate at 95% is holding; the parser itself didn't change |
| Tuesday Brief unbuilt | **Templates written, renderer not built** — TUESDAY_BRIEF_TEMPLATES.md is 4 lenses ready to wire | This is now the biggest unshipped-value lever in the repo |

Net read: foundation is meaningfully stronger than 48 hours ago. Observability tightened (#18, #19, #20), the auto-chase that the original audit flagged as orphaned is now firing, and the loss-reason story — the one the original audit said would be the conversational hook with Lonny — is going to land *more honest* once #21 merges. That last point is more important than it sounds (see §4).

What did NOT change since 2026-05-31: zero Tuesday Briefs sent. The single most valuable feature in the original audit is still vapor in production. That's the bottleneck.

---

## 2. Sunday-night priorities for THIS week

Today is Tuesday 2026-06-02. Win-rate this week is **0%** (0 wins / 4 Q&L / 2 NQ across 6 RFQs). Michael needs a quick win, and the quick win is not in the data — it's in the relationship.

Three things, ranked by leverage, all doable before Sunday:

**(a) Ship the Tuesday Brief renderer this week — manually.** Templates are done. Don't wait for `gen_tuesday_brief.py` to be production-grade. Build the smallest possible renderer (see §3) and send Lens 1 to Lonny **next Tuesday, Jun 9**. That puts a competitive-intel artifact in Lonny's inbox before the next monthly review. Even if it's hand-edited, even if the data is a 30-day snapshot rendered by a half-finished script, getting one real Tuesday Brief into Lonny's hand is the single most consequential thing Michael can do this week. Time: 6-8 hours over Wed-Fri.

**(b) Make a phone call to Lonny — not an email.** Win-rate is 0% this week. Four Q&Ls in two days is a number Lonny will see when he opens the dashboard Tuesday morning. Pre-empt the question. A 10-minute Wednesday call with a single sentence — "I want to make sure you know I'm watching the same data you are, here's what I'm chasing" — is worth a year of dashboard polish. The dashboard is for evidence; the relationship is in the voice. Time: 30 minutes including prep.

**(c) Ship PR #20 and #21.** They are open; they are small; they are correct. #20 stops false-alarm Sentry noise on stale-vs-logic-bug confusion. #21 replaces the PRICE catch-all with UNDIFFERENTIATED. Both fix the "94% PRICE was wrong" finding (§4). Merging these before Sunday means next week's dashboard tells the truth. Don't let these sit. Time: 1 hour to review + merge.

Everything else (RateIntel decoupling, dashboard polish, new QC checks) is below the line this week. The 0% win-rate is a relationship problem, not a product problem.

---

## 3. Tuesday Brief shipping plan

Should this week's brief go out today (Tue Jun 2)? **No.** It's already 2 PM ET by the time most of this gets written, there's no renderer, and a rushed first brief sets the wrong precedent (templates exist but no production data has flowed through them). A bad first send is worse than no first send because Lonny's expectations get anchored.

**Target: Lens 1 manually sent next Tuesday Jun 9.**

Smallest possible path to that target:

1. **Wed Jun 3 (3h):** Write a `scripts/gen_tuesday_brief.py` that handles ONLY Lens 1 (Trade-region pulse). No lens auto-selection logic. No multi-lens dispatch. Just a single function that reads `tracking-data-v2.json`, computes the region-pulse table from `core.aggregate_trade_regions`, fills the placeholders that already have backing data (`region_rows[*]`, `capacity_carriers_*`), and stubs the ones from `DATA GAPS` with sensible defaults or omits the sentences. Output: `reports/tuesday-brief-2026-06-09.html` and `.txt`. Skip Jinja2 — use `str.replace()` on the template constants. Ship dumb, ship today.

2. **Thu Jun 4 (2h):** Run it against current production data. Hand-eyeball the output. Confirm the region pulse numbers are believable. Fix the obvious wrong stuff (no need for tests yet — this is a one-shot).

3. **Fri Jun 5 (1h):** Send the rendered HTML to **michael.deitchman@idealx.us only** (NOT Lonny). Read it on iPhone. If it looks dumb, fix it. Do NOT send to Lonny yet.

4. **Mon Jun 8 (1h):** Re-render against fresh Monday data. Re-review. Hand-edit if needed — copy the HTML out of the file, paste into Outlook, edit the two or three sentences that need a human touch, save as draft.

5. **Tue Jun 9 morning (15 min):** Send to Lonny from `michael.deitchman@ol-usa.com`. Subject: whatever the actual data warrants — NOT the template's placeholder. CC: nobody. Watch for reply.

What you're explicitly NOT doing in this plan:
- Auto-selection logic between 4 lenses (do Lens 1 only)
- Auto-send wiring into the wrapper (manual send, week 1)
- QC-051 through QC-054 (write them in week 2 once you know what the renderer actually does)
- Jinja2 templating (str.replace is fine for one lens)
- The `min_quotes_for_send` floor gate (eyeball it)
- Multi-week production schedule

The fastest possible path from "templates exist" to "first brief in Lonny's hand" is 8 hours of work spread over 5 days, with the renderer ugly but real. Don't gold-plate the renderer this week. Once Lonny replies, you'll know which features matter and can productize accordingly.

---

## 4. The "94% PRICE was wrong" finding — what it means for Lonny

This is the most important strategic finding in today's audit, and the original revenue audit didn't have it.

The previous narrative — "94% of losses are PRICE" — was a heuristic artifact of the loss-reason classifier defaulting to PRICE when no other reason matched. PR #21 introduces UNDIFFERENTIATED as the catch-all instead. Post-merge, expect PRICE losses to drop to **30-40%** of Q&Ls, with UNDIFFERENTIATED capturing the rest.

This changes the conversation with Lonny in a real way.

**Old story (wrong):** "We're losing on price. Carrier rates are the bottleneck. OL needs to negotiate harder with CMA / MSC / ONE."

**New story (more honest):** "On the deals where we know we lost on price, we lost on price — about 1 in 3 Q&Ls. On the rest, **we have no clue why Lonny picked someone else**. He sent us a quote, we responded, he booked elsewhere, and the email thread doesn't tell us why. That's not a rates problem. That's a **feedback-loop** problem."

For Michael's conversation with Lonny, this rephrases the ask. The Q3 quarterly-review pitch was going to be: "OL needs better rates." That pitch dies on contact with the actual data. The new pitch is:

> "Lonny, I can tell when you book us and I can tell when you don't, but I can't tell why on most of them. Would you be willing to drop a one-word reason in the booking-confirmation email when you go with someone else? Even 'transit', 'equipment', or 'incumbent' would let me triage what to negotiate harder on. Right now I'm guessing."

That ask costs Lonny ~5 seconds per quote. It would give Michael a **feedback signal** that no other broker has — and would make the daily tracker uniquely valuable to Lonny because Lonny's input shapes the data.

The 94% PRICE finding was bad data telling a clean story. The 30-40% PRICE + 60% UNDIFFERENTIATED is true data telling an *uncomfortable* story — and uncomfortable stories with action items are how Michael graduates from "one of 8 brokers" to "the broker who tells me what's actually happening."

For OL ops internally, the same finding routes differently: "Stop assuming rate is our biggest problem. We don't actually know what our biggest problem is. We need to either (a) get Lonny to label losses, or (b) build a parser that infers it from booking-confirmation emails." Both are feasible. Both are cheaper than wholesale renegotiating carrier contracts.

---

## 5. RateIntel Week 1 reality check

The productization plan said Week 1 is "decouple Hilmar branding, parametrize client config." Is that still right?

**Mostly no. The order should shift.**

The plan was written assuming the Hilmar pipeline was stable and the next problem was multi-tenancy. Today's audit makes clear the Hilmar pipeline still has *substantive* improvements to make that have nothing to do with tenant-decoupling:

- The loss-reason classifier just got rebuilt (#21). It needs a week of real production data before we know if UNDIFFERENTIATED is bucket-collapsing too much.
- Tuesday Brief isn't shipped. The original audit called this the most valuable single feature. Multi-tenanting a feature that doesn't exist in production yet is premature decoupling.
- The 0% week-to-date win rate may turn out to be statistical noise OR may indicate a real parser issue with this week's bookings. Until that's clarified, multi-tenancy is moving deck chairs.

The right Week 1 for the next 10 days is **not** the productization-plan Week 1. It's:

1. Ship Tuesday Brief Lens 1 (§3) — the highest-value feature you don't have.
2. Watch UNDIFFERENTIATED for 5-7 days post-#21 to confirm the bucket isn't a different kind of catch-all.
3. Have the §4 conversation with Lonny — if he agrees to label losses, the loss-reason story changes again in 2-3 weeks.

Multi-tenant decoupling can wait until **Week 3 (Jun 16+)** at the earliest. The cost of waiting is zero — there are zero prospective tenants in pipeline right now. The cost of skipping Tuesday Brief to do decoupling is the original audit's primary finding: you're leaving the highest-leverage relationship move on the table.

If you want a single sentence: **don't ship multi-tenant infrastructure for a product whose flagship weekly feature is still unbuilt.**

---

## 6. Underused features still in the pipeline

Four candidates flagged in the question:

**`gen_rate_intelligence.py`** — Produces the rate-negotiation cheat sheet + carrier-cooling alerts. **Underused but salvageable.** The "Evergreen silent 14d" finding came from here, and it's actionable. Decision: **promote.** Specifically — surface the cooling-carriers list at the top of the daily email (one line: "Carriers we'd normally see who haven't quoted this week: Evergreen (14d)"). Five minutes of work; would have been the lead story this week if it were there.

**`gen_carrier_scorecard_pdf.py`** — Per-carrier negotiation scorecards. **Genuinely underused.** Has Michael actually opened one of these in the last 30 days? If yes, fine. If no — and the honest answer is probably no — this is producing artifacts nobody reads. Decision: **fix or kill.** Concrete check: when did Michael last reference a carrier scorecard PDF in a conversation with OL ops or a carrier rep? If "never," kill the scorecard generation step entirely and reclaim the 30-60 seconds it adds to the daily fire. If "rarely," fold the carrier-scorecard data INTO the Tuesday Brief Lens 3 instead of producing standalone PDFs nobody opens.

**`gen_weekly_summary.py`** — Not in the 16-step pipeline (the pipeline ends at step 15: `sync_to_quote_tracker.py`). The fact that this script exists in `scripts/` and isn't wired into the daily fire is a smell. Decision: **investigate or kill.** Either it's vestigial from an earlier design and should be deleted, or it's the foundation of what Tuesday Brief is becoming. Spend 15 minutes Wednesday confirming which. If it's not the Tuesday Brief seed, delete it.

**`share_intel.py export`** — Pushes to SHARED/client_intelligence/hilmar/. **The output IS being consumed** — by ol-quote-tracker. So this isn't underused; it's invisible-but-load-bearing. Decision: **leave it alone**, but add a one-line health check to the daily audit email confirming the most recent export landed in the SHARED folder. Right now if `share_intel.py` silently regresses, nobody would notice for weeks.

Net: promote rate-intelligence visibility, kill or fold carrier scorecards, investigate weekly_summary, monitor share_intel. Reclaim ~90 seconds per daily fire and one moving part.

---

## 7. Lonny relationship moves — highest leverage operator-action

The data this week paints a clear operator-action picture, no code required.

**The find:** Evergreen silent 14 days. 15 lanes losing >50%. CMA CGM is 66% of carrier concentration. 0% win-rate this week.

**The single highest-leverage move:** Call Evergreen first, Lonny second, and CMA CGM third — all this week, in that order.

- **Evergreen call (Tuesday afternoon or Wednesday morning).** "Hey, we haven't seen quotes from you on Hilmar lanes in two weeks. Did the relationship change? Did we do something? Are you still in market on these lanes?" Information cost to OL: 15 minutes. Information value: huge — either re-opens the relationship, identifies a structural change Michael needs to know, or confirms the carrier is genuinely cooling and Michael can build the story around it.

- **Lonny call (Wednesday).** Per §2(b). Don't lead with the 0% win-rate; lead with "I'm chasing Evergreen for you, and I noticed you've got 15 lanes where we're losing more than half — want me to dig into any of those specifically?" That positions Michael as proactive instead of reactive.

- **CMA CGM conversation (Friday).** 66% concentration is brittle. One CMA pricing change and the Hilmar book gets hit hard. Talk to CMA's account rep — not to negotiate price, but to understand their posture on Hilmar's lanes for Q3. If CMA is planning a rate increase, Michael needs to be diversifying carrier mix NOW, not in August.

None of this requires code. None of this requires the Tuesday Brief. All of it would make the next 7 days dramatically more productive than another sprint of dashboard work.

The original revenue audit said the biggest gap was the lack of weekly proactive outreach. The fix isn't a script — it's three phone calls.

---

## 8. What I might be wrong about

Calling out three load-bearing assumptions from the revenue audit and productization plan that look shakier today:

**(a) "Tuesday Brief is the single most valuable feature."** This was the headline finding of the 2026-05-31 audit and it's repeated in the productization plan. Today's data nudges this — the §4 finding about the loss-reason classifier suggests that **getting Lonny to label losses might be a higher-leverage product feature than any weekly intel email.** The labeling ask, once granted, creates a permanent data asymmetry between OL and Lonny's other brokers. Tuesday Brief is still valuable; it might not be the single most valuable thing. The audit may have been right about format but wrong about content.

**(b) "OL-USA partner referral is the highest-ROI Y1 channel."** The productization plan treats the OL-USA partner-network conversation in Week 9 as the single highest-EV hour of the 12 weeks. That assumes OL-USA leadership sees RateIntel as additive to OL's volume rather than competitive with OL's services. Today, with Michael's contractor relationship to OL-USA as the source of all current cash, that conversation has more downside than the plan acknowledged. The plan's risk #1 ("OL-USA pulls Michael's contracting work") is correctly identified but the mitigation ("frame as broker-side") may be insufficient. **Worth pre-testing the OL-USA reaction before committing the 12-week plan to it.** A 30-minute conversation with one OL-USA leader BEFORE Week 9, sounding out the partnership idea, would change the plan's expected value substantially.

**(c) "$1,500/month per key account is cheap enough that any broker losing one deal will pay for a year."** This is the productization plan's pricing thesis. It's plausible but completely untested. The actual evidence is one Hilmar relationship where the buyer (Lonny) doesn't pay anything — OL-USA does. The willingness-to-pay for the broker-side version of this product is a guess. **Before Week 5 (the managed-runner build), Michael should validate price with at least 2 actual broker founders** — not "would you consider paying X" but "here's the dashboard, what would you pay for this monthly?" Pricing assumptions that don't survive 2 conversations should be reset before infrastructure gets built around them.

---

## 9. The "should we build it?" filter for next 10 days

Ranked by (impact × certainty / effort). One line each. Higher score = higher build priority.

| Rank | Item | Score | Why |
|---|---|---|---|
| 1 | Tuesday Brief Lens 1 renderer + manual send Jun 9 | **High × High / Medium** | Highest-leverage shippable feature, templates already done, smallest path to Lonny in §3 |
| 2 | Surface cooling-carriers (Evergreen) in daily email subject/header | **Med × High / Low** | 1 hour of work, would have led this week's story |
| 3 | "Labeling ask" prep for Lonny — draft the proposal, send Thursday | **High × Med / Low** | If Lonny says yes, transforms loss-reason data forever |
| 4 | Kill or fold `gen_carrier_scorecard_pdf.py` | **Low × High / Low** | Reclaims fire time, simplifies pipeline |
| 5 | Investigate `gen_weekly_summary.py` (delete or absorb) | **Low × High / Low** | Cleanup; clears mental overhead |
| 6 | Tuesday Brief Lens 2/3/4 renderers + auto-selection | **High × Low / High** | Defer to week of Jun 16 — get Lens 1 traction first |
| 7 | Multi-tenant `tenants/hilmar/config.yaml` decoupling (productization Week 1) | **High × Low / High** | No prospective tenants yet; premature for next 10 days |
| 8 | Real rate-shop integration (Xeneta/SeaRates) | **Med × Low / High** | Productization plan explicitly excludes this; respect the plan |
| 9 | COVERED-status parser (new request status) | **Low × Med / Med** | Vaguely useful, not load-bearing |
| 10 | Stripe Billing scaffolding, OAuth admin-consent UI, status page | **High × Low / Very High** | All productization Phase 2 — zero customers means zero urgency |

Net: **only items 1-5 should ship in the next 10 days.** Everything from 6 down is the productization plan's future and should wait until Tuesday Brief is producing relationship traction.

---

## 10. The kill list

Specific things Michael should stop doing or building:

**(a) Stop iterating on dashboard polish.** The dashboard is *adequate*. Lonny doesn't read it daily. The marginal hour spent on a new KPI tile or a better tab layout produces less value than the marginal hour spent on the §7 phone calls. Hard rule: no dashboard CSS changes until Tuesday Brief has shipped 4 weeks in a row.

**(b) Stop pre-emptively building productization infrastructure.** Zero prospective tenants. Multi-tenant config, OAuth admin-consent, Stripe Billing, status pages — all of this is real work in service of customers who don't exist. The productization plan said Week 1 was tenant-decoupling; the data says wait. Don't write a single `tenants/` directory file before Jun 16.

**(c) Stop generating artifacts nobody opens.** Specifically: the per-carrier scorecard PDFs (`gen_carrier_scorecard_pdf.py` output). If Michael honestly hasn't opened a carrier scorecard PDF in the last 30 days, kill the generation step. The pipeline is faster, the audit email is shorter, and one moving part disappears.

**(d) Stop treating Sentry as something to babysit.** PRs #20 and #21 just spent significant effort improving Sentry signal quality. Trust the gates. Stop manually triaging Sentry events more than once a day. Set an alert for ERROR-severity QC findings and otherwise ignore the dashboard.

**(e) Stop saying "PRICE is 94% of losses."** That number is wrong (§4). Replace it in every conversation — internal, with Lonny, with OL ops — starting tomorrow. Don't carry a known-bad statistic into the next OL meeting.

**(f) Stop writing more 4000-word audit docs every two days.** This one is meta. There are now multiple audit docs in `docs/` and `docs/audits/2026-06-02/`. The marginal value of audit #7 written next week is approximately zero. Audit cadence should drop to monthly until either (a) revenue exists, or (b) the pipeline materially changes. The Sunday-night read should be the data, not another doc telling you what the data means.

---

## If Michael only does 3 things this week, do these

**1. Build a one-shot `gen_tuesday_brief.py` for Lens 1 only and send the first Tuesday Brief to Lonny on Jun 9.** Detailed plan in §3. The templates are done. The data is sitting in `tracking-data-v2.json`. The renderer is 6-8 hours of work spread across Wed-Fri. Don't auto-select between 4 lenses; don't wire it into the pipeline; don't write QC-051 through QC-054. Just render Lens 1, hand-edit if needed, and send it. This is the single highest-leverage move in the repo right now.

**2. Make three phone calls this week — Evergreen Tuesday/Wednesday, Lonny Wednesday, CMA CGM Friday.** Detailed in §7. The 0% win-rate this week is a relationship signal, not a product signal. None of these calls require code. All of them produce information no script can generate. Michael's competitive moat against the other 7 brokers in Lonny's queue is being the human who calls Lonny on a 0%-win-rate week before Lonny notices.

**3. Merge PRs #20 and #21, then draft the "labeling ask" email to Lonny for Thursday.** PRs are ready, replace the bad PRICE-94% narrative with truth. Then write a 4-sentence email to Lonny asking him to drop a one-word reason into his booking-confirmation responses when he goes with another broker. Frame it as "this would help me serve you better on the next quote." If Lonny says yes, the loss-reason data permanently differentiates OL from his other brokers — a far bigger product win than anything in the productization plan's first month.

Everything else — multi-tenant decoupling, OAuth flows, dashboard polish, productization Phase 1 — waits until June 16th at the earliest. Tuesday Brief shipping + relationship moves + truth-in-data are this week's whole job.

---

*End of refresh. Next audit no earlier than 2026-07-01 unless the pipeline materially changes or revenue arrives.*
