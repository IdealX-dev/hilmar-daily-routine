"""Acceptance tests for the 2026-06-02 audit's four KPI-critical fixes.

Each test locks one of the bugs surfaced by the 2026-06-02 full audit
(see docs/audits/2026-06-02/00-synthesis.md):

  C-1 — win_rate denominator must EXCLUDE NQ (CLAUDE.md §6)
  C-2 — lonny_covered=True rows must set quoted=True so they're Q&L not NQ
  C-3 — schema.json loss_reason enum must include UNDIFFERENTIATED + COVERED + DRAFT_ONLY
  C-5 — QC-017 carrier-concentration must count STRICT-form Q&L rows, not just WIN+LOSS

Tests target both trees where applicable (cross-tree parity is the
standing rule per CLAUDE.md §2).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hilmar import core as hilmar_core  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


scripts_core = _load(SCRIPTS / "core.py", "scripts_core_kpi_tests")


# ── C-1: win_rate denominator excludes NQ ───────────────────────────────
#
# IMPORTANT shape note: scripts/core.aggregate_summary buckets LEGACY rows
# (status="LOSS" + quoted disambiguator). src/hilmar/core.aggregate_summary
# buckets STRICT rows (status in WIN/Q&L/NQ/PENDING). Production data is
# LEGACY (per CLAUDE.md §6). Each parametrised test uses the form the
# tree natively understands. Mixed-form data is a separate concern out
# of scope for this PR.

@pytest.fixture(params=[scripts_core, hilmar_core], ids=["scripts", "hilmar"])
def core(request):
    return request.param


@pytest.fixture
def _rows_factory(core):
    """Return shape-correct row factories for the given tree."""
    if core is scripts_core:
        return _legacy_rows()
    return _strict_rows()


def _legacy_rows():
    return {
        "win":     lambda: {"status": "WIN", "quoted": True},
        "ql":      lambda: {"status": "LOSS", "loss_reason": "PRICE", "quoted": True},
        "nq":      lambda: {"status": "LOSS", "loss_reason": "NO_RESPONSE", "quoted": False},
        "pending": lambda: {"status": "PENDING", "quoted": False},
    }


def _strict_rows():
    return {
        "win":     lambda: {"status": "WIN", "quoted": True},
        "ql":      lambda: {"status": "Q&L", "loss_reason": "PRICE", "quoted": True},
        "nq":      lambda: {"status": "NQ", "loss_reason": "NO_RESPONSE", "quoted": False},
        "pending": lambda: {"status": "PENDING", "quoted": False},
    }


def test_win_rate_excludes_nq_from_denominator(core, _rows_factory):
    """C-1: Wins / (Wins + Q&L). NQ is "no contest", not a loss."""
    R = _rows_factory
    rows = [R["win"](), R["win"](), R["ql"](),
            R["nq"](), R["nq"](), R["nq"]()]
    s = core.aggregate_summary(rows)
    # 2 wins, 1 Q&L, 3 NQ → win_rate = 2/(2+1) = 66.7%
    assert abs(s["win_rate"] - 66.7) < 0.5, (
        f"win_rate={s['win_rate']} — NQ likely back in denominator. "
        f"Should be 2/(2+1)=66.7, NOT 2/(2+1+3)=33.3."
    )
    assert s["wins"] == 2
    assert s["quoted_lost"] == 1
    assert s["not_quoted"] == 3


def test_win_rate_zero_when_only_nq_rows(core, _rows_factory):
    """All NQ → no contest happened → win_rate is 0 (denom=0)."""
    R = _rows_factory
    rows = [R["nq"](), R["nq"](), R["nq"]()]
    s = core.aggregate_summary(rows)
    assert s["win_rate"] == 0.0
    assert s["not_quoted"] == 3


def test_win_rate_100_when_only_wins(core, _rows_factory):
    R = _rows_factory
    rows = [R["win"](), R["win"](), R["nq"]()]
    s = core.aggregate_summary(rows)
    # 2 wins, 0 Q&L, 1 NQ → 2/(2+0) = 100%
    assert abs(s["win_rate"] - 100.0) < 0.01


def test_win_rate_zero_division_safe_with_no_rows(core):
    s = core.aggregate_summary([])
    assert s["win_rate"] == 0.0


def test_win_rate_pending_does_not_inflate_denominator(core, _rows_factory):
    """PENDING rows are in-flight — they must not count in either
    numerator OR denominator. Quoted but not yet decided."""
    R = _rows_factory
    rows = [R["win"](), R["ql"](), R["pending"](), R["pending"]()]
    s = core.aggregate_summary(rows)
    # 1 win, 1 Q&L, 2 pending → 1/(1+1) = 50%
    assert abs(s["win_rate"] - 50.0) < 0.5


def test_trade_region_win_rate_excludes_nq_scripts_only():
    """The per-trade-region win_rate aggregator (aggregate_trade_regions)
    is scripts-only. Same C-1 fix applies — NQ excluded."""
    rows = [
        {"status": "WIN", "destination": "Yokohama", "carrier_won": "MSC",
         "teu_won": 2, "teu_requested": 2, "request_id": "r1"},
        {"status": "LOSS", "loss_reason": "PRICE", "quoted": True,
         "destination": "Yokohama", "teu_requested": 2, "request_id": "r2"},
        {"status": "LOSS", "loss_reason": "NO_RESPONSE", "quoted": False,
         "destination": "Yokohama", "teu_requested": 2, "request_id": "r3"},
    ]
    out = scripts_core.aggregate_trade_regions(rows)
    region = "Far East"
    if region in out:
        # 1 win, 1 Q&L, 1 NQ → 1/(1+1) = 50%
        assert abs(out[region]["win_rate"] - 50.0) < 0.5, (
            f"trade-region win_rate={out[region]['win_rate']} — NQ likely "
            f"back in denominator."
        )


# ── C-3: schema.json includes the post-PR-#21 loss reasons ─────────────

def _schema_loss_reason_enum():
    """Helper: extract the loss_reason enum from schema.json. Located
    under `definitions.request.properties.loss_reason.enum`."""
    schema = json.loads((ROOT / "schema.json").read_text())
    return schema["definitions"]["request"]["properties"]["loss_reason"]["enum"]


def test_schema_loss_reason_enum_includes_undifferentiated():
    """PR #21 ships UNDIFFERENTIATED writes. Schema must allow it."""
    enum = _schema_loss_reason_enum()
    for required in ("UNDIFFERENTIATED", "COVERED", "DRAFT_ONLY"):
        assert required in enum, (
            f"schema.json loss_reason enum missing {required!r}. "
            f"This is the post-PR-#21 blocker — any Q&L row with the new "
            f"value gets rejected by downstream validators."
        )


def test_schema_loss_reason_enum_matches_canonical_set():
    """Every value in LOSS_REASONS must appear in the schema enum (the
    schema MAY also include None for nullable). Catches future drift in
    EITHER direction."""
    enum_set = {v for v in _schema_loss_reason_enum() if v is not None}
    canonical = set(hilmar_core.LOSS_REASONS)
    missing_from_schema = canonical - enum_set
    extra_in_schema = enum_set - canonical
    assert not missing_from_schema, (
        f"LOSS_REASONS values not in schema enum: {missing_from_schema}"
    )
    assert not extra_in_schema, (
        f"schema enum has values not in LOSS_REASONS: {extra_in_schema}"
    )


# ── C-2: lonny_covered honor sets quoted=True ──────────────────────────
#
# Tests target scripts/qc_selfheal.py:phase_3_entries indirectly via the
# behavior contract: after the heal, a lonny_covered=True row has
# quoted=True, status="LOSS", loss_reason="COVERED".

def test_lonny_covered_heal_sets_quoted_true():
    """C-2: A row with lonny_covered=True must end with quoted=True so
    it bucketed as Q&L (real lost contest) not NQ. Before this fix,
    COVERED rows with no extracted rate landed in NQ and were excluded
    from win-rate."""
    sys.path.insert(0, str(SCRIPTS))
    import qc_selfheal

    class _Log:
        def __init__(self):
            self.fixes = []; self.errors = []; self.warnings = []
        def fix(self, m):   self.fixes.append(m)
        def warn(self, m):  self.warnings.append(m)
        def error(self, m): self.errors.append(m)
        def ok(self, m):    pass
        def section(self, m): pass

    log = _Log()
    data = {"requests": [{
        "request_id": "r_covered",
        "destination": "Yokohama",
        "lonny_covered": True,
        "status": "PENDING",
        "loss_reason": None,
        "quoted": False,            # the bug scenario: covered but no rate
        "request_date": "2026-06-01",
    }]}
    qc_selfheal.phase_3_entries(log, data)
    r = data["requests"][0]
    assert r["status"] == "LOSS"
    assert r["loss_reason"] == "COVERED"
    assert r["quoted"] is True, (
        "C-2 regression: lonny_covered heal must set quoted=True so the "
        "row counts as Q&L in win-rate, not NQ."
    )


def test_lonny_covered_row_is_q_and_l_not_nq_after_heal():
    """End-to-end: after the lonny_covered heal, display_status returns
    'Q&L' (so the row counts toward win-rate denominator), not 'NQ'."""
    sys.path.insert(0, str(SCRIPTS))
    import qc_selfheal

    class _Log:
        def __init__(self):
            self.fixes = []; self.errors = []; self.warnings = []
        def fix(self, m): self.fixes.append(m)
        def warn(self, m): self.warnings.append(m)
        def error(self, m): self.errors.append(m)
        def ok(self, m): pass
        def section(self, m): pass

    log = _Log()
    data = {"requests": [{
        "request_id": "r1", "destination": "Yokohama",
        "lonny_covered": True, "status": "PENDING",
        "loss_reason": None, "quoted": False,
        "request_date": "2026-06-01",
    }]}
    qc_selfheal.phase_3_entries(log, data)
    r = data["requests"][0]
    assert scripts_core.display_status(r) == "Q&L"
    assert scripts_core.is_quoted_and_lost(r) is True
    assert scripts_core.is_not_quoted(r) is False


def test_drift_check_phase6_accepts_covered_with_quoted_true():
    """drift_check.phase6_covered_honor must accept LOSS/COVERED with
    quoted=True as the new correct shape (and OTHER as the legacy back-
    compat shape, preserving historical rows)."""
    sys.path.insert(0, str(SCRIPTS))
    import drift_check
    data = {"requests": [
        # New shape — must pass
        {"request_id": "r1", "lonny_covered": True, "status": "LOSS",
         "loss_reason": "COVERED", "quoted": True},
        # Legacy OTHER shape — back-compat, still passes if quoted=True
        {"request_id": "r2", "lonny_covered": True, "status": "LOSS",
         "loss_reason": "OTHER", "quoted": True},
        # Broken: COVERED but quoted=False — must flag
        {"request_id": "r3", "lonny_covered": True, "status": "LOSS",
         "loss_reason": "COVERED", "quoted": False},
    ]}
    log = {"phase6": {"covered_honor_issues": []}}
    drift_check.phase6_covered_honor(data, log)
    issues = log["phase6"]["covered_honor_issues"]
    issue_ids = [i["request_id"] for i in issues]
    assert "r1" not in issue_ids
    assert "r2" not in issue_ids
    assert "r3" in issue_ids


# ── C-5: QC-017 carrier-concentration counts STRICT Q&L rows ───────────

def test_qc_017_counts_strict_q_and_l_rows():
    """C-5 regression test: pre-fix, QC-017 only counted rows with
    status in ("WIN", "LOSS"). STRICT-form Q&L rows (status="Q&L") were
    silently excluded — making today's "66% CMA CGM" computed from WINs
    only. Now uses display_status / is_quoted_and_lost helpers."""
    # 25 STRICT-form Q&L rows for CMA CGM. Pre-fix: 0 counted (status
    # not in WIN/LOSS). Post-fix: 25 counted, triggers WARN >65%.
    sys.path.insert(0, str(SCRIPTS))
    import qc_selfheal as qsh

    class _Log:
        def __init__(self):
            self.fixes = []; self.errors = []; self.warnings = []; self.oks = []
        def fix(self, m): self.fixes.append(m)
        def warn(self, m): self.warnings.append(m)
        def error(self, m): self.errors.append(m)
        def ok(self, m): self.oks.append(m)
        def section(self, m): pass

    rows = []
    # 25 STRICT Q&L CMA CGM (would have been INVISIBLE pre-fix)
    rows += [{"status": "Q&L", "loss_reason": "PRICE", "quoted": True,
              "carrier_quoted": "CMA CGM"} for _ in range(25)]
    # 1 WIN MSC (only thing the old logic counted)
    rows.append({"status": "WIN", "carrier_won": "MSC", "quoted": True})

    log = _Log()
    qsh.phase_6_rules(log, {"requests": rows, "version": "2",
                             "summary": {}, "date_range": "x"})
    qc017_messages = [m for m in (log.warnings + log.errors + log.oks)
                      if "QC-017" in m]
    assert qc017_messages, "QC-017 should have produced SOME message"
    qc017_text = " ".join(qc017_messages)
    # Total should now be 26 (25 Q&L + 1 WIN), not 1 (WIN only)
    assert "/26" in qc017_text or " 26 " in qc017_text or "26 quotes" in qc017_text, (
        f"QC-017 count is missing the 25 STRICT Q&L rows. Message: {qc017_text!r}"
    )


# ── helper ports — cross-tree parity ────────────────────────────────────

def test_display_status_parity():
    """display_status, is_quoted_and_lost, is_not_quoted, is_loss were
    ported to scripts/core.py 2026-06-02. Behavior must match src/."""
    cases = [
        {"status": "WIN", "quoted": True},
        {"status": "WIN", "quoted": False},
        {"status": "LOSS", "quoted": True},
        {"status": "LOSS", "quoted": False},
        {"status": "Q&L", "quoted": True},
        {"status": "NQ", "quoted": False},
        {"status": "PENDING"},
        {},
    ]
    for r in cases:
        assert scripts_core.display_status(r) == hilmar_core.display_status(r), r
        assert scripts_core.is_quoted_and_lost(r) == hilmar_core.is_quoted_and_lost(r), r
        assert scripts_core.is_not_quoted(r) == hilmar_core.is_not_quoted(r), r
        assert scripts_core.is_loss(r) == hilmar_core.is_loss(r), r
