"""Smoke/contract test for scripts/gen_pdf.py (audit finding [8]).

gen_pdf is Step 12 — the 6-page client PDF emailed daily — yet no test imported
it. This builds the full reportlab story from the golden fixture through every
build_* section and renders a real PDF to a tmp path, asserting it is a valid,
non-trivial document. A crash in any section (or an empty render) now fails CI
instead of shipping a broken/empty attachment.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="module")
def fixture_data():
    return json.loads((ROOT / "tests" / "fixtures" / "golden_day.json").read_text())


@pytest.fixture(scope="module")
def cfg():
    return json.loads((ROOT / "config.json").read_text())


def test_full_pdf_builds_and_is_valid(tmp_path, cfg, fixture_data):
    import gen_pdf as GP

    styles = GP.make_styles()
    story = []
    # Every section the daily PDF emits — a crash in any is a client-facing bug.
    GP.build_cover(story, styles, fixture_data, cfg)
    GP.build_dod(story, styles, fixture_data)
    GP.build_turnaround(story, styles, fixture_data)
    GP.build_carriers(story, styles, fixture_data)
    GP.build_trade_regions(story, styles, fixture_data)
    GP.build_lanes(story, styles, fixture_data)
    GP.build_pending_trends_qc(story, styles, fixture_data)
    assert story, "story must not be empty"

    out = tmp_path / "report.pdf"
    doc = GP.SimpleDocTemplate(
        str(out), pagesize=GP.LETTER,
        leftMargin=0.5 * GP.inch, rightMargin=0.5 * GP.inch,
        topMargin=0.7 * GP.inch, bottomMargin=0.55 * GP.inch,
    )
    doc.build(story)

    assert out.exists()
    blob = out.read_bytes()
    assert blob.startswith(b"%PDF"), "must be a valid PDF"
    assert len(blob) > 3000, "a 6-page report should be more than a few KB"


def test_cover_section_alone_builds(cfg, fixture_data):
    """Isolate the cover so a regression there is attributable."""
    import gen_pdf as GP
    styles = GP.make_styles()
    story = []
    GP.build_cover(story, styles, fixture_data, cfg)
    assert story
