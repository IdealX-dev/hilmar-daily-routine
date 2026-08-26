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


def _reachable_from_build_body():
    """Every gen_email function build_body can actually reach.

    A renderer that is merely DEFINED tells you nothing — that is the hole
    this closes. On 2026-08-26 six sections were removed from build_body,
    their eight renderers stayed defined and uncalled, the old
    `hasattr(gen_email, fn)` guard stayed green, and the manual attached to
    every daily email went on describing sections nobody could find.

    Walks the call graph by NAME (ast.Name and ast.Attribute), which is
    coarse enough to include a function referenced but not called — the
    safe direction for a guard that must not fail on healthy code.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(gen_email))
    defs = {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _names(fn):
        out = set()
        for n in ast.walk(defs[fn]):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.Attribute):
                out.add(n.attr)
        return out & set(defs)

    seen, stack = set(), ["build_body"]
    while stack:
        fn = stack.pop()
        if fn in seen:
            continue
        seen.add(fn)
        stack.extend(_names(fn) - seen)
    return seen, {f for f in defs if f.endswith("_html")}


def test_every_cataloged_section_is_reachable_from_build_body():
    reachable, _all_html = _reachable_from_build_body()
    for title, fn_name, _desc in gen_manual.EMAIL_SECTIONS:
        assert hasattr(gen_email, fn_name), (
            f"manual catalogs '{title}' via gen_email.{fn_name}, which no "
            f"longer exists — update gen_manual.EMAIL_SECTIONS")
        assert fn_name in reachable, (
            f"manual catalogs '{title}' via gen_email.{fn_name}, which "
            f"build_body cannot reach — the section is described to every "
            f"staff member and appears in nobody's email. Either wire it "
            f"back into build_body or move the entry to "
            f"gen_manual.MOVED_TO_DASHBOARD.")


def test_moved_out_sections_really_are_out_of_the_email():
    # The other direction: a section listed as "find it in the dashboard"
    # must not also be in the email, or the manual sends readers away from
    # something sitting in front of them.
    reachable, _ = _reachable_from_build_body()
    cataloged = {fn for _t, fn, _d in gen_manual.EMAIL_SECTIONS}
    for title, _home, _desc in gen_manual.MOVED_TO_DASHBOARD:
        assert title not in {t for t, _f, _d in gen_manual.EMAIL_SECTIONS}, (
            f"'{title}' is listed as moved out AND as an email section")
    # And nothing build_body renders is undescribed (the direction the old
    # guard had). Read the AST, not the source text: build_body carries a
    # comment block NAMING the six moved-out renderers, and a regex over the
    # source counts those names as calls — which is part of why the old
    # guard never noticed they had stopped being called.
    import ast
    import inspect
    import textwrap
    body_ast = ast.parse(textwrap.dedent(inspect.getsource(gen_email.build_body)))
    invoked = set()
    for n in ast.walk(body_ast):
        if isinstance(n, ast.Call):
            f = n.func
            nm = getattr(f, "id", None) or getattr(f, "attr", None)
            if nm and nm.endswith("_html"):
                invoked.add(nm)
    exempt = {"_header_html"}          # chrome, not a section
    missing = {fn for fn in invoked
               if fn not in cataloged and fn not in exempt}
    assert not missing, (
        f"gen_email.build_body renders sections the manual doesn't describe: "
        f"{sorted(missing)} — add them to gen_manual.EMAIL_SECTIONS")
    assert reachable, "call-graph walk found nothing — the guard is broken"


def test_moved_out_sections_are_produced_somewhere():
    """A section the manual sends readers to the dashboard for must be IN
    the dashboard. This is the check that was missing: Loss-Reason Mix was
    moved out of the email on the claim that the dashboard already had it,
    and for a few hours it was rendered by nothing at all.
    """
    dash_src = Path(gen_dashboard.__file__).read_text(encoding="utf-8")
    pdf_src = (Path(gen_dashboard.__file__).parent / "gen_pdf.py").read_text(
        encoding="utf-8")
    # Each moved-out section names the renderer/heading that proves it ships.
    proof = {
        "Week over Week":        ["Week-over-Week"],
        "Carrier Performance":   ["Carrier Performance"],
        "Volume by Trade Region": ["Volume by Trade Region", "trade_region"],
        "Top Winning Lanes":     ["Top Winning Lanes"],
        "Top Losing Lanes":      ["Top Losing Lanes"],
        "Loss-Reason Mix":       ["_loss_reason_mix_html"],
    }
    for title, home, _desc in gen_manual.MOVED_TO_DASHBOARD:
        needles = proof.get(title)
        assert needles, (
            f"'{title}' is listed in MOVED_TO_DASHBOARD with no proof that "
            f"anything renders it — add one to this test's `proof` map")
        hay = dash_src if home == "gen_dashboard.py" else pdf_src
        assert any(n in hay for n in needles), (
            f"the manual tells readers '{title}' is in {home}, and {home} "
            f"does not mention {needles}. Either restore the section there "
            f"or stop telling people where to find it.")


def test_the_reachability_guard_catches_a_planted_orphan():
    # The guard must fail on a section that exists but is unreachable —
    # otherwise it is the same guard that already missed this once.
    reachable, all_html = _reachable_from_build_body()
    orphans = all_html - reachable
    assert orphans, (
        "expected at least one defined-but-unreachable _html renderer to "
        "prove the walk discriminates; found none")
    assert "build_body" in reachable and "_today_block_html" in reachable


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
