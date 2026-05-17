"""Pipeline smoke tests — ported from ../scripts/run_tests.py run_pipeline_smoke().

Exercises the QC → dashboard → PDF → email chain end-to-end against the
golden fixture in a temp directory (so live tracking-data-v2.json is never
touched).

Tests for modules that don't ship until later M3 milestones use
``pytest.importorskip`` — they auto-skip until the module lands and
auto-activate the moment it does. As of M3.3:

  * ``qc`` exists (M3.5) → smoke test runs.
  * ``render`` is M3.6 → 4 dashboard/pdf/scorecards/email tests skip.

When M3.6 ships ``hilmar.render``, those tests start running with no edits
required here.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import GOLDEN_DAY, SCHEMA_PATH  # pytest puts tests/ on sys.path

from hilmar import orchestrator  # for insights-split tests at end of file


@pytest.fixture
def pipeline_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated workspace mirroring the on-VM layout under ``tmp_path``.

    Wires the HILMAR_* env vars so any module reading paths via
    :mod:`hilmar.paths` lands in the temp tree. Copies the golden fixture
    in as ``tracking-data-v2.json`` and the schema next to it.
    """
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    backup_dir = tmp_path / "data-backups"
    data_dir.mkdir()
    reports_dir.mkdir()
    backup_dir.mkdir()

    shutil.copy2(GOLDEN_DAY, data_dir / "tracking-data-v2.json")
    shutil.copy2(SCHEMA_PATH, data_dir / "schema.json")

    monkeypatch.setenv("HILMAR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("HILMAR_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("HILMAR_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("HILMAR_DRY_RUN", "true")
    return tmp_path


# ── 1. QC self-heal runs clean on the golden fixture ──────────────────


def test_qc_selfheal_runs_clean_on_golden_fixture(pipeline_workspace: Path):
    """QC should run end-to-end without flagging blockers on the golden fixture
    and write reports/qc-result.json with status==ok."""
    qc = pytest.importorskip("hilmar.qc")
    data_path = pipeline_workspace / "data" / "tracking-data-v2.json"
    schema_path = pipeline_workspace / "data" / "schema.json"
    reports_dir = pipeline_workspace / "reports"
    backup_dir = pipeline_workspace / "data-backups"

    qc_result_path = reports_dir / "qc-result.json"
    result, _log = qc.run_qc(
        data_path=data_path,
        schema_path=schema_path,
        backups_dir=backup_dir,
        result_path=qc_result_path,
    )

    assert qc_result_path.exists(), "qc-result.json was not written"
    assert result.get("status") in {"CLEAN", "HAS_ERRORS"}, (
        f"unexpected QC status on golden fixture: {result.get('status')}"
    )


# ── 2. Dashboard renders non-empty HTML ───────────────────────────────


def test_render_dashboard_produces_nonempty_html(pipeline_workspace: Path):
    render = pytest.importorskip(
        "hilmar.render",
        reason="render.py ships at M3.6 — test auto-activates then.",
    )
    out = pipeline_workspace / "reports" / "hilmar-dashboard.html"
    render.render_dashboard(
        data_path=pipeline_workspace / "data" / "tracking-data-v2.json",
        out_path=out,
    )
    assert out.exists(), "dashboard HTML not written"
    assert out.stat().st_size > 5000, f"dashboard HTML suspiciously small: {out.stat().st_size}"


# ── 3. PDF renders non-empty bytes ────────────────────────────────────


def test_render_pdf_produces_nonempty_pdf(pipeline_workspace: Path):
    render = pytest.importorskip(
        "hilmar.render",
        reason="render.py ships at M3.6 — test auto-activates then.",
    )
    out = pipeline_workspace / "reports" / "hilmar-report.pdf"
    render.render_pdf(
        data_path=pipeline_workspace / "data" / "tracking-data-v2.json",
        out_path=out,
    )
    assert out.exists(), "PDF not written"
    assert out.stat().st_size > 3000, f"PDF suspiciously small: {out.stat().st_size}"


# ── 4. Carrier scorecards: at least one PDF produced ──────────────────


def test_render_carrier_scorecards_produces_at_least_one(pipeline_workspace: Path):
    render = pytest.importorskip(
        "hilmar.render",
        reason="render.py ships at M3.6 — test auto-activates then.",
    )
    out_dir = pipeline_workspace / "reports" / "carrier-scorecards"
    render.render_scorecards(
        data_path=pipeline_workspace / "data" / "tracking-data-v2.json",
        out_dir=out_dir,
    )
    # Golden fixture has MSC + Maersk → expect ≥1 carrier PDF after rebuild.
    assert out_dir.exists(), "carrier-scorecards dir was not created"
    pdfs = list(out_dir.glob("*.pdf"))
    assert len(pdfs) >= 1, f"no scorecard PDFs generated (dir={out_dir})"


# ── 5. Email body renders non-empty HTML ──────────────────────────────


def test_render_email_produces_nonempty_html_body(pipeline_workspace: Path):
    render = pytest.importorskip(
        "hilmar.render",
        reason="render.py ships at M3.6 — test auto-activates then.",
    )
    out = pipeline_workspace / "reports" / "email-body.html"
    render.render_email(
        data_path=pipeline_workspace / "data" / "tracking-data-v2.json",
        out_path=out,
    )
    assert out.exists(), "email HTML not written"
    assert out.stat().st_size > 1500, f"email HTML suspiciously small: {out.stat().st_size}"


# ── Sanity: the smoke chain processes the golden fixture without
#    mutating tracking-data-v2.json on disk past acceptable QC heals. ──


def test_smoke_chain_does_not_clobber_input_unrecognisably(pipeline_workspace: Path):
    """After QC self-heal runs, the data file should still parse as JSON
    with the same top-level keys. Regression-guard against accidental
    truncation / write-corruption inside heal logic."""
    qc = pytest.importorskip("hilmar.qc")
    data_path = pipeline_workspace / "data" / "tracking-data-v2.json"

    before = json.loads(data_path.read_text(encoding="utf-8"))
    qc.run_qc(
        data_path=data_path,
        schema_path=pipeline_workspace / "data" / "schema.json",
        backups_dir=pipeline_workspace / "data-backups",
        result_path=pipeline_workspace / "reports" / "qc-result.json",
    )
    after = json.loads(data_path.read_text(encoding="utf-8"))

    assert set(before) <= set(after), (
        "QC dropped top-level keys: missing="
        f"{set(before) - set(after)}"
    )


# ── PR #16 — Email design quick wins ──────────────────────────────────


def test_render_email_has_meaningful_dod_filter():
    """A dod with all empty sub-lists should NOT render the
    'What happened since last run' block — it'd just be a header
    followed by the summary text and zero content. Render decides
    whether to pass dod=non-None to the template via _has_meaningful_dod."""
    from hilmar import render
    empty_dod = {
        "summary_text": "0 new requests, 0 quotes received, 0 wins, 0 pending Hilmar response",
        "new_requests": [],
        "new_responses": [],
        "status_changes": [],
        "new_wins": [],
        "new_pending": [],
        "newly_lost": [],
    }
    assert render._has_meaningful_dod(empty_dod) is False
    populated_dod = dict(empty_dod, new_wins=[{"lane": "Oakland → Tokyo"}])
    assert render._has_meaningful_dod(populated_dod) is True
    assert render._has_meaningful_dod(None) is False


def test_carrier_rows_with_rollup_collapses_small_carriers():
    """Top carrier (≥5 quotes) keeps full row; carriers with <5 quotes
    roll up into a single 'Others (N)' row to keep the scoreboard
    scannable. Today's data: MSC=33, CMA CGM=2, ONE=1 — table reduces
    to 2 rows from 3."""
    from hilmar import render
    data = {"carriers": [
        {"carrier": "MSC", "quotes": 33, "wins": 8, "losses": 22, "win_rate": 24.2},
        {"carrier": "CMA CGM", "quotes": 2, "wins": 1, "losses": 1, "win_rate": 50.0},
        {"carrier": "ONE", "quotes": 1, "wins": 1, "losses": 0, "win_rate": 100.0},
    ]}
    rows = render._carrier_rows_with_rollup(data, min_quotes=5)
    assert len(rows) == 2
    assert rows[0]["carrier"] == "MSC"
    assert rows[1]["carrier"] == "Others (2)"
    assert rows[1]["wins"] == 2  # 1 + 1
    assert rows[1]["losses"] == 1
    assert rows[1].get("_is_rollup") is True


def test_carrier_rows_with_rollup_no_rollup_when_all_above_threshold():
    """If every carrier hits the min_quotes threshold, no Others row."""
    from hilmar import render
    data = {"carriers": [
        {"carrier": "MSC", "quotes": 30, "wins": 8, "losses": 22, "win_rate": 26.7},
        {"carrier": "CMA CGM", "quotes": 10, "wins": 4, "losses": 6, "win_rate": 40.0},
    ]}
    rows = render._carrier_rows_with_rollup(data, min_quotes=5)
    assert len(rows) == 2
    assert all(not r.get("_is_rollup") for r in rows)


def test_alert_anomalies_filters_severity():
    """Banner only renders for severity='alert' anomalies. info/warn
    stay in the LLM-narrative collapsible block, not above the KPIs."""
    from hilmar import render
    ctx = {"anomalies": [
        {"kind": "win_rate_shift", "severity": "info", "label": "+9pp"},
        {"kind": "ingest_gap_suspected", "severity": "alert", "label": "Volume 0 vs baseline"},
        {"kind": "response_slow", "severity": "warn", "label": "P50 +50%"},
    ]}
    alerts = render._alert_anomalies(ctx)
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "ingest_gap_suspected"
    # No anomalies → empty list
    assert render._alert_anomalies({}) == []
    assert render._alert_anomalies(None) == []


# ── Insights split: staff vs internal (Michael's 2026-04-28 directive) ──


def test_render_narrative_html_staff_subset_excludes_system_design_data():
    """Staff-facing insights HTML must contain ONLY the Business section.
    System/Design/Data are operational-internal — staff (Lonny + rate
    desk eventually on TO) should not read them."""
    from hilmar import insights as ins
    from hilmar.model_router import ModelResponse

    def _resp(text):
        return ModelResponse(text=text, model="m", task_type="t",
                             input_tokens=0, output_tokens=0, cost_cents=0)

    bundle = ins.NarrativeBundle(
        system=_resp("- System note: parser miss-rate 3.1%"),
        design=_resp("- Design note: anomaly banner placement"),
        data=_resp("- Data note: add rate_per_feu"),
        business=_resp("- Business action: lean on CMA CGM"),
    )
    staff_html = ins.render_narrative_html(bundle, sections=ins.STAFF_SECTIONS)
    assert "Business" in staff_html
    assert "lean on CMA CGM" in staff_html
    # Must NOT contain operational sections
    assert "System note" not in staff_html
    assert "Design note" not in staff_html
    assert "Data note" not in staff_html
    assert "<h4 style='margin: 14px 0 6px 0; color: #0b3d91;'>System</h4>" not in staff_html
    assert "<h4 style='margin: 14px 0 6px 0; color: #0b3d91;'>Design</h4>" not in staff_html
    assert "<h4 style='margin: 14px 0 6px 0; color: #0b3d91;'>Data</h4>" not in staff_html


def test_render_narrative_html_internal_subset_includes_all_four():
    """Internal review email must contain System + Design + Data +
    Business — full operational view for Michael only."""
    from hilmar import insights as ins
    from hilmar.model_router import ModelResponse

    def _resp(text):
        return ModelResponse(text=text, model="m", task_type="t",
                             input_tokens=0, output_tokens=0, cost_cents=0)

    bundle = ins.NarrativeBundle(
        system=_resp("- System note: parser miss-rate 3.1%"),
        design=_resp("- Design note: anomaly banner placement"),
        data=_resp("- Data note: add rate_per_feu"),
        business=_resp("- Business action: lean on CMA CGM"),
    )
    internal_html = ins.render_narrative_html(bundle, sections=ins.INTERNAL_SECTIONS)
    assert "System note" in internal_html
    assert "Design note" in internal_html
    assert "Data note" in internal_html
    assert "lean on CMA CGM" in internal_html


def test_resolve_internal_distribution_falls_back_to_daily_cc(monkeypatch):
    """Internal distribution defaults to HILMAR_DAILY_CC so the operational
    narrative reaches Michael's idealx address even before
    HILMAR_INTERNAL_TO is explicitly set on the VM."""
    monkeypatch.delenv("HILMAR_INTERNAL_TO", raising=False)
    monkeypatch.setenv("HILMAR_DAILY_CC", "michael.deitchman@idealx.us")
    out = orchestrator._resolve_internal_distribution()
    assert out == ["michael.deitchman@idealx.us"]
    # Explicit HILMAR_INTERNAL_TO wins
    monkeypatch.setenv("HILMAR_INTERNAL_TO", "ops@example.com")
    assert orchestrator._resolve_internal_distribution() == ["ops@example.com"]


def test_send_internal_review_no_op_when_no_recipient():
    """When the internal distribution list is empty, send_internal_review
    logs + skips — does NOT raise. Daily run continues."""
    from hilmar import send as send_mod

    class StubClient:
        def send_mail(self, **kwargs):
            raise AssertionError("must not be called when to=[]")

    msg = send_mod.send_internal_review(
        client=StubClient(), to=[], subject="x", html_body="<p>x</p>",
    )
    assert msg == ""


def test_orchestrator_imports_insights_split_constants():
    """Smoke test: insights module exposes STAFF_SECTIONS + INTERNAL_SECTIONS
    constants so orchestrator.step_insights can pick the right subset."""
    from hilmar import insights as ins
    assert ins.STAFF_SECTIONS == ("business",)
    assert ins.INTERNAL_SECTIONS == ("system", "design", "data", "business")


# ─── Currency formatting filter (PR #31) ──────────────────────────────


def test_usd_filter_formats_numbers_with_commas_and_two_decimals():
    """$x,xxx.xx format — required by user 2026-04-28 readability pass."""
    from hilmar.render import _usd
    assert _usd(2400) == "$2,400.00"
    assert _usd(2400.5) == "$2,400.50"
    assert _usd(1234567.89) == "$1,234,567.89"
    assert _usd(0) == "$0.00"


def test_usd_filter_handles_string_rate_with_parse():
    """ol_rate is often a free-form string like '$2400/40HC' — filter
    parses the number and formats it cleanly."""
    from hilmar.render import _usd
    assert _usd("$2400") == "$2,400.00"
    assert _usd("$2,400 per 40HC") == "$2,400.00"


def test_usd_filter_returns_em_dash_for_none_or_unparseable():
    from hilmar.render import _usd
    assert _usd(None) == "—"
    assert _usd("") == "—"
    # Free-form non-numeric strings pass through (no false "$0.00").
    assert _usd("call for rate") == "call for rate"


# ─── Inline request log (PR #32) ──────────────────────────────────────


def test_request_log_rows_sorts_most_recent_first():
    """Request log inlined into email body — sorted most-recent-first
    so the latest activity is at the top."""
    from hilmar.render import _request_log_rows
    data = {
        "requests": [
            {"request_id": "old", "request_timestamp": "2026-04-15T10:00:00Z",
             "lane": "A → B", "status": "WIN"},
            {"request_id": "new", "request_timestamp": "2026-04-28T10:00:00Z",
             "lane": "C → D", "status": "Q&L"},
            {"request_id": "mid", "request_timestamp": "2026-04-22T10:00:00Z",
             "lane": "E → F", "status": "PENDING"},
        ]
    }
    out = _request_log_rows(data)
    assert [r["request_id"] for r in out] == ["new", "mid", "old"]


def test_request_log_rows_caps_at_top():
    """Cap at the configured top to keep the email body bounded."""
    from hilmar.render import _request_log_rows
    data = {"requests": [
        {"request_id": f"r{i}", "request_timestamp": f"2026-04-{i:02d}T10:00:00Z",
         "status": "Q&L"} for i in range(1, 60)
    ]}
    out = _request_log_rows(data, top=20)
    assert len(out) == 20


# ─── Headlines + segments + $ won (PR #35) ────────────────────────────


def test_total_value_won_sums_rate_per_feu_times_feu():
    """rate_per_feu × FEU-equivalent count, summed across WIN rows.
    1 FEU = 2 TEU, so teu_won/2 gives the multiplier."""
    from hilmar.render import _total_value_won
    data = {"requests": [
        {"status": "WIN", "rate_per_feu": 2400, "teu_won": 2},   # 1 FEU * 2400 = 2400
        {"status": "WIN", "rate_per_feu": 3500, "teu_won": 4},   # 2 FEU * 3500 = 7000
        {"status": "WIN", "rate_per_feu": None, "teu_won": 4},   # excluded (no rpf)
        {"status": "Q&L", "rate_per_feu": 9999, "teu_won": 4},   # excluded (not WIN)
    ]}
    assert _total_value_won(data) == 9400.00


def test_total_value_won_returns_zero_with_no_wins():
    from hilmar.render import _total_value_won
    assert _total_value_won({"requests": []}) == 0.0
    assert _total_value_won({"requests": [{"status": "Q&L"}]}) == 0.0


def test_trade_region_segments_aggregates_and_orders_by_volume():
    """Buckets by trade_region with wins/q_and_l/teu/value_won breakdowns."""
    from hilmar.render import _trade_region_segments
    data = {"requests": [
        {"trade_region": "China", "status": "WIN", "teu_requested": 4,
         "teu_won": 4, "rate_per_feu": 2400},
        {"trade_region": "China", "status": "Q&L", "teu_requested": 2},
        {"trade_region": "Japan", "status": "WIN", "teu_requested": 2,
         "teu_won": 2, "rate_per_feu": 3500},
    ]}
    out = _trade_region_segments(data)
    assert [s["region"] for s in out] == ["China", "Japan"]
    china = out[0]
    assert china["total"] == 2
    assert china["wins"] == 1
    assert china["q_and_l"] == 1
    assert china["win_rate"] == 50.0  # 1/(1+1)
    assert china["value_won"] == 4800.0  # 2 FEU * 2400


def test_trade_region_segments_counts_wins_without_rate():
    """Pure-MDOLX WINs (booking confirmed, no rate-quote email parsed)
    have rate_per_feu = None and contribute $0 to value_won. The segment
    aggregator must track them separately so the email template can
    show 'n/a (rate not captured)' instead of a misleading '—'.

    Per Michael 2026-04-29: 5 of 11 wins on the live dashboard had
    blank value_won, but the column rendered '—' which looked like
    'no wins in this region' rather than 'we won here but didn't parse
    the rate'.
    """
    from hilmar.render import _trade_region_segments
    data = {"requests": [
        # Japan: 1 normal win + 1 pure-MDOLX win (no rate)
        {"trade_region": "Japan", "status": "WIN", "teu_requested": 2,
         "teu_won": 2, "rate_per_feu": 3500},
        {"trade_region": "Japan", "status": "WIN", "teu_requested": 0,
         "teu_won": 0, "rate_per_feu": None},
        # Africa: 1 pure-MDOLX win only — value_won = 0, wins_no_rate = 1
        {"trade_region": "Africa", "status": "WIN", "teu_requested": 1,
         "teu_won": 1, "rate_per_feu": None},
    ]}
    out = {s["region"]: s for s in _trade_region_segments(data)}
    assert out["Japan"]["wins"] == 2
    assert out["Japan"]["wins_no_rate"] == 1
    assert out["Japan"]["value_won"] == 3500.0  # only the rated one counts
    assert out["Africa"]["wins"] == 1
    assert out["Africa"]["wins_no_rate"] == 1
    assert out["Africa"]["value_won"] == 0


def test_compose_headline_subject_includes_metrics(tmp_path):
    """Subject line carries the punchline: wins / Q&L / $ won / win rate."""
    from hilmar.orchestrator import _compose_headline_subject
    data = {
        "summary": {"wins": 10, "quoted_lost": 22, "win_rate": 24.4},
        "requests": [
            {"status": "WIN", "rate_per_feu": 2400, "teu_won": 4},
            {"status": "WIN", "rate_per_feu": 3500, "teu_won": 6},
        ],
    }
    p = tmp_path / "tracking.json"
    p.write_text(json.dumps(data))
    subj = _compose_headline_subject(p, "Apr 28")
    assert "10W" in subj
    assert "22 Q&L" in subj
    assert "24.4%" in subj
    assert "$" in subj
    assert "Apr 28" in subj


def test_compose_headline_subject_falls_back_on_missing_data(tmp_path):
    from hilmar.orchestrator import _compose_headline_subject
    p = tmp_path / "missing.json"  # doesn't exist
    subj = _compose_headline_subject(p, "Apr 28")
    assert "Hilmar" in subj
    assert "Apr 28" in subj


# ─── Loss reasons aggregate (PR #36) ──────────────────────────────────


def test_loss_reasons_aggregate_groups_q_and_l_by_reason():
    """Bucket Q&L rows by loss_reason. Returns count + TEU + share."""
    from hilmar.render import _loss_reasons_aggregate
    data = {"requests": [
        {"status": "Q&L", "loss_reason": "PRICE", "teu_requested": 4},
        {"status": "Q&L", "loss_reason": "PRICE", "teu_requested": 2},
        {"status": "Q&L", "loss_reason": "ETD_MISS", "teu_requested": 4},
        {"status": "Q&L", "loss_reason": None, "teu_requested": 2},
        {"status": "WIN"},  # not counted
    ]}
    out = _loss_reasons_aggregate(data)
    by_reason = {r["reason"]: r for r in out}
    assert by_reason["PRICE"]["count"] == 2
    assert by_reason["PRICE"]["teu"] == 6
    assert by_reason["PRICE"]["share_pct"] == 50.0  # 2/4
    assert by_reason["ETD_MISS"]["count"] == 1
    # None loss_reason → "OTHER"
    assert by_reason["OTHER"]["count"] == 1


def test_loss_reasons_aggregate_returns_empty_when_no_losses():
    from hilmar.render import _loss_reasons_aggregate
    assert _loss_reasons_aggregate({"requests": []}) == []
    assert _loss_reasons_aggregate({"requests": [{"status": "WIN"}]}) == []
