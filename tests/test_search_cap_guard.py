"""refresh_stage.search_messages — 250/500-cap truncation visibility.

Graph KQL $search ranks by RELEVANCE, not date, and cannot be combined with
$orderby — so a result-cap hit drops an ARBITRARY (possibly recent) tail
silently. search_messages must detect a cap-hit-with-more-available and warn
loudly (the 2026-06-24 outage-audit finding). These tests stub graph_get so no
network/auth is needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_stage as RS  # noqa: E402


def _page(n, next_link=None):
    d = {"value": [{"id": f"m{i}", "internetMessageId": f"<{i}>"} for i in range(n)]}
    if next_link:
        d["@odata.nextLink"] = next_link
    return d


def test_no_warn_when_results_fit_under_cap(monkeypatch):
    # Single page, no nextLink -> everything fetched, no truncation.
    monkeypatch.setattr(RS, "graph_get", lambda *a, **k: _page(10))
    warned = {}
    monkeypatch.setattr(RS, "_warn_search_cap",
                        lambda *a, **k: warned.setdefault("hit", a))
    out = RS.search_messages("tok", "from:lonny", max_results=500)
    assert len(out) == 10
    assert "hit" not in warned


def test_warns_when_cap_hit_with_more_pages(monkeypatch):
    # Every page is full and always advertises a nextLink -> infinite supply;
    # search_messages must stop at the cap AND warn that it truncated.
    monkeypatch.setattr(RS, "graph_get",
                        lambda *a, **k: _page(50, next_link="https://next"))
    warned = {}
    monkeypatch.setattr(RS, "_warn_search_cap",
                        lambda kql, got, cap: warned.update(got=got, cap=cap))
    out = RS.search_messages("tok", "from:lonny", max_results=100)
    assert len(out) == 100          # stopped exactly at the cap
    assert warned == {"got": 100, "cap": 100}


def test_no_warn_when_last_page_exact_fill_no_nextlink(monkeypatch):
    # Exactly cap results, last page has no nextLink -> we got everything.
    pages = [_page(50, next_link="https://next"), _page(50)]  # 50 + 50 = 100, no more
    it = iter(pages)
    monkeypatch.setattr(RS, "graph_get", lambda *a, **k: next(it))
    warned = {}
    monkeypatch.setattr(RS, "_warn_search_cap",
                        lambda *a, **k: warned.setdefault("hit", True))
    out = RS.search_messages("tok", "from:lonny", max_results=100)
    assert len(out) == 100
    assert "hit" not in warned
