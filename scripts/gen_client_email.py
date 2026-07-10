"""
gen_client_email.py — CLIENT-facing daily service update email.

A SEPARATE artifact from gen_email.py (the staff email). This one goes to
THE CLIENT — Lonny Upfold at Hilmar Ingredients — so it is a service update
from OL-USA to its customer and carries ZERO internal analytics: no win/loss
rates, no "Quoted & Lost"/NQ framing, no carrier scoreboards, no rate-
negotiation intel, no QC/parser/system language. QC-065 enforces both the
recipient invariants and this content rule on the rendered body.

SHIPS GATED OFF: config.json `client_report.enabled = false`. While disabled
the pipeline still builds this artifact every fire, and the send step mails a
SAMPLE to Michael only (`--force --no-flag`, never touching idempotency
state). Lonny receives nothing until the operator flips enabled=true.

Produces: reports/client-email-body.html + reports/client-email-subject.txt
Usage:
  python3 scripts/gen_client_email.py
  python3 scripts/gen_client_email.py --data tests/fixtures/golden_day.json --out-dir /tmp/out
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import branding as B  # noqa: E402
import core  # noqa: E402

# Shared helpers from the staff renderer — same escaping (raw strings in,
# single-escape out; QC-044), same Windows-portable strftime mapping (no
# bare %-d/%-I; QC-046), same report-day math, same today-bucketing.
from gen_email import (  # noqa: E402
    EMAIL_FONT_STACK,
    EMAIL_TNUM,
    _esc,
    _fmt_date,
    _report_date,
    _today_events,
)

# Navy palette used repo-wide. Table headers are a SOLID color only (QC-045:
# Outlook strips linear-gradient; the banner pairs the gradient with a solid
# background-color fallback, listed first so Outlook keeps the solid).
TH_BG = "#1e3a5f"
HEADER_BG_SOLID = B.HILMAR_NAVY
HEADER_GRADIENT = f"linear-gradient(135deg,{B.HILMAR_NAVY} 0%,{B.HILMAR_BLUE} 100%)"

_TH_STYLE = (
    f'style="padding:6px 8px;background-color:{TH_BG};background:{TH_BG};'
    'color:#ffffff;font-size:11px;font-weight:600;text-align:left;'
    f'border-bottom:1px solid {TH_BG}"'
)
_TD_STYLE = (
    'style="padding:6px 8px;font-size:12px;color:#1f2937;'
    'border-bottom:1px solid #e5e7eb"'
)
_TD_FIRST_STYLE = _TD_STYLE.replace('color:#1f2937', 'color:#1f2937;font-weight:600')

# Mobile overrides — same .hx-* conventions as gen_email._header_html so the
# client email renders on phones: .hx-wrap full-bleed, .hx-pad one horizontal
# scroll surface, .hx-data tables keep readable column geometry and scroll
# sideways instead of squishing. Desktop Outlook ignores <style> entirely.
MOBILE_STYLE = """
<style>
@media only screen and (max-width:640px) {
  .hx-wrap { width:100% !important; max-width:100% !important; border-radius:0 !important; }
  .hx-pad { padding:14px 8px !important; overflow-x:auto !important; -webkit-overflow-scrolling:touch; }
  td.hx-kpi { display:block !important; width:100% !important; box-sizing:border-box !important; }
  .hx-kpi-card { height:auto !important; min-height:0 !important; }
  table.hx-data { min-width:640px !important; }
}
</style>
"""


# ─────────────────────────────────────────────────────────────────────
# Small formatters (client-safe, Windows-portable)
# ─────────────────────────────────────────────────────────────────────

def _short_tz(iso, tz, label):
    """'Jul 9 2:05 PM PT' — portable strftime (no %-d/%-I; strip the leading
    zeros with .replace, per CLAUDE.md rule #8)."""
    dt = core.parse_iso(iso)
    if not dt:
        return "—"
    s = dt.astimezone(tz).strftime("%b %d %I:%M %p")
    s = s.replace(" 0", " ", 1).replace(" 0", " ", 1)
    return f"{s} {label}"


def _short_pt(iso):
    return _short_tz(iso, core.PT, "PT")


def _short_et(iso):
    return _short_tz(iso, core.ET, "ET")


def _rate(r):
    v = r.get("ol_rate")
    return f"${v:,.0f}" if isinstance(v, (int, float)) else (str(v) if v else "—")


def _lane(r):
    return r.get("lane") or f"{r.get('origin', '?')} → {r.get('destination', '?')}"


def _teu(r):
    return str(r.get("teu_requested") or 0)


# ─────────────────────────────────────────────────────────────────────
# Day bucketing — reuses gen_email._today_events (stand_* rows are already
# excluded from the requests/quotes buckets there; they surface only through
# a →WIN status_history entry, i.e. the bookings section — the honest event).
# ─────────────────────────────────────────────────────────────────────

def _client_sections(data, report_date):
    """Return the five client-facing row buckets for the report day."""
    new_req, ol_resp, status_ch, pending = _today_events(data, report_date)
    quotes = [r for r in ol_resp if r.get("ol_rate")]
    bookings, seen = [], set()
    for r, h in status_ch:
        if h.get("to") == "WIN" and id(r) not in seen:
            seen.add(id(r))
            bookings.append(r)
    awaiting = [r for r in pending if core.pending_substate(r) == "PENDING_HILMAR"]
    in_progress = [r for r in pending if core.pending_substate(r) == "PENDING_OL"]
    return {
        "requests": new_req,
        "quotes": quotes,
        "bookings": bookings,
        "awaiting": awaiting,
        "in_progress": in_progress,
    }


# ─────────────────────────────────────────────────────────────────────
# HTML builders
# ─────────────────────────────────────────────────────────────────────

def _table(headers, rows):
    """Outlook-safe table. Every cell value is escaped HERE (pass RAW strings —
    QC-044). Empty → a stable friendly one-liner so the email keeps its shape."""
    head = "".join(f"<th {_TH_STYLE}>{_esc(h)}</th>" for h in headers)
    if rows:
        body = ""
        for row in rows:
            cells = "".join(
                f"<td {_TD_FIRST_STYLE if i == 0 else _TD_STYLE}>{_esc(v)}</td>"
                for i, v in enumerate(row)
            )
            body += f"<tr>{cells}</tr>"
    else:
        body = (
            f'<tr><td colspan="{len(headers)}" '
            f'{_TD_STYLE.replace("text-align:left", "text-align:center")}>'
            f'<em style="color:#64748b">None today.</em></td></tr>'
        )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" class="hx-data" '
        'style="width:100%;border-collapse:collapse;font-size:12px;'
        'margin:4px 0 18px 0;border:1px solid #d1d5db">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def _section(title, count, note, table_html):
    return (
        f'<h2 style="margin:18px 0 2px;color:{TH_BG};font-size:15px;'
        f'font-weight:700;letter-spacing:-0.01em">{_esc(title)} ({count})</h2>'
        f'<p style="margin:0 0 6px;font-size:11px;color:#64748b">{_esc(note)}</p>'
        f"{table_html}"
    )


def _header_html(report_label, updated_label):
    # CID logo — Outlook blocks data: URIs in email bodies (QC-042);
    # outlook_send attaches the PNG with contentId=hilmar-logo + isInline.
    logo_html = B.logo_html_cid(height=56, alt="Hilmar Ingredients")
    logo_block = (
        f'<div style="background:white;padding:2px 6px;border-radius:4px;'
        f'display:inline-block;margin-bottom:6px">{logo_html}</div>'
        if logo_html else ""
    )
    return f"""
{MOBILE_STYLE}
<div class="hx-wrap" style="max-width:900px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;font-family:{EMAIL_FONT_STACK};{EMAIL_TNUM}">
  <div style="padding:16px 28px;background-color:{HEADER_BG_SOLID};background:{HEADER_GRADIENT};color:white;font-family:{EMAIL_FONT_STACK}">
    {logo_block}
    <h1 style="margin:0;font-size:21px;font-weight:700;letter-spacing:-0.3px;font-family:{EMAIL_FONT_STACK}">Daily Shipment Update — Hilmar Ingredients</h1>
    <p style="margin:4px 0 0;font-size:13px;opacity:0.9;font-family:{EMAIL_FONT_STACK}">Prepared by OL-USA · {_esc(report_label)} · Updated {_esc(updated_label)}</p>
  </div>
  <div class="hx-pad" style="padding:20px 28px;font-family:{EMAIL_FONT_STACK};{EMAIL_TNUM}">
"""


FOOTER_HTML = """
<div style="border-top:2px solid #e5e7eb;padding-top:14px;margin-top:24px">
  <p style="margin:0 0 4px;font-size:12px;color:#374151">Questions? Reply to this email or contact <a href="mailto:MBD_OceanExportBookingShared@ol-usa.com" style="color:#1e3a5f">MBD_OceanExportBookingShared@ol-usa.com</a>.</p>
  <p style="margin:0;font-size:11px;color:#6b7280">This daily update is generated automatically by OL-USA for Hilmar Ingredients.</p>
</div>
"""


def build_subject(data, cfg):
    report = _report_date()
    label = _fmt_date(datetime.combine(report, datetime.min.time()), "%b %-d, %Y")
    return f"OL-USA — Daily Shipment Update for Hilmar Ingredients ({label})"


def build_body(data, cfg):
    now_et = datetime.now(timezone.utc).astimezone(core.ET)
    report_date = _report_date(now_et)
    report_label = _fmt_date(
        datetime.combine(report_date, datetime.min.time()), "%A, %B %-d, %Y")
    updated_label = _fmt_date(now_et, "%B %-d, %Y at %-I:%M %p ET")
    s = _client_sections(data, report_date)

    html = _header_html(report_label, updated_label)
    html += (
        f'<p style="margin:0 0 10px;font-size:13px;color:#374151">'
        f'Here is today\'s summary of your ocean shipment activity with OL-USA '
        f'— requests received, rates provided, and bookings confirmed for '
        f'{_esc(report_label)}.</p>'
    )

    # b. Requests received today — acknowledgment of TODAY's RFQs.
    html += _section(
        "Requests received today", len(s["requests"]),
        "Rate requests we received from your team today — all acknowledged and being worked.",
        _table(
            ["Lane", "Equipment", "TEU", "Product", "Temp", "Requested ETA", "Received (PT)"],
            [[
                _lane(r),
                r.get("containers") or "—",
                _teu(r),
                r.get("product") or "—",
                r.get("temperature") or "—",
                r.get("eta_requested") or r.get("etd_requested") or "—",
                r.get("lonny_time_pt") or _short_pt(r.get("request_timestamp")),
            ] for r in s["requests"]],
        ),
    )

    # c. Quotes provided today — TODAY's rate responses.
    html += _section(
        "Quotes provided today", len(s["quotes"]),
        "Rates our booking team sent you today.",
        _table(
            ["Lane", "Equipment", "TEU", "Carrier", "Rate ($/container)",
             "Quoted at (ET)", "ETD offered", "ETA offered"],
            [[
                _lane(r),
                r.get("containers") or "—",
                _teu(r),
                r.get("carrier_quoted") or "—",
                _rate(r),
                _short_et(r.get("response_timestamp")),
                r.get("etd_offered") or "—",
                r.get("eta_offered") or "—",
            ] for r in s["quotes"]],
        ),
    )

    # d. Bookings confirmed today — →WIN transitions on the report day.
    html += _section(
        "Bookings confirmed today", len(s["bookings"]),
        "Shipments confirmed today. Booking references are listed once carrier confirmation arrives.",
        _table(
            ["Lane", "Equipment / TEU", "Carrier", "Booking ref", "ETD", "ETA"],
            [[
                _lane(r),
                f"{r.get('containers') or '—'} / {_teu(r)} TEU",
                r.get("carrier_won") or r.get("carrier_quoted") or "—",
                r.get("mdolx_ref") or "Confirmation to follow",
                r.get("etd_offered") or "—",
                r.get("eta_offered") or "—",
            ] for r in s["bookings"]],
        ),
    )

    # e. Awaiting your decision — quotes on the table (OL quoted, no booking yet).
    html += _section(
        "Awaiting your decision", len(s["awaiting"]),
        "Quotes on the table — reply to book, or let us know if the dates or rates need adjusting.",
        _table(
            ["Lane", "Carrier", "Rate ($/container)", "Quoted at (ET)", "ETD offered"],
            [[
                _lane(r),
                r.get("carrier_quoted") or "—",
                _rate(r),
                _short_et(r.get("response_timestamp")),
                r.get("etd_offered") or "—",
            ] for r in s["awaiting"]],
        ),
    )

    # f. In progress — requests we're still pricing.
    html += _section(
        "In progress — quote coming", len(s["in_progress"]),
        "We're working on these and will have rates to you shortly.",
        _table(
            ["Lane", "Equipment", "TEU", "Received (PT)"],
            [[
                _lane(r),
                r.get("containers") or "—",
                _teu(r),
                r.get("lonny_time_pt") or _short_pt(r.get("request_timestamp")),
            ] for r in s["in_progress"]],
        ),
    )

    html += FOOTER_HTML
    html += "</div></div>"
    return html


# ─────────────────────────────────────────────────────────────────────
# Entry point — mirrors gen_email.main()'s config/path resolution, with
# --data/--out-dir overrides so tests + smoke runs work without production
# data (e.g. against tests/fixtures/golden_day.json).
# ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--data", default=None,
                    help="Override the tracking-data path (default: config paths.data)")
    ap.add_argument("--out-dir", default=None,
                    help="Override the output directory (default: the reports/ dir "
                         "holding config paths.email_body)")
    args = ap.parse_args(argv)
    cfg = core.load_config(args.config)
    data_path = Path(args.data) if args.data else Path(cfg["paths"]["data"])
    data = json.loads(data_path.read_text(encoding="utf-8"))
    body = build_body(data, cfg)
    subject = build_subject(data, cfg)
    out_dir = Path(args.out_dir) if args.out_dir else Path(cfg["paths"]["email_body"]).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    body_path = out_dir / "client-email-body.html"
    subject_path = out_dir / "client-email-subject.txt"
    body_path.write_text(body, encoding="utf-8")
    subject_path.write_text(subject, encoding="utf-8")
    print(f"✅ Client email body: {len(body):,} bytes -> {body_path}")
    print(f"✅ Client email subject: {subject!r} -> {subject_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
