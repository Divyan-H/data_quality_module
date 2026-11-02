"""`Issues` and `Changes` sheets of `Structural_Review.xlsx`: what Phase A found, fixed and flagged, in one place.

Issues: one row per finding, with a Status the analyst can sort on:
  Warning    - needs a look (severity warn or block);
  Fixed      - Phase A changed the table's structure to resolve it (see Changes for exactly what);
  Identified - detected and reported; the data was left as the file stores it.
Changes: one row per structural change from each file's `audit.parquet`.

Both sheets are read-only and are not part of the import (review_import ignores them).
"""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FONT = Font(bold=True)
STATUS_FILL = {"Warning": PatternFill("solid", fgColor="FCE4D6"), "Fixed": PatternFill("solid", fgColor="E2EFDA"),
               "Identified": PatternFill("solid", fgColor="DDEBF7")}

ISSUE_NAMES = {
    "S-01": "Headers not at row 1", "S-02": "Headers not at column A", "S-03": "Multi-row headers",
    "S-04": "Data at an arbitrary location", "S-05": "Empty columns", "S-06": "Sparse sheet",
    "S-07": "Multiple tables in one sheet", "S-08": "Aesthetic gap rows", "S-09": "Data scattered across tabs",
    "S-10": "Merged cells", "S-11": "Hidden rows", "S-12": "Hidden columns", "S-13": "Grouped rows",
    "S-14": "Grouped columns", "S-15": "Frozen panes", "S-16": "AutoFilter", "S-17": "Print area",
    "S-18": "Formatting hides values", "S-19": "Data validation", "S-20": "Broken named ranges",
    "S-21": "Pivot tables", "S-22": "Excel Table objects", "S-23": "Protection", "S-24": "Sheet name",
    "S-25": "Hidden sheet", "S-26": "Formulas without a usable result", "S-27": "Objects covering data",
    "S-28": "Cell comments", "footer_rows": "Footer rows", "interior_totals": "Total rows inside the data",
    "sheet_no_table": "Sheet without a table", "sheet_error": "Sheet could not be read",
    "sheet_too_large": "Sheet too large", "selection_required": "Tables to select",
    "selection_not_found": "Selected table not found", "selection_ambiguous": "Selection matches several tables",
    "multiple_layouts": "Several layouts in one source", "override_orphaned": "Correction no longer applies",
    "assistant_decision": "Changed by the LLM review", "llm_fallback": "LLM review incomplete",
    "unsupported_format": "Unsupported file", "password_protected": "Password protected",
    "file_unreadable": "File unreadable", "file_empty": "File empty", "file_format_mismatch": "Extension mismatch",
    "format_converted": "Converted to XLSX", "read_conflict": "Conflicting read settings",
    "csv_encoding_guessed": "Encoding guessed", "csv_ragged": "Ragged CSV records",
}

# What Phase A did about each issue ("code.check", or the code alone). Wording is for the analyst.
ACTIONS = {
    "S-01.placement": "Table starts at its own header row and first column; rows above moved to Excluded_Cells",
    "S-01.uncertain_header": "Header row chosen by the rules; confirm it (Header rows in Structural_Findings)",
    "S-01.no_header": "Columns named by their letter; set Header rows if the table has one",
    "S-01.blank_header": "Unlabelled columns named by their letter (col_X)",
    "S-03.groups": "Group and unit rows recorded on each column",
    "S-03.composite_ambiguous": "Names composed from inferred group spans; check the column names",
    "S-03.duplicate_header": "Duplicate names numbered __2, __3",
    "S-03.wide_time_layout": "Left as it is; time across columns is handled in mapping",
    "S-05": "Columns with no label and no data dropped; sparse columns kept",
    "S-06": "Only real tables extracted; scattered cells kept in Excluded_Cells",
    "S-07.tables": "Each table extracted separately",
    "S-07.soft_split": "Split at a single empty row or column; confirm the split",
    "S-08": "Empty rows inside the data removed (Row_IDs keep the Excel row)",
    "S-09": "Layouts grouped; likely renames passed to the mapping step",
    "S-10.merges": "Merged labels copied into each cell of the merge",
    "S-10.numeric_merge": "Number kept on the merge's first row only, to avoid multiplying totals",
    "S-11": "Hidden rows included in the table and flagged in _row_hidden",
    "S-12": "Hidden columns included in the table",
    "S-13": "Rows kept; outline level recorded in _outline_level",
    "S-14": "Columns kept; outline level recorded",
    "S-15": "Header chosen from the data, not from the freeze",
    "S-16": "All rows included; filtered rows flagged in _row_filtered",
    "S-17": "All rows and columns included; the print area is ignored",
    "S-18": "Values included; affected cells marked",
    "S-19": "Values kept as stored; violating cells marked",
    "S-20": "No effect on the tables",
    "S-21.pivot": "Extracted as a table but never auto-selected",
    "S-21.suspected_static_summary": "Extracted; check whether this is raw data or a summary",
    "S-22.excel_table": "The Excel Table's range used as the table",
    "S-22.table_object_mismatch": "Cells outside the Excel Table kept as a separate region",
    "S-23": "Read from a copy; the uploaded file is never changed",
    "S-24": "A safe key is used in references; the exact name is kept",
    "S-25": "Read like any sheet; its tables are never auto-selected",
    "S-26.formulas": "Stored results used; missing results left empty (never recalculated)",
    "S-26": "Stored results used as they are",
    "S-27": "Values read as usual",
    "S-28": "Comment kept for review (Comments column); the value is not changed",
    "footer_rows": "Moved out of the table into Excluded_Cells",
    "interior_totals": "Kept in the table; exclude them with Exclude rows if they are not data",
    "multiple_layouts": "Each layout is mapped on its own",
    "sheet_no_table": "Cells kept in Excluded_Cells",
    "sheet_error": "Cells kept as unread in Excluded_Cells",
    "override_orphaned": "Correction not applied; re-apply or remove it",
    "llm_fallback": "The rules decided the remaining questions",
}

# When the issue was found but nothing needed changing, the action says so instead.
ACTIONS_NOT_FIXED = {
    "S-05": "Nothing dropped; sparse and header-only columns kept",
    "S-10.merges": "Header merges recorded as column groups; no data cell filled",
}

ISSUE_COLS = ["Status", "Severity", "Issue", "Code", "File", "Sheet", "Table", "Where", "What was found",
              "Action taken"]
CHANGE_COLS = ["File", "Sheet", "Table", "Issue", "Code", "Change", "Range", "Count", "Decided by", "Before",
               "After", "Note"]
_STATUS_ORDER = {"Warning": 0, "Fixed": 1, "Identified": 2}
_SEV_ORDER = {"block": 0, "warn": 1, "info": 2}


def status(f: dict) -> str:
    if f["severity"] in ("warn", "block"):
        return "Warning"
    return "Fixed" if f.get("fixed") else "Identified"


def _action(f: dict) -> str:
    keys = (f"{f['code']}.{f['check']}", f["code"])
    if not f.get("fixed"):
        text = next((ACTIONS_NOT_FIXED[k] for k in keys if k in ACTIONS_NOT_FIXED), None)
        if text:
            return text
    return next((ACTIONS[k] for k in keys if k in ACTIONS), "")


def _where(f: dict) -> str:
    """Cells or columns the finding names (from its locations), else blank."""
    parts = []
    for loc in f.get("locations", [])[:10]:
        if "ref" in loc:
            parts.append(loc["ref"])
        elif "col" in loc:
            parts.append(f"column {loc['col']}")
        elif "name" in loc:
            parts.append(loc["name"])
    return ", ".join(parts)


def _scope(f: dict, paths: dict, sheet_names: dict) -> tuple[str, str, str]:
    """(file, sheet, table) of a finding, from its id `code@{source}/{file_key}:{sheet}:{table}`."""
    scope = f["id"].split("@", 1)[1].split("#")[0]
    if scope.startswith("config/"):
        scope = scope[len("config/"):]
    elif "/" in scope:
        scope = scope.split("/", 1)[1]
    else:
        return "", "", ""
    parts = scope.split(":")
    file_key = parts[0]
    sheet = sheet_names.get((file_key, int(parts[1])), parts[1]) if len(parts) > 1 and parts[1].isdigit() else ""
    return paths.get(file_key, file_key), sheet, parts[2] if len(parts) > 2 else ""


def _header(ws, cols: list[str], widths: list[int]) -> None:
    """Sheet setup, then the bold header row (the sheets are write-only: set up before the first row)."""
    for i, w in enumerate(widths):
        ws.column_dimensions[get_column_letter(i + 1)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}1"
    head = []
    for c in cols:
        cell = WriteOnlyCell(ws, value=c)
        cell.font = HEADER_FONT
        head.append(cell)
    ws.append(head)


def write_issues(ws, findings: list[dict], paths: dict, sheet_names: dict) -> dict:
    """Fills the Issues sheet; returns the count per status (for the README)."""
    _header(ws, ISSUE_COLS, [11, 9, 30, 26, 24, 20, 7, 18, 70, 60])
    rows = []
    for f in findings:
        file, sheet, table = _scope(f, paths, sheet_names)
        code = f"{f['code']}.{f['check']}" if f["check"] else f["code"]
        rows.append([status(f), f["severity"], ISSUE_NAMES.get(f["code"], f["code"]), code, file, sheet, table,
                     _where(f), f["message"], _action(f)])
    rows.sort(key=lambda r: (_STATUS_ORDER[r[0]], _SEV_ORDER.get(r[1], 3), r[4], r[5], r[6], r[3]))
    counts = {"Warning": 0, "Fixed": 0, "Identified": 0}
    for r in rows:
        first = WriteOnlyCell(ws, value=r[0])
        first.fill = STATUS_FILL[r[0]]
        ws.append([first, *r[1:]])
        counts[r[0]] += 1
    return counts


def write_changes(ws, out: Path, inventory: dict, sheet_names: dict) -> int:
    """Fills the Changes sheet from each file's audit.parquet; returns the number of changes."""
    _header(ws, CHANGE_COLS, [24, 20, 7, 26, 12, 20, 16, 7, 11, 30, 30, 20])
    n, seen = 0, set()
    for f in inventory["files"]:
        path = out / "files" / f["file_key"] / "audit.parquet"
        if f["file_key"] in seen or not path.exists():      # one file shared by two sources is listed once
            continue
        seen.add(f["file_key"])
        for a in pq.read_table(path).to_pylist():
            before = "" if a["before"] in (None, "{}") else json.dumps(json.loads(a["before"]))
            after = "" if a["after"] in (None, "{}") else json.dumps(json.loads(a["after"]))
            ws.append([f["relative_path"], sheet_names.get((f["file_key"], a["sheet_index"]), a["sheet_index"]),
                       a["table_key"], ISSUE_NAMES.get(a["code"], a["code"]), a["code"],
                       a["action"].replace("_", " "), a["ref"], a["count"], a["decided_by"], before, after, a["note"]])
            n += 1
    return n
