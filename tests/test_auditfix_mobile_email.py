"""Mobile rendering of the two emails (Michael 2026-07-01: "reading email on
phone is poorly formatted").

Both emails are sized for desktop Outlook (1040px/800px containers, 7-11
column fixed tables), so a phone either crushed the columns or scaled the
whole email to unreadable. The fix is <style> @media overrides that phone
clients (iOS Mail, Gmail app) honor and desktop Outlook's Word engine ignores
entirely:
  - .hx-wrap / .hx-pad  full-bleed container + ONE horizontal scroll surface
  - td.hx-kpi           KPI tiles stack 2-up instead of 4-5 crushed across
  - table.hx-data       data tables KEEP readable column geometry (min-width)
                        and scroll sideways instead of squishing

These assertions are static-source (like test_auditfix_gen_email.py) plus one
rendered-output check per email, so they fail if the classes or the @media
block are ever dropped.
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

GEN_EMAIL = ROOT / "scripts" / "gen_email.py"
GEN_IMPROVE = ROOT / "scripts" / "gen_improvements_report.py"


# ── client email (gen_email.py) ───────────────────────────────────────────
def test_client_email_has_mobile_media_block():
    src = GEN_EMAIL.read_text(encoding="utf-8")
    assert "@media only screen and (max-width:640px)" in src
    # The four mobile hooks all exist in the style block.
    for hook in (".hx-wrap", ".hx-pad", "td.hx-kpi", "table.hx-data"):
        assert hook in src, f"mobile hook {hook} missing from gen_email"


def test_client_email_kpi_tiles_stack_on_mobile():
    """KPI <td> cells carry hx-kpi so the media rule can stack them 2-up."""
    src = GEN_EMAIL.read_text(encoding="utf-8")
    assert '<td class="hx-kpi"' in src


def test_client_email_data_tables_are_classed():
    """Both shared daily-table constants AND the section tables carry hx-data
    so they keep readable column geometry on a phone (min-width + sideways
    scroll) instead of crushing. The KPI-tile tables are deliberately NOT
    classed — stacking handles them and a min-width would fight it."""
    src = GEN_EMAIL.read_text(encoding="utf-8")
    assert src.count('class="hx-data"') >= 10, (
        "expected the 2 shared table constants + ~8 section tables classed"
    )
    # KPI rows stay unclassed (they stack instead).
    assert 'class="hx-data" style="width:100%;border-collapse:collapse;margin-bottom:16px"' not in src
    assert 'class="hx-data" style="width:100%;border-collapse:collapse;margin-bottom:20px">\n  <tr>\n    ' not in src


def test_client_email_rendered_kpi_block_carries_classes():
    import gen_email as GE
    summary = {"total_entries": 3, "wins": 1, "quoted_lost": 1, "not_quoted": 0,
               "pending_hilmar": 1, "win_rate": 50.0, "quote_rate": 90.0,
               "teu_won": 2, "teu_quoted_lost": 1, "teu_not_quoted": 0,
               "teu_requested": 5, "teu_pending": 2, "turnaround_avg_biz_hours": 5.0}
    requests = [{"status": "PENDING", "quoted": True, "request_date": "2026-07-01",
                 "destination": "Osaka", "teu_requested": 2}]
    html = GE._kpi_block_html(summary, requests, report_date=date(2026, 7, 1))
    assert 'class="hx-kpi"' in html


def test_client_email_header_wraps_with_mobile_classes():
    import gen_email as GE
    html = GE._header_html("Jul 1", "Jun 1 – Jul 1", "Jul 1, 7:00 PM ET")
    assert "@media only screen" in html
    assert 'class="hx-wrap"' in html
    assert 'class="hx-pad"' in html
    # Desktop rendering unchanged: the fixed container width is still there.
    assert "max-width:1040px" in html


# ── audit email (gen_improvements_report.py) ──────────────────────────────
def test_audit_email_has_mobile_media_block_in_head():
    src = GEN_IMPROVE.read_text(encoding="utf-8")
    assert "@media only screen and (max-width:640px)" in src
    assert 'name="viewport"' in src
    assert 'class="hx-wrap"' in src
    assert 'class="hx-data"' in src   # the Sentry table


def test_audit_email_rate_intel_tables_classed_at_embed(monkeypatch, tmp_path):
    """The rate-intel section is a prebuilt HTML file; its tables get the
    hx-data class at embed time so the mobile rules apply to them too."""
    import gen_improvements_report as GI
    (tmp_path / "rate-intelligence-section.html").write_text(
        '<h2>rates</h2><table style="width:100%">x</table>', encoding="utf-8")
    monkeypatch.setattr(GI, "REPORTS", tmp_path)
    out = GI._rate_intel_section_inline()
    assert '<table class="hx-data" style="width:100%">' in out


def test_audit_email_rate_intel_missing_file_is_empty(monkeypatch, tmp_path):
    import gen_improvements_report as GI
    monkeypatch.setattr(GI, "REPORTS", tmp_path)
    assert GI._rate_intel_section_inline() == ""
