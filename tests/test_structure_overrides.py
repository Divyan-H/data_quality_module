"""Overrides and the job/service contract (spec 8.4, 11; acceptance criterion 37)."""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from conftest import cells, read_json, rows, run, simple_sheet, tables, types
from src.api.api_server import create_app
from src.structure.job import run_job


@pytest.fixture
def two_tables(make_xlsx):
    def build(wb):
        ws = wb.active
        ws.title = "Data"
        simple_sheet(ws, n=6, row0=1)
        for i, (x, y) in enumerate([("Region", "Sales"), ("N", 1), ("S", 2), ("E", 3)], start=12):
            ws.cell(i, 1, x)
            ws.cell(i, 2, y)
    return make_xlsx(builder=build)


def layout(out, key):
    facts = json.loads(next(out.glob("files/*/file.json")).read_text())
    return next(t for t in facts["sheets"][0]["tables"] if t["key"] == key), facts


def override_cfg(*overrides):
    return {"structure": {"selection": {"s-1": [{"table": "t1"}]}, "overrides": list(overrides)}}


def test_not_table_turns_cells_into_placements(two_tables, tmp_path):
    _, base = run(tmp_path, [two_tables], out_name="o1", config={"structure": {"selection": {"s-1": [{"table": "t1"}]}}})
    t2, _ = layout(base, "t2")
    res, out = run(tmp_path, [two_tables], override_cfg({"table": t2["table_ref"], "base": t2["layout_hash"],
                                                          "status": "not_table"}))
    assert len(tables(out)) == 1
    assert {c["kind"] for c in cells(out) if c["category"] == "placement"} == {"excluded_table"}


def test_stale_base_is_orphaned_not_applied(two_tables, tmp_path):
    _, base = run(tmp_path, [two_tables], out_name="o1", config={"structure": {"selection": {"s-1": [{"table": "t1"}]}}})
    t2, _ = layout(base, "t2")
    res, out = run(tmp_path, [two_tables], override_cfg({"table": t2["table_ref"], "base": "sha256:stale",
                                                          "status": "not_table"}))
    assert len(tables(out)) == 2 and types(res)["structure.override_orphaned"] == "warn"


def test_range_header_and_exclusions(two_tables, tmp_path):
    _, base = run(tmp_path, [two_tables], out_name="o1", config={"structure": {"selection": {"s-1": [{"table": "t1"}]}}})
    t1, _ = layout(base, "t1")
    res, out = run(tmp_path, [two_tables], override_cfg({
        "table": t1["table_ref"], "base": t1["layout_hash"], "exclude_rows": ["3-4"], "exclude_columns": ["B"]}))
    data = rows(next(p for p in tables(out) if "_t1" in str(p)))
    assert [r["_source_row"] for r in data] == [2, 5, 6, 7] and "Channel" not in data[0]
    kinds = {c["kind"] for c in cells(out) if c["category"] == "placement"}
    assert {"excluded_row", "excluded_column"} <= kinds


def test_new_o_table_takes_cells_from_other_regions(two_tables, tmp_path):
    _, base = run(tmp_path, [two_tables], out_name="o1", config={"structure": {"selection": {"s-1": [{"table": "t1"}]}}})
    file_key = json.loads(next(base.glob("files/*/file.json")).read_text())["file_key"]
    ov = {"table": f"{file_key}:0:o1", "base": None, "status": "table", "range": "A12:B15", "header_rows": [12]}
    res, out = run(tmp_path, [two_tables], override_cfg(ov))
    sheet = read_json(out, next(out.glob("files/*/sheets/00.json")).relative_to(out).as_posix())
    assert {t["table_key"] for t in sheet["tables"]} == {"t1", "o1"}          # t2 gave up its cells; its key is not reused


def test_job_failures_are_events_not_exceptions(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    body = {"job_id": "j", "job_type": "data.structure", "inputs": [{"ref": "uploads/missing.csv", "kind": "raw_file",
            "content_hash": "sha256:0", "source_id": "s-1", "file_id": "f"}], "output_prefix": "out/", "params": {}}
    ev = run_job(body, root)
    assert ev["type"] == "failed" and ev["error"]["code"] == "input_not_found" and ev["error"]["retryable"]
    (root / "uploads").mkdir()
    csv = root / "uploads" / "a.csv"
    csv.write_text("A,B\n1,2\n", encoding="utf8")
    good_hash = "sha256:" + hashlib.sha256(csv.read_bytes()).hexdigest()
    (root / "bad.json").write_text(json.dumps({"structure": {"params": {"row_gap": 99}}}))
    (root / "ok.json").write_text("{}")

    def with_inputs(config_ref, content_hash):
        return {**body, "inputs": [{"ref": "uploads/a.csv", "kind": "raw_file", "content_hash": content_hash,
                                    "source_id": "s-1", "file_id": "f"}, {"ref": config_ref, "kind": "config"}]}
    assert run_job(with_inputs("bad.json", good_hash), root)["error"]["code"] == "config_invalid"
    mismatch = run_job(with_inputs("ok.json", "sha256:0"), root)
    assert mismatch["error"]["code"] == "input_hash_mismatch" and not mismatch["error"]["retryable"]
    assert run_job(with_inputs("ok.json", good_hash), root)["type"] == "succeeded"
    assert run_job(with_inputs("ok.json", good_hash), root, is_cancelled=lambda: True)["error"]["code"] == "cancelled"


def test_service_accepts_jobs_idempotently_and_prechecks_uploads(tmp_path, two_tables):
    root = tmp_path / "store"
    (root / "uploads").mkdir(parents=True)
    (root / "uploads" / "a.csv").write_text("A,B\n1,2\n", encoding="utf8")
    client = TestClient(create_app(str(root)))
    assert client.get("/health").json() == {"status": "ok"}
    body = {"job_id": "j-9", "attempt": 1, "job_type": "data.structure", "output_prefix": "out/j-9/",
            "inputs": [{"ref": "uploads/a.csv", "kind": "raw_file", "content_hash": "sha256:bad", "source_id": "s-1",
                        "file_id": "f"}], "params": {}}
    first = client.post("/jobs", json=body)
    assert first.status_code == 202
    again = client.post("/jobs", json=body).json()
    assert again["job_id"] == "j-9"                                                # (job_id, attempt) is idempotent
    assert client.post("/jobs/unknown/cancel").status_code == 404
    bad = client.post("/review/precheck", content=two_tables.read_bytes())
    assert bad.json()["valid"] is False and bad.json()["violations"][0]["code"] == "review_base_mismatch"
