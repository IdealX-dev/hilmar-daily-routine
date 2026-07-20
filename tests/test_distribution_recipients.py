"""Distribution-list invariants for the real config.json.

Michael 2026-07-20: "remove caren tobel from all reports." Caren was a RECIPIENT
of the daily/weekly reports (distribution.full_list); this locks her out so she
is never re-added. She remains a SENDER exclusion (ingest_scope.mailboxes_excluded)
— her rate-desk emails were never Hilmar rates and that filtering is unrelated to
who receives the report.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

CAREN = "caren.tobel@ol-usa.com"


def test_caren_is_not_a_report_recipient():
    full = [a.lower() for a in CONFIG["distribution"]["full_list"]]
    assert CAREN not in full, "Caren Tobel must not receive the reports (removed 2026-07-20)"


def test_caren_remains_a_sender_exclusion():
    excluded = [a.lower() for a in CONFIG["ingest_scope"]["mailboxes_excluded"]]
    assert CAREN in excluded, (
        "Caren's SENDER exclusion is separate from recipient removal — her "
        "rate-desk emails must still be filtered out of ingest."
    )


def test_full_list_still_valid_after_removal():
    full = CONFIG["distribution"]["full_list"]
    # Michael's own address must stay; the self-heal recipient invariant allows
    # 8-12 recipients in normal mode.
    assert "michael.deitchman@idealx.us" in full
    assert 8 <= len(full) <= 12, f"unexpected recipient count {len(full)}"
    # No external (non-ol-usa, non-idealx) domains may leak into the list.
    external = [a for a in full
                if not (a.lower().endswith("@ol-usa.com") or a.lower().endswith("@idealx.us"))]
    assert not external, f"external recipients present: {external}"
