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


# ── Policy floors: the ONE-TREE-ONLY blind spot ───────────────────────────

# 2026-08-14. test_no_undocumented_constants_drift above compares
# `set(scripts) & set(hilmar)` — INTERSECTION. A constant added to only ONE
# tree is therefore invisible to it, and that is exactly what happened with
# NQ_VALID_FROM: it shipped to scripts/core.py in PR #209 with a green suite
# while src/hilmar/core.py never got it. The suite could not have caught it.
#
# The *_VALID_FROM family is the highest-stakes constant class in this repo:
# each one suppresses a category of number on a report that goes to the CEO.
# A floor that exists in one tree and not the other means one tree reports a
# figure the other has deliberately withheld. So the floors are checked by
# UNION, not intersection, and a divergence must be written down.

ALLOWED_POLICY_FLOOR_DRIFT = {
    "NQ_VALID_FROM": (
        "scripts-only, deliberately. The fire renders Not-Quoted exclusively "
        "through scripts/core (gen_email and qc_selfheal both `import core`, "
        "which resolves to scripts/core.py). src/hilmar/core.is_not_quoted "
        "has NO callers anywhere under src/hilmar, so mirroring the floor "
        "there would add dead code, not safety. If src/hilmar/core ever "
        "grows a real NQ consumer, mirror the floor and delete this entry."
    ),
}


def _policy_floors(mod) -> dict:
    return {
        n: getattr(mod, n) for n in dir(mod)
        if n.endswith("_VALID_FROM") and isinstance(getattr(mod, n), str)
    }


def test_policy_floors_exist_in_both_trees_or_are_documented():
    """UNION check over the *_VALID_FROM floors — catches the one-tree-only
    addition that the intersection-based drift test structurally cannot see.

    A floor present in one core and absent from the other means the two
    trees disagree about which rows are reportable. That is allowed only
    with a written rationale in ALLOWED_POLICY_FLOOR_DRIFT."""
    sc = _policy_floors(scripts_core)
    hc = _policy_floors(hilmar_core)
    one_tree_only = sorted(
        (set(sc) ^ set(hc)) - set(ALLOWED_POLICY_FLOOR_DRIFT)
    )
    assert not one_tree_only, (
        "Policy floor(s) present in one core but not the other: "
        f"{one_tree_only}. A *_VALID_FROM floor suppresses a category of "
        "number on the CEO's report — it must either exist in BOTH trees "
        "with the same value, or be listed in ALLOWED_POLICY_FLOOR_DRIFT "
        "with a written rationale explaining why one tree is exempt."
    )


def test_policy_floors_shared_by_both_trees_have_equal_values():
    """A floor carried by both trees must hold the same date, or the two
    report paths silently disagree about the cutoff (TIMING_VALID_FROM is
    the live example — retired to "" in both, together)."""
    sc = _policy_floors(scripts_core)
    hc = _policy_floors(hilmar_core)
    for name in sorted(set(sc) & set(hc)):
        assert sc[name] == hc[name], (
            f"{name} drift: scripts={sc[name]!r} hilmar={hc[name]!r}. "
            "Both trees must apply the same cutoff."
        )


def test_documented_floor_exemptions_are_real():
    """Keeps ALLOWED_POLICY_FLOOR_DRIFT honest: an entry that no longer
    describes an actual divergence is stale documentation, and stale
    documentation is how the next drift gets waved through."""
    sc = _policy_floors(scripts_core)
    hc = _policy_floors(hilmar_core)
    for name in ALLOWED_POLICY_FLOOR_DRIFT:
        assert (name in sc) ^ (name in hc), (
            f"{name} is listed in ALLOWED_POLICY_FLOOR_DRIFT but is no "
            f"longer one-tree-only (scripts={name in sc}, "
            f"hilmar={name in hc}). Remove the exemption."
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
# An HOUR QUANTITY, not any number. "48h", "24 hours", "48 CLOCK hours",
# "72 biz hours" are thresholds; "2026-07-14", "Tuesday 18:00", "1.05x" and
# "5 days" are not.
#
# This narrowed from "any 1-3 digit number" on 2026-08-21, when _prose_lines
# started joining wrapped comment BLOCKS. Joining is what lets the guard see a
# phrase split across a line break, but it also puts every number in a long
# comment on one line — and a guard that cries wolf on "supersedes the
# 2026-06-04 Tuesday-18:00 carve-out" is a guard someone switches off.
_NUM_RE = re.compile(
    r"\b(\d{1,3})\s*(?:-\s*)?(?:CLOCK|clock|biz|business|wall|BIZ)?\s*"
    r"(?:h\b|hrs?\b|hours?\b)", re.I)
#: Write this in timer prose that intentionally names a SUPERSEDED value.
_HISTORIC_MARKER = "[historic]"
# A RANGE is a distribution bucket, not a threshold: the turnaround histogram
# in core.py's timing-reset docstring reads "0-48h ~254" / "48h-7d 3". Those
# 48s describe measured data, and flagging them would train a reader to
# ignore this guard — which is how the drift it exists to catch shipped.
_RANGE_RE = re.compile(
    r"\b\d{1,3}\s*-\s*\d{1,3}\s*[hd]\b"
    r"|\b\d{1,3}\s*[hd]\s*-\s*\d{1,3}\s*[hd]\b", re.I)
# What counts as TIMER prose worth checking. Punctuation-tolerant since
# 2026-08-21: the live defect read "within the 48h (biz-hours) cutoff", and
# the old pattern demanded "hours" immediately followed by "cutoff", so the
# ")" alone was enough to hide a wrong threshold that shipped in the CEO's
# report. Over-triggering is cheap here — a match only becomes a failure when
# the prose also carries an hour quantity no timer constant holds.
_TIMER_LINE_RE = re.compile(
    r"PENDING_(?:HILMAR|OL)_LOSS_HOURS|PENDING_WINDOW_HOURS"
    r"|CLOCK\s*hours?|biz[-\s]*hours?|business\s*hours?"
    r"|decision\s+window|aging\s+window", re.I)


def _prose_lines(text: str) -> dict[int, str]:
    """Timer PROSE of a module, by line number — comments, docstrings AND the
    string literals the program prints at people.

    THREE SOURCES, and the third one is why this guard was green over two live
    defects on 2026-08-21:

      comments   — grouped into BLOCKS. A comment block wraps, so the trigger
                   phrase routinely straddles a line break:
                       # ... window (Michael 2026-07-14): 48 CLOCK
                       # hours from the OL quote -> Q&L ...
                   Matching line-by-line, "CLOCK hours" is on NEITHER line and
                   the whole block was skipped. Consecutive comment lines are
                   now joined before matching and reported at the block's
                   first line.

      docstrings — as before.

      strings    — NEW. core.decide_status RETURNED the sentence "Send
                   received but no MDOLX within the 48h (biz-hours) cutoff"
                   as a StatusDecision reason_detail, which record_transition
                   stores in status_history and gen_email renders into the
                   Reason column of the daily report. A threshold the code
                   has not used since 2026-07-26 was being PRINTED TO THE CEO,
                   and a scanner that only reads comments could never see it.
                   Prose the program shows a human is prose.

    Executable code is still excluded on purpose: `weekday() == 4` is a
    weekday index, not an hour, and a scanner that cannot tell prose from code
    is the exact mistake this repo has now made four times in two days.
    """
    import ast as _ast
    import io as _io
    import tokenize as _tok
    lines = text.splitlines()
    out: dict[int, str] = {}

    # 1. COMMENTS, joined into blocks of consecutive lines.
    comments: dict[int, str] = {}
    for t in _tok.generate_tokens(_io.StringIO(text).readline):
        if t.type == _tok.COMMENT:
            comments[t.start[0]] = t.string
    for ln in sorted(comments):
        if ln - 1 in comments:
            continue                      # not the start of a block
        block, cur = [], ln
        while cur in comments:
            block.append(comments[cur].lstrip("# ").rstrip())
            cur += 1
        out[ln] = " ".join(block)

    # 2. DOCSTRINGS and 3. every other string literal, including f-strings.
    #    ast.JoinedStr covers f-strings, whose pieces tokenize splits apart on
    #    Python 3.12+ — walking the AST keeps this working on 3.11 and 3.12
    #    alike, which matters because CI is 3.12 and the box is 3.11.
    tree = _ast.parse(text)
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Constant, _ast.JoinedStr)):
            if isinstance(node, _ast.Constant) and not isinstance(node.value, str):
                continue
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if not start:
                continue
            seg = " ".join(lines[i - 1].strip() for i in range(start, end + 1))
            out[start] = (out.get(start, "") + " " + seg).strip()
    return out


def _timer_prose_offenders(text: str, allowed: set[str]) -> list[str]:
    """Hour literals in timer PROSE that no timer constant holds.

    Dates and commit/issue refs are stripped first — a comment may cite
    "2026-07-26" or "0c73c4b" without that reading as a threshold. What
    remains must be an actual hour quantity (see _NUM_RE), so a threshold the
    code no longer uses is caught while ordinary prose is not.
    """
    out = []
    for i, line in sorted(_prose_lines(text).items()):
        if not _TIMER_LINE_RE.search(line):
            continue
        # EXPLICIT OPT-OUT for prose that is deliberately recounting a
        # superseded value ("Michael said 48h on <date> and then 24h on
        # <date>"). That history is worth keeping — it is the reason nobody
        # should "fix" the constant back — but a scanner cannot tell it from
        # drift, and guessing from nearby words like "said"/"supersedes"
        # would let real drift through on any comment that happened to use
        # them. The author marks it, so silence is never the default: prose
        # naming a stale threshold WITHOUT the marker still fails.
        if _HISTORIC_MARKER in line:
            continue
        cleaned = _RANGE_RE.sub(" ", _REF_RE.sub(" ", _DATE_RE.sub(" ", line)))
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
