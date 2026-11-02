"""Performance targets of spec 11.8 (run with `pytest -m perf`)."""
import time

import openpyxl
import pytest

from conftest import run


@pytest.mark.perf
def test_golden_sized_workbook_runs_within_15_seconds(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["B1"] = "Title"
    for j in range(30):
        ws.cell(3, 2 + j, f"col{j}")
    for r in range(4, 5004):
        for j in range(30):
            ws.cell(r, 2 + j, (r * j) % 97 if j % 3 else f"txt{r % 50}")
    path = tmp_path / "big.xlsx"
    wb.save(path)                                          # about 150,000 populated cells
    start = time.time()
    res, _ = run(tmp_path, [path])
    assert res["summary"]["rows_out"] == 5000 and time.time() - start <= 15
