"""Builds the two structural test workbooks in samples/ (run from the repo root):

    python tests/fixtures/build_structural_workbooks.py

structural_test_A.xlsx  - written by openpyxl: one structural issue per sheet, modelled on the spec's golden
                          workbook (§15.2). Formulas carry no stored result, as from a tool that does not
                          calculate.
structural_test_B.xlsx  - edge cases, then opened and saved by real Excel (Windows only): Excel computes the
                          formula results, applies a real AutoFilter and builds a real pivot table.

What Phase A must do with each sheet is asserted in tests/test_structure_workbooks.py.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import sys
from pathlib import Path

import openpyxl
from openpyxl.chart import BarChart, Reference
from openpyxl.comments import Comment
from openpyxl.formatting.rule import ColorScaleRule, Rule
from openpyxl.styles import Font, PatternFill
from openpyxl.styles.differential import DifferentialStyle
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "samples"
random.seed(42)
D0 = dt.date(2026, 1, 1)


def day(i: int) -> dt.date:
    return D0 + dt.timedelta(days=i)


# ====================================================================== workbook A (openpyxl)


def sales_raw(wb):
    """Sheet 0 — most Excel-feature issues on one table (S-01..S-05, S-10..S-19, S-23, S-24, S-26, S-28)."""
    ws = wb.active
    ws.title = " Sales Raw@Data "                              # S-24: edge spaces and '@'
    ws["B1"] = "Sales Raw Data Export"                         # title over a merge          S-01, S-10 title
    ws.merge_cells("B1:M1")
    for ref, label in (("B2:D2", "Identifiers"), ("E2:H2", "Metrics"), ("I2:M2", "Quality Traps")):
        ws[ref.split(":")[0]] = label                          # group row                   S-03, S-10 header
        ws.merge_cells(ref)
    headers = ["Invoice_ID", "Date", "Region", "Spend", "Clicks", "Amount", "Status",
               "External_Check", "Broken_Formula", "Notes", None, "Masked"]   # L: blank label, no data -> S-05
    for j, h in enumerate(headers):
        if h:
            ws.cell(3, 2 + j, h)
    regions = ["North", "South", "East", "West"]
    for i in range(40):                                        # data rows 4..43
        r = 4 + i
        ws.cell(r, 2, f"INV-{1000 + i}")
        ws.cell(r, 3, day(i))
        ws.cell(r, 4, regions[i % 4])
        ws.cell(r, 5, round(100 + i * 12.5, 2))
        ws.cell(r, 6, 10 * i)
        ws.cell(r, 7, f"=E{r}*2")                               # uncached formula column      S-26 block
        ws.cell(r, 8, "Active" if i % 3 else "Paused")
        ws.cell(r, 13, f"M{i:03d}")                             # hidden column with data     S-12
    ws["I5"] = "checked manually"                               # sparse column (the external link lives in B)
    ws["J6"] = "=SUM(#REF!)"                                    # broken reference            S-26
    ws["J12"] = "#DIV/0!"                                       # error literal
    ws["K8"] = "check with finance"                             # sparse column               S-05 sparse
    ws["K8"].comment = Comment("use actual value 500, not the displayed 450", "analyst")   # S-28
    ws["H15"], ws["H16"] = "Unknown", "Stopped"                 # violate the dropdown         S-19
    dv = DataValidation(type="list", formula1='"Active,Paused"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add("H4:H43")
    for r in (10, 11):
        ws.row_dimensions[r].hidden = True                      # hidden rows                 S-11
    ws.column_dimensions["M"].hidden = True
    for r in range(20, 26):
        ws.row_dimensions[r].outlineLevel = 1                   # grouped rows                S-13
    for r in (26, 27):
        ws.row_dimensions[r].outlineLevel = 2
    for col in ("E", "F"):
        ws.column_dimensions[col].outlineLevel = 1              # grouped columns             S-14
    ws.freeze_panes = "B4"                                      # agrees with header row 3    (no S-15)
    ws.auto_filter.ref = "B3:M43"                               # filter, no criteria         S-16 info
    ws.print_area = "B1:G20"                                    # print area                  S-17
    hide = DifferentialStyle(font=Font(color="FFFFFF"), fill=PatternFill(bgColor="FFFFFF", fill_type="solid"))
    ws.conditional_formatting.add("H30:H32", Rule(type="expression", formula=["TRUE"], dxf=hide))   # S-18
    ws.protection.sheet = True                                  # protection                  S-23


def corrupt_import_log(wb):
    ws = wb.create_sheet("Corrupt_Import_Log")                  # prose only -> notes, no table
    for i, line in enumerate(["Import failed at 02:14 because the upstream feed timed out.",
                              "Rows after the failure point were not written to this export.",
                              "Please contact the data engineering team for a corrected file."], 1):
        ws.cell(i, 1, line)


def product_lookup(wb):
    ws = wb.create_sheet("Product_Lookup")                      # clean control table
    ws.append(["SKU", "Product", "Category"])
    for i in range(5):
        ws.append([f"SKU-{i}", f"Product {i}", "Hardware" if i % 2 else "Software"])


def sparse_template(wb):
    ws = wb.create_sheet("Sparse_Template")                     # scattered cells -> fragments S-06
    for r, c in [(1, 1), (5, 8), (12, 3), (20, 15), (33, 2), (47, 11), (60, 6), (80, 19), (95, 4), (120, 20)]:
        ws.cell(r, c, f"field {r}-{c}")


def multi_tables(wb):
    ws = wb.create_sheet("Multi_Tables")                        # S-07 above/beside, S-15 mismatch
    ws["A1"] = "Two independent tables and one more below"
    ws.merge_cells("A1:J1")
    for j, h in enumerate(["Region", "Sales", "Orders", "Returns"]):
        ws.cell(3, 1 + j, h)
    for i in range(5):
        for j, v in enumerate([f"R{i}", 100 + i, 10 + i, i]):
            ws.cell(4 + i, 1 + j, v)
    for j, h in enumerate(["Brand", "Cost", "Units"]):
        ws.cell(4, 8 + j, h)
    for i in range(4):
        for j, v in enumerate([f"B{i}", 50 + i, 5 + i]):
            ws.cell(5 + i, 8 + j, v)
    for j, h in enumerate(["Store", "Rev", "Ord", "Ret", "Tax"]):
        ws.cell(13, 1 + j, h)
    for i in range(4):
        for j, v in enumerate([f"S{i}", 9 + i, 3 + i, 1, 0.5]):
            ws.cell(14 + i, 1 + j, v)
    ws.freeze_panes = "A2"                                       # frozen row 1, header row 3


def aesthetic_gaps(wb):
    ws = wb.create_sheet("Aesthetic_Gaps")                      # S-08: empty row after every data row
    ws.append(["Item", "Qty", "Price"])
    for i in range(15):
        ws.cell(2 + 2 * i, 1, f"Item {i}")
        ws.cell(2 + 2 * i, 2, i + 1)
        ws.cell(2 + 2 * i, 3, round(9.99 + i, 2))


def jan_feb(wb):
    jan = wb.create_sheet("Jan Sales")                           # S-09 family, renamed columns
    jan.append(["Date", "Store", "Revenue", "Orders"])
    for i in range(20):
        jan.append([day(i), f"Store {i % 3}", 1000 + 10 * i, 20 + i])
    feb = wb.create_sheet("Feb Sales ")                          # S-24 trailing space
    feb.append(["Order Date", "Store_ID", "Sales_Amount", "Order_Count", "Region"])
    for i in range(20):
        feb.append([day(31 + i), f"Store {i % 3}", 1100 + 10 * i, 25 + i, "North"])


def stale_pivot(wb):
    ws = wb.create_sheet("Stale_Pivot_Cache")                    # S-21 heuristic, key-value, footer
    ws["A1"] = "Static pivot-style summary"
    ws["A2"], ws["B2"] = "Last Refreshed", "2025-12-31"
    ws["A3"], ws["B3"] = "Region", "Pivot_Revenue"
    for i, (k, v) in enumerate([("North", 120), ("South", 80), ("East", 60), ("West", 40)], start=4):
        ws.cell(i, 1, k)
        ws.cell(i, 2, v)
    ws["A9"], ws["B9"] = "Total", 300                             # row 8 empty separates the footer
    ws.freeze_panes = "A2"


def very_hidden(wb):
    ws = wb.create_sheet("VeryHiddenData")                        # S-25
    ws.append(["Key", "Value"])
    ws.append(["adjustment", -250])
    ws.sheet_state = "veryHidden"


def chart_over_data(wb):
    ws = wb.create_sheet("Chart_Over_Data")                       # S-27
    ws.append(["Month", "Spend"])
    for i in range(12):
        ws.append([f"M{i + 1:02d}", 1000 + 50 * i])
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=13), titles_from_data=True)
    ws.add_chart(chart, "A3")


def build_a(path: Path) -> None:
    wb = openpyxl.Workbook()
    for f in (sales_raw, corrupt_import_log, product_lookup, sparse_template, multi_tables, aesthetic_gaps,
              jan_feb, stale_pivot, very_hidden, chart_over_data):
        f(wb)
    wb.defined_names["Broken_Deleted_Area"] = DefinedName("Broken_Deleted_Area",
                                                          attr_text="'DeletedSheet'!$A$1:$B$2")   # S-20
    wb.save(path)


# ====================================================================== workbook B (edge cases, saved by Excel)


def edge_sheets(wb):
    ws = wb.active
    ws.title = "Deep_Header"                                      # notes + key-values, header at row 15
    ws["A1"] = "Quarterly media plan — internal draft, figures are provisional and subject to change."
    ws["A3"], ws["B3"] = "Client", "Acme Motors"
    ws["A4"], ws["B4"] = "Period", "Q1 2026"
    for j, h in enumerate(["Week", "Channel", "Spend", "Reach"]):
        ws.cell(15, 1 + j, h)
    for i in range(8):
        for j, v in enumerate([day(7 * i), ["TV", "Radio", "Digital"][i % 3], 1000 + 100 * i, 0.3 + i / 100]):
            ws.cell(16 + i, 1 + j, v)

    ws = wb.create_sheet("Two_Row_Header")                        # Q1/Q2 over Budget/Actual -> composite names
    ws["A1"] = None
    ws["B1"], ws["D1"] = "Q1", "Q2"
    ws.merge_cells("B1:C1")
    ws.merge_cells("D1:E1")
    for j, h in enumerate(["Region", "Budget", "Actual", "Budget", "Actual"]):
        ws.cell(2, 1 + j, h)
    for i in range(6):
        ws.append([f"Region {i}", 100 + i, 95 + i, 110 + i, 104 + i])

    ws = wb.create_sheet("Unit_Row")                              # unit row under the header
    ws.append(["Channel", "Spend", "Share", "Volume"])
    ws.append([None, "USD", "%", "units"])
    for i in range(6):
        ws.append([["TV", "Radio", "Print"][i % 3], 1000 + i, 0.1 * (i + 1), 50 * i])

    ws = wb.create_sheet("Merges_In_Data")                        # label merge filled, number merge not
    ws.append(["Region", "City", "Spend"])
    for i, (city, spend) in enumerate([("Mumbai", 100), ("Pune", 80), ("Nagpur", 60), ("Delhi", 120), ("Agra", 40)], 2):
        ws.cell(i, 2, city)
        ws.cell(i, 3, spend)
    ws["A2"] = "West"
    ws.merge_cells("A2:A4")
    ws["A5"] = "North"
    ws.merge_cells("A5:A6")
    ws["C5"] = 160
    ws.merge_cells("C5:C6")                                       # numeric merge

    ws = wb.create_sheet("Wide_Time")                             # years across the columns
    ws.append(["Channel", 2023, 2024, 2025, 2026])
    for ch in ["TV", "Radio", "Digital", "Print"]:
        ws.append([ch] + [random.randint(100, 900) for _ in range(4)])

    ws = wb.create_sheet("Blank_Header_Dates")                    # dates under a blank A1 -> col_A
    ws["B1"], ws["C1"] = "Sales", "Spend"
    for i in range(5):
        ws.cell(2 + i, 1, day(i))
        ws.cell(2 + i, 2, 10 + i)
        ws.cell(2 + i, 3, 3 * i)

    ws = wb.create_sheet("Null_Tokens")                           # kept exactly as written
    ws.append(["Id", "Note"])
    for i, v in enumerate(["null", "  None ", "N/A", "NA", "-", "   ", "real note"], 1):
        ws.append([i, v])

    ws = wb.create_sheet("Totals_Outline")                        # subtotals closing outline groups
    ws.append(["Item", "Qty"])
    rows = [("a", 1), ("b", 2), ("Total", 3), ("c", 4), ("d", 5), ("Total", 9), ("e", 6), ("f", 7)]
    for k, v in rows:
        ws.append([k, v])
    for r in (2, 3, 5, 6):
        ws.row_dimensions[r].outlineLevel = 1

    ws = wb.create_sheet("Excel_Table_Totals")                    # Excel makes A1:C6 a Table with a totals row (7)
    ws.append(["Item", "Qty", "Price"])
    for i in range(5):
        ws.append([f"Item {i}", i + 1, 10.0 + i])
    ws.cell(8, 1, "Item 5")                                        # continues the pattern below the table
    ws.cell(8, 2, 6)
    ws.cell(8, 3, 15.0)
    ws.cell(9, 1, "Item 6")
    ws.cell(9, 2, 7)
    ws.cell(9, 3, 16.0)

    ws = wb.create_sheet("Duplicates_Sparse")                     # duplicate labels, header-only, sparse
    ws.append(["Channel", "Spend", "Spend", "Comment", "Planned"])
    for i in range(20):
        ws.append([["TV", "Radio"][i % 2], 100 + i, 90 + i, "late" if i == 7 else None, None])

    ws = wb.create_sheet("Headerless")                             # numbers only -> columns by letter
    for i in range(6):
        ws.append([1000 + i, 2.5 * i, 7 * i, 3])

    ws = wb.create_sheet("Side_By_Side_1gap")                      # one empty column between tables
    for j, h in enumerate(["Region", "Sales"]):
        ws.cell(1, 1 + j, h)
    for i in range(6):
        ws.cell(2 + i, 1, f"R{i}")
        ws.cell(2 + i, 2, 10 * i)
    for j, h in enumerate(["Brand", "Units", "Share"]):
        ws.cell(1, 4 + j, h)
    for i in range(3):
        ws.cell(2 + i, 4, f"B{i}")
        ws.cell(2 + i, 5, i + 1)
        ws.cell(2 + i, 6, 0.2 * i)

    ws = wb.create_sheet("Footer_Date")                            # Total row under a date column
    ws.append(["Date", "Spend", "Clicks"])
    for i in range(6):
        ws.append([day(i), 100 + i, 10 * i])
    ws.append(["Total", "=SUM(B2:B7)", "=SUM(C2:C7)"])

    ws = wb.create_sheet("Grand_Total_Summary")                    # one row per category + grand total
    ws.append(["Region", "Revenue"])
    for k, v in [("North", 10), ("South", 20), ("East", 30), ("West", 40)]:
        ws.append([k, v])
    ws.append(["Grand Total", 100])

    ws = wb.create_sheet("Values")                                 # rendering edge cases
    ws.append(["Case", "Value"])
    cases = [("float sum", "=0.1+0.2"), ("long decimal", 99999999.98999999), ("percent", 0.05),
             ("huge", 1e20), ("tiny", 1.5e-7), ("leap bug 1900-02-28", dt.date(1900, 2, 28)),
             ("boolean", True), ("error", "#N/A"), ("long text", "x" * 80), ("url", "https://example.com/a"),
             ("leading zeros", "00123"), ("rupees", "₹ 1,200"), ("emoji", "launch 🚀"),
             ("datetime", dt.datetime(2026, 3, 4, 5, 6, 7)), ("time", dt.time(13, 45))]
    for name, v in cases:
        ws.append([name, v])
    ws["B4"].number_format = "0%"
    ws["B12"].number_format = "@"

    wb.create_sheet("Sales@Data").append(["A", "B"])               # sheet keys that collide when sanitised
    wb["Sales@Data"].append([1, 2])
    wb["Sales@Data"].append([3, 4])
    ws = wb.create_sheet("Sales Data")
    ws.append(["Montant ₹", "Région"])
    ws.append([1200, "Île"])
    ws.append([1300, "Ré"])

    ws = wb.create_sheet("Hidden_Sheet")                           # hidden (not very hidden)
    ws.append(["Key", "Value"])
    ws.append(["k", 1])
    ws.append(["j", 2])
    ws.sheet_state = "hidden"

    wb.create_sheet("Empty_Sheet")

    ws = wb.create_sheet("One_Row")                                # a header with no data
    ws.append(["Date", "Channel", "Spend"])

    ws = wb.create_sheet("Validation_Rules")                       # list from a range, whole, text length
    ws.append(["Channel", "Qty", "Code"])
    for ch, q, code in [("TV", 5, "AB"), ("Radio", 50, "ABC"), ("Podcast", 7, "ABCDEFGHIJK"), ("TV", 120, "X")]:
        ws.append([ch, q, code])
    ws["F1"], ws["F2"], ws["F3"] = "TV", "Radio", "Digital"
    for dv, ref in [(DataValidation(type="list", formula1="$F$1:$F$3"), "A2:A5"),
                    (DataValidation(type="whole", operator="between", formula1="1", formula2="100"), "B2:B5"),
                    (DataValidation(type="textLength", operator="lessThanOrEqual", formula1="5"), "C2:C5")]:
        ws.add_data_validation(dv)
        dv.add(ref)

    ws = wb.create_sheet("Hidden_Formats")                          # ;;; must raise S-18, colour scale must not
    ws.append(["Item", "Score", "Secret"])
    for i in range(6):
        ws.append([f"i{i}", 10 * i, 7 * i])
        ws.cell(2 + i, 3).number_format = ";;;"
    ws.conditional_formatting.add("B2:B7", ColorScaleRule(start_type="min", start_color="FFFFFF",
                                                          end_type="max", end_color="63BE7B"))

    ws = wb.create_sheet("Side_Calc")                               # side calculation glued to the table
    ws.append(["Date", "Spend", "Clicks", "Total Clicks"])
    for i in range(6):
        ws.append([day(i), 100 + i, 10 * i, None])
    ws["D2"] = "=SUM(C2:C7)"

    ws = wb.create_sheet("Filter_Source")                           # filtered by Excel below; pivot source
    ws.append(["Region", "Channel", "Spend"])
    for i in range(12):
        ws.append([["North", "South", "East"][i % 3], ["TV", "Radio"][i % 2], 100 + 10 * i])


def build_b(path: Path) -> None:
    wb = openpyxl.Workbook()
    edge_sheets(wb)
    wb.save(path)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    build_a(OUT / "structural_test_A.xlsx")
    build_b(OUT / "structural_test_B.xlsx")
    print("written", OUT / "structural_test_A.xlsx", OUT / "structural_test_B.xlsx")
    if os.name == "nt":
        print("next: powershell -File tests/fixtures/excel_post.ps1  (Excel finishes workbook B)", file=sys.stderr)
