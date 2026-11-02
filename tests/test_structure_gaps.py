"""Review-driven fixes: the Issues/Changes sheets, sheet JSON (§9.4), invariants, footers, S-10/S-13/S-21/S-22/S-26,
unread sheets and parallel files."""
import datetime

import zipfile

import openpyxl
import pytest
from openpyxl.worksheet.table import Table as XlTable
from openpyxl.worksheet.table import TableColumn

from conftest import cells, read_json, rows, run, simple_sheet, tables, types
from src.structure import pipeline
from src.structure.extract import InvariantViolation, _check_invariants, analyze_sheet
from src.structure.config import resolve_settings
from test_structure_reader import package


def media_book(wb):
    """The shape of samples/sample_media_spend.xlsx: title, group row, header, data, hidden row, Total row."""
    ws = wb.active
    ws.title = "Media Spend"
    ws["B1"] = "Media Spend Report"
    ws.merge_cells("B1:F1")
    ws["B2"], ws["D2"] = "Identifiers", "Metrics"
    ws.merge_cells("B2:C2")
    ws.merge_cells("D2:F2")
    for j, h in enumerate(["Date", "Channel", "Spend", "Clicks", "Notes"]):
        ws.cell(3, 2 + j, h)
    for i in range(10):
        ws.cell(4 + i, 2, datetime.date(2026, 1, 1 + i))
        ws.cell(4 + i, 3, "TV" if i % 2 else "Radio")
        ws.cell(4 + i, 4, 100.5 + i)
        ws.cell(4 + i, 5, i * 10)
    ws.row_dimensions[6].hidden = True
    ws["B15"], ws["D15"], ws["E15"] = "Total", "=SUM(D4:D13)", "=SUM(E4:E13)"


def test_total_row_under_a_date_column_is_a_footer(make_xlsx, tmp_path):
    res, out = run(tmp_path, [make_xlsx(builder=media_book)])
    data = rows(tables(out)[0])
    assert data[-1]["_source_row"] == 13 and all(r["Date"] != "Total" for r in data)
    assert [c["ref"] for c in cells(out) if c["kind"] == "footer"] == ["B15", "D15", "E15"]


def test_review_workbook_lists_issues_by_status_and_every_change(make_xlsx, tmp_path):
    res, out = run(tmp_path, [make_xlsx(builder=media_book)])
    wb = openpyxl.load_workbook(out / "review" / "Structural_Review.xlsx")
    assert wb.sheetnames[:4] == ["README", "Structural_Findings", "Issues", "Changes"]
    issues = list(wb["Issues"].iter_rows(values_only=True))
    header, body = issues[0], issues[1:]
    by_code = {r[header.index("Code")]: r for r in body}
    assert by_code["S-11.hidden_rows"][0] == "Warning"                         # hidden rows: included and warned
    assert by_code["S-01.placement"][0] == "Fixed"                             # header promoted, title moved out
    assert by_code["footer_rows"][0] == "Fixed"
    assert by_code["S-26.recalc_on_open"][0] == "Identified"
    statuses = [r[0] for r in body]
    assert statuses == sorted(statuses, key=["Warning", "Fixed", "Identified"].index)   # warnings first
    assert all(r[header.index("Action taken")] for r in body)                 # every issue says what was done
    changes = list(wb["Changes"].iter_rows(values_only=True))
    actions = {r[changes[0].index("Change")] for r in changes[1:]}
    assert {"peel title", "exclude footer", "attach groups"} <= actions
    table_sheet = next(n for n in wb.sheetnames if n.startswith("T1"))
    assert wb[table_sheet].max_row == 11                                       # the fixed table: header + 10 rows


def test_sheet_json_follows_the_spec_shape(make_xlsx, tmp_path):
    res, out = run(tmp_path, [make_xlsx(builder=media_book)])
    sheet = read_json(out, next(out.glob("files/*/sheets/00.json")).relative_to(out).as_posix())
    assert sheet["schema"] == "dp.structure.sheet/1" and sheet["sheet_name"] == "Media Spend"
    f = sheet["features"]
    assert f["hidden_rows"] == [[6, 6]] and f["formulas"]["uncached"] == 2
    assert {m["role"] for m in f["merged"]} == {"title", "header"}
    t = sheet["tables"][0]
    assert t["header"]["rows"] == [3] and t["header"]["group_rows"] == [2]
    assert t["data"]["footer_rows"] == [15] and t["outputs"]["data"].startswith("files/")
    assert any(i.startswith("S-11.hidden_rows@") for i in t["findings"])
    assert sheet["llm"]["mode"] in ("off", "assist")


def test_invariants_catch_a_changed_value_and_bad_row_order(make_xlsx, tmp_path):
    from src.structure.xlsx import read_xlsx
    wb = read_xlsx(str(make_xlsx(builder=lambda w: simple_sheet(w.active))))
    sheet = wb.sheets[0]
    params = resolve_settings({}).params
    res = analyze_sheet(sheet, params, "0" * 12)
    t = res.tables[0]
    key = next(iter(t.cells))
    t.cells[key] = type(t.cells[key])("N", "tampered", "n")
    with pytest.raises(InvariantViolation, match="I-02"):
        _check_invariants(sheet, res.tables, res.placements)
    res = analyze_sheet(sheet, params, "0" * 12)
    res.tables[0].data_rows.reverse()
    with pytest.raises(InvariantViolation, match="I-04"):
        _check_invariants(sheet, res.tables, res.placements)


def test_excel_table_names_fill_blank_header_cells(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws["A1"], ws["B1"] = "Item", "Qty"
        for i in range(2, 6):
            ws.cell(i, 1, f"i{i}")
            ws.cell(i, 2, i)
        table = XlTable(displayName="Stock", ref="A1:B5")
        table.tableColumns = [TableColumn(id=1, name="Item"), TableColumn(id=2, name="Qty")]
        ws.add_table(table)
        ws["B1"] = None                                  # header cell blank; the table part still says "Qty"
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    assert set(rows(tables(out)[0])[0]) >= {"Item", "Qty"}


def test_numeric_merge_is_annotated_and_merges_counted_by_class(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        simple_sheet(ws, n=6)
        ws["C3"] = 999
        ws.merge_cells("C3:C4")
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    assert [c["ref"] for c in cells(out) if c["kind"] == "merge_numeric"] == ["C3"]
    assert types(res)["structure.S-10.numeric_merge"] == "warn"
    detail = read_json(out, "report.json")["findings"]
    assert any("1 data" in f["message"] for f in detail if f["id"].startswith("S-10.merges"))


def test_grand_total_summary_is_suspected(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Region", "Revenue"])
        for k, v in [("North", 10), ("South", 20), ("East", 30)]:
            ws.append([k, v])
        ws.append(["Grand Total", 60])
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    assert types(res)["structure.S-21.suspected_static_summary"] == "info"


def test_outlined_subtotals_are_noted(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Item", "Qty"])
        for r, (k, v) in enumerate([("a", 1), ("b", 2), ("Total", 3), ("c", 4)], start=2):
            ws.append([k, v])
        for r in (2, 3):
            ws.row_dimensions[r].outlineLevel = 1
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    msg = next(a["message"] for a in res["action_items"] if a["type"] == "structure.S-13.row_outline")
    assert "close outlined groups" in msg


def test_external_link_parts_are_reported(tmp_path):
    path = package(tmp_path / "t.xlsx", "<row r='1'><c r='A1' t='s'><v>0</v></c><c r='B1' t='s'><v>1</v></c></row>"
                   "<row r='2'><c r='A2'><v>1</v></c><c r='B2'><v>2</v></c></row>"
                   "<row r='3'><c r='A3'><v>3</v></c><c r='B3'><v>4</v></c></row>", shared=["Id", "Value"])
    with zipfile.ZipFile(path) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b"</Relationships>", b"<Relationship Id='rId9' Type='http://schemas.openxmlformats.org/officeDocument/2006/"
                             b"relationships/externalLink' Target='externalLinks/externalLink1.xml'/></Relationships>")
    with zipfile.ZipFile(path, "w") as z:
        for n, b in parts.items():
            z.writestr(n, b)
    from pathlib import Path
    res, out = run(tmp_path, [Path(path)])
    assert types(res)["structure.S-26.external_links"] == "info"


def test_a_sheet_that_fails_mid_read_keeps_its_cells_as_unread(tmp_path):
    from pathlib import Path
    path = package(tmp_path / "t.xlsx", "<row r='1'><c r='A1'><v>1</v></c><c r='B1'><v>2</v></c></row>"
                                        "<row r='2'><c r='A2'><v>not a number</v></c></row>")
    res, out = run(tmp_path, [Path(path)])
    assert types(res)["structure.sheet_error"] in ("warn", "block")
    assert {c["ref"] for c in cells(out) if c["kind"] == "unread"} == {"A1", "B1"}


def test_a_file_without_any_table_is_written(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws["A1"] = "This export failed half way through the night run."
        ws["A2"] = "Please contact the data team for a corrected file."
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    assert not tables(out) and {c["kind"] for c in cells(out)} == {"notes"}
    assert any(f["id"].startswith("sheet_no_table@") for f in read_json(out, "report.json")["findings"])


def test_parallel_files_give_the_same_output(make_xlsx, tmp_path, monkeypatch):
    books = [make_xlsx(f"b{i}.xlsx", lambda wb, i=i: simple_sheet(wb.active, n=5 + i)) for i in range(3)]
    serial, _ = run(tmp_path, books, out_name="serial")
    monkeypatch.setattr(pipeline, "PARALLEL_MIN_BYTES", 0)
    parallel, _ = run(tmp_path, books, out_name="parallel")
    assert serial["artifacts"] == parallel["artifacts"]
    assert [a["id"] for a in serial["action_items"]] == [a["id"] for a in parallel["action_items"]]


def test_csv_is_decoded_as_a_stream(tmp_path):
    f = tmp_path / "big.csv"
    f.write_bytes(("Id;Name\n" + "".join(f"{i};Zoë{i}\n" for i in range(2000))).encode("cp1252"))
    res, out = run(tmp_path, [f])
    data = rows(tables(out)[0])
    assert len(data) == 2000 and data[0]["Name"] == "Zoë0"
    assert "structure.csv_encoding_guessed" in types(res)


def test_review_workbook_bytes_do_not_depend_on_the_clock(tmp_path):
    import time
    f = tmp_path / "a.csv"
    f.write_text("K,V\nx,1\ny,2\n", encoding="utf8")
    first, _ = run(tmp_path, [f], out_name="o1")
    time.sleep(1.1)                                        # the second save lands on a later clock second
    second, _ = run(tmp_path, [f], out_name="o2")
    key = "review/Structural_Review.xlsx"
    assert first["artifacts"][key] == second["artifacts"][key]


def test_a_sheet_filter_belongs_only_to_the_table_it_covers(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        simple_sheet(ws, n=6, row0=1, col0=1)
        simple_sheet(ws, ("Brand", "Cost", "Units"), n=6, row0=1, col0=8)
        ws.auto_filter.ref = "A1:C7"                          # covers the first table only
        ws.auto_filter.add_filter_column(1, ["TV"])
        ws.row_dimensions[3].hidden = True
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    t1, t2 = (rows(p) for p in tables(out))
    assert next(r for r in t1 if r["_source_row"] == 3)["_row_filtered"] is True
    assert next(r for r in t2 if r["_source_row"] == 3)["_row_filtered"] is False     # not under the filter
    filters = [a for a in res["action_items"] if a["type"].startswith("structure.S-16")]
    assert len(filters) == 1 and " t1 " in filters[0]["message"]
