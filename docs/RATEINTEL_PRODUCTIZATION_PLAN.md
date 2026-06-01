# RateIntel — 12-Week Productization Plan

*Author: Claude (for Michael) · Drafted: 2026-05-31 · Status: Sunday-night decision draft*

---

## Executive Summary

RateIntel is the multi-tenant SaaS evolution of the Hilmar Daily Tracker: a per-(broker, key-account) competitive-intelligence pipeline that ingests the broker's RFQ ↔ response email thread with one of their key shippers, classifies wins/losses/no-responses, parses competitor carrier intel, and ships a daily branded scorecard email + dashboard + PDF to the broker's account team. The Hilmar build already has the architectural backbone — parametric tenant slugs in `share_intel.py`, a task-keyed `model_router.py`, a 95%-gated parser, a 50-check QC engine, and Sentry observability — so productization is a decoupling exercise, not a rewrite. Over 12 weeks, Michael decouples the pipeline from Hilmar-specific assumptions (Weeks 1–4), wraps it in a non-technical-broker onboarding flow with per-tenant MSAL, billing, and white-labeling (Weeks 5–8), and closes 5 paying tenants at $1,250/month average via OL-USA partner referrals and LinkedIn outbound to small-to-mid freight brokers (Weeks 9–12). Conservative model: $6,250 MRR by end of month 12. Aggressive: $30K MRR. The bet is that every freight broker has 1–15 Hilmar-shaped accounts they'd pay $500–2,000/month to retain — and that Xeneta's $30–100K/year enterprise SaaS leaves the entire SMB-broker market unserved.

---

## Product Spec — What RateIntel Is

### The core promise

> *"Every weekday at 10 AM, RateIntel sends your account team a one-page scorecard showing exactly which RFQs you won, which you lost, which carrier beat you on each loss, and what to do about it today. Your shipper sees a branded version. Your competitors see nothing."*

### What's in the box (all tiers)

- **Daily branded scorecard email** at a tenant-configured fire time (default 10:00 AM in the tenant's timezone)
- **Interactive HTML dashboard** with clickable KPI tiles, tabbed status views, lane/carrier filters
- **Client-facing PDF** (6 pages) — for the broker's own use OR forwarded to the shipper-account contact
- **Per-carrier scorecard PDFs** — used in carrier rate negotiations
- **Rate-negotiation cheat sheet** (`gen_rate_intelligence.py`) — auto-generated, current-week
- **Auto-chase soft nudges** for stale RFQs (off by default; opt-in per tenant)
- **Operator audit email** — daily QA report visible only to the broker admin (mirror of `gen_improvements_report.py`)

### Tiers

| Tier | Target | Price (USD/mo) | Account caps | Features included | Excluded |
|---|---|---|---|---|---|
| **Starter** | Solo broker, 1 key account | $500 | 1 account, 1 broker | Daily scorecard email, dashboard, PDF, carrier scorecards, QC alerts to broker admin | White-labeling, multi-account rollup, API access, custom branding beyond logo+color, SSO |
| **Pro** | Mid-broker (5–25 reps), 3–8 key accounts | $1,500 | 5 accounts, 5 broker users | Everything in Starter + multi-account rollup dashboard, white-label PDFs (logo+colors), cross-account carrier rollups, Slack/Teams webhook | API access, SSO, custom report templates |
| **Enterprise** | Larger broker (25+ reps), 10+ key accounts, IT-engaged | $4,000+ (negotiated) | 15+ accounts, unlimited users | Everything in Pro + REST API, SSO (SAML/OIDC), custom PDF templates, on-prem option, named CSM (= Michael) | Nothing material — Enterprise is "we'll do what you need" |

### Explicitly excluded (do not build these in v1)

- **Carrier-direct integrations.** No SeaRates, no SeaIntelligence, no Project44. Rate intel comes from the broker's own RFQ thread — that's the whole moat.
- **Rate forecasting.** Xeneta does this; we don't try to be Xeneta. We do *competitive intel on actual deals*.
- **TMS/OMS replacement.** We are downstream of Magaya/CargoWise/MercuryGate. We sit on top of email. Period.
- **Mobile app.** Email + responsive web dashboard cover 95% of the use case. iOS app is a Year-2 conversation.
- **Multi-language UI.** English only through Y1.

---

## Pricing Model — The Math

### Anchors

| Tool | Price | Audience |
|---|---|---|
| **Xeneta** | $30K–100K/year | Enterprise BCO + large NVOCC. Forecasting, not deal intel. |
| **Freightos WebCargo** | Transaction-based | Booking platform, not analytics. |
| **MercuryGate** | $30K–80K/year TMS | Operations, not competitive intel. |
| **Magaya** | $5K–25K/year | Operations + accounting. |
| **Salesforce + custom build** | $50K–500K | Whatever you can configure into it. |
| **Spreadsheet-by-account-manager** | $0 | The status quo. The real competitor. |

### Pricing thesis

Charge **$1,000–2,000 per (broker, key-account) pair per month**, NOT per seat. A broker with 5 key accounts on Pro pays $1,500/mo and that covers all 5. A broker with 15 accounts pays Enterprise pricing ($4K+).

Why per-account, not per-seat:
- Aligns price with value (a key account is a 7-figure revenue line; $500/mo to protect it is rounding).
- Avoids the "we'll just give one login to the whole team" haircut.
- Scales with broker's portfolio without punishing seat-count.
- Easy to upsell: broker adds account → another invoice line.

### Floor / ceiling

- **Floor: $500/mo Starter.** Below this it's not worth the support cost of one tenant. Brokers below this price point should self-onboard or churn.
- **Ceiling: $10K/mo Enterprise.** Above this, Michael is selling consulting + custom work, not SaaS. Don't chase deals that need full custom development in Y1.

### Billing mechanics

- **Stripe Billing** for cards (Starter, Pro).
- **Invoice + ACH (Bill.com)** for Enterprise.
- **Annual prepay 2 months free** (i.e., 10x monthly = 1 year) — improves cash flow during bootstrap.
- **No free trial.** A 14-day "pilot at full price, refund if not satisfied" instead — qualifying friction is the goal.

---

## GTM Motion — One Person, Six Months

Michael is alone. There is no SDR, no marketing team, no growth hacker. The GTM motion must be ruthlessly minimal-effort-per-touch.

### Three channels, ranked by ROI for a one-person founder

1. **OL-USA partner referral (highest leverage).** Michael is already inside OL-USA. Every broker OL-USA does business with is a candidate — and OL-USA's account team will introduce Michael IF he frames it as "I'm helping our broker partners retain their accounts" not "I'm building a competitor." This is the single most important channel. Spend Week 9–10 negotiating the referral framework with OL-USA leadership directly.

2. **LinkedIn outbound + content.** Michael posts 2x/week (Tuesday + Thursday) with: (a) anonymized win/loss vignettes from Hilmar, (b) "what your shipper isn't telling you" think-pieces, (c) screenshots of the dashboard (redacted). DMs to broker founders/VPs ops at firms with 5–50 reps. Target: 50 DMs/week, 2-week cadence, expect 2–5% response rate.

3. **Freight forwarder podcasts.** *Freight Caviar*, *The Freight Project*, *NMFTA Cyber Podcast*, *FreightWaves Mary O'Connell*. One podcast appearance is worth ~200 LinkedIn DMs in qualified inbound. Aim for 1 podcast guest spot per month from Week 10 onward.

### Channels DROP for Y1

- **Paid ads (Google/Meta/LinkedIn).** CAC will be brutal for a $1,500 ACV with no brand. Skip.
- **Trade shows (TPM, JOC).** Expensive, low-conversion, not how SMB brokers buy.
- **Cold email at scale.** Apollo/Outreach setup eats a week and brokers ignore cold email. LinkedIn DMs convert 5–10x better.
- **Channel partnerships with Xeneta/Freightos.** Premature — they won't take you seriously until you have 20+ tenants.

### The 30-second pitch

> "I built a tool that reads your account team's RFQ email thread with one of your key shippers and sends them a daily one-page scorecard: wins, losses, which carrier beat you on each loss, and what to do today. It's running for one shipper relationship right now and the broker's win rate went from X to Y. Want to see the actual scorecard from yesterday?"

Show, don't tell. The dashboard sells itself in 90 seconds.

---

## The First-5-Customers Play

### Ideal customer profile (ICP)

- **Broker size:** 5–50 reps. Big enough to have 3+ key accounts; small enough that the founder/COO is the buyer (no committee).
- **Account size:** Each "key account" books 200–2,000 TEU/year or generates $500K–$5M/year in broker revenue.
- **Sophistication:** Currently tracking deals in a shared Excel sheet OR a CRM (Salesforce, HubSpot) that nobody updates. Pain is real, current tooling is bad.
- **Geography:** US-based brokers first (Outlook/Microsoft 365 is dominant, MSAL flow works). UK/AU after Month 12.
- **Trigger:** Lost a deal in the last 90 days they wish they'd seen coming. Every broker has this story; just ask.

### The 5 specifically

1. **Two from OL-USA's partner network.** Michael asks Alan Baer (OL-USA leadership) for two warm intros to brokers OL-USA has carrier relationships with. Frame: "These brokers' accounts retention helps OL-USA's volume too." Close: Week 10.
2. **One LinkedIn-inbound.** From the content + DM motion in Weeks 9–11. Likely a broker that DMs Michael "saw your post about win-rate, can we talk?". Close: Week 11.
3. **One referral-from-Hilmar.** Lonny Upfold knows other freight buyers. Once the Hilmar relationship is rock-solid (and it is), Michael asks Lonny for ONE intro to a peer at another mid-size food/beverage importer who buys from a small broker. The shipper makes the intro to the broker. Close: Week 11–12.
4. **One from a podcast.** The Freight Caviar episode from Week 10 produces 3–5 inbound. Close one. Close: Week 12.
5. **The wildcard.** Some channel Michael didn't predict — Reddit, a slack community, an old contact. Don't plan for it; do leave time to chase it. Close: Week 12.

If only 3 of 5 close by Week 12, that's still validation. If 0 close by Week 12, kill the project or pivot. If 5+ close by Week 12, hire the first CS contractor in Month 4.

---

## What to DROP From the Current Pipeline Before RateIntel

These are Hilmar-specific tax that will rot the product if carried forward.

| Item | Why drop | Replacement |
|---|---|---|
| `assets/branding/` hardcoded Hilmar+OL logos | Multi-tenant means white-label per (broker, account). | `assets/branding/{tenant_slug}/` resolved at render time. Default falls back to RateIntel branding. |
| `config.json` → `distribution.full_list` (10-recipient invariant in QC-022) | Each tenant has their own distribution; the "10" is meaningless to others. | Per-tenant distribution lists; QC-022 generalizes to "distribution must match `tenants/{slug}/config.yaml` exactly." |
| `idealx.us` email signers, `michael.deitchman@idealx.us` audit recipient | Hardcodes Michael as the operator. Wrong for multi-tenant. | `tenants/{slug}/admin_email` from per-tenant config. Michael becomes the *RateIntel* admin who gets cross-tenant ops alerts, not the per-tenant admin. |
| `MDOLX` booking-ref format hardcoded throughout | OL-USA-specific reference prefix. Other providers use different formats. | Provider-configurable booking-ref regex in `tenants/{slug}/provider.yaml`. |
| `MBD_OceanExportBookingShared@ol-usa.com` hardcoded responder | OL-USA-specific. | Per-tenant `provider.responders[]` list. |
| `ingest_scope.mailbox_folder = "Hilmar Tracker"` | Implies one folder per pipeline. Multi-tenant means one folder per tenant. | `tenants/{slug}/mailbox_folder`. |
| `auto_chase.recipient = lupfold@hilmaringredients.com` hardcoded | Same problem. | Per-tenant. |
| `tracking-data-v2.json` at repo root | Multi-tenant means N tracking files. | `tenants/{slug}/tracking-data.json`. Repo-root file becomes a legacy symlink for backcompat through Month 6, then removed. |
| `secrets/token-cache.json` (single MSAL cache) | One MSAL identity per pipeline run; can't serve N tenants. | `secrets/tenants/{slug}/token-cache.json` per tenant, OR a delegated-auth flow where the tenant's admin owns their cache. |
| `share_intel.py` hardcoded `CLIENT_ID = "hilmar"` | Already parametric in folder layout; needs CLI arg. | `python share_intel.py export --tenant {slug}`. Trivial fix. |
| `gen_improvements_report.py` recipient hardcoded to Michael | Same issue. | Per-tenant admin email. |
| Conditional Access on OL-USA source IP | Cloud PC ties Michael to OL-USA's IP. Multi-tenant requires tenant-owned auth. | See Week 5: delegated MSAL flow where each tenant authorizes RateIntel against *their* tenant directory. |
| The "Hilmar" name in 200+ places across scripts | Cosmetic but corrosive. | Find/replace to `tenant_slug` parameter; keep `HILMAR_*` env vars only where they exist for backcompat. |

What does NOT need to drop:
- The QC engine. Generalizes cleanly — each check just needs the tenant slug threaded through.
- The model router. Already tenant-agnostic.
- The Sentry observability. Add `tenant_slug` as a tag on every event; you get per-tenant dashboards for free.
- The parser. Body shapes are similar across freight brokers; expect 80% reuse, 20% per-tenant tweaking via prompt examples.

---

## Phase 1 (Weeks 1–4) — Decouple From Hilmar

**Phase goal:** Make the pipeline run end-to-end for a second tenant (manually onboarded) without breaking the Hilmar pipeline. Architecture must be multi-tenant in *fact*, not just in design.

### Week 1 — Tenant config + path discipline

**Goal:** Replace every Hilmar-hardcoded path/identifier with a tenant-keyed lookup.

**Deliverables:**
- `tenants/` directory at repo root with `tenants/hilmar/config.yaml` containing every Hilmar-specific value currently in `config.json` + scattered constants.
- `src/rateintel/tenant_config.py` — a `load_tenant(slug) -> TenantConfig` API. Pydantic model with strict validation.
- Migration of `scripts/run_pipeline.py` to accept `--tenant {slug}` flag; all 16 steps thread the slug through.
- `scripts/share_intel.py` accepts `--tenant {slug}` (currently hardcoded `CLIENT_ID = "hilmar"`).
- `tracking-data-v2.json` moves to `tenants/hilmar/tracking-data.json`; symlink at old location for backcompat.
- Unit tests: 20 new tests in `tests/test_tenant_config.py` covering missing keys, schema validation, default fallback.

**Acceptance criteria:**
- `python scripts/run_pipeline.py --tenant hilmar --dry-run` produces byte-identical output to the pre-refactor run.
- All 519 existing tests still pass. New tests bring total to 539+.
- Grep for `"hilmar"` (case-insensitive) in `scripts/` returns < 10 hits, all justified.

**Dependencies:** None. Start Monday.

**Risks:** Cloud PC fires daily — refactor must NOT break Friday's production fire. Mitigation: do the refactor on a branch, gate on a full dry-run match, merge to main only after a successful Monday fire on the branch as `--tenant hilmar`. If anything breaks, the symlink rollback path is one git revert.

**Effort:** 28 hours (4 days × 7h).

---

### Week 2 — Brand + distribution decoupling

**Goal:** Per-tenant branding (logo, colors, signer) and per-tenant distribution lists.

**Deliverables:**
- `assets/branding/{tenant_slug}/` directory convention. Hilmar's existing assets move to `assets/branding/hilmar/`. Default `assets/branding/_default/` is the RateIntel-branded fallback.
- `gen_dashboard.py`, `gen_pdf.py`, `gen_carrier_scorecard_pdf.py`, `gen_email.py` all resolve branding via `TenantConfig.branding_dir`.
- Per-tenant distribution lists in `tenants/{slug}/config.yaml` → `distribution.full_list`, `distribution.test_list`, `distribution.iteration_lock_to`.
- QC-022 generalizes: enforces distribution matches `tenants/{slug}/config.yaml` invariant (count, no external domains, iteration-lock if set), no longer hardcoded to 10 recipients.
- A second tenant fixture: `tenants/_fixture_acme/` with synthetic data + a different logo + a 4-recipient distribution — used in tests.

**Acceptance criteria:**
- Running pipeline against `_fixture_acme` produces a dashboard with the fixture's logo, an email with the fixture's signer, and QC-022 passes against the fixture's 4-recipient distribution.
- Hilmar's outputs (logo, 10-recipient distribution) are byte-identical to pre-refactor.
- New tests: 15+ in `tests/test_branding.py` and `tests/test_distribution.py`.

**Dependencies:** Week 1.

**Risks:** PDF rendering with new logo may break WeasyPrint layout (assets dimensioned for Hilmar's logo). Mitigation: budget 4 hours specifically for layout-tuning the default RateIntel logo. Also: branding files in tenant directories will bloat git history if committed; gitignore them and store in a separate `branding-assets/` repo or S3 bucket from Week 3 onward.

**Effort:** 22 hours.

---

### Week 3 — Tenant MSAL + mailbox scoping

**Goal:** Each tenant has their own MSAL identity + their own mailbox-folder scope.

**Deliverables:**
- `secrets/tenants/{slug}/token-cache.json` per-tenant MSAL caches. `outlook_send.py` and `ingest.py` thread `--tenant` through to load the right cache.
- `tenants/{slug}/config.yaml` → `mailbox.account`, `mailbox.folder_name`, `mailbox.ingest_filter` per tenant.
- `python scripts/outlook_send.py auth --tenant {slug}` device-code flow that targets the tenant's mailbox.
- Documentation: `docs/TENANT_ONBOARDING.md` step-by-step for getting a new tenant's MSAL set up.
- QC-023 generalizes: check token freshness per tenant; warn at 60d, error at 80d.

**Acceptance criteria:**
- A second test tenant can authenticate against a second test Microsoft 365 mailbox (Michael's `michael.deitchman@idealx.us` mailbox serves as the second tenant for testing).
- Both tenants' ingest runs in parallel from a single `python scripts/run_pipeline.py --tenant {slug}` invocation, with no token-cache cross-contamination.
- Documentation reviewed by a non-Michael reader (ask a friend who's never seen the codebase to follow it).

**Dependencies:** Week 1.

**Risks:** **High.** MSAL device-code flow assumes a human at the terminal. SaaS scale needs a delegated-auth flow where the tenant's admin authorizes RateIntel once via OAuth consent screen and Michael never sees a token. That's an Azure App Registration with admin-consent + a redirect URI — significantly more work than what Week 3 plans. Flag this for Week 6 as the proper fix; for Phase 1, device-code is acceptable because tenants are manually onboarded. Document the limitation explicitly.

**Effort:** 30 hours.

---

### Week 4 — End-to-end second-tenant dry run + observability

**Goal:** Run the full 16-step pipeline for a second real tenant (synthetic data on Michael's test mailbox) and prove it produces a correct branded scorecard + dashboard + PDF.

**Deliverables:**
- `_fixture_acme` tenant graduates to a real test tenant with synthetic RFQ emails in Michael's `idealx.us` mailbox (10–20 emails simulating a fake broker-shipper thread).
- Full pipeline run for tenant `acme` produces all 16 step outputs in `tenants/acme/reports/`.
- Sentry events tagged with `tenant_slug` — verify in Sentry UI that filtering by tenant works.
- A "second-tenant playbook" doc: every gotcha discovered during Weeks 1–4 documented.
- Phase 1 retro: what assumptions died, what's still hardcoded that we missed.

**Acceptance criteria:**
- `python scripts/run_pipeline.py --tenant acme` runs end-to-end without errors on a synthetic mailbox.
- Hilmar production fire on Friday Week 4 is byte-identical to Friday Week 0 (no regressions).
- Sentry dashboard shows `acme` and `hilmar` as separate filterable tag values.
- At least 3 hardcoded-Hilmar bugs were found during this week and fixed (if you find zero, you weren't looking hard enough).

**Dependencies:** Weeks 1–3.

**Risks:** The parser will likely break on synthetic emails because they don't match Lonny's exact style. This is the point — Week 4 finds the gaps. Budget 6 hours for parser tweaking and treat failures as Phase 1 learning, not Phase 1 blockers.

**Effort:** 26 hours. **Phase 1 total: ~106 hours over 4 weeks.**

---

## Phase 2 (Weeks 5–8) — Productize

**Phase goal:** A non-technical broker admin can be onboarded in <2 hours. Per-tenant MSAL delegated auth works. Billing scaffolding exists. The pipeline runs on managed infra (not Michael's Cloud PC).

### Week 5 — Managed runner + per-tenant scheduling

**Goal:** Move the pipeline off Michael's Cloud PC and onto a managed runner that can serve N tenants at their own fire times in their own timezones.

**Deliverables:**
- A small VM fleet (start with one `t3.medium` on AWS, or a Hetzner CPX21 for cheaper) running Ubuntu 24.04 with Python 3.12.
- `cron` (or `systemd timers`) per tenant: `0 10 * * 1-5 /opt/rateintel/run.sh --tenant {slug}` at the tenant's local 10 AM.
- `deploy/runner_setup.sh` provisioning script — idempotent, can be re-run.
- Secret management: secrets pulled from AWS Secrets Manager (or Hetzner + GitHub Actions secrets if cheaper) at run start, never on disk persistently.
- Sentry per-tenant Cron monitors — each `--tenant {slug}` run has its own heartbeat.
- Hilmar's daily fire continues on the Cloud PC for now (no big-bang migration); the new runner serves test tenants only this week.

**Acceptance criteria:**
- Synthetic `acme` tenant fires from the new runner at 10 AM ET successfully for 5 consecutive weekdays.
- Sentry shows the cron heartbeat from the runner, not from Cloud PC.
- Secrets are not on disk after the run completes.
- Total monthly infra cost < $50 (single VM, 1–5 tenants is fine on one $20/mo box).

**Dependencies:** Phase 1 complete.

**Risks:** OL-USA Conditional Access policy restricts certain mailbox access to OL-USA's IP range. The Hilmar fire may continue to require Cloud PC for that reason (Hilmar's mailbox is OL-USA's). Mitigation: keep Hilmar on Cloud PC indefinitely if needed; new tenants run their own mailbox under their own auth and don't have this restriction. Document this clearly.

**Effort:** 32 hours.

---

### Week 6 — Delegated MSAL OAuth flow

**Goal:** Replace device-code auth with proper OAuth admin-consent. A tenant admin clicks a link, consents, and Michael never sees a token.

**Deliverables:**
- Azure App Registration for RateIntel (multi-tenant) with `Mail.Read`, `Mail.Send`, `MailboxFolder.Read.All` delegated scopes.
- Hosted consent screen at `https://app.rateintel.io/oauth/consent` (Vercel/Cloudflare Pages, ~$0).
- Callback handler that stores the refresh token to the tenant's secret store.
- `outlook_send.py` and `ingest.py` use the refresh token to mint access tokens (no more `device-code` for new tenants).
- Documentation: `docs/TENANT_ONBOARDING.md` updated with the consent-screen flow + screenshots.
- Hilmar stays on device-code through Month 12 (don't fix what isn't broken).

**Acceptance criteria:**
- A test tenant admin can complete consent in <5 minutes without Michael's involvement.
- A new tenant onboarded entirely via the OAuth flow ingests their first day of mail successfully.
- Token refresh works automatically for 30 consecutive days (no manual re-auth).

**Dependencies:** Week 5.

**Risks:** Azure admin consent is the most common onboarding friction in B2B SaaS. Some IT departments will block third-party app consent. Mitigation: have a fallback "service account" mode where the broker creates a dedicated Microsoft 365 mailbox + service account that RateIntel uses with basic auth — slightly less secure but unblocks IT-restrictive customers. Document both flows.

**Effort:** 36 hours. (High because OAuth is a footgun; budget for it.)

---

### Week 7 — Per-tenant config UI + onboarding wizard

**Goal:** A web form where a tenant admin enters their distribution list, mailbox folder, branding, and fire time. Backend writes `tenants/{slug}/config.yaml`.

**Deliverables:**
- Single-page web app (Next.js or just static HTML + a Cloudflare Worker — keep it simple) hosted at `app.rateintel.io`.
- Onboarding wizard: 6 screens (account info, OAuth consent, mailbox/folder pick, distribution list, branding upload, fire-time picker).
- Backend writes the config to a git repo (`rateintel-tenants` — private) which the runner pulls hourly.
- A "dry-run sample" button: posts a synthetic-data tenant fire, emails the admin the sample dashboard within 5 minutes.
- Stripe Billing connected: tenant selects tier on a final billing screen, card on file, subscription starts billing on Day 14 (so the first 14 days are a refundable pilot).

**Acceptance criteria:**
- A non-technical friend (not Michael, not a developer) can complete the onboarding wizard in <2 hours starting from a cold link.
- The sample fire arrives within 5 minutes of the wizard completion.
- Stripe subscription is created with the correct tier price.
- All tenant config is in a private git repo (auditable, recoverable).

**Dependencies:** Week 6.

**Risks:** Scope creep on the UI is the biggest Week 7 risk. Resist it. The wizard must be functional, not pretty. Tailwind defaults + zero custom CSS is fine. If Michael starts polishing pixel-perfectly, kill the polish and ship.

**Effort:** 40 hours (the heaviest week — front-end is slow when you're not a front-end engineer).

---

### Week 8 — Onboarding doc + first non-Michael tenant

**Goal:** A real second tenant (a friend's broker, or a paid pilot at $1 for the month) is onboarded entirely via the wizard. Documentation is tested by a real outsider.

**Deliverables:**
- `docs/CUSTOMER_ONBOARDING.md` — what the customer needs to know before signing up.
- `docs/CUSTOMER_FAQ.md` — top 20 questions Michael expects.
- A status page at `status.rateintel.io` (Cloudflare Workers or `statuspage.io` free tier) showing last-fire-time per tenant from public-safe Sentry data.
- One real second tenant onboarded end-to-end via the wizard. They fire daily for at least 3 weekdays during Week 8.
- A 5-minute screen-recording demo posted on LinkedIn — the wizard + sample dashboard. This becomes the GTM hook.

**Acceptance criteria:**
- Second tenant fires successfully 3 days in a row.
- Onboarding tenant rates the experience 8/10 or higher on a single-question survey.
- The LinkedIn demo gets ≥5,000 impressions (proxy for inbound pipeline quality).

**Dependencies:** Week 7.

**Risks:** The first real tenant will find at least 5 things Michael missed. That is expected and good. Don't promise SLAs in Week 8 — call it "founding tenant" status with the understanding that they're getting white-glove service and the price will go up for tenant 6+.

**Effort:** 24 hours. **Phase 2 total: ~132 hours over 4 weeks.**

---

## Phase 3 (Weeks 9–12) — Launch

**Phase goal:** Close 5 paying tenants. Establish the GTM motion. Make sure tenant 1 (Hilmar's broker-side OL-USA arrangement) is rock solid as the reference customer.

### Week 9 — OL-USA partnership conversation + reference-customer write-up

**Goal:** Get OL-USA leadership's blessing to pitch RateIntel to their broker partners.

**Deliverables:**
- A 1-page memo to Alan Baer + OL-USA leadership: "RateIntel helps your broker partners retain their accounts → which protects OL-USA's volume → here's the partnership ask."
- A signed referral agreement (or at minimum an email "yes, refer freely with these guardrails") with OL-USA on what Michael can and cannot say about OL-USA in pitches.
- A redacted Hilmar case study: `case-studies/hilmar.md` showing the win-rate lift, the QC-saved-the-day moments, the dashboard screenshots (logos redacted/permissioned).
- 2 warm intros from OL-USA to broker partners arranged for Week 10–11 calls.

**Acceptance criteria:**
- Written OL-USA partnership terms in hand.
- ≥2 warm intros scheduled.
- Case study reviewed by Lonny Upfold + OL-USA legal (so nothing in it gets Michael in trouble later).

**Dependencies:** Phase 2 complete.

**Risks:** **This is the highest-stakes conversation of the 12 weeks.** If OL-USA leadership perceives RateIntel as competitive to their service, Michael's contractor relationship is at risk. Mitigation: lead with "this helps OL-USA win more bookings because the broker has better intel" and structure the partnership as a referral fee (5–10% of MRR for referred tenants for first 12 months) so OL-USA has economic alignment. If the conversation goes badly, pivot the channel strategy to LinkedIn-only and accept slower growth.

**Effort:** 16 hours (mostly meetings + negotiation).

---

### Week 10 — Outbound launch + first paid close

**Goal:** Close the first paying tenant (any of the 5 candidates). Ship 50 LinkedIn DMs + 1 podcast spot recorded.

**Deliverables:**
- LinkedIn outbound script + 50 DMs sent to mid-size broker founders/VPs ops.
- 1 podcast guest appearance recorded (Freight Caviar or similar) — air date in Weeks 11–12.
- 4 sales calls scheduled with broker prospects.
- First paid tenant signed at Pro tier ($1,500/mo) or Starter ($500/mo). Cash in Stripe.
- A "founding tenants" pricing locked: first 5 tenants get 50% off for 6 months in exchange for testimonial rights + 30-day pilot grace.

**Acceptance criteria:**
- Stripe shows ≥1 active subscription.
- ≥3 sales calls completed.
- Podcast recording exists.

**Dependencies:** Week 9.

**Risks:** Sales is the skill Michael has the least practice with. Mitigation: use a tight script, record every call (with consent), review every loss within 24 hours. Don't discount below 50% — desperation pricing trains the market that RateIntel is cheap.

**Effort:** 30 hours.

---

### Week 11 — Support process + tenants 2–3

**Goal:** Build the customer-support process (ticket intake, response SLAs, escalation) that one founder can sustain. Close tenants 2 and 3.

**Deliverables:**
- A support intake at `support@rateintel.io` → Linear or Plain (whichever has a generous free tier). Don't build a ticketing system; use one off the shelf.
- SLA published: response within 4 business hours, resolution within 1 business day for P0 (pipeline didn't fire), 3 days for P1 (data wrong), 5 days for P2 (cosmetic).
- A weekly Loom video Michael sends to each tenant: "Here's what your team's intel showed this week, and here's one thing I noticed in your data." (Differentiator vs. set-and-forget SaaS.)
- Tenants 2 and 3 close at Starter or Pro tier.
- Total MRR by end of Week 11: $3,000–4,500.

**Acceptance criteria:**
- ≥3 active Stripe subscriptions.
- Support ticket median response time <4 business hours over the last 30 days (use Plain/Linear analytics).
- 2 weekly Looms delivered to founding tenants.

**Dependencies:** Week 10.

**Risks:** Support load could explode if any of the first 3 tenants have wildly different email formats that the parser doesn't handle. Mitigation: build a per-tenant `parser_overrides.yaml` capability in Week 11 if any tenant's parser accuracy drops below 90%. Don't let the parser gate become a customer-facing failure.

**Effort:** 28 hours.

---

### Week 12 — Tenants 4–5 + 12-week retro + Year-1 plan

**Goal:** Close tenants 4 and 5. Decide whether to commit to Y1 build-out or pivot.

**Deliverables:**
- Tenants 4 and 5 close.
- Total MRR by end of Week 12: $5,000–7,500 (conservative) or higher (aggressive).
- 12-week retro doc: what worked, what didn't, what's the bottleneck for the next 12 weeks.
- Year-1 commitment decision: continue solo through Month 6 OR hire first CS contractor at Month 4 if MRR ≥ $10K.
- Updated `docs/RATEINTEL_PRODUCTIZATION_PLAN.md` with the next 12-week plan.
- A second podcast guest spot recorded.

**Acceptance criteria:**
- ≥5 active Stripe subscriptions.
- MRR ≥ $5,000 (kill criterion: if MRR < $3,000 at Week 12, seriously consider pivoting back to contractor work).
- Year-1 plan committed to.

**Dependencies:** Weeks 9–11.

**Risks:** The hardest week mentally — Michael has been alone for 12 weeks and is exhausted. Mitigation: schedule a real 3-day break before Week 13. Burnout in Month 4 is the #1 way bootstrap SaaS dies.

**Effort:** 30 hours. **Phase 3 total: ~104 hours over 4 weeks.**

**Grand total: ~342 hours over 12 weeks = ~28.5 hours/week of focused build-and-sell time.** Realistic for someone with an existing contractor income still flowing in from OL-USA. NOT realistic if Michael also tries to take on new contractor work.

---

## Risks Ranked by Likelihood × Severity (Top 10)

| # | Risk | Likelihood | Severity | Combined | Mitigation |
|---|---|---|---|---|---|
| 1 | **OL-USA perceives RateIntel as competitive and pulls Michael's contractor work.** | Medium | Catastrophic | **Critical** | Frame as broker-side, not provider-side. Get explicit OL-USA blessing in Week 9 before pitching their network. Have a 90-day cash reserve before launching. |
| 2 | **Parser breaks on a new tenant's email format and accuracy drops below 90%.** | High | High | **Critical** | Build per-tenant `parser_overrides.yaml` by Week 11. Budget 4 hrs/week per tenant for parser tuning in Months 1–3. |
| 3 | **Tenant churns within 60 days because they don't read the daily emails.** | High | High | **Critical** | Weekly Loom from Michael becomes the engagement loop. Track email open rates (1x1 pixel) and intervene at <40% open rate. |
| 4 | **MSAL OAuth admin consent is blocked by tenant's IT.** | Medium-High | Medium | **High** | Service-account fallback flow ready by Week 7. Add a "we'll set up a dedicated mailbox" white-glove option for Enterprise. |
| 5 | **Outlook Graph API rate-limits with N tenants running concurrent ingest at 10 AM ET.** | Medium | Medium | **High** | Stagger fire times by 15 min across tenants. Implement exponential backoff in `ingest.py` (probably already exists; verify). |
| 6 | **Sales cycle is longer than expected — 5 tenants take 20 weeks, not 12.** | Medium-High | Medium | **High** | Have a 6-month cash runway, not 3. If at Week 12 there are 2 tenants instead of 5, treat that as a data point and re-plan, don't quit. |
| 7 | **Michael burns out from 30+ hr/week of new work on top of OL-USA contracting.** | High | High | **Critical** | Hard cap at 35 hrs/week on RateIntel. Take Sundays off entirely. Hire CS contractor at $25K MRR. |
| 8 | **A bad Sentry day during a tenant's first week causes them to churn.** | Medium | High | **High** | First 30 days of any tenant: Michael personally reviews every Sentry event within 4 business hours and emails the tenant with the resolution. |
| 9 | **Competitor emerges (someone else builds the same thing) before Week 12.** | Low | Medium | **Medium** | The moat is the OL-USA channel + the parser tuned on real Hilmar data. Both are hard to replicate. Move fast but don't panic. |
| 10 | **Stripe/billing tax issues (state sales tax, VAT for any non-US tenant).** | Medium | Low–Medium | **Medium** | Stripe Tax handles this for $0/mo until $100K ARR. Enable from Day 1; revisit at $50K ARR. |

---

## Revenue Projection Model

### Assumptions

- **Conservative scenario:** 5 tenants by Month 3, +1 net/month thereafter, average ACV $1,250/mo, 6% monthly churn.
- **Aggressive scenario:** 5 tenants by Month 3, +3 net/month thereafter, average ACV $1,500/mo, 3% monthly churn.
- **Costs (monthly):** Infra $50, Stripe fees ~3.5% of MRR, tools (Sentry, Linear, Vercel, Cloudflare) ~$200, Anthropic API ~$100/tenant (so scales with tenant count), Michael's draw $0 in first 6 months (lives off OL-USA contractor income).

### Conservative scenario (month-by-month)

| Month | Tenants (EoM) | New | Churn | MRR (EoM) | Costs | Net | Cumulative |
|---|---|---|---|---|---|---|---|
| 1 | 0 | 0 | 0 | $0 | $250 | -$250 | -$250 |
| 2 | 0 | 0 | 0 | $0 | $250 | -$250 | -$500 |
| 3 | 5 | 5 | 0 | $6,250 | $1,000 | $5,250 | $4,750 |
| 4 | 6 | 1 | 0 | $7,500 | $1,150 | $6,350 | $11,100 |
| 5 | 7 | 1.4 | 0.4 | $8,750 | $1,300 | $7,450 | $18,550 |
| 6 | 8 | 1.5 | 0.5 | $10,000 | $1,450 | $8,550 | $27,100 |
| 7 | 9 | 1.5 | 0.5 | $11,250 | $1,600 | $9,650 | $36,750 |
| 8 | 10 | 1.6 | 0.6 | $12,500 | $1,750 | $10,750 | $47,500 |
| 9 | 11 | 1.7 | 0.7 | $13,750 | $1,900 | $11,850 | $59,350 |
| 10 | 12 | 1.7 | 0.7 | $15,000 | $2,050 | $12,950 | $72,300 |
| 11 | 12 | 0.7 | 0.7 | $15,000 | $2,050 | $12,950 | $85,250 |
| 12 | 13 | 1.7 | 0.7 | $16,250 | $2,200 | $14,050 | **$99,300** |

**Conservative breakeven: Month 3 (when first 5 tenants close). Cumulative cash positive: Month 4.**

### Aggressive scenario (month-by-month)

| Month | Tenants (EoM) | New | Churn | MRR (EoM) | Costs | Net | Cumulative |
|---|---|---|---|---|---|---|---|
| 1 | 0 | 0 | 0 | $0 | $250 | -$250 | -$250 |
| 2 | 0 | 0 | 0 | $0 | $250 | -$250 | -$500 |
| 3 | 5 | 5 | 0 | $7,500 | $1,000 | $6,500 | $6,000 |
| 4 | 8 | 3 | 0 | $12,000 | $1,400 | $10,600 | $16,600 |
| 5 | 11 | 3.2 | 0.2 | $16,500 | $1,800 | $14,700 | $31,300 |
| 6 | 14 | 3.3 | 0.3 | $21,000 | $2,200 | $18,800 | $50,100 |
| 7 | 17 | 3.4 | 0.4 | $25,500 | $2,600 | $22,900 | $73,000 |
| 8 | 20 | 3.5 | 0.5 | $30,000 | $3,000 | $27,000 | $100,000 |
| 9 | 23 | 3.6 | 0.6 | $34,500 | $3,400 | $31,100 | $131,100 |
| 10 | 25 | 2.7 | 0.7 | $37,500 | $3,700 | $33,800 | $164,900 |
| 11 | 27 | 2.8 | 0.8 | $40,500 | $4,000 | $36,500 | $201,400 |
| 12 | 29 | 2.8 | 0.8 | $43,500 | $4,300 | $39,200 | **$240,600** |

**Aggressive breakeven: Month 3. Cumulative cash positive: Month 3. End-Y1 MRR: ~$43K.**

### What this tells you

- **Either scenario clears breakeven by Month 4 if you hit 5 tenants by Month 3.** The whole bet rests on closing those 5.
- **Conservative gets you to ~$16K MRR / ~$195K ARR by Month 12.** That's a real business, but it's not life-changing solo income for the work invested unless ACVs creep up via Pro→Enterprise upgrades.
- **Aggressive gets you to ~$43K MRR / ~$520K ARR by Month 12.** That's the "hire the second person, raise a seed if you want" outcome.
- **The single biggest lever is closing the first 5 tenants by Month 3.** Everything compounds from there. The OL-USA partnership conversation in Week 9 has the highest expected value of any single hour of work in the plan.

### Kill criteria (be honest)

- **<3 paying tenants by Week 16** → pivot or kill. Either you can't sell or the product isn't ready.
- **<$5K MRR by Month 6** → kill. Bootstrapping past this is grinding for sub-contractor income.
- **>20% monthly churn at any point in Months 4–6** → the product isn't sticky enough; rework the value prop before adding more tenants.

---

## What This Plan Does NOT Cover (Explicitly Out of Scope)

- **Hiring.** No FTE hires through Month 6. CS contractor (~$2K/mo) at Month 4 if MRR ≥ $10K.
- **Fundraising.** Don't raise. Bootstrap to $30K MRR, then revisit. A seed raise at $10K MRR with one founder is a bad trade.
- **International expansion.** US Microsoft 365 brokers only through Y1.
- **A native mobile app.** Year 2 conversation.
- **Carrier-direct integrations.** Specifically out of scope per the product spec.
- **An open API.** Enterprise-tier only, and Enterprise-tier doesn't have to mean a fully self-serve API — it can mean "I'll build you a webhook for $X".

---

## The Bet, Stated Plainly

The bet is that:

1. The Hilmar pipeline is **already** the hardest engineering problem in this space — multi-tenanting it is decoupling work, not invention.
2. There are **thousands** of broker-shipper relationships shaped like Lonny↔OL, and the broker side of those relationships has no systematic competitive intelligence today.
3. **$1,500/month per key account** is cheap enough that any broker losing one deal will pay for a year of RateIntel just to win the next one.
4. **OL-USA's partner network is a 50–200-broker addressable market** that Michael has unique access to. Even 5% conversion is the Y1 plan.

If 3 of these 4 are true at the end of 12 weeks, RateIntel is a real business. If only 1–2 are true, fall back to a higher-quality contractor engagement and revisit in Y2.

If all 4 are true, hire by Month 6 and raise no money — let MRR pay for growth.

That's the call. Sunday night, this is the plan. Monday morning: Week 1 Day 1, start with `tenants/hilmar/config.yaml`.

---

*End of plan. Last updated: 2026-05-31.*
