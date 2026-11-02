"""Data_Validation_Report.xlsx: the reference report's sheets, plus what the analyst review needs.

Same as Data_Validation_Report_fixed.py: Overall_Summary, Errors, WOW_Duplicates, MOM_Duplicates, No_Executions,
Duplicate_Variable_Data, Check_Index (a Code column added where a sheet names checks). Added:
  Variable_Checks   N-01 to N-05 and N-15 findings, one row per variable and check (the Errors sheet aggregates them)
  Date_Integrity    N-11 to N-14 findings, one row per date (Overall_Summary only counts them)
  Execution_Weeks   N-06 for every variable of the Variables range, checked or not
  Data              the checked table, editable; cells involved in a finding are yellow with a comment
Every findings sheet ends with Finding_ID and Review; Review = "Valid – no change" stops a finding being reported."""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import openpyxl
from openpyxl.cell import WriteOnlyCell
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .checks import CHECKS, SHEET, VALUE_CODES, Result
from src.common.dataset import Dataset, TableRows
from .layout import Layout
from src.common.values import excel_text

VALID = "Valid – no change"
REVIEW = ["Finding_ID", "Review"]
# Findings sheets: their own columns (then REVIEW). One row per finding, in findings.json order.
COLUMNS = {
    "Variable_Checks": ["Code", "Check", "Bucket", "Description", "Variables", "Count", "Periods", "Detail"],
    "WOW_Duplicates": ["Series", "Variables", "Description", "Bucket", "Period", "Compared Period", "Comparison Type",
                       "Value", "Occurrences", "Periods"],
    "MOM_Duplicates": ["Series", "Variables", "Description", "Bucket", "Month", "Compared Month", "Comparison Type",
                       "Value", "Occurrences", "Periods"],
    "No_Executions": ["Series", "Bucket", "Description", "Variables", "Occurrence", "Start Period", "End Period",
                      "Number of Weeks", "All Periods"],
    "Duplicate_Variable_Data": ["Variable 1", "Variable 1 Description", "Variable 1 Bucket", "Variable 2",
                                "Variable 2 Description", "Variable 2 Bucket", "Reason"],
    "Date_Integrity": ["Code", "Check", "Series", "Period", "Related Period", "Detail", "Row_IDs"],
}
SHEETS = ["Overall_Summary", "Errors", "Variable_Checks", "WOW_Duplicates", "MOM_Duplicates", "No_Executions",
          "Duplicate_Variable_Data", "Date_Integrity", "Execution_Weeks", "Check_Index", "Data"]
ERRORS = ["Bucket", "Description", "Variables", "Execution Weeks", "Codes", "Error", "Error Count", "Reason"]
VARIABLES = ["Bucket", "Description", "Variables", "Status", "Execution Weeks"]
DATASET_CODES = ["N-13", "N-11", "N-12", "N-14", "N-15"]      # Overall_Summary rows, in the reference's order
VAR_CODES = [*VALUE_CODES, "N-07", "N-08", "N-09", "N-10"]   # aggregated per variable on the Errors sheet
COUNTED = {"N-07": "No. of occurrences = {n}. Each occurrence contains {weeks}+ consecutive periods with no executions.",
           "N-08": "No. of occurrences = {n} repeated value(s).",
           "N-09": "No. of occurrences = {n} repeated monthly total(s)."}
PASS_TEXT = {"N-13": "Dates are in chronological order within each series", "N-11": "No duplicate dates within a series",
             "N-12": "No missing {level} periods", "N-14": "Consecutive dates are one {level} period apart",
             "N-15": "Variable names are unique"}

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
REVIEW_FILL = PatternFill("solid", fgColor="BF8F00")         # the one column the analyst fills in
GREEN, RED = PatternFill("solid", fgColor="C6EFCE"), PatternFill("solid", fgColor="FFC7CE")
FLAG_FILL = PatternFill("solid", fgColor="FFEB9C")            # Data sheet: cell involved in an open finding
WHITE_BOLD, RED_FONT, GREEN_FONT = (Font(color="FFFFFF", bold=True), Font(color="9C0006", bold=True),
                                    Font(color="006100", bold=True))


def shown(res: Result, limit: int) -> dict[str, list]:
    """Open findings listed on each findings sheet (at most `limit` rows a sheet)."""
    out = {s: [] for s in COLUMNS}
    for f in res.findings:
        if f.status == "open" and len(out[f.sheet]) < limit:
            out[f.sheet].append(f)
    return out


# ── sheet contents ───────────────────────────────────────────────────────────
def _fail_text(code: str, fs: list) -> str:
    if code == "N-15":
        return "Duplicate variable name(s): " + ", ".join(f.row["Variables"] for f in fs)
    n = len(fs)
    return {"N-13": f"Dates are not in chronological order ({n} row(s) dated before the series' row above)",
            "N-11": f"{n} date(s) repeated within a series", "N-12": f"{n} missing period(s)",
            "N-14": f"One or more gaps in the calendar ({n})"}[code]


def overall_rows(ds: Dataset, lay: Layout, res: Result, cfg: dict, hidden: dict[str, int]) -> list[list]:
    rows = []
    for code in DATASET_CODES:
        fs = [f for f in res.findings if f.code == code]
        open_f = [f for f in fs if f.status == "open"]
        valid = len(fs) - len(open_f)
        if open_f:
            status, reason = "FAIL", _fail_text(code, open_f) + (f"; {valid} more marked {VALID}" if valid else "")
        elif valid:
            status, reason = "PASS", f"All {valid} finding(s) marked {VALID}"
        else:
            status, reason = "PASS", PASS_TEXT[code].format(level=res.level)
        rows.append([code, CHECKS[code][0], status, reason])
    distinct = sorted({d for d in res.dates if d})
    buckets = "/".join(b or "blank" for b in cfg["ignore_buckets"])
    rows += [["", "Variables Checked", "INFO", str(len(res.checked))],
             ["", "Variables Ignored", "INFO", f"{len(lay.variables) - len(res.checked)} (not in Variable Mapping, "
                                                f"a repeated name, or Bucket {buckets})"],
             ["", "Total Periods", "INFO", str(len(distinct))],
             ["", "First Period", "INFO", distinct[0].isoformat() if distinct else ""],
             ["", "Last Period", "INFO", distinct[-1].isoformat() if distinct else ""],
             ["", "Table", "INFO", f"{ds.ref} ({ds.source_file or '-'} / {ds.sheet or '-'}), {len(ds.row_ids)} row(s)"]]
    rows += [["", label, "INFO", text] for label, text in res.info]
    valid_n = sum(f.status == "valid" for f in res.findings)
    if valid_n:
        rows.append(["", "Findings Marked Valid", "INFO", f"{valid_n} finding(s) marked {VALID}, not reported again "
                                                          "(findings.json lists them)"])
    rows += [["", "Rows Not Shown", "INFO", f"{sheet}: {n} more finding(s) than the sheet shows (all are in "
                                            "findings.json)"] for sheet, n in hidden.items() if n]
    return rows


def errors_rows(res: Result) -> tuple[list[str], list[list]]:
    """One row per variable with an open finding: checks and reasons joined, then one detail column per check."""
    open_f = [f for f in res.findings if f.status == "open" and f.code in VAR_CODES]
    counts = Counter((f.code, f.variables[0]) for f in open_f if f.code in COUNTED)
    per: dict[str, dict] = {}
    detail_cols: dict[str, None] = {}

    def add(v: str, f, reason: str, detail: str | None) -> None:
        e = per.setdefault(v, {"codes": {}, "errors": {}, "reasons": {}, "detail": {}})
        e["codes"][f.code] = e["errors"][f.check] = e["reasons"][reason] = None
        if detail is not None:
            detail_cols[f.check] = None
            e["detail"][f.check] = detail

    for f in open_f:
        if f.code == "N-10":
            a, b = f.variables
            add(a, f, f"Variable data is duplicated with '{b}'.", None)
            add(b, f, f"Variable data is duplicated with '{a}'.", None)
        elif f.code in COUNTED:
            n = counts[(f.code, f.variables[0])]
            add(f.variables[0], f, COUNTED[f.code].format(n=n, weeks=res.min_weeks), f"No. of occurrences = {n}")
        else:
            add(f.variables[0], f, f.reason, "Periods: " + " | ".join(dict.fromkeys(p for p in f.periods if p)))
    rows = []
    for v in sorted(per, key=lambda v: (v, res.meta.get(v, ("", ""))[1], res.meta.get(v, ("", ""))[0])):
        e, (b, d) = per[v], res.meta.get(v, ("", ""))
        rows.append([b, d, v, res.execution.get(v), " | ".join(e["codes"]), " | ".join(e["errors"]), len(e["errors"]),
                     " | ".join(e["reasons"]), *(e["detail"].get(c, "") for c in detail_cols)])
    return ERRORS + list(detail_cols), rows


def check_index_rows(res: Result, cfg: dict) -> list[list]:
    rows = [[code, name, logic.format(weeks=res.min_weeks, step=res.level.removesuffix("ly").replace("dai", "day")),
             SHEET.get(code, "Execution_Weeks")]
            for code, (name, logic) in CHECKS.items()]
    return rows + [[], ["", "Review", f"Findings sheets ({', '.join(COLUMNS)}): set Review to '{VALID}' to stop a "
                        "finding being reported. Data: correct values, add rows for missing weeks (Row_ID blank), "
                        "delete or reorder rows, but keep every existing Row_ID. Upload the workbook to re-run "
                        "Phase C on it.", ""]]


# ── writing (streamed: openpyxl write-only mode, so a sheet of a million findings is written row by row) ────
def _cell(ws, v, fill=None, font=None, comment: str | None = None, align=None):
    """One value for a streamed row: plain values as they are, styled / formula-like text as a WriteOnlyCell."""
    v = excel_text(v)
    formula_like = isinstance(v, str) and v.startswith("=")       # text that looks like a formula stays text
    if not (fill or font or comment or align or formula_like):
        return v
    c = WriteOnlyCell(ws, value=v)
    if formula_like:
        c.data_type = "s"
    if fill:
        c.fill = fill
    if font:
        c.font = font
    if align:
        c.alignment = align
    if comment:
        c.comment = Comment(comment, "Phase C", width=260, height=60 + 20 * comment.count("\n"))
    return c


def _sheet(wb, name: str, header: list, rows: list[list], marks: dict | None = None, wide: tuple = (),
           review_col: int | None = None) -> None:
    """Blue bold header, frozen top row, filter, widths from the first 500 rows (as the reference).
    `marks` = {row index: {column index: (fill, font, comment)}} styles single cells."""
    ws = wb.create_sheet(name)
    ws.freeze_panes = "A2"                                        # streamed sheets: set before the first row
    for j, h in enumerate(header):
        width = max((len(str(v)) for v in [h, *(r[j] for r in rows[:500] if j < len(r))] if v is not None), default=0)
        ws.column_dimensions[get_column_letter(j + 1)].width = (90 if h in wide else 20 if j + 1 == review_col
                                                                else min(max(width + 2, 12), 70))
    centre = Alignment(horizontal="center", vertical="center")
    ws.append([_cell(ws, h, REVIEW_FILL if j + 1 == review_col else HEADER_FILL, WHITE_BOLD, align=centre)
               for j, h in enumerate(header)])
    marks = marks or {}
    for i, r in enumerate(rows):
        styled = marks.get(i, {})
        ws.append([_cell(ws, v, *styled[j]) if j in styled else _cell(ws, v) for j, v in enumerate(r)])
    last = get_column_letter(len(header))
    ws.auto_filter.ref = f"A1:{last}{len(rows) + 1}"
    if review_col:
        col = get_column_letter(review_col)
        dv = DataValidation(type="list", formula1=f'"{VALID}"', allow_blank=True, promptTitle="Review",
                            prompt=f"{VALID}: this finding is correct data and is not reported again",
                            showInputMessage=True)
        dv.add(f"{col}2:{col}{max(len(rows) + 1, 2)}")
        ws.data_validations.append(dv)


def _data_marks(ds: Dataset, res: Result) -> dict:
    """Data sheet cells involved in an open finding: yellow, with a comment naming the checks."""
    notes: dict[tuple, dict] = defaultdict(dict)
    for f in res.findings:
        if f.status == "open":
            for cell in f.cells:
                notes[cell][f"{f.code} {f.check}"] = None
    row_of = {rid: n for n, rid in enumerate(ds.row_ids)}
    col_of = {c: n + 1 for n, c in enumerate(ds.columns)}             # column 0 is Row_ID
    marks: dict[int, dict] = defaultdict(dict)
    for (rid, col), lines in notes.items():
        if rid in row_of and col in col_of:
            marks[row_of[rid]][col_of[col]] = (FLAG_FILL, None, "\n".join(lines))
    return marks


def export(path: Path, ds: Dataset, lay: Layout, res: Result, cfg: dict) -> Path:
    listed = shown(res, cfg["max_sheet_rows"])
    total = Counter(f.sheet for f in res.findings if f.status == "open")
    hidden = {s: total[s] - len(listed[s]) for s in COLUMNS}
    wb = openpyxl.Workbook(write_only=True)
    for name in SHEETS:
        if name == "Overall_Summary":
            rows = overall_rows(ds, lay, res, cfg, hidden)
            marks = {i: {2: (GREEN, GREEN_FONT, None) if r[2] == "PASS" else (RED, RED_FONT, None)}
                     for i, r in enumerate(rows) if r[2] in ("PASS", "FAIL")}
            _sheet(wb, name, ["Code", "Check", "Status", "Reason"], rows, marks, wide=("Reason",))
        elif name == "Errors":
            header, rows = errors_rows(res)
            count = ERRORS.index("Error Count")
            _sheet(wb, name, header, rows, {i: {count: (RED, RED_FONT, None)} for i, r in enumerate(rows) if r[count]},
                   wide=("Reason",))
        elif name == "Execution_Weeks":
            _sheet(wb, name, VARIABLES, [[r[c] for c in VARIABLES] for r in res.variables])
        elif name == "Check_Index":
            _sheet(wb, name, ["Code", "Check", "Logic", "Sheet"], check_index_rows(res, cfg))
        elif name == "Data":
            rows = TableRows(ds)
            _sheet(wb, name, ["Row_ID", *ds.columns], rows, _data_marks(ds, res))
        else:
            cols = COLUMNS[name]
            _sheet(wb, name, cols + REVIEW, [[f.row.get(c) for c in cols] + [f.id, None] for f in listed[name]],
                   review_col=len(cols) + 2)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
