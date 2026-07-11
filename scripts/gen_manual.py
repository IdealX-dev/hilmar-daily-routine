"""
gen_manual.py — auto-generated USER manual for the Hilmar Daily Tracker.

Per Michael 2026-07-10: "make sure the app also includes constantly updated
instruction manual for users." This is the CONSUMER manual — for the people
who receive the daily email — not the developer/operator docs in docs/.

"Constantly updated" is enforced two ways:
  1. Regenerated on EVERY fire, with the live values pulled from config.json
     (recipient counts, escalation contacts, client-email gate state, aging
     thresholds) — so the manual can never describe a stale configuration.
  2. tests/test_gen_manual.py drift-guards the section catalog against the
     actual renderers in gen_email.py / gen_dashboard.py — remove or rename
     a report section without updating the manual and CI goes red.

Produces: reports/user-manual.html (attached to the daily staff email).
Usage:    python3 scripts/gen_manual.py [--config config.json] [--out PATH]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import core  # noqa: E402

NAVY = "#1e3a5f"

#: Staff-email section catalog. `renderer` names the gen_email function that
#: draws the section — the drift-guard test asserts it still exists, so this
#: catalog cannot silently rot when the email changes.
EMAIL_SECTIONS = [
    ("What Happened Today", "_today_block_html",
     "The day's events in four tables: new requests from Lonny, OL-USA rate "
     "responses (with carrier, rate, OL signer and color-coded Time to "
     "Quote), status changes, and items that went pending."),
    ("KPIs — Today + Period to Date", "_kpi_block_html",
     "Tiles for requests, wins (with TEU), Quoted & Lost, Not Quoted and "
     "Pending — plus period Win Rate, No-Response Rate and average "
     "business-hours from Lonny's ask to OL's quote."),
    ("Loss-Reason Mix", "_loss_reason_mix_html",
     "Why we lost: the breakdown of loss reasons over the recent windows. "
     "Hidden automatically on days with no losses."),
    ("Week over Week", "_week_block_html",
     "Volume and wins per week (count and TEU) so you can see the trend "
     "without opening the dashboard."),
    ("Carrier Performance", "_carrier_block_html",
     "Per-carrier wins / losses / pending and TEU offered — the scoreboard "
     "behind carrier negotiations."),
    ("Volume by Trade Region", "_trade_region_html",
     "Requests, wins and win rate rolled up by trade region."),
    ("Top Winning Lanes", "_winning_lanes_html",
     "Lanes ranked by TEU won, with Offered (# · TEU) so percentages have a "
     "denominator."),
    ("Top Losing Lanes", "_losing_lanes_html",
     "Lanes ranked by TEU lost, including which carriers are beating us "
     "there."),
    ("Not Quoted (last 14 days)", "_nq_html",
     "Requests we never priced — with the full TEU tally, the volume-leverage "
     "number for rate negotiations."),
    ("Pending — OL Quote", "_pending_ol_html",
     "Requests waiting on OUR rate desk. Chase these internally."),
    ("Pending — Hilmar Response", "_pending_html",
     "Quotes waiting on Lonny's decision, with hours-since-quote so you know "
     "when to nudge."),
    ("AI Insights — Business", "_ai_insights_business_html",
     "Strategy bullets generated daily by the AI insights engine — carrier "
     "plays, lane opportunities, win-rate movers. Appears only when the "
     "narrative was generated the same day; omitted on skipped days."),
]

#: Dashboard tab catalog — labels must match gen_dashboard's tabs (drift-guarded).
DASHBOARD_TABS = [
    ("Summary", "KPIs, what happened, week-over-week bars, confirmed wins "
                "with MDOLX refs, top winning & losing lanes."),
    ("Turnaround", "Lonny request time (PT) vs OL response (ET), "
                   "business-hours adjusted."),
    ("Dates", "Requested vs offered dates side-by-side, with the ETD-fit "
              "score (how close OL's offer was to what Lonny asked)."),
    ("Carriers", "Carrier performance overview plus per-carrier drill-down — "
                 "lanes won/lost, turnaround, rate posture — built for 1:1 "
                 "carrier line meetings."),
    ("Pending", "The aging watchlist with the escalation warnings."),
    ("Trends", "Biggest rate movers per carrier/lane, normalized $/FEU, with "
               "the recent rate series."),
    ("QC", "Data-quality checks behind the numbers — what was auto-fixed and "
           "what needs review."),
]

STATUS_GLOSSARY = [
    ("WIN", "Lonny booked with OL — confirmed by an MDOLX booking reference."),
    ("Quoted & Lost (Q&L)", "OL quoted, Hilmar went elsewhere (or the move "
                            "died). The loss reason is tracked."),
    ("Not Quoted (NQ)", "Lonny asked, OL never priced it. These count "
                        "against the quote rate, not the win rate."),
    ("Pending OL", "Waiting on OL's rate desk to quote."),
    ("Pending Hilmar", "OL quoted; waiting on Lonny's decision."),
]

METRIC_DEFINITIONS = [
    ("Win Rate", "Wins ÷ decided competitive quotes (wins + Quoted & Lost). "
                 "Not-quoted requests are excluded — they show in Quote Rate "
                 "instead."),
    ("Quote Rate", "Share of Lonny's requests that received an OL rate."),
    ("Time to Quote", "Business hours from Lonny's email (PT) to OL's rate "
                      "reply (ET). Color-coded: green ≤ 4h, amber ≤ 24h, red "
                      "beyond."),
    ("ETD Fit", "Days between the sailing Lonny asked for and the one OL "
                "offered — a negotiation lever when a carrier consistently "
                "misses the ask."),
    ("TEU", "Twenty-foot-equivalent units; one 40' container = 2 TEU."),
]


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _section(title, body_html):
    return (f'<h2 style="color:{NAVY};font-size:17px;margin:26px 0 6px">'
            f'{_esc(title)}</h2>{body_html}')


def _dl(pairs):
    rows = "".join(
        f'<tr><td style="padding:5px 10px 5px 0;font-weight:600;color:{NAVY};'
        f'vertical-align:top;white-space:nowrap">{_esc(k)}</td>'
        f'<td style="padding:5px 0">{_esc(v)}</td></tr>'
        for k, v in pairs)
    return f'<table style="border-collapse:collapse;font-size:13px">{rows}</table>'


def build_manual(cfg: dict) -> str:
    now_et = datetime.now(timezone.utc).astimezone(core.ET)
    stamp = now_et.strftime("%B %d, %Y at %I:%M %p ET").replace(" 0", " ")
    dist = cfg.get("distribution", {}) or {}
    rules = cfg.get("rules", {}) or {}
    cr = cfg.get("client_report", {}) or {}
    chase = cfg.get("auto_chase", {}) or {}

    gate_state = (
        "ON — Lonny Upfold receives the client update daily"
        if cr.get("enabled") else
        "OFF — Lonny receives NOTHING; a sample goes to Michael only, "
        "pending go-live approval")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hilmar Daily Tracker — User Manual</title></head>
<body style="margin:0;background:#f1f5f9;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2937">
<div style="max-width:860px;margin:0 auto;background:#ffffff;padding:26px 30px">
<div style="background:{NAVY};color:#ffffff;border-radius:8px;padding:18px 22px">
  <h1 style="margin:0;font-size:22px">Hilmar Daily Tracker — User Manual</h1>
  <p style="margin:6px 0 0;font-size:12px;opacity:.9">Regenerated with every run so it always matches the live system · This copy: {_esc(stamp)}</p>
</div>
"""

    html += _section("What you receive, and when", _dl([
        ("Daily email", f"Every business day at ~6:07 PM ET, after Lonny's "
                        f"California business day ends, to the "
                        f"{len(dist.get('full_list', []))}-person distribution. "
                        f"A liveness monitor re-fires it automatically if the "
                        f"scheduled run ever fails."),
        ("Attachments", "hilmar-dashboard.html (interactive dashboard — opens "
                        "in any browser, phone or desktop), hilmar-report.pdf "
                        "(printable 6-page report), and this manual."),
        ("Weekly summary", "Friday evenings: an executive summary with "
                           "4-week trend sparklines."),
        ("Client update", f"Separate daily service email for Hilmar "
                          f"Ingredients. Current gate: {gate_state}."),
        ("Escalations", f"Operational escalations go to "
                        f"{', '.join(dist.get('escalation_to', []) or ['—'])}."),
    ]))

    secs = "".join(
        f'<p style="margin:7px 0;font-size:13px"><b style="color:{NAVY}">'
        f'{_esc(t)}</b> — {_esc(d)}</p>'
        for t, _fn, d in EMAIL_SECTIONS)
    html += _section("How to read the daily email", secs)

    tabs = "".join(
        f'<p style="margin:7px 0;font-size:13px"><b style="color:{NAVY}">'
        f'{_esc(t)}</b> — {_esc(d)}</p>'
        for t, d in DASHBOARD_TABS)
    html += _section("The dashboard, tab by tab", tabs)

    html += _section("Status glossary", _dl(STATUS_GLOSSARY))
    html += _section("How the numbers are computed", _dl(METRIC_DEFINITIONS))

    html += _section("Timers behind the pending sections", _dl([
        ("Aging warnings", "A pending item turns amber at "
         + "/".join(str(h) for h in rules.get("pending_warn_hours", []))
         + " business hours and is treated as aged at "
         f"{rules.get('pending_aging_hours', '—')}."),
        ("Auto-chase", (f"Enabled — up to {chase.get('max_per_day', 0)} gentle "
                        f"nudges per day to {chase.get('recipient', '—')} for "
                        f"quotes older than {chase.get('min_age_hours', '—')}h, "
                        f"never before {chase.get('earliest_send_hour_et', '—')}"
                        f":00 ET.") if chase.get("enabled")
                       else "Disabled."),
        ("Rate-move flag", f"A lane/carrier rate change beyond "
                           f"{rules.get('rate_trend_threshold_pct', '—')}% is "
                           f"flagged as a mover on the Trends tab."),
    ]))

    html += _section("Questions or corrections", _dl([
        ("Data looks wrong?", "Reply to the daily email — corrections are "
                              "applied through a tracked operator-corrections "
                              "process, never silent edits."),
        ("Want a new view?", "Ask — sections, tabs and thresholds are "
                             "config-driven and extend quickly."),
    ]))

    html += """
<div style="border-top:2px solid #e5e7eb;margin-top:26px;padding-top:10px">
  <p style="font-size:11px;color:#6b7280">Auto-generated by the Hilmar Shipment Tracker. This manual rebuilds on every run — if it disagrees with the system, the run that produced it is the authority.</p>
</div>
</div></body></html>"""
    return html


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--out", default=None,
                    help="Override output path (default: reports/user-manual.html "
                         "next to config paths.email_body)")
    args = ap.parse_args(argv)
    cfg = core.load_config(args.config)
    out = Path(args.out) if args.out else \
        Path(cfg["paths"]["email_body"]).parent / "user-manual.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    html = build_manual(cfg)
    out.write_text(html, encoding="utf-8")
    print(f"✅ User manual: {len(html):,} bytes -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
