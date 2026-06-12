"""PENDING substates — per Michael 2026-06-12 "on pending there should be
several pending statuses to be clear": Pending OL Quote (RFQ sent, OL
hasn't quoted — chase OL) vs Pending Hilmar Response (quoted, Lonny
deciding — chase Lonny). Derived at render time; the 4-status state
machine and the data file are untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from hilmar import core as hcore  # noqa: E402


def test_substate_derivation_both_trees():
    import core as score
    for core in (hcore, score):
        assert core.pending_substate({"status": "PENDING", "quoted": True}) == "PENDING_HILMAR"
        assert core.pending_substate({"status": "PENDING", "quoted": False}) == "PENDING_OL"
        assert core.pending_substate({"status": "PENDING"}) == "PENDING_OL"
        assert core.pending_substate({"status": "WIN", "quoted": True}) is None
        assert core.pending_substate({"status": "LOSS"}) is None


def test_email_renders_both_pending_sections():
    import gen_email as ge
    ol_row = {"status": "PENDING", "lane": "Dalhart → Caucedo",
              "containers": "1-20'", "teu_requested": 1, "product": "Protein",
              "request_timestamp": "2026-06-12T13:52:00+00:00"}
    hil_row = {"status": "PENDING", "quoted": True, "lane": "Oakland → Busan",
               "containers": "2-40' HC", "teu_requested": 4,
               "request_timestamp": "2026-06-11T16:00:00+00:00",
               "response_timestamp": "2026-06-11T18:00:00+00:00",
               "carrier_quoted": "ONE", "ol_rate": 615.0}

    ol_html = ge._pending_ol_html([ol_row])
    assert "Pending OL Quote" in ol_html
    assert "Dalhart → Caucedo" in ol_html
    assert "Waiting on OL" in ol_html
    # quoted-only section must NOT receive unquoted rows — and renders fine
    hil_html = ge._pending_html([hil_row])
    assert "Pending Hilmar Response" in hil_html
    assert "Oakland → Busan" in hil_html
    # empty inputs render nothing (section disappears instead of an empty shell)
    assert ge._pending_ol_html([]) == ""
