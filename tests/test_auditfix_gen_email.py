"""Regression test for the audit fix to scripts/gen_email.py.

Finding: the "Pending" KPI tiles draw their value from
``summary["pending_hilmar"]`` / ``day["pending"]``, which (per
``core.aggregate_summary``) counts EVERY row with status==PENDING — both
PENDING_HILMAR (OL quoted, awaiting Lonny) AND PENDING_OL (RFQ sent, OL has
not quoted yet). The headline tiles, however, labeled that combined count
"awaiting Hilmar" (day-row tile) and "Pending Lonny" (period tile),
overstating how many shipments actually wait on Lonny/Hilmar and
misattributing OL-side delays to the client.

The safe fix is a label-only correction: make the tile labels/sublabels
party-neutral so the all-party pending value is honest. We do NOT change any
counts or the ``pending_hilmar`` key (that would alter the fixture-pinned
summary contract and require its own QC check + test per CLAUDE.md Rule #3).

These assertions read the source statically so the test is dependency-light
and does not require rendering the full email. They fail against the
pre-fix source (which contained the misleading party-specific labels) and
pass against the corrected source.
"""

from pathlib import Path

GEN_EMAIL = Path(__file__).resolve().parents[1] / "scripts" / "gen_email.py"


def _kpi_block_source() -> str:
    """Return the source of the _kpi_block_html function body."""
    src = GEN_EMAIL.read_text(encoding="utf-8")
    start = src.index("def _kpi_block_html(")
    # Stop at the next top-level def so we only inspect this function.
    rest = src[start + 1 :]
    nxt = rest.find("\ndef ")
    end = start + 1 + nxt if nxt != -1 else len(src)
    return src[start:end]


def test_misleading_pending_labels_removed():
    """The party-specific 'awaiting Hilmar' / 'Pending Lonny' labels are gone.

    The combined PENDING value mixes PENDING_OL and PENDING_HILMAR, so it
    must not be labeled as if it were waiting only on Hilmar/Lonny.
    """
    block = _kpi_block_source()
    assert "awaiting Hilmar" not in block, (
        "day-row Pending tile still labels the combined (OL+Lonny) pending "
        "count 'awaiting Hilmar'"
    )
    assert "Pending Lonny" not in block, (
        "period Pending tile still labels the combined (OL+Lonny) pending "
        "count 'Pending Lonny'"
    )


def test_pending_tiles_still_present_with_neutral_labels():
    """A Pending tile still exists, now with party-neutral wording."""
    block = _kpi_block_source()
    # Day-row tile keeps its 'Pending — {day_short}' label and gains a
    # party-neutral sublabel covering both substates.
    assert "OL quote + Lonny decision" in block
    # Period tile is now a plain 'Pending' with an any-party sublabel.
    assert '"Pending"' in block
    assert "any party" in block


def test_pending_value_source_unchanged():
    """The fix is label-only: the value still comes from pending_hilmar / day['pending']."""
    block = _kpi_block_source()
    # Value sources must be untouched (no count/key change in this safe fix).
    assert 'summary.get("pending_hilmar", 0)' in block
    assert "day['pending']" in block
