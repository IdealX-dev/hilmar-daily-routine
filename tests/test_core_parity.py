"""Cross-tree parity tests — scripts/core.py vs src/hilmar/core.py.

THE GAP THIS CLOSES: the test suite + coverage gate target src/hilmar/,
but the Cloud PC runs scripts/. On 2026-05-30 we found the two had
DRIFTED IN LOGIC (not just enums, which QC-040 already guards): the
Reading-B "WIN requires BOTH send AND mdolx" classifier lived in
src/hilmar/core.py (tested, green) but scripts/core.py still ran the old
"has_send OR mdolx -> WIN" rule — producing permanent phantom WINs in
production while the suite stayed green for over a month.

These tests import decide_status + send_signal_stale from BOTH modules
and assert they agree, so a future logic drift between the paired files
fails CI / the daily QC-052 routine instead of silently shipping.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SRC = ROOT / "src"
for p in (str(SRC), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

# scripts/core.py and src/hilmar/core.py are both importable but under
# different names (bare `core` vs `hilmar.core`). Import each explicitly.
import importlib.util


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec — dataclass introspection looks the module up
    # in sys.modules during class creation.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


scripts_core = _load(SCRIPTS / "core.py", "scripts_core_under_test")
hilmar_core = _load(SRC / "hilmar" / "core.py", "hilmar_core_under_test")


UTC = timezone.utc


# ── send_signal_stale must be byte-identical behaviour ───────────────────────

@pytest.mark.parametrize("send_iso,now_iso", [
    # Tuesday send, same day → fresh
    ("2026-04-21T03:00:00Z", "2026-04-21T12:00:00Z"),
    # Tuesday send, next Monday → stale
    ("2026-04-21T13:00:00Z", "2026-04-27T13:00:00Z"),
    # Friday send, Monday morning → NOT stale (weekend carve-out)
    ("2026-04-24T20:00:00Z", "2026-04-27T18:00:00Z"),
    # Friday send, Monday evening → stale
    ("2026-04-24T20:00:00Z", "2026-04-27T23:00:00Z"),
    # Wednesday send, +47h → fresh; +49h → stale
    ("2026-04-22T12:00:00Z", "2026-04-24T11:00:00Z"),
    ("2026-04-22T12:00:00Z", "2026-04-24T13:00:00Z"),
    # Saturday send → not stale until Monday eve
    ("2026-04-25T15:00:00Z", "2026-04-27T18:00:00Z"),
    ("2026-04-25T15:00:00Z", "2026-04-27T23:30:00Z"),
])
def test_send_signal_stale_parity(send_iso, now_iso):
    send_dt = datetime.fromisoformat(send_iso.replace("Z", "+00:00"))
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    a = scripts_core.send_signal_stale(send_dt, now)
    b = hilmar_core.send_signal_stale(send_dt, now)
    assert a == b, f"send_signal_stale drift: scripts={a} hilmar={b} for send={send_iso} now={now_iso}"


def test_send_signal_stale_none_parity():
    assert scripts_core.send_signal_stale(None) == hilmar_core.send_signal_stale(None) is False


# ── is_business_stale: same function under canonical name ─────────────────

@pytest.mark.parametrize("send_iso,now_iso,hours", [
    # Default 48h cases
    ("2026-04-21T03:00:00Z", "2026-04-21T12:00:00Z", 48),
    ("2026-04-21T13:00:00Z", "2026-04-27T13:00:00Z", 48),
    # Custom hours param (e.g. for short pings — must work both trees)
    ("2026-04-22T12:00:00Z", "2026-04-23T13:00:00Z", 24),
    ("2026-04-22T12:00:00Z", "2026-04-22T20:00:00Z", 4),
])
def test_is_business_stale_parity(send_iso, now_iso, hours):
    send_dt = datetime.fromisoformat(send_iso.replace("Z", "+00:00"))
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    a = scripts_core.is_business_stale(send_dt, now, hours=hours)
    b = hilmar_core.is_business_stale(send_dt, now, hours=hours)
    assert a == b, f"is_business_stale drift: scripts={a} hilmar={b} for {send_iso}/{now_iso}/{hours}h"


def test_send_signal_stale_alias_is_same_callable():
    """send_signal_stale is the backwards-compat alias for is_business_stale.
    The cross-tree drift catcher relies on the canonical name; the alias
    must keep working until any external caller is migrated off it."""
    assert scripts_core.send_signal_stale is scripts_core.is_business_stale
    assert hilmar_core.send_signal_stale is hilmar_core.is_business_stale


# ── Constant equality — locks the PR #13-class bug ────────────────────────

# Constants intentionally allowed to differ between trees. Adding a name
# here documents the divergence; nothing else may diverge silently.
ALLOWED_CROSS_FOLDER_DRIFT = {
    "VALID_STATUSES": (
        "scripts/ uses LEGACY 3-set {WIN,LOSS,PENDING}; src/hilmar/ uses "
        "STRICT+LEGACY union. QC-041 enforces production stays LEGACY-form. "
        "These intentionally differ — do not unify without changing every "
        "stored row's status field."
    ),
}


@pytest.mark.parametrize("name,expected", [
    # 2026-06-04 — corrected from 48 back to 24 per Michael's restated rule
    # ("if hilmar doesn't reply after 24 hours during biz week the deal is
    # lost"). The 48 was a previous stabilization that drifted from the
    # client-stated policy. Friday/weekend carve-out extended from Monday
    # 18:00 ET to Tuesday 18:00 ET to match the "by Tuesday" half of the rule.
    ("PENDING_WINDOW_HOURS", 24),
    ("RATE_TREND_THRESHOLD_PCT", 10),
    # 2026-06-02 — smarter PRICE classifier knobs. Locks the threshold
    # and minimum-wins requirement across trees.
    ("PRICE_GAP_THRESHOLD_MULT", 1.05),
    ("PRICE_GAP_MIN_LANE_WINS", 3),
    # 2026-07-24 — PENDING-OL response window. Before this, an unquoted row
    # was LOSS/NO_RESPONSE the instant it was ingested (no grace), which made
    # PENDING_OL structurally unreachable. Locked across trees.
    ("PENDING_OL_LOSS_HOURS", 48),
    ("PENDING_OL_LOSS_HOURS_FRIDAY", 72),
])
def test_numeric_constants_match_across_trees(name, expected):
    """Locks the constants that gate behavior. Any numeric/policy constant
    that exists in BOTH cores MUST match — if you need to diverge, add to
    ALLOWED_CROSS_FOLDER_DRIFT with a written explanation. This is the
    check that would have caught PENDING_WINDOW_HOURS=24 vs 48 drifting
    for a month with a green suite (audit finding #2, 2026-05-31)."""
    a = getattr(scripts_core, name, None)
    b = getattr(hilmar_core, name, None)
    assert a == b == expected, (
        f"{name} drift: scripts={a} hilmar={b} (expected {expected}). "
        f"Either fix the drift or add {name!r} to ALLOWED_CROSS_FOLDER_DRIFT "
        f"with a written rationale."
    )


def test_loss_reasons_set_equal_across_trees():
    """LOSS_REASONS must match (status semantics depend on this). QC-040
    also checks this — duplicated here so the parity test is self-contained
    and a fresh checkout's CI catches it without needing QC to run."""
    a = scripts_core.LOSS_REASONS
    b = hilmar_core.LOSS_REASONS
    assert a == b, f"LOSS_REASONS drift: scripts-only={a-b} hilmar-only={b-a}"


def test_no_undocumented_constants_drift():
    """Walk all UPPERCASE module-level names in BOTH cores. Any name that
    exists in both, holds a (numeric/string/tuple/frozenset) literal, and
    is NOT in ALLOWED_CROSS_FOLDER_DRIFT must have equal values across
    the trees. This catches the next PR #13-class drift before it ships."""
    import re
    def constants(mod):
        out = {}
        for n in dir(mod):
            if not re.match(r"^[A-Z][A-Z0-9_]+$", n):
                continue
            v = getattr(mod, n)
            if isinstance(v, (int, float, str, bytes, tuple, frozenset, set, dict)):
                out[n] = v
        return out
    sc = constants(scripts_core)
    hc = constants(hilmar_core)
    common = set(sc) & set(hc) - set(ALLOWED_CROSS_FOLDER_DRIFT)
    drifted = {n: (sc[n], hc[n]) for n in common if sc[n] != hc[n]}
    assert not drifted, (
        "Undocumented constant drift between scripts/core.py and "
        "src/hilmar/core.py:\n  " +
        "\n  ".join(f"{n}: scripts={a!r} hilmar={b!r}" for n, (a, b) in drifted.items())
    )


# ── decide_status: canonical outcome parity (accounts for LEGACY vs STRICT) ──

def _canon(decision) -> tuple:
    """Reduce a StatusDecision to a vocabulary-independent outcome so the
    LEGACY (scripts: WIN/LOSS/PENDING) and STRICT (hilmar: WIN/Q&L/NQ/
    PENDING) classifiers can be compared. The (is_win, loss_reason) pair
    is the part that must agree — the status string differs by design."""
    return (decision.status == "WIN", decision.loss_reason)


@pytest.mark.parametrize("kwargs", [
    # send + mdolx → WIN on both
    dict(has_send=True, mdolx_ref="MDX1", response_timestamp="2026-04-21T03:00:00Z",
         quoted=True, etd_fit_days=0),
    # send only, fresh (Tuesday +9h) → AWAITING_MDOLX on both
    dict(has_send=True, mdolx_ref=None, response_timestamp="2026-04-21T03:00:00Z",
         quoted=True, etd_fit_days=0,
         send_signal_events=[{"at": "2026-04-21T03:00:00Z"}]),
    # send only, stale (Tuesday → next Monday) → SEND_NO_BOOKING on both
    dict(has_send=True, mdolx_ref=None, response_timestamp="2026-04-21T13:00:00Z",
         quoted=True, etd_fit_days=0,
         send_signal_events=[{"at": "2026-04-21T13:00:00Z"}]),
    # mdolx only (no send) → MDOLX_NO_SEND anomaly on both
    dict(has_send=False, mdolx_ref="MDX9", response_timestamp="2026-04-21T03:00:00Z",
         quoted=True, etd_fit_days=0),
    # NQ taxonomy parity (the fix): truly silent → NO_RESPONSE on both
    dict(has_send=False, mdolx_ref=None, response_timestamp=None,
         quoted=False, etd_fit_days=None),
    # responded but no rate parsed → RESPONSE_NO_RATE on both
    dict(has_send=False, mdolx_ref=None, response_timestamp="2026-04-21T03:00:00Z",
         quoted=False, etd_fit_days=None),
    # THE BUG: quoted=True but missing timestamp → Q&L (not NQ) on both
    dict(has_send=False, mdolx_ref=None, response_timestamp=None,
         quoted=True, etd_fit_days=None),
])
def test_decide_status_win_and_send_outcomes_parity(kwargs):
    now = datetime(2026, 4, 27, 13, 0, tzinfo=UTC)
    a = _canon(scripts_core.decide_status(now=now, **kwargs))
    b = _canon(hilmar_core.decide_status(now=now, **kwargs))
    assert a == b, f"decide_status outcome drift: scripts={a} hilmar={b} for {kwargs}"


def test_old_or_rule_is_gone_in_both():
    """The exact regression that caused the phantom WINs: send-signal
    alone (no mdolx, stale) must NOT be WIN in either tree."""
    now = datetime(2026, 4, 27, 13, 0, tzinfo=UTC)
    stale_send = dict(
        has_send=True, mdolx_ref=None,
        response_timestamp="2026-04-21T13:00:00Z", quoted=True, etd_fit_days=0,
        send_signal_events=[{"at": "2026-04-21T13:00:00Z"}], now=now,
    )
    assert scripts_core.decide_status(**stale_send).status != "WIN"
    assert hilmar_core.decide_status(**stale_send).status != "WIN"
