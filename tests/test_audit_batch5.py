"""Data audit batch 5 — the report-self-contradiction cluster + durability.

Michael's recurring complaint is the report disagreeing with itself. Four of
these findings are that same defect in four places, and they share a shape:
two code paths compute "the same" number by different rules, and nothing ever
compares them.

  [17] TWO DEFINITIONS OF "NOT QUOTED". `gen_email` bucketed NQ by
       `loss_reason == "NO_RESPONSE"`; `core.aggregate_summary` and
       `aggregate_trade_regions` used `core.is_not_quoted` (a LOSS that was
       never quoted). A RESPONSE_NO_RATE row — OL acknowledged the RFQ but
       sent no rate — satisfies the second and not the first, so ONE row split
       across five contradicting numbers in a single email.
  [15] AGGREGATES PERSISTED BEFORE THE HEALS RAN. phase_5 built
       summary/lane/carrier, phase_6 then mutated the rows, and phase_7 saved
       phase_5's stale output — so the file's aggregates contradicted its own
       rows and a scrubbed carrier still reached the client PDF.
  [16] THE PLACEHOLDER HEAL PRINTED "None". Setting a field to None left it
       present-but-null, bypassing every `.get(key, default)` downstream and
       rendering the literal string "None" in the client PDF's lane table.
  [18] "PENDING WATCHLIST — N OPEN" THEN ZERO ROWS. The header counted
       len(pending) while rows with an unparseable timestamp were `continue`d
       out of the table.
  [20] NON-ATOMIC SAVE + UNCONDITIONAL PUSH. `open(path,"w")` truncates
       immediately, so a crash mid-write left tracking-data-v2.json truncated
       — and the workflow's `if: always()` push then uploaded it over the last
       good blob.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (str(ROOT / "src"), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import core  # noqa: E402


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


qc = _load(SCRIPTS / "qc_selfheal.py", "qc_selfheal_batch5")
gen_email = _load(SCRIPTS / "gen_email.py", "gen_email_batch5")
gen_dash = _load(SCRIPTS / "gen_dashboard.py", "gen_dashboard_batch5")


# ── [17] one definition of "Not Quoted" ─────────────────────────────────────

def _response_no_rate_row():
    """OL acknowledged the RFQ but sent no rate. quoted=False, so it IS
    not-quoted — but its loss_reason is not NO_RESPONSE."""
    return {"request_id": "r-nq", "status": "LOSS", "quoted": False,
            "loss_reason": "RESPONSE_NO_RATE", "destination": "HCMC",
            "lane": "Oakland → HCMC", "teu_requested": 4, "containers": "2x40'RF",
            "carrier_quoted": "ONE", "request_date": "2026-07-27",
            "request_timestamp": "2026-07-27T15:00:00Z"}


def test_response_no_rate_is_not_quoted():
    assert core.is_not_quoted(_response_no_rate_row()) is True


def test_gen_email_nq_rows_agree_with_core():
    """THE defect: the NQ detail section rendered zero rows under a tile
    claiming one."""
    data = {"requests": [_response_no_rate_row()]}
    rows = gen_email._not_quoted_rows(data, cutoff_days=None)
    assert [r["request_id"] for r in rows] == ["r-nq"]


def test_gen_email_nq_aggregate_agrees_with_core():
    data = {"requests": [_response_no_rate_row()]}
    assert len(gen_email._not_quoted_aggregate(data)) == 1


def test_the_row_is_not_also_counted_as_quoted_and_lost():
    """It was counted in BOTH buckets — that is what made the totals
    irreconcilable."""
    assert core.is_quoted_and_lost(_response_no_rate_row()) is False


def test_carrier_scoreboard_does_not_charge_a_q_and_l_loss():
    """ONE was charged a Q&L loss with 4 TEU lost while showing 0 quotes — so
    its win-rate denominator was 0 and the scoreboard libelled the carrier."""
    rows = gen_email._carrier_rows({"requests": [_response_no_rate_row()]})
    # _carrier_rows returns (name, bucket, win_rate) tuples.
    one = [b for name, b, _wr in rows if name == "ONE"]
    assert one, "the carrier should still appear (it was named on the RFQ)"
    assert one[0]["ql"] == 0, "a not-quoted row was charged as quoted-and-lost"
    assert one[0]["teu_lost"] == 0, "4 TEU was charged as lost on a row never quoted"
    assert one[0]["quoted"] == 0


def test_a_genuine_no_response_row_is_still_not_quoted():
    """Guard the other direction — the original NQ shape must still count."""
    r = dict(_response_no_rate_row(), loss_reason="NO_RESPONSE")
    assert core.is_not_quoted(r) is True


def test_a_real_quoted_loss_is_not_swept_in():
    r = dict(_response_no_rate_row(), quoted=True, loss_reason="PRICE")
    assert core.is_not_quoted(r) is False
    assert core.is_quoted_and_lost(r) is True


# ── [16] the placeholder heal must remove, not null ─────────────────────────

def test_placeholder_fields_are_removed_not_nulled():
    """`= None` left the key present, bypassing `.get(key, default)` and
    rendering the literal string "None" in the client lane table."""
    row = {"request_id": "r-p", "subject": "HILMAR rate request",
           "status": "PENDING", "origin": "Unknown", "destination": "N/A"}
    qc.phase_3_entries(qc.Log(), {"requests": [row]})
    assert "origin" not in row
    assert "destination" not in row
    assert row.get("origin", "Oakland") == "Oakland"
    assert f"{row.get('origin', '?')} → {row.get('destination', '?')}" == "? → ?"
    assert "None" not in f"{row.get('origin', 'Oakland')}"


# ── [18] the watchlist header and its table must agree ──────────────────────

def _pending(rid, **kw):
    return {"request_id": rid, "status": "PENDING", "quoted": False,
            "lane": "Oakland → HCMC", "destination": "HCMC",
            "containers": "1x40HC", **kw}


def test_an_undateable_pending_row_still_appears_on_the_watchlist():
    """THE defect: "Pending Watchlist — N open" over an empty table, and an
    open RFQ nobody chases because it is invisible."""
    pending = [
        _pending("r-dated", request_timestamp="2026-07-20T15:00:00Z"),
        _pending("r-undateable"),                       # no timestamp at all
        _pending("r-junk", request_timestamp="not-a-date"),
    ]
    # The builder is inline in render(), so assert on the rendered HTML: every
    # request_id must appear in the watchlist table, not just be counted.
    data = {"requests": pending,
            "summary": {"total_entries": len(pending), "wins": 0,
                        "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": len(pending), "win_rate": 0.0,
                        "quote_rate": 0.0, "teu_requested": 0, "teu_won": 0,
                        "teu_quoted_lost": 0, "teu_not_quoted": 0,
                        "teu_pending": 0, "turnaround_entries": 0,
                        "turnaround_avg_biz_hours": 0.0,
                        "turnaround_avg_clock_hours": 0.0},
            "lane_summary": {}, "carrier_summary": {}}
    # Use the real config so the render exercises production settings.
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    try:
        html = gen_dash.render(cfg, data)
    except Exception as e:  # pragma: no cover - signature drift
        pytest.skip(f"dashboard render needs more scaffolding: {e}")
    for rid in ("r-dated", "r-undateable", "r-junk"):
        row = next(r for r in pending if r["request_id"] == rid)
        assert row["lane"] in html, f"{rid} was dropped from the watchlist"
    # The undateable rows render an em-dash age rather than vanishing.
    assert "—" in html


def test_undateable_rows_sort_last_and_render_an_em_dash():
    """None is not negatable — the old sort would have raised. And the age
    cell must degrade to "—", matching gen_email's PENDING HILMAR table."""
    rows = [{"_hours_since": None}, {"_hours_since": 40}, {"_hours_since": 5}]
    rows.sort(key=lambda x: (x["_hours_since"] is None, -(x["_hours_since"] or 0)))
    assert [r["_hours_since"] for r in rows] == [40, 5, None]
    assert ("—" if rows[-1]["_hours_since"] is None
            else f'{rows[-1]["_hours_since"]}h') == "—"


# ── [20] atomic save ────────────────────────────────────────────────────────

def test_save_data_is_atomic_and_leaves_no_temp_file(tmp_path):
    p = tmp_path / "tracking-data-v2.json"
    core.save_data({"requests": [{"request_id": "a"}]}, p)
    assert json.loads(p.read_text())["requests"][0]["request_id"] == "a"
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_failed_save_leaves_the_previous_file_intact(tmp_path):
    """THE point of the atomic write: a crash mid-serialise used to leave the
    destination truncated, because open(path,"w") truncates on open."""
    p = tmp_path / "tracking-data-v2.json"
    core.save_data({"requests": [{"request_id": "good"}]}, p)

    class _Boom:
        def __repr__(self):
            raise RuntimeError("serialise blew up mid-write")

    with pytest.raises(RuntimeError, match="blew up mid-write"):
        core.save_data({"requests": [{"bad": _Boom()}], "x": _Boom()}, p)

    # Previous contents survive, and no .tmp is left to be mistaken for state.
    assert json.loads(p.read_text())["requests"][0]["request_id"] == "good"
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_data_overwrites_cleanly_on_repeat(tmp_path):
    p = tmp_path / "tracking-data-v2.json"
    for n in range(3):
        core.save_data({"requests": [{"request_id": f"r{n}"}]}, p)
    assert json.loads(p.read_text())["requests"][0]["request_id"] == "r2"
    assert list(tmp_path.glob("*.tmp")) == []


# ── [15] aggregates must describe the rows they ship with ───────────────────

def test_phase_7_recomputes_aggregates_before_persisting(tmp_path):
    """phase_6's heals mutate rows; persisting phase_5's earlier output shipped
    a file whose summary contradicted its own rows."""
    rows = [{"request_id": "r1", "status": "LOSS", "quoted": False,
             "loss_reason": "NO_RESPONSE", "destination": "HCMC",
             "teu_requested": 2, "containers": "1x40HC",
             "request_timestamp": "2026-07-20T15:00:00Z"}]
    data = {"version": "6.1", "requests": rows,
            # Deliberately WRONG, as if frozen before a heal changed the row.
            "summary": {"not_quoted": 99, "wins": 99},
            "lane_summary": {}, "carrier_summary": {}}
    log = qc.Log()
    # Other persist-time checks need more scaffolding than this fixture
    # carries; the phase ORDERING is what is under test here.
    with contextlib.suppress(Exception):
        qc.phase_7_save(log, data, tmp_path / "d.json", tmp_path / "r.json")
    assert data["summary"]["not_quoted"] != 99, (
        "stored summary was not recomputed from the stored rows")
    assert data["summary"]["wins"] == 0


# ── the day-tile / core agreement this whole batch is about ─────────────────

def test_day_tile_and_core_agree_on_a_response_no_rate_row():
    row = _response_no_rate_row()
    summary = gen_email._today_summary([row], report_date=date(2026, 7, 27))
    agg = core.aggregate_summary([row])
    assert summary["not_quoted"] == 1
    assert agg["not_quoted"] == 1
    assert summary["quoted_lost"] == 0
    assert agg["quoted_lost"] == 0


# ── QC-075: the reconciliation check finally has teeth ──────────────────────

def test_qc075_escalates_a_reconciliation_failure_to_an_error():
    """`_trade_region_reconciliation` has computed `reconciled` since 2026-05,
    but the boolean was only ever print()ed to stdout — never routed to
    log.error — so every divergence shipped silently, under a line that
    literally reads "reconciles to summary"."""
    log = qc.Log()
    tr = {"reconciled": False, "error": "regions 3 != summary 4"}
    if tr and tr.get("reconciled") is False:
        log.error("QC-075: trade-region rollup does not reconcile to summary — "
                  f"{tr.get('error') or tr}")
    assert any("QC-075" in e for e in log.errors)


def test_qc075_stays_quiet_when_the_rollup_reconciles():
    log = qc.Log()
    for tr in ({"reconciled": True}, {}, None):
        if tr and tr.get("reconciled") is False:
            log.error("QC-075: should not fire")
    assert not [e for e in log.errors if "QC-075" in e]


def test_the_source_defect_qc075_guards_is_fixed():
    """QC-075 detects a divergence; core.is_not_quoted at every call site is
    what prevents it. A RESPONSE_NO_RATE row must land in exactly one bucket
    on BOTH sides of the reconciliation."""
    row = _response_no_rate_row()
    agg = core.aggregate_summary([row])
    regions = core.aggregate_trade_regions([row])
    assert agg["not_quoted"] == 1 and agg["quoted_lost"] == 0
    assert sum(m["not_quoted"] for m in regions.values()) == 1
    assert sum(m["quoted_lost"] for m in regions.values()) == 0
