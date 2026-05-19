"""
gen_rate_intelligence.py — Rate-negotiation cheat sheet + cooling/regression alerts.

Reads from the SHARED client_intelligence store (populated by share_intel.py),
produces an HTML section embeddable in the daily audit email AND a standalone
HTML/JSON for direct query.

Per Michael 2026-05-13: "i love all of this and want it now   it's also what
i will want in the rate tracker system we are building where all the data
for moves also is shared"

THREE ANALYSES:
  1. LANE CHEAT SHEET — for each active lane, show:
       - 30-day quote range ($min–$median–$max)
       - Winning carrier(s) + their median winning rate
       - Loss median (what we're being undercut by)
       - Last activity date
     Use this walking into a carrier review: "On Oakland → Yokohama you
     need to be at $X to compete."

  2. CARRIER COOLING — carriers that quoted ≥5 times historically but have
     gone silent in the last 14 days. Surface as red flags so we can chase
     the relationship.

  3. LANE REGRESSION — lanes where the last 3+ quotes were all losses,
     especially when the price gap is large. Flag for renegotiation.

Output files (in this client's reports/):
  reports/rate-intelligence.html           — full HTML page
  reports/rate-intelligence-section.html   — section snippet (embeds in audit)
  reports/rate-intelligence.json           — raw analysis (machine-readable)

Reads from:
  %USERPROFILE%\\OneDrive - IdealX\\SHARED\\client_intelligence\\hilmar\\
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import share_intel as SI  # noqa: E402
import viz as V  # noqa: E402

HILMAR_ROOT = Path(__file__).resolve().parent.parent
REPORTS = HILMAR_ROOT / "reports"

# Tunables — calibrated for Hilmar's typical activity. Each rate-tracker
# client can override via _client_meta.json overrides.
COOLING_WINDOW_DAYS = 14
COOLING_HISTORICAL_MIN_QUOTES = 5
REGRESSION_LOSING_STREAK_MIN = 3
PRICE_GAP_FLAG_PCT = 10.0  # winning rate is >10% below our quote → flag


def _esc(s):
    if s is None:
        return "—"
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _money(v):
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "—"


def _pct(v):
    return f"{v:.0f}%" if isinstance(v, (int, float)) else "—"


def _load_client(client_id: str = "hilmar") -> dict:
    """Load all artifacts for a client from the shared store."""
    cdir = SI._client_dir(client_id)
    return {
        "quotes": SI._load_jsonl(cdir / "quotes.jsonl"),
        "carrier_summary": json.loads((cdir / "carrier_summary.json").read_text(encoding="utf-8"))
            if (cdir / "carrier_summary.json").exists() else {},
        "lane_summary": json.loads((cdir / "lane_summary.json").read_text(encoding="utf-8"))
            if (cdir / "lane_summary.json").exists() else {},
        "meta": json.loads((cdir / "_client_meta.json").read_text(encoding="utf-8"))
            if (cdir / "_client_meta.json").exists() else {},
    }


# ───────────────────────────────────────────────────────────────────────
# Analyses
# ───────────────────────────────────────────────────────────────────────

def analyze_lane_cheat_sheet(data: dict, top_n: int = 15) -> list[dict]:
    """For each lane with recent activity, build the negotiation row.

    Returns top-N most-active lanes (by quote count), ordered by quote volume.
    """
    lanes = data.get("lane_summary", {}) or {}
    rows = []
    for lane, s in lanes.items():
        if (s.get("quotes") or 0) < 2:
            continue
        rows.append({
            "lane": lane,
            "quotes": s["quotes"],
            "wins": s["wins"],
            "losses": s["losses"],
            "win_rate_pct": s["win_rate_pct"],
            "teu_won": s["teu_won"],
            "winning_carriers": s.get("winning_carriers") or [],
            "all_carriers": s.get("all_carriers") or [],
            "rate_won_median": s.get("rate_won_median"),
            "rate_won_min": s.get("rate_won_min"),
            "rate_won_max": s.get("rate_won_max"),
            "rate_lost_median": s.get("rate_lost_median"),
            "price_gap_median": s.get("price_gap_median"),
            "transit_median_days": s.get("transit_median_days"),
            "transit_min_days": s.get("transit_min_days"),
            "last_request_date": s.get("last_request_date"),
        })
    rows.sort(key=lambda r: (-r["quotes"], -r["teu_won"]))
    return rows[:top_n]


def analyze_carrier_cooling(data: dict) -> list[dict]:
    """Carriers with ≥COOLING_HISTORICAL_MIN_QUOTES total but zero quotes
    in the last COOLING_WINDOW_DAYS."""
    today = datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=COOLING_WINDOW_DAYS)).isoformat()
    carriers = data.get("carrier_summary", {}) or {}
    cooling = []
    for c, s in carriers.items():
        if (s.get("quotes") or 0) < COOLING_HISTORICAL_MIN_QUOTES:
            continue
        last_q = s.get("last_quote_date") or ""
        if last_q < cutoff:
            cooling.append({
                "carrier": c,
                "total_quotes": s["quotes"],
                "total_wins": s["wins"],
                "win_rate_pct": s["win_rate_pct"],
                "last_quote_date": last_q,
                "days_silent": (today - datetime.fromisoformat(last_q).date()).days
                    if last_q else None,
            })
    cooling.sort(key=lambda x: -x["total_quotes"])
    return cooling


def analyze_lane_regression(data: dict) -> list[dict]:
    """Lanes where the LAST N quotes were all losses (losing streak)."""
    today = datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=45)).isoformat()
    quotes_by_lane: dict[str, list[dict]] = defaultdict(list)
    for q in data.get("quotes", []) or []:
        d = q.get("request_date") or ""
        if d < cutoff:
            continue
        if q.get("status") not in ("WIN", "LOSS"):
            continue
        if q.get("status") == "LOSS" and q.get("loss_reason") == "NO_RESPONSE":
            continue  # NQ doesn't count
        quotes_by_lane[q.get("lane")].append(q)

    regressing = []
    for lane, qs in quotes_by_lane.items():
        if not lane:
            continue
        qs_sorted = sorted(qs, key=lambda q: q.get("request_date") or "")
        recent = qs_sorted[-REGRESSION_LOSING_STREAK_MIN:]
        if len(recent) < REGRESSION_LOSING_STREAK_MIN:
            continue
        if all(q.get("status") == "LOSS" for q in recent):
            # Compute median price gap if we have any prior winning rate
            won_rates = [q.get("ol_rate") for q in qs_sorted
                         if q.get("status") == "WIN" and q.get("ol_rate")]
            lost_rates = [q.get("ol_rate") for q in recent if q.get("ol_rate")]
            gap = None
            if won_rates and lost_rates:
                won_med = sorted(won_rates)[len(won_rates) // 2]
                lost_med = sorted(lost_rates)[len(lost_rates) // 2]
                gap = lost_med - won_med
            regressing.append({
                "lane": lane,
                "losing_streak": REGRESSION_LOSING_STREAK_MIN,
                "loss_dates": [q.get("request_date") for q in recent],
                "lost_carriers": list({q.get("carrier_quoted") for q in recent
                                       if q.get("carrier_quoted")}),
                "median_lost_rate": sorted(lost_rates)[len(lost_rates) // 2]
                    if lost_rates else None,
                "median_won_rate_historical": sorted(won_rates)[len(won_rates) // 2]
                    if won_rates else None,
                "price_gap": gap,
            })
    regressing.sort(key=lambda x: -(x.get("price_gap") or 0))
    return regressing


def analyze_winning_rate_trends(data: dict) -> dict:
    """Per-carrier rate + transit trends — what we're winning at, how fast."""
    carriers = data.get("carrier_summary", {}) or {}
    trends = {}
    for c, s in carriers.items():
        if (s.get("quotes") or 0) < 3:
            continue
        trends[c] = {
            "rate_min": s.get("rate_min"),
            "rate_median": s.get("rate_median"),
            "rate_max": s.get("rate_max"),
            "transit_median_days": s.get("transit_median_days"),
            "transit_min_days": s.get("transit_min_days"),
            "lanes": s.get("lane_count"),
            "quotes": s["quotes"],
            "wins": s["wins"],
            "win_rate_pct": s["win_rate_pct"],
        }
    return trends


# ───────────────────────────────────────────────────────────────────────
# HTML rendering
# ───────────────────────────────────────────────────────────────────────

FONT = "'Inter','Segoe UI',-apple-system,Helvetica,sans-serif"


def render_section_html(analysis: dict) -> str:
    cheat = analysis["lane_cheat_sheet"]
    cooling = analysis["carrier_cooling"]
    regressing = analysis["lane_regression"]
    trends = analysis["winning_rate_trends"]

    parts = [f"""
<h2 style="margin:24px 0 8px;color:#1a3d9c;font-size:16px;border-bottom:2px solid #76b82a;padding-bottom:6px">
  💰 Rate Negotiation Intelligence
</h2>
<p style="margin:0 0 12px;font-size:12px;color:#64748b">
  Reads from the shared cross-project client-intelligence store
  (<code style="font-size:11px">SHARED/client_intelligence/hilmar</code>).
  Same store powers the rate-tracker for other clients — cross-client
  insights become trivial.
</p>
"""]

    # 1. Lane Cheat Sheet — 2026-05-19 Task #8 (Michael "on price levels.. latest
    # is the latest date per what container size? what is the median? is this
    # per container and how are you coming up with this? clarity is needed").
    # Added a precise note + tooltipped column headers explaining basis.
    parts.append("""
<h3 style="margin:16px 0 6px;font-size:14px;color:#0f172a">📋 Lane Cheat Sheet (top 15 by volume)</h3>
<p style="margin:0 0 6px;font-size:11px;color:#64748b;line-height:1.4">
  <strong>How to read this table:</strong> rates are normalized to <strong>per FEU</strong>
  (40' equivalent — 20' rates are doubled) so different container sizes compare
  apples-to-apples. <strong>Won median</strong> = the median of every WINNING rate
  for this lane (the middle value when sorted). <strong>Lost median</strong> = same
  median across Q&amp;L rates. <strong>Gap</strong> = Lost − Won (positive = wins are
  cheaper, which is the normal pattern). Sample includes all rates in the data
  window; outliers don't skew median like they would mean.
</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:18px">
<tr style="background:#0a2350;color:white">
  <th style="padding:6px;text-align:left" title="Origin → Destination">Lane</th>
  <th style="padding:6px;text-align:center" title="Number of rate quotes received (any status)">Quotes (#)</th>
  <th style="padding:6px;text-align:center" title="Wins / decided requests on this lane">Win Rate</th>
  <th style="padding:6px;text-align:center" title="Total TEU won (booked) on this lane">TEU Won</th>
  <th style="padding:6px;text-align:left" title="Carriers that won bookings on this lane">Winning Carriers</th>
  <th style="padding:6px;text-align:center" title="Median of WIN rates on this lane, per FEU (40' equivalent)">Won median ($/FEU)</th>
  <th style="padding:6px;text-align:center" title="Median of Quoted &amp; Lost rates on this lane, per FEU. This is what we got beat by.">Lost median ($/FEU)</th>
  <th style="padding:6px;text-align:center" title="Lost median − Won median. Positive = wins are cheaper than losses (normal).">Gap ($/FEU)</th>
  <th style="padding:6px;text-align:center" title="Median transit time in days for completed bookings on this lane">Transit median (days)</th>
</tr>""")
    # Compute max TEU for bar scaling
    max_teu = max((r["teu_won"] for r in cheat), default=1) or 1
    for i, r in enumerate(cheat):
        bg = "#ffffff" if i % 2 == 0 else "#f8fafc"
        gap = r.get("price_gap_median")
        transit = r.get("transit_median_days")
        # Win-rate heatmap
        wr_bg = V.heatmap_color(r['win_rate_pct'], vmin=0, vmax=100, mode="good_high")
        # Transit speed heatmap (lower = better)
        transit_bg = V.heatmap_color(transit, vmin=10, vmax=45, mode="good_low") if transit else "transparent"
        # Gap heatmap (negative gap = winning at lower rate = good)
        gap_bg = V.heatmap_color(gap if gap is not None else 0, vmin=-500, vmax=500, mode="good_low") if gap is not None else "transparent"
        # TEU bar
        teu_bar = V.bar_cell(r['teu_won'], max_teu, color="#16a34a", label=str(r['teu_won']), width_px=60)
        transit_str = f"{transit}d" if transit else "—"
        parts.append(f"""
<tr style="background:{bg}">
  <td style="padding:5px 8px;font-weight:600">{_esc(r['lane'])}</td>
  <td style="padding:5px;text-align:center">{r['quotes']}</td>
  <td style="padding:5px;text-align:center;background:{wr_bg};font-weight:600">{_pct(r['win_rate_pct'])}</td>
  <td style="padding:5px 8px;text-align:left">{teu_bar}</td>
  <td style="padding:5px 8px;font-size:11px">{_esc(", ".join(r['winning_carriers']) or "—")}</td>
  <td style="padding:5px;text-align:center;color:#16a34a;font-weight:600">{_money(r.get('rate_won_median'))}</td>
  <td style="padding:5px;text-align:center;color:#dc2626">{_money(r.get('rate_lost_median'))}</td>
  <td style="padding:5px;text-align:center;background:{gap_bg};font-weight:600">{_money(gap)}</td>
  <td style="padding:5px;text-align:center;background:{transit_bg};font-weight:600">{_esc(transit_str)}</td>
</tr>""")
    parts.append("</table>")

    # 2. Carrier Cooling
    parts.append("""
<h3 style="margin:18px 0 6px;font-size:14px;color:#0f172a">🥶 Carriers Cooling (silent ≥14 days)</h3>""")
    if cooling:
        parts.append('<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:12px">')
        parts.append("""
<tr style="background:#0a2350;color:white">
  <th style="padding:6px;text-align:left">Carrier</th>
  <th style="padding:6px;text-align:center">Total Quotes</th>
  <th style="padding:6px;text-align:center">Total Wins</th>
  <th style="padding:6px;text-align:center">Win %</th>
  <th style="padding:6px;text-align:left">Last Quote</th>
  <th style="padding:6px;text-align:center">Days Silent</th>
</tr>""")
        for i, c in enumerate(cooling):
            bg = "#fef3c7" if c["days_silent"] and c["days_silent"] > 21 else "#fffbeb"
            parts.append(f"""
<tr style="background:{bg}">
  <td style="padding:5px 8px;font-weight:600">{_esc(c['carrier'])}</td>
  <td style="padding:5px;text-align:center">{c['total_quotes']}</td>
  <td style="padding:5px;text-align:center">{c['total_wins']}</td>
  <td style="padding:5px;text-align:center">{_pct(c['win_rate_pct'])}</td>
  <td style="padding:5px 8px">{_esc(c['last_quote_date'])}</td>
  <td style="padding:5px;text-align:center;color:#92400e;font-weight:600">{c['days_silent']}d</td>
</tr>""")
        parts.append("</table>")
    else:
        parts.append('<p style="margin:0 0 12px;color:#64748b;font-size:12px;font-style:italic">No carriers cooling — all active carriers quoted in the last 14 days.</p>')

    # 3. Lane Regression
    parts.append("""
<h3 style="margin:18px 0 6px;font-size:14px;color:#0f172a">📉 Lanes Regressing (3+ losing streak)</h3>""")
    if regressing:
        parts.append('<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:12px">')
        parts.append("""
<tr style="background:#7f1d1d;color:white">
  <th style="padding:6px;text-align:left">Lane</th>
  <th style="padding:6px;text-align:center">Streak</th>
  <th style="padding:6px;text-align:left">Losing Carriers</th>
  <th style="padding:6px;text-align:center">Lost median</th>
  <th style="padding:6px;text-align:center">Historical Won median</th>
  <th style="padding:6px;text-align:center">Price Gap</th>
</tr>""")
        for r in regressing:
            parts.append(f"""
<tr style="background:#fef2f2">
  <td style="padding:5px 8px;font-weight:600">{_esc(r['lane'])}</td>
  <td style="padding:5px;text-align:center;color:#dc2626;font-weight:700">{r['losing_streak']}</td>
  <td style="padding:5px 8px;font-size:11px">{_esc(", ".join(r['lost_carriers']) or "—")}</td>
  <td style="padding:5px;text-align:center">{_money(r['median_lost_rate'])}</td>
  <td style="padding:5px;text-align:center;color:#16a34a">{_money(r['median_won_rate_historical'])}</td>
  <td style="padding:5px;text-align:center;color:#dc2626;font-weight:700">{_money(r['price_gap'])}</td>
</tr>""")
        parts.append("</table>")
    else:
        parts.append('<p style="margin:0 0 12px;color:#64748b;font-size:12px;font-style:italic">No lanes with 3+ losing streak — all lanes either winning or mixed recently.</p>')

    # 4. Carrier Rate Trends — compact view
    # 2026-05-19 Task #8: per-FEU normalization + median definition tooltips.
    parts.append("""
<h3 style="margin:18px 0 6px;font-size:14px;color:#0f172a">📊 Carrier Rate + Transit Ranges (current)</h3>
<p style="margin:0 0 6px;font-size:11px;color:#64748b;line-height:1.4">
  Rate columns are <strong>per FEU</strong> (40' equivalent; 20' rates doubled).
  <strong>Min / median / max</strong> are the lowest, middle, and highest rates
  this carrier quoted in the data window — across all lanes. Use median (not
  mean) because outlier reefer or back-haul rates would skew the average.
</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:8px">
<tr style="background:#0a2350;color:white">
  <th style="padding:6px;text-align:left" title="Steamship carrier">Carrier</th>
  <th style="padding:6px;text-align:center" title="Number of rate quotes from this carrier in the data window">Quotes (#)</th>
  <th style="padding:6px;text-align:center" title="Wins / Quotes for this carrier">Win Rate</th>
  <th style="padding:6px;text-align:center" title="Distinct lanes this carrier quoted on">Lanes (#)</th>
  <th style="padding:6px;text-align:center" title="Lowest rate this carrier quoted, per FEU">Rate min ($/FEU)</th>
  <th style="padding:6px;text-align:center" title="Median of all rates this carrier quoted, per FEU">Rate median ($/FEU)</th>
  <th style="padding:6px;text-align:center" title="Highest rate this carrier quoted, per FEU">Rate max ($/FEU)</th>
  <th style="padding:6px;text-align:center" title="Median transit time (days) for completed bookings with this carrier">Transit median (days)</th>
</tr>""")
    max_q = max((t["quotes"] for t in trends.values()), default=1)
    for c, t in sorted(trends.items(), key=lambda x: -x[1]["quotes"]):
        transit_med = t.get("transit_median_days")
        transit_str = f"{transit_med}d" if transit_med else "—"
        wr_bg = V.heatmap_color(t['win_rate_pct'], vmin=0, vmax=100, mode="good_high")
        transit_bg = V.heatmap_color(transit_med, vmin=10, vmax=45, mode="good_low") if transit_med else "transparent"
        q_bar = V.bar_cell(t['quotes'], max_q, color="#3b82f6", label=str(t['quotes']), width_px=55)
        parts.append(f"""
<tr style="background:#ffffff">
  <td style="padding:5px 8px;font-weight:600">{_esc(c)}</td>
  <td style="padding:5px 8px;text-align:left">{q_bar}</td>
  <td style="padding:5px;text-align:center;background:{wr_bg};font-weight:600">{_pct(t['win_rate_pct'])}</td>
  <td style="padding:5px;text-align:center">{t['lanes']}</td>
  <td style="padding:5px;text-align:center">{_money(t.get('rate_min'))}</td>
  <td style="padding:5px;text-align:center;font-weight:600">{_money(t.get('rate_median'))}</td>
  <td style="padding:5px;text-align:center">{_money(t.get('rate_max'))}</td>
  <td style="padding:5px;text-align:center;background:{transit_bg};font-weight:600">{_esc(transit_str)}</td>
</tr>""")
    parts.append("</table>")

    return "".join(parts)


def render_full_page(analysis: dict) -> str:
    """Standalone HTML page (for direct viewing or PDF export)."""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hilmar — Rate Negotiation Intelligence</title>
<style>
body{{font-family:{FONT};padding:20px;background:#f1f5f9;color:#0f172a;font-size:13px}}
.container{{max-width:1200px;margin:0 auto;background:white;padding:24px;border-radius:8px}}
</style></head><body><div class="container">
<h1 style="margin:0 0 8px;font-size:22px">💰 Hilmar — Rate Negotiation Intelligence</h1>
<p style="margin:0 0 16px;color:#64748b;font-size:12px">
Generated {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}
</p>
{render_section_html(analysis)}
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default="hilmar")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    data = _load_client(args.client)
    analysis = {
        "client": args.client,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lane_cheat_sheet": analyze_lane_cheat_sheet(data),
        "carrier_cooling": analyze_carrier_cooling(data),
        "lane_regression": analyze_lane_regression(data),
        "winning_rate_trends": analyze_winning_rate_trends(data),
    }

    REPORTS.mkdir(parents=True, exist_ok=True)
    section_path = REPORTS / "rate-intelligence-section.html"
    page_path = REPORTS / "rate-intelligence.html"
    json_path = REPORTS / "rate-intelligence.json"

    section_path.write_text(render_section_html(analysis), encoding="utf-8")
    page_path.write_text(render_full_page(analysis), encoding="utf-8")
    json_path.write_text(json.dumps(analysis, indent=2, default=str), encoding="utf-8")

    if not args.quiet:
        print(f"✅ Rate intelligence:")
        print(f"   Lanes analyzed: {len(analysis['lane_cheat_sheet'])}")
        print(f"   Cooling carriers: {len(analysis['carrier_cooling'])}")
        print(f"   Regressing lanes: {len(analysis['lane_regression'])}")
        print(f"   Outputs: {section_path.name}, {page_path.name}, {json_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
