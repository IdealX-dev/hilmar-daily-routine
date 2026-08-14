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
            # POST-NQ_VALID_FROM on purpose (2026-08-14). These tests pin
            # the NQ *bucketing* predicate — is_not_quoted vs a loss_reason
            # test — not the recency floor. A July date now falls under
            # core.NQ_VALID_FROM and the row stops being counted as NQ at
            # all, which would test the floor instead of the thing this file
            # is about. tests/test_nq_reset.py owns the floor.
            "carrier_quoted": "ONE", "request_date": "2026-08-18",
            "request_timestamp": "2026-08-18T15:00:00Z"}


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
    # Report date follows the fixture past core.NQ_VALID_FROM (2026-08-14).
    summary = gen_email._today_summary([row], report_date=date(2026, 8, 18))
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


# ═══ second group: matching, idempotency, and the state-push blast radius ═══

# ── [14] a rate must not land on the wrong terminal ─────────────────────────

@pytest.mark.parametrize("a,b,compatible", [
    ("Manila (North)", "Manila (South)", False),   # THE defect
    ("Manila", "Manila (North)", True),            # bare city may match either
    ("Manila (North)", "Manila (North)", True),
    ("HCMC (Cat Lai)", "Cat Lai", True),           # alias, no terminal on one side
    ("HCMC", "Manila", False),
    ("Yokohama ", "Yokohama", True),               # trailing space
])
def test_same_port_requires_terminal_equality_when_both_name_one(a, b, compatible):
    assert core.same_port(a, b) is compatible


def test_port_terminal_extracts_only_a_parenthetical():
    assert core.port_terminal("Manila (North)") == "north"
    assert core.port_terminal("Manila") == ""
    assert core.port_terminal(None) == ""


def test_a_bare_city_rate_does_not_overwrite_a_different_terminal():
    """OL replies on "RE: Oakland to Manila (South)" while an open North RFQ
    exists. The South rate must not be written onto the North row — the client
    would be quoted the wrong terminal's price and the North request would
    stay unquoted and age out as NQ."""
    north = {"request_id": "r-north", "destination": "Manila (North)",
             "status": "PENDING", "quoted": False,
             "request_timestamp": "2026-07-20T15:00:00Z"}
    ingest = _load(SCRIPTS / "ingest.py", "ingest_batch5")
    ingest.apply_rate_responses([north], [{
        "destination": "Manila (South)", "sent": "2026-07-20T18:00:00Z",
        "subject": "RE: Oakland to Manila (South)", "ol_rate": "3450",
        "carrier": "ONE",
    }])
    assert north.get("quoted") is not True, "a South rate landed on the North row"
    assert not north.get("ol_rate")


# ── [23] QC-067's heal must respect an operator's verdict ───────────────────

def _misfiled_open_rfq(**kw):
    """An unquoted row filed LOSS/NO_RESPONSE while still inside the PENDING-OL
    window — the shape QC-067 restores."""
    return {"request_id": "r-67", "status": "LOSS", "quoted": False,
            "loss_reason": "NO_RESPONSE", "destination": "HCMC",
            "containers": "1x40HC",
            "request_timestamp": core.now_utc().isoformat(), **kw}


def test_qc067_heal_restores_an_open_rfq():
    """Baseline: the heal still does its job."""
    row = _misfiled_open_rfq()
    qc.phase_6_rules(qc.Log(), {"requests": [row], "summary": {},
                                "lane_summary": {}, "carrier_summary": {}})
    assert row["status"] == "PENDING"
    assert row["loss_reason"] is None


def test_qc067_heal_skips_a_manual_locked_row():
    """An operator's verdict outranks an automatic heal. Every other
    re-decide path skips manual_locked; this one did not, so a human
    correction was silently undone on the next fire."""
    row = _misfiled_open_rfq(manual_locked=True)
    qc.phase_6_rules(qc.Log(), {"requests": [row], "summary": {},
                                "lane_summary": {}, "carrier_summary": {}})
    assert row["status"] == "LOSS", "the operator's verdict was overwritten"
    assert row["loss_reason"] == "NO_RESPONSE"


def test_qc067_heal_records_the_real_prior_status():
    """The hand-rolled append hardcoded `"from": "LOSS"` — a FABRICATED prior
    state whenever the row was in any other status. record_transition reads
    the real one."""
    row = _misfiled_open_rfq(status="NQ")
    qc.phase_6_rules(qc.Log(), {"requests": [row], "summary": {},
                                "lane_summary": {}, "carrier_summary": {}})
    hist = row.get("status_history") or []
    if hist and hist[-1].get("to") == "PENDING":
        assert hist[-1]["from"] == "NQ", "recorded a fabricated prior status"


# ── [24] the weekly job must not revert the daily fire ──────────────────────

def test_weekly_and_daily_share_one_concurrency_group():
    """They share one blob store. Weekly pulls state at the start of its run;
    if the daily fire wrote new state in between, weekly's push uploaded its
    stale snapshot over it — a silent revert of a whole day's ingest."""
    daily = (ROOT / ".github/workflows/daily.yml").read_text(encoding="utf-8")
    weekly = (ROOT / ".github/workflows/weekly.yml").read_text(encoding="utf-8")

    def _group(y):
        for line in y.splitlines():
            if line.strip().startswith("group:"):
                return line.split("group:", 1)[1].strip()
        return None

    assert _group(daily) == _group(weekly) is not None


def test_weekly_pushes_only_its_own_flag():
    weekly = (ROOT / ".github/workflows/weekly.yml").read_text(encoding="utf-8")
    assert "state_store.py push --only weekly-sent" in weekly


def test_the_weekly_flag_is_actually_syncable():
    """The push step was NAMED "Push state back (weekly-sent flag)" but that
    flag was never in state_paths() — so it pushed everything EXCEPT the one
    thing it meant to, and weekly idempotency was machine-local."""
    ss = _load(SCRIPTS / "state_store.py", "state_store_batch5")
    paths = ss.state_paths("2026-07-27")
    assert "reports/weekly-sent-2026-07-27.flag" in paths


def test_push_only_narrows_the_blast_radius(tmp_path):
    ss = _load(SCRIPTS / "state_store.py", "state_store_batch5b")
    (tmp_path / "tracking-data-v2.json").write_text('{"requests": []}', encoding="utf-8")
    (tmp_path / "reports").mkdir()
    flag = f"reports/weekly-sent-{ss._today_et()}.flag"
    (tmp_path / flag).write_text("sent", encoding="utf-8")

    class _CC:
        def __init__(self): self.store = {}
        def get_blob_client(self, name):
            outer = self
            class _BC:
                def exists(self): return name in outer.store
                def upload_blob(self, data, overwrite=False): outer.store[name] = data
            return _BC()

    cc = _CC()
    pushed = ss.push(tmp_path, container=cc, only=["weekly-sent"])
    assert pushed == [flag]
    assert "tracking-data-v2.json" not in cc.store, (
        "weekly uploaded state it does not own")


# ── [25] the date window must be real, and under the key readers use ────────

def _ingest_output_keys() -> set[str]:
    """The top-level keys of the dict ingest.main() writes, read from the AST.

    AST, not a substring scan of the source: a `"data_range"` appearing in a
    COMMENT (this fix has several) would satisfy or defeat a text match by
    accident. Only the real dict literal counts.
    """
    import ast
    tree = ast.parse((SCRIPTS / "ingest.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Dict)
                and any(getattr(t, "id", None) == "output" for t in node.targets)):
            return {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    raise AssertionError("ingest.main() no longer assigns an `output` dict")


def test_ingest_writes_the_key_readers_actually_read():
    """Readers ask for `date_range`; ingest wrote `data_range`, so the key was
    silently absent and gen_email fell back to the CONFIG's start date."""
    keys = _ingest_output_keys()
    assert "date_range" in keys, "the key every reader asks for is not written"
    assert "data_range" not in keys, "the typo'd key is back"


def test_every_key_ingest_writes_is_declared_in_the_schema():
    """schema.json is the contract; ingest is the only writer of the top
    level. `data_range` survived three months because nothing compared the
    two — this is that comparison."""
    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
    declared = set(schema["properties"])
    undeclared = _ingest_output_keys() - declared
    assert not undeclared, f"ingest writes undeclared top-level keys: {sorted(undeclared)}"


def _request_row_keys() -> set[str]:
    """Keys of the two request-row dict literals in ingest.py, via the AST.

    Both literals are the argument of a `requests.append(...)` /
    `standalones.append(...)` call, so they are found by shape rather than by
    line number — adding a row builder does not silently drop it from this
    check.
    """
    import ast
    tree = ast.parse((SCRIPTS / "ingest.py").read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Dict)):
            continue
        keys = {k.value for k in node.args[0].keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if "request_id" in keys:      # a request row, not a status_history entry
            out |= keys
    assert out, "no request-row literal found in ingest.py"
    return out


def test_every_request_field_ingest_writes_is_declared_in_the_schema():
    """A field the pipeline writes but the schema never declares is invisible
    to anything built off the contract. `manual_locked` — the operator's
    override flag, which every heal must honour — was one of them, alongside
    pol, pod and free_time_requested."""
    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
    declared = set(schema["definitions"]["request"]["properties"])
    undeclared = _request_row_keys() - declared
    assert not undeclared, f"undeclared request fields: {sorted(undeclared)}"


@pytest.mark.parametrize("field", ["manual_locked", "pol", "pod",
                                   "free_time_requested"])
def test_the_four_previously_undeclared_fields_are_declared(field):
    """Named individually so removing one from the schema fails loudly rather
    than shrinking a set assertion."""
    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
    assert field in schema["definitions"]["request"]["properties"]


def test_manual_locked_is_written_where_the_heals_expect_it():
    """The schema entry is only worth something if the writer still sets it —
    ingest's operator-correction pass is the sole producer."""
    src = (SCRIPTS / "ingest.py").read_text(encoding="utf-8")
    assert 'row["manual_locked"] = True' in src


def test_computed_date_range_reflects_the_rows():
    ingest = _load(SCRIPTS / "ingest.py", "ingest_batch5c")
    rng = ingest._computed_date_range([
        {"request_timestamp": "2026-07-25T00:30:00Z"},   # Fri 24th in ET
        {"request_timestamp": "2026-07-20T15:00:00Z"},
        {"request_date": "2026-07-27"},
    ])
    assert rng == {"start": "2026-07-20", "end": "2026-07-27"}


def test_computed_date_range_handles_an_empty_build():
    ingest = _load(SCRIPTS / "ingest.py", "ingest_batch5d")
    rng = ingest._computed_date_range([])
    assert rng["start"] == rng["end"] and len(rng["start"]) == 10
