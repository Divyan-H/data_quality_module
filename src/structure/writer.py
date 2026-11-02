"""Output writers (spec 6): Parquet tables, columns/samples JSON, cells and audit Parquet."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .extract import Table
from .model import Sheet, a1

_CELL_TYPE = {"s": "s", "n": "n", "date": "d", "datetime": "d", "time": "d", "b": "b", "e": "e",
              "formula_uncached": "f", "text": "text"}


def dump_json(path: Path, obj) -> None:
    """Deterministic JSON (sorted keys, no timestamps)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf8")


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def write_table(tdir: Path, t: Table, sheet: Sheet, file_key: str, config_version, rule_pack: str) -> dict:
    tdir.mkdir(parents=True, exist_ok=True)
    row_ids = [f"{file_key}:{sheet.index}:{t.key}:{r}" for r in t.data_rows]
    filtered = set(t.info["filtered_rows"])
    arrays = {
        "_row_id": pa.array(row_ids, pa.string()),
        "_source_row": pa.array(t.data_rows, pa.int64()),
        "_row_hidden": pa.array([r in sheet.hidden_rows for r in t.data_rows], pa.bool_()),
        "_row_filtered": pa.array([r in filtered for r in t.data_rows], pa.bool_()),
        "_outline_level": pa.array([sheet.row_outline.get(r, 0) for r in t.data_rows], pa.int8()),
    }
    for col in t.columns:
        vals = []
        for r in t.data_rows:
            cell = t.cells.get((r, col["col"]))
            vals.append(cell.text if cell is not None else None)
        arrays[col["name"]] = pa.array(vals, pa.large_string())
    meta = {"table_ref": t.table_ref, "schema": "dp.structure.table/1",
            "config_version": config_version, "rule_pack": rule_pack}
    table = pa.table(arrays).replace_schema_metadata(
        {b"dp.structure.table": json.dumps(meta, sort_keys=True).encode()})
    pq.write_table(table, tdir / "data.parquet", compression="zstd", row_group_size=100_000,
                   write_statistics=True)

    cols_json = {
        "schema": "dp.structure.columns/1", "table_ref": t.table_ref,
        "columns": [{k: v for k, v in c.items() if k not in ("col", "uncached")} for c in t.columns],
        "dropped": t.dropped,
    }
    dump_json(tdir / "columns.json", cols_json)
    samples = {}
    for col in t.columns:
        seen: list = []
        for r in t.data_rows:
            cell = t.cells.get((r, col["col"]))
            if cell is not None and cell.text is not None and cell.text not in seen:
                seen.append(cell.text)
                if len(seen) == 5:
                    break
        samples[col["name"]] = seen
    dump_json(tdir / "samples.json", {"schema": "dp.structure.samples/1", "table_ref": t.table_ref,
                                      "samples": samples})
    return {"data": f"tables/{tdir.name}/data.parquet", "columns": f"tables/{tdir.name}/columns.json"}


_CELLS_SCHEMA = pa.schema([
    ("sheet_index", pa.int16()), ("ref", pa.string()), ("row", pa.int32()), ("col", pa.int16()),
    ("category", pa.string()), ("kind", pa.string()), ("table_key", pa.string()),
    ("region", pa.string()), ("value", pa.string()), ("cell_type", pa.string()),
    ("number_format", pa.string()), ("detail", pa.string()),
])
_AUDIT_SCHEMA = pa.schema([
    ("audit_id", pa.string()), ("seq", pa.int32()), ("sheet_index", pa.int16()),
    ("table_key", pa.string()), ("step", pa.string()), ("code", pa.string()),
    ("action", pa.string()), ("ref", pa.string()), ("count", pa.int32()), ("before", pa.string()),
    ("after", pa.string()), ("decided_by", pa.string()), ("note", pa.string()),
])


def write_cells(path: Path, results) -> int:
    """Placements and annotations for every sheet of a file, sorted by (sheet, row, col, kind)."""
    rows = []
    for res in results:
        sh = res.sheet
        foot_key = {}
        for t in res.tables:
            for coord, (kind, _) in t.placements.items():
                if kind in ("footer", "totals_row"):
                    foot_key[coord] = t.key
        for (r, c), (kind, region) in res.placements.items():
            cell = sh.cells[(r, c)]
            rows.append((sh.index, a1(r, c), r, c, "placement", kind, foot_key.get((r, c)), region,
                         cell.text, _CELL_TYPE.get(cell.ctype, "s"), cell.fmt, None))
        for t in res.tables:
            for an in t.annotations:
                cell = t.cells.get((an["row"], an["col"])) or sh.cells.get((an["row"], an["col"]))
                rows.append((sh.index, a1(an["row"], an["col"]), an["row"], an["col"], "annotation",
                             an["kind"], t.key, None, cell.text if cell else None,
                             _CELL_TYPE.get(cell.ctype, "s") if cell else "s", cell.fmt if cell else None,
                             json.dumps(an["detail"], sort_keys=True)))
    rows.sort(key=lambda x: (x[0], x[2], x[3], x[5]))
    cols = list(zip(*rows)) if rows else [[] for _ in _CELLS_SCHEMA]
    table = pa.table({f.name: pa.array(list(cols[i]), f.type) for i, f in enumerate(_CELLS_SCHEMA)})
    pq.write_table(table, path, compression="zstd")
    return len(rows)


def write_audit(path: Path, results, file_key: str) -> int:
    recs = []
    for res in results:
        for t in res.tables:
            for e in t.audit:
                aid = hashlib.sha256(json.dumps(
                    [file_key, res.sheet.index, t.key, e["step"], e["code"], e["action"], e["ref"]]
                ).encode()).hexdigest()[:16]
                recs.append((aid, res.sheet.index, t.key, e))
    recs.sort(key=lambda x: (x[1], x[2], x[0]))
    cols = [[] for _ in _AUDIT_SCHEMA]
    for seq, (aid, si, tk, e) in enumerate(recs, start=1):
        vals = (aid, seq, si, tk, e["step"], e["code"], e["action"], e["ref"], e["count"],
                json.dumps(e["before"], sort_keys=True), json.dumps(e["after"], sort_keys=True),
                e["decided_by"], e["note"])
        for i, v in enumerate(vals):
            cols[i].append(v)
    table = pa.table({f.name: pa.array(cols[i], f.type) for i, f in enumerate(_AUDIT_SCHEMA)})
    pq.write_table(table, path, compression="zstd")
    return len(recs)


def hash_tree(root: Path) -> dict:
    return {str(p.relative_to(root)).replace(os.sep, "/"): file_hash(p)
            for p in sorted(root.rglob("*")) if p.is_file()}
