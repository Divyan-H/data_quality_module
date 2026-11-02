"""The table Phases C and D check: reading it (a previous phase's output or one loose file), writing it back, and the
Data File lookups both phases use. Values stay Python objects (int / float / date / str / None); header text is kept
next to the unique column names, so repeated names are still visible."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import openpyxl
import polars as pl

from .columns import SYSTEM
from .errors import ConfigError
from .values import clean_name, letter, letter_index, render

ENCODINGS = ["utf-8-sig", "cp1252", "latin-1"]          # tried in order for a csv file
LOOSE = {".csv", ".xlsx", ".xlsm", ".parquet"}


@dataclass
class Dataset:
    ref: str                          # table ref (Phases A-C) or {file key}:{sheet index}:t1 for a loose file
    data: dict[str, list]             # every column, system columns (_row_id, _source_row, ...) included
    raw_header: list[str]             # header text of each data column as in the source (repeats kept)
    source_file: str | None = None
    sheet: str | None = None
    derived: str | None = None        # "monthly" when this is a weekly table calculated from a monthly one (common/weekly_rollup.py)

    @property
    def columns(self) -> list[str]:
        return [c for c in self.data if c not in SYSTEM]

    @property
    def row_ids(self) -> list[str]:
        return self.data["_row_id"]

    @property
    def stem(self) -> str:
        return self.ref.replace(":", "_")


class TableRows(Sequence):
    """[Row_ID, *values] for each row of a Dataset, built when asked for: a report's Data sheet streams the table
    without first copying it into a list of rows."""

    def __init__(self, ds: Dataset):
        self.ids, self.cols = ds.row_ids, [ds.data[c] for c in ds.columns]

    def __len__(self) -> int:
        return len(self.ids)

    def __getitem__(self, i):
        if isinstance(i, slice):
            return [self[n] for n in range(*i.indices(len(self)))]
        return [self.ids[i], *(col[i] for col in self.cols)]


# ── header names ─────────────────────────────────────────────────────────────
def unique_names(raw: list[str]) -> list[str]:
    """Header texts -> unique column names, as Phase A names them: blank -> col_<letter>, a repeat (case-insensitive)
    -> name__2, name__3 ..., a leading underscore (reserved for system columns) -> src_..."""
    names, used = [], set()
    for i, h in enumerate(raw):
        base = clean_name(h) or f"col_{letter(i)}"
        base = "src" + base if base.startswith("_") else base
        name, k = base, 2
        while name.casefold() in used:
            name, k = f"{base}__{k}", k + 1
        names.append(name)
        used.add(name.casefold())
    return names


def raw_names(columns: list[str]) -> list[str]:
    """Header text behind Phase A's unique names: 'Spend__2' is a repeat of 'Spend' when 'Spend' is also a column."""
    lower = {c.casefold() for c in columns}
    out = []
    for c in columns:
        m = re.fullmatch(r"(.+?)__(\d+)", c)
        out.append(m[1] if m and m[1].casefold() in lower else c)
    return out


# ── reading ──────────────────────────────────────────────────────────────────
def _filled(v) -> bool:
    return v is not None and not (isinstance(v, str) and not v.strip())


def cell_value(v):
    """A cell as the phases keep it: '' -> None, a date-only datetime (how Excel stores a date) -> date."""
    if isinstance(v, datetime) and v.time() == datetime.min.time():
        return v.date()
    return None if v == "" else v


def _from_grid(rows, ref: str, source_file: str, sheet: str | None) -> Dataset | None:
    """(row number, values) rows -> Dataset. The first non-empty row is the header; empty rows are skipped."""
    header, width, body = None, 0, []
    for r, vals in rows:
        vals = [cell_value(v) for v in vals]
        if header is None:
            if any(_filled(v) for v in vals):
                width = max(i for i, v in enumerate(vals) if _filled(v)) + 1
                header = [render(v) or "" for v in vals[:width]]
            continue
        if any(_filled(v) for v in vals[:width]):
            body.append((r, (list(vals) + [None] * width)[:width]))
    if header is None:
        return None
    data = {"_row_id": [f"{ref}:{n}" for n in range(1, len(body) + 1)], "_source_row": [r for r, _ in body]}
    data |= {c: [vals[j] for _, vals in body] for j, c in enumerate(unique_names(header))}
    return Dataset(ref, data, [clean_name(h) for h in header], source_file, sheet)


def _xlsx(p: Path, key: str, sheet: str | None) -> list[Dataset]:
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    try:
        found = (_from_grid(enumerate(ws.iter_rows(values_only=True), start=1), f"{key}:{i}:t1", p.name, ws.title)
                 for i, ws in enumerate(wb.worksheets) if not sheet or ws.title == sheet)
        return [d for d in found if d]
    finally:
        wb.close()


def _csv(p: Path, key: str) -> list[Dataset]:
    """Read straight from disk with the first encoding that decodes the whole file (latin-1 always does)."""
    for enc in ENCODINGS:
        try:
            with p.open(encoding=enc, newline="") as f:
                d = _from_grid(enumerate(csv.reader(f), start=1), f"{key}:0:t1", p.name, None)
            return [d] if d else []
        except UnicodeDecodeError:
            continue
    return []


def _parquet(p: Path, key: str) -> list[Dataset]:
    df = pl.read_parquet(p)
    data = {c: df[c].to_list() for c in df.columns}
    if "_row_id" in data and data["_row_id"]:
        ref = data["_row_id"][0].rsplit(":", 1)[0]
    else:
        ref = f"{key}:0:t1"
        data = {"_row_id": [f"{ref}:{n}" for n in range(1, df.height + 1)],
                "_source_row": list(range(2, df.height + 2)), **data}
    return [Dataset(ref, data, raw_names([c for c in data if c not in SYSTEM]), p.name, None)]


def phase_b_refs(folder: Path) -> list[str]:
    return json.loads((folder / "run.json").read_text("utf8")).get("tables", [])


def load_phase_b(folder: Path, ref: str) -> Dataset:
    """One table of a Phase B run, with its source file and sheet from the quality report."""
    stem = ref.replace(":", "_")
    df = pl.read_parquet(folder / "tables" / stem / "data.parquet")
    rep_path = folder / "quality_reports" / f"{stem}.json"
    rep = json.loads(rep_path.read_text("utf8")) if rep_path.exists() else {}
    data = {c: df[c].to_list() for c in df.columns}
    return Dataset(ref, data, raw_names([c for c in data if c not in SYSTEM]), rep.get("source_file"), rep.get("sheet"))


def iter_phase_b(folder: Path):
    """Every table of a Phase B run, in run.json order, loaded one at a time (only one is in memory)."""
    for ref in phase_b_refs(folder):
        yield load_phase_b(folder, ref)


def load_loose(p: Path, sheet: str | None, what: str) -> list[Dataset]:
    """The tables of one csv / xlsx / parquet file; `what` names the folder the caller would otherwise want."""
    suffix = p.suffix.lower()
    if suffix not in LOOSE:
        raise FileNotFoundError(f"unsupported input {p.name}: give {what} or a csv / xlsx / parquet file")
    with p.open("rb") as f:                                  # hashed in chunks, never held whole
        key = hashlib.file_digest(f, "sha256").hexdigest()[:12]
    found = _csv(p, key) if suffix == ".csv" else _parquet(p, key) if suffix == ".parquet" else _xlsx(p, key, sheet)
    return [d for d in found if not sheet or d.sheet == sheet]


def read_table(folder: Path, run: dict) -> Dataset:
    """The table a finished run wrote to <folder>/tables/<ref>/data.parquet (run.json names it)."""
    df = pl.read_parquet(folder / "tables" / run["table_ref"].replace(":", "_") / "data.parquet")
    return Dataset(run["table_ref"], {c: df[c].to_list() for c in df.columns}, run["raw_header"],
                   run.get("source_file"), run.get("sheet"), run.get("derived"))


# ── writing ──────────────────────────────────────────────────────────────────
def _kind(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, datetime):
        return "datetime"
    if isinstance(v, date):
        return "date"
    return {int: "int", float: "float"}.get(type(v), "text")


def _series(name: str, vals: list) -> pl.Series:
    """One column, typed when every value has the same kind (numbers may mix int and float), else as text."""
    kinds = {_kind(v) for v in vals if v is not None}
    dtype = {frozenset({"bool"}): pl.Boolean, frozenset({"int"}): pl.Int64, frozenset({"float"}): pl.Float64,
             frozenset({"int", "float"}): pl.Float64, frozenset({"date"}): pl.Date,
             frozenset({"datetime"}): pl.Datetime, frozenset({"date", "datetime"}): pl.Datetime}.get(frozenset(kinds))
    if dtype is not None:
        cast = {pl.Float64: float, pl.Datetime: lambda v: v if isinstance(v, datetime) else datetime(v.year, v.month, v.day)}
        try:
            return pl.Series(name, [cast[dtype](v) if dtype in cast and v is not None else v for v in vals], dtype=dtype)
        except (OverflowError, TypeError, pl.exceptions.PolarsError):
            pass
    return pl.Series(name, [v if isinstance(v, str) or v is None else render(v) for v in vals], dtype=pl.String)


def write_table(ds: Dataset, out: Path) -> Path:
    """The checked table as tables/<ref>/data.parquet (Phase B's layout): what the next phase reads."""
    path = out / "tables" / ds.stem / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([_series(c, v) for c, v in ds.data.items()]).write_parquet(path)
    return path


def dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf8")


def dump_lines(path: Path, items: list) -> None:
    """A JSON list with one item per line: still one JSON document, a fraction of the indented size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ",\n".join(json.dumps(x, ensure_ascii=False, default=str) for x in items)
    path.write_text(f"[\n{body}\n]\n" if items else "[]\n", encoding="utf8")


# ── the Data File sheet on the table ─────────────────────────────────────────
def locate(ref: str, what: str, ds: Dataset) -> int:
    """A Data File reference -> column index: a column letter of the table (as the reference script reads it),
    else a header name (so 'Week' or 'TV' name a column when the table has no such letter)."""
    cols, raw = ds.columns, ds.raw_header
    is_letter = re.fullmatch(r"[A-Za-z]{1,3}", ref) is not None
    if is_letter and letter_index(ref) < len(cols):
        return letter_index(ref)
    for names in (cols, raw):
        if ref in names:
            return names.index(ref)
    if is_letter:
        raise ConfigError(f"Data File: {what} is column {ref.upper()}, but the table has {len(cols)} column(s) "
                          f"(A to {letter(len(cols) - 1)})")
    raise ConfigError(f"Data File: {what} column '{ref}' is neither a column letter nor a column name of the table")

