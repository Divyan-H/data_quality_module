"""Phase D extra output: a monthly table spread over Monday-to-Sunday weeks (src/common/monthly_weeks.py).

Nothing in Phase D's checks, report or findings depends on it. It is written only for a table whose time level is monthly,
beside the checks, under <phase D folder>/weekly/<table stem>/:

    tables/<stem>_w/data.parquet   the weekly table (what the Results page publishes as <table>_weekly)
    Weekly_Allocation.xlsx         what was split: the rule, one line per monthly row, the weekly table (when it fits)
    meta.json                      counts for the summary
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import openpyxl

from src.common import units
from src.common.dataset import Dataset, dump, write_table
from src.common.monthly_weeks import NEW_COLUMNS, monthly_to_weekly

DIR = "weekly"
WORKBOOK = "Weekly_Allocation.xlsx"
RULE = ("Each monthly figure is split over the Monday-to-Sunday weeks of its month in proportion to the days each week has "
        "in the month. A week that crosses a month boundary is two separate rows, one per month (a month starting on a "
        "Saturday has a first row of 2 days). The rows of a month add up to its monthly figure.")


def _cell(v):
    return v if v is None or isinstance(v, (int, float, str, bool)) else str(v)


def _workbook(path: Path, weekly: Dataset, log: list[dict], meta: dict, max_rows: int) -> None:
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("Summary")
    for row in (["Rule", RULE], ["Source table", f"{meta['source_file'] or '-'} / {meta['sheet'] or '-'}"],
                ["Monthly rows", meta["rows_in"]], ["Weekly rows", meta["rows_out"]],
                ["Measure columns split", ", ".join(meta["measures"]) or "(none)"],
                ["Rows whose weeks add back up to the month", f"{meta['exact']} of {meta['rows_in'] - meta['unreadable']}"],
                ["Rows skipped (date cannot be read)", meta["unreadable"]]):
        ws.append(row)
    ws = wb.create_sheet("Allocation")
    ws.append(["Row_ID", "Month", "Status", "Weeks", "Days per week", "Adds up to the month"])
    for e in log:
        ws.append([e["row_id"], e["month"], e["status"], e["segments"],
                   ", ".join(map(str, e.get("days", []))), None if e["exact"] is None else ("yes" if e["exact"] else "NO")])
    if len(weekly.row_ids) <= max_rows:
        ws = wb.create_sheet("Weekly")
        ws.append(["Row_ID", *weekly.columns])
        for i, rid in enumerate(weekly.row_ids):
            ws.append([rid, *(_cell(weekly.data[c][i]) for c in weekly.columns)])
    wb.save(path)


def write_weekly(ds: Dataset, phase_dir: Path, cfg: dict, log: logging.Logger) -> dict | None:
    """Write the weekly table of `ds` when it is monthly; returns its meta, or None (not monthly, or switched off).
    A failure is logged and recorded in meta.json, never raised: it must not stop the checks."""
    if not cfg.get("monthly_to_weekly", True):
        return None
    unit = Path(phase_dir) / DIR / units.stem(ds.ref)
    shutil.rmtree(unit, ignore_errors=True)
    try:
        done = monthly_to_weekly(ds, cfg["dayfirst"])
        if done is None:
            return None
        weekly, entries = done
        unit.mkdir(parents=True, exist_ok=True)
        write_table(weekly, unit)
        measures = sorted({m for e in entries for m in e.get("figures", {})})
        meta = {"table_ref": ds.ref, "weekly_ref": weekly.ref, "source_file": ds.source_file, "sheet": ds.sheet,
                "rows_in": len(ds.row_ids), "rows_out": len(weekly.row_ids), "measures": measures,
                "added_columns": NEW_COLUMNS, "exact": sum(e["exact"] is True for e in entries),
                "unreadable": sum(e["status"] != "split" for e in entries),
                "inexact": [e["row_id"] for e in entries if e["exact"] is False][:50], "error": None}
        _workbook(unit / WORKBOOK, weekly, entries, meta, cfg.get("max_sheet_rows", 1_000_000))
        dump(unit / "meta.json", meta)
        log.info("phase D: %s is monthly: %d row(s) spread over %d week row(s)", ds.ref, meta["rows_in"], meta["rows_out"])
        return meta
    except Exception as e:  # noqa: BLE001 - an extra output must never stop the checks; the reason is kept for the analyst
        unit.mkdir(parents=True, exist_ok=True)
        meta = {"table_ref": ds.ref, "error": f"{type(e).__name__}: {e}"}
        dump(unit / "meta.json", meta)
        log.warning("phase D: weekly split of %s failed: %s", ds.ref, meta["error"])
        return meta


def tables(phase_dir: Path) -> list[tuple[dict, Path]]:
    """(meta, folder) of every weekly table written under a Phase D folder."""
    out = []
    root = Path(phase_dir) / DIR
    for folder in sorted(root.glob("*")) if root.is_dir() else []:
        try:
            meta = json.loads((folder / "meta.json").read_text("utf8"))
        except (OSError, ValueError):
            continue
        if not meta.get("error") and (folder / "tables").is_dir():
            out.append((meta, folder))
    return out
