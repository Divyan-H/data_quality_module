"""The table Phase D checks. Input is a Phase C run folder (its checked table, after any Phase C upload: the corrected
one) or one csv / xlsx / parquet file (Phase D alone, as the reference script). Only the file contract is shared with
the other phases - tables/<ref>/data.parquet with a _row_id column, and run.json - plus the loading code in
src/common/dataset.py, so Row_IDs and table refs are the ones Phases A to C give the same file."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.common import units
from src.common.granularity import with_period
from src.common.dataset import Dataset, load_loose, load_phase_b, locate, phase_b_refs, read_table

from .config import ClientConfig

from src.common.errors import ConfigError
from src.common.values import Periods
from src.common.values import clean_name, letter

PHASE_C = "phase_c_timeseries"
PHASE_B = "phase_b_hygiene"


def _tables(b: Path, c: Path | None, sheet: str | None):
    """Phase B's tables in its order, each as Phase C left it when Phase C checked it (after any Phase C upload: the
    corrected table), else as Phase B wrote it. Loaded one at a time."""
    done = {e["table_ref"]: folder for e, folder in units.checked(c)} if c else {}
    for ref in phase_b_refs(b):
        run = json.loads((done[ref] / "run.json").read_text("utf8")) if ref in done else {}
        if ref in done and not run.get("derived"):
            ds = read_table(done[ref], run)                                  # Phase C's: its Period is real
        else:                    # not checked by C, or C worked on a weekly table made from this monthly one: Phase D keeps the monthly table
            ds = with_period(load_phase_b(b, ref))
        if not sheet or ds.sheet == sheet:
            yield ds


def _checked_tables(c: Path, sheet: str | None):
    """Phase C ran on a loose file (no Phase B): its checked tables are the input."""
    for _, unit in units.checked(c):
        ds = read_table(unit, json.loads((unit / "run.json").read_text("utf8")))
        if not sheet or ds.sheet == sheet:
            yield ds


def load_input(path, sheet: str | None = None):
    """(candidate tables, one at a time; the client config workbook Phase C ran with, or None)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"input not found: {path}")
    if not p.is_dir():
        return (with_period(d) for d in load_loose(p, sheet, "a Phase C run folder")), None
    b = next((f for f in (p / PHASE_B, p) if (f / "run.json").exists() and (f / "tables").is_dir()), None)
    c = p / PHASE_C if units.read_index(p / PHASE_C) else None
    if b is None and c is None:
        raise FileNotFoundError(f"{p} has neither a Phase C nor a Phase B result: run Phase C (or B) first, "
                                "or give one csv / xlsx / parquet file")
    config = c / "client_config.xlsx" if c and (c / "client_config.xlsx").exists() else None
    return (_tables(b, c, sheet) if b else _checked_tables(c, sheet)), config


# ── layout: the Data File sheet on the table ─────────────────────────────────
@dataclass
class Layout:
    period: str                       # column names in the table
    geography: str | None             # mapped only to keep it out of the Variables range (no Phase D check reads it)
    variables: list[str]              # the Variables range: that column and every later one with a header
    raw: dict[str, str]               # variable column -> header text in the source (repeats kept)
    repeated: set[str]                # variable columns that repeat an earlier header (named name__2 ...)


def resolve_layout(ds: Dataset, cc: ClientConfig, cfg: dict) -> Layout:
    """Map the Data File sheet onto the table. A letter counts the table's columns (A = its first column)."""
    cols, raw = ds.columns, ds.raw_header
    if not ds.row_ids:
        raise ConfigError(f"table {ds.ref} has no rows to check")
    p = locate(cc.columns["period"], "Period", ds)
    g = locate(cc.columns["geography"], "Geography", ds) if cc.columns.get("geography") else None
    v = locate(cc.columns["variables"], "Variables", ds)
    period = cols[p]
    parse = Periods(ds.data[period], cfg["dayfirst"])
    if not any(parse(x) for x in ds.data[period]):
        raise ConfigError(f"the Period column '{period}' (column {letter(p)}) holds no dates: check the Data File "
                          "sheet (a letter counts the checked table's columns, A = its first column)")
    idx = [i for i in range(v, len(cols)) if raw[i].strip() and i not in (p, g)]
    seen: set[str] = set()
    repeated = set()
    for i in idx:
        key = clean_name(raw[i]).casefold()
        if key in seen and cols[i] != clean_name(raw[i]):
            repeated.add(cols[i])
        seen.add(key)
    return Layout(period, cols[g] if g is not None else None, [cols[i] for i in idx], {cols[i]: raw[i] for i in idx},
                  repeated)


def variable_columns(lay: Layout) -> dict[str, str]:
    """Variable Mapping name -> column of the Variables range: by column name, then by header text (the first of a
    repeated header wins; the repeats are left out)."""
    out: dict[str, str] = {}
    for c in lay.variables:
        out.setdefault(clean_name(c), c)
    for c in lay.variables:
        if c not in lay.repeated:
            out.setdefault(clean_name(lay.raw[c]), c)
    return out


def why_not(ds: Dataset, cc: ClientConfig, cfg: dict) -> str | None:
    """Why the client config does not describe this table (no Period column of mostly dates, or no Support variable
    of Variable Mapping), or None when it does."""
    try:
        lay = resolve_layout(ds, cc, cfg)
    except ConfigError as e:
        return str(e)
    parse = Periods(ds.data[lay.period], cfg["dayfirst"])
    vals = [x for x in ds.data[lay.period] if x is not None]
    if not vals or sum(parse(x) is not None for x in vals) / len(vals) < 0.5:
        return f"fewer than half of the '{lay.period}' values are dates"
    names = variable_columns(lay)
    if not any(p.support in names for p in cc.pairs):
        return "none of its variables is a Support variable marked Y in Variable Mapping"
    return None
