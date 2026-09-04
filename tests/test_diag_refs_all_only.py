"""The diagnostic that sizes the win-count change before it reaches Lonny.

PR #254 makes production's `decide_status` read the union of `mdolx_ref` and
`mdolx_refs_all`. The fix is correct — `booking_count` and `is_confirmed_win`
already read the union, and a ref only reaches `mdolx_refs_all` by parsing a
real OL booking email — but the number of live rows it moves was never
measured, and the move is not cosmetic:

  * `.github/workflows/daily.yml:381` resolves
    `SEND_TO: github.event_name == 'schedule' && 'full'`, so a SCHEDULED fire
    is always the full distribution; `send_to=test` guards manual dispatch
    only. Merging is therefore the send decision.
  * `gen_client_email` renders `is_confirmed_win` rows under "Your confirmed
    bookings", so a row invisible to Lonny yesterday appears tomorrow.

So the diagnostic has to be trustworthy in two ways this file pins: it must
compute the delta BOTH ways rather than assume one, and it must not touch the
state it read.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import diag_refs_all_only as D  # noqa: E402


def _row(rid, status, ref, refs_all, **kw):
    base = {
        "request_id": rid, "status": status, "quoted": True, "has_send": True,
        "mdolx_ref": ref, "mdolx_refs_all": refs_all,
        "teu_won": 2, "teu_requested": 2, "lane": "Oakland → Yokohama",
        "request_timestamp": "2026-08-12T10:00:00+00:00",
        "response_timestamp": "2026-08-13T20:04:00+00:00", "etd_fit_days": 3,
    }
    base.update(kw)
    return base


# ── which rows the parity gap actually acted on ──────────────────────────
def test_hazard_shape_is_a_ref_present_only_in_the_list():
    assert D.is_hazard_shape(_row("a", "LOSS", None, ["261031"])) is True


def test_a_row_with_a_primary_ref_is_not_the_hazard_shape():
    assert D.is_hazard_shape(_row("a", "WIN", "261031", ["261031"])) is False


def test_an_empty_string_primary_counts_as_absent():
    """`_merge_prior_win_into`'s fill loop skips a FALSY prior value, so `""`
    is how a row acquires the shape without ever holding None."""
    assert D.is_hazard_shape(_row("a", "LOSS", "", ["261031"])) is True


def test_no_refs_at_all_is_not_the_hazard_shape():
    """The negative control that keeps this from over-counting: a row with no
    booking reference anywhere is a loss on the merits, not a parity victim."""
    assert D.is_hazard_shape(_row("a", "LOSS", None, [])) is False


# ── the number the send decision needs ───────────────────────────────────
def test_it_reports_the_flip_and_the_headline_movement():
    rows = [
        _row("req_haz", "LOSS", None, ["261031"]),
        _row("req_ok", "WIN", "261099", ["261099"], teu_won=1),
        _row("req_none", "LOSS", None, []),
    ]
    s = D.summarise(rows)
    assert s["hazard_rows"] == 1
    assert [f["request_id"] for f in s["flips"]] == ["req_haz"]
    assert s["flips"][0]["new"] == "WIN"
    assert s["wins_after"] - s["wins_before"] == 1, (
        "the whole point is the delta — a diagnostic that cannot say how far "
        "the number moves does not inform the decision")
    assert s["teu_after"] > s["teu_before"]


def test_a_row_with_no_refs_never_flips():
    """Guards against the diagnostic overstating the impact, which would push
    the decision the wrong way just as surely as understating it."""
    s = D.summarise([_row("req_none", "LOSS", None, [])])
    assert s["hazard_rows"] == 0
    assert s["flips"] == []
    assert s["wins_after"] == s["wins_before"]


def test_preserved_from_prior_rows_are_counted_separately():
    """That population is the one the adversarial pass flagged as the likely
    source, so it has to be reported on its own rather than folded in."""
    rows = [_row("a", "LOSS", None, ["261031"], preserved_from_prior=True),
            _row("b", "LOSS", None, ["261032"])]
    s = D.summarise(rows)
    assert s["hazard_rows"] == 2
    assert s["preserved_from_prior"] == 1


# ── read-only, and provably so ───────────────────────────────────────────
def test_it_does_not_mutate_the_state_it_read():
    """THE guarantee that lets this run against production state. Every
    decision is computed on a copy; the pulled rows come back untouched."""
    rows = [_row("req_haz", "LOSS", None, ["261031"]),
            _row("req_ok", "WIN", "261099", ["261099"])]
    before = copy.deepcopy(rows)
    D.summarise(rows)
    assert rows == before, "the diagnostic mutated the rows it was given"


def test_the_render_names_the_send_mode_consequence():
    """The output has to say why the number matters, or a reader takes it as
    a curiosity rather than a decision input."""
    text = D.render(D.summarise([_row("a", "LOSS", None, ["261031"])]))
    assert "send_to=full" in text
    assert "CLIENT REPORT" in text
    assert "bookings counted" in text


# ── it must fail loudly, never green ─────────────────────────────────────
def test_main_errors_when_the_state_has_no_rows(tmp_path, monkeypatch, capsys):
    """A diagnostic that cannot fail loudly is worse than none — the
    2026-08-20 `|| true` that went green in zero seconds."""
    (tmp_path / "tracking-data-v2.json").write_text(
        json.dumps({"requests": []}), encoding="utf-8")

    class _FakeStore:
        @staticmethod
        def pull(root=None, **_):
            (Path(root) / "tracking-data-v2.json").write_text(
                json.dumps({"requests": []}), encoding="utf-8")
            return ["tracking-data-v2.json"]

    monkeypatch.setitem(sys.modules, "state_store", _FakeStore)
    assert D.main() == 2
    assert "::error::" in capsys.readouterr().out


def test_main_errors_when_the_pull_fails(monkeypatch, capsys):
    class _Broken:
        @staticmethod
        def pull(root=None, **_):
            raise RuntimeError("no connection string")

    monkeypatch.setitem(sys.modules, "state_store", _Broken)
    assert D.main() == 2
    out = capsys.readouterr().out
    assert "::error::" in out and "pull FAILED" in out


# ── the headline must agree with the rows it summarises ──────────────────
# Reported by an automated reviewer on PR #254. `_wins_teu` read `teu_won`
# raw while the `flips` list two lines above used
# `teu_won or teu_requested or 0`. That is not a stylistic difference: it is
# wrong for exactly the population this diagnostic targets.
#
# ingest._clear_win_evidence_on_exit zeroes teu_won on the WIN -> not-WIN edge,
# which is HOW a row reaches the hazard shape — the old primary-only
# decide_status demoted a send-signal WIN and the volume was cleared on the way
# out while mdolx_refs_all stayed populated. MEASURED before the fix, one row
# with teu_won=0 and teu_requested=2:
#
#     flips list says teu : 2
#     headline says TEU   : 0 -> 0
#     bookings            : 0 -> 1
#
# A row counted as a new booking, reported as moving no volume — in the number
# an operator reads before approving a full-distribution send.
def _demoted_row():
    """The real shape: demoted out of WIN, so teu_won was cleared and only
    teu_requested still carries the volume."""
    return _row("req_demoted", "LOSS", None, ["261031"],
                teu_won=0, teu_requested=2)


def test_the_teu_headline_agrees_with_the_per_row_flip_list():
    s = D.summarise([_demoted_row()])
    assert s["wins_after"] - s["wins_before"] == 1
    assert s["teu_after"] == s["flips"][0]["teu"], (
        "the headline and the per-row list disagree about the same row's "
        "volume — the headline read teu_won raw on a row whose teu_won was "
        "cleared when it left WIN")
    assert s["teu_after"] > s["teu_before"]


def test_the_teu_fallback_does_not_leak_onto_rows_the_union_left_alone():
    """NEGATIVE CONTROL. The fallback must apply only to flipped rows, and
    only in the AFTER arm — otherwise the baseline stops matching what
    production reports today and the delta is flattered from both ends."""
    already_win = _row("req_ok", "WIN", "261099", ["261099"],
                       teu_won=0, teu_requested=9)
    s = D.summarise([already_win])
    assert s["flips"] == []
    assert s["teu_before"] == 0 and s["teu_after"] == 0, (
        "teu_requested was substituted on a row the union did not flip")
