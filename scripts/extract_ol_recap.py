"""extract_ol_recap.py — Linda's booking export (.xlsx) → the recap JSON.

2026-08-13. Michael: "linda only ran a partial report... i'll have a run a
year long report.. and get it to you shortly."

WHY THIS EXISTS. The Jun 1 - Aug 12 export was transcribed into
data/ol-booking-recap-2026-06-01_2026-08-12.json by hand — 35 rows, which
was tolerable. A year of bookings is not, and hand-transcription is exactly
the step where a wrong MDOLX enters the tracker as a win that never
happened. This turns the spreadsheet into the JSON that diag_reconcile and
backfill_ol_bookings already read, in one command, deterministically.

STDLIB ONLY. openpyxl is not installed in the runner and this must work
without a network install; .xlsx is a zip of XML, so zipfile + ElementTree
is enough.

IT BINDS COLUMNS BY HEADER, AND SAYS WHAT IT BOUND. Column ORDER is the
thing most likely to differ between a two-month export and a year-long one,
so nothing here is positional. Each field is matched against a list of
header spellings, most specific first, and the binding is PRINTED — so a
wrong guess is visible in the run log rather than silent in the data. An
unbound required field is a hard error that dumps every header it saw,
which turns "the layout changed" into one run instead of a debugging
session.

WHAT IT WILL NOT DO. It does not invent an MDOLX, a port or a date: a row
whose reference will not parse is REPORTED and dropped, never guessed at.
Matching those references to Lonny's requests is backfill_ol_bookings' job
and stays conservative there; this step only reads.

NOTE ON sheet_date. In the Jun-Aug export this column ran 2026-06-27 to
2026-09-05 for a report titled "06-01-26 thru 08-12-26" — i.e. it is a
SAIL/ETD date, not the date the booking was made. backfill_ol_bookings
treats it as the upper bound on when the request could have been made,
which stays correct either way (a request precedes its sailing), but it is
the reason --max-age exists rather than an exact-date match.
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

#: Header spellings per output field, MOST SPECIFIC FIRST. The first alias
#: that matches any header claims the column, so "booking no" is tried
#: before the looser "booking", and the broad date words come last.
FIELD_ALIASES: dict[str, list[str]] = {
    "mdolx": ["mdolx", "mdolx no", "mdolx number", "file no", "file number",
              "shipment no", "shipment number", "job no", "reference no"],
    "booking_no": ["booking no", "booking number", "booking ref", "bkg no",
                   "bkg number", "carrier booking", "booking"],
    "carrier": ["carrier", "steamship line", "ssl", "scac", "vessel operator",
                "ocean carrier", "line"],
    "pol": ["pol", "port of load", "port of loading", "load port",
            "origin port", "origin"],
    # "discharge port" before "destination": OL's transaction report carries
    # BOTH, and they are different places — discharge is where the vessel
    # drops the box, destination is the inland move after it (Cai Mep vs Ho
    # Chi Minh City, Kobe vs Osaka). The lane a rate was quoted on is the
    # discharge port.
    "pod": ["pod", "port of discharge", "discharge port", "discharge",
            "port of destination", "destination port", "destination"],
    "final_destination": ["destination", "final destination", "place of delivery"],
    "sheet_date": ["etd", "sail date", "sailing date", "departure date",
                   "vessel etd", "on board date", "booking date", "date"],
    "teu": ["teu", "total teu", "teus"],
    "customer": ["customer", "customer name", "shipper", "consignee",
                 "account", "client"],
    # Michael has never asked for a cancelled booking to count as a win, and
    # a cancelled row reaching the tracker would be exactly that.
    "cancelled": ["cancelled", "canceled", "void", "status"],
}

#: Without an MDOLX a row cannot be reconciled against anything.
REQUIRED = ("mdolx",)

#: Cell values in a cancelled column that mean "this booking did not happen".
CANCELLED_TRUE = {"y", "yes", "true", "1", "cancelled", "canceled", "void",
                  "x"}

#: Excel's serial-date origin. 1900-01-00 in Excel's own terms; the
#: off-by-one comes from its deliberate 1900-leap-year bug, which is why
#: this is 1899-12-30 and not 1899-12-31.
EXCEL_EPOCH = date(1899, 12, 30)


def as_text(value) -> str:
    """Any cell as a string. Numeric cells arrive as ints from a hand-built
    grid and as strings from the XML; both have to normalize the same way."""
    return "" if value is None else str(value)


def normalize(text) -> str:
    """A header reduced to comparable words: lowercase, no punctuation."""
    lowered = as_text(text).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", lowered)).strip()


def col_index(ref: str) -> int:
    """0-based column from a cell reference ('A1' -> 0, 'AB7' -> 27).

    Cells for empty columns are omitted from the XML entirely, so position
    in the row is not the column — the reference is.
    """
    n = 0
    for ch in ref or "":
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    out = []
    for si in ET.fromstring(raw).findall(f"{NS}si"):
        # Runs (<r>) split a styled cell across several <t>; join them all.
        out.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
    return out


def cell_text(c: ET.Element, shared: list[str]) -> str:
    t = c.get("t")
    if t == "s":
        v = c.find(f"{NS}v")
        try:
            return shared[int(v.text)] if v is not None else ""
        except (ValueError, IndexError):
            return ""
    if t == "inlineStr":
        return "".join(x.text or "" for x in c.iter(f"{NS}t"))
    v = c.find(f"{NS}v")
    if v is not None:
        return v.text or ""
    return "".join(x.text or "" for x in c.iter(f"{NS}t"))


def sheet_rows(xml: bytes, shared: list[str]) -> list[list[str]]:
    """Worksheet XML → a rectangular grid of strings."""
    rows = []
    for r in ET.fromstring(xml).iter(f"{NS}row"):
        cells: dict[int, str] = {}
        for c in r.findall(f"{NS}c"):
            i = col_index(c.get("r") or "")
            if i >= 0:
                cells[i] = cell_text(c, shared)
        width = (max(cells) + 1) if cells else 0
        rows.append([cells.get(i, "") for i in range(width)])
    return rows


def find_header(rows: list[list[str]], scan: int = 25) -> int:
    """Index of the header row — exports carry title//filter rows above it.

    The header is the row that binds the most fields, not simply the first
    non-empty one; ties go to the earliest row.

    A row holding an actual MDOLX value is DATA and cannot be the header, no
    matter how many aliases its other cells happen to hit. Without that rule
    a first data row can outscore a sparse header — "LINE-1" binds carrier,
    "MDOLX260001" binds mdolx, two fields against a two-column header's one —
    and the real first booking is then eaten as the header.
    """
    best, best_score = -1, 0
    for i, row in enumerate(rows[:scan]):
        if any(parse_mdolx(c) for c in row):
            continue
        score = len(bind_headers(row)[0])
        if score > best_score:
            best, best_score = i, score
    return best


def bind_headers(header: list[str]) -> tuple[dict[str, int], dict[str, str]]:
    """(field -> column index, field -> the header text it bound to).

    Aliases are tried most-specific-first and a column is claimed once, so
    a sheet with both "POD" and "POD ETA" cannot bind the same column to
    two fields.
    """
    norm = [normalize(h) for h in header]
    # OL's transaction report writes headers with no separator at all —
    # "dischargeport", "loadport", "customerreference" — while Linda's
    # container report spaces them. Comparing both forms means one alias
    # list covers both exports instead of two.
    squashed = [h.replace(" ", "") for h in norm]
    bound: dict[str, int] = {}
    labels: dict[str, str] = {}
    taken: set[int] = set()
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            flat = alias.replace(" ", "")
            hit = next((i for i, h in enumerate(norm)
                        if h == alias and i not in taken), None)
            if hit is None:
                hit = next((i for i, h in enumerate(squashed)
                            if h == flat and i not in taken), None)
            if hit is None:
                hit = next((i for i, h in enumerate(norm)
                            if h and alias in h.split() and i not in taken), None)
            if hit is None:
                hit = next((i for i, h in enumerate(norm)
                            if h and alias in h and i not in taken), None)
            if hit is None:
                hit = next((i for i, h in enumerate(squashed)
                            if h and flat in h and i not in taken), None)
            if hit is not None:
                bound[field] = hit
                labels[field] = header[hit]
                taken.add(hit)
                break
    return bound, labels


def parse_mdolx(value) -> str | None:
    """The 6-digit reference, prefix and leading zeros tolerated."""
    m = re.search(r"(?:MDOLX\s*)?0*(\d{6})", as_text(value), re.I)
    return m.group(1) if m else None


def parse_date(value) -> str:
    """ISO date from an Excel serial or an already-formatted string.

    Unparseable input returns "" — backfill_ol_bookings reports a booking
    with no usable date rather than matching it, which is the behaviour we
    want over a guessed date.
    """
    s = as_text(value).strip()
    if not s:
        return ""
    try:
        return (EXCEL_EPOCH + timedelta(days=int(float(s)))).isoformat()
    except ValueError:
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", s)
    if m:
        mo, dy, yr = (int(x) for x in m.groups())
        yr += 2000 if yr < 100 else 0
        try:
            return date(yr, mo, dy).isoformat()
        except ValueError:
            return ""
    return ""


def mdolx_column(rows, header_i: int, bound: dict) -> tuple[int | None, str]:
    """The column that actually holds MDOLX references, and why.

    Header names are the fragile part: Linda's container report calls it
    "MDOLX #", OL's transaction report calls it "number", and "number" is a
    word that also appears in "Booking Number". The VALUES are not fragile —
    an MDOLX is a 6-digit reference and nothing else in these exports looks
    like one. So the header binding is a proposal and this is the check:
    whichever column parses as MDOLX on most rows wins, and a rebinding is
    reported rather than done quietly.
    """
    body = rows[header_i + 1:]
    if not body:
        return bound.get("mdolx"), "no data rows to verify against"
    width = max((len(r) for r in body), default=0)
    scores = []
    for i in range(width):
        hits = sum(1 for r in body if i < len(r) and parse_mdolx(r[i]))
        scores.append(hits / len(body))

    proposed = bound.get("mdolx")
    if proposed is not None and proposed < len(scores) and scores[proposed] >= 0.5:
        return proposed, ""
    best = max(range(len(scores)), key=lambda i: scores[i]) if scores else None
    if best is None or scores[best] < 0.5:
        return None, "no column parses as an MDOLX on most rows"
    if proposed is None:
        return best, (f"no MDOLX header matched; bound by content to column "
                      f"{best} ({rows[header_i][best]!r} if labelled)")
    return best, (f"header bound column {proposed} "
                  f"({rows[header_i][proposed]!r}) but only "
                  f"{scores[proposed]:.0%} of its values parse as an MDOLX; "
                  f"rebound by content to column {best} "
                  f"({rows[header_i][best]!r})")


def extract(rows, customer_filter=None):
    """(records, dropped, labels) — pure, so the tests drive it directly."""
    hdr_i = find_header(rows)
    if hdr_i < 0:
        return [], [("-", "no header row bound any known column")], {}
    bound, labels = bind_headers(rows[hdr_i])

    col, why = mdolx_column(rows, hdr_i, bound)
    if col is None:
        raise LookupError(
            "no MDOLX column found, by header or by content. Headers seen: "
            + ", ".join(repr(h) for h in rows[hdr_i] if h))
    if col != bound.get("mdolx"):
        bound["mdolx"] = col
        labels["mdolx"] = rows[hdr_i][col] if col < len(rows[hdr_i]) else f"col {col}"
    rebind_note = why

    missing = [f for f in REQUIRED if f not in bound]
    if missing:
        raise LookupError(
            f"required column(s) {missing} not found. Headers seen: "
            + ", ".join(repr(h) for h in rows[hdr_i] if h))

    def cell(row, field):
        i = bound.get(field)
        return (as_text(row[i]).strip() if i is not None and i < len(row) else "")

    want = normalize(customer_filter) if customer_filter else None
    records, dropped = [], []
    for row in rows[hdr_i + 1:]:
        if not any(as_text(c).strip() for c in row):
            continue
        raw_ref = cell(row, "mdolx")
        ref = parse_mdolx(raw_ref)
        if not ref:
            if raw_ref:
                dropped.append((raw_ref, "no 6-digit MDOLX in this cell"))
            continue
        if want:
            who = normalize(cell(row, "customer"))
            if want not in who:
                dropped.append((ref, f"customer {cell(row, 'customer')!r} "
                                     f"does not contain {customer_filter!r}"))
                continue
        if normalize(cell(row, "cancelled")) in CANCELLED_TRUE:
            dropped.append((ref, f"cancelled ({cell(row, 'cancelled')!r})"))
            continue
        rec = {
            "mdolx": ref,
            "carrier": cell(row, "carrier"),
            "pol": cell(row, "pol"),
            "pod": cell(row, "pod"),
            "booking_no": cell(row, "booking_no"),
            "sheet_date": parse_date(cell(row, "sheet_date")),
        }
        # The inland leg after discharge (Cai Mep -> Ho Chi Minh City). Kept
        # only when it differs, because a request quoted to the inland name
        # will not match the discharge port and the matcher needs both to
        # try.
        final = cell(row, "final_destination")
        if final and final.upper() != rec["pod"].upper():
            rec["final_destination"] = final
        if "teu" in bound and cell(row, "teu"):
            rec["teu"] = cell(row, "teu")
        if "customer" in bound and cell(row, "customer"):
            rec["customer"] = cell(row, "customer")
        records.append(rec)

    # One booking per MDOLX. A year-long export repeats a reference once per
    # container line, and every one of those repeats would otherwise become
    # a separate "win" to reconcile.
    seen: dict[str, dict] = {}
    for r in records:
        if r["mdolx"] in seen:
            dropped.append((r["mdolx"], "duplicate row for this MDOLX (kept the first)"))
            continue
        seen[r["mdolx"]] = r
    if rebind_note:
        dropped.insert(0, ("(binding)", rebind_note))
    return list(seen.values()), dropped, labels


def read_xls(path: Path, sheet: int = 0) -> list[list[str]]:
    """Legacy BIFF .xls — OL's transaction report is exported in this format.

    xlrd is imported here rather than at module scope so the .xlsx path
    keeps working with no third-party dependency at all; only someone
    handing this an .xls pays for it, and they get a usable message instead
    of an ImportError traceback.
    """
    try:
        import xlrd
    except ImportError as e:
        raise LookupError(
            f"{path.name} is a legacy .xls (OLE/BIFF), which needs xlrd: "
            "pip install xlrd. Re-saving it as .xlsx also works.") from e
    book = xlrd.open_workbook(str(path))
    if sheet >= book.nsheets:
        raise LookupError(f"--sheet {sheet} but the file has {book.nsheets}")
    sh = book.sheet_by_index(sheet)
    return [[sh.cell_value(r, c) for c in range(sh.ncols)]
            for r in range(sh.nrows)]


def read_any(path: Path, sheet: int = 0) -> list[list[str]]:
    """Either spreadsheet format, chosen by what the file actually is.

    Not by extension: OL's export is named .xls and IS one, but exports that
    are really HTML or CSV under an .xls name are common enough that the
    magic bytes are the honest test.
    """
    head = path.read_bytes()[:8]
    if head[:4] == b"PK\x03\x04":
        return read_xlsx(path, sheet)
    if head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return read_xls(path, sheet)
    raise LookupError(
        f"{path.name} is neither a .xlsx (zip) nor a legacy .xls (OLE) — "
        f"first bytes {head!r}. If it is really HTML or CSV renamed to .xls, "
        "open it and re-save as .xlsx.")


def read_xlsx(path: Path, sheet: int = 0) -> list[list[str]]:
    with zipfile.ZipFile(path) as zf:
        shared = shared_strings(zf)
        names = sorted(n for n in zf.namelist()
                       if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not names:
            raise LookupError(f"{path} contains no worksheet XML")
        if sheet >= len(names):
            raise LookupError(f"--sheet {sheet} but the file has {len(names)}")
        return sheet_rows(zf.read(names[sheet]), shared)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("xlsx", help="OL's export, as received (.xlsx or .xls)")
    ap.add_argument("-o", "--out", help="Write JSON here (default: stdout only)")
    ap.add_argument("--sheet", type=int, default=0)
    ap.add_argument("--customer", default=None,
                    help="Keep only rows whose customer column contains this "
                         "(e.g. HILMAR). Omit if the export is already "
                         "Hilmar-only, as the Jun-Aug one was.")
    args = ap.parse_args()

    rows = read_any(Path(args.xlsx), args.sheet)
    print(f"{args.xlsx}: {len(rows)} row(s) in sheet {args.sheet}")
    try:
        records, dropped, labels = extract(rows, args.customer)
    except LookupError as e:
        print(f"::error::{e}")
        return 2

    print("\ncolumn binding (check this before trusting the output):")
    for field in FIELD_ALIASES:
        print(f"  {field:<12} <- {labels.get(field, '— NOT FOUND —')!r}")

    print(f"\n{len(records)} booking(s) extracted")
    if records:
        dates = sorted(r["sheet_date"] for r in records if r["sheet_date"])
        if dates:
            print(f"  date range {dates[0]} .. {dates[-1]}")
        blank = [r["mdolx"] for r in records if not r["sheet_date"]]
        if blank:
            print(f"  no usable date on {len(blank)}: {', '.join(blank)}")
        nopod = [r["mdolx"] for r in records if not r["pod"]]
        if nopod:
            print(f"  no POD on {len(nopod)}: {', '.join(nopod)}")

    if dropped:
        print(f"\ndropped {len(dropped)} row(s):")
        for ref, why in dropped[:40]:
            print(f"  {ref}: {why}")
        if len(dropped) > 40:
            print(f"  ... and {len(dropped) - 40} more")

    if args.out:
        Path(args.out).write_text(
            json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nWROTE {args.out}")
        print("Next: diag-reconcile with these refs, then backfill_ol_bookings "
              "--recap <that file> (dry run first).")
    else:
        print("\n(no --out given; nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
