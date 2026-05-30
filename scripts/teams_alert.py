"""
teams_alert.py — Real-time Teams/Slack alerts for high-signal events.

Per Michael 2026-05-13 Tier 2.5. Replaces "wait for tomorrow's audit" with
push notifications for events that matter NOW:
  - 🎉 New WIN booking (carrier + lane + TEU + rate)
  - 🚨 Pending past 24h (QC-007 trigger)
  - ❌ QC ERROR / drift FAIL
  - 🔥 Big-day WIN (TEU > 30 in one day)

WEBHOOK CONFIG (in config.json):
  "alerts": {
    "teams_webhook_url": "https://outlook.office.com/webhook/...",
    "events": ["win", "pending_overdue", "qc_error", "big_day"],
    "min_teu_for_big_day": 30
  }

If teams_webhook_url is empty/missing, this script no-ops. Until Michael
adds the webhook URL, alerts are stored as JSON in reports/alerts-queue.json
for review.

CLI:
  python scripts/teams_alert.py scan        # detect events + send (or queue)
  python scripts/teams_alert.py test        # send a hello-world alert
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
DATA = ROOT / "tracking-data-v2.json"


def _config():
    cfg = core.load_config(ROOT / "config.json")
    return cfg.get("alerts", {})


def _send_teams(webhook_url: str, title: str, text: str, color: str = "0078D4") -> bool:
    """POST a Teams MessageCard. Returns True on success."""
    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": title,
        "themeColor": color,
        "title": title,
        "text": text,
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        return r.status_code in (200, 202)
    except Exception as e:
        print(f"  ⚠️  Teams POST failed: {e}")
        return False


def _queue_alert(alert: dict):
    """Persist alert to reports/alerts-queue.json for review when no webhook."""
    queue_path = REPORTS / "alerts-queue.json"
    queue = []
    if queue_path.exists():
        try:
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    alert["queued_at"] = datetime.now(timezone.utc).isoformat()
    queue.append(alert)
    queue = queue[-100:]  # cap at 100 alerts
    REPORTS.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(json.dumps(queue, indent=2, default=str), encoding="utf-8")


def _was_alerted(event_key: str) -> bool:
    """Check sent-alerts log to avoid duplicating alerts for the same event."""
    flag = REPORTS / "alerts-sent.json"
    if not flag.exists():
        return False
    try:
        sent = json.loads(flag.read_text(encoding="utf-8"))
    except Exception:
        return False
    return event_key in sent


def _record_alert(event_key: str):
    flag = REPORTS / "alerts-sent.json"
    sent = []
    if flag.exists():
        try:
            sent = json.loads(flag.read_text(encoding="utf-8"))
        except Exception:
            pass
    sent.append(event_key)
    sent = sent[-500:]
    REPORTS.mkdir(parents=True, exist_ok=True)
    flag.write_text(json.dumps(sent, default=str), encoding="utf-8")


def detect_events(data: dict, cfg: dict) -> list[dict]:
    """Find events worth alerting on since the last fire."""
    events = []
    rows = data.get("requests", []) or []
    enabled = set(cfg.get("events", []))

    # 1. New WINs (status_history shows transition to WIN today)
    today_iso = datetime.now(core.ET).date().isoformat()
    if "win" in enabled:
        for r in rows:
            for h in (r.get("status_history") or []):
                at = (h.get("at") or "")[:10]
                if at == today_iso and h.get("to") == "WIN" and h.get("from") != "WIN":
                    key = f"win:{r.get('request_id')}:{at}"
                    if not _was_alerted(key):
                        rate = r.get("ol_rate")
                        events.append({
                            "type": "win",
                            "key": key,
                            "color": "16a34a",
                            "title": f"🎉 WIN — {r.get('lane', '?')}",
                            "text": (
                                f"**Carrier**: {r.get('carrier_won') or '?'}  "
                                f"**Rate**: ${rate:,.0f}  " if rate else f"**Carrier**: {r.get('carrier_won') or '?'}  "
                            ) + (
                                f"**TEU**: {r.get('teu_won') or r.get('teu_requested')}  "
                                f"**ETD**: {r.get('etd_offered') or '?'}  "
                                f"**MDOLX**: {r.get('mdolx_ref') or '?'}"
                            ),
                        })

    # 2. Pending past 24h
    if "pending_overdue" in enabled:
        now = datetime.now(timezone.utc)
        for r in rows:
            if r.get("status") != "PENDING" or not r.get("response_timestamp"):
                continue
            ts = core.parse_iso(r["response_timestamp"])
            if not ts:
                continue
            hrs = (now - ts).total_seconds() / 3600.0
            if hrs >= 24:
                key = f"pending_overdue:{r.get('request_id')}:{today_iso}"
                if not _was_alerted(key):
                    events.append({
                        "type": "pending_overdue",
                        "key": key,
                        "color": "f59e0b",
                        "title": f"🚨 Pending {hrs:.0f}h — {r.get('lane', '?')}",
                        "text": (
                            f"OL quoted {r.get('carrier_quoted') or '?'} "
                            f"@ ${r.get('ol_rate') or 0:,.0f} "
                            f"{hrs:.0f}h ago — Lonny hasn't replied. Consider an auto-chase."
                        ),
                    })

    # 3. QC error / drift FAIL
    if "qc_error" in enabled:
        qc_path = REPORTS / "qc-result.json"
        if qc_path.exists():
            try:
                qc = json.loads(qc_path.read_text(encoding="utf-8"))
                if qc.get("status") not in ("CLEAN", None) and qc.get("errors", 0) > 0:
                    key = f"qc_error:{today_iso}:{qc.get('errors')}"
                    if not _was_alerted(key):
                        events.append({
                            "type": "qc_error",
                            "key": key,
                            "color": "dc2626",
                            "title": f"❌ QC errors today — {qc.get('errors')} error(s)",
                            "text": "; ".join(qc.get("error_details", [])[:3]),
                        })
            except Exception:
                pass

    # 4. QC-049 unconfirmed WINs — auto-notify booking team for review.
    # Added 2026-05-28 per Michael's "do all 7-9" direction. The audit
    # surfaces these as a red flag in QC-049, but until now there was no
    # mechanism to push them to whoever owns the booking-team handoff.
    # Now: each unconfirmed WIN that's >=7d old generates ONE alert per
    # week (de-duped via _was_alerted using a week-keyed event-key). Alert
    # is informational/amber, not critical — the work is human review of
    # whether it's a real win with an unlinked booking or a false win.
    if "qc049_unconfirmed_win" in enabled:
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
        wk = datetime.now(core.ET).strftime("%Y-W%V")  # iso year+week for de-dup window
        for r in rows:
            if r.get("status") != "WIN":
                continue
            if r.get("mdolx_ref") or r.get("mdolx_refs_all"):
                continue
            req_date = r.get("request_date") or ""
            if req_date >= cutoff_iso:
                continue  # too recent — normal booking-confirmation lag
            key = f"qc049_unconfirmed_win:{r.get('request_id')}:{wk}"
            if _was_alerted(key):
                continue
            age_days = (datetime.now(timezone.utc).date()
                        - datetime.strptime(req_date, "%Y-%m-%d").date()).days \
                       if req_date else "?"
            events.append({
                "type": "qc049_unconfirmed_win",
                "key": key,
                "color": "f59e0b",  # amber — informational, not critical
                "title": f"📋 Unconfirmed WIN — {r.get('lane', '?')} ({age_days}d old)",
                "text": (
                    f"**Request**: {r.get('request_id', '?')} | "
                    f"**Lane**: {r.get('lane', '?')} | "
                    f"**Date**: {req_date}\n\n"
                    f"Flipped to WIN on a Lonny send-signal but no MDOLX booking "
                    f"confirmation has linked back after {age_days} days. Per the "
                    f"Linda Echevarria 2026-05-19 audit, some of these are real "
                    f"with an unlinked confirmation; some are false wins. "
                    f"**Booking team: please review and either link the MDOLX "
                    f"booking or demote to Q&L via the operator-corrections layer.**"
                ),
            })

    # 5. Big-day WIN (total TEU won today ≥ threshold)
    if "big_day" in enabled:
        teu_today = sum(int(r.get("teu_won") or r.get("teu_requested") or 0)
                        for r in rows
                        if r.get("status") == "WIN"
                        and (r.get("status_history") or [{}])[-1].get("at", "")[:10] == today_iso)
        threshold = cfg.get("min_teu_for_big_day", 30)
        if teu_today >= threshold:
            key = f"big_day:{today_iso}:{teu_today}"
            if not _was_alerted(key):
                events.append({
                    "type": "big_day",
                    "key": key,
                    "color": "16a34a",
                    "title": f"🔥 Big day — {teu_today} TEU won",
                    "text": f"Total TEU on confirmed bookings today exceeds {threshold} — strong volume.",
                })

    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "test"])
    args = ap.parse_args()

    cfg = _config()
    webhook = cfg.get("teams_webhook_url") or ""

    if args.cmd == "test":
        title = "✅ Hilmar Tracker alerts — test"
        text = f"This is a test alert from teams_alert.py at {datetime.now(core.ET).isoformat()}"
        if webhook:
            ok = _send_teams(webhook, title, text)
            print(f"  test send: {'OK' if ok else 'FAILED'}")
        else:
            _queue_alert({"type": "test", "title": title, "text": text})
            print("  no webhook configured — alert queued in reports/alerts-queue.json")
        return 0

    if args.cmd == "scan":
        if not DATA.exists():
            print("tracking-data-v2.json missing — skipping")
            return 0
        data = json.loads(DATA.read_text(encoding="utf-8"))
        events = detect_events(data, cfg)
        print(f"Detected {len(events)} event(s)")
        sent = 0
        queued = 0
        for ev in events:
            if webhook:
                if _send_teams(webhook, ev["title"], ev["text"], ev.get("color", "0078D4")):
                    _record_alert(ev["key"])
                    sent += 1
                    print(f"  ✅ {ev['type']}: {ev['title']}")
                else:
                    _queue_alert(ev)
                    queued += 1
            else:
                _queue_alert(ev)
                _record_alert(ev["key"])  # so we don't re-queue
                queued += 1
                print(f"  📋 queued {ev['type']}: {ev['title']}")
        if not webhook:
            print(f"\n  No webhook configured. Add config.json:")
            print(f'    "alerts": {{')
            print(f'      "teams_webhook_url": "https://outlook.office.com/webhook/...",')
            print(f'      "events": ["win", "pending_overdue", "qc_error", "big_day"]')
            print(f'    }}')
        print(f"\nteams_alert: {sent} sent live, {queued} queued")
        return 0


if __name__ == "__main__":
    sys.exit(main())
