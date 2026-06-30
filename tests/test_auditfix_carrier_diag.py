"""Carrier-extraction diagnostics — PII-scrubbed audit surface (QC-056/QC-002).

These guard the diagnostic that surfaces the EXACT (scrubbed) text the carrier
parser failed on for the stuck rate-without-carrier (QC-056) and
WIN-without-carrier_won (QC-002) rows, in BOTH the idealx.us audit email and the
uploaded qc-result.json artifact. The hard rule is PII: no raw emails / MDOLX /
IMIDs may leak — the snippet must go through sentry_setup._scrub_string. Tests
drive the pure helper directly + the real phase_6_rules log path (sys.path
pattern from tests/test_env_integrity_checks.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_improvements_report as g  # noqa: E402
import qc_selfheal as q  # noqa: E402


def _base_data():
    return {"version": "2", "requests": [],
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                        "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                        "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}}


def _run(monkeypatch, data):
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    log = q.Log()
    q.phase_6_rules(log, data)
    return log


# ── _carrier_diag_snippet: PII scrubbing ─────────────────────────────────
def test_carrier_diag_snippet_scrubs_pii_but_keeps_carrier():
    """A body + stored fields carrying an email, an MDOLX ref and an internet
    message-id must come back REDACTED, while a real carrier token survives so
    the parser can be fixed against the actual text."""
    imid = "<AB12@host.com>"
    bodies_idx = {imid: {"text_body": (
        "Ocean freight via Wan Hai vessel\n"
        "contact lupfold@hilmaringredients.com booking MDOLX260432\n"
        "rate $1500"
    )}}
    r = {
        "lane": "LA-Yokohama", "status": "LOSS", "ol_rate": 1500,
        "source_imids": [imid],
        "reason_detail": "lost; see lupfold@hilmaringredients.com / MDOLX260432",
    }
    d = q._carrier_diag_snippet(r, bodies_idx)
    snip = d["snippet"]
    # None of the raw PII tokens may appear.
    assert "lupfold@hilmaringredients.com" not in snip
    assert "MDOLX260432" not in snip
    assert "AB12@host.com" not in snip
    # The real carrier token survives so the parser fix can target it.
    assert "Wan Hai" in snip
    assert d["has_body"] is True
    assert d["lane"] == "LA-Yokohama"
    assert d["rate"] == 1500


def test_carrier_diag_snippet_bare_rate_path():
    """No cached body + empty carrier fields → the 'BARE rate' explanation and
    has_body False (nothing to extract; a genuinely bare quote)."""
    r = {"lane": "Dalhart-Busan", "status": "LOSS", "ol_rate": 900,
         "source_imids": [], "quoted": True}
    d = q._carrier_diag_snippet(r, {})
    assert d["has_body"] is False
    assert "BARE rate" in d["snippet"]
    assert d["lane"] == "Dalhart-Busan"


def test_carrier_diag_snippet_never_raises_on_bad_input():
    """A diagnostic must never break the QC run — malformed input yields a
    minimal, still-safe dict."""
    d = q._carrier_diag_snippet(None, None)  # type: ignore[arg-type]
    assert "snippet" in d


# ── phase_6_rules: QC-056 populates log.carrier_diag ─────────────────────
def test_phase6_qc056_populates_carrier_diag(monkeypatch):
    data = _base_data()
    data["requests"].append({
        "request_id": "req_t1", "status": "LOSS", "quoted": True,
        "ol_rate": 1234, "carrier_quoted": None, "lane": "Oakland-Manila",
        "source_imids": [],
    })
    log = _run(monkeypatch, data)
    qc056 = [d for d in log.carrier_diag if d.get("check") == "QC-056"]
    assert qc056, log.carrier_diag
    assert qc056[0]["lane"] == "Oakland-Manila"
    # Bare rate (no body, no stored carrier fields) → the BARE-rate explainer.
    assert "BARE rate" in qc056[0]["snippet"]


# ── gen_improvements_report: the rendered section ────────────────────────
def test_carrier_diag_section_renders_lane_and_snippet():
    qc = {"carrier_diagnostics": [
        {"lane": "Oakland-Manila", "check": "QC-056", "rate": 797,
         "has_body": True, "snippet": "Ocean freight via Wan Hai | rate $797"},
    ]}
    html = g._carrier_diag_section(qc)
    assert "Carrier-extraction diagnostics" in html
    assert "Oakland-Manila" in html
    assert "Wan Hai" in html
    assert "QC-056" in html


def test_carrier_diag_section_empty_renders_nothing():
    assert g._carrier_diag_section({"carrier_diagnostics": []}) == ""
    assert g._carrier_diag_section({}) == ""
    assert g._carrier_diag_section(None) == ""


def test_carrier_diag_section_escapes_html():
    """Every value is _esc()'d — a snippet with HTML specials can't break the
    audit email."""
    qc = {"carrier_diagnostics": [
        {"lane": "A->B", "check": "QC-002", "rate": None, "has_body": False,
         "snippet": "<script>alert(1)</script> & 'quote'"},
    ]}
    html = g._carrier_diag_section(qc)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "A-&gt;B" in html
