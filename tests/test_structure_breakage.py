"""Regressions for defects found by breaking Phase A on purpose (real Excel files, hostile inputs, Excel round trip).

The .xls/.xlsx fixtures in tests/fixtures/ were written by real Excel 16 (one encrypted with a password)."""
import zipfile
from pathlib import Path

import openpyxl

from conftest import cells, read_json, run, tables, types
from src.structure.review_import import _same_at_excel_precision, import_review
from src.structure.config import resolve_settings
from src.structure.sniff import sniff

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- password detection (real Excel files)

def test_excel_encrypted_workbooks_are_password_protected():
    assert sniff(str(FIX / "excel_encrypted.xlsx")).format == "password"            # OOXML in an OLE container
    assert sniff(str(FIX / "excel_encrypted_legacy.xls")).format == "password"     # BIFF8 FILEPASS record
    assert sniff(str(FIX / "excel_plain_legacy.xls")).format == "xls"              # same file, no password


def test_encrypted_ods_is_password_protected(tmp_path):
    def ods(path, manifest):
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
            z.writestr("META-INF/manifest.xml", manifest)
        return str(path)
    enc = '<manifest:file-entry manifest:full-path="content.xml"><manifest:encryption-data/></manifest:file-entry>'
    assert sniff(ods(tmp_path / "e.ods", enc)).format == "password"
    assert sniff(ods(tmp_path / "p.ods", "<manifest:file-entry/>")).format == "ods"


def test_password_message_tells_the_analyst_what_to_do(tmp_path):
    res, _ = run(tmp_path, [FIX / "excel_encrypted_legacy.xls"])
    item = next(a for a in res["action_items"] if a["type"] == "structure.password_protected")
    assert item["severity"] == "block" and "Remove the password" in item["message"]


# ---------------------------------------------------------------- junk that must not become a table

def test_a_grid_of_whitespace_is_not_a_table(make_xlsx, tmp_path):
    def build(wb):
        for r in range(1, 6):
            for c in range(1, 4):
                wb.active.cell(r, c, "   ")
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    assert not tables(out) and {c["kind"] for c in cells(out)} == {"fragment"}


# ---------------------------------------------------------------- noise that must not be raised

def test_no_llm_fallback_when_no_model_is_pinned(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("A,B\n1,2\n3,4\n", encoding="utf8")
    res, out = run(tmp_path, [f])                                       # default config: llm.mode = assist
    assert not any(x["id"].startswith("llm_fallback") for x in read_json(out, "report.json")["findings"])


def test_a_csv_file_name_is_not_a_sheet_name_issue(tmp_path):
    f = tmp_path / "media.spend@2026.csv"
    f.write_text("A,B\n1,2\n3,4\n", encoding="utf8")
    res, out = run(tmp_path, [f])
    assert not any(x["id"].startswith("S-24") for x in read_json(out, "report.json")["findings"])


# ---------------------------------------------------------------- review workbook: no bleed between sheets

def _two_sheets_same_position(wb):
    first = wb.active
    first.title = "First"
    first.append(["Case", "Value"])
    first.append(["a", 1])
    first.append(["b", "#N/A"])                          # an error cell at row 3, column B of sheet 0's t1
    first.append(["c", 3])
    first["B2"].comment = openpyxl.comments.Comment("first sheet note", "x")
    second = wb.create_sheet("Second")
    second.append(["Item", "Qty"])
    second.append(["x", 10])
    second.append(["y", 20])                             # same position, an ordinary number, on sheet 1's t1
    second.append(["z", 30])


def test_types_and_comments_do_not_bleed_between_sheets(make_xlsx, tmp_path):
    res, out = run(tmp_path, [make_xlsx(builder=_two_sheets_same_position)])
    wb = openpyxl.load_workbook(out / "review" / "Structural_Review.xlsx")
    second = wb[next(n for n in wb.sheetnames if n.endswith("second"))]
    assert second["C3"].data_type == "n" and second["C3"].value == 20             # not written as an error cell
    first = wb[next(n for n in wb.sheetnames if n.endswith("first"))]
    assert first["C3"].data_type == "e" and first["C3"].value == "#N/A"
    rows = list(wb["Structural_Findings"].iter_rows(values_only=True))
    comments = {r[2]: r[9] for r in rows[1:] if r[0]}
    assert "first sheet note" in comments["First"] and not comments["Second"]


# ---------------------------------------------------------------- Excel rounds numbers to 15 digits on save

def test_excel_precision_is_not_an_edit():
    assert _same_at_excel_precision("0.3", "n", "0.30000000000000004")
    assert not _same_at_excel_precision("0.31", "n", "0.30000000000000004")
    assert not _same_at_excel_precision("0.3", "s", "0.30000000000000004")


def test_resaving_rounded_numbers_gives_no_cell_edits(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Case", "Value"])
        ws.append(["sum", 0.1 + 0.2])                    # 0.30000000000000004: 17 significant digits
        ws.append(["other", 2])
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    path = out / "review" / "Structural_Review.xlsx"
    wb = openpyxl.load_workbook(path)
    ws = wb[next(n for n in wb.sheetnames if n.startswith("T1"))]
    ws["C2"] = 0.3                                       # what Excel writes back after open-and-save
    resaved = tmp_path / "resaved.xlsx"
    wb.save(resaved)
    inv = read_json(out, "inventory.json")
    patch, edits, violations = import_review(str(resaved), out, inv, resolve_settings({}), [])
    assert not violations and edits == []


# ---------------------------------------------------------------- merged label across an empty column

def test_a_merge_across_an_empty_column_keeps_the_table_whole(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        ws["A1"], ws["C1"] = "Ids", "Metrics"
        ws.merge_cells("A1:B1")
        ws.merge_cells("C1:E1")                           # spans the empty column D
        for j, h in enumerate(["Id", "Name", "Spend", None, "Hidden"]):
            if h:
                ws.cell(2, 1 + j, h)
        for i in range(5):
            for j, v in enumerate([i, f"n{i}", 10 * i, None, f"h{i}"]):
                if v is not None:
                    ws.cell(3 + i, 1 + j, v)
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    assert len(tables(out)) == 1
    sheet = read_json(out, next(out.glob("files/*/sheets/00.json")).relative_to(out).as_posix())
    assert sheet["tables"][0]["dropped_columns"] == ["D"]                        # the spacer column is listed
    assert "structure.S-07.soft_split" not in types(res)
