"""Review workbook loop and the Job Contract (spec 9, 11; acceptance criteria 27-30, 37)."""
import hashlib
import json
import shutil
from pathlib import Path

import openpyxl
import pyarrow.parquet as pq
import pytest

from conftest import simple_sheet
from src.structure.job import run_job
from src.structure.review_import import precheck

CFG = {"version": 1}


class Store:
    """A tiny local object store laid out like the platform's prefixes."""

    def __init__(self, root: Path):
        self.root, self.n = root, 0

    def put(self, ref: str, src: Path | None = None, content: bytes | None = None) -> str:
        dest = self.root / ref
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest) if src else dest.write_bytes(content)
        return ref

    def body(self, files: dict, cfg: dict, prev: str | None = None, upload: str | None = None, mode="run", **params):
        self.n += 1
        job = f"j-{self.n}"
        inputs = [{"ref": self.put(f"uploads/{name}", src), "content_hash": "sha256:" + hashlib.sha256(src.read_bytes()).hexdigest(),
                   "kind": "raw_file", "source_id": "s-1", "file_id": f"f-{i}", "relative_path": name}
                  for i, (name, src) in enumerate(files.items())]
        inputs.append({"ref": self.put(f"config/v{self.n}.json", content=json.dumps(cfg).encode()), "kind": "config"})
        if prev:
            inputs.append({"ref": prev, "kind": "previous_structure"})
        if upload:
            inputs.append({"ref": upload, "kind": "review_upload"})
        return {"job_id": job, "job_type": "data.structure", "attempt": 1, "context": {"project_id": "p"},
                "inputs": inputs, "output_prefix": f"staging/structure/{job}/",
                "params": {"mode": mode, "config_ref": "x", **params}}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "store")


@pytest.fixture
def book(tmp_path):
    def build(rows=8, name="data.xlsx"):
        path = tmp_path / name
        if path.exists():                    # one file per name: a rebuilt workbook would have another content hash
            return path
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Data"
        ws["A1"] = "Media plan"
        simple_sheet(ws, n=rows, row0=3)
        wb.save(path)
        return path
    return build


def first_run(store, book, cfg=None):
    events = []
    ev = run_job(store.body({"data.xlsx": book()}, cfg or CFG), store.root, events.append)
    assert ev["type"] == "succeeded", ev
    return "staging/structure/j-1/", events


def resave(src: Path, dest: Path, edit=None):
    """What Excel does when the analyst opens, edits and saves the workbook."""
    wb = openpyxl.load_workbook(src)
    if edit:
        edit(wb)
    wb.save(dest)
    return dest


def edit_flow(store, prev, upload_path, files, cfg=None, **extra):
    ref = store.put("uploads/review.xlsx", upload_path)
    return run_job(store.body(files, cfg or CFG, prev=prev, upload=ref, mode="review_import", **extra), store.root)


def test_export_layout_and_events(store, book):
    prev, events = first_run(store, book)
    wb = openpyxl.load_workbook(store.root / prev / "review" / "Structural_Review.xlsx")
    assert wb.sheetnames[:2] == ["README", "Structural_Findings"] and wb["_meta"].sheet_state == "hidden"
    assert "Excluded_Cells" in wb.sheetnames and any(n.startswith("T1 ") for n in wb.sheetnames)
    ws = wb[next(n for n in wb.sheetnames if n.startswith("T1"))]
    assert ws["A1"].value == "Row_ID" and ws["A2"].value.endswith(":0:t1:4") and ws["B1"].value == "Date"
    types_ = [e["type"] for e in events]
    assert types_[0] == "accepted" and types_[-1] == "succeeded" and "progress" in types_
    out = events[-1]["outputs"]
    assert {o["kind"] for o in out} >= {"report", "dataset", "review", "log"}
    assert all("Radio" not in json.dumps(e) for e in events)                       # value-free events (P7)
    assert events[-1]["summary"]["tables"] == 1


def test_unchanged_upload_even_after_resave_is_a_noop(store, book, tmp_path):
    prev, _ = first_run(store, book)
    original = store.root / prev / "review" / "Structural_Review.xlsx"
    assert not precheck(str(original)).violations
    saved = resave(original, tmp_path / "saved.xlsx")
    ev = edit_flow(store, prev, saved, {"data.xlsx": book()})
    assert ev["type"] == "succeeded", ev
    patch = json.loads((store.root / "staging/structure/j-2/review_import/patch.json").read_text())
    assert patch["overrides"] == [] and patch["cell_edits"]["count"] == 0          # criterion 27
    a = next((store.root / prev).glob("files/*/tables/*/data.parquet"))
    b = next((store.root / "staging/structure/j-2").glob("files/*/tables/*/data.parquet"))
    assert pq.read_table(a).to_pylist() == pq.read_table(b).to_pylist()


def test_broken_workbooks_are_rejected_by_the_precheck(store, book, tmp_path):
    prev, _ = first_run(store, book)
    original = store.root / prev / "review" / "Structural_Review.xlsx"

    def rename(wb):
        ws = wb[next(n for n in wb.sheetnames if n.startswith("T1"))]
        ws["C1"] = "Renamed"

    def delete_sheet(wb):
        del wb[next(n for n in wb.sheetnames if n.startswith("T1"))]

    def add_sheet(wb):
        wb.create_sheet("Extra")

    def break_meta(wb):
        wb["_meta"]["A1"] = "tampered"

    def lock_break(wb):
        wb["Structural_Findings"]["B2"] = "other.xlsx"

    def bad_value(wb):
        wb["Structural_Findings"]["N2"] = "not a range"
    expect = {rename: "review_columns_changed", delete_sheet: "review_sheet_missing", add_sheet: "review_sheet_unexpected",
              break_meta: "review_base_mismatch", lock_break: "review_locked_edited", bad_value: "review_value_invalid"}
    for fn, code in expect.items():
        review = precheck(str(resave(original, tmp_path / f"{fn.__name__}.xlsx", fn)))
        assert code in {v.code for v in review.violations}, fn.__name__              # criterion 28
    ev = edit_flow(store, prev, resave(original, tmp_path / "r.xlsx", rename), {"data.xlsx": book()})
    assert ev["type"] == "failed" and ev["error"]["code"] == "review_import_invalid" and not ev["error"]["retryable"]
    assert ev["error"]["details"]["violations"][0]["code"] == "review_columns_changed"


def test_row_id_changes_pass_the_precheck_but_fail_the_job(store, book, tmp_path):
    prev, _ = first_run(store, book)
    original = store.root / prev / "review" / "Structural_Review.xlsx"

    def edit_id(wb):
        wb[next(n for n in wb.sheetnames if n.startswith("T1"))]["A3"] = "hacked"
    saved = resave(original, tmp_path / "ids.xlsx", edit_id)
    assert not precheck(str(saved)).violations
    ev = edit_flow(store, prev, saved, {"data.xlsx": book()})
    assert ev["error"]["code"] == "review_import_invalid"
    assert {v["code"] for v in ev["error"]["details"]["violations"]} == {"review_row_ids_changed"}


def test_header_edit_becomes_an_override_that_later_runs_keep(store, book, tmp_path):
    prev, _ = first_run(store, book)
    original = store.root / prev / "review" / "Structural_Review.xlsx"

    def edit(wb):
        ws = wb["Structural_Findings"]
        ws["M2"] = "4"                                   # Header rows: move to row 4
        ws["O2"] = "5"                                   # Exclude rows
        ws["Q2"] = "checked by analyst"
    ev = edit_flow(store, prev, resave(original, tmp_path / "e.xlsx", edit), {"data.xlsx": book()})
    assert ev["type"] == "succeeded", ev
    patch = json.loads((store.root / "staging/structure/j-2/review_import/patch.json").read_text())
    (ov,) = patch["overrides"]
    assert ov["header_rows"] == [4] and ov["exclude_rows"] == ["5"] and ov["base"].startswith("sha256:")
    assert patch["notes"] == [{"table": ov["table"], "note": "checked by analyst"}]
    new_cfg = {"version": 2, "structure": {"overrides": patch["overrides"]}}          # what the platform merges (v K+1)
    run_job(store.body({"data.xlsx": book()}, new_cfg), store.root)               # a plain run still applies it
    out = store.root / "staging/structure/j-3"
    data = pq.read_table(next(out.glob("files/*/tables/*/data.parquet"))).to_pylist()
    assert data[0]["_source_row"] == 5 + 1 and 5 not in {r["_source_row"] for r in data}   # criterion 29
    assert any(c["kind"] == "excluded_row" for c in
               pq.read_table(next(out.glob("files/*/cells.parquet"))).to_pylist())


def test_cell_edit_is_kept_and_orphaned_when_the_file_changes(store, book, tmp_path):
    prev, _ = first_run(store, book)
    original = store.root / prev / "review" / "Structural_Review.xlsx"

    def edit(wb):
        ws = wb[next(n for n in wb.sheetnames if n.startswith("T1"))]
        ws["D3"] = 999.5                                  # Spend of the second data row
    ev = edit_flow(store, prev, resave(original, tmp_path / "c.xlsx", edit), {"data.xlsx": book()})
    assert ev["type"] == "succeeded"
    out = store.root / "staging/structure/j-2"
    data = pq.read_table(next(out.glob("files/*/tables/*/data.parquet"))).to_pylist()
    assert data[1]["Spend"] == "999.5"
    ann = pq.read_table(next(out.glob("files/*/cells.parquet"))).to_pylist()
    assert [a["kind"] for a in ann if a["kind"] == "analyst_edit"] == ["analyst_edit"]
    audit = pq.read_table(next(out.glob("files/*/audit.parquet"))).to_pylist()
    edit_row = next(a for a in audit if a["action"] == "apply_cell_edit")
    assert json.loads(edit_row["after"]) == {"value": "999.5"}                        # values only in this entry
    edits_ref = {"path": "staging/structure/j-2/review_import/cell_edits.parquet"}
    cfg = {"version": 2, "structure": {"cell_edits_ref": edits_ref}}
    run_job(store.body({"data.xlsx": book()}, cfg), store.root)
    data3 = pq.read_table(next((store.root / "staging/structure/j-3").glob("files/*/tables/*/data.parquet"))).to_pylist()
    assert data3[1]["Spend"] == "999.5"                                                 # a later run keeps it (criterion 30)
    # the file changes underneath the edit: the edit is not applied and is reported
    changed = tmp_path / "changed.xlsx"
    wb = openpyxl.load_workbook(book())
    wb["Data"]["C5"] = 12345                     # the edited cell now holds another value
    wb.save(changed)
    gone = run_job(store.body({"data.xlsx": changed}, cfg), store.root)               # another content hash: the edit's file is gone
    assert "structure.override_orphaned" in {a["type"] for a in gone["action_items"]}


def test_use_column_and_new_table_rows_produce_selection_and_o_tables(store, tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    simple_sheet(ws, n=8, row0=1)
    simple_sheet(ws, n=5, row0=14, col0=1)
    path = tmp_path / "two.xlsx"
    wb.save(path)
    cfg = {"version": 1, "structure": {"selection": {"s-1": [{"table": "t1"}]}}}
    run_job(store.body({"two.xlsx": path}, cfg), store.root)
    prev = "staging/structure/j-1/"
    original = store.root / prev / "review" / "Structural_Review.xlsx"

    def edit(w):
        f = w["Structural_Findings"]
        header = [c.value for c in f[1]]
        use = header.index("Use") + 1
        f.cell(2, use).value = "N"
        f.cell(3, use).value = "Y"
        new = 4
        f.cell(new, header.index("File") + 1).value = "two.xlsx"
        f.cell(new, header.index("Sheet") + 1).value = "Data"
        f.cell(new, header.index("Data range") + 1).value = "A1:C3"
        f.cell(new, header.index("Header rows") + 1).value = "1"
        f.cell(new, use).value = "Y"
    ev2 = edit_flow(store, prev, resave(original, tmp_path / "u.xlsx", edit), {"two.xlsx": path}, cfg)
    assert ev2["type"] == "succeeded", ev2
    patch = json.loads((store.root / "staging/structure/j-2/review_import/patch.json").read_text())
    assert {s["table"] for s in patch["selection"]["s-1"]} == {"t2", "o1"}
    assert any(o["table"].endswith(":o1") and o["base"] is None for o in patch["overrides"])
