"""An unmapped destination must NAME ITSELF, at every count.

WHAT HAPPENED, 2026-08-16. Michael's report carried a pink Unmapped row —
3 requests, 18 TEU, 2 wins, 66.7% — and QC-015 reported OK. The tiers were:

    >10  -> log.error
    >5   -> log.warn
    else -> log.ok          # green, and the port names never printed

So one to five unmapped destinations were green AND silent. Worse, the
silent branch named `_urows` (rows with NO destination at all) rather than
`_unmapped` (real ports missing from the map) — two different conditions.
With `_urows` empty it printed "zero unresolved rows" while three real ports
sat unclassified on the CEO's report.

Finding them took reading the delivered email out of his mailbox and running
every lane in it back through trade_region_for. Michael: "we fixed this at
root before and it's back." He was right that it was back, and right that
the earlier fix was a root fix: the 2026-08-05 comma-qualified LOOKUP is
intact and still correct. What was broken is the DETECTOR — and a detector
that hides small counts is how a symptom returns looking like a regression.

TWO RULES PINNED HERE:
  1. Any non-empty unmapped list is reported, never log.ok, and always with
     the port names — the names are the entire actionable payload.
  2. The email's Unmapped row carries the names too, because the audit is
     not what Michael reads and the lane tables show a top-N these rows fall
     outside of.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402

QC_SRC = (ROOT / "scripts" / "qc_selfheal.py").read_text(encoding="utf-8")
EMAIL_SRC = (ROOT / "scripts" / "gen_email.py").read_text(encoding="utf-8")


# ── the ports that were actually sitting unmapped ─────────────────────────

def test_shekou_maps_to_far_east():
    """Confirmed in the real book as ol_260291 (Oakland → Shekou)."""
    assert core.trade_region_for("Shekou") == "Far East"


def test_lyttelton_maps_to_oceania():
    """Confirmed in the real book as ol_260140 (Oakland → Lyttelton)."""
    assert core.trade_region_for("Lyttelton") == "Oceania"


def test_the_2026_08_05_comma_lookup_still_works():
    """The earlier root fix. It was never the problem this time, and a
    regression here would look identical from the report."""
    assert core.trade_region_for("Shanghai, CN") == "Far East"
    assert core.trade_region_for("Shekou, CN") == "Far East"
    assert core.trade_region_for("Lyttelton, NZ") == "Oceania"


def test_paren_terminal_forms_still_resolve():
    assert core.trade_region_for("HCMC (Cai Mep)") == "SE Asia"
    assert core.trade_region_for("Manila (North)") == "SE Asia"


def test_a_genuinely_unknown_port_still_reports_unmapped():
    """The signal must survive. Unmapped is how a new port announces
    itself; silencing it is what this whole file exists to prevent."""
    assert core.trade_region_for("Zzyzx Harbour") == "Unmapped"


# ── the detector ──────────────────────────────────────────────────────────

def _qc015_tier_block() -> str:
    m = re.search(r"_names = \", \"\.join.*?QC-015: zero unmapped destinations;",
                  QC_SRC, re.S)
    assert m, "QC-015 unmapped tier block not found — did it get rewritten?"
    return m.group(0)


def test_a_small_unmapped_count_is_not_logged_ok():
    """THE REGRESSION. `elif len(_unmapped) > 5` followed by `log.ok` meant
    1-5 unmapped ports reported green."""
    block = _qc015_tier_block()
    ok_calls = re.findall(r"log\.ok\((.{0,80})", block, re.S)
    for call in ok_calls:
        assert "_unmapped" not in call or "zero unmapped" in call, (
            "QC-015 logs a non-empty unmapped list as OK again. Michael's "
            "rule is 'unmapped shouldn't exist' — a pink row on his report "
            "must never be green here, however few ports caused it."
        )


def test_the_port_names_are_always_in_the_message():
    """The count alone cost a full investigation. The names are the fix."""
    block = _qc015_tier_block()
    assert "_names" in block, (
        "QC-015 no longer interpolates the port names. A count without "
        "names is not actionable — it is what sent 2026-08-16 to a manual "
        "mailbox read to identify three ports."
    )
    assert re.search(r"if len\(_unmapped\) > 10:\s*\n\s*log\.error", block), (
        "QC-015 lost its error tier for large unmapped counts."
    )


def test_unmapped_and_unresolved_are_reported_as_different_things():
    """`_urows` is rows with NO destination; `_unmapped` is real ports
    missing from the map. Conflating them is how 'zero unresolved rows'
    got printed while three ports sat unclassified."""
    block = _qc015_tier_block()
    assert "zero unmapped destinations" in block, (
        "the no-unmapped branch no longer says so plainly, which is how the "
        "two conditions got confused the first time."
    )


# ── the report ────────────────────────────────────────────────────────────

def test_email_unmapped_row_names_the_destinations():
    """The audit is not what Michael reads, and the lane tables show a
    top-N that these rows fall outside of. The row itself must say which
    ports it is made of."""
    m = re.search(r'if m\["region"\] == "Unmapped":(.{0,1400})', EMAIL_SRC, re.S)
    assert m, "the Unmapped row branch is gone from gen_email"
    branch = m.group(1)
    assert 'm.get("destinations")' in branch, (
        "the Unmapped row no longer renders its destinations. It is then a "
        "bare count again, and identifying the ports needs a mailbox read."
    )


def test_aggregate_still_supplies_destinations_to_the_renderer():
    """The renderer's names come from the aggregator; if this field ever
    stops being populated the row silently goes back to a bare count."""
    rows = [
        {"destination": "Zzyzx Harbour", "status": "WIN", "teu_requested": 2},
        {"destination": "Shekou", "status": "WIN", "teu_requested": 2},
    ]
    regions = core.aggregate_trade_regions(rows)
    unmapped = next((m for m in regions.values() if m["region"] == "Unmapped"), None)
    assert unmapped is not None, "the unknown port did not land in Unmapped"
    assert "Zzyzx Harbour" in unmapped["destinations"], (
        f"destinations={unmapped['destinations']} — the renderer and QC both "
        "read this field; empty means a bare count reaches the report."
    )
    assert "Far East" in regions, "Shekou should now map, not sit in Unmapped"
