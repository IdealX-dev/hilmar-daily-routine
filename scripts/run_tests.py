"""
run_tests.py — Regression harness for the Hilmar rate desk pipeline.

Tests fall into three groups:
  1. Unit tests — pure functions in core.py (TEU parse, biz hours, status machine, etc.)
  2. Schema tests — golden fixture conforms to schema.json
  3. Pipeline smoke — QC → dashboard → PDF → email all produce non-empty outputs
     against the golden fixture (writes to a tmp copy so we don't clobber live data)

Exits 0 on pass, 1 on any failure. Designed to run before every production cycle.
  python3 scripts/run_tests.py
  python3 scripts/run_tests.py --verbose
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"

sys.path.insert(0, str(SCRIPTS))

# ── Results accumulator ───────────────────────────────────────────────────
results = []

def _test(name):
    """Decorator that runs the test *immediately* when defined."""
    def wrap(fn):
        try:
            fn()
            results.append((True, name, None))
            print(f"  ✅ {name}")
        except AssertionError as e:
            results.append((False, name, str(e)))
            print(f"  ❌ {name}")
            print(f"     → {e}")
        except Exception as e:
            results.append((False, name, f"{type(e).__name__}: {e}"))
            print(f"  ❌ {name} — crashed")
            traceback.print_exc()
        return fn
    return wrap


# ── 1. Core unit tests ────────────────────────────────────────────────────
def run_core_tests():
    import core
    print("\n── core.py unit tests ──")

    @_test("parse_teu handles 3×40'RF")
    def t1():
        count, teu = core.parse_teu("3×40'RF")
        assert count == 3 and teu == 6, f"got {count=} {teu=}"

    @_test("parse_teu handles 2x40HC")
    def t2():
        count, teu = core.parse_teu("2x40HC")
        assert count == 2 and teu == 4

    @_test("parse_teu handles empty/None")
    def t3():
        assert core.parse_teu(None) == (0, 0)
        assert core.parse_teu("") == (0, 0)

    @_test("biz_hours_between same-day Tue 9:00→12:30 = 3.5")
    def t4():
        start = datetime(2026, 4, 7, 13, 0, tzinfo=timezone.utc)   # 9:00 ET
        end = datetime(2026, 4, 7, 16, 30, tzinfo=timezone.utc)     # 12:30 ET
        got = core.biz_hours_between(start, end)
        assert got is not None and abs(got - 3.5) < 0.05, f"got {got}"

    @_test("biz_hours_between crosses weekend (Fri 4:30pm ET → Mon 9:00am ET = 1.5h)")
    def t5():
        start = datetime(2026, 4, 3, 20, 30, tzinfo=timezone.utc)   # Fri 4:30pm ET → 1.0h till 5:30pm close
        end = datetime(2026, 4, 6, 13, 0, tzinfo=timezone.utc)       # Mon 9:00am ET → 0.5h past 8:30 open
        got = core.biz_hours_between(start, end)
        assert got is not None and abs(got - 1.5) < 0.1, f"got {got}"

    @_test("is_lonny_send_reply detects 'Send please'")
    def t6():
        assert core.is_lonny_send_reply("Send please", is_reply=True) is True

    @_test("is_lonny_send_reply requires is_reply=True")
    def t6b():
        # A fresh request that starts with "Send" should NOT count as acceptance
        assert core.is_lonny_send_reply("Send", is_reply=False) is False

    @_test("is_lonny_send_reply rejects 'send both cutoffs'")
    def t7():
        assert core.is_lonny_send_reply("Can you send both cutoffs?", is_reply=True) is False

    @_test("request_id is stable for same inputs")
    def t8():
        a = core.request_id("CONV-1", "2026-04-10T15:00:00Z", "Shanghai, CN")
        b = core.request_id("CONV-1", "2026-04-10T15:00:00Z", "Shanghai, CN")
        assert a == b and len(a) >= 10

    @_test("decide_status WIN when has_send=True and mdolx_ref")
    def t9():
        d = core.decide_status(has_send=True, mdolx_ref="MDX-1", response_timestamp="2026-04-10T15:00:00Z", quoted=True, etd_fit_days=0)
        assert d.status == "WIN", f"got {d.status}"

    @_test("decide_status LOSS/NO_RESPONSE when not quoted after window")
    def t10():
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        d = core.decide_status(has_send=False, mdolx_ref=None, response_timestamp=None, quoted=False,
                               etd_fit_days=None, now=now)
        assert d.status == "LOSS" and d.loss_reason == "NO_RESPONSE", f"got {d.status}/{d.loss_reason}"

    @_test("etd_fit_days positive when offered is later than requested")
    def t11():
        got = core.etd_fit_days("2026-04-10", "2026-04-13")
        assert got == 3, f"got {got}"

    @_test("aggregate_summary reproduces fixture win_rate")
    def t12():
        fx = json.loads((FIXTURES / "golden_day.json").read_text(encoding="utf-8"))
        s = core.aggregate_summary(fx["requests"])
        # Decided = wins + quoted_lost + not_quoted = 2 + 1 + 1 = 4; wins=2 → 50%
        assert abs(s["win_rate"] - 50.0) < 0.5, f"got {s['win_rate']}"
        assert s["wins"] == 2
        assert s["pending_hilmar"] == 1

    @_test("load_config auto-heals stale session paths")
    def t13():
        # Simulates the real failure mode: config.json on disk has paths.root
        # pointing at a previous Cowork session that no longer exists. The fix
        # rewrites paths in memory using the live config file location — no
        # disk mutation. If this regresses, every daily run will fail again
        # with PermissionError on a non-existent /sessions/<old-id>/... path.
        tmp = Path(tempfile.mkdtemp(prefix="hilmar_pathheal_"))
        try:
            stale_root = "/sessions/does-not-exist-xyz-12345/mnt/PROJECT HILMAR"
            cfg_src = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            cfg_src["paths"] = {
                "root": stale_root,
                "data": stale_root + "/tracking-data-v2.json",
                "schema": stale_root + "/schema.json",
                "backups": stale_root + "/data-backups",
                "scripts": stale_root + "/scripts",
                "reports": stale_root + "/reports",
                "history": stale_root + "/reports/history",
                "dashboard": stale_root + "/reports/hilmar-dashboard.html",
                "pdf": stale_root + "/reports/hilmar-report.pdf",
                "carrier_scorecards_dir": stale_root + "/reports/carrier-scorecards",
                "email_body": stale_root + "/reports/email-body.html",
                "qc_result": stale_root + "/reports/qc-result.json",
                "escalation_log": stale_root + "/escalation-log.json",
            }
            cfg_path = tmp / "config.json"
            cfg_path.write_text(json.dumps(cfg_src, indent=2), encoding="utf-8")
            healed = core.load_config(cfg_path)
            # Compare via Path.resolve() so Windows 8.3 short names (MICHAE~1)
            # don't fail equality vs the resolved long form.
            tmp_resolved = str(Path(tmp).resolve())
            healed_root = str(Path(healed["paths"]["root"]).resolve())
            assert healed_root == tmp_resolved, \
                f"root should heal to {tmp_resolved}, got {healed_root}"
            for k, v in healed["paths"].items():
                assert not v.startswith(stale_root), \
                    f"paths[{k}] still has stale prefix: {v}"
                v_resolved = str(Path(v).resolve())
                assert v_resolved.startswith(tmp_resolved), \
                    f"paths[{k}] not under live root: {v_resolved}"
            # Disk file must NOT be mutated.
            on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert on_disk["paths"]["root"] == stale_root, \
                "load_config mutated config.json on disk — must be in-memory only"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @_test("load_config no-op when paths.root already exists on disk")
    def t14():
        # If the configured root resolves to a real directory (e.g. test
        # harness using a tmp dir), load_config must NOT rewrite paths —
        # the caller knows what they're doing.
        tmp = Path(tempfile.mkdtemp(prefix="hilmar_pathnoop_"))
        try:
            cfg_src = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            cfg_src["paths"] = {
                "root": str(tmp),
                "data": str(tmp / "tracking-data-v2.json"),
                "schema": str(tmp / "schema.json"),
                "backups": str(tmp / "data-backups"),
                "scripts": str(tmp / "scripts"),
                "reports": str(tmp / "reports"),
                "history": str(tmp / "reports" / "history"),
                "dashboard": str(tmp / "reports" / "hilmar-dashboard.html"),
                "pdf": str(tmp / "reports" / "hilmar-report.pdf"),
                "carrier_scorecards_dir": str(tmp / "reports" / "carrier-scorecards"),
                "email_body": str(tmp / "reports" / "email-body.html"),
                "qc_result": str(tmp / "reports" / "qc-result.json"),
                "escalation_log": str(tmp / "escalation-log.json"),
            }
            # NOTE: cfg lives in a *different* dir than its declared root.
            other_dir = Path(tempfile.mkdtemp(prefix="hilmar_cfgdir_"))
            try:
                cfg_path = other_dir / "config.json"
                cfg_path.write_text(json.dumps(cfg_src, indent=2), encoding="utf-8")
                healed = core.load_config(cfg_path)
                # tmp exists → no rewrite, even though config file is elsewhere.
                assert healed["paths"]["root"] == str(tmp), \
                    f"existing root should be preserved, got {healed['paths']['root']}"
            finally:
                shutil.rmtree(other_dir, ignore_errors=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @_test("load_config heals when stale_root raises PermissionError on is_dir")
    def t15():
        # Real-world failure mode: stale_root points at /sessions/<other-session>/...
        # which exists in /sessions but is unreadable to this user. Path.is_dir()
        # raises PermissionError, NOT returns False. Heal must catch and treat
        # as stale. If this regresses, daily run silently dies on backup.py.
        import unittest.mock as mock

        tmp = Path(tempfile.mkdtemp(prefix="hilmar_pathperm_"))
        try:
            stale_root = "/some/locked/foreign/session/PROJECT HILMAR"
            cfg_src = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            cfg_src["paths"] = {
                "root": stale_root,
                "data": stale_root + "/tracking-data-v2.json",
            }
            cfg_path = tmp / "config.json"
            cfg_path.write_text(json.dumps(cfg_src, indent=2), encoding="utf-8")

            real_is_dir = Path.is_dir
            def fake_is_dir(self):
                if str(self) == stale_root:
                    raise PermissionError(13, "Permission denied", str(self))
                return real_is_dir(self)

            with mock.patch.object(Path, "is_dir", fake_is_dir):
                healed = core.load_config(cfg_path)
            # Resolve both sides — Windows tempdirs come back as 8.3 short names.
            tmp_resolved = str(Path(tmp).resolve())
            healed_root = str(Path(healed["paths"]["root"]).resolve())
            healed_data = str(Path(healed["paths"]["data"]).resolve())
            assert healed_root == tmp_resolved, \
                f"root should heal to {tmp_resolved}, got {healed_root}"
            assert healed_data.startswith(tmp_resolved), \
                f"data path not healed: {healed_data}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ── 2. Schema compliance ──────────────────────────────────────────────────
def run_schema_tests():
    print("\n── schema.json compliance ──")

    @_test("golden fixture has all required top-level keys")
    def t1():
        schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
        fx = json.loads((FIXTURES / "golden_day.json").read_text(encoding="utf-8"))
        for key in schema["required"]:
            assert key in fx, f"missing top-level key: {key}"

    @_test("every request has required per-request fields")
    def t2():
        schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
        fx = json.loads((FIXTURES / "golden_day.json").read_text(encoding="utf-8"))
        req_fields = schema["definitions"]["request"]["required"]
        for i, r in enumerate(fx["requests"]):
            for f in req_fields:
                assert f in r, f"request[{i}] missing {f}"

    @_test("status values are from enum")
    def t3():
        fx = json.loads((FIXTURES / "golden_day.json").read_text(encoding="utf-8"))
        for r in fx["requests"]:
            assert r["status"] in ("WIN", "LOSS", "PENDING"), r["status"]


# ── 3. Pipeline smoke (against golden fixture copy) ───────────────────────
def run_pipeline_smoke(verbose=False):
    print("\n── pipeline smoke (QC → dashboard → PDF → email) ──")
    tmp = Path(tempfile.mkdtemp(prefix="hilmar_test_"))
    try:
        # Clone project into tmp
        (tmp / "reports").mkdir(parents=True, exist_ok=True)
        (tmp / "data-backups").mkdir(parents=True, exist_ok=True)
        # Copy fixture → data
        shutil.copy2(FIXTURES / "golden_day.json", tmp / "tracking-data-v2.json")
        shutil.copy2(ROOT / "schema.json", tmp / "schema.json")
        # Build a test-only config
        cfg_src = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        cfg_src["paths"] = {
            "root": str(tmp),
            "data": str(tmp / "tracking-data-v2.json"),
            "schema": str(tmp / "schema.json"),
            "backups": str(tmp / "data-backups"),
            "scripts": str(SCRIPTS),
            "reports": str(tmp / "reports"),
            "history": str(tmp / "reports" / "history"),
            "dashboard": str(tmp / "reports" / "hilmar-dashboard.html"),
            "pdf": str(tmp / "reports" / "hilmar-report.pdf"),
            "carrier_scorecards_dir": str(tmp / "reports" / "carrier-scorecards"),
            "email_body": str(tmp / "reports" / "email-body.html"),
            "qc_result": str(tmp / "reports" / "qc-result.json"),
            "escalation_log": str(tmp / "escalation-log.json"),
        }
        cfg_path = tmp / "config.json"
        cfg_path.write_text(json.dumps(cfg_src, indent=2), encoding="utf-8")

        def run_step(label, script):
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / script), "--config", str(cfg_path)],
                capture_output=True, text=True, timeout=120,
            )
            if verbose or r.returncode != 0:
                if r.stdout.strip():
                    print(f"    [{label} stdout] {r.stdout.strip()[:400]}")
                if r.stderr.strip():
                    print(f"    [{label} stderr] {r.stderr.strip()[:400]}")
            return r

        @_test("qc_selfheal runs clean on golden fixture")
        def t1():
            r = run_step("qc", "qc_selfheal.py")
            assert r.returncode == 0, f"qc exited {r.returncode}"
            assert (tmp / "reports" / "qc-result.json").exists()

        @_test("gen_dashboard produces non-empty HTML")
        def t2():
            r = run_step("dashboard", "gen_dashboard.py")
            assert r.returncode == 0, f"dashboard exited {r.returncode}"
            out = tmp / "reports" / "hilmar-dashboard.html"
            assert out.exists() and out.stat().st_size > 5000, f"size {out.stat().st_size if out.exists() else 0}"

        @_test("gen_pdf produces non-empty PDF")
        def t3():
            r = run_step("pdf", "gen_pdf.py")
            assert r.returncode == 0, f"pdf exited {r.returncode}"
            out = tmp / "reports" / "hilmar-report.pdf"
            assert out.exists() and out.stat().st_size > 1000, f"size {out.stat().st_size if out.exists() else 0}"

        @_test("gen_carrier_scorecard_pdf produces at least one scorecard")
        def t4():
            r = run_step("scorecard", "gen_carrier_scorecard_pdf.py")
            assert r.returncode == 0, f"scorecard exited {r.returncode}"
            out_dir = tmp / "reports" / "carrier-scorecards"
            assert out_dir.exists(), "scorecards dir missing"
            pdfs = list(out_dir.glob("*.pdf"))
            assert len(pdfs) >= 1, "no scorecards generated"

        @_test("gen_email produces non-empty HTML body")
        def t5():
            r = run_step("email", "gen_email.py")
            assert r.returncode == 0, f"email exited {r.returncode}"
            out = tmp / "reports" / "email-body.html"
            assert out.exists() and out.stat().st_size > 200, f"size {out.stat().st_size if out.exists() else 0}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 4. Code-quality regressions ──────────────────────────────────────────
def run_code_quality_tests():
    """Catch the failure mode that B5 fixed in patch_carriers.py 2026-05-05:
    a script reads config.json directly via raw json.loads and then dereferences
    cfg["paths"], bypassing core._heal_session_paths. This is invisible on Linux
    (where the on-disk paths happen to match the runtime root) but explodes on
    Windows / on a fresh laptop / in any session whose root differs from
    what's serialized in config.json. Fail loudly here so it doesn't ship.
    """
    import re
    print("\n── code-quality regressions ──")

    raw_load = re.compile(r"json\s*\.\s*loads\s*\(\s*\(?\s*ROOT\s*/\s*['\"]config\.json['\"]")
    paths_use = re.compile(r"cfg\s*\[\s*['\"]paths['\"]\s*\]")
    EXEMPT = {"run_tests.py"}  # this harness itself simulates raw loads in tmp
    SCRIPT_FILES = sorted(
        p for p in SCRIPTS.glob("*.py")
        if p.name not in EXEMPT and not p.name.startswith("_")
    )

    @_test("no script bypasses core.load_config when reading cfg['paths']")
    def t_bypass():
        offenders = []
        for p in SCRIPT_FILES:
            src = p.read_text(encoding="utf-8")
            if raw_load.search(src) and paths_use.search(src):
                offenders.append(p.name)
        assert not offenders, (
            f"these scripts read config.json raw AND deref cfg['paths'] — "
            f"will fail on any session where on-disk paths.root != live cwd: {offenders}. "
            f"Use `import core as C; cfg = C.load_config()` instead."
        )


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Hilmar pipeline regression tests")
    ap.add_argument("--verbose", action="store_true", help="show subprocess stdout/stderr")
    args = ap.parse_args()

    print("╭─ Hilmar pipeline regression tests ──────────────────╮")
    run_core_tests()
    run_schema_tests()
    run_pipeline_smoke(verbose=args.verbose)
    run_code_quality_tests()
    print("\n╰─ Summary ──────────────────────────────────────────╯")
    passed = sum(1 for r in results if r[0])
    failed = sum(1 for r in results if not r[0])
    print(f"  {passed}/{passed + failed} passed, {failed} failed")
    if failed == 0:
        print("  ✅ All green — safe to run pipeline.")
        sys.exit(0)
    else:
        print("  ❌ Failures detected — fix before running pipeline.")
        sys.exit(1)


if __name__ == "__main__":
    main()
