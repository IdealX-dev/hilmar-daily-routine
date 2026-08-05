"""Nothing we render may contain mojibake, and nothing we read may create it.

Michael 2026-08-05, on a dashboard row reading `Oakland â†' Shanghai`:
"illegible characters".

WHAT MOJIBAKE IS AND WHY IT SURVIVES. `→` is the three bytes E2 86 92. Decode
those as cp1252 and you get `â` `†` `'` — three perfectly valid characters. The
decode does not raise. It cannot: every byte 0x00–0xFF is a legal cp1252
character, so cp1252 accepts literally any input. The read succeeds, the wrong
string flows on, and when it is written back out as utf-8 the original bytes
are gone for good.

That is why `open(path)` is the dangerous spelling. It uses
locale.getpreferredencoding(), which is utf-8 on the Linux CI runners — where
the tests pass — and cp1252 on the Windows Cloud PC that actually runs the
daily pipeline. The bug is invisible in every environment that could catch it
and permanent in the one that matters.

Three layers here, because catching this after the fact is not enough:
  1. the shipped fixture is clean (it was NOT — 11 strings, repaired 2026-08-05)
  2. nothing we render contains mojibake
  3. no production module reads or writes text without naming the codec
Layer 3 is the only one that actually prevents it. The other two find it.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

FIXTURES = ROOT / "tests" / "fixtures"


# ── detector ─────────────────────────────────────────────────────────────────

# A utf-8 lead byte misread as cp1252 becomes Â (C2), Ã (C3) or â (E2); the
# continuation bytes become C1-range punctuation or Latin-1 supplement
# characters. That PAIRING is the signature — neither half is suspicious
# alone, which is why this looks for the two together rather than for "any
# non-ASCII". Real accented text ("âme", "Ângela") pairs the same lead with an
# ordinary letter and is correctly ignored.
_MOJIBAKE = re.compile(
    "[ÂÃâ]"
    "[-¿ŒœŠšŸŽžˆ˜"
    "–—‘-„†-•…‰‹›"
    "€™]"
)


def _mojibake_in(text: str) -> list[str]:
    """Return the distinct offending snippets, with a little context so a
    failure message points at something a human can find."""
    seen, out = set(), []
    for m in _MOJIBAKE.finditer(text):
        snippet = text[max(0, m.start() - 20):m.end() + 20].replace("\n", " ")
        if snippet not in seen:
            seen.add(snippet)
            out.append(snippet)
    return out


def test_the_detector_actually_detects():
    """A guard nobody has watched fail is a guard nobody knows works. These
    are the exact strings that shipped in golden_day.json."""
    assert _mojibake_in("Oakland â†’ Shanghai")
    assert _mojibake_in("Ocean rate request â€” Oakland")
    assert _mojibake_in("3Ã—40'RF")


def test_the_detector_does_not_cry_over_real_text():
    """The characters the reports legitimately contain — em dashes, arrows,
    bullets, the × in an equipment string, accented carrier names, emoji
    section icons — must all pass clean, or the guard gets switched off."""
    for clean in ("Oakland → Shanghai", "3×40'RF", "Requests — PTD",
                  "0 listed • 1 total", "Hapag-Lloyd", "Kühne + Nagel",
                  "âme", "Ângela", "📊 Summary", "±5d", "€1,200"):
        assert not _mojibake_in(clean), clean


# ── layer 1: the fixtures ────────────────────────────────────────────────────

@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
def test_fixture_is_free_of_mojibake(path):
    """golden_day.json carried 11 mangled strings — every lane arrow, every
    equipment "×", and the subject line's em dash. It is the fixture the
    dashboard, PDF, client-email and schema tests all render from, so those
    tests had been asserting mangled output was correct for as long as it had
    been there."""
    hits = _mojibake_in(path.read_text(encoding="utf-8"))
    assert not hits, f"{path.name} contains mojibake: {hits}"


# ── layer 2: what we render ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads((FIXTURES / "golden_day.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfg() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def test_dashboard_render_is_free_of_mojibake(cfg, golden):
    import gen_dashboard as GD
    hits = _mojibake_in(GD.render(cfg, golden))
    assert not hits, f"dashboard render contains mojibake: {hits}"


def test_client_email_render_is_free_of_mojibake(cfg, golden):
    """The one that reaches Lonny. Its lane column is built from the same
    `lane` field the dashboard mangled."""
    import gen_client_email as GCE
    rendered = GCE.build_subject(golden, cfg) + " " + GCE.build_body(golden, cfg)
    hits = _mojibake_in(rendered)
    assert not hits, f"client email render contains mojibake: {hits}"


# ── layer 3: the only layer that prevents it ─────────────────────────────────

# open() on a binary mode, and these modules' own file-like wrappers, are not
# text decodes and take no encoding.
_BINARY_OWNERS = {"tarfile", "pdfplumber", "zipfile", "gzip", "urllib", "request", "shutil"}

_PRODUCTION = sorted(
    [p for p in (ROOT / "scripts").glob("*.py")]
    + [p for p in (ROOT / "src" / "hilmar").glob("*.py")]
)


def _unpinned_text_io(path: Path):
    """Yield (lineno, call) for every text-mode open/read_text/write_text that
    does not name its codec.

    AST, not regex: this file's own docstring says `open(path)` several times,
    and a scanner that cannot tell a call from a sentence about one goes red on
    prose. That has happened three times in this repo already.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (
            f.id if isinstance(f, ast.Name) else None)
        if name not in ("open", "read_text", "write_text"):
            continue
        if any(k.arg == "encoding" for k in node.keywords):
            continue
        if name == "open":
            mode = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for k in node.keywords:
                if k.arg == "mode" and isinstance(k.value, ast.Constant):
                    mode = k.value.value
            if isinstance(mode, str) and ("b" in mode or ":" in mode):
                continue
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id in _BINARY_OWNERS:
                continue
        yield node.lineno, ast.unparse(node)


@pytest.mark.parametrize("path", _PRODUCTION, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_text_io_always_names_its_codec(path):
    """71 sites across 24 modules were relying on the platform default when
    this was written. On the Cloud PC that default is cp1252, so every one of
    them was a place the tracking file could come back mangled — including
    core.load_data, which every renderer goes through.

    The assertion is zero, not a shrinking allowlist: the fix is a single
    keyword argument, so there is no such thing as a site too expensive to
    convert."""
    hits = list(_unpinned_text_io(path))
    assert not hits, (
        f"{path.name} reads/writes text without an explicit codec at "
        + "; ".join(f"line {ln}: {src}" for ln, src in hits)
        + ' — pass encoding="utf-8"'
    )


def test_the_io_scanner_catches_a_planted_violation(tmp_path):
    p = tmp_path / "planted.py"
    p.write_text(
        '"""A docstring saying open(path) must NOT trip this."""\n'
        "from pathlib import Path\n"
        "def f(p):\n"
        "    return open(p).read() + Path(p).read_text()\n"
        "def g(p):\n"
        '    return open(p, "rb").read()\n',
        encoding="utf-8",
    )
    hits = list(_unpinned_text_io(p))
    assert [ln for ln, _ in hits] == [4, 4], hits   # the "rb" open is exempt
