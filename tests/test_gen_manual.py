"""gen_manual.py — the auto-updated USER manual (Michael 2026-07-10:
"constantly updated instruction manual for users").

Two properties are pinned:
  1. LIVE VALUES — the manual is rebuilt from config.json every fire, so
     config changes (client gate, thresholds, distribution size) must show
     up in the rendered HTML without code changes.
  2. DRIFT GUARDS — the manual's section/tab catalogs must track the real
     renderers. Remove or rename a section in gen_email.py / a tab in
     gen_dashboard.py without updating the manual and these tests go red.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_dashboard  # noqa: E402
import gen_email  # noqa: E402
import gen_manual  # noqa: E402


def _cfg(**client_report_over):
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["client_report"] = {**cfg.get("client_report", {}), **client_report_over}
    return cfg


def test_manual_renders_every_cataloged_section():
    html = gen_manual.build_manual(_cfg())
    assert len(html) > 5_000
    for title, _fn, _desc in gen_manual.EMAIL_SECTIONS:
        assert title in html, f"email section missing from manual: {title}"
    for tab, _desc in gen_manual.DASHBOARD_TABS:
        assert tab in html, f"dashboard tab missing from manual: {tab}"
    for term, _d in gen_manual.STATUS_GLOSSARY + gen_manual.METRIC_DEFINITIONS:
        assert gen_manual._esc(term.split(" (")[0]) in html


def test_live_config_values_propagate():
    cfg = _cfg(enabled=False)
    html = gen_manual.build_manual(cfg)
    assert "Lonny receives NOTHING" in html
    html_on = gen_manual.build_manual(_cfg(enabled=True))
    assert "Lonny receives NOTHING" not in html_on
    assert "ON — Lonny Upfold receives" in html_on
    # Threshold values come from config, not prose.
    rules = cfg["rules"]
    assert str(rules["pending_aging_hours"]) in html
    assert str(rules["rate_trend_threshold_pct"]) in html
    assert str(len(cfg["distribution"]["full_list"])) + "-person" in html


def test_email_section_catalog_tracks_real_renderers():
    # Every cataloged section must name a renderer that still exists in
    # gen_email — the drift guard that keeps the manual honest.
    for title, fn_name, _desc in gen_manual.EMAIL_SECTIONS:
        assert hasattr(gen_email, fn_name), (
            f"manual catalogs '{title}' via gen_email.{fn_name}, which no "
            f"longer exists — update gen_manual.EMAIL_SECTIONS")
    # And the catalog must COVER the email: every section renderer invoked in
    # build_body appears in the catalog (footer/header/kpi variants aside).
    import inspect
    body_src = inspect.getsource(gen_email.build_body)
    cataloged = {fn for _t, fn, _d in gen_manual.EMAIL_SECTIONS}
    import re
    invoked = set(re.findall(r"_(?:[a-z_]+)_html", body_src))
    invoked = {f"_{m}" if not m.startswith("_") else m for m in invoked}
    exempt = {"_header_html", "_kpi_block_html"}  # kpi cataloged, header is chrome
    missing = {fn for fn in invoked
               if fn not in cataloged and fn not in exempt
               and fn.endswith("_html")}
    assert not missing, (
        f"gen_email.build_body renders sections the manual doesn't describe: "
        f"{sorted(missing)} — add them to gen_manual.EMAIL_SECTIONS")


def test_dashboard_tab_catalog_tracks_gen_dashboard():
    src = Path(gen_dashboard.__file__).read_text(encoding="utf-8")
    for tab, _desc in gen_manual.DASHBOARD_TABS:
        assert tab in src, (
            f"manual catalogs dashboard tab '{tab}' which gen_dashboard.py "
            f"no longer mentions — update gen_manual.DASHBOARD_TABS")


def test_cli_writes_the_artifact(tmp_path):
    out = tmp_path / "user-manual.html"
    assert gen_manual.main(["--out", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    assert "<title>Hilmar Daily Tracker — User Manual</title>" in html
    assert len(html) < 200_000  # stays a trivial attachment vs the 3 MB cap
