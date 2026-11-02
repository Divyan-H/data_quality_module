"""End-to-end expectations on the two structural test workbooks in samples/ (rules only, no LLM).

structural_test_A.xlsx: one structural issue per sheet (openpyxl, formulas without stored results).
structural_test_B.xlsx: edge cases saved by real Excel (computed formulas, real AutoFilter, pivot, Excel Table,
external link, threaded comment). Rebuild both with tests/fixtures/build_structural_workbooks.py and
tests/fixtures/excel_post.ps1.

Every expectation is its own test case, so one failure never hides the others.
"""
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from src.structure.pipeline import run_structure

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


class Out:
    """Accessors over one Phase A output folder."""

    def __init__(self, path: Path, result: dict):
        self.path, self.result = path, result
        self.inventory = json.loads((path / "inventory.json").read_text("utf8"))
        self.report = json.loads((path / "report.json").read_text("utf8"))
        self.file_key = self.inventory["files"][0]["file_key"]
        self.sheets = {}
        for p in sorted((path / "files" / self.file_key / "sheets").glob("*.json")):
            s = json.loads(p.read_text("utf8"))
            self.sheets[s["sheet_name"]] = s
        self.cells = pq.read_table(path / "files" / self.file_key / "cells.parquet").to_pylist()
        self.audit = pq.read_table(path / "files" / self.file_key / "audit.parquet").to_pylist()
        self.selected = set(self.inventory["selection"][0]["tables"])

    def tables(self, sheet):
        return {t["table_key"]: t for t in self.sheets[sheet]["tables"]}

    def rows(self, sheet, key="t1"):
        return pq.read_table(self.path / self.tables(sheet)[key]["outputs"]["data"]).to_pylist()

    def columns(self, sheet, key="t1"):
        return json.loads((self.path / self.tables(sheet)[key]["outputs"]["columns"]).read_text("utf8"))["columns"]

    def names(self, sheet, key="t1"):
        return [c["name"] for c in self.columns(sheet, key)]

    def index(self, sheet):
        return self.sheets[sheet]["sheet_index"]

    def findings(self, prefix, sheet=None):
        """Findings whose id starts with `prefix` (e.g. 'S-11.hidden_rows'), optionally on one sheet."""
        out = []
        for f in self.report["extras"]["structure"]["findings_detail"]:
            if not f["id"].startswith(prefix + "@") and not f["id"].startswith(prefix + "#"):
                continue
            if sheet is not None:
                scope = f["id"].split("@", 1)[1].split("#")[0].split("/", 1)[-1].split(":")
                if len(scope) < 2 or scope[1] != str(self.index(sheet)):
                    continue
            out.append(f)
        return out

    def severity(self, prefix, sheet=None):
        found = self.findings(prefix, sheet)
        return found[0]["severity"] if found else None

    def message(self, prefix, sheet=None):
        ids = {f["id"] for f in self.findings(prefix, sheet)}
        return next(f["message"] for f in self.report["findings"] if f["id"] in ids)

    def placements(self, sheet, kind=None):
        i = self.index(sheet)
        return sorted(c["ref"] for c in self.cells
                      if c["sheet_index"] == i and c["category"] == "placement" and (kind is None or c["kind"] == kind))

    def annotations(self, sheet, kind):
        i = self.index(sheet)
        return sorted(c["ref"] for c in self.cells if c["sheet_index"] == i and c["kind"] == kind)


def _run(tmp_path_factory, name):
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"{name} missing: run tests/fixtures/build_structural_workbooks.py")
    out = tmp_path_factory.mktemp(name.split(".")[0])
    res = run_structure({"files": [{"path": str(path), "source_id": "s-1", "file_id": "f-1", "relative_path": name}],
                         "config": {}, "output_dir": str(out / "out")})
    return Out(out / "out", res)


@pytest.fixture(scope="module")
def A(tmp_path_factory):
    return _run(tmp_path_factory, "structural_test_A.xlsx")


@pytest.fixture(scope="module")
def B(tmp_path_factory):
    return _run(tmp_path_factory, "structural_test_B.xlsx")


SALES = " Sales Raw@Data "

# ---------------------------------------------------------------------------------------------- workbook A


def test_a_sales_table_found_at_its_header_with_groups(A):
    t = A.tables(SALES)["t1"]
    assert (t["range"], t["header"]["rows"], t["header"]["group_rows"], t["data"]["rows"]) == ("B2:M43", [3], [2], 40)
    assert A.placements(SALES, "title") == ["B1"]
    assert A.columns(SALES)[0]["groups"] == ["Identifiers"]


def test_a_blank_empty_column_dropped_sparse_kept(A):
    assert A.names(SALES) == ["Invoice_ID", "Date", "Region", "Spend", "Clicks", "Amount", "Status",
                              "External_Check", "Broken_Formula", "Notes", "Masked"]
    assert A.tables(SALES)["t1"]["dropped_columns"] == ["L"]
    msg = A.message("S-05.columns", SALES)
    assert "sparse" in msg and "K" in msg


def test_a_hidden_rows_and_columns_kept_and_warned(A):
    rows = A.rows(SALES)
    assert len(rows) == 40 and [r["_source_row"] for r in rows if r["_row_hidden"]] == [10, 11]
    assert A.severity("S-11.hidden_rows", SALES) == "warn" and A.severity("S-12.hidden_cols", SALES) == "warn"
    assert all(r["Masked"] for r in rows)


def test_a_outlines_freeze_filter_print_area(A):
    assert A.severity("S-13.row_outline", SALES) == "info" and A.severity("S-14.col_outline", SALES) == "info"
    assert not A.findings("S-15.freeze_mismatch", SALES)                       # freeze at B4 agrees
    assert A.severity("S-16.filter", SALES) == "info" and A.severity("S-17.print_area", SALES) == "info"


def test_a_hidden_formatting_and_validation(A):
    assert A.annotations(SALES, "invisible_format") == ["H30", "H31", "H32"]
    assert A.severity("S-18.hidden_values", SALES) == "warn"
    assert A.annotations(SALES, "dv_violation") == ["H15", "H16"]


def test_a_formulas_without_results_block(A):
    assert all(r["Amount"] is None for r in A.rows(SALES))
    f = A.findings("S-26.formulas", SALES)[0]
    assert f["severity"] == "block"
    assert {"ref": "J6", "check": "broken_reference"} in f["locations"]
    assert A.annotations(SALES, "error") == ["J12"]


def test_a_protection_sheet_name_comment(A):
    assert A.severity("S-23.protected", SALES) == "warn" and A.severity("S-24.sheet_name", SALES) == "info"
    comment = next(c for c in A.cells if c["kind"] == "comment")
    assert comment["ref"] == "K8" and "actual value 500" in comment["detail"]
    assert all("actual value" not in f["message"] for f in A.report["findings"])        # never in a message


def test_a_merges_classified(A):
    assert "1 title" in A.message("S-10.merges", SALES) and "3 header" in A.message("S-10.merges", SALES)


def test_a_prose_sheet_is_notes(A):
    assert not A.tables("Corrupt_Import_Log") and A.placements("Corrupt_Import_Log", "notes") == ["A1", "A2", "A3"]
    assert A.findings("sheet_no_table", "Corrupt_Import_Log")


def test_a_clean_table_raises_nothing(A):
    t = A.tables("Product_Lookup")["t1"]
    assert (t["range"], t["data"]["rows"]) == ("A1:C6", 5)
    assert t["findings"] == []


def test_a_scattered_cells_are_fragments(A):
    assert not A.tables("Sparse_Template") and len(A.placements("Sparse_Template", "fragment")) == 10
    assert A.severity("S-06.sparse_sheet", "Sparse_Template") == "info"


def test_a_multi_tables_split_and_freeze_mismatch(A):
    t = A.tables("Multi_Tables")
    assert [(k, v["range"], v["header"]["rows"]) for k, v in t.items()] == \
           [("t1", "A3:D8", [3]), ("t2", "H4:J8", [4]), ("t3", "A13:E17", [13])]
    assert A.placements("Multi_Tables", "title") == ["A1"]
    assert A.severity("S-15.freeze_mismatch", "Multi_Tables") == "info"


def test_a_gap_rows_removed_with_one_audit_entry(A):
    t = A.tables("Aesthetic_Gaps")["t1"]
    assert (t["range"], t["data"]["rows"], t["data"]["gap_rows"]) == ("A1:C30", 15, list(range(3, 30, 2)))
    assert sum(1 for a in A.audit if a["action"] == "remove_gap_rows") == 1


def test_a_monthly_tabs_form_a_family_with_renames(A):
    msg = A.message("S-09.family_drift")
    for pair in ("Date~Order Date (name)", "Store~Store_ID (name)", "Orders~Order_Count (name)",
                 "Revenue~Sales_Amount (position)", "extra Region"):
        assert pair in msg
    assert A.severity("S-24.sheet_name", "Feb Sales ") == "info"


def test_a_static_summary_with_key_value_and_footer(A):
    t = A.tables("Stale_Pivot_Cache")["t1"]
    assert (t["range"], t["data"]["rows"], t["data"]["footer_rows"]) == ("A3:B9", 4, [9])
    assert A.placements("Stale_Pivot_Cache", "key_value") == ["A2", "B2"]
    assert A.placements("Stale_Pivot_Cache", "footer") == ["A9", "B9"]
    assert A.severity("S-21.suspected_static_summary", "Stale_Pivot_Cache") == "info"
    assert A.severity("S-15.freeze_mismatch", "Stale_Pivot_Cache") == "info"


def test_a_very_hidden_sheet_read_but_not_selected(A):
    t = A.tables("VeryHiddenData")["t1"]
    assert t["table_ref"] not in A.selected and A.severity("S-25.hidden_sheet", "VeryHiddenData") == "info"


def test_a_chart_over_data(A):
    assert A.tables("Chart_Over_Data")["t1"]["data"]["rows"] == 12
    assert "Chart 1" in A.message("S-27.objects", "Chart_Over_Data")


def test_a_file_level_names(A):
    assert A.findings("S-20.names")[0]["locations"] == [{"name": "Broken_Deleted_Area", "class": "missing_sheet"}]


def test_a_auto_selects_every_visible_table(A):
    assert len(A.selected) == 10 and not any(":9:" in r for r in A.selected)     # all but VeryHiddenData


# ---------------------------------------------------------------------------------------------- workbook B


def test_b_deep_header_after_notes_and_key_values(B):
    t = B.tables("Deep_Header")["t1"]
    assert (t["header"]["rows"], t["data"]["rows"]) == ([15], 8)
    assert B.placements("Deep_Header", "title") == ["A1"]                       # one cell on top: a title
    assert B.placements("Deep_Header", "key_value") == ["A3", "A4", "B3", "B4"]


def test_b_two_row_header_composite_names(B):
    assert B.names("Two_Row_Header") == ["Region", "Q1_Budget", "Q1_Actual", "Q2_Budget", "Q2_Actual"]


def test_b_unit_row(B):
    cols = B.columns("Unit_Row")
    assert [c["unit"] for c in cols] == [None, "USD", "%", "units"]
    assert B.tables("Unit_Row")["t1"]["data"]["rows"] == 6


def test_b_label_merges_filled_numbers_not(B):
    rows = B.rows("Merges_In_Data")
    assert [r["Region"] for r in rows] == ["West", "West", "West", "North", "North"]
    assert [r["Spend"] for r in rows][-2:] == ["160", None]
    assert B.annotations("Merges_In_Data", "merge_numeric") == ["C5"]
    assert B.severity("S-10.numeric_merge", "Merges_In_Data") == "warn"


def test_b_year_headers_kept_and_wide_time_flagged(B):
    assert B.names("Wide_Time") == ["Channel", "2023", "2024", "2025", "2026"]
    assert B.severity("S-03.wide_time_layout", "Wide_Time") == "info"


def test_b_dates_under_blank_header(B):
    rows = B.rows("Blank_Header_Dates")
    assert B.names("Blank_Header_Dates")[0] == "col_A" and rows[0]["col_A"] == "2026-01-01"
    assert B.severity("S-01.blank_header", "Blank_Header_Dates") == "info"


def test_b_null_like_text_untouched(B):
    assert [r["Note"] for r in B.rows("Null_Tokens")] == ["null", "  None ", "N/A", "NA", "-", "   ", "real note"]


def test_b_interior_subtotals_kept_and_tied_to_outline(B):
    rows = B.rows("Totals_Outline")
    assert [r["Item"] for r in rows].count("Total") == 2
    assert "close outlined groups" in B.message("S-13.row_outline", "Totals_Outline")
    assert B.severity("interior_totals", "Totals_Outline") == "info"


def test_b_excel_table_totals_row_and_rows_below(B):
    t = B.tables("Excel_Table_Totals")["t1"]
    assert (t["source"], t["excel_table"], t["data"]["rows"], t["data"]["totals_rows"]) == \
           ("excel_table", "StockTable", 5, [7])
    assert B.placements("Excel_Table_Totals", "totals_row")[0] == "A7"
    assert B.severity("S-22.table_object_mismatch", "Excel_Table_Totals") == "warn"


def test_b_duplicates_header_only_sparse(B):
    assert B.names("Duplicates_Sparse") == ["Channel", "Spend", "Spend__2", "Comment", "Planned"]
    msg = B.message("S-05.columns", "Duplicates_Sparse")
    assert "header-only E" in msg and "sparse D" in msg
    assert B.severity("S-03.duplicate_header", "Duplicates_Sparse") == "info"


def test_b_headerless_numbers(B):
    assert B.names("Headerless") == ["col_A", "col_B", "col_C", "col_D"]
    assert B.tables("Headerless")["t1"]["data"]["rows"] == 6
    assert B.severity("S-01.no_header", "Headerless") == "warn"


def test_b_tables_one_column_apart_are_split(B):
    t = B.tables("Side_By_Side_1gap")
    assert [v["range"] for v in t.values()] == ["A1:B7", "D1:F4"]
    assert B.severity("S-07.soft_split", "Side_By_Side_1gap") == "warn"


def test_b_total_under_dates_is_a_footer_and_threaded_comment(B):
    rows = B.rows("Footer_Date")
    assert len(rows) == 6 and rows[-1]["Date"] == "2026-01-06"
    assert B.placements("Footer_Date", "footer") == ["A8", "B8", "C8"]
    assert B.annotations("Footer_Date", "comment") == ["B3"]


def test_b_grand_total_summary(B):
    assert B.severity("S-21.suspected_static_summary", "Grand_Total_Summary") == "info"
    assert B.tables("Grand_Total_Summary")["t1"]["data"]["footer_rows"] == [6]


def test_b_values_render_exactly(B):
    got = {r["Case"]: r["Value"] for r in B.rows("Values")}
    assert got["long decimal"] == "99999999.99" and got["percent"] == "0.05"
    assert got["huge"] == "1E+20" and got["tiny"] == "1.5E-07"
    assert got["leap bug 1900-02-28"] == "1900-02-28" and got["boolean"] == "TRUE" and got["error"] == "#N/A"
    assert got["leading zeros"] == "00123" and got["rupees"] == "₹ 1,200" and got["emoji"] == "launch 🚀"
    assert got["datetime"] == "2026-03-04T05:06:07" and got["time"] == "13:45:00"
    assert got["float sum"] in ("0.30000000000000004", "0.3")                  # Excel's stored result


def test_b_colliding_sheet_names_get_distinct_keys(B):
    keys = {B.sheets[n]["sheet_key"] for n in ("Sales@Data", "Sales Data")}
    assert len(keys) == 2
    assert B.names("Sales Data") == ["Montant ₹", "Région"]


def test_b_hidden_empty_and_one_row_sheets(B):
    assert B.tables("Hidden_Sheet")["t1"]["table_ref"] not in B.selected
    assert not B.tables("Empty_Sheet") and B.sheets["Empty_Sheet"]["populated"] == 0
    assert not B.tables("One_Row") and B.findings("sheet_no_table", "One_Row")


def test_b_validation_rules_checked(B):
    assert B.annotations("Validation_Rules", "dv_violation") == ["A4", "B5", "C4"]


def test_b_number_format_hiding_but_not_colour_scale(B):
    assert B.annotations("Hidden_Formats", "invisible_format") == [f"C{r}" for r in range(2, 8)]


def test_b_external_link(B):
    f = B.findings("S-26.formulas", "Side_Calc")
    assert f and any(loc.get("check") == "external_link" for loc in f[0]["locations"])
    assert B.findings("S-26.external_links")


def test_b_excel_autofilter_marks_filtered_rows(B):
    rows = B.rows("Filter_Source")
    assert [r["_source_row"] for r in rows if r["_row_filtered"]] == [3, 5, 7, 9, 11, 13]
    assert B.severity("S-16.filtered_rows", "Filter_Source") == "warn"
    assert not B.findings("S-11.hidden_rows", "Filter_Source")                # filtered rows are not S-11


def test_b_real_pivot_table(B):
    t = B.tables("Pivot")
    pivots = [v for v in t.values() if v["source"] == "pivot"]
    assert pivots and pivots[0]["table_ref"] not in B.selected
    assert B.findings("S-21.pivot", "Pivot")


def test_b_runs_without_invariant_failures_and_without_blocks(B):
    assert B.result["status"] == "succeeded" and not B.result["blocking"]
