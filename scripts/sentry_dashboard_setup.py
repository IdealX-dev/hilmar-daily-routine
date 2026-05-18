"""
sentry_dashboard_setup.py — One-shot to provision the Hilmar KPI dashboard
in Sentry via the REST API.

Per Michael 2026-05-17 ("you can use sentry for self check and improvements
as well"). Instead of clicking 8+ widgets in the Sentry UI, this script
creates the full dashboard with one API call. Re-running is idempotent —
it deletes the existing "Hilmar Daily Tracker — KPIs" dashboard first.

USAGE
  python scripts/sentry_dashboard_setup.py            # create / replace
  python scripts/sentry_dashboard_setup.py --dry      # show payload, don't create

WIDGETS

Row 1 — Parser quality
  1. Parser Accuracy (overall) — line chart, 90d
  2. Parser Accuracy per Field — table, latest values
Row 2 — Pipeline performance
  3. Pipeline Duration p50/p95 — line chart
  4. Step Duration Heatmap — by step name
Row 3 — QC + reconcile
  5. QC Errors by Check — stacked bar
  6. Reconcile Drift Trend — line chart
Row 4 — Send + Sentry health
  7. Send Success Rate — stat
  8. Sentry Issues Unresolved — stat

Schema reference:
  https://docs.sentry.io/api/dashboards/create-an-organization-dashboard/
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

DASHBOARD_TITLE = "Hilmar Daily Tracker — KPIs"


def _widget(title, display_type, queries, layout=None):
    """Build a widget dict per Sentry's dashboard widget schema."""
    w = {
        "title": title,
        "displayType": display_type,
        "interval": "1d",
        "widgetType": "discover",
        "queries": queries,
    }
    if layout:
        w["layout"] = layout
    return w


def _metric_query(name, alias, aggregate="avg", tags=None):
    """Helper for metric-based widget queries."""
    q = {
        "name": alias,
        "fields": [f"{aggregate}({name})"],
        "aggregates": [f"{aggregate}({name})"],
        "columns": [],
        "conditions": "",
        "orderby": "",
    }
    if tags:
        q["conditions"] = " ".join(f"{k}:{v}" for k, v in tags.items())
    return q


def build_widgets():
    """Return the list of widget configs for the Hilmar dashboard."""
    return [
        # Row 1: Parser quality
        _widget(
            "Parser Accuracy (Overall)",
            "line",
            [_metric_query("parser.accuracy_overall", "accuracy",
                           tags={"phase": "post-patch"})],
            layout={"x": 0, "y": 0, "w": 2, "h": 2, "minH": 2},
        ),
        _widget(
            "Parser Accuracy by Field (latest)",
            "table",
            [_metric_query("parser.accuracy_per_field", "field_rate",
                           tags={"phase": "post-patch"})],
            layout={"x": 2, "y": 0, "w": 2, "h": 2, "minH": 2},
        ),
        # Row 2: Pipeline performance
        _widget(
            "Pipeline Duration (p50 / p95)",
            "line",
            [_metric_query("pipeline.duration_s", "duration", "p50"),
             _metric_query("pipeline.duration_s", "duration_p95", "p95")],
            layout={"x": 0, "y": 2, "w": 2, "h": 2, "minH": 2},
        ),
        _widget(
            "Step Duration Heatmap",
            "bar",
            [_metric_query("pipeline.step_duration_s", "step_dur", "p95")],
            layout={"x": 2, "y": 2, "w": 2, "h": 2, "minH": 2},
        ),
        # Row 3: QC + reconcile
        _widget(
            "QC Errors by Check (counter)",
            "bar",
            [_metric_query("qc.errors", "errors", "sum")],
            layout={"x": 0, "y": 4, "w": 2, "h": 2, "minH": 2},
        ),
        _widget(
            "Reconcile Drift Trend",
            "line",
            [_metric_query("reconcile.drift_count", "drift")],
            layout={"x": 2, "y": 4, "w": 2, "h": 2, "minH": 2},
        ),
        # Row 4: Send + Sentry health
        _widget(
            "Send Success (counter)",
            "big_number",
            [_metric_query("send.success", "sends", "sum")],
            layout={"x": 0, "y": 6, "w": 2, "h": 1, "minH": 1},
        ),
        _widget(
            "Sentry Unresolved Issues",
            "big_number",
            [_metric_query("sentry.unresolved_count", "unresolved")],
            layout={"x": 2, "y": 6, "w": 2, "h": 1, "minH": 1},
        ),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="Show payload, don't create")
    args = ap.parse_args()

    from sentry_api import SentryAPI
    api = SentryAPI()
    if not api.enabled:
        print("⚠️  Sentry auth token not configured (secrets/sentry-auth-token.txt)")
        sys.exit(1)

    widgets = build_widgets()
    if args.dry:
        print(f"Would create dashboard '{DASHBOARD_TITLE}' with {len(widgets)} widgets:")
        for w in widgets:
            print(f"  - {w['title']} ({w['displayType']})")
        print()
        print("Full payload:")
        print(json.dumps({"title": DASHBOARD_TITLE, "widgets": widgets},
                          indent=2, default=str))
        return 0

    # Check if dashboard already exists; delete + recreate (idempotent)
    existing = api.list_dashboards()
    for d in existing:
        if d.get("title") == DASHBOARD_TITLE:
            print(f"📦 Existing dashboard found: {d.get('id')} — skipping recreate "
                  "(use Sentry UI to delete if you want to recreate)")
            return 0

    # Create it
    result = api.create_dashboard(DASHBOARD_TITLE, widgets)
    if result:
        dash_id = result.get("id")
        print(f"✅ Created dashboard '{DASHBOARD_TITLE}' (id={dash_id})")
        print(f"   View at: https://idealx-llc.sentry.io/dashboards/{dash_id}/")
        return 0
    else:
        print("❌ Dashboard creation failed — check sentry_api error output above")
        return 1


if __name__ == "__main__":
    sys.exit(main())
