#!/usr/bin/env python3
"""
gen_client_weekly.py — CLIENT-facing WEEKLY rollup for Hilmar.

Michael 2026-08-05, asked whether Lonny gets tallied reports with all the
data: he does not. Lonny receives a body-only daily and nothing weekly. This
is the weekly, and Michael chose its shape — "Client-safe weekly rollup":

    week at a glance · your bookings · quotes still open ·
    upcoming cutoffs · 4-week volume trend

WHAT IS DELIBERATELY ABSENT, AND WHY. This is a service summary from OL-USA
to its customer, not the staff exec summary with the client's name on it. So
it carries no success rate, no lost-quote framing, no unanswered-request
framing, and no carrier league table. Those are OL-internal performance
measures; showing a customer how often we fail to win their business is a
negotiating position handed over for free, and showing them a carrier ranking
is commercially ours. gen_weekly_summary.py remains the staff artifact and is
untouched.

The rule is ENFORCED, not just intended: qc_selfheal.qc065_internal_leaks —
the same scanner that guards the client daily — runs over this body, and the
tests assert it returns empty. That scanner reads the RENDERED HTML, so it
catches a leak arriving through data (a lane name, a carrier note) as
readily as one written into a template.

VOLUME, NOT PERFORMANCE. The 4-week trend is requests and TEU only. Volume is
the client's own number — it is what Hilmar shipped, and it is the number
Lonny needs when his side asks what moved. A win rate over the same four
weeks would be OL's report card, which is exactly the line this file exists
to hold.

SHIPS GATED OFF: config.json `client_weekly.enabled` defaults false, matching
how the client daily shipped. While disabled nothing reaches Lonny; the
artifact still builds every run so it can be reviewed, and the operator sends
a sample to Michael with --verification. Flipping enabled=true is a human
decision, made once, by a person who has read a rendered week.

Produces: reports/client-weekly.html + reports/client-weekly-subject.txt
Usage:
  python3 scripts/gen_client_weekly.py
  python3 scripts/gen_client_weekly.py --start 2026-07-27 --end 2026-08-04
  python3 scripts/gen_client_weekly.py --data tests/fixtures/golden_day.json \
      --out-dir /tmp/out
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import branding as B  # noqa: E402
import core  # noqa: E402

# The client daily owns the house style — the Outlook-safe table, the section
# wrappers, the escaping convention, the mobile <style> block. Importing them
# is the point: a second copy would drift, and the two artifacts land in the
# same inbox a day apart.
from gen_client_email import (  # noqa: E402
    MOBILE_STYLE,
    _cutoff_callout,
    _esc,
    _etd_with_staleness,
    _lane,
    _lane_resolved,
    _plural,
    _rate,
    _section_or_line,
    _table,
    _teu,
    _teu_sum,
    _upcoming_cutoffs,
)
from gen_email import _fmt_date, _iso_date, _kpi_card  # noqa: E402

REPORTS = ROOT / "reports"

#: How many weeks the volume trend covers, including the reported week.
TREND_WEEKS = 4


# ─────────────────────────────────────────────────────────────────────
# Period
# ─────────────────────────────────────────────────────────────────────

def week_bounds(today: date | None = None) -> tuple[date, date]:
    """Monday–Friday of the PREVIOUS (just-completed) week.

    Same anchor as the staff weekly: this fires Monday morning about the week
    that just ended, so a client reading it on Monday is reading a closed
    week rather than a partial one.
    """
    today = today or datetime.now(core.ET).date()
    this_mon = today - timedelta(days=today.weekday())
    prev_mon = this_mon - timedelta(days=7)
    return prev_mon, prev_mon + timedelta(days=4)


def range_label(start: date, end: date) -> str:
    """'Jul 27–31, 2026' / 'Jul 27–Aug 4, 2026' / across a year boundary.

    Shared shape with gen_weekly_summary._range_label, and the cross-month
    case is here for the same reason it is there: the first draft rendered
    "Jul 27–4, 2026", which reads as a typo rather than a date.
    """
    d = "%#d" if sys.platform == "win32" else "%-d"
    if start.year != end.year:
        return f"{start.strftime(f'%b {d}, %Y')}–{end.strftime(f'%b {d}, %Y')}"
    if start.month != end.month:
        return f"{start.strftime(f'%b {d}')}–{end.strftime(f'%b {d}, %Y')}"
    return f"{start.strftime(f'%b {d}')}–{end.strftime(f'{d}, %Y')}"


def _in_period(r, start: date, end: date) -> bool:
    d = _iso_date(r.get("request_date") or r.get("date"))
    return bool(d and start <= d <= end)


# ─────────────────────────────────────────────────────────────────────
# Buckets — client vocabulary only
# ─────────────────────────────────────────────────────────────────────

def client_sections(data, start: date, end: date) -> dict:
    """The week's rows, in the four groupings a customer thinks in.

    Note what each key is windowed BY, because the daily's equivalent got this
    wrong in a way that took a screenshot to catch:
      requests / bookings — the reported WEEK
      open_quotes         — CURRENT STATE, deliberately not windowed. A quote
                            delivered three weeks ago that Lonny has not
                            answered is still open, and a weekly that hid it
                            because it is old would be hiding the only rows
                            that need him to do something.
    """
    rows = data.get("requests") or []
    week = [r for r in rows if _in_period(r, start, end)]

    requests = [r for r in week if _lane_resolved(r)]
    bookings = [r for r in week if core.is_win(r) and _lane_resolved(r)]
    # "Awaiting your decision" — we quoted, the customer has not booked.
    open_quotes = [
        r for r in rows
        if core.pending_substate(r) == "PENDING_HILMAR" and _lane_resolved(r)
    ]
    return {"requests": requests, "bookings": bookings, "open_quotes": open_quotes}


def volume_trend(data, end: date, weeks: int = TREND_WEEKS) -> list[dict]:
    """Requests and TEU for each of the last `weeks` Mon–Fri weeks, oldest
    first. Volume only — see the module docstring on why there is no rate
    here."""
    rows = data.get("requests") or []
    out = []
    last_mon = end - timedelta(days=end.weekday())
    for i in range(weeks - 1, -1, -1):
        mon = last_mon - timedelta(days=7 * i)
        fri = mon + timedelta(days=4)
        wk = [r for r in rows if _in_period(r, mon, fri)]
        out.append({
            "label": range_label(mon, fri),
            "requests": len(wk),
            "teu": _teu_sum(wk),
            "bookings": sum(1 for r in wk if core.is_win(r)),
            "teu_booked": _teu_sum([r for r in wk if core.is_win(r)], won=True),
        })
    return out


def active_shipments(data, end: date):
    """Confirmed bookings whose quoted arrival has not yet passed — the rows a
    cutoff can still be upcoming for."""
    out = []
    for r in data.get("requests") or []:
        if not (core.is_win(r) and _lane_resolved(r)):
            continue
        eta = _iso_date(r.get("eta_offered"))
        if eta and eta < end:
            continue
        out.append(r)
    return sorted(out, key=lambda r: str(r.get("etd_offered") or "9999"))


# ─────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────

def _header(period_label: str, prepared_label: str) -> str:
    return (
        f"{MOBILE_STYLE}"
        f'<div class="hx-wrap" style="max-width:860px;margin:0 auto;'
        f'background:{B.DOC_CARD};border:1px solid {B.DOC_LINE};border-radius:10px;'
        f'overflow:hidden;font-family:Segoe UI,Helvetica,Arial,sans-serif">'
        f'<div style="padding:16px 28px;background-color:{B.HILMAR_NAVY};'
        f'color:{B.DOC_CARD}">'
        f'<div style="font-size:18px;font-weight:700;letter-spacing:-0.01em">'
        f"Hilmar Ingredients — Weekly Shipping Summary</div>"
        f'<div style="font-size:12px;opacity:0.85;margin-top:3px">'
        f"Prepared by OL-USA · Covers {_esc(period_label)} · "
        f"Sent {_esc(prepared_label)}</div></div>"
        f'<div class="hx-pad" style="padding:18px 28px 24px">'
    )


def _glance(s, active, period_label: str) -> str:
    """Week at a glance. Four counts, all of them the customer's own numbers."""
    return f"""
<h2 style="margin:4px 0 8px;color:{B.DOC_INK};font-size:15px;font-weight:700">Week at a glance</h2>
<p style="margin:0 0 8px;font-size:11px;color:{B.DOC_MUTED}">{_esc(period_label)}</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;margin:2px 0 8px">
  <tr>
    {_kpi_card(len(s["requests"]), "Rate requests", B.DOC_INFO, "25%", sublabel=f"{_teu_sum(s['requests'])} TEU")}
    {_kpi_card(len(s["bookings"]), "Bookings confirmed", B.DOC_GOOD, "25%", sublabel=f"{_teu_sum(s['bookings'], won=True)} TEU")}
    {_kpi_card(len(s["open_quotes"]), "Quotes open", B.DOC_WARN, "25%", sublabel="awaiting your decision")}
    {_kpi_card(len(active), "In transit or booked", B.DOC_PENDING, "25%", sublabel="not yet arrived")}
  </tr>
</table>
"""


def _trend_table(trend) -> str:
    """Four weeks of volume, most recent LAST so the row order reads as time.

    A bar would need an image or a table hack that Outlook's Word engine
    renders differently on every desk; the numbers are four rows and read
    fine as numbers.
    """
    rows = [[t["label"], str(t["requests"]), str(t["teu"]),
             str(t["bookings"]), str(t["teu_booked"])] for t in trend]
    return _table(
        ["Week", "Rate requests", "TEU requested", "Bookings", "TEU booked"],
        rows,
    )


def _narrative(s, period_label: str) -> str:
    n_req, n_book = len(s["requests"]), len(s["bookings"])
    n_open = len(s["open_quotes"])
    if not (n_req or n_book):
        line = f"A quiet week on new activity — no new rate requests or bookings in {period_label}."
    else:
        line = (f"In {period_label} we received {_plural(n_req, 'rate request')} "
                f"and confirmed {_plural(n_book, 'booking')} "
                f"({_teu_sum(s['bookings'], won=True)} TEU).")
    if n_open:
        verb = "is" if n_open == 1 else "are"
        line += f" {_plural(n_open, 'quote')} {verb} still open for your decision."
    return line


def build_subject(start: date, end: date, cfg=None) -> str:
    return f"Hilmar Ingredients — Weekly Shipping Summary ({range_label(start, end)})"


def build_body(data, cfg, start: date, end: date, now=None) -> str:
    now_et = (now or datetime.now(timezone.utc)).astimezone(core.ET)
    period_label = range_label(start, end)
    prepared_label = (_fmt_date(now_et, "%A, %B %-d, %Y") + " at "
                      + _fmt_date(now_et, "%-I:%M %p") + " ET")

    s = client_sections(data, start, end)
    active = active_shipments(data, end)
    cutoffs = _upcoming_cutoffs(active, end)

    html = _header(period_label, prepared_label)
    html += _glance(s, active, period_label)
    html += (f'<p style="margin:6px 2px 14px;font-size:13px;color:{B.DOC_INK};'
             f'line-height:1.5">{_esc(_narrative(s, period_label))}</p>')

    if cutoffs:
        html += _cutoff_callout(cutoffs)

    html += _section_or_line(
        "Your bookings this week",
        "Shipments confirmed during the week, with booking reference and the "
        "schedule as quoted at booking.",
        f"No bookings confirmed in {period_label}.",
        ["Lane", "Booking ref", "Equipment", "TEU", "Vessel", "ETD", "ETA"],
        [[
            _lane(r),
            r.get("mdolx_ref") or "Confirmation to follow",
            r.get("containers") or "—",
            str(r.get("teu_won") or r.get("teu_requested") or 0),
            r.get("vessel_voyage") or "—",
            r.get("etd_offered") or "—",
            r.get("eta_offered") or "—",
        ] for r in s["bookings"]],
    )

    html += _section_or_line(
        "Quotes still open",
        "Rates we have delivered that are waiting on your decision — including "
        "any from previous weeks, so nothing open drops off this list.",
        "Nothing is waiting on your decision.",
        ["Lane", "Equipment", "TEU", "Rate", "ETD offered", "Quoted"],
        [[
            _lane(r),
            r.get("containers") or "—",
            str(_teu(r)),
            _rate(r),
            _etd_with_staleness(r.get("etd_offered"), end),
            str(_iso_date(r.get("response_timestamp")) or "—"),
        ] for r in s["open_quotes"]],
    )

    html += (
        f'<h2 style="margin:18px 0 2px;color:{B.DOC_INK};font-size:15px;'
        f'font-weight:700">Volume — last {TREND_WEEKS} weeks</h2>'
        f'<p style="margin:0 0 6px;font-size:11px;color:{B.DOC_MUTED}">'
        f"Your shipping volume by week, oldest first.</p>"
        + _trend_table(volume_trend(data, end))
    )

    html += (
        f'<p style="margin:18px 0 0;padding-top:12px;border-top:1px solid '
        f'{B.DOC_LINE};font-size:11px;color:{B.DOC_MUTED}">'
        f"Questions on any line above — reply to this email and the OL-USA "
        f"export desk will pick it up.</p>"
    )
    return html + "</div></div>"


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--data", default=None)
    ap.add_argument("--out-dir", default=str(REPORTS))
    ap.add_argument("--start", default=None, help="Period start YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="Period end YYYY-MM-DD, inclusive")
    args = ap.parse_args(argv)

    cfg = core.load_config(args.config)
    data_path = Path(args.data) if args.data else Path(cfg["paths"]["data"])
    data = core.load_data(data_path)

    if bool(args.start) != bool(args.end):
        print("--start and --end must be given together", file=sys.stderr)
        return 2
    if args.start:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
        if end < start:
            print("--end precedes --start", file=sys.stderr)
            return 2
    else:
        start, end = week_bounds()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    body = build_body(data, cfg, start, end)
    (out / "client-weekly.html").write_text(body, encoding="utf-8")
    (out / "client-weekly-subject.txt").write_text(
        build_subject(start, end, cfg), encoding="utf-8")

    enabled = bool((cfg.get("client_weekly") or {}).get("enabled", False))
    print(f"client weekly: {range_label(start, end)} → {out / 'client-weekly.html'}")
    print(f"  requests={len(client_sections(data, start, end)['requests'])} "
          f"bookings={len(client_sections(data, start, end)['bookings'])}")
    print(f"  send enabled: {enabled} "
          f"({'Lonny receives this' if enabled else 'GATED OFF — sample to Michael only'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
