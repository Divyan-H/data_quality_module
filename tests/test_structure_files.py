"""Formats, folders, selection, reuse and the job-level rules (spec 3, 8.3, 11; criteria 25, 35-41, 51)."""
import json
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from conftest import cells, read_json, rows, run, simple_sheet, tables, types
from src.structure.config import ConfigInvalid, resolve_settings
from src.structure.errors import JobFailed
from src.structure.pipeline import run_structure


def test_csv_with_preamble_semicolons_cp1252_and_ragged_records(tmp_path):
    f = tmp_path / "a.csv"
    f.write_bytes("Sales report\nGenerated today\nChannel;Spend;Note\nTV;10;ä\nRadio;20\nWeb;30;x;extra\nPrint;40;y\n"
                  .encode("cp1252"))
    res, out = run(tmp_path, [f])
    sheet = read_json(out, next(out.glob("files/*/sheets/00.json")).relative_to(out).as_posix())
    assert sheet["tables"][0]["header"]["rows"] if "header" in sheet["tables"][0] else sheet["tables"][0]["header_rows"] == [3]
    (t,) = tables(out)
    assert len(rows(t)) == 4                                                    # no record is skipped (K-20)
    kinds = {c["kind"] for c in cells(out) if c["category"] == "annotation"}
    assert {"ragged_short", "ragged_long"} <= kinds
    assert "structure.csv_ragged" in types(res) and "structure.csv_encoding_guessed" in types(res)


def test_utf16_tsv_with_bom(tmp_path):
    f = tmp_path / "u.tsv"
    f.write_bytes("Id\tName\n1\tAnna\n2\tBo\n3\tCy\n".encode("utf-16"))
    res, out = run(tmp_path, [f])
    (t,) = tables(out)
    assert [r["Name"] for r in rows(t)] == ["Anna", "Bo", "Cy"]


def test_xlsx_named_csv_is_read_by_content(make_xlsx, tmp_path):
    src = make_xlsx("real.xlsx", lambda wb: simple_sheet(wb.active))
    disguised = tmp_path / "x.csv"
    disguised.write_bytes(src.read_bytes())
    res, out = run(tmp_path, [disguised])
    assert types(res)["structure.file_format_mismatch"] == "info" and len(tables(out)) == 1


def test_bad_files_block_only_themselves(tmp_path):
    good = tmp_path / "ok.csv"
    good.write_text("A,B\n1,2\n3,4\n", encoding="utf8")
    empty, arc = tmp_path / "empty.csv", tmp_path / "x.zip"
    empty.write_bytes(b"")
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("a.txt", "hi")
    corrupt = tmp_path / "bad.xlsx"
    corrupt.write_bytes(b"PK\x03\x04garbage")
    res, out = run(tmp_path, [good, empty, arc, corrupt])
    blocks = {k for k, v in types(res).items() if v == "block"}
    assert {"structure.file_empty", "structure.unsupported_format", "structure.file_unreadable"} <= blocks
    assert len(tables(out)) == 1
    assert any("upload the folder" in a["message"].lower() for a in res["action_items"])


def test_folder_paths_and_hidden_sheets_are_inventoried(make_xlsx, tmp_path):
    def build(wb):
        simple_sheet(wb.active)
        hid = wb.create_sheet("H")
        hid.sheet_state = "hidden"
        simple_sheet(hid)
    book = make_xlsx("b.xlsx", build)
    entries = [{"path": str(book), "source_id": "s-1", "file_id": "f-1", "relative_path": "media/2026/Q1/b.xlsx"}]
    run_structure({"files": entries, "config": {}, "output_dir": str(tmp_path / "o")})
    inv = read_json(tmp_path / "o", "inventory.json")
    assert inv["files"][0]["relative_path"] == "media/2026/Q1/b.xlsx"
    assert [s["state"] for s in inv["files"][0]["sheets"]] == ["visible", "hidden"]     # criterion 41


def test_every_visible_table_is_selected_and_gets_its_own_review_sheet(make_xlsx, tmp_path):
    import openpyxl

    def build(wb):
        ws = wb.active
        simple_sheet(ws, row0=1, col0=1)                         # two tables side by side on one sheet ...
        simple_sheet(ws, ("Brand", "Cost", "Units"), row0=1, col0=8)
        simple_sheet(wb.create_sheet("Second"))                  # ... one on another sheet ...
        hidden = wb.create_sheet("Hidden")                       # ... and one on a hidden sheet
        simple_sheet(hidden)
        hidden.sheet_state = "hidden"
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    sel = read_json(out, "inventory.json")["selection"][0]
    assert sel["status"] == "resolved" and sel["by"] == "auto" and not res["blocking"]
    assert sorted(r.split(":", 1)[1] for r in sel["tables"]) == ["0:t1", "0:t2", "1:t1"]   # hidden sheet left out
    wb = openpyxl.load_workbook(out / "review" / "Structural_Review.xlsx")
    assert [n for n in wb.sheetnames if n[0] == "T" and n[1].isdigit()] == ["T1 sheet", "T2 sheet", "T3 second"]


def test_selectors_match_by_pattern_and_report_ambiguity(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        simple_sheet(ws, row0=1)
        simple_sheet(ws, row0=12)
    book = make_xlsx(builder=build)
    ok, _ = run(tmp_path, [book], {"structure": {"selection": {"s-1": [{"table": "t2"}]}}}, "o1")
    assert not ok["blocking"]
    amb, _ = run(tmp_path, [book], {"structure": {"selection": {"s-1": [{"table": "only"}]}}}, "o2")
    assert types(amb)["structure.selection_not_found"] == "block"
    two, _ = run(tmp_path, [book], {"structure": {"selection": {"s-1": [{"sheet": {"name": "Sheet", "match": "exact"},
                                                                      "table": "t1"}, {"table": "t9"}]}}}, "o3")
    assert types(two)["structure.selection_not_found"] == "block"


def test_findings_on_unselected_tables_are_info_and_no_action_items(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        simple_sheet(ws, n=8)
        ws.row_dimensions[3].hidden = True
        other = wb.create_sheet("Other")
        simple_sheet(other)
    res, out = run(tmp_path, [make_xlsx(builder=build)], {"structure": {"selection": {"s-1": [{"sheet": "Other", "table": "only"}]}}})
    assert "structure.S-11.hidden_rows" not in types(res)                        # criterion 24
    detail = read_json(out, "report.json")["extras"]["structure"]["findings_detail"]
    hidden = next(f for f in detail if f["id"].startswith("S-11.hidden_rows"))
    assert hidden["severity"] == "info" and hidden["selected"] is False


def test_same_content_in_two_sources_is_processed_once(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("K,V\nx,1\ny,2\n", encoding="utf8")
    b.write_bytes(a.read_bytes())
    entries = [{"path": str(a), "source_id": "s-1", "file_id": "f-1", "relative_path": "a.csv"},
               {"path": str(b), "source_id": "s-2", "file_id": "f-2", "relative_path": "b.csv"}]
    run_structure({"files": entries, "output_dir": str(tmp_path / "o")})
    assert len(list((tmp_path / "o").glob("files/*/tables/*/data.parquet"))) == 1
    assert len(read_json(tmp_path / "o", "inventory.json")["selection"]) == 2


def test_parquet_is_listed_not_rewritten(tmp_path):
    f = tmp_path / "p.parquet"
    pq.write_table(pa.table({"_id": [1, 2], "Name": ["a", "b"], "name": ["c", "d"]}), f)
    res, out = run(tmp_path, [f])
    cols = json.loads(next(out.glob("files/*/tables/*/columns.json")).read_text())
    assert [c["name"] for c in cols["columns"]] == ["src_id", "Name", "name__2"] and cols["data_ref"] == str(f)
    assert not tables(out) and not res["blocking"]                              # criterion 51


def test_reuse_only_reprocesses_changed_files(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("K,V\nx,1\ny,2\n", encoding="utf8")
    b.write_text("K,V\nz,3\nw,4\n", encoding="utf8")
    entries = lambda: [{"path": str(a), "source_id": "s-1", "file_id": "f-1", "relative_path": "a.csv"},  # noqa: E731
                       {"path": str(b), "source_id": "s-1", "file_id": "f-2", "relative_path": "b.csv"}]
    cfg = {"structure": {"selection": {"s-1": [{"file": "*", "table": "only"}]}}}
    run_structure({"files": entries(), "config": cfg, "output_dir": str(tmp_path / "o1"), "job_id": "j1"})
    b.write_text("K,V\nz,3\nw,5\n", encoding="utf8")
    run_structure({"files": entries(), "config": cfg, "output_dir": str(tmp_path / "o2"), "previous_dir": str(tmp_path / "o1"), "job_id": "j2"})
    inv = read_json(tmp_path / "o2", "inventory.json")
    assert [f["reused_from"] for f in inv["files"]] == ["j1", None]             # criterion 36
    cfg2 = {"structure": {"selection": {"s-1": [{"file": "a.csv", "table": "only"}, {"file": "b.csv", "table": "only"}]}}}
    run_structure({"files": entries(), "config": cfg2, "output_dir": str(tmp_path / "o3"), "previous_dir": str(tmp_path / "o2"), "job_id": "j3"})
    assert [f["reused_from"] for f in read_json(tmp_path / "o3", "inventory.json")["files"]] == ["j2", "j2"]   # selection is not file config
    run_structure({"files": entries(), "config": cfg, "output_dir": str(tmp_path / "o4"), "previous_dir": str(tmp_path / "o3"),
                   "job_id": "j4", "full": True})
    assert all(f["reused_from"] is None for f in read_json(tmp_path / "o4", "inventory.json")["files"])


def test_outputs_are_byte_identical_across_runs(make_xlsx, tmp_path):
    book = make_xlsx(builder=lambda wb: simple_sheet(wb.active))
    r1, o1 = run(tmp_path, [book], out_name="o1")
    r2, o2 = run(tmp_path, [book], out_name="o2")
    data = {k: v for k, v in r1["artifacts"].items() if k.endswith(("data.parquet", "columns.json", "cells.parquet", "audit.parquet"))}
    assert data and data == {k: r2["artifacts"][k] for k in data}                # criterion 35
    assert [a["id"] for a in r1["action_items"]] == [a["id"] for a in r2["action_items"]]


def test_config_bounds_and_hash_mismatch(tmp_path):
    for bad in ({"params": {"row_gap": 1}}, {"params": {"header_min_score": 9}}, {"llm": {"votes": 2}},
                {"params": {"composite_separator": "%"}}, {"params": {"llm_override_margin": 0.1}}):
        with pytest.raises(ConfigInvalid):
            resolve_settings({"structure": bad})                                  # criterion 45
    f = tmp_path / "m.csv"
    f.write_text("A,B\n1,2\n", encoding="utf8")
    with pytest.raises(JobFailed) as e:
        run_structure({"files": [{"path": str(f), "source_id": "s-1", "content_hash": "sha256:00"}],
                       "output_dir": str(tmp_path / "o")})
    assert e.value.code == "input_hash_mismatch" and not e.value.retryable
    with pytest.raises(JobFailed) as e:
        run_structure({"files": [{"path": str(f), "source_id": "s-1"}], "config": {"structure": {"params": {"row_gap": 0}}},
                       "output_dir": str(tmp_path / "o")})
    assert e.value.code == "config_invalid"


def test_sheet_keys_never_collide(make_xlsx, tmp_path):
    def build(wb):
        simple_sheet(wb.active)
        wb.active.title = "Sales@Data"
        simple_sheet(wb.create_sheet("Sales Data"))
    res, out = run(tmp_path, [make_xlsx(builder=build)])
    inv = read_json(out, "inventory.json")
    keys = [s["key"] for s in inv["files"][0]["sheets"]]
    assert len(set(keys)) == 2                                                    # criterion 46
    assert len(list(out.glob("files/*/tables/*"))) == 2
