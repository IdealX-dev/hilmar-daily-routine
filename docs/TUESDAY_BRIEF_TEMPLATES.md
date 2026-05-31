# Tuesday Brief — Templates

The Tuesday Brief is an **additive** weekly send to Lonny Upfold. It does
not replace the 10 AM ET daily shipment-tracker; it goes Tuesday morning
ET (so it lands Monday evening Pacific, ready for Lonny's Tuesday
workday). Goal: shift the relationship from "one of 8 brokers in the
queue" to "freight advisor we hear from on Tuesdays."

Four lenses are defined. Rotate them weekly. Each lens has its own
subject line, HTML body, and plaintext fallback. Every claim is backed by
a field name from `tracking-data-v2.json` (see `schema.json`) or one of
the aggregations in `src/hilmar/core.py` / `scripts/share_intel.py`.

Visual tokens reused from `scripts/gen_email.py`:

- `B.HILMAR_NAVY` `#0a2350` — header solid + table header bg
- `B.HILMAR_BLUE` `#1a3d9c` — accent / section underline
- `B.HILMAR_GREEN` `#76b82a` — section underline accent
- Header gradient: `linear-gradient(135deg, B.HILMAR_NAVY 0%, B.HILMAR_BLUE 100%)` with `HILMAR_NAVY` solid fallback (Outlook strips gradients)
- Logo via `B.logo_html_cid(height=56, alt="Hilmar Ingredients")`
- Font stack: `'Inter','Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif`
- `font-variant-numeric:tabular-nums` so columns line up
- KPI tile shape from `_kpi_card`: 88px min-height, white bold value, 22px, label below 11px
- Tables: 1px solid `#d1d5db` outer, `#e5e7eb` row dividers, 12px body

Every template uses the same header and footer. They are factored out
below; the four lens bodies slot between them.

---

## Shared header

``` html
<!--[if !mso]><!-->
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<!--<![endif]-->
<div style="max-width:760px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;font-family:'Inter','Segoe UI',-apple-system,Helvetica,Arial,sans-serif;font-variant-numeric:tabular-nums">
  <div style="padding:18px 28px;background-color:#0a2350;background:linear-gradient(135deg,#0a2350 0%,#1a3d9c 100%);color:#ffffff">
    <div style="background:#ffffff;padding:2px 6px;border-radius:4px;display:inline-block;margin-bottom:6px">
      <img src="cid:hilmar-logo" alt="Hilmar Ingredients" height="56" style="display:block">
    </div>
    <h1 style="margin:0;font-size:20px;font-weight:700;letter-spacing:-0.3px">Hilmar Tuesday Brief — Week of {{week_of_date_str}}</h1>
    <p style="margin:4px 0 0;font-size:13px;opacity:0.9">A weekly read on the freight market behind your RFQs. From OL-USA.</p>
  </div>
  <div style="padding:22px 28px;color:#0f172a;font-size:14px;line-height:1.55">
```

## Shared footer

``` html
    <p style="margin:24px 0 6px;font-size:13px;color:#0f172a">— Michael Deitchman, OL-USA &middot; <a href="https://idealx.us" style="color:#1a3d9c;text-decoration:none">idealx.us</a></p>
    <p style="margin:0;font-size:12px;color:#64748b">Reply with questions or to schedule a call. This brief is generated from {{n_quotes_window}} quotes across {{n_lanes_window}} active lanes in the last 30 days.</p>
  </div>
</div>
```

Plaintext footer:

``` text
— Michael Deitchman, OL-USA · idealx.us
Reply with questions or to schedule a call.
```

---

## Lens 1 — Trade-region pulse

**Use when:** the market is moving and Lonny benefits from a heads-up
before he sends Tuesday's RFQs. Default if no other lens is overdue.

**Subject (≤70 chars):**
``` text
Hilmar Tuesday Brief — Vietnam reefer up 12d, Far East holding
```
Dynamic template: `Hilmar Tuesday Brief — {{lens1.headline_region}} {{lens1.headline_direction}} {{lens1.headline_metric}}`

**HTML body:**

``` html
<p style="margin:0 0 14px">Quick read on how your trade regions are moving this week. Pulled from the {{n_quotes_window}} quotes I've seen across all carriers serving Hilmar's lanes — not just the ones I'm winning.</p>

<h2 style="margin:18px 0 8px;color:#1a3d9c;font-size:15px;border-bottom:2px solid #76b82a;padding-bottom:4px">Region pulse — last 7 days vs prior 7</h2>
<table style="width:100%;border-collapse:collapse;font-size:12px;border:1px solid #d1d5db">
  <thead><tr>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:left;font-size:11px">Region</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">RFQs (WoW)</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">Median transit (Δ d)</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">Quote rate</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:left;font-size:11px">14-day activity</th>
  </tr></thead>
  <tbody>
    {{#each lens1.region_rows}}
    <tr>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;font-weight:600">{{region}}</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{requests_7d}} ({{requests_wow_delta_signed}})</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{transit_median_days}} ({{transit_delta_signed}})</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{quote_rate_pct}}%</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;font-family:monospace;font-size:13px">{{sparkline_total}}</td>
    </tr>
    {{/each}}
  </tbody>
</table>

<h2 style="margin:20px 0 8px;color:#1a3d9c;font-size:15px;border-bottom:2px solid #76b82a;padding-bottom:4px">What I'm watching</h2>
<p style="margin:0 0 10px"><strong>{{lens1.watch_1.region}}.</strong> Transit times on {{lens1.watch_1.lane}} stretched {{lens1.watch_1.transit_delta_days}} days versus the 28-day median ({{lens1.watch_1.transit_median_days}}d → {{lens1.watch_1.transit_latest_days}}d). {{lens1.watch_1.carrier_count}} carriers are quoting; {{lens1.watch_1.fastest_carrier}} is currently the fastest at {{lens1.watch_1.fastest_transit_days}}d.</p>
<p style="margin:0 0 10px"><strong>{{lens1.watch_2.region}}.</strong> RFQ volume into this region is {{lens1.watch_2.volume_delta_pct_signed}} versus your 28-day pace. {{lens1.watch_2.context_sentence}}</p>
<p style="margin:0 0 14px"><strong>Capacity signal.</strong> {{lens1.capacity_carriers_quoting}} of the {{lens1.capacity_carriers_total}} carriers we normally see on your lanes have quoted in the last 14 days. {{lens1.capacity_cooled_count}} have gone quiet ({{lens1.capacity_cooled_list}}). I'm chasing them this week.</p>

<div style="background:#eff6ff;border-left:4px solid #1a3d9c;padding:12px 14px;margin:14px 0;border-radius:4px">
  <p style="margin:0;font-size:14px">Want a deeper read on {{lens1.watch_1.region}} reefer? Reply and I'll send the carrier-by-carrier breakdown — rates, transit, equipment posture — for next week's planning.</p>
</div>
```

**Plaintext fallback:**

``` text
Hilmar Tuesday Brief — Week of {{week_of_date_str}}

Quick read on how your trade regions are moving this week. Pulled from
{{n_quotes_window}} quotes across all carriers serving Hilmar's lanes.

REGION PULSE — last 7 days vs prior 7
{{#each lens1.region_rows}}
- {{region}}: {{requests_7d}} RFQs ({{requests_wow_delta_signed}}),
  median transit {{transit_median_days}}d ({{transit_delta_signed}}),
  quote rate {{quote_rate_pct}}%
{{/each}}

WHAT I'M WATCHING
- {{lens1.watch_1.region}}: transit on {{lens1.watch_1.lane}} stretched
  {{lens1.watch_1.transit_delta_days}}d vs 28-day median.
  Fastest right now: {{lens1.watch_1.fastest_carrier}} at
  {{lens1.watch_1.fastest_transit_days}}d.
- {{lens1.watch_2.region}}: RFQ volume {{lens1.watch_2.volume_delta_pct_signed}}
  vs 28-day pace. {{lens1.watch_2.context_sentence}}
- Capacity: {{lens1.capacity_carriers_quoting}}/{{lens1.capacity_carriers_total}}
  carriers active in the last 14 days. Cooled:
  {{lens1.capacity_cooled_list}}.

Want a deeper read on {{lens1.watch_1.region}} reefer? Reply and I'll
send the carrier-by-carrier breakdown.
```

**Field sources:** `core.aggregate_trade_regions`,
`core.compute_period_trends` (wow block), `share_intel._transit_days`
rolled per region, `core.compute_lane_activity_sparklines` (14d),
`gen_rate_intelligence.analyze_carrier_cooling`.

---

## Lens 2 — Where you're winning, where the market is shifting

**Use when:** there's a real YoY or product-mix story. Best week 2-3 of
the month once the 30-day window has settled.

**Subject (≤70 chars):**
``` text
Hilmar Tuesday Brief — Whey up 18% QoQ, SE Asia carrying the volume
```
Dynamic: `Hilmar Tuesday Brief — {{lens2.headline_product}} {{lens2.headline_pct_signed}} QoQ, {{lens2.headline_region}} carrying`

**HTML body:**

``` html
<p style="margin:0 0 14px">Stepping back from this week's quotes for a moment. Here is how Hilmar's freight footprint has shifted over the last 90 days versus the 90 before — same data set, just zoomed out. The pattern shows up clearest by region and by product mix.</p>

<h2 style="margin:18px 0 8px;color:#1a3d9c;font-size:15px;border-bottom:2px solid #76b82a;padding-bottom:4px">Volume by trade region — 90d vs prior 90d</h2>
<table style="width:100%;border-collapse:collapse;font-size:12px;border:1px solid #d1d5db">
  <thead><tr>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:left;font-size:11px">Region</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">TEU requested (90d)</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">vs prior 90d</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">Share of mix</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:left;font-size:11px">Dominant product</th>
  </tr></thead>
  <tbody>
    {{#each lens2.region_yoy_rows}}
    <tr>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;font-weight:600">{{region}}</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{teu_requested_90d}}</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right;color:{{delta_color}};font-weight:600">{{teu_delta_pct_90d_signed}}</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{share_of_total_pct}}%</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb">{{dominant_product}} ({{dominant_product_share_pct}}%)</td>
    </tr>
    {{/each}}
  </tbody>
</table>

<h2 style="margin:20px 0 8px;color:#1a3d9c;font-size:15px;border-bottom:2px solid #76b82a;padding-bottom:4px">Product mix shift</h2>
<p style="margin:0 0 10px">Across all destinations, your product mix has moved: <strong>{{lens2.product_shift.up_product}} is up {{lens2.product_shift.up_pct_signed}}</strong> share-of-RFQs versus the prior 90 days; <strong>{{lens2.product_shift.down_product}} is down {{lens2.product_shift.down_pct_signed}}</strong>. {{lens2.product_shift.context_sentence}}</p>

<h2 style="margin:20px 0 8px;color:#1a3d9c;font-size:15px;border-bottom:2px solid #76b82a;padding-bottom:4px">New lanes we noticed</h2>
<p style="margin:0 0 10px">{{lens2.new_lanes.count}} destinations appeared in your RFQs for the first time in 90 days: <strong>{{lens2.new_lanes.list}}</strong>. {{lens2.new_lanes.context_sentence}}</p>

<div style="background:#f0fdf4;border-left:4px solid #76b82a;padding:12px 14px;margin:14px 0;border-radius:4px">
  <p style="margin:0;font-size:14px">We see <strong>{{lens2.cta.open_rfq_count}} open RFQs to {{lens2.cta.lane}}</strong> right now. Would packaging these into a single volume conversation save you negotiation time? Reply yes and I'll set up a 20-minute call with the relevant carrier reps.</p>
</div>
```

**Plaintext fallback:**

``` text
Hilmar Tuesday Brief — Week of {{week_of_date_str}}

How Hilmar's freight footprint has shifted over the last 90 days vs the
90 before.

VOLUME BY TRADE REGION (90d vs prior 90d)
{{#each lens2.region_yoy_rows}}
- {{region}}: {{teu_requested_90d}} TEU ({{teu_delta_pct_90d_signed}}),
  {{share_of_total_pct}}% of mix, dominant product {{dominant_product}}
  at {{dominant_product_share_pct}}%
{{/each}}

PRODUCT MIX
- {{lens2.product_shift.up_product}} is {{lens2.product_shift.up_pct_signed}}
  share vs prior 90 days
- {{lens2.product_shift.down_product}} is {{lens2.product_shift.down_pct_signed}}

NEW LANES IN THE LAST 90 DAYS
{{lens2.new_lanes.list}}

We see {{lens2.cta.open_rfq_count}} open RFQs to {{lens2.cta.lane}} right
now. Want to package these into one volume conversation? Reply yes and
I'll set up a 20-minute call.
```

**Field sources:** `core.aggregate_trade_regions` (rolled to TEU),
`core.compute_period_trends` (90d window — extension needed, see DATA
GAPS), `core.aggregate_carriers` for product fields, `request.product`,
`request.destination` first-seen scan over 90d.

---

## Lens 3 — Carrier intelligence brief

**Use when:** rate dispersion is wide, a carrier has cooled, or you want
to position for a quarterly carrier review. Highest authority lens —
use when you want Lonny to feel that OL-USA tracks the whole market, not
just our own quotes.

**Subject (≤70 chars):**
``` text
Hilmar Tuesday Brief — MSC cooled 21d, ONE undercutting on Oakland
```
Dynamic: `Hilmar Tuesday Brief — {{lens3.headline_carrier_cool}} cooled {{lens3.headline_days}}d, {{lens3.headline_carrier_aggressive}} {{lens3.headline_action}}`

**HTML body:**

``` html
<p style="margin:0 0 14px">Carrier-level read on what we are seeing across your lanes. This combines the rates OL-USA quoted, the rates we see winning (so the carriers you booked with via other brokers show up too), and the silence we are observing. Three signals to act on this week.</p>

<h2 style="margin:18px 0 8px;color:#1a3d9c;font-size:15px;border-bottom:2px solid #76b82a;padding-bottom:4px">Carrier rate posture — last 30 days</h2>
<table style="width:100%;border-collapse:collapse;font-size:12px;border:1px solid #d1d5db">
  <thead><tr>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:left;font-size:11px">Carrier</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">Quotes</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">Win %</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">Rate median $/FEU</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">vs 60d prior</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:left;font-size:11px">Signal</th>
  </tr></thead>
  <tbody>
    {{#each lens3.carrier_rows}}
    <tr>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;font-weight:600">{{carrier}}</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{quotes_30d}}</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{win_rate_pct}}%</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">${{rate_median_feu}}</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right;color:{{rate_delta_color}};font-weight:600">{{rate_delta_pct_signed}}</td>
      <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb">{{signal_label}}</td>
    </tr>
    {{/each}}
  </tbody>
</table>

<h2 style="margin:20px 0 8px;color:#1a3d9c;font-size:15px;border-bottom:2px solid #76b82a;padding-bottom:4px">Cooled carriers — silent &ge; 14 days</h2>
<p style="margin:0 0 10px">{{lens3.cooled.count}} carriers we'd normally see quoting on your lanes have gone quiet. The notable ones:</p>
<ul style="margin:0 0 12px;padding-left:22px">
  {{#each lens3.cooled.rows}}
  <li style="margin-bottom:4px"><strong>{{carrier}}</strong> — {{days_silent}}d silent, last quote {{last_quote_date}}, historical win rate {{win_rate_pct}}% on {{lanes_quoted}} of your lanes. {{context_sentence}}</li>
  {{/each}}
</ul>

<h2 style="margin:20px 0 8px;color:#1a3d9c;font-size:15px;border-bottom:2px solid #76b82a;padding-bottom:4px">Rate dispersion — where the spread is widest</h2>
<p style="margin:0 0 10px">On <strong>{{lens3.dispersion.lane}}</strong>, last-30d quotes ranged ${{lens3.dispersion.rate_min}}–${{lens3.dispersion.rate_max}}/FEU across {{lens3.dispersion.carrier_count}} carriers. The {{lens3.dispersion.spread_pct}}% spread is the widest on your active book. That is leverage for a renegotiation conversation with {{lens3.dispersion.top_carrier}}.</p>

<div style="background:#fef3c7;border-left:4px solid #d97706;padding:12px 14px;margin:14px 0;border-radius:4px">
  <p style="margin:0;font-size:14px">Want a <strong>quarterly carrier scorecard</strong> — every carrier's quote count, win rate, rate trend, and transit performance on your lanes? I can have one ready Friday. Reply and tell me which carriers you'd like ranked first.</p>
</div>
```

**Plaintext fallback:**

``` text
Hilmar Tuesday Brief — Week of {{week_of_date_str}}

Carrier-level read on what we are seeing across your lanes — rates OL
quoted, rates we see winning, and silence we are observing.

CARRIER RATE POSTURE — last 30 days
{{#each lens3.carrier_rows}}
- {{carrier}}: {{quotes_30d}} quotes, {{win_rate_pct}}% win,
  ${{rate_median_feu}}/FEU median ({{rate_delta_pct_signed}} vs 60d prior),
  signal: {{signal_label}}
{{/each}}

COOLED CARRIERS (silent >= 14 days)
{{#each lens3.cooled.rows}}
- {{carrier}}: {{days_silent}}d silent, last {{last_quote_date}},
  historical win rate {{win_rate_pct}}%
{{/each}}

WIDEST RATE SPREAD
{{lens3.dispersion.lane}}: ${{lens3.dispersion.rate_min}}–
${{lens3.dispersion.rate_max}}/FEU across
{{lens3.dispersion.carrier_count}} carriers ({{lens3.dispersion.spread_pct}}%
spread). Leverage for renegotiation with
{{lens3.dispersion.top_carrier}}.

Want a quarterly carrier scorecard? I can have one ready Friday. Reply
with the carriers to rank first.
```

**Field sources:** `core.aggregate_carriers`,
`gen_rate_intelligence.analyze_winning_rate_trends`,
`gen_rate_intelligence.analyze_carrier_cooling`,
`baselines.json → carrier_lane_winrate`, lane price-spread computed from
`share_intel.quotes.jsonl` `rate_won_min/max` per lane.

---

## Lens 4 — Operational signal — quote-to-decision velocity

**Use when:** decision pace has visibly shifted, validity windows are
biting, or it's been a quiet rate-stability week (no big region/carrier
story to lead with). This is the lens that says "I'm paying attention
to how you work, not just bidding for your freight."

**Subject (≤70 chars):**
``` text
Hilmar Tuesday Brief — Your quote-to-decision time held at 2.4 days
```
Dynamic: `Hilmar Tuesday Brief — Your quote-to-decision time {{lens4.headline_direction}} at {{lens4.headline_metric_d}}`

**HTML body:**

``` html
<p style="margin:0 0 14px">This week's brief is about <em>your</em> process more than the market's. The data shows how fast quotes are being decided, where validity windows are pinching you, and which days of the week your decision pace shifts. Useful if you are planning Q3 carrier conversations and want to set expectations on response windows.</p>

<h2 style="margin:18px 0 8px;color:#1a3d9c;font-size:15px;border-bottom:2px solid #76b82a;padding-bottom:4px">Decision velocity — 30-day vs prior 30-day</h2>
<table style="width:100%;border-collapse:collapse;font-size:12px;border:1px solid #d1d5db">
  <thead><tr>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:left;font-size:11px">Metric</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">Last 30d</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">Prior 30d</th>
    <th style="padding:6px 8px;background:#0a2350;color:#ffffff;text-align:right;font-size:11px">Δ</th>
  </tr></thead>
  <tbody>
    <tr><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb">OL time-to-quote (biz hrs, median)</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{lens4.ttq_median_30d}}h</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{lens4.ttq_median_prior_30d}}h</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right;font-weight:600">{{lens4.ttq_delta_signed}}</td></tr>
    <tr><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb">Your time-to-decide (quote → booked or passed, days)</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{lens4.t2d_median_30d}}d</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{lens4.t2d_median_prior_30d}}d</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right;font-weight:600">{{lens4.t2d_delta_signed}}</td></tr>
    <tr><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb">Quotes that expired before decision</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{lens4.expired_count_30d}}</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{lens4.expired_count_prior_30d}}</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right;font-weight:600">{{lens4.expired_delta_signed}}</td></tr>
    <tr><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb">Median validity window offered (days)</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{lens4.validity_median_30d}}d</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right">{{lens4.validity_median_prior_30d}}d</td><td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;text-align:right;font-weight:600">{{lens4.validity_delta_signed}}</td></tr>
  </tbody>
</table>

<h2 style="margin:20px 0 8px;color:#1a3d9c;font-size:15px;border-bottom:2px solid #76b82a;padding-bottom:4px">When you decide</h2>
<p style="margin:0 0 8px">Your decision pattern, by weekday (medians, last 30 days):</p>
<ul style="margin:0 0 10px;padding-left:22px">
  {{#each lens4.dow_rows}}
  <li style="margin-bottom:3px"><strong>{{day_name}}</strong> — {{requests_count}} RFQs sent, median decision in {{decide_hours_median}}h{{dow_note}}</li>
  {{/each}}
</ul>
<p style="margin:0 0 14px">{{lens4.dow_observation_sentence}}</p>

<h2 style="margin:20px 0 8px;color:#1a3d9c;font-size:15px;border-bottom:2px solid #76b82a;padding-bottom:4px">Where validity windows hurt</h2>
<p style="margin:0 0 10px">In the last 30 days, <strong>{{lens4.expired_count_30d}} quotes</strong> went past their validity window before you decided — most on <strong>{{lens4.expired_top_lane}}</strong>. Median offered validity on that lane was {{lens4.expired_top_lane_validity_d}}d; your median decision time on the same lane is {{lens4.expired_top_lane_t2d_d}}d. The window is too tight.</p>

<div style="background:#eff6ff;border-left:4px solid #1a3d9c;padding:12px 14px;margin:14px 0;border-radius:4px">
  <p style="margin:0;font-size:14px">If quote-validity is hurting you, I can pre-coordinate with carriers for <strong>5-day windows</strong> on your top {{lens4.cta.lane_count}} lanes next quarter — at no premium, contingent on volume. Reply and I'll start the conversation with the carriers this week.</p>
</div>
```

**Plaintext fallback:**

``` text
Hilmar Tuesday Brief — Week of {{week_of_date_str}}

This week's brief is about YOUR process — quote-to-decision pace,
validity windows, and weekday decision patterns.

DECISION VELOCITY (30d vs prior 30d)
- OL time-to-quote: {{lens4.ttq_median_30d}}h vs
  {{lens4.ttq_median_prior_30d}}h ({{lens4.ttq_delta_signed}})
- Your time-to-decide: {{lens4.t2d_median_30d}}d vs
  {{lens4.t2d_median_prior_30d}}d ({{lens4.t2d_delta_signed}})
- Quotes expired before decision: {{lens4.expired_count_30d}} vs
  {{lens4.expired_count_prior_30d}}
- Median validity window offered: {{lens4.validity_median_30d}}d vs
  {{lens4.validity_median_prior_30d}}d

WHEN YOU DECIDE (medians by weekday, last 30d)
{{#each lens4.dow_rows}}
- {{day_name}}: {{requests_count}} RFQs, decided in
  {{decide_hours_median}}h
{{/each}}

WHERE VALIDITY WINDOWS HURT
- {{lens4.expired_count_30d}} quotes expired before decision in last 30
  days; most on {{lens4.expired_top_lane}}
- Lane validity median: {{lens4.expired_top_lane_validity_d}}d; your
  decision time on that lane: {{lens4.expired_top_lane_t2d_d}}d

If quote-validity is hurting you, I can pre-coordinate 5-day windows on
your top {{lens4.cta.lane_count}} lanes for next quarter, no premium,
contingent on volume. Reply and I'll start the conversation.
```

**Field sources:** `request.turnaround_biz_hours` (OL time-to-quote),
`request.status_history` to compute quote → WIN/Q&L delta (Lonny's
decision time), `request.validity_window` parsed by
`core.parse_validity_window`, `request.send_signal_events` for last
chase activity, `request.request_date` weekday bucketing.

---

## DATA GAPS

These are the placeholders above that don't yet have a backing
aggregation. If we ship Tuesday Brief without them we either drop the
sentence or hard-code a sensible default. Listed so we know what to
build in the same commit as the renderer.

| Placeholder | Needs |
|---|---|
| `lens1.region_rows[*].requests_wow_delta_signed` | Extend `core.compute_period_trends` to bucket by `trade_region` (today it only bucket-aggregates the whole dataset). |
| `lens1.watch_*.transit_delta_days` | Add 28-day rolling median per (lane, carrier) — pull from `share_intel.lane_summary.transit_median_days` historized. |
| `lens2.region_yoy_rows[*].teu_delta_pct_90d_signed` | Add a 90d-vs-prior-90d block to `compute_period_trends` (today only has wow / mom / ytd). |
| `lens2.product_shift.*` | New aggregation: bucket `request.product` over 90d windows. Source data already in schema. |
| `lens2.new_lanes.*` | First-seen scan: lanes present in last 90d but absent in prior 90d. Trivial — add to `core` as `find_emerging_lanes`. |
| `lens3.carrier_rows[*].rate_delta_pct_signed` | Compare `aggregate_carriers.rate_median` over 30d vs prior 60d. New function `compute_carrier_rate_trends`. |
| `lens3.dispersion.*` | Per-lane `rate_max - rate_min` from `share_intel.lane_summary`. Need to expose `rate_min`/`rate_max` (already in `quotes.jsonl`, not surfaced in summary). |
| `lens4.t2d_median_*` | Quote → decision delta from `status_history`. Compute the time from `status_history` event where `to == 'PENDING' (Lonny side)` to the next `WIN`/`Q&L` event. New function `compute_lonny_decision_velocity`. |
| `lens4.expired_*` | A quote is "expired before decision" if `now > validity_window_end` AND `status == 'PENDING'` at that moment, OR `decided_at > validity_window_end`. Need to parse `validity_window` into a `validity_end_iso` field — extend `core.parse_validity_window` to return a date, not a string. |
| `lens4.dow_rows[*]` | Bucket by `request_date.weekday()`. Pure compute; no schema change. |

---

## Implementation notes for `scripts/gen_tuesday_brief.py`

**Where it lives in the pipeline.** Add as **Step 16** in
`scripts/run_pipeline.py`, after `gen_rate_intelligence.py` (Step 14)
and `sync_to_quote_tracker.py` (Step 15). Gate the step on weekday so
it only runs Tuesday — non-Tuesday invocations are a no-op:

``` python
# scripts/run_pipeline.py — Step 16
if datetime.now(core.ET).weekday() == 1:  # Tuesday
    run_step("gen_tuesday_brief.py", ["--lens", "auto"])
```

The wrapper (`deploy/run_daily_laptop.cmd`) does **not** auto-send the
brief. Tuesday Brief stays operator-triggered for at least the first 4
weeks (see "Approval gate" below). Once trusted, flip
`config.json -> tuesday_brief.auto_send = true` to wire it into the
wrapper's send sequence the same way the daily fire is wired.

**Lens selection.** The script picks one lens per Tuesday based on which
story is strongest:

1. If any region has a `transit_delta_days >= 4` or a carrier in
   `analyze_carrier_cooling` ≥ 21d silent: **Lens 3**.
2. Else if 90d product mix has any product shifted ≥ 8pp share: **Lens 2**.
3. Else if `lens4.expired_count_30d / lens4.t2d_median_30d` flags
   validity-pinch: **Lens 4**.
4. Else default: **Lens 1**.

Override with `--lens 1|2|3|4`. The router writes the chosen lens to
`reports/tuesday-brief-<YYYY-MM-DD>.lens` so the audit email can
reference it.

**Outputs.**
```
reports/tuesday-brief-<YYYY-MM-DD>.html       # rendered HTML
reports/tuesday-brief-<YYYY-MM-DD>.txt        # plaintext fallback
reports/tuesday-brief-<YYYY-MM-DD>.subject    # subject line
reports/tuesday-brief-<YYYY-MM-DD>.json       # the placeholder dict that fed the render (for audit + regression)
reports/tuesday-brief-<YYYY-MM-DD>.lens       # "1" / "2" / "3" / "4"
```

**Config knobs** (extend `config.json`):

``` json
"tuesday_brief": {
  "enabled": true,
  "auto_send": false,
  "to": ["lupfold@hilmaringredients.com"],
  "cc": ["michael.deitchman@idealx.us"],
  "from": "michael.deitchman@ol-usa.com",
  "test_to": ["michael.deitchman@idealx.us"],
  "lens_override": null,
  "min_quotes_for_send": 25,
  "min_lanes_for_send": 4,
  "thresholds": {
    "lens3_transit_delta_days": 4,
    "lens3_carrier_cool_days": 21,
    "lens2_product_shift_pp": 8.0
  }
}
```

`min_quotes_for_send` / `min_lanes_for_send` are floor gates — if the
30-day window has fewer than that, the script writes the artifact but
exits non-zero so the operator notices and the wrapper doesn't auto-send.
Prevents shipping an under-informed brief during quiet weeks.

**Approval gate.** First 4 weeks: `auto_send=false`. The script writes
the HTML + JSON; a new step in the wrapper pipes the rendered HTML into
Michael's idealx.us audit email under a "DRAFT — Tuesday Brief — review
before send" section. Michael copy-edits, then runs
`scripts/outlook_send.py tuesday_brief --send` to fire from his Cloud PC.
Once the cadence is trusted, flip `auto_send=true` and the wrapper
fires it without intervention. This matches the daily-email pattern
(QC-022 distribution iteration lock).

**QC checks to add same commit.** Per the §3 standing rule in
`CLAUDE.md`:

- **QC-051** — Tuesday Brief freshness. ERROR if the chosen lens's
  primary metric is computed from < `min_quotes_for_send` quotes.
- **QC-052** — Tuesday Brief recipient guard. Same shape as QC-022 — if
  `tuesday_brief.to` ever contains a non-Hilmar domain, ERROR. The
  recipient list ships with `lupfold@hilmaringredients.com` plus CC to
  Michael; nothing else.
- **QC-053** — Tuesday Brief placeholder leak. ERROR if the rendered
  HTML contains `{{` or `}}` (unfilled handlebar). This is the same
  failure mode the daily email guards against in `gen_email.py` — same
  check, new artifact.
- **QC-054** — Tuesday Brief content-skip on quiet week. WARN (not
  ERROR) if `min_quotes_for_send` floor blocked a send. Notifies
  Michael; doesn't break the pipeline.

**Sentry routing.** Add the four new QC IDs to
`scripts/qc_actions_from_sentry.py` → `ACTIONS` mapping:

``` python
"QC-051": "flag_for_operator",   # quiet-week judgment call
"QC-052": "flag_for_operator",   # distribution security — never auto-resolve
"QC-053": "trigger_seer",        # rendering bug, autofix candidate
"QC-054": "log_only",            # documented, expected on quiet weeks
```

**Send mechanics.** Reuse `scripts/outlook_send.py` — add a new mode
`tuesday_brief` that loads the most recent
`reports/tuesday-brief-<DATE>.html` + matching `.subject` and sends via
the existing MSAL token cache. Idempotency flag:
`reports/sent-tuesday-brief-<YYYY-MM-DD>.flag` (same pattern as the
daily `sent-<DATE>.flag`).

**Renderer.** Use Jinja2 (already a transitive dep) for the handlebar
substitution. Templates live as constants in
`scripts/gen_tuesday_brief.py` (matching the `gen_email.py` pattern —
no separate template files). One function per lens
(`render_lens_1`, `render_lens_2`, ...), each returning
`(subject, html, plaintext)`.

**Tests.** Add `tests/test_gen_tuesday_brief.py`:

1. `test_lens_selection_prefers_carrier_signal` — synthetic data with a
   21d-cooled carrier should pick Lens 3.
2. `test_lens_selection_default_lens_1` — calm market data picks Lens 1.
3. `test_min_quotes_floor_blocks_send` — 10-quote dataset against
   `min_quotes_for_send=25` exits non-zero.
4. `test_no_unfilled_handlebars` — render every lens against the golden
   fixture; assert `{{` not in output.
5. `test_recipient_guard` — config with a non-Hilmar `to` address
   surfaces a QC-052 error.

All five tests run in CI under the existing
`PYTHONIOENCODING=utf-8 pytest tests/` step.

**Mirror reminder.** Tuesday Brief is in `scripts/`, so after editing
copy to the production OneDrive folder per §3 rule 5 in `CLAUDE.md`. The
wrapper's `git pull + xcopy` step will pick it up on the next fire — no
manual deploy.
