"""Reading Linda's export without transcribing it by hand.

Michael, 2026-08-13: "linda only ran a partial report... i'll have a run a
year long report.. and get it to you shortly."

35 rows were typed in by hand. A year of bookings cannot be, and a mistyped
MDOLX in that file becomes a WIN in the tracker that never happened — the
exact failure this session spent itself removing. So the risks worth testing
are the ones that produce a WRONG booking silently: a column bound to the
wrong header, a date read as the wrong day, one booking counted once per
container line.
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import extract_ol_recap as X  # noqa: E402

NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def _xlsx(grid, sheets=1):
    """A real .xlsx, built with stdlib — inline strings, no shared table."""
    def col(i):
        s, i = "", i + 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    rows = []
    for ri, row in enumerate(grid, start=1):
        cells = []
        for ci, v in enumerate(row):
            if v == "":
                continue
            if isinstance(v, (int, float)):
                cells.append(f'<c r="{col(ci)}{ri}"><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{col(ci)}{ri}" t="inlineStr">'
                             f"<is><t>{v}</t></is></c>")
        rows.append(f'<row r="{ri}">{"".join(cells)}</row>')
    sheet = f"<worksheet {NS}><sheetData>{''.join(rows)}</sheetData></worksheet>"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in range(1, sheets + 1):
            zf.writestr(f"xl/worksheets/sheet{n}.xml", sheet)
    buf.seek(0)
    return buf


HEAD = ["MDOLX #", "Customer", "Carrier", "POL", "POD", "Booking #", "ETD", "TEU"]


def _row(ref="261046", cust="HILMAR INGREDIENTS", pod="YOKOHAMA,JAPAN",
         bkg="NAM1234", etd=46261, teu=2):
    return [ref, cust, "CMA CGM", "OAKLAND,CA", pod, bkg, etd, teu]


# ── the grid → records path ──────────────────────────────────────────────

def test_a_normal_export_extracts_its_bookings():
    recs, dropped, labels = X.extract([HEAD, _row(), _row("261047")])
    assert [r["mdolx"] for r in recs] == ["261046", "261047"]
    assert recs[0]["carrier"] == "CMA CGM" and recs[0]["pod"] == "YOKOHAMA,JAPAN"
    assert not dropped


def test_columns_bind_by_header_not_by_position():
    """The year-long export is the same report over a longer range, but
    column ORDER is exactly what quietly differs between two exports."""
    head = ["ETD", "POD", "Booking #", "MDOLX #", "Carrier", "POL"]
    row = [46356, "KOBE,JAPAN", "NAM9", "MDOLX261050", "ONE", "OAKLAND,CA"]
    recs, _, _ = X.extract([head, row])
    assert recs[0]["mdolx"] == "261050"
    assert recs[0]["pod"] == "KOBE,JAPAN" and recs[0]["carrier"] == "ONE"


def test_the_header_row_is_found_below_title_rows():
    """Exports carry a title and a filter line above the real header."""
    grid = [["Container Report With TEU By Container Size"], [], ["Jun 1 - Aug 12"],
            HEAD, _row()]
    recs, _, _ = X.extract(grid)
    assert [r["mdolx"] for r in recs] == ["261046"]


def test_a_missing_mdolx_column_is_a_hard_error_that_shows_the_headers():
    """Silently emitting zero bookings would read as 'OL booked nothing',
    which is a worse lie than crashing."""
    with pytest.raises(LookupError) as e:
        X.extract([["Customer", "Carrier", "POD"], ["HILMAR", "ONE", "KOBE,JAPAN"]])
    assert "Customer" in str(e.value)


# ── the ways a wrong booking gets in ─────────────────────────────────────

def test_one_booking_per_mdolx_even_when_split_by_container_size():
    """The report is 'By Container Size': one booking of 2x20 and 1x40 is
    two lines. Counting both would inflate the win count."""
    recs, dropped, _ = X.extract([HEAD, _row(), _row(), _row("261047")])
    assert [r["mdolx"] for r in recs] == ["261046", "261047"]
    assert any("duplicate" in why for _, why in dropped)


def test_an_unparseable_reference_is_reported_not_guessed():
    recs, dropped, _ = X.extract([HEAD, _row(ref="PENDING"), _row()])
    assert [r["mdolx"] for r in recs] == ["261046"]
    assert dropped and dropped[0][0] == "PENDING"


def test_a_customer_filter_keeps_only_hilmar_when_asked():
    """A year-long pull may span OL's whole book, not just this account."""
    recs, dropped, _ = X.extract(
        [HEAD, _row(), _row("261047", cust="SOME OTHER SHIPPER")],
        customer_filter="HILMAR")
    assert [r["mdolx"] for r in recs] == ["261046"]
    assert dropped and "does not contain" in dropped[0][1]


def test_without_a_filter_every_row_is_kept():
    recs, _, _ = X.extract([HEAD, _row(), _row("261047", cust="OTHER")])
    assert len(recs) == 2


def test_blank_rows_are_skipped_silently():
    recs, dropped, _ = X.extract([HEAD, _row(), ["", "", "", ""], _row("261047")])
    assert len(recs) == 2 and not dropped


# ── dates, where an off-by-one changes which request matches ─────────────

@pytest.mark.parametrize("raw,want", [
    (46261, "2026-08-27"),          # Excel serial
    ("2026-08-27", "2026-08-27"),   # already ISO
    ("8/27/2026", "2026-08-27"),    # US text
    ("08-27-26", "2026-08-27"),
    ("", ""),                       # blank stays blank
    ("TBD", ""),                    # unparseable stays blank, never today()
])
def test_dates_parse_or_stay_empty(raw, want):
    assert X.parse_date(raw) == want


def test_the_excel_epoch_is_the_1900_bug_one():
    """44927 is 2023-01-01 in Excel. Using 1899-12-31 shifts every booking
    a day, which silently changes which request it can match."""
    assert X.parse_date(44927) == "2023-01-01"


def test_a_row_with_no_date_survives_with_an_empty_one():
    """backfill_ol_bookings already reports 'no usable date' and refuses to
    match it — dropping the row here would hide the booking entirely."""
    recs, _, _ = X.extract([HEAD, _row(etd="")])
    assert recs[0]["sheet_date"] == ""


# ── the xlsx container itself ────────────────────────────────────────────

def test_it_reads_a_real_xlsx_file(tmp_path):
    p = tmp_path / "export.xlsx"
    p.write_bytes(_xlsx([HEAD, _row(), _row("261047")]).getvalue())
    rows = X.read_xlsx(p)
    recs, _, _ = X.extract(rows)
    assert [r["mdolx"] for r in recs] == ["261046", "261047"]


def test_shared_strings_are_resolved(tmp_path):
    """Excel itself writes a shared-string table; only our test fixture uses
    inline strings, so the shared path needs its own proof."""
    sheet = (f'<worksheet {NS}><sheetData>'
             '<row r="1"><c r="A1" t="s"><v>0</v></c>'
             '<c r="B1" t="s"><v>1</v></c></row>'
             '<row r="2"><c r="A2" t="s"><v>2</v></c>'
             '<c r="B2" t="s"><v>3</v></c></row>'
             "</sheetData></worksheet>")
    shared = ('<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
              '2006/main"><si><t>MDOLX #</t></si><si><t>POD</t></si>'
              "<si><t>MDOLX261046</t></si><si><t>KOBE,JAPAN</t></si></sst>")
    p = tmp_path / "shared.xlsx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        zf.writestr("xl/sharedStrings.xml", shared)
    recs, _, _ = X.extract(X.read_xlsx(p))
    assert recs[0]["mdolx"] == "261046" and recs[0]["pod"] == "KOBE,JAPAN"


def test_a_gap_in_the_row_does_not_shift_the_columns(tmp_path):
    """Excel omits empty cells entirely, so position in the XML is not the
    column — only the cell reference is. Getting this wrong slides POD into
    POL for every row that has a blank."""
    sheet = (f'<worksheet {NS}><sheetData>'
             '<row r="1"><c r="A1" t="inlineStr"><is><t>MDOLX #</t></is></c>'
             '<c r="C1" t="inlineStr"><is><t>POD</t></is></c></row>'
             '<row r="2"><c r="A2" t="inlineStr"><is><t>261046</t></is></c>'
             '<c r="C2" t="inlineStr"><is><t>KOBE,JAPAN</t></is></c></row>'
             "</sheetData></worksheet>")
    p = tmp_path / "gap.xlsx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    recs, _, _ = X.extract(X.read_xlsx(p))
    assert recs[0]["pod"] == "KOBE,JAPAN"


def test_column_letters_past_z_resolve():
    assert X.col_index("A1") == 0 and X.col_index("Z9") == 25
    assert X.col_index("AA1") == 26 and X.col_index("AB7") == 27


# ── it produces what the downstream tools already consume ────────────────

def test_the_output_matches_the_stored_recap_schema():
    """backfill_ol_bookings and diag_reconcile read the Jun-Aug file; the
    year-long one has to be the same shape or both break on arrival."""
    stored = json.loads((ROOT / "data" /
                         "ol-booking-recap-2026-06-01_2026-08-12.json"
                         ).read_text(encoding="utf-8"))
    recs, _, _ = X.extract([HEAD, _row()])
    assert set(stored[0]) <= set(recs[0]), (
        "the extractor drops a key the stored recap has")


def test_extracted_rows_feed_the_backfill_matcher():
    import backfill_ol_bookings as B
    import core
    recs, _, _ = X.extract([HEAD, _row(etd="2026-08-27")])
    m, un, sk = B.propose(recs, [{"request_id": "r1", "destination": "Yokohama",
                                  "lane": "Oakland → Yokohama", "status": "LOSS",
                                  "request_timestamp": "2026-08-05T15:00:00Z"}],
                          "2026-07-01", 60, core)
    assert len(m) == 1 and m[0][0] == "261046"


def test_it_reproduces_the_hand_transcribed_recap():
    """The Jun-Aug file was typed in by hand and then reconciled to 35/35, so
    it is the only ground truth available — the original .xlsx is not in the
    repo. Rendering it back to a grid and re-extracting must return the same
    35 bookings, or the extractor and the file the corrections were built
    from disagree about what OL booked.
    """
    stored = json.loads((ROOT / "data" /
                         "ol-booking-recap-2026-06-01_2026-08-12.json"
                         ).read_text(encoding="utf-8"))
    head = ["MDOLX #", "Carrier", "POL", "POD", "Booking #", "ETD"]
    grid = [head] + [[r["mdolx"], r["carrier"], r["pol"], r["pod"],
                      r["booking_no"], r["sheet_date"]] for r in stored]
    out, dropped, _ = X.extract(grid)
    assert not dropped
    assert len(out) == len(stored) == 35
    for want, got in zip(stored, out, strict=True):
        # MDOLX261071 is the sparse row: carrier is null there and "" here.
        # Both are falsy and backfill_ol_bookings gates on truthiness, so the
        # two files describe the same booking — that equivalence is the thing
        # being pinned, not the literal type.
        assert got["mdolx"] == want["mdolx"]
        for k in ("pol", "pod", "booking_no", "sheet_date"):
            assert got[k] == want[k], f"{want['mdolx']} {k}"
        assert (got["carrier"] or None) == (want["carrier"] or None)


# ── OL's transaction report: a second export with none of the same headers ──

TXN_HEAD = ["module", "number", "date", "customer", "office", "officetiny",
            "customerreference", "equipment", "origin", "loadport",
            "transshipmentport", "dischargeport", "destination", "carrier",
            "vessel", "voyage", "groupfield", "sortfield", "cancelled", "teu",
            "profit"]


def _txn(num="MDOLX260716", pod="YOKOHAMA", dest="", cancelled="No", teu=2.0,
         date=46261.0, cust="HILMAR CHEESE COMPANY"):
    return ["Sea", num, date, cust, "OL MANHATTAN - MBD", "OLWBMBD", "",
            "20' DV", "", "OAKLAND", "OAKLAND", pod, dest, "CMA CGM SA",
            "VSL", "V1", "", "", cancelled, teu, 275.0]


def test_the_transaction_report_binds_every_column_it_needs():
    """Michael, 2026-08-13, sent transactionreport_20260813083250.xls — the
    COMPLETE 2026 book. It shares not one header spelling with Linda's
    container report: number/date/loadport/dischargeport against
    MDOLX #/ETD/POL/POD."""
    recs, dropped, labels = X.extract([TXN_HEAD, _txn()])
    assert labels["mdolx"] == "number"
    assert labels["pol"] == "loadport" and labels["pod"] == "dischargeport"
    assert labels["sheet_date"] == "date" and labels["teu"] == "teu"
    assert recs[0]["mdolx"] == "260716" and recs[0]["pod"] == "YOKOHAMA"


def test_the_mdolx_column_is_confirmed_by_its_VALUES():
    """'number' is not in any alias list and could not be added safely — it
    is a word inside 'Booking Number' too. The values are what identify the
    column, and the rebinding is reported, not silent."""
    recs, dropped, _ = X.extract([TXN_HEAD, _txn(), _txn("MDOLX260718")])
    assert [r["mdolx"] for r in recs] == ["260716", "260718"]
    assert any(ref == "(binding)" and "bound by content" in why
               for ref, why in dropped)


def test_a_header_bound_column_that_holds_no_mdolx_is_rebound():
    """The dangerous case: a sheet whose 'File No' column holds something
    else entirely. Trusting the header would emit 134 wrong references."""
    head = ["File No", "Ref"]
    rows = [head] + [[f"LINE-{i}", f"MDOLX26{i:04d}"] for i in range(1, 9)]
    recs, dropped, labels = X.extract(rows)
    assert [r["mdolx"] for r in recs] == [f"26{i:04d}" for i in range(1, 9)]
    assert any("rebound by content" in why for _, why in dropped)


def test_discharge_port_wins_over_the_inland_destination():
    """Cai Mep is the discharge port; Ho Chi Minh City is the inland move
    after it. The rate was quoted on the discharge port, so binding
    'destination' to POD would put every Vietnam booking on the wrong lane."""
    recs, _, _ = X.extract([TXN_HEAD, _txn(pod="CAI MEP",
                                           dest="HO CHI MINH CITY, VIETNAM")])
    assert recs[0]["pod"] == "CAI MEP"
    assert recs[0]["final_destination"] == "HO CHI MINH CITY, VIETNAM"


def test_an_inland_leg_equal_to_the_port_is_not_repeated():
    recs, _, _ = X.extract([TXN_HEAD, _txn(pod="YOKOHAMA", dest="YOKOHAMA")])
    assert "final_destination" not in recs[0]


def test_a_cancelled_booking_never_becomes_a_win():
    """Every row in the 2026 export reads cancelled=No, which means the
    column exists and can read Yes. A cancelled booking counted as a win is
    a win that did not happen."""
    recs, dropped, _ = X.extract([TXN_HEAD, _txn(), _txn("MDOLX260718",
                                                        cancelled="Yes")])
    assert [r["mdolx"] for r in recs] == ["260716"]
    assert any("cancelled" in why for _, why in dropped)


def test_profit_is_not_extracted():
    """The export carries OL's margin per shipment. It is not needed to
    reconcile a booking and it is not going into a file in this repo."""
    recs, _, _ = X.extract([TXN_HEAD, _txn()])
    assert "profit" not in recs[0]
    stored = json.loads((ROOT / "data" / "ol-transaction-report-2026.json"
                         ).read_text(encoding="utf-8"))
    assert not any("profit" in r for r in stored), (
        "OL's margin was committed to the repo")


def test_the_stored_2026_export_is_what_michael_sent():
    """134 Hilmar bookings, 2026 sailings, none cancelled — the file the
    reconciliation is run against, pinned so a re-extract that changes it
    fails here rather than silently moving the win count."""
    rows = json.loads((ROOT / "data" / "ol-transaction-report-2026.json"
                       ).read_text(encoding="utf-8"))
    assert len(rows) == 134
    assert all(r["customer"] == "HILMAR CHEESE COMPANY" for r in rows)
    dates = sorted(r["sheet_date"] for r in rows if r["sheet_date"])
    assert dates[0] == "2026-01-03" and dates[-1] == "2026-09-05"
    refs = {r["mdolx"] for r in rows}
    # The 13 of Linda's 15 "missing" wins this export confirms are real.
    for ref in ("260716", "260718", "260719", "260720", "260721", "260722",
                "260723", "260748", "260770", "260809", "260811", "260833",
                "260842"):
        assert ref in refs, f"{ref} vanished from the export"


def test_it_routes_a_legacy_xls_to_the_biff_reader(tmp_path, monkeypatch):
    """OL's export is a real OLE/BIFF .xls, not a renamed zip. Routing is on
    the magic bytes rather than the extension, because an export named .xls
    that is really HTML is a common enough trap to be worth not falling in."""
    p = tmp_path / "report.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    monkeypatch.setattr(X, "read_xls", lambda path, sheet=0: [["routed"]])
    assert X.read_any(p) == [["routed"]]


def test_the_xls_branch_says_what_to_install_when_xlrd_is_absent(tmp_path,
                                                                monkeypatch):
    """The runner has no xlrd. An ImportError traceback would read as a bug
    in this script rather than a missing package."""
    p = tmp_path / "report.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    import builtins
    real = builtins.__import__

    def no_xlrd(name, *a, **kw):
        if name == "xlrd":
            raise ImportError("no xlrd")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_xlrd)
    with pytest.raises(LookupError) as e:
        X.read_xls(p)
    assert "pip install xlrd" in str(e.value)


def test_an_unknown_container_says_what_it_saw(tmp_path):
    p = tmp_path / "report.xls"
    p.write_bytes(b"<html><table>")
    with pytest.raises(LookupError) as e:
        X.read_any(p)
    assert "neither" in str(e.value)


def test_a_zip_is_routed_to_the_xlsx_reader(tmp_path):
    p = tmp_path / "export.xlsx"
    p.write_bytes(_xlsx([HEAD, _row()]).getvalue())
    recs, _, _ = X.extract(X.read_any(p))
    assert recs[0]["mdolx"] == "261046"


def test_it_writes_nothing_without_out(tmp_path):
    src = (ROOT / "scripts" / "extract_ol_recap.py").read_text(encoding="utf-8")
    assert "if args.out:" in src
    i = src.find("Path(args.out).write_text")
    assert i > src.find("if args.out:"), "a write happens outside the --out guard"


# ── ETS / ETA: the dates the client report's ETD and ETA columns want ────

def test_ets_and_eta_are_captured_as_their_own_fields():
    """Michael, 2026-08-13, on a client report showing "—" in ETD/ETA for 11
    of 14 bookings: "LOTS OF DATA MISSING AND YOU HAD IT ALL ON THE REPORT."
    He was right — OL's customer transaction report carries ETS and ETA for
    132 of its 134 bookings, in columns separate from `date`."""
    rows = json.loads((ROOT / "data" / "ol-transaction-report-2026.json"
                       ).read_text(encoding="utf-8"))
    with_ets = [r for r in rows if r.get("ets")]
    with_eta = [r for r in rows if r.get("eta_date")]
    assert len(with_ets) == 132, len(with_ets)
    assert len(with_eta) == 132, len(with_eta)


def test_ets_binds_only_when_it_is_its_own_column():
    """Placement matters. sheet_date's alias list starts with "etd", and on
    Linda's container report the ONLY date column is headed ETD. If `ets`
    claimed it first, sheet_date would go unbound and every downstream date
    would break."""
    head = ["MDOLX #", "Carrier", "POL", "POD", "Booking #", "ETD"]
    row = ["261046", "ONE", "OAKLAND", "YOKOHAMA", "NAM1", "2026-08-27"]
    recs, _, labels = X.extract([head, row])
    assert labels["sheet_date"] == "ETD"
    assert "ets" not in labels, "ets stole the only date column"
    assert recs[0]["sheet_date"] == "2026-08-27"


def test_a_separate_ets_column_does_not_disturb_sheet_date():
    head = ["number", "date", "ets", "eta", "pod"]
    row = ["MDOLX252071", "2026-01-03", "2026-01-05", "2026-01-27", "CAI MEP"]
    recs, _, labels = X.extract([head, row])
    assert recs[0]["sheet_date"] == "2026-01-03"
    assert recs[0]["ets"] == "2026-01-05"
    assert recs[0]["eta_date"] == "2026-01-27"


def test_an_unparseable_sailing_date_is_omitted_not_blanked():
    """The two cancelled rows carry "  -   -  : :" in ETS/ETA. A key present
    with an empty value reads as "we looked and there is none"; omitting it
    says the export had nothing."""
    head = ["number", "date", "ets", "eta"]
    recs, _, _ = X.extract([head, ["MDOLX260192", "2026-02-25", "  -   -  : :", ""]])
    assert "ets" not in recs[0] and "eta_date" not in recs[0]
