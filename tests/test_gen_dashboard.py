"""Smoke/contract test for scripts/gen_dashboard.py (audit finding [8]).

gen_dashboard is Step 11 — the interactive HTML dashboard emailed daily — yet
no test imported it. This asserts render() produces valid, non-empty HTML on the
golden fixture and surfaces the headline KPI values, so an output regression
can't ship green.
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


def test_render_returns_nonempty_html(cfg, fixture_data):
    import gen_dashboard as GD
    html = GD.render(cfg, fixture_data)
    assert isinstance(html, str)
    assert len(html) > 5000
    assert "<html" in html.lower()
    assert "</html>" in html.lower()


def test_render_surfaces_client_name(cfg, fixture_data):
    import gen_dashboard as GD
    html = GD.render(cfg, fixture_data)
    client = fixture_data.get("client") or cfg["client"]["name"]
    assert client in html


def test_render_surfaces_a_kpi_value(cfg, fixture_data):
    """A headline KPI from the fixture must appear in the output, so an empty
    or value-dropping render is caught (not just a well-formed empty shell).
    The dashboard is a standalone browser page, so inline data: URIs for the
    logo are expected here — that is NOT the QC-042 email-only constraint."""
    import gen_dashboard as GD
    html = GD.render(cfg, fixture_data)
    summary = fixture_data.get("summary", {})
    wins = summary.get("wins")
    assert wins is not None
    assert str(wins) in html


def test_render_is_deterministic(cfg, fixture_data):
    import gen_dashboard as GD
    a = GD.render(cfg, fixture_data)
    b = GD.render(cfg, fixture_data)
    assert a == b
