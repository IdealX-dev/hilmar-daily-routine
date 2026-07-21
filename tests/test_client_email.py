"""Client-facing daily email (scripts/gen_client_email.py) + its guardrails.

This artifact goes to THE CLIENT (Lonny Upfold, Hilmar Ingredients), so the
tests pin the two things that must never drift:
  1. CONTENT — a service update only: rates, bookings, open requests. ZERO
     internal analytics (win rate, Q&L/NQ framing, scoreboard/negotiation
     intel), raw or &amp;-escaped.
  2. GATING — ships disabled (config client_report.enabled=false, sample to
     Michael only); QC-065 pins the only approved client recipients; the
     send gets its own idempotency namespace (client-sent flags, synced).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import gen_client_email as gce  # noqa: E402
import outlook_send as os_send  # noqa: E402
import qc_selfheal as q  # noqa: E402
import run_pipeline as rp  # noqa: E402
import state_store  # noqa: E402

#: The report business day the renderer buckets "today" on.
RD = core.report_business_day(datetime.now(core.ET)).isoformat()

#: Internal-analytics strings that must NEVER reach the client (raw AND
#: &amp;-escaped forms; matched case-insensitively).
INTERNAL_MARKERS = (
    "win rate", "quoted & lost", "quoted &amp; lost",
    "q&l", "q&amp;l", "not quoted", "carrier scoreboard", "negotiation",
)

APPROVED_TO = ["lupfold@hilmaringredients.com"]
APPROVED_CC = ["michael.deitchman@ol-usa.com"]


def _row(rid, **kw):
    base = {
        "request_id": rid, "status": "PENDING", "quoted": False,
        "request_date": RD, "request_timestamp": f"{RD}T15:00:00Z",
        "lane": "Oakland → Tokyo", "containers": "2×40'RF",
        "container_count": 2, "teu_requested": 4, "status_history": [],
    }
    base.update(kw)
    return base


def _mixed_data():
    """WIN + Q&L-style LOSS + NQ-style LOSS + both PENDING substates, all on
    the report day — the statuses whose framing must never leak."""
    return {"requests": [
        _row("r-win", status="WIN", quoted=True, ol_rate=4250.0,
             carrier_quoted="MSC", carrier_won="MSC", mdolx_ref="MDOLX-T1",
             response_timestamp=f"{RD}T18:00:00Z",
             etd_offered="2026-07-20", eta_offered="2026-08-05",
             status_history=[{"from": "PENDING", "to": "WIN",
                              "at": f"{RD}T20:00:00Z", "reason": "booked"}]),
        _row("r-ql", status="LOSS", quoted=True, loss_reason="RATE",
             ol_rate=3900.0, carrier_quoted="CMA CGM",
             response_timestamp=f"{RD}T17:00:00Z", lane="Oakland → Shanghai"),
        _row("r-nq", status="LOSS", quoted=False, loss_reason="NO_RESPONSE",
             lane="Oakland → Jakarta"),
        _row("r-pend-hil", status="PENDING", quoted=True, ol_rate=3100.0,
             carrier_quoted="ONE", response_timestamp=f"{RD}T16:00:00Z",
             lane="Oakland → Busan"),
        _row("r-pend-ol", status="PENDING", quoted=False,
             lane="Oakland → Manila"),
    ]}


def _rd():
    return core.report_business_day(datetime.now(core.ET))


def _win(rid, days_ago=0, **kw):
    """A WIN row `days_ago` days before the report day — the active-shipments
    window (14 days) and cutoff horizon (7 days) key off these dates."""
    d = (_rd() - timedelta(days=days_ago)).isoformat()
    base = _row(rid, status="WIN", quoted=True, ol_rate=4000.0,
                carrier_quoted="MSC", carrier_won="MSC", mdolx_ref="MDOLX-W",
                request_date=d, request_timestamp=f"{d}T15:00:00Z",
                response_timestamp=f"{d}T18:00:00Z")
    base.update(kw)
    return base


# ── 1. Renderer writes both artifacts; subject shape ─────────────────────

def test_main_writes_both_artifacts_with_subject(tmp_path):
    fixture = ROOT / "tests" / "fixtures" / "golden_day.json"
    assert gce.main(["--data", str(fixture), "--out-dir", str(tmp_path)]) == 0
    body = (tmp_path / "client-email-body.html").read_text(encoding="utf-8")
    subject = (tmp_path / "client-email-subject.txt").read_text(encoding="utf-8")
    assert "Hilmar Ingredients" in subject
    label = gce._fmt_date(
        datetime.combine(core.report_business_day(datetime.now(core.ET)),
                         datetime.min.time()), "%b %-d, %Y")
    assert label in subject
    assert subject.startswith("OL-USA")
    assert "Daily Shipment Update" in body


# ── 2. Content invariants — service data in, internal analytics out ──────

def test_body_shows_rates_and_booking_refs_but_no_internal_analytics():
    html = gce.build_body(_mixed_data(), {})
    assert "$4,250" in html            # a quoted rate reaches the client
    assert "MDOLX-T1" in html          # a booking ref reaches the client
    low = html.lower()
    for marker in INTERNAL_MARKERS:
        assert marker not in low, f"internal marker leaked into client email: {marker!r}"
    # QC-065's own scanner must agree the rendered body is clean.
    assert q.qc065_internal_leaks(html) == []
    # QC-042: no data: URIs in email HTML (logo must be CID-referenced).
    assert "data:image" not in low


# ── 3. stand_* exclusion + bookings section ──────────────────────────────

def test_standalone_rows_excluded_from_requests_and_quotes():
    data = {"requests": [
        _row("r-normal"),
        _row("stand_0001", status="WIN", quoted=True, ol_rate=2800.0,
             carrier_won="HMM", mdolx_ref="MDOLX-S9",
             response_timestamp=f"{RD}T18:30:00Z", lane="Rollover → Yokohama",
             status_history=[{"from": "PENDING", "to": "WIN",
                              "at": f"{RD}T18:30:00Z", "reason": "standalone"}]),
    ]}
    s = gce._client_sections(data, core.report_business_day(datetime.now(core.ET)))
    ids = lambda rows: {r["request_id"] for r in rows}  # noqa: E731
    assert "stand_0001" not in ids(s["requests"])
    assert "stand_0001" not in ids(s["quotes"])
    # ...but its confirmed booking IS the honest client-facing event.
    assert "stand_0001" in ids(s["bookings"])
    assert "r-normal" in ids(s["requests"])


def test_bookings_section_shows_today_win():
    html = gce.build_body(_mixed_data(), {})
    assert "Bookings confirmed (1)" in html
    s = gce._client_sections(_mixed_data(), core.report_business_day(datetime.now(core.ET)))
    assert [r["request_id"] for r in s["bookings"]] == ["r-win"]


# ── 3b. FIX 2: unresolved-lane rows NEVER reach the client ────────────────

def test_unresolved_lane_row_excluded_from_client_body():
    """HARD GUARANTEE for Lonny (2026-07-14, run 29292014093): a booking whose
    lane is unresolved (destination Unknown/None OR lane 'Lane unresolved') is
    an OL-internal cleanup — it must not appear in ANY client section. A
    resolved booking on the same day still renders, and the body stays
    leak-free."""
    d = _rd().isoformat()
    data = {"requests": [
        _row("stand_bad", status="WIN", quoted=True, ol_rate=5100.0,
             carrier_won="ZIM", carrier_quoted="ZIM", mdolx_ref="MDOLX-BADLANE",
             lane="Lane unresolved", destination="Unknown",
             request_date=d, request_timestamp=f"{d}T15:00:00Z",
             response_timestamp=f"{d}T18:00:00Z",
             status_history=[{"from": "PENDING", "to": "WIN",
                              "at": f"{d}T18:00:00Z", "reason": "standalone"}]),
        _row("r-good", status="WIN", quoted=True, ol_rate=4200.0,
             carrier_won="MSC", carrier_quoted="MSC", mdolx_ref="MDOLX-GOODLANE",
             lane="Oakland → Tokyo",
             request_date=d, request_timestamp=f"{d}T15:00:00Z",
             response_timestamp=f"{d}T18:00:00Z",
             status_history=[{"from": "PENDING", "to": "WIN",
                              "at": f"{d}T18:00:00Z", "reason": "booked"}]),
    ]}
    html = gce.build_body(data, {})
    assert "MDOLX-BADLANE" not in html          # unresolvable booking hidden
    assert "ZIM" not in html                     # ...and its carrier
    assert "Lane unresolved" not in html
    assert "MDOLX-GOODLANE" in html              # resolved booking shown
    assert q.qc065_internal_leaks(html) == []


def test_unresolved_row_excluded_from_every_section_bucket():
    unres = _row("u1", status="PENDING", quoted=False, lane="Lane unresolved",
                 destination="Unknown")
    s = gce._client_sections({"requests": [unres]}, _rd())
    assert all(unres not in bucket for bucket in s.values())


# ── 4. PENDING split ──────────────────────────────────────────────────────

def test_pending_split_quoted_awaiting_vs_unquoted_in_progress():
    data = _mixed_data()
    s = gce._client_sections(data, core.report_business_day(datetime.now(core.ET)))
    assert [r["request_id"] for r in s["awaiting"]] == ["r-pend-hil"]
    assert [r["request_id"] for r in s["in_progress"]] == ["r-pend-ol"]
    html = gce.build_body(data, {})
    assert "Awaiting your decision (1)" in html
    assert "In progress — quote coming (1)" in html
    # "Awaiting your decision" is the client's ACTION LIST — with rows it must
    # render as a full table (never collapse), immediately after its heading.
    idx = html.index("Awaiting your decision (1)")
    assert "<table" in html[idx:idx + 400]
    # Zero-row day: both pending sections collapse to friendly one-liners.
    empty_html = gce.build_body({"requests": []}, {})
    assert "Nothing awaiting your decision" in empty_html
    assert "Nothing in the pricing queue" in empty_html


# ── 4b. Hero KPI strip ────────────────────────────────────────────────────

def _tile_value(html, label):
    """The big number rendered directly above a KPI tile's label."""
    m = re.search(r">([^<]*)</div>\s*<div[^>]*>" + re.escape(label) + r"</div>", html)
    assert m, f"KPI tile {label!r} missing"
    return m.group(1)


def test_hero_kpi_tiles_present_with_correct_counts():
    html = gce.build_body(_mixed_data(), {})
    assert _tile_value(html, "Requests received") == "5"
    assert _tile_value(html, "Quotes delivered") == "3"
    assert _tile_value(html, "Bookings confirmed") == "1"
    assert _tile_value(html, "Awaiting your decision") == "1"
    # Exactly 4 gen_email-style tiles sharing the mobile stacking class.
    assert html.count('class="hx-kpi"') == 4
    # Narrative under the tiles — counts + the PACIFIC reply-speed clause
    # (2026-07-12: today's quotes carry request/response timestamps, so the
    # PT-window metric renders; all three land on the same PT calendar day).
    assert "We received 5 rate requests and returned 3 quotes" in html
    assert "1 booking confirmed." in html
    assert "all the same business day" in html
    assert "business hours, Pacific)" in html


def test_narrative_uses_pt_window_not_stored_et_metric():
    """2026-07-12 (Michael 2026-07-11 "lonny is uswc and we are usec"): the
    reply-speed clause is computed request→response in the PACIFIC window
    (core.biz_hours_between_pt) — the stored turnaround_biz_hours is the
    ET staff-desk SLA and must never drive the client narrative."""
    data = _mixed_data()
    for r in data["requests"]:
        if r.get("response_timestamp") and r.get("ol_rate"):
            r["turnaround_biz_hours"] = 99.9   # ET metric — must NOT render
    html = gce.build_body(data, {})
    assert "business hours, Pacific)" in html
    assert "99.9" not in html


def test_narrative_omits_reply_speed_without_timestamps():
    """No request/response timestamps on today's quotes → the parenthetical
    is omitted, never guessed (and never fed from turnaround_biz_hours)."""
    data = _mixed_data()
    for r in data["requests"]:
        r.pop("request_timestamp", None)
        r["turnaround_biz_hours"] = 1.4
    html = gce.build_body(data, {})
    assert "business hours" not in html


# ── 4c. Active shipments (recent WINs) ────────────────────────────────────

def test_active_shipments_lists_recent_wins_sorted_by_etd():
    rd = _rd()
    data = {"requests": [
        _win("w-late", days_ago=2, mdolx_ref="MDOLX-L8",
             etd_offered=(rd + timedelta(days=25)).isoformat()),
        _win("w-early", days_ago=5, mdolx_ref="MDOLX-E1",
             vessel_voyage="MSC AURORA 331E",
             etd_offered=(rd + timedelta(days=20)).isoformat()),
        _win("w-noref", days_ago=1, mdolx_ref=None, etd_offered=None),
        _win("w-stale", days_ago=30, mdolx_ref="MDOLX-OLD"),
    ]}
    html = gce.build_body(data, {})
    assert "Active shipments (3)" in html
    assert "MDOLX-E1" in html and "MDOLX-L8" in html
    assert "MDOLX-OLD" not in html                 # outside the 14-day window
    assert "Confirmation to follow" in html        # booking ref not yet issued
    assert "MSC AURORA 331E" in html               # vessel column
    assert html.index("MDOLX-E1") < html.index("MDOLX-L8")  # ETD ascending
    assert f'bgcolor="{gce.STRIPE_BG}"' in html    # alternating row striping


# ── 4d. Upcoming-cutoffs callout ──────────────────────────────────────────

def test_cutoff_callout_lists_doc_cutoffs_within_seven_days_only():
    rd = _rd()
    near = (rd + timedelta(days=3)).isoformat()
    far = (rd + timedelta(days=20)).isoformat()
    data = {"requests": [
        _win("w-near", days_ago=1, lane="Oakland → Kobe", doc_cutoff=near,
             etd_offered=(rd + timedelta(days=6)).isoformat()),
        _win("w-far", days_ago=1, lane="Oakland → Laem Chabang", doc_cutoff=far,
             etd_offered=(rd + timedelta(days=22)).isoformat()),
    ]}
    html = gce.build_body(data, {})
    assert "Upcoming cutoffs" in html
    box = html[html.index("Upcoming cutoffs"):html.index("Active shipments")]
    assert "Oakland → Kobe — doc cutoff" in box
    assert "Laem Chabang" not in box
    # A shipment with everything outside the horizon renders NO callout.
    quiet = gce.build_body({"requests": [
        _win("w-far", days_ago=1, doc_cutoff=far,
             etd_offered=(rd + timedelta(days=22)).isoformat()),
    ]}, {})
    assert "Upcoming cutoffs" not in quiet


def test_cutoff_callout_falls_back_to_departure_when_no_doc_cutoff():
    rd = _rd()
    html = gce.build_body({"requests": [
        _win("w-sail", days_ago=1, lane="Oakland → Kaohsiung", doc_cutoff=None,
             etd_offered=(rd + timedelta(days=5)).isoformat()),
    ]}, {})
    assert "Upcoming cutoffs" in html
    assert "Oakland → Kaohsiung — vessel departs" in html
    # Unparseable dates are skipped defensively, not rendered or raised.
    junk = gce.build_body({"requests": [
        _win("w-junk", days_ago=1, doc_cutoff="TBD", etd_offered="see booking"),
    ]}, {})
    assert "Upcoming cutoffs" not in junk


# ── 4e. Quiet day — empty sections collapse, email stays composed ─────────

def test_quiet_day_collapses_empty_sections_to_friendly_lines():
    html = gce.build_body({"requests": []}, {})
    # Exactly ONE table remains — the hero KPI strip. No empty data tables.
    assert html.count("<table") == 1
    assert 'class="hx-data"' not in html
    for line in (
        "No shipments currently in transit or awaiting departure.",
        "No new rate requests that day.",
        "No new quotes that day.",
        "No new bookings confirmed that day.",
        "Nothing awaiting your decision — all caught up.",
        "Nothing in the pricing queue — every request has been quoted.",
    ):
        assert line in html, f"missing friendly line: {line!r}"
    assert "None that day." not in html
    # Hero tiles still render (zeros) + narrative + footer → composed email.
    assert html.count('class="hx-kpi"') == 4
    assert _tile_value(html, "Requests received") == "0"
    assert "A quiet day on new activity" in html
    assert "Reply with the booking reference" in html
    # And a quiet day is still leak-free.
    assert q.qc065_internal_leaks(html) == []


# ── 4f. Mobile CSS — KPI tiles stack block/full-width, tables scroll ──────

def test_mobile_css_kpi_tiles_stack_block_full_width():
    html = gce.build_body(_mixed_data(), {})
    m = re.search(r"<style>(.*?)</style>", html, re.S)
    assert m, "mobile <style> block missing"
    css = m.group(1)
    assert "td.hx-kpi { display:block !important; width:100% !important" in css
    assert ".hx-kpi-card { height:auto !important" in css
    assert "table.hx-data { min-width:640px !important; }" in css
    # Regression guard: the failed first ship stacked tiles inline-block/50%
    # and iOS Mail collapsed them to strips (Michael 2026-07-02 screenshot).
    assert "inline-block" not in css
    assert "50%" not in css


# ── 4g. Golden fixture render — leak-free; subject format pinned ──────────

def test_golden_fixture_render_is_leak_free(tmp_path):
    fixture = ROOT / "tests" / "fixtures" / "golden_day.json"
    assert gce.main(["--data", str(fixture), "--out-dir", str(tmp_path)]) == 0
    body = (tmp_path / "client-email-body.html").read_text(encoding="utf-8")
    assert q.qc065_internal_leaks(body) == []


def test_subject_format_unchanged():
    label = gce._fmt_date(
        datetime.combine(_rd(), datetime.min.time()), "%b %-d, %Y")
    assert gce.build_subject({}, {}) == (
        f"OL-USA — Daily Shipment Update for Hilmar Ingredients ({label})")


# ── 5. QC-065 — client-report invariants ──────────────────────────────────

def _qc065_run(tmp_path, monkeypatch, cfg_dict, body_text=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg_dict), encoding="utf-8")
    body_path = tmp_path / "client-email-body.html"
    if body_text is not None:
        body_path.write_text(body_text, encoding="utf-8")
    monkeypatch.setattr(q, "QC065_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(q, "QC065_CLIENT_BODY_PATH", body_path)
    log = q.Log()
    q.phase_6_rules(log, {"requests": [], "summary": {}})
    return log


def _cfg(enabled, to=None, cc=None):
    return {
        "distribution": {"full_list": [
            "michael.deitchman@ol-usa.com", "michael.deitchman@idealx.us",
            "alan.baer@ol-usa.com"]},
        "client_report": {"enabled": enabled,
                          "to": APPROVED_TO if to is None else to,
                          "cc": APPROVED_CC if cc is None else cc},
    }


def test_qc065_errors_when_enabled_with_wrong_recipients(tmp_path, monkeypatch):
    log = _qc065_run(tmp_path, monkeypatch,
                     _cfg(True, to=["someone.else@hilmaringredients.com"]))
    assert any("QC-065" in m for m in log.errors)


def test_qc065_errors_when_enabled_with_wrong_cc(tmp_path, monkeypatch):
    log = _qc065_run(tmp_path, monkeypatch, _cfg(True, cc=["extra@ol-usa.com"]))
    assert any("QC-065" in m for m in log.errors)


def test_qc065_clean_when_enabled_with_exact_approved_pair(tmp_path, monkeypatch):
    log = _qc065_run(tmp_path, monkeypatch, _cfg(True))
    assert not any("QC-065" in m for m in log.errors + log.warnings)


def test_qc065_errors_on_staff_recipient_even_while_disabled(tmp_path, monkeypatch):
    # The internal 10-list must never appear in client_report.to — checked
    # even while disabled (a wrong value goes live the moment the flag flips).
    log = _qc065_run(tmp_path, monkeypatch,
                     _cfg(False, to=["alan.baer@ol-usa.com"]))
    assert any("QC-065" in m for m in log.errors)


def test_qc065_errors_on_more_than_one_client_recipient(tmp_path, monkeypatch):
    log = _qc065_run(tmp_path, monkeypatch,
                     _cfg(False, to=APPROVED_TO + ["second@hilmaringredients.com"]))
    assert any("QC-065" in m for m in log.errors)


def test_qc065_disabled_reports_sample_only_mode(tmp_path, monkeypatch, capsys):
    log = _qc065_run(tmp_path, monkeypatch, _cfg(False))
    assert not any("QC-065" in m for m in log.errors + log.warnings)
    out = capsys.readouterr().out
    assert "QC-065" in out and "sample-only" in out


def test_qc065_errors_on_internal_string_leak_in_body(tmp_path, monkeypatch):
    log = _qc065_run(tmp_path, monkeypatch, _cfg(False),
                     body_text="<p>Win Rate: 62%</p>")
    assert any("QC-065" in m and "leak" in m for m in log.errors)


def test_qc065_catches_escaped_form_leak(tmp_path, monkeypatch):
    log = _qc065_run(tmp_path, monkeypatch, _cfg(False),
                     body_text="<td>Quoted &amp; Lost</td>")
    assert any("QC-065" in m for m in log.errors)


def test_qc065_clean_body_passes(tmp_path, monkeypatch):
    log = _qc065_run(tmp_path, monkeypatch, _cfg(False),
                     body_text=gce.build_body(_mixed_data(), {}))
    assert not any("QC-065" in m for m in log.errors + log.warnings)


# ── 6. outlook_send --flag-name override ──────────────────────────────────

def _send_args(tmp_path, **over):
    subj = tmp_path / "subject.txt"
    body = tmp_path / "body.html"
    subj.write_text("Client Update", encoding="utf-8")
    body.write_text("<p>hi</p>", encoding="utf-8")
    base = dict(
        to=None, cc=None, to_from_config=False,
        subject_from_file=str(subj), body_from_file=str(body),
        attach=None, dry=False, force=False, no_flag=False, flag_name=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _wire_send(tmp_path, monkeypatch):
    sends: list[list[str]] = []
    monkeypatch.setattr(os_send, "ROOT", tmp_path)
    monkeypatch.setattr(os_send, "send_mail",
                        lambda **kw: sends.append(kw["to"]) or "req-test")
    monkeypatch.setattr(os_send, "_sent_today_in_mailbox", lambda s: None)
    return sends


def test_flag_name_override_writes_client_sent_flag(tmp_path, monkeypatch):
    sends = _wire_send(tmp_path, monkeypatch)
    args = _send_args(tmp_path, to=APPROVED_TO, cc=APPROVED_CC,
                      flag_name="client-sent")
    assert os_send.cmd_daily(args) == 0
    flagdate = os_send._flag_date(datetime.now(ZoneInfo("America/New_York")))
    flag = tmp_path / "reports" / f"client-sent-{flagdate}.flag"
    assert flag.exists()
    # The client namespace is idempotent on its own: a second send is refused.
    assert os_send.cmd_daily(args) == 0
    assert len(sends) == 1
    # And it never touched the staff namespaces.
    assert not (tmp_path / "reports" / f"sent-{flagdate}.flag").exists()
    assert not (tmp_path / "reports" / f"improvements-sent-{flagdate}.flag").exists()


def test_flag_name_absent_keeps_derived_behavior(tmp_path, monkeypatch):
    _wire_send(tmp_path, monkeypatch)
    assert os_send.cmd_daily(
        _send_args(tmp_path, to=["michael.deitchman@idealx.us"])) == 0
    flagdate = os_send._flag_date(datetime.now(ZoneInfo("America/New_York")))
    assert (tmp_path / "reports" / f"improvements-sent-{flagdate}.flag").exists()


def test_no_flag_beats_flag_name_override(tmp_path, monkeypatch):
    # A verification/sample send (--no-flag) must never touch idempotency
    # state, even when a flag name is supplied.
    sends = _wire_send(tmp_path, monkeypatch)
    args = _send_args(tmp_path, to=APPROVED_TO, flag_name="client-sent",
                      no_flag=True)
    assert os_send.cmd_daily(args) == 0
    assert len(sends) == 1
    flagdate = os_send._flag_date(datetime.now(ZoneInfo("America/New_York")))
    assert not (tmp_path / "reports" / f"client-sent-{flagdate}.flag").exists()


# ── 7. config.json ships gated off with the exact approved recipients ─────

def test_config_client_report_live_state():
    # Go-live approved by Michael Deitchman 2026-07-12 (recorded in the
    # config _note + CHANGELOG). This test pins the LIVE state: enabled,
    # and recipients EXACTLY the QC-065-approved pair — any drift is red.
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cr = cfg["client_report"]
    assert cr["enabled"] is True
    assert cr["to"] == APPROVED_TO
    assert cr["cc"] == APPROVED_CC
    assert cr["sample_to"] == ["michael.deitchman@idealx.us"]


# ── 8. Pipeline wiring — step present, ordered, best-effort ───────────────

def test_pipeline_step_present_after_staff_email_and_best_effort():
    names = [s[0] for s in rp.STEPS]
    assert "Client-facing email HTML" in names
    assert names.index("Client-facing email HTML") == names.index("Email body HTML") + 1
    # A brand-new artifact must never block the staff email.
    assert "Client-facing email HTML" in rp.BEST_EFFORT_STEPS


# ── 9. state_store syncs the client-send idempotency flag ─────────────────

def test_state_paths_include_client_sent_flags():
    paths = state_store.state_paths("2031-01-02")
    assert "reports/client-sent-2031-01-02.flag" in paths
    # Every day whose staff flag syncs also syncs its client flag (incl. the
    # report-business-day variant when it differs from the calendar day).
    staff_days = [p[len("reports/sent-"):-len(".flag")]
                  for p in paths if p.startswith("reports/sent-")]
    assert staff_days
    for d in staff_days:
        assert f"reports/client-sent-{d}.flag" in paths


def test_pdf_lane_performance_carriers_wrap_not_overflow():
    """gen_pdf Lane Performance: the Winning Carriers cell is a wrapping
    Paragraph inside its column, never a raw string that overflows the page
    margin (Michael 2026-07-12 "poor formatting" — multi-carrier lists spilled
    past the 1.0in column). Pins: cells are Paragraphs, and the worst-case
    3-carrier list stays within the widened column width."""
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "scripts"))
    import gen_pdf as G
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph

    data = {"lane_summary": {
        "Oakland → Xingang": {
            "requests": 31, "wins": 5, "quoted_lost": 26, "not_quoted": 0,
            "pending": 0, "teu_requested": 130, "teu_won": 30,
            "winning_carriers": "CMA CGM, HMM, Hapag-Lloyd"},
    }}
    styles = G.make_styles()
    story = []
    G.build_lanes(story, styles, data)
    table = story[-1]
    # Last column of the data row must be a Paragraph (wraps), not a str.
    carriers_cell = table._cellvalues[1][-1]
    assert isinstance(carriers_cell, Paragraph), \
        "Winning Carriers must be a wrapping Paragraph, not a raw string"
    # And it fits inside the carriers column (1.75in) — no margin overflow.
    w, _h = carriers_cell.wrap(1.75 * inch, 100)
    assert w <= 1.75 * inch


# ── 10. FIX 4: client-facing trade-region rollup drops unresolved rows ─────

def test_aggregate_trade_regions_excludes_unresolved_when_flagged():
    """include_unresolved=False (CLIENT surfaces) drops None/placeholder-
    destination rows so no mystery 'Unmapped' region shows; the default keeps
    them so STAFF/QC totals still reconcile to summary. A real-but-unmapped
    string is NOT dropped — it stays the extend-the-map signal."""
    rows = [
        {"status": "WIN", "destination": None, "teu_requested": 2, "teu_won": 2,
         "request_id": "x"},
        {"status": "WIN", "destination": "Unknown", "teu_requested": 2,
         "teu_won": 2, "request_id": "y"},
        {"status": "WIN", "destination": "Yokohama", "teu_requested": 2,
         "teu_won": 2, "request_id": "z"},
    ]
    assert "Unmapped" in core.aggregate_trade_regions(rows)          # staff/QC default
    client = core.aggregate_trade_regions(rows, include_unresolved=False)
    assert "Unmapped" not in client
    assert "Far East" in client                                     # real dest kept


def test_is_unresolved_destination_predicate():
    assert core.is_unresolved_destination(None)
    assert core.is_unresolved_destination("Unknown")
    assert core.is_unresolved_destination("  n/a ")
    assert core.is_unresolved_destination("")
    assert not core.is_unresolved_destination("Yokohama")
    # A real-but-unmapped string is NOT unresolved (keeps the map-gap signal).
    assert not core.is_unresolved_destination("Totally Fake Port 9000")


def test_pdf_trade_regions_no_unmapped_row_for_placeholder_dest():
    """gen_pdf (client-facing) must not render an 'Unmapped' region for a
    healed None/placeholder destination; the excluded rows are reconciled in
    the footnote, never silently dropped."""
    import gen_pdf as G
    from reportlab.platypus import Paragraph, Table

    data = {"requests": [
        {"status": "WIN", "destination": None, "teu_requested": 2, "teu_won": 2,
         "request_id": "x", "lane": "Lane unresolved"},
        {"status": "WIN", "destination": "Yokohama", "teu_requested": 4,
         "teu_won": 4, "request_id": "z"},
    ], "summary": {"total_entries": 2, "wins": 2, "quoted_lost": 0,
                   "not_quoted": 0, "pending_hilmar": 0}}
    styles = G.make_styles()
    story: list = []
    G.build_trade_regions(story, styles, data)
    table = next(s for s in story if isinstance(s, Table))
    region_col = [str(row[0]) for row in table._cellvalues]
    assert "Unmapped" not in region_col
    assert "Far East" in region_col                                 # Yokohama's region
    footnotes = " ".join(s.text for s in story if isinstance(s, Paragraph))
    assert "pending lane assignment" in footnotes
