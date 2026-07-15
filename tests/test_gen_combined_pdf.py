"""Smoke/contract tests for scripts/gen_combined_pdf.py.

Michael 2026-07-15: "the 3 reports should be made into one and emailed to
everyone as pdfs." One PDF = tracker report + client-service-update copy +
systems audit, attached to ONE staff email (10-recipient full_list). Lonny's
client email is unchanged (QC-065).

Locks: the combined PDF builds end-to-end from the golden fixture and contains
all three parts; the client part inherits gen_client_email's resolved-lane
filter (imported, not reimplemented); daily.yml sends ONE email with the
combined PDF and keeps the 3-email fallback if the build fails.
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


@pytest.fixture()
def tmp_cfg(tmp_path, fixture_data):
    """Real config with paths pointed at a tmp copy of the golden fixture."""
    cfg = json.loads((ROOT / "config.json").read_text())
    data_path = tmp_path / "tracking-data-v2.json"
    data_path.write_text(json.dumps(fixture_data), encoding="utf-8")
    cfg["paths"]["data"] = str(data_path)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg_path


def _pdf_text(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def test_combined_pdf_builds_with_all_three_parts(tmp_path, tmp_cfg):
    import gen_combined_pdf as GCP

    out = tmp_path / "combined.pdf"
    rc = GCP.main(["--config", str(tmp_cfg), "--out", str(out)])
    assert rc == 0
    blob = out.read_bytes()
    assert blob.startswith(b"%PDF"), "must be a valid PDF"
    assert len(blob) > 5000, "three-part report should be a substantial document"

    text = _pdf_text(out)
    # Part 1 — tracker cover renders KPIs (win rate appears on the cover).
    assert "Win Rate" in text or "WIN RATE" in text.upper()
    # Part 2 + 3 — the merged-in reports, by their part headers.
    assert "Part 2 — Client Service Update" in text
    assert "Part 3 — Systems Audit" in text
    # Audit substance — QC status line renders.
    assert "QC status:" in text


def test_missing_data_file_returns_1(tmp_path):
    import gen_combined_pdf as GCP

    cfg = json.loads((ROOT / "config.json").read_text())
    cfg["paths"]["data"] = str(tmp_path / "nope.json")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    assert GCP.main(["--config", str(cfg_path), "--out", str(tmp_path / "x.pdf")]) == 1


def test_client_part_inherits_resolved_lane_filter(tmp_path, fixture_data):
    """The client part must reuse gen_client_email's buckets, so an
    unresolved-lane row can never surface in the client-copy section."""
    from datetime import datetime, timezone

    import gen_combined_pdf as GCP
    import gen_pdf as GP
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate
    data = dict(fixture_data)
    now = datetime.now(timezone.utc)
    data["requests"] = list(fixture_data.get("requests", [])) + [{
        "request_id": "unresolved-1",
        "status": "PENDING",
        "quoted": True,
        "lane": "Lane unresolved",
        "origin": "Oakland",
        "destination": None,
        "carrier_quoted": "HMM",
        "ol_rate": 1234.0,
        "request_date": now.date().isoformat(),
        "request_timestamp": now.isoformat(),
        "response_timestamp": now.isoformat(),
    }]

    styles = GP.make_styles()
    story = []
    GCP.build_client_part(story, styles, data, now=now)
    out = tmp_path / "client-part.pdf"
    SimpleDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.7 * inch, bottomMargin=0.55 * inch,
    ).build(story)
    assert "Lane unresolved" not in _pdf_text(out), (
        "client part must inherit gen_client_email's _lane_resolved filter"
    )


# ── daily.yml contract — one email, combined PDF, safe fallback ──────────

def _daily_yml():
    return (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")


def test_daily_workflow_builds_and_attaches_combined_pdf():
    text = _daily_yml()
    assert "gen_combined_pdf.py" in text
    assert "hilmar-combined.pdf" in text
    # Audit must be generated BEFORE the combined build so its content rides
    # the PDF.
    assert text.index("gen_improvements_report.py") < text.index("gen_combined_pdf.py")


def test_daily_workflow_keeps_three_email_fallback():
    """If the combined PDF fails to build, the fire must degrade to the old
    model (tracker PDF + separate audit email) — never lose the deliverable."""
    text = _daily_yml()
    assert "COMBINED_OK" in text
    assert "hilmar-report.pdf" in text, "fallback attachment must remain wired"
    assert "improvements-subject.txt" in text, "fallback audit email must remain wired"


def test_daily_workflow_client_email_step_untouched():
    """QC-065 boundary: Lonny's client email step still sends ONLY the client
    body, gated exactly as before — the combined PDF never rides it."""
    text = _daily_yml()
    step = text[text.index("Send the client-facing email"):text.index("Prove the fire shipped")]
    assert "client-email-body.html" in step
    assert "hilmar-combined.pdf" not in step, (
        "combined PDF (internal analytics) must never attach to the client email"
    )
