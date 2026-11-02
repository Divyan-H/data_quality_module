"""Phase B issue workbook: every Phase B issue planted in known cells, then run through Phase A and Phase B.

    python tests/fixtures/build_phase_b_workbook.py

Writes (in samples/):
  phase_b_issues.xlsx           - one table, 42 rows; each PLANTS entry below puts one issue in one cell or column
  phase_b_issue_catalogue.xlsx  - one row per planted issue: what Phase B does with it, the expected result, the
                                  actual result from the run, and PASS/FAIL
and runs the pipeline into out_phase_b_issues/ (Phase A) and out_phase_b_issues_run/ (Phase B).
tests/test_hygiene_workbook.py asserts every row of the catalogue passes.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

N = 40                                    # base rows; rows 41-42 are the duplicate and same-key rows
D0 = dt.date(2026, 1, 1)

# Column order of the data sheet. "Clicks " keeps its trailing space on purpose (D-11).
COLUMNS = ["Invoice_ID", "Order Date", "Week Start", "Channel", "Customer Name", "Status", "Region", "Spend",
           "Clicks ", "Impressions", "Revenue", "CTR Pct", "Loss", "Budget", "Unit Price", "ZIP_Code",
           "Account_Number", "Vehicle Category", "Description", "Notes", "Total", "col_7", "fd_burn"]

# One entry per planted issue: (id, issue, how Phase B handles it, column, invoice row, planted value,
# expected: ("value", final text) | ("null",) | ("review", rule) | ("column_review", rule) | ("column_fix", rule)
#           | ("unchanged",) | ("dropped",) | ("renamed", new name))
PLANTS = [
    ("E-01", "Currency symbol and thousands separator", "Stripped before conversion", "Spend", 3, "$1,234.50", ("value", "1234.5")),
    ("E-01", "Rupee amount as text", "Stripped before conversion", "Spend", 4, "₹2,500", ("value", "2500")),
    ("E-01", "European decimal comma", "Comma read as decimal (last separator, not 3 digits)", "Spend", 9, "€99,99", ("value", "99.99")),
    ("N-01 / D-04", "Text in a numeric column", "Set to NULL; original on Needs_Review", "Spend", 6, "abc", ("review", "D-04")),
    ("N-04 / D-09", "Leading/trailing spaces in a number", "Trimmed, then converted", "Spend", 7, " 120 ", ("value", "120")),
    ("N-03 / D-18", "'NA' placeholder in a numeric column", "Set to NULL", "Spend", 8, "NA", ("null",)),
    ("D-01", "Number Excel shows as a date (120 formatted as a date)", "Date in a numeric column -> its Excel serial", "Clicks", 5, 120, ("value", "120")),
    ("D-03", "Date in another format (dd/mm/yyyy)", "Rewritten in the column's dominant format", "Order Date", 10, "15/01/2026", ("value", "2026-01-15")),
    ("D-03", "Date with month name", "Rewritten in the column's dominant format", "Order Date", 14, "Jan 20, 2026", ("value", "2026-01-20")),
    ("D-02", "Impossible day (30 Feb)", "Clamped to the last day of the month", "Order Date", 11, "30/02/2026", ("value", "2026-02-28")),
    ("D-02", "Invalid month (13)", "Set to NULL; original on Needs_Review", "Order Date", 12, "2026-13-01", ("review", "D-02")),
    ("D-25", "Excel serial in a date column", "Converted with the 1899-12-30 epoch", "Order Date", 13, 46030, ("value", "2026-01-08")),
    ("D-25", "Whole column of serials", "Converted to dates", "Week Start", 1, None, ("value", "2026-01-08")),
    ("D-18", "'N/A' placeholder", "Set to NULL", "Status", 15, "N/A", ("null",)),
    ("D-18", "'null' placeholder", "Set to NULL", "Status", 16, "null", ("null",)),
    ("D-18", "'None' placeholder", "Set to NULL", "Status", 17, "None", ("null",)),
    ("D-18", "'-' placeholder", "Set to NULL", "Status", 18, "-", ("null",)),
    ("D-18 guard", "'Pending' is the column's real top value", "Left alone (dominant-value guard)", "Status", 2, None, ("value", "Pending")),
    ("D-18 guard", "'NA' = North America in a region column", "Left alone (geo guard)", "Region", 2, None, ("value", "NA")),
    ("D-10", "Lower-case variant", "Most common spelling", "Channel", 19, "facebook", ("value", "Facebook")),
    ("D-10", "Upper-case variant", "Most common spelling", "Channel", 20, "FACEBOOK", ("value", "Facebook")),
    ("D-12", "Misspelt name", "Merged into the most frequent spelling; on Needs_Review", "Channel", 21, "Facebok", ("value", "Facebook")),
    ("D-12", "Name with trailing punctuation", "Merged into the most frequent spelling; on Needs_Review", "Channel", 22, "Google Ads.", ("value", "Google Ads")),
    ("D-12", "Placeholder value 'test'", "Set to NULL; on Needs_Review", "Customer Name", 27, "test", ("null",)),
    ("D-12", "Sentinel 99999", "Set to NULL; on Needs_Review", "Budget", 37, 99999, ("null",)),
    ("D-09", "Extra spaces in text", "Trimmed, inner runs collapsed", "Customer Name", 23, "  Ann   Lee ", ("value", "Ann Lee")),
    ("D-20", "Mojibake", "Re-decoded cp1252 -> UTF-8", "Customer Name", 24, "CafÃ© Mocha", ("value", "Café Mocha")),
    ("N-05 / D-13", "Tab inside text", "Replaced by a space", "Customer Name", 25, "Bob\tSmith", ("value", "Bob Smith")),
    ("N-05 / D-13", "Non-breaking space", "Replaced by a space", "Customer Name", 26, "Raj Kumar", ("value", "Raj Kumar")),
    ("N-02", "Blank (spaces only) cell", "Set to NULL", "Customer Name", 28, "   ", ("null",)),
    ("D-08", "#N/A error value", "Set to NULL, original logged", "Notes", 29, "#N/A", ("null",)),
    ("D-08", "#DIV/0! error value", "Set to NULL, original logged", "Notes", 30, "#DIV/0!", ("null",)),
    ("D-07", "#REF! error value", "Set to NULL, original logged", "Notes", 31, "#REF!", ("null",)),
    ("D-05", "Formula with no stored result", "Value is NULL; on Needs_Review", "Total", 32, "=H32*2", ("review", "D-05")),
    ("D-07", "Formula with a broken reference", "NULL; logged", "Total", 33, "=SUM(#REF!)", ("null",)),
    ("D-06", "Formula linking another workbook", "Reported on Needs_Review (client)", "Total", 34, "='[budget.xlsx]Sheet1'!A1", ("review", "D-06")),
    ("D-24", "ZIP code that lost its leading zero", "Padded to the column's usual width", "ZIP_Code", 35, 2134, ("value", "02134")),
    ("D-14", "Outlier", "Capped to the nearest normal value; original on Needs_Review", "Impressions", 36, 250000, ("value", "1060")),
    ("D-22", "Whole-number percentage among fractions", "Divided by 100", "CTR Pct", 38, 3, ("value", "0.03")),
    ("D-22", "Cents among dollars", "Divided by 100", "Revenue", 2, 15500, ("value", "155")),
    ("D-15", "Numeric column with negative values", "Flagged for the client; nothing changed", "Loss", 1, None, ("column_review", "D-15")),
    ("D-21", "18-digit account number", "Column kept as text", "Account_Number", 1, None, ("column_fix", "D-21")),
    ("D-04", "Column half numbers, half text", "Flagged; nothing converted", "Vehicle Category", 1, None, ("column_review", "D-04")),
    ("E-01", "European number with thousands dot", "Read as 1234.56", "Unit Price", 40, "1.234,56", ("value", "1234.56")),
    ("D-23", "Values cut at the same length", "Client flag (cut text cannot be rebuilt)", "Description", 1, None, ("column_review", "D-23", "exactly 30 characters")),
    ("D-16", "Numbered column name", "Client flag (or renamed from a data dictionary)", "col_7", 1, None, ("column_review", "D-16")),
    ("D-16", "Abbreviated column name", "Client flag (or renamed from a data dictionary)", "fd_burn", 1, None, ("column_review", "D-16")),
    ("D-11", "Header with a trailing space", "Trimmed (by Phase A, or by Phase B if it arrives untrimmed)", "Clicks", 1, None, ("renamed", "Clicks")),
    ("D-19", "Exact duplicate row", "Dropped, first kept; tombstone in the audit", "Invoice_ID", 41, None, ("dropped",)),
    ("D-19", "Same key, different values", "On Needs_Review", "Invoice_ID", 42, None, ("review", "D-19")),
]


def base_row(i: int) -> dict:
    """Clean values for row i (1-based); plants overwrite single cells."""
    return {
        "Invoice_ID": f"INV-{i:03d}",
        "Order Date": dt.datetime.combine(D0 + dt.timedelta(days=i - 1), dt.time()),
        "Week Start": 46023 + 7 * i,                                 # Excel serials of weekly dates
        "Channel": ["Facebook", "Facebook", "Google Ads", "TV", "Radio"][i % 5],
        "Customer Name": f"Customer {i}",
        "Status": "Pending" if i % 2 == 0 else "Done",
        "Region": ["EU", "APAC", "NA", "LATAM"][i % 4],
        "Spend": round(100 + i * 3.5, 2),
        "Clicks ": 100 + i,
        "Impressions": 1000 + (i % 7) * 10,
        "Revenue": 150 + (i % 10) * 5,
        "CTR Pct": round(0.02 + (i % 5) * 0.005, 3),
        "Loss": 10 + i if i % 5 else -(10 + i),                 # D-15: every fifth value is negative
        "Budget": 5000 + i * 10,
        "Unit Price": f"1.{i * 10:03d},50",                        # European format: 1.010,50
        "ZIP_Code": ["02134", "10001", "07001", "90210", "60601"][i % 5],
        "Account_Number": str(123456789012345000 + i),
        "Vehicle Category": ["97", "1144", "HCV - 9ft", "128", "LCV"][i % 5],
        "Description": f"Acct {i:02d} premium enterprise subscription" if i % 3 else f"Standard plan for SMB {i}",
        "Notes": None,
        "Total": 2 * (100 + i),
        "col_7": "x" if i % 2 else "y",
        "fd_burn": 300 + i,
    }


def build_rows() -> list[dict]:
    rows = [base_row(i) for i in range(1, N + 1)]
    for r in rows:                                                   # D-23: the source cut every value at 30 chars
        r["Description"] = r["Description"][:30]
    rows.append(dict(rows[0]))                                       # 41: exact duplicate of row 1
    dup = dict(rows[9])                                              # 42: same Invoice_ID as row 10, other Spend
    dup["Spend"] = 999.0
    rows.append(dup)
    for revenue_row in (12, 22):                                     # two more cents values (D-22 needs >= 5%)
        rows[revenue_row - 1]["Revenue"] = 16000
    for _id, _issue, _how, col, row, value, _exp in PLANTS:
        if value is not None:
            rows[row - 1]["Clicks " if col == "Clicks" else col] = value
    return rows


def build_workbook(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Issues"
    ws.append(COLUMNS)
    for r in build_rows():
        ws.append([r[c] for c in COLUMNS])
    for row in ws.iter_rows(min_row=2, min_col=2, max_col=2):        # Order Date: real date format
        for cell in row:
            if isinstance(cell.value, dt.datetime):
                cell.number_format = "yyyy-mm-dd"
    clicks = COLUMNS.index("Clicks ") + 1
    ws.cell(row=5 + 1, column=clicks).number_format = "yyyy-mm-dd"   # D-01: the number 120 displayed as a date
    wb.save(path)


# ── run + compare ────────────────────────────────────────────────────────────
def run_pipeline(xlsx: Path, out_a: Path, out_b: Path) -> None:
    import shutil
    from src.hygiene.pipeline import run_hygiene
    from src.structure.pipeline import run_structure
    shutil.rmtree(out_a, ignore_errors=True)
    shutil.rmtree(out_b, ignore_errors=True)
    run_structure({"files": [{"path": str(xlsx), "source_id": "s-1", "file_id": "f-1", "relative_path": xlsx.name}],
                   "config": {}, "output_dir": str(out_a)})
    run_hygiene(str(out_a), str(out_b), str(ROOT / "config" / "settings.yaml"))


def compare(out_b: Path) -> list[dict]:
    """Actual result for every plant, read from Phase B's outputs."""
    import polars as pl
    from src.hygiene.review import render
    base = out_b / "phase_b_hygiene"
    df = pl.read_parquet(next(base.glob("tables/*/data.parquet")))
    audit = json.loads((base / "remediation_audit.json").read_text("utf8"))
    review = json.loads((base / "needs_review.json").read_text("utf8"))
    src_row = {i: i + 1 for i in range(1, N + 3)}                   # header is Excel row 1
    rows = {r["_source_row"]: r for r in df.iter_rows(named=True)}
    results = []
    for pid, issue, how, col, row, planted, exp in PLANTS:
        r = rows.get(src_row[row])
        cur = render(r.get(col)) if r and col in r else None
        rules = sorted({a["rule"] for a in audit if a["column"] == col and a.get("source_row") == src_row[row]})
        flags = sorted({i["rule"] for i in review if i["column"] == col and i.get("source_row") == src_row[row]})
        col_flags = sorted({i["rule"] for i in review if i["column"] == col and i["row_id"] is None})
        col_fixes = sorted({a["rule"] for a in audit if a["column"] == col and a["row_id"] is None})
        kind = exp[0]
        if kind == "value":
            ok, actual = cur == exp[1], cur
        elif kind == "null":
            ok, actual = r is not None and cur is None, cur
        elif kind == "review":
            ok, actual = exp[1] in flags, f"value {cur!r}; review {flags}"
        elif kind == "column_review":
            why = [i["why_flagged"] for i in review if i["column"] == col and i["row_id"] is None and i["rule"] == exp[1]]
            ok = bool(why) and (len(exp) < 3 or exp[2] in why[0])
            actual = why[0] if why else f"column review {col_flags}"
        elif kind == "column_fix":
            ok, actual = exp[1] in col_fixes, f"column fixes {col_fixes}"
        elif kind == "renamed":
            by_b = any(a["action"] == "renamed" and a["new_value"] == exp[1] for a in audit)
            ok = exp[1] in df.columns and f"{exp[1]} " not in df.columns
            actual = f"'{exp[1]}' ({'trimmed by Phase B' if by_b else 'already trimmed by Phase A'})"
        elif kind == "dropped":
            ok, actual = r is None and any(a["action"] == "dropped" and a.get("source_row") == src_row[row] for a in audit), \
                "dropped" if r is None else "still present"
        else:
            ok, actual = False, "unknown expectation"
        results.append({"ID": pid, "Issue": issue, "How Phase B handles it": how, "Column": col,
                        "Excel row": src_row[row], "Planted value": "" if planted is None else str(planted),
                        "Expected": " ".join(str(x) for x in exp), "Actual": str(actual),
                        "Rules applied": ", ".join(rules + [f"review {x}" for x in flags]),
                        "Status": "PASS" if ok else "FAIL"})
    return results


def write_catalogue(path: Path, results: list[dict]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Phase_B_Issues"
    ws.append(list(results[0]))
    for r in results:
        ws.append(list(r.values()))
        ws.cell(row=ws.max_row, column=len(r)).fill = PatternFill(
            "solid", fgColor="C6EFCE" if r["Status"] == "PASS" else "FFC7CE")
    for c in ws[1]:
        c.font = Font(bold=True)
    for col, width in zip("ABCDEFGHIJ", (11, 38, 48, 16, 9, 24, 24, 34, 24, 8)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    wb.save(path)


def main() -> int:
    xlsx = ROOT / "samples" / "phase_b_issues.xlsx"
    out_a, out_b = ROOT / "out_phase_b_issues", ROOT / "out_phase_b_issues_run"
    build_workbook(xlsx)
    run_pipeline(xlsx, out_a, out_b)
    results = compare(out_b)
    write_catalogue(ROOT / "samples" / "phase_b_issue_catalogue.xlsx", results)
    failed = [r for r in results if r["Status"] == "FAIL"]
    for r in failed:
        print(f"FAIL {r['ID']:12} {r['Column']:16} row {r['Excel row']:>3}  expected {r['Expected']!r:28} got {r['Actual']}")
    print(f"{len(results) - len(failed)}/{len(results)} planted issues handled as expected")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
