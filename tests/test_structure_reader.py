"""The streaming workbook reader on hand-written packages (spec 4.2; criteria 5, 8, 9, 38)."""
import zipfile

import pytest

from src.structure.errors import FileUnreadable
from src.structure.sniff import OLE2_MAGIC, sniff
from src.structure.xlsx import read_xlsx

MAIN = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
REL = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
STYLES = f"""<styleSheet {MAIN}>
<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/></numFmts>
<fonts count="2"><font/><font><color rgb="FFFFFFFF"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFFFFFF"/></patternFill></fill></fills>
<cellXfs count="6"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/><xf numFmtId="9"/><xf numFmtId="21"/>
<xf numFmtId="0" fontId="1" fillId="2"/></cellXfs></styleSheet>"""


def package(path, sheet_xml: str, date1904: bool = False, shared: list[str] | None = None):
    """A minimal XLSX with one sheet; style 0 General, 1 date, 2 date-time, 3 percent, 4 time, 5 white on white."""
    strings = "".join(f"<si><t>{s}</t></si>" for s in shared or [])
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>")
        z.writestr("xl/workbook.xml", f"<workbook {MAIN} {REL}><workbookPr date1904='{int(date1904)}'/>"
                   f"<sheets><sheet name='S' sheetId='1' r:id='rId1'/></sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
                   "<Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet' Target='worksheets/sheet1.xml'/>"
                   "<Relationship Id='rId2' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings' Target='sharedStrings.xml'/>"
                   "</Relationships>")
        z.writestr("xl/styles.xml", STYLES)
        z.writestr("xl/sharedStrings.xml", f"<sst {MAIN}>{strings}</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {MAIN}><sheetData>{sheet_xml}</sheetData></worksheet>")
    return str(path)


def read(tmp_path, sheet_xml, **kw):
    wb = read_xlsx(package(tmp_path / "t.xlsx", sheet_xml, **kw))
    return wb.sheets[0].cells


def test_numbers_render_as_the_shortest_stored_decimal_never_the_display_text(tmp_path):
    cells = read(tmp_path, "<row r='1'><c r='A1'><v>99999999.98999999</v></c><c r='B1' s='3'><v>0.05</v></c>"
                           "<c r='C1'><v>45658</v></c><c r='D1'><v>1.5E-7</v></c></row>")
    assert [cells[(1, c)].text for c in (1, 2, 3, 4)] == ["99999999.99", "0.05", "45658", "1.5E-07"]
    assert cells[(1, 2)].fmt == "0%"


def test_dates_render_identically_in_both_date_systems(tmp_path):
    xml = "<row r='1'><c r='A1' s='1'><v>{}</v></c><c r='B1' s='2'><v>{}.4375</v></c><c r='C1' s='4'><v>0.4375</v></c></row>"
    d1900 = read(tmp_path, xml.format(45658, 45658))
    d1904 = read(tmp_path, xml.format(45658 - 1462, 45658 - 1462), date1904=True)
    assert [d1900[(1, c)].text for c in (1, 2, 3)] == ["2025-01-01", "2025-01-01T10:30:00", "10:30:00"]
    assert [d1904[(1, c)].text for c in (1, 2, 3)] == [d1900[(1, c)].text for c in (1, 2, 3)]    # criterion 9


def test_excel_leap_year_bug_and_out_of_range_serials_stay_numbers(tmp_path):
    cells = read(tmp_path, "<row r='1'><c r='A1' s='1'><v>1</v></c><c r='B1' s='1'><v>59</v></c>"
                           "<c r='C1' s='1'><v>60</v></c><c r='D1' s='1'><v>61</v></c><c r='E1' s='1'><v>0</v></c>"
                           "<c r='F1' s='1'><v>2958466</v></c></row>")
    assert [cells[(1, c)].text for c in range(1, 7)] == ["1900-01-01", "1900-02-28", "60", "1900-03-01", "0", "2958466"]
    assert [cells[(1, c)].out_of_range for c in range(1, 7)] == [False, False, True, False, True, True]


def test_cell_types_and_missing_coordinates(tmp_path):
    cells = read(tmp_path, "<row r='2'><c t='s'><v>0</v></c><c t='inlineStr'><is><t>inline</t></is></c><c t='b'><v>1</v></c>"
                           "<c t='e'><v>#DIV/0!</v></c><c t='d'><v>2025-03-04T05:06:07</v></c><c><v>7</v></c></row>",
                 shared=["shared _x0041_"])
    assert [cells[(2, c)].text for c in range(1, 7)] == ["shared A", "inline", "TRUE", "#DIV/0!", "2025-03-04T05:06:07", "7"]
    assert cells[(2, 4)].error and cells[(2, 3)].cls == "B"          # no `r` attribute: the position decides


def test_formulas_use_stored_results_and_never_evaluate(tmp_path):
    cells = read(tmp_path, "<row r='1'><c r='A1'><f>1+1</f><v>2</v></c><c r='B1'><f>A1*2</f></c>"
                           "<c r='C1'><f t='shared' ref='C1:C2' si='0'>[1]Sheet1!A1</f><v>9</v></c></row>"
                           "<row r='2'><c r='C2'><f t='shared' si='0'/><v>8</v></c></row>")
    assert cells[(1, 1)].text == "2" and cells[(1, 1)].formula
    assert cells[(1, 2)].uncached and cells[(1, 2)].text is None and cells[(1, 2)].cls == "F"    # criterion 8


def test_shared_formula_children_inherit_external_flags(tmp_path):
    wb = read_xlsx(package(tmp_path / "t.xlsx", "<row r='1'><c r='C1'><f t='shared' ref='C1:C2' si='0'>[1]S!A1</f><v>9</v></c></row>"
                                                "<row r='2'><c r='C2'><f t='shared' si='0'/><v>8</v></c></row>"))
    assert wb.sheets[0].formula_flags == {(1, 3): {"external"}, (2, 3): {"external"}}


def test_hidden_by_style_is_detected(tmp_path):
    wb = read_xlsx(package(tmp_path / "t.xlsx", "<row r='1'><c r='A1' s='5'><v>1</v></c><c r='B1'><v>2</v></c></row>"))
    assert wb.sheets[0].hidden_style_cells == {(1, 1)}


def test_corrupt_and_hostile_packages(tmp_path):
    bad = tmp_path / "bad.xlsx"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("xl/workbook.xml", "<!DOCTYPE x [<!ENTITY a 'b'>]><workbook/>")
    with pytest.raises(FileUnreadable):
        read_xlsx(str(bad))
    broken = tmp_path / "broken.xlsx"
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook")
    with pytest.raises(FileUnreadable):
        read_xlsx(str(broken))


def test_encrypted_and_legacy_content_is_sniffed_from_bytes(tmp_path):
    enc = tmp_path / "enc.xlsx"
    enc.write_bytes(OLE2_MAGIC + "EncryptionInfo".encode("utf-16-le") + "EncryptedPackage".encode("utf-16-le"))
    assert sniff(str(enc)).format == "password"
    xls = tmp_path / "old.xlsx"
    xls.write_bytes(OLE2_MAGIC + "Workbook".encode("utf-16-le"))
    assert sniff(str(xls)).format == "xls"
