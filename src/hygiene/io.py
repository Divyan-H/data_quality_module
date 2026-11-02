"""Reading Phase A tables (and loose CSV/XLSX/Parquet files) and writing the cleaned tables."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from fnmatch import fnmatch
from datetime import date, datetime, time
from pathlib import Path

import polars as pl
from openpyxl.utils import get_column_letter

from src.common.columns import SYSTEM

from .table import Table
from .values import parse_date, parse_number

ENCODINGS = ["utf-8-sig", "cp1252", "latin-1"]          # D-17: alternative load methods, in order
ADDED = "added_columns.json"                              # in the Phase A folder: columns pasted after Phase A
ANNOTATIONS = {"formula_uncached", "external_link", "broken_reference", "error"}


# ── reading ──────────────────────────────────────────────────────────────────
def _read_csv(path: Path) -> pl.DataFrame:
    for enc in ENCODINGS:
        try:
            return pl.read_csv(path, infer_schema=False, encoding="utf8" if enc == "utf-8-sig" else enc,
                               truncate_ragged_lines=True)
        except (pl.exceptions.ComputeError, UnicodeDecodeError):
            continue
    raise ValueError("cannot decode the file with utf-8, cp1252 or latin-1")


def _cell_text(v) -> str | None:
    """One text rendering per cell, as Phase A does: dates as ISO, whole floats without '.0', TRUE/FALSE, '' as NULL."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == time() else v.isoformat()
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


_EXTERNAL = re.compile(r"\[[^\]]+\]")                    # ='[budget.xlsx]Sheet1'!A1 or [1]Sheet1!A1


def read_workbook(path: Path) -> list[dict]:
    """Every non-empty sheet of an .xlsx as {index, name, header, rows: [(excel_row, [text...])], annotations}.

    Standalone input (no Phase A): the first non-empty row is the header, fully empty rows are skipped, and formula
    cells are annotated the way Phase A does it (no stored result / external link / #REF! in the formula)."""
    import openpyxl
    values = openpyxl.load_workbook(path, read_only=True, data_only=True)      # stored results
    formulas = openpyxl.load_workbook(path, read_only=True, data_only=False)   # formula text
    sheets = []
    for index, (ws, wf) in enumerate(zip(values.worksheets, formulas.worksheets)):
        header, rows, notes = None, [], []
        for r, (vals, fs) in enumerate(zip(ws.iter_rows(values_only=True), wf.iter_rows(values_only=True)), start=1):
            texts = [_cell_text(v) for v in vals]
            if header is None:
                if any(texts):
                    header = _header_names(texts)
                continue
            if not any(texts) and not any(isinstance(f, str) and f.startswith("=") for f in fs):
                continue
            rows.append((r, (texts + [None] * len(header))[:len(header)]))
            for c, f in enumerate(fs[:len(header)]):
                if isinstance(f, str) and f.startswith("="):
                    kinds = (["formula_uncached"] if vals[c] is None else []) + \
                        (["external_link"] if _EXTERNAL.search(f) else []) + (["broken_reference"] if "#REF!" in f else [])
                    notes += [(len(rows) - 1, header[c], k, f if k in ("formula_uncached", "external_link") else texts[c] or f)
                              for k in kinds]            # the formula text is what D-05 / D-06 quote
        if header:
            sheets.append({"index": index, "name": ws.title, "header": header, "rows": rows, "annotations": notes})
    return sheets


def _header_names(texts: list) -> list[str]:
    """Header row -> unique column names; a blank header becomes col_<n>, a repeated one gets _2, _3 ..."""
    names, seen = [], Counter()
    for n, t in enumerate(texts, start=1):
        name = t if t is not None else f"col_{n}"
        seen[name] += 1
        names.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return names


def _loose_tables(p: Path, cfg: dict) -> list[Table]:
    """A file given directly (no Phase A): one table per xlsx sheet, or one table for a csv/parquet file.
    Row_ID is {file_key}:{sheet index}:t1:{Excel row}; _source_row is that Excel row (a csv header counts as row 1)."""
    with p.open("rb") as f:                                  # hashed in chunks, never held whole
        key = hashlib.file_digest(f, "sha256").hexdigest()[:12]
    if p.suffix.lower() == ".xlsx":
        sheets = read_workbook(p)
    else:
        df = read_frame(p)
        sheets = [{"index": 0, "name": None, "header": df.columns, "annotations": [],
                   "rows": [(n + 2, list(r)) for n, r in enumerate(df.iter_rows())]}]
    tables = []
    for s in sheets:
        ref = f"{key}:{s['index']}:t1"
        data = {"_row_id": [f"{ref}:{r}" for r, _ in s["rows"]],          # same form as Phase A: ends in the Excel row
                "_source_row": [r for r, _ in s["rows"]]}
        data |= {c: [vals[j] for _, vals in s["rows"]] for j, c in enumerate(s["header"])}
        t = Table(ref, data, cfg, source_file=p.name, sheet=s["name"])
        t.annotations = s["annotations"]
        tables.append(t)
    return tables


def read_frame(path: Path) -> pl.DataFrame:
    """Parquet, CSV or XLSX as an all-text frame; raises if every load method fails."""
    if path.suffix.lower() == ".xlsx":
        sh = read_workbook(path)[0]
        return pl.DataFrame({c: [v[j] for _, v in sh["rows"]] for j, c in enumerate(sh["header"])},
                            schema={c: pl.String for c in sh["header"]})
    readers = {".parquet": pl.read_parquet, ".csv": _read_csv}
    df = readers[path.suffix.lower()](path)
    return df.cast({c: pl.String for c in df.columns if c not in SYSTEM})


def _annotations(tdir: Path, data: dict, ref: str) -> list:
    """Phase A's per-cell notes for this table (uncached formula, external link, broken reference, error)."""
    cells_path, cols_path = tdir.parents[1] / "cells.parquet", tdir / "columns.json"
    if not (cells_path.exists() and cols_path.exists()):
        return []
    _, sheet, key = ref.split(":")[:3]
    letters = {c["letter"]: c["name"] for c in json.loads(cols_path.read_text("utf8"))["columns"]}
    index = {r: i for i, r in enumerate(data["_source_row"])}
    cells = pl.read_parquet(cells_path).filter((pl.col("category") == "annotation") & (pl.col("table_key") == key)
                                               & (pl.col("sheet_index") == int(sheet)) & pl.col("kind").is_in(ANNOTATIONS))
    return [(index[r["row"]], letters[get_column_letter(r["col"])], r["kind"],
             json.loads(r["detail"] or "{}").get("formula") or r["value"])      # D-05 / D-06 quote the formula
            for r in cells.iter_rows(named=True) if r["row"] in index and get_column_letter(r["col"]) in letters]


def phase_a_index(root: Path) -> tuple[set | None, dict, dict]:
    """From Phase A's inventory.json: the selected table refs (None = no selection recorded), each table's layout_id,
    and (file_key, sheet index) -> (file path, sheet name)."""
    inv_path = root / "inventory.json"
    if not inv_path.exists():
        return None, {}, {}
    inv = json.loads(inv_path.read_text("utf8"))
    sels = inv.get("selection")
    selected = {ref for s in sels for ref in s.get("tables", [])} if sels is not None else None
    layouts = {ref: g["layout_id"] for s in sels or [] for g in s.get("layout_groups", []) for ref in g["tables"]}
    names = {(f["file_key"], sh["index"]): (f.get("relative_path"), sh["name"])
             for f in inv.get("files", []) for sh in f.get("sheets", [])}
    return selected, layouts, names


def load_tables(root: Path, cfg: dict, sheet: str | None = None, tables: list[str] | None = None,
                all_tables: bool = False) -> tuple[list[Table], list[dict], list[str]]:
    """Every table under `root` -> (tables, unreadable, skipped).

    `root` is a Phase A output folder (only the tables Phase A selected are read, unless `all_tables`) or a single
    CSV/XLSX/Parquet file. `sheet` / `tables` narrow the run to one sheet name / table refs (fnmatch patterns).
    A file that cannot be read is listed in `unreadable`, never fatal (D-17)."""
    root = Path(root)
    selected, layouts, names = phase_a_index(root) if root.is_dir() else (None, {}, {})
    added = {}
    if root.is_dir() and (root / ADDED).exists():                 # columns the analyst pasted after Phase A
        added = json.loads((root / ADDED).read_text("utf8"))
    paths = [root] if root.is_file() else (sorted(root.glob("**/tables/*/data.parquet")) or
                                           sorted(p for p in root.rglob("*") if p.suffix.lower() in (".csv", ".xlsx", ".parquet")))
    out, unreadable, skipped = [], [], []
    for p in paths:
        try:
            phase_a = p.name == "data.parquet" and p.parent.parent.name == "tables"
            found = _phase_a_table(p, cfg, layouts, names, added) if phase_a else _loose_tables(p, cfg)
        except Exception as e:                                    # D-17: report what could not be read
            unreadable.append({"file": str(p), "error": f"{type(e).__name__}: {e}"})
            continue
        for t in found:
            if ((selected is not None and not all_tables and t.ref not in selected)
                    or (sheet and t.sheet != sheet) or (tables and not any(fnmatch(t.ref, pat) for pat in tables))):
                skipped.append(t.ref)
            else:
                out.append(t)
    return out, unreadable, skipped


def _phase_a_table(p: Path, cfg: dict, layouts: dict, names: dict, added: dict | None = None) -> list[Table]:
    """One table written by Phase A (data.parquet + columns.json + the file's cells.parquet annotations), plus the columns
    the analyst pasted next to it after Phase A (`added`: table ref -> column -> {Row_ID: text}), joined on Row_ID."""
    df = read_frame(p)
    cols_json = p.parent / "columns.json"
    ref = json.loads(cols_json.read_text("utf8")).get("table_ref", p.parent.name) if cols_json.exists() else p.parent.name
    file_key, sheet_index = ref.split(":")[0], int(ref.split(":")[1]) if ref.count(":") >= 2 else 0
    src, sheet_name = names.get((file_key, sheet_index), (None, None))
    data = {c: df[c].to_list() for c in df.columns}
    pasted = {c: v for c, v in (added or {}).get(ref, {}).items() if c not in data}
    for c, by_row in pasted.items():
        data[c] = [by_row.get(rid) for rid in data["_row_id"]]
    t = Table(ref, data, cfg, layouts.get(ref), src, sheet_name)
    t.annotations = _annotations(p.parent, data, ref)
    for c, by_row in pasted.items():
        t.audit.append(t._entry("analyst", c, None, action="column_added", old_value=None, new_value=f"{len(by_row)} value(s)",
                                note="Column added by the analyst after Phase A and checked with the Phase B rules."))
    return [t]


# ── writing ──────────────────────────────────────────────────────────────────
def typed_frame(t: Table) -> pl.DataFrame:
    """Final value-clean table: numeric columns as numbers, date columns as dates, everything else as text.
    A column stays text if any value would not survive the conversion (nothing is silently dropped)."""
    cols: dict[str, pl.Series] = {}
    for c, vals in t.data.items():
        if c in SYSTEM:                                           # Phase A bookkeeping columns keep their own types
            cols[c] = pl.Series(c, vals)
            continue
        kind = t.types.get(c)
        s = pl.Series(c, vals, dtype=pl.String)
        if kind == "numeric" and c not in t.text_cols:
            nums = [parse_number(v, t.cfg["currency_symbols"]) if v is not None and (c, i) not in t.held else None
                    for i, v in enumerate(vals)]
            if all(n is not None or v is None for n, v in zip(nums, vals)):
                s = pl.Series(c, nums, dtype=pl.String).cast(pl.Int64 if all(n is None or "." not in n for n in nums) else pl.Float64)
        elif kind == "date":
            args = t.date_args(c)
            parsed = [parse_date(v, *args) if v is not None and (c, i) not in t.held else (("held", None, None) if v is not None else None)
                      for i, v in enumerate(vals)]
            if all(p is None or (p[0] == "ok" and "T" not in vals[i] and ":" not in vals[i]) for i, p in enumerate(parsed)):
                s = pl.Series(c, [date(*p[1]) if p else None for p in parsed], dtype=pl.Date)
        cols[c] = s
    return pl.DataFrame(cols)


def write_table(t: Table, out: Path) -> Path:
    path = out / "tables" / t.ref.replace(":", "_") / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    typed_frame(t).write_parquet(path)
    return path
