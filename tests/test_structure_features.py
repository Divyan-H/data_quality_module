"""Excel feature findings S-09 to S-28 (spec 5.3; acceptance criteria 17-24, 26)."""
from openpyxl.chart import BarChart, Reference
from openpyxl.comments import Comment
from openpyxl.formatting.rule import ColorScaleRule, Rule
from openpyxl.styles import Font, PatternFill
from openpyxl.styles.differential import DifferentialStyle
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation

from conftest import cells, read_json, run, simple_sheet, types

SELECT_DATA = {"structure": {"selection": {"s-1": [{"sheet": "Data", "table": "only"}]}}}


def data_book(build):
    def _b(wb):
        ws = wb.active
        ws.title = "Data"
        simple_sheet(ws, n=10)
        build(wb, ws)
    return _b


def finding_ids(out):
    return {f["id"].split("@")[0] for f in read_json(out, "report.json")["findings"]}


def test_hidden_rows_cols_and_outline(make_xlsx, tmp_path):
    def build(wb, ws):
        ws.row_dimensions[4].hidden = True
        ws.row_dimensions[5].outlineLevel = 1
        ws.column_dimensions["C"].hidden = True
        ws.column_dimensions["B"].outlineLevel = 1
    res, out = run(tmp_path, [make_xlsx(builder=data_book(build))], SELECT_DATA)
    t = types(res)
    assert t["structure.S-11.hidden_rows"] == "warn" and t["structure.S-12.hidden_cols"] == "warn"
    assert t["structure.S-13.row_outline"] == "info" and t["structure.S-14.col_outline"] == "info"
    assert len([r for r in __import__("conftest").rows(next(out.glob("files/*/tables/*/data.parquet")))]) == 10   # nothing dropped


def test_frozen_panes_only_reported_on_disagreement(make_xlsx, tmp_path):
    agree, _ = run(tmp_path, [make_xlsx("a.xlsx", data_book(lambda wb, ws: setattr(ws, "freeze_panes", "A2")))], SELECT_DATA, "o1")
    assert "structure.S-15.freeze_mismatch" not in types(agree)
    disagree, _ = run(tmp_path, [make_xlsx("b.xlsx", data_book(lambda wb, ws: setattr(ws, "freeze_panes", "A4")))], SELECT_DATA, "o2")
    assert types(disagree)["structure.S-15.freeze_mismatch"] == "info"


def test_autofilter_with_criteria_hides_rows(make_xlsx, tmp_path):
    def build(wb, ws):
        ws.auto_filter.ref = "A1:C11"
        ws.auto_filter.add_filter_column(1, ["TV"])
        ws.row_dimensions[3].hidden = True
    res, out = run(tmp_path, [make_xlsx(builder=data_book(build))], SELECT_DATA)
    t = types(res)
    assert t["structure.S-16.filtered_rows"] == "warn" and "structure.S-11.hidden_rows" not in t   # S-11 excludes filtered rows
    import pyarrow.parquet as pq
    data = pq.read_table(next(out.glob("files/*/tables/*/data.parquet"))).to_pylist()
    assert next(r for r in data if r["_source_row"] == 3)["_row_filtered"] is True


def test_print_area_never_crops(make_xlsx, tmp_path):
    def build(wb, ws):
        ws.print_area = "A1:B4"
    res, out = run(tmp_path, [make_xlsx(builder=data_book(build))], SELECT_DATA)
    assert types(res)["structure.S-17.print_area"] == "info"
    assert len(__import__("conftest").rows(next(out.glob("files/*/tables/*/data.parquet")))) == 10


def test_formatting_that_hides_values(make_xlsx, tmp_path):
    def build(wb, ws):
        hide = DifferentialStyle(font=Font(color="FFFFFF"), fill=PatternFill(bgColor="FFFFFF", fill_type="solid"))
        ws.conditional_formatting.add("C2:C4", Rule(type="expression", formula=["TRUE"], dxf=hide))
        ws.conditional_formatting.add("C6:C8", ColorScaleRule(start_type="min", start_color="FFFFFF",
                                                              end_type="max", end_color="000000"))
        ws["B9"].font = Font(color="FFFFFF")
        ws["B9"].fill = PatternFill("solid", fgColor="FFFFFF")
    res, out = run(tmp_path, [make_xlsx(builder=data_book(build))], SELECT_DATA)
    assert types(res)["structure.S-18.hidden_values"] == "warn"
    marked = sorted(c["ref"] for c in cells(out) if c["kind"] == "invisible_format")
    assert marked == ["B9", "C2", "C3", "C4"]                                # the colour scale raises nothing (K-12)


def test_data_validation_violations(make_xlsx, tmp_path):
    def build(wb, ws):
        dv = DataValidation(type="list", formula1='"TV,Radio"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add("B2:B11")
        ws["B5"], ws["B6"] = "Print", "Web"
    res, out = run(tmp_path, [make_xlsx(builder=data_book(build))], SELECT_DATA)
    assert types(res)["structure.S-19.validation"] == "info"
    assert sorted(c["ref"] for c in cells(out) if c["kind"] == "dv_violation") == ["B5", "B6"]


def test_broken_named_range_reported_once_per_file(make_xlsx, tmp_path):
    def build(wb, ws):
        wb.defined_names["Gone"] = DefinedName("Gone", attr_text="'DeletedSheet'!$A$1:$B$2")
        wb.create_sheet("Other")
    res, out = run(tmp_path, [make_xlsx(builder=data_book(build))], SELECT_DATA)
    f = [x for x in read_json(out, "report.json")["extras"]["structure"]["findings_detail"] if x["codes"] == ["S-20"]]
    assert len(f) == 1 and f[0]["locations"] == [{"name": "Gone", "class": "missing_sheet"}]


def test_hidden_sheet_and_protection_and_special_name(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws.title = " Odd@Name "
        simple_sheet(ws)
        ws.protection.sheet = True
        hid = wb.create_sheet("Secret")
        simple_sheet(hid)
        hid.sheet_state = "veryHidden"
    cfg = {"structure": {"selection": {"s-1": [{"sheet": "Secret", "table": "only", "include_hidden": True}]}}}
    res, out = run(tmp_path, [make_xlsx(builder=build)], cfg)
    t = types(res)
    assert t["structure.S-25.hidden_sheet"] == "warn"                        # warn only because it is selected
    auto = {"structure": {"selection": {"s-1": [{"sheet": "Secret", "table": "only"}]}}}
    res2, _ = run(tmp_path, [make_xlsx("c.xlsx", build)], auto, "o2")
    assert types(res2)["structure.selection_not_found"] == "block"           # never matched without include_hidden
    assert "S-23.protected" in finding_ids(out) and "S-24.sheet_name" in finding_ids(out)


def test_uncached_external_and_broken_formulas(make_xlsx, tmp_path):
    def build(wb, ws):
        ws["D1"] = "Calc"
        for r in range(2, 12):
            ws.cell(r, 4, f"=C{r}*2")
        ws["D5"] = "=[1]Other!A1"
        ws["D6"] = "=SUM(#REF!)"
        wb.calculation.fullCalcOnLoad = True
    res, out = run(tmp_path, [make_xlsx(builder=data_book(build))], SELECT_DATA)
    assert types(res)["structure.S-26.formulas"] == "block"
    detail = next(x for x in read_json(out, "report.json")["extras"]["structure"]["findings_detail"]
                  if x["id"].startswith("S-26.formulas"))
    checks = {l["check"] for l in detail["locations"]}
    assert checks == {"uncached", "external_link", "broken_reference"}
    assert types(res)["structure.S-26.recalc_on_open"] == "info"


def test_chart_over_data_and_comments(make_xlsx, tmp_path):
    def build(wb, ws):
        chart = BarChart()
        chart.add_data(Reference(ws, min_col=3, min_row=1, max_row=6), titles_from_data=True)
        ws.add_chart(chart, "A3")
        ws["C4"].comment = Comment("use the actual value", "me")
    res, out = run(tmp_path, [make_xlsx(builder=data_book(build))], SELECT_DATA)
    t = types(res)
    assert t["structure.S-27.objects"] == "info" and t["structure.S-28.comments"] == "info"
    comment = next(c for c in cells(out) if c["kind"] == "comment")
    assert "actual value" in comment["detail"]                               # kept as annotation, never applied
    assert all("actual value" not in a["message"] for a in res["action_items"])   # never in a message (E-13)


def test_static_summary_heuristic(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws.title = "Data"
        ws["A1"] = "Static pivot-style summary"
        ws["A2"], ws["B2"] = "Last Refreshed", "2025-01-01"
        ws["A3"], ws["B3"] = "Pivot_Revenue", "Total"
        for i, k in enumerate("XYZ", start=4):
            ws.cell(i, 1, k)
            ws.cell(i, 2, i)
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    assert types(res)["structure.S-21.suspected_static_summary"] == "info"


def test_family_across_tabs_and_layout_groups(make_xlsx, tmp_path):
    def build(wb):
        jan = wb.active
        jan.title = "Jan Sales"
        simple_sheet(jan, ("Date", "Store", "Revenue", "Orders"), n=5)
        for r in range(1, 7):
            jan.cell(r, 4, r if r > 1 else "Orders")
        feb = wb.create_sheet("Feb Sales ")
        simple_sheet(feb, ("Order Date", "Store_ID", "Sales_Amount", "Order_Count", "Region"), n=5)
        for r in range(2, 7):
            feb.cell(r, 4, r)
            feb.cell(r, 5, "N")
    cfg = {"structure": {"selection": {"s-1": [{"table": "t1", "many": True}]}}}
    # `many` needs a sheet selector per tab: select both tabs explicitly.
    cfg = {"structure": {"selection": {"s-1": [{"sheet": "Jan Sales", "table": "only"}, {"sheet": "Feb Sales", "table": "only"}]}}}
    res, out = run(tmp_path, [make_xlsx(builder=build)], cfg)
    t = types(res)
    assert t["structure.S-09.family_drift"] == "info" and t["structure.multiple_layouts"] == "info"
    inv = read_json(out, "inventory.json")
    assert len(inv["selection"][0]["layout_groups"]) == 2                    # nothing is blocked
    assert not res["blocking"]
    msg = next(a["message"] for a in res["action_items"] if a["type"] == "structure.S-09.family_drift")
    assert "Order Date" in msg and "Store_ID" in msg and "(name)" in msg and "(position)" in msg
