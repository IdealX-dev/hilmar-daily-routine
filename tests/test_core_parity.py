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

import re
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
    # 2026-07-26 Michael: "win loss timer is the 24/72 hours" — BOTH sides of
    # the deal resolve on the same clock. "ol response time has to be 3 hours"
    # is a separate BUSINESS-hour SLA (overdue/chase), never a loss threshold.
    ("PENDING_OL_LOSS_HOURS", 24),
    ("PENDING_OL_LOSS_HOURS_FRIDAY", 72),
    ("PENDING_OL_SLA_BIZ_HOURS", 3),
    ("PENDING_HILMAR_LOSS_HOURS", 24),
    ("PENDING_HILMAR_LOSS_HOURS_FRIDAY", 72),
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


# ── port identity: canonical_port_key / port_terminal / same_port ─────────

@pytest.mark.parametrize("a,b", [
    # Same city, both name a terminal, terminals DIFFER → not the same call.
    ("Manila (North)", "Manila (South)"),
    ("HCMC (Cat Lai)", "HCMC (Cai Mep)"),
    # Same city, both name the SAME terminal.
    ("Manila (North)", "manila (north)"),
    # One side terminal-less → matches either terminal.
    ("Manila", "Manila (South)"),
    ("Manila (North)", "Manila"),
    # Aliased city names, no terminals.
    ("Saigon", "Ho Chi Minh City"),
    ("Pusan", "Busan"),
    ("Ladkrabang", "Lat Krabang"),
    # Deliberately NOT merged — distinct physical ports.
    ("Bangkok", "Laem Chabang"),
    ("Tokyo", "Yokohama"),
    # Degenerate inputs.
    ("", ""),
    ("Manila", ""),
])
def test_same_port_parity(a, b):
    """same_port decides which rate response lands on which request row.
    scripts/ runs the fire; src/hilmar/ is what the suite covers. A drift
    here writes the wrong carrier's rate onto a client-facing quote in
    production while CI stays green — the exact PR #13 failure mode."""
    x = scripts_core.same_port(a, b)
    y = hilmar_core.same_port(a, b)
    assert x == y, f"same_port drift: scripts={x} hilmar={y} for {a!r}/{b!r}"


@pytest.mark.parametrize("dest", [
    "Manila (North)", "Manila", "HCMC (Cat Lai)", "Vietnam (Cat Lai)",
    "Lat Krabang", "Yokohama ", "", None, "Hong Kong (Kwai Tsing)",
])
def test_port_key_and_terminal_parity(dest):
    assert scripts_core.canonical_port_key(dest) == hilmar_core.canonical_port_key(dest), \
        f"canonical_port_key drift for {dest!r}"
    assert scripts_core.port_terminal(dest) == hilmar_core.port_terminal(dest), \
        f"port_terminal drift for {dest!r}"


# ── the prose in this file must agree with the numbers in it ────────────────

_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_REF_RE = re.compile(r"#\d+|\b[0-9a-f]{7,40}\b")
# 1-3 digits NOT followed by another digit, so "48h" is caught and "2026" is not.
_NUM_RE = re.compile(r"\b(\d{1,3})(?!\d)")
_TIMER_LINE_RE = re.compile(
    r"PENDING_(?:HILMAR|OL)_LOSS_HOURS|CLOCK hours|biz-?\s*hours? cutoff", re.I)


def _prose_lines(text: str) -> dict[int, str]:
    """The COMMENT and DOCSTRING lines of a module, by line number.

    Comments come from tokenize, docstrings from the AST. Code is excluded on
    purpose: `weekday() == 4` is a weekday index, not an hour, and a scanner
    that cannot tell prose from code is the exact mistake this repo has now
    made four times in two days.
    """
    import ast as _ast
    import io as _io
    import tokenize as _tok
    out = {}
    for t in _tok.generate_tokens(_io.StringIO(text).readline):
        if t.type == _tok.COMMENT:
            out[t.start[0]] = t.string
    tree = _ast.parse(text)
    for node in _ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, _ast.Expr) and isinstance(first.value, _ast.Constant) \
                and isinstance(first.value.value, str):
            lines = text.splitlines()
            for ln in range(first.lineno, first.end_lineno + 1):
                out.setdefault(ln, lines[ln - 1])
    return out


def _timer_prose_offenders(text: str, allowed: set[str]) -> list[str]:
    """Hour literals in timer PROSE that no timer constant holds.

    Dates and commit/issue refs are stripped first — a comment may cite
    "2026-07-26" or "0c73c4b" without that reading as a threshold.
    """
    out = []
    for i, line in sorted(_prose_lines(text).items()):
        if not _TIMER_LINE_RE.search(line):
            continue
        cleaned = _REF_RE.sub(" ", _DATE_RE.sub(" ", line))
        for n in _NUM_RE.findall(cleaned):
            if n not in allowed:
                out.append(f"line {i}: {line.strip()[:100]}")
                break
    return out


def test_timer_docs_match_constants():
    """A comment that states a threshold the code does not use is a trap.

    Michael set PENDING_HILMAR_LOSS_HOURS 48->24 on 2026-07-26 (0c73c4b,
    "supersedes 2026-07-14") and did the same for PENDING_OL_LOSS_HOURS. The
    surrounding comments and both stale-check docstrings kept saying 48. For
    eleven days, four separate places in core.py asserted a window the code had
    not used since the operator changed his mind.

    Found 2026-08-06 while chasing "PENDING OL (0) — missing a ton of data".
    The prose made the CONSTANT look like the bug, and the obvious next move
    was to "fix" 24 back to 48 — silently reverting an operator decision, in a
    timer that decides whether live business gets called lost. Getting that
    number wrong would have been far worse than the report being empty.
    """
    allowed = {
        str(scripts_core.PENDING_HILMAR_LOSS_HOURS),
        str(scripts_core.PENDING_HILMAR_LOSS_HOURS_FRIDAY),
        str(scripts_core.PENDING_OL_LOSS_HOURS),
        str(scripts_core.PENDING_OL_LOSS_HOURS_FRIDAY),
        str(scripts_core.PENDING_WINDOW_HOURS),
        str(scripts_core.PENDING_OL_SLA_BIZ_HOURS),
    }
    bad = []
    for mod, path in (("scripts", SCRIPTS / "core.py"),
                      ("src/hilmar", SRC / "hilmar" / "core.py")):
        bad += [f"{mod}/core.py {h}"
                for h in _timer_prose_offenders(path.read_text(encoding="utf-8"), allowed)]
    assert not bad, (
        "timer prose names an hour value no timer constant holds — update the "
        "comment, or name the constant instead of a number:\n  " + "\n  ".join(bad))


def test_the_timer_doc_guard_catches_a_planted_drift():
    """A guard nobody has watched fail is a guard nobody knows works.

    Planted as real module source, because the scanner parses it — the EXACT
    comment and docstring that sat in core.py for eleven days.
    """
    allowed = {"24", "72", "3"}
    stale = (
        '#: PENDING_HILMAR_LOSS_HOURS: Quoted & Lost after 48 CLOCK hours\n'
        'def f():\n'
        '    """Pure CLOCK hours, mirroring the other side: >= 48h, or 72h."""\n'
        '    return 1\n'
    )
    hits = _timer_prose_offenders(stale, allowed)
    assert len(hits) == 2, hits

    fixed = (
        '#: PENDING_HILMAR_LOSS_HOURS: Quoted & Lost after 24 CLOCK hours\n'
        'def f():\n'
        '    """Pure CLOCK hours, mirroring the other side: >= 24h, or 72h."""\n'
        '    return 1\n'
    )
    assert _timer_prose_offenders(fixed, allowed) == []


def test_the_timer_doc_guard_ignores_code_and_citations():
    """Two false-positive classes it must not fire on: a weekday index in
    CODE (`weekday() == 4`), and a date or commit sha cited in prose."""
    allowed = {"24", "72", "3"}
    src = (
        '#: 24 CLOCK hours, set in 0c73c4b (2026-07-26); see PENDING_OL_LOSS_HOURS\n'
        'PENDING_OL_LOSS_HOURS = 24\n'
        'def g(resp_et):\n'
        '    deadline = 72 if resp_et.weekday() == 4 else PENDING_OL_LOSS_HOURS\n'
        '    return deadline\n'
    )
    assert _timer_prose_offenders(src, allowed) == []


def test_both_trees_hold_the_same_timer_values():
    """The values themselves, not just the prose. These decide whether live
    business is called lost, and the two trees must never disagree."""
    for name in ("PENDING_HILMAR_LOSS_HOURS", "PENDING_HILMAR_LOSS_HOURS_FRIDAY",
                 "PENDING_OL_LOSS_HOURS", "PENDING_OL_LOSS_HOURS_FRIDAY",
                 "PENDING_WINDOW_HOURS"):
        a = getattr(scripts_core, name, None)
        b = getattr(hilmar_core, name, None)
        assert a is not None and a == b, f"{name}: scripts={a!r} src/hilmar={b!r}"
