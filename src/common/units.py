"""Several tables in one Phase C or D run: one folder per checked table, and an index of every candidate table.

    <phase folder>/run.json        {"tables": [{"table_ref", "source_file", "sheet", "rows", "folder", "reason"}], ...}
    <phase folder>/<table stem>/   what a one-table run writes: report, findings.json, run.json, tables/, review files

`folder` is None for a table the client config does not describe; `reason` says why. A review upload is routed to its
table by the Row_IDs of its Data sheet (every Row_ID starts with its table ref).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import openpyxl

from .dataset import dump

INDEX = "run.json"


def stem(ref: str) -> str:
    return ref.replace(":", "_")


def read_index(out: Path) -> dict | None:
    """The phase folder's index, or None when the folder holds no multi-table run."""
    try:
        index = json.loads((Path(out) / INDEX).read_text("utf8"))
    except (OSError, ValueError):
        return None
    return index if isinstance(index.get("tables"), list) else None


def write_index(out: Path, entries: list[dict], **extra) -> dict:
    index = {"tables": entries, **extra}
    dump(Path(out) / INDEX, index)
    return index


def file_names(entries: list[dict]) -> dict[str, str]:
    """table ref -> a file-name-safe name for it: its sheet name (or source file name), made unique within the run."""
    out, used = {}, set()
    for e in entries:
        base = e.get("sheet") or Path(e.get("source_file") or e["table_ref"]).stem
        base = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_")[:60] or stem(e["table_ref"])
        name, n = base, 1
        while name.casefold() in used:
            n += 1
            name = f"{base}_{n}"
        used.add(name.casefold())
        out[e["table_ref"]] = name
    return out


def checked(out: Path) -> list[tuple[dict, Path]]:
    """(index entry, folder) of every table the run checked, in index order."""
    index = read_index(out) or {"tables": []}
    return [(e, Path(out) / e["folder"]) for e in index["tables"] if e.get("folder")]


def table_of_upload(workbook: Path) -> str | None:
    """The table a report belongs to: the ref its Data sheet's first Row_ID starts with (None if it has none)."""
    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    try:
        if "Data" not in wb.sheetnames:
            return None
        for row in wb["Data"].iter_rows(min_row=2, max_col=1, values_only=True):
            rid = str(row[0] or "").strip()
            if rid.count(":") >= 3:
                return rid.rsplit(":", 1)[0]
        return None
    finally:
        wb.close()


def unit_for_upload(out: Path, workbook: Path) -> Path:
    """The folder of the table an uploaded report belongs to. Raises ValueError when it cannot be told."""
    units = checked(out)
    ref = table_of_upload(workbook)
    for entry, folder in units:
        if entry["table_ref"] == ref:
            return folder
    if ref is None and len(units) == 1:
        return units[0][1]
    raise ValueError("this workbook does not belong to any table of this run: download the report of the table you "
                     "want to correct and edit that copy" if ref else
                     "the Data sheet has no Row_IDs, so the table this workbook belongs to cannot be told")
