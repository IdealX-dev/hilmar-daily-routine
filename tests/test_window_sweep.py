"""Michael, 2026-08-12: "they are in my mailbox ... where they always have
been since day one".

They were. The pipeline could not see them, and my previous conclusion —
"the replies are not in the mailbox we read" — was wrong. What the
verification fire's own log actually said:

    query 'lonny-flow':       got 275 results
    query 'hilmar-bookings':  got 275 results
    query 'ol-quote-senders': got 275 results

Three semantically unrelated queries cannot each match exactly 275 messages.
Graph stopped paginating $search at a service-side ceiling BELOW our 500 cap,
so `truncated` stayed False and _warn_search_cap never fired. And $search
ranks by RELEVANCE and cannot be combined with $orderby, so the slice we kept
was arbitrary — 357 of the 599 unique results were pre-cutoff, i.e. the
ranker handed back mostly OLD mail and dropped the current week.

That is why it "worked weeks ago": while fewer than ~275 messages matched,
the slice covered everything including today. As the mailbox grew past the
ceiling the coverage rotted from the newest end — invisibly, because no
count ever looked wrong.

The fix is to stop asking a ranker for the truth. $filter on
receivedDateTime + $orderby is date-ordered, deterministic and complete for
the window: pagination ends because the window ends.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import refresh_stage as RS  # noqa: E402

SRC = (ROOT / "scripts" / "refresh_stage.py").read_text(encoding="utf-8")
SINCE = "2026-07-22T00:00:00+00:00"


class _FakeGraph:
    """Records the request Graph would have received, and pages a fixed set."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, token, url, params=None):
        self.calls.append((url, params))
        return self.pages.pop(0) if self.pages else {"value": []}


def test_the_sweep_asks_by_date_not_by_relevance(monkeypatch):
    """THE defect, as a request assertion: no $search, an explicit
    receivedDateTime lower bound, and newest-first ordering."""
    fake = _FakeGraph([{"value": [{"id": "1"}]}])
    monkeypatch.setattr(RS, "graph_get", fake)
    RS.list_messages_since("tok", SINCE, base="https://g/me")
    _url, params = fake.calls[0]
    assert "$search" not in params, (
        "the window sweep still uses $search — it will be relevance-ranked "
        "and silently ceilinged, which is the whole defect")
    assert params["$filter"] == f"receivedDateTime ge {SINCE}"
    assert params["$orderby"] == "receivedDateTime desc", (
        "without an explicit date order the page sequence is the server's "
        "choice again")


def test_the_sweep_follows_every_page_to_the_end_of_the_window(monkeypatch):
    """Completeness is the entire point: pagination must stop because the
    window ran out, not because a page limit did."""
    fake = _FakeGraph([
        {"value": [{"id": "1"}, {"id": "2"}], "@odata.nextLink": "https://g/p2"},
        {"value": [{"id": "3"}], "@odata.nextLink": "https://g/p3"},
        {"value": [{"id": "4"}]},
    ])
    monkeypatch.setattr(RS, "graph_get", fake)
    got = RS.list_messages_since("tok", SINCE, base="https://g/me")
    assert [m["id"] for m in got] == ["1", "2", "3", "4"]
    assert len(fake.calls) == 3
    assert fake.calls[1][1] is None, (
        "params were re-sent on a nextLink — Graph rejects that, and the "
        "nextLink already carries the query")


def test_the_guard_is_a_runaway_stop_not_a_business_limit(monkeypatch, capsys):
    """A cap that silently truncates is how this defect stayed hidden for
    weeks. If the guard ever binds it must SAY so."""
    fake = _FakeGraph([
        {"value": [{"id": str(i), "receivedDateTime": "2026-08-11T00:00:00Z"}
                   for i in range(100)], "@odata.nextLink": "https://g/p2"},
        {"value": [{"id": "x", "receivedDateTime": "2026-08-10T00:00:00Z"}]},
    ])
    monkeypatch.setattr(RS, "graph_get", fake)
    got = RS.list_messages_since("tok", SINCE, base="https://g/me", max_results=100)
    assert len(got) == 100
    out = capsys.readouterr()
    assert "guard" in (out.out + out.err).lower(), (
        "the sweep hit its guard and said nothing — a silent ceiling, the "
        "exact failure being fixed")


def test_it_reports_how_far_back_it_actually_REACHED(monkeypatch, capsys):
    """The first live run read 4000 messages and stopped — newest-first, so
    it never reached Jul 29-31 and W31 stayed empty for a THIRD reason. A
    count cannot show that; the oldest date reached can. Coverage, not
    volume, is the number that says whether the window was read."""
    fake = _FakeGraph([{"value": [
        {"id": "1", "receivedDateTime": "2026-08-12T10:00:00Z"},
        {"id": "2", "receivedDateTime": "2026-08-09T10:00:00Z"},
    ]}])
    monkeypatch.setattr(RS, "graph_get", fake)
    RS.list_messages_since("tok", SINCE, base="https://g/me", max_results=999)
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "2026-08-09" in combined, "the oldest message reached is not reported"
    assert "did NOT reach the window floor" in combined, (
        "the sweep stopped 18 days short of the requested window and did not "
        "say so — exactly the silence that hid the $search ceiling")


def test_full_coverage_of_the_window_is_not_flagged(monkeypatch, capsys):
    """The warning must mean something: reaching the floor is silent."""
    fake = _FakeGraph([{"value": [
        {"id": "1", "receivedDateTime": "2026-08-12T10:00:00Z"},
        {"id": "2", "receivedDateTime": "2026-07-21T10:00:00Z"},   # past the floor
    ]}])
    monkeypatch.setattr(RS, "graph_get", fake)
    RS.list_messages_since("tok", SINCE, base="https://g/me", max_results=999)
    out = capsys.readouterr()
    assert "did NOT reach" not in (out.out + out.err), (
        "a fully-read window raised a false alarm — a warning that fires on "
        "success is one people learn to ignore")


def test_the_page_size_is_large_enough_to_finish(monkeypatch):
    """At 100/page the live sweep spent two minutes and still fell short.
    Graph allows up to 1000."""
    fake = _FakeGraph([{"value": []}])
    monkeypatch.setattr(RS, "graph_get", fake)
    RS.list_messages_since("tok", SINCE, base="https://g/me")
    assert int(fake.calls[0][1]["$top"]) >= 500


def test_the_sweep_is_the_primary_intake_and_search_is_supplementary():
    """Ordering matters: the complete source must populate the set first, so
    a $search slice can only ADD older mail, never define the window."""
    i_sweep = SRC.find("PRIMARY INTAKE")
    i_search = SRC.find("for label, kql in queries:")
    assert 0 < i_sweep < i_search, (
        "the $search loop runs before the date sweep — the window is being "
        "defined by the ranker again")
    assert "list_messages_since(mtoken, cutoff.isoformat()" in SRC


def test_identical_search_counts_are_reported_as_a_service_ceiling():
    """The detector that failed. _warn_search_cap only fires at OUR cap;
    Graph's ceiling sits below it. Three queries returning exactly 275 is the
    fingerprint, and nothing said a word."""
    assert "each returned exactly" in SRC
    assert "_search_counts" in SRC, (
        "per-query counts are not retained, so the identical-count "
        "fingerprint cannot be detected")


def test_a_failed_sweep_is_loud_rather_than_a_quiet_fallback():
    """Falling back to $search alone is falling back to the defect. That is
    sometimes the only option, but it must never be silent."""
    i = SRC.find("date sweep FAILED")
    assert i > 0
    assert "::error::" in SRC[max(0, i - 200):i + 200], (
        "a failed sweep degrades to $search without an error annotation")


def test_the_window_guard_is_dispatchable():
    assert "--max-window-messages" in SRC
    import argparse
    assert isinstance(RS.main.__doc__, (str, type(None)))
    del argparse
