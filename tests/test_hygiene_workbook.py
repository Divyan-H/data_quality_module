"""Phase A -> Phase B on the Phase B issue workbook: every planted issue must be handled as the catalogue says.
The workbook and the expectations live in tests/fixtures/build_phase_b_workbook.py (PLANTS)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

import build_phase_b_workbook as wbk  # noqa: E402


def test_every_planted_issue_is_handled(tmp_path):
    xlsx = tmp_path / "phase_b_issues.xlsx"
    wbk.build_workbook(xlsx)
    wbk.run_pipeline(xlsx, tmp_path / "a", tmp_path / "b")
    failed = [f"{r['ID']} {r['Column']} row {r['Excel row']}: expected {r['Expected']}, got {r['Actual']}"
              for r in wbk.compare(tmp_path / "b") if r["Status"] == "FAIL"]
    assert not failed, "\n".join(failed)


def test_phase_b_alone_on_the_raw_workbook(tmp_path):
    """Phase B given the .xlsx directly (no Phase A): its own reader must find the same 50 issues."""
    from src.hygiene.pipeline import run_hygiene
    xlsx = tmp_path / "phase_b_issues.xlsx"
    wbk.build_workbook(xlsx)
    run_hygiene(str(xlsx), str(tmp_path / "b"), str(wbk.ROOT / "config" / "settings.yaml"))
    failed = [f"{r['ID']} {r['Column']} row {r['Excel row']}: expected {r['Expected']}, got {r['Actual']}"
              for r in wbk.compare(tmp_path / "b") if r["Status"] == "FAIL"]
    assert not failed, "\n".join(failed)


def test_row_numbers_are_excel_rows(tmp_path):
    """Every Needs_Review / Auto_Fix_Log row names the input's real Excel row, in Excel_Row and at the end of Row_ID,
    and the D-05 row points at the formula cell itself."""
    import openpyxl
    from src.hygiene.pipeline import run_hygiene
    xlsx = tmp_path / "phase_b_issues.xlsx"
    wbk.build_workbook(xlsx)
    run_hygiene(str(xlsx), str(tmp_path / "b"), None)
    wb = openpyxl.load_workbook(tmp_path / "b" / "phase_b_hygiene" / "Hygiene_Review.xlsx")
    for name in ("Needs_Review", "Auto_Fix_Log"):
        rows = list(wb[name].iter_rows(values_only=True))
        assert rows[0][:2] == ("Row_ID", "Excel_Row")
        for row_id, excel_row, *_ in rows[1:]:
            if row_id is not None:
                assert int(row_id.rsplit(":", 1)[1]) == excel_row
    src = openpyxl.load_workbook(xlsx).active
    total = [c.value for c in src[1]].index("Total") + 1
    d05 = [r for r in wb["Needs_Review"].iter_rows(values_only=True) if r[4] == "D-05"]
    assert d05 and all(str(src.cell(r[1], total).value).startswith("=") for r in d05)
    assert "=H32*2" in d05[0][6]
    d06 = [r for r in wb["Needs_Review"].iter_rows(values_only=True) if r[4] == "D-06"]
    assert d06 and all("[budget.xlsx]" in r[6] and "depends on a file the pipeline does not have" in r[6] for r in d06)
