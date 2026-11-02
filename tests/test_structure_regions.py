"""Regions, headers and naming (spec 4.4-4.7; acceptance criteria 1-3, 6-8, 10-13, 15, 16)."""
import datetime

from conftest import cells, read_json, rows, run, simple_sheet, tables, types


def test_clean_csv_keeps_stored_text_and_csv_record_numbers(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("Date,Channel,Spend\n2025-01-01,TV,100\n2025-01-02,Radio,50.5\n", encoding="utf8")
    res, out = run(tmp_path, [f])
    data = rows(tables(out)[0])
    assert [r["Channel"] for r in data] == ["TV", "Radio"] and data[1]["Spend"] == "50.5"     # P1: the stored text
    assert data[0]["_row_id"].endswith(":0:t1:2") and not res["blocking"]                       # header record counts


def test_table_at_c3_uses_excel_coordinates(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws["C3"], ws["D3"] = "Date", "Sales"
        ws["C4"], ws["D4"] = "2025-01-01", 10
        ws["C5"], ws["D5"] = "2025-01-02", 20
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    (t,) = tables(out)
    data = rows(t)
    assert [r["_source_row"] for r in data] == [4, 5]
    assert set(data[0]) >= {"Date", "Sales"}
    sheet = read_json(out, next(out.glob("files/*/sheets/00.json")).relative_to(out).as_posix())
    assert sheet["tables"][0]["range"] == "C3:D5"                 # criterion 1


def test_merged_title_over_header(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws["B2"] = "Quarterly report"
        ws.merge_cells("B2:D2")
        ws["B4"], ws["C4"], ws["D4"] = "Date", "Sales", "Spend"
        for r in (5, 6):
            ws.cell(r, 2, f"2025-01-0{r}")
            ws.cell(r, 3, r)
            ws.cell(r, 4, r * 2)
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    assert [c["kind"] for c in cells(out) if c["category"] == "placement"] == ["title"]   # criterion 2
    sheet = read_json(out, next(out.glob("files/*/sheets/00.json")).relative_to(out).as_posix())
    assert sheet["tables"][0]["range"] == "B4:D6"


def test_dates_under_blank_a1_are_kept(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws["B1"], ws["C1"] = "Sales", "Spend"
        for r in (2, 3, 4):
            ws.cell(r, 1, datetime.date(2025, 1, r))
            ws.cell(r, 2, r)
            ws.cell(r, 3, r * 3)
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    (t,) = tables(out)
    data = rows(t)
    assert "col_A" in data[0] and data[0]["col_A"] == "2025-01-02"          # criterion 3, K-02
    assert "S-01.blank_header" in {a["type"].replace("structure.", "") for a in res["action_items"]}


def test_null_like_text_is_kept_unchanged(tmp_path):
    f = tmp_path / "n.csv"
    f.write_text("Id,Note\n1,null\n2,  None \n3,N/A\n4,-\n5,   \n", encoding="utf8")
    res, out = run(tmp_path, [f])
    (t,) = tables(out)
    assert [r["Note"] for r in rows(t)] == ["null", "  None ", "N/A", "-", "   "]     # criterion 6, K-06


def test_year_headers_keep_their_names(tmp_path):
    f = tmp_path / "y.csv"
    f.write_text("Channel,2023,2024\nTV,10,20\nRadio,5,6\nWeb,1,2\n", encoding="utf8")
    res, out = run(tmp_path, [f])
    (t,) = tables(out)
    assert {"2023", "2024"} <= set(rows(t)[0])                                # criterion 12, K-07
    assert types(res).get("structure.S-03.wide_time_layout") is None


def test_errors_and_uncached_formulas_are_annotated(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Id", "Calc", "Err"])
        for i in range(1, 6):
            ws.append([i, f"=A{i + 1}*2", "#DIV/0!" if i == 2 else i])
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    kinds = {c["kind"] for c in cells(out) if c["category"] == "annotation"}
    assert {"formula_uncached", "error"} <= kinds                             # criteria 7, 8
    (t,) = tables(out)
    assert all(r["Calc"] is None for r in rows(t))


def test_multi_tables_split_above_and_beside(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws["A1"] = "Two independent tables"
        ws.merge_cells("A1:J1")
        simple_sheet(ws, ("Region", "Sales", "Orders", "Returns"), n=5, row0=3, col0=1)
        simple_sheet(ws, ("Brand", "Cost", "Units"), n=4, row0=4, col0=8)
        simple_sheet(ws, ("Store", "Rev", "Ord", "Ret", "Tax"), n=4, row0=13, col0=1)
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    sheet = read_json(out, next(out.glob("files/*/sheets/00.json")).relative_to(out).as_posix())
    assert [t["range"] for t in sheet["tables"]] == ["A3:D8", "H4:J8", "A13:E17"]   # criterion 10
    assert [t["table_key"] for t in sheet["tables"]] == ["t1", "t2", "t3"]


def test_aesthetic_gaps_removed_with_one_audit_entry(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Item", "Qty", "Cost"])
        for i in range(1, 8):
            ws.append([f"i{i}", i, i * 2])
            ws.append([None, None, None])
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    import pyarrow.parquet as pq
    audit = pq.read_table(next(out.glob("files/*/audit.parquet"))).to_pylist()
    gap = [a for a in audit if a["action"] == "remove_gap_rows"]
    assert len(gap) == 1 and gap[0]["count"] >= 6                              # criterion 11


def test_composite_names_only_when_needed(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws["A1"], ws["C1"] = "Q1", "Q2"
        ws.merge_cells("A1:B1")
        ws.merge_cells("C1:D1")
        for c, h in enumerate(["Budget", "Actual", "Budget", "Actual"], 1):
            ws.cell(2, c, h)
        for r in range(3, 8):
            for c in range(1, 5):
                ws.cell(r, c, r * c)
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    import json
    cols = json.loads(next(out.glob("files/*/tables/*/columns.json")).read_text())["columns"]
    assert [c["name"] for c in cols] == ["Q1_Budget", "Q1_Actual", "Q2_Budget", "Q2_Actual"]
    assert cols[0]["groups"] == ["Q1"]


def test_unit_row_is_attached_not_data(tmp_path):
    f = tmp_path / "u.csv"
    f.write_text("Channel,Spend,Reach\n,USD,%\nTV,100,20\nRadio,50,10\nWeb,20,5\n", encoding="utf8")
    res, out = run(tmp_path, [f])
    import json
    cols = json.loads(next(out.glob("files/*/tables/*/columns.json")).read_text())["columns"]
    assert [c["unit"] for c in cols][1:] == ["USD", "%"]


def test_excel_table_with_totals_row(make_xlsx, tmp_path):
    from openpyxl.worksheet.table import Table

    def build(wb):
        ws = wb.active
        ws["A1"], ws["B1"] = "Item", "Qty"
        for i in range(2, 6):
            ws.cell(i, 1, f"i{i}")
            ws.cell(i, 2, i)
        ws["A6"], ws["B6"] = "Total", 14
        t = Table(displayName="T1", ref="A1:B6")
        t.totalsRowCount = 1
        ws.add_table(t)
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    (t,) = tables(out)
    assert [r["Item"] for r in rows(t)] == ["i2", "i3", "i4", "i5"]
    assert any(c["kind"] == "totals_row" for c in cells(out))


def test_near_tie_raises_uncertain_header(tmp_path):
    f = tmp_path / "t.csv"
    f.write_text("a,b,c\nd,e,f\ng,h,i\nj,k,l\n", encoding="utf8")            # text-only: two rows tie
    res, out = run(tmp_path, [f])
    assert types(res).get("structure.S-01.uncertain_header") == "warn"        # criterion 16
