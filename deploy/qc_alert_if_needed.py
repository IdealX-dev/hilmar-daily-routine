"""qc_alert_if_needed.py — Email Michael only if QC self-heal status != CLEAN.

Reads reports/qc-result.json (written by the most recent run_pipeline.py).
Sends a single Outlook email to Michael with the failure summary if status is
WARN/FAIL/anything-other-than-CLEAN.

Designed to be quiet on the happy path (CLEAN → exit 0, no email).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import outlook_send as O  # noqa: E402

QC_RESULT_PATH = ROOT / "reports" / "qc-result.json"
ALERT_RECIPIENT = "michael.deitchman@ol-usa.com"


def _summarize(qc: dict) -> str:
    counts = qc.get("counts") or {}
    lines = [
        f"<b>Status:</b> {qc.get('status')}",
        f"<b>Fixes:</b> {qc.get('fixes', 0)}",
        f"<b>Warnings:</b> {qc.get('warnings', 0)}",
        f"<b>Errors:</b> {qc.get('errors', 0)}",
        f"<b>Counts:</b> {counts.get('total', '?')} entries — "
        f"{counts.get('wins', '?')} W / {counts.get('ql', '?')} Q&L / "
        f"{counts.get('nq', '?')} NQ",
    ]
    if qc.get("error_details"):
        lines.append("<br><b>Errors:</b><ul>")
        for d in qc["error_details"]:
            lines.append(f"  <li>{d}</li>")
        lines.append("</ul>")
    if qc.get("warning_details"):
        lines.append("<br><b>Warnings:</b><ul>")
        for d in qc["warning_details"]:
            lines.append(f"  <li>{d}</li>")
        lines.append("</ul>")
    return "<br>".join(lines)


def main() -> int:
    if not QC_RESULT_PATH.exists():
        print(f"qc_alert: {QC_RESULT_PATH} missing — pipeline may not have run")
        return 1
    qc = json.loads(QC_RESULT_PATH.read_text(encoding="utf-8"))
    status = (qc.get("status") or "").upper()
    if status == "CLEAN":
        print("qc_alert: status=CLEAN, no email")
        return 0
    body = (
        f"<p>Laptop daily run produced QC status <b>{status}</b> "
        f"at {datetime.now(timezone.utc).isoformat(timespec='seconds')}.</p>"
        + _summarize(qc)
        + "<p>Full result: reports/qc-result.json on the laptop.</p>"
    )
    subject = f"[Hilmar laptop] QC {status} — {datetime.now().strftime('%Y-%m-%d %H:%M ET')}"
    req_id = O.send_mail(
        to=[ALERT_RECIPIENT],
        subject=subject,
        html_body=body,
    )
    print(f"qc_alert: sent. request-id={req_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
