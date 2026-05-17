"""
reconcile_with_quote_tracker.py — Cross-check Hilmar wins against ol-quote-tracker.

Per Michael 2026-05-16: "you see hilmar data is also on there as a good check
point for won bookings".

PREMISE
Both systems independently ingest the same OL inbox emails:
  - Hilmar tracker: refresh_stage.py pulls Lonny↔OL threads + HILMAR booking
    confirmations via Microsoft Graph from michael.deitchman@ol-usa.com
  - ol-quote-tracker: ingests from multiple OL operator inboxes incl. the
    booking-confirmation senders, classifies entities, stores in Turso

For wins (confirmed bookings), counts SHOULD match. Any drift means one
system missed an email or mis-classified it — surfaces a real data-quality
bug.

WHAT THIS CHECKS
1. Count of Hilmar wins per system (last 60 days)
2. Total TEU won per system (last 60 days)
3. Per-month win count match
4. Lane-level overlap (lanes in Hilmar but not in QT, vice versa)

WHAT IT DOESN'T DO
- Doesn't try to match individual rows by MDOLX (QT stores quoteNumber as
  OL booking number "00+074660", not MDOLX). Aggregate reconciliation is
  enough to surface drift; per-row debugging is manual.

OUTPUT
- reports/reconcile-quote-tracker.json — machine-readable reconciliation
- reports/reconcile-quote-tracker-section.html — embeddable in audit
- reports/quote-tracker-reconcile.log — audit trail (one line per run)

CLI
  python scripts/reconcile_with_quote_tracker.py            # full reconcile
  python scripts/reconcile_with_quote_tracker.py --window 90 # 90-day window
  python scripts/reconcile_with_quote_tracker.py --dry      # don't write artifacts
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import sync_to_quote_tracker as STQ  # noqa: E402  reuse _load_password() + DEFAULT_API_BASE

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"

HILMAR_ALIASES = {"hilmar", "hilmar ingredients", "hilmar cheese", "hilmar inc"}
HILMAR_DOMAINS = {"hilmaringredients.com"}


def _is_hilmar(qt_row: dict) -> bool:
    """Best-effort match: clientCompany name, alias, or email domain."""
    company = (qt_row.get("clientCompany") or "").strip().lower()
    if any(a in company or company in a for a in HILMAR_ALIASES):
        return True
    email = (qt_row.get("clientEmail") or "").strip().lower()
    if "@" in email:
        dom = email.split("@", 1)[1]
        if dom in HILMAR_DOMAINS:
            return True
    return False


def _ymd(s: str | None) -> str | None:
    """Normalize a timestamp/date string to YYYY-MM-DD. None on failure."""
    if not s:
        return None
    return s[:10] if len(s) >= 10 else None


def _ym(s: str | None) -> str | None:
    """YYYY-MM from a date/timestamp."""
    if not s:
        return None
    return s[:7] if len(s) >= 7 else None


def fetch_qt_hilmar_wins(base_url: str, password: str, window_days: int = 60) -> list[dict]:
    """Pull Hilmar wins from ol-quote-tracker via /api/quotes?status=won.
    Filters client-side to clientCompany == Hilmar Ingredients (or aliases).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).date().isoformat()
    session = requests.Session()
    # Login
    r = session.post(f"{base_url}/api/auth/login",
                     json={"password": password}, timeout=30)
    if not (200 <= r.status_code < 300):
        raise RuntimeError(f"login {base_url} → {r.status_code}: {r.text[:200]}")
    # Pull all won quotes (API returns all; we filter Hilmar + window client-side)
    r = session.get(f"{base_url}/api/quotes",
                    params={"status": "won"}, timeout=60)
    if not (200 <= r.status_code < 300):
        raise RuntimeError(f"/api/quotes → {r.status_code}: {r.text[:200]}")
    all_rows = r.json()
    if not isinstance(all_rows, list):
        # API might wrap in {data: [...]} — handle both
        all_rows = all_rows.get("data") or all_rows.get("quotes") or []
    hilmar_rows = [
        q for q in all_rows
        if _is_hilmar(q) and (_ymd(q.get("requestedAt")) or "") >= cutoff
    ]
    return hilmar_rows


def load_hilmar_wins(window_days: int = 60) -> list[dict]:
    """Hilmar's local wins from tracking-data-v2.json within the window."""
    data_path = ROOT / "tracking-data-v2.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).date().isoformat()
    return [
        r for r in (data.get("requests") or [])
        if r.get("status") == "WIN"
        and (r.get("request_date") or "") >= cutoff
    ]


def _row_lane(r: dict, source: str) -> str:
    if source == "qt":
        o = (r.get("origin") or "?").strip().title()
        d = (r.get("destination") or "?").strip().title()
        return f"{o} → {d}"
    return r.get("lane") or "Unknown → Unknown"


def _row_teu(r: dict, source: str) -> int:
    if source == "qt":
        # equipment string parsing — best effort
        eq = (r.get("equipment") or "")
        import re
        m = re.search(r"(\d+)\s*[xX]\s*(\d{2})", eq)
        if m:
            count, size = int(m.group(1)), int(m.group(2))
            return count * (2 if size == 40 else 1)
        return 0
    return int(r.get("teu_won") or r.get("teu_requested") or 0)


def reconcile(qt_rows: list[dict], hilmar_rows: list[dict], window_days: int) -> dict:
    qt_count = len(qt_rows)
    hi_count = len(hilmar_rows)
    qt_teu = sum(_row_teu(r, "qt") for r in qt_rows)
    hi_teu = sum(_row_teu(r, "hi") for r in hilmar_rows)

    # Per-month breakdown
    qt_by_month = Counter(_ym(r.get("requestedAt")) for r in qt_rows)
    hi_by_month = Counter(_ym(r.get("request_date")) for r in hilmar_rows)
    months = sorted(set(qt_by_month) | set(hi_by_month))

    # Lane sets (case-insensitive comparison)
    def _norm_lane(s):
        return (s or "").strip().lower()
    qt_lanes = Counter(_norm_lane(_row_lane(r, "qt")) for r in qt_rows)
    hi_lanes = Counter(_norm_lane(_row_lane(r, "hi")) for r in hilmar_rows)
    all_lanes = sorted(set(qt_lanes) | set(hi_lanes))
    lane_drift = []
    for lane in all_lanes:
        qt_n = qt_lanes.get(lane, 0)
        hi_n = hi_lanes.get(lane, 0)
        if qt_n != hi_n:
            lane_drift.append({"lane": lane, "qt_wins": qt_n, "hilmar_wins": hi_n,
                                "delta": hi_n - qt_n})
    lane_drift.sort(key=lambda x: -abs(x["delta"]))

    drift_count = hi_count - qt_count
    drift_teu = hi_teu - qt_teu

    return {
        "window_days": window_days,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "qt_total_wins": qt_count,
        "hilmar_total_wins": hi_count,
        "win_count_delta": drift_count,
        "qt_total_teu": qt_teu,
        "hilmar_total_teu": hi_teu,
        "teu_delta": drift_teu,
        "by_month": [
            {"month": m, "qt": qt_by_month.get(m, 0), "hilmar": hi_by_month.get(m, 0),
             "delta": hi_by_month.get(m, 0) - qt_by_month.get(m, 0)}
            for m in months
        ],
        "lane_drift": lane_drift[:20],  # top 20 mismatches
        "lanes_total": len(all_lanes),
        "lanes_matched": sum(1 for l in all_lanes if qt_lanes.get(l, 0) == hi_lanes.get(l, 0)),
    }


def render_section_html(rec: dict) -> str:
    """Embed-ready section for the daily audit."""
    drift = rec["win_count_delta"]
    teu_drift = rec["teu_delta"]
    color = "#16a34a" if drift == 0 and teu_drift == 0 else ("#f59e0b" if abs(drift) <= 2 else "#dc2626")
    status_emoji = "✅" if drift == 0 and teu_drift == 0 else ("⚠️" if abs(drift) <= 2 else "🔴")
    drift_str = f"+{drift}" if drift > 0 else str(drift)
    teu_drift_str = f"+{teu_drift}" if teu_drift > 0 else str(teu_drift)

    month_rows = "".join(
        f'<tr><td style="padding:4px 8px">{m["month"]}</td>'
        f'<td style="padding:4px;text-align:center">{m["qt"]}</td>'
        f'<td style="padding:4px;text-align:center">{m["hilmar"]}</td>'
        f'<td style="padding:4px;text-align:center;font-weight:600;color:{"#dc2626" if m["delta"] != 0 else "#16a34a"}">'
        f'{("+" if m["delta"] > 0 else "")}{m["delta"]}</td></tr>'
        for m in rec["by_month"]
    )

    lane_rows = "".join(
        f'<tr><td style="padding:4px 8px">{ld["lane"]}</td>'
        f'<td style="padding:4px;text-align:center">{ld["qt_wins"]}</td>'
        f'<td style="padding:4px;text-align:center">{ld["hilmar_wins"]}</td>'
        f'<td style="padding:4px;text-align:center;color:#dc2626;font-weight:600">'
        f'{("+" if ld["delta"] > 0 else "")}{ld["delta"]}</td></tr>'
        for ld in rec["lane_drift"][:10]
    )
    lane_section = ""
    if rec["lane_drift"]:
        lane_section = f"""
<h4 style="margin:12px 0 4px;color:#0f172a;font-size:13px">Top lane mismatches</h4>
<table style="width:100%;border-collapse:collapse;font-size:12px">
<tr style="background:#0a2350;color:white">
  <th style="padding:5px;text-align:left">Lane</th>
  <th style="padding:5px;text-align:center">QT wins</th>
  <th style="padding:5px;text-align:center">Hilmar wins</th>
  <th style="padding:5px;text-align:center">Δ</th>
</tr>
{lane_rows}
</table>"""

    return f"""
<h2 style="margin:24px 0 8px;color:#1a3d9c;font-size:16px;border-bottom:2px solid #76b82a;padding-bottom:6px">
  🔁 ol-quote-tracker Reconciliation (last {rec["window_days"]} days)
</h2>
<p style="margin:0 0 8px;font-size:12px;color:#64748b">
  Cross-check: both systems ingest the same OL inbox emails. Hilmar wins should match ol-quote-tracker
  wins under <code>clientCompany=Hilmar Ingredients</code>. Drift = one system missed or mis-classified.
</p>
<div style="background:#f8fafc;border-left:4px solid {color};padding:10px 14px;margin:6px 0 12px;border-radius:4px">
  <div style="font-size:13px;font-weight:600;color:{color}">
    {status_emoji} Win count delta: {drift_str} &nbsp;&nbsp; TEU delta: {teu_drift_str}
  </div>
  <div style="font-size:11px;color:#475569;margin-top:4px">
    Quote tracker: {rec["qt_total_wins"]} wins / {rec["qt_total_teu"]} TEU &nbsp;&middot;&nbsp;
    Hilmar local: {rec["hilmar_total_wins"]} wins / {rec["hilmar_total_teu"]} TEU
  </div>
  <div style="font-size:11px;color:#475569;margin-top:2px">
    Lanes matched: {rec["lanes_matched"]}/{rec["lanes_total"]}
  </div>
</div>

<h4 style="margin:12px 0 4px;color:#0f172a;font-size:13px">Per-month breakdown</h4>
<table style="width:100%;border-collapse:collapse;font-size:12px">
<tr style="background:#0a2350;color:white">
  <th style="padding:5px;text-align:left">Month</th>
  <th style="padding:5px;text-align:center">QT wins</th>
  <th style="padding:5px;text-align:center">Hilmar wins</th>
  <th style="padding:5px;text-align:center">Δ</th>
</tr>
{month_rows}
</table>
{lane_section}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=60, help="Window days (default 60)")
    ap.add_argument("--dry", action="store_true", help="Don't write artifacts")
    ap.add_argument("--base-url", default=os.environ.get("QT_API_BASE", STQ.DEFAULT_API_BASE))
    args = ap.parse_args()

    password = STQ._load_password()
    if not password:
        print("⚠️  No APP_PASSWORD — drop in secrets/quote-tracker-pwd.txt to enable")
        return 0

    try:
        qt_rows = fetch_qt_hilmar_wins(args.base_url, password, args.window)
    except Exception as e:
        # 2026-05-17: Return 0 (NOT 1) on fetch failure so the pipeline keeps
        # running and the daily email still ships. The reconcile is a NICE-TO-
        # HAVE checkpoint — ol-quote-tracker being slow/down must NEVER break
        # the daily output. QC-038 reads the audit log and surfaces the failure
        # in the next QC pass. See pipeline incident 2026-05-17 11:31 ET where
        # /api/quotes timeout caused full pipeline FAILED status.
        print(f"⚠️  fetch from ol-quote-tracker failed (pipeline continues): {e}")
        _append_audit({"error": str(e), "ok": False})
        return 0

    hi_rows = load_hilmar_wins(args.window)
    rec = reconcile(qt_rows, hi_rows, args.window)
    print(json.dumps({k: v for k, v in rec.items() if k != "lane_drift"},
                     indent=2, default=str))
    print(f"  Lane drift: {len(rec['lane_drift'])} mismatches (top in JSON)")

    if not args.dry:
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / "reconcile-quote-tracker.json").write_text(
            json.dumps(rec, indent=2, default=str), encoding="utf-8")
        (REPORTS / "reconcile-quote-tracker-section.html").write_text(
            render_section_html(rec), encoding="utf-8")
        _append_audit({"ok": True, "delta": rec["win_count_delta"],
                       "qt": rec["qt_total_wins"], "hilmar": rec["hilmar_total_wins"]})
    return 0


def _append_audit(payload: dict):
    REPORTS.mkdir(parents=True, exist_ok=True)
    log = REPORTS / "quote-tracker-reconcile.log"
    line = f"{datetime.now(timezone.utc).isoformat()} | " + " ".join(
        f"{k}={v}" for k, v in payload.items()
    )
    with log.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    sys.exit(main())
