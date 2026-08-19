"""
auto_chase_pending.py — Send Lonny a soft nudge on PENDING quotes that
have aged past the SLA threshold.

Per Michael 2026-05-13: "i love all of this and want it now" (Tier 1.3 —
pending-overdue auto-chase). This script:
  1. Identifies PENDING rows where the OL response is ≥24h old (QC-007
     trigger threshold) AND Lonny hasn't replied with accept/reject.
  2. Composes a soft chase email per row: subject + body referencing the
     specific lane, carrier, and rate.
  3. Sends ONLY if config.json.auto_chase.enabled == true (default false —
     never live until Michael explicitly enables).
  4. Idempotent via reports/chase-sent-YYYY-MM-DD.flag — never chases the
     same MDOLX twice in the same day.

GUARDRAILS
  - Default off (enabled=false in config)
  - Max chases per day capped (default 3 — avoid spam)
  - End-of-business-day timing only (≥4 PM ET — let Lonny respond
    organically first)
  - One nudge per request per day — track in flag
  - Dry-run mode prints what would send without actually sending

To enable in production:
  edit config.json:
    "auto_chase": {
      "enabled": true,
      "max_per_day": 3,
      "earliest_send_hour_et": 16,
      "min_age_hours": 24
    }

CLI:
  python scripts/auto_chase_pending.py --dry      # show what would send
  python scripts/auto_chase_pending.py            # send (respects config)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
DATA = ROOT / "tracking-data-v2.json"


def _config() -> dict:
    cfg = core.load_config(ROOT / "config.json")
    return cfg.get("auto_chase", {})


def _find_overdue_pending(data: dict, min_age_hours: int) -> list[dict]:
    """PENDING rows ≥ min_age_hours old, measured from the OL quote when we
    have one and from Lonny's request when the quote is undated.

    2026-08-13: this used to `continue` on a missing response_timestamp, so a
    quote carrying a rate but no parseable response time was never chased — it
    sat PENDING indefinitely with no detector and no follow-up, which is the
    same blind spot QC-007 and gen_improvements_report had.

    Rows admitted on the REQUEST anchor are marked `_age_dated=False`.
    _build_chase_email MUST branch on it: this email goes to the CLIENT, and
    telling Lonny "your quote from 6 days ago" off a request timestamp states
    a quote time we cannot evidence. Fabricated timing has shipped from this
    repo once already (core.TIMING_VALID_FROM exists because of it); it is not
    going out over Michael's signature.
    """
    out = []
    now = datetime.now(timezone.utc)
    for r in data.get("requests", []):
        if r.get("status") != "PENDING":
            continue
        # core.response_time_is_evidenced, NOT `response_timestamp is not
        # None`. A borrowed date (copied off another row's quote by
        # qc_selfheal's sibling heal) is not a minute OL sent anything, and
        # `dated` licenses the "quote from N days ago" wording below — which
        # is exactly the fabrication this function's docstring forbids.
        # 2026-08-19: this file sends to lupfold@hilmaringredients.com.
        ts = (core.parse_iso(r.get("response_timestamp"))
              if core.response_time_is_evidenced(r) else None)
        dated = ts is not None
        if ts is None:
            ts = core.parse_iso(r.get("request_timestamp") or r.get("request_date"))
        if not ts:
            continue
        hrs = (now - ts).total_seconds() / 3600.0
        if hrs >= min_age_hours:
            r["_age_hours"] = hrs
            r["_age_dated"] = dated
            out.append(r)
    out.sort(key=lambda r: -r["_age_hours"])
    return out


def _build_chase_email(r: dict) -> tuple[str, str]:
    """Compose soft-chase subject + body for one PENDING row."""
    lane = r.get("lane") or f"{r.get('origin', '?')} → {r.get('destination', '?')}"
    carrier = r.get("carrier_quoted") or r.get("carrier_won") or "(carrier TBD)"
    rate = r.get("ol_rate")
    rate_str = f"${rate:,.0f}" if rate else "(rate TBD)"
    containers = r.get("containers") or "?"
    etd = r.get("etd_offered") or "?"
    age_h = r.get("_age_hours", 0)
    days = int(age_h / 24)
    # Is age_h measured from OUR quote, or from Lonny's request because the
    # quote carries no usable timestamp? Only the first licenses "quote from N
    # days ago" — see _find_overdue_pending. Defaults True so any caller that
    # builds an email from a row this module did not select keeps the old copy.
    dated = r.get("_age_dated", True)
    if dated:
        opener = f"quote from {days} days ago" if days >= 1 else "recent quote"
        footer = f"sent because the quote is {age_h:.0f}h old without a reply"
    else:
        # No quote time on record. Anchor the sentence on the request, which
        # IS evidenced, and say nothing about when we answered.
        opener = f"request from {days} days ago" if days >= 1 else "recent request"
        footer = (f"sent because the request is {age_h:.0f}h old without a reply "
                  f"(our quote on it carries no timestamp)")

    subject = f"Quick check — {lane}, {containers} {('(') if rate else ''}{rate_str}{(')') if rate else ''}"
    body = f"""<html><body style="font-family:'Segoe UI',Arial,sans-serif;font-size:14px;color:#0f172a">
<p>Hi Lonny,</p>

<p>Quick check on this {opener} — let me know how you'd like to proceed:</p>

<table style="border-collapse:collapse;margin:12px 0;font-size:13px">
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Lane</td><td style="padding:4px 0;font-weight:600">{lane}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Containers</td><td style="padding:4px 0">{containers}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Carrier</td><td style="padding:4px 0">{carrier}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Rate</td><td style="padding:4px 0">{rate_str}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">ETD</td><td style="padding:4px 0">{etd}</td></tr>
</table>

<p>If you'd like to book, just reply confirming. If you're going with a different carrier, no worries — just let me know.</p>

<p>Thanks,<br>Michael</p>
<p style="font-size:11px;color:#94a3b8">Auto-generated chase from the Hilmar tracker — {footer}.</p>
</body></html>"""
    return subject, body


def _already_chased_today(r: dict, flag_path: Path) -> bool:
    if not flag_path.exists():
        return False
    rid = r.get("request_id") or ""
    return rid in flag_path.read_text(encoding="utf-8", errors="ignore")


def _record_chase(r: dict, req_id: str, flag_path: Path):
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    with flag_path.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} "
                f"req={r.get('request_id')} "
                f"lane={r.get('lane')} "
                f"sent_id={req_id}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="Show what would send without sending")
    ap.add_argument("--force", action="store_true",
                    help="Send even if config.auto_chase.enabled == false (manual override)")
    args = ap.parse_args()

    chase_cfg = _config()
    enabled = chase_cfg.get("enabled", False)
    max_per_day = chase_cfg.get("max_per_day", 3)
    earliest_hour = chase_cfg.get("earliest_send_hour_et", 16)
    min_age = chase_cfg.get("min_age_hours", 24)
    lonny_email = chase_cfg.get("recipient", "lupfold@hilmaringredients.com")

    if not enabled and not args.force and not args.dry:
        print("ℹ  auto_chase.enabled == false in config.json — no chases sent (use --dry to preview, --force to override)")
        return 0

    # Time gate — Lonny's PT day, end-of-OL-biz-day
    now_et = datetime.now(core.ET)
    if not args.dry and not args.force and now_et.hour < earliest_hour:
        print(f"⏰  current hour {now_et.hour}:00 ET < earliest_send_hour_et={earliest_hour}; "
              "skipping — chases run EOD only")
        return 0

    data = json.loads(DATA.read_text(encoding="utf-8"))
    overdue = _find_overdue_pending(data, min_age)
    if not overdue:
        print("✅ No PENDING rows past SLA — nothing to chase.")
        return 0

    flag_path = REPORTS / f"chase-sent-{datetime.now().strftime('%Y-%m-%d')}.flag"
    sent_today = 0
    skipped = 0

    print(f"Found {len(overdue)} overdue PENDING rows (≥{min_age}h since quote):")
    for r in overdue:
        if sent_today >= max_per_day:
            print(f"  (max_per_day={max_per_day} reached — stopping)")
            break
        if _already_chased_today(r, flag_path):
            print(f"  SKIP {r.get('request_id', '?')[:18]} — already chased today")
            skipped += 1
            continue

        subject, body = _build_chase_email(r)
        print(f"\n  📧 {r.get('lane')} | {r['_age_hours']:.0f}h old | {r.get('carrier_quoted') or '?'}")
        print(f"     SUBJECT: {subject}")

        if args.dry:
            print(f"     DRY — would send to {lonny_email}")
            continue

        # Send via outlook_send.py — but as a nudge, NOT the daily/audit shape
        import outlook_send as OS
        try:
            req_id = OS.send_mail(
                to=[lonny_email],
                cc=[chase_cfg.get("cc", "michael.deitchman@idealx.us")],
                subject=subject,
                html_body=body,
                attachments=[],
            )
            print(f"     ✅ Sent. request-id={req_id}")
            _record_chase(r, req_id, flag_path)
            sent_today += 1
        except Exception as e:
            print(f"     ❌ Send failed: {e}")

    print(f"\nauto_chase: {sent_today} sent, {skipped} skipped (already chased today)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
