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
import sys
from datetime import datetime
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
    assert "Bookings confirmed today (1)" in html
    s = gce._client_sections(_mixed_data(), core.report_business_day(datetime.now(core.ET)))
    assert [r["request_id"] for r in s["bookings"]] == ["r-win"]


# ── 4. PENDING split ──────────────────────────────────────────────────────

def test_pending_split_quoted_awaiting_vs_unquoted_in_progress():
    data = _mixed_data()
    s = gce._client_sections(data, core.report_business_day(datetime.now(core.ET)))
    assert [r["request_id"] for r in s["awaiting"]] == ["r-pend-hil"]
    assert [r["request_id"] for r in s["in_progress"]] == ["r-pend-ol"]
    html = gce.build_body(data, {})
    assert "Awaiting your decision (1)" in html
    assert "In progress — quote coming (1)" in html
    # Empty sections keep the email's stable shape with a friendly row.
    empty_html = gce.build_body({"requests": []}, {})
    assert empty_html.count("None today.") == 5


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

def test_config_client_report_ships_gated_off():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cr = cfg["client_report"]
    assert cr["enabled"] is False
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
