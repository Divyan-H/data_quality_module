"""Spend_Support_Trend_Report.xlsx (spec DP-D §5): the reference report's sheets, built without Excel.

Same as Spend_Support_Trend_Report.py: Trend Line, MOM / QOQ / HOH / YOY_Changes (CPM columns moved to Ratio_Changes),
Spend Skewness, Rules_Used (hidden), Spend_Support_Availability, Outlier_Details, Spend_Concentration (hidden),
Unmapped_or_Missing. Changed or added:
  Trend Line      the reference built a native PivotTable + PivotChart through win32com (Excel on the machine, spec §6
                  note 1). Here it is a static table of every pair on every configured date with a line chart (Spend on
                  the secondary axis) that follows the Performance Variable filter: Excel plots only visible rows.
  Ratio_Changes   N-18, one row per ratio, period type and period (spec §5)
  Rules_Used      every rule with its threshold and ACTIVE / INACTIVE / ...; shown (not hidden) when a rule is inactive
                  for lack of a threshold, so the gap is visible (spec §3.6)
  Data            the checked table, editable for the review loop; N-16 / N-19 cells yellow with a comment
The Excel Data Model "Spends Trend Pivot Table" and its two pivot source sheets are not built: Spend_Concentration is
the same ranking, computed. Findings sheets end with Finding_ID and Review (Confirmed Issue / Valid Movement / Corrected).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import openpyxl
from openpyxl.cell import WriteOnlyCell
from openpyxl.chart import LineChart, Reference
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .checks import CHANGES, Result
from .config import PERIOD_TYPES
from src.common.dataset import Dataset, TableRows
from src.common.values import excel_text

STATUSES = ["Confirmed Issue", "Valid Movement", "Corrected"]       # the Review values (workflow document, Phase D)
STATUS_OF = {"Confirmed Issue": "confirmed", "Valid Movement": "valid", "Corrected": "corrected"}
REVIEW = ["Finding_ID", "Review"]
CHANGE_COLUMNS = [
    "Period", "Sequential Period", "Last Year Same Period", "__SEP1__", "Description", "Bucket", "__SEP2__",
    "Support Variable", "Current Support", "Sequential Period Support", "Last Year Same Period Support",
    "Sequential Period Support Change %", "Last Year Same Period Support Change %", "__SEP3__",
    "Spend Variable", "Current Spend", "Sequential Period Spend", "Last Year Same Period Spend",
    "Sequential Period Spend Change %", "Last Year Same Period Spend Change %", "__SEP4__",
    "Threshold %", "Change Status"]
# Sheets with a Finding_ID / Review column: their own columns (then REVIEW).
COLUMNS = {
    **{CHANGES[t]: CHANGE_COLUMNS for t in PERIOD_TYPES},
    "Ratio_Changes": ["Period Type", "Period", "Sequential Period", "Last Year Same Period", "Ratio Name", "Numerator",
                      "Denominator", "Current Numerator", "Current Denominator", "Current Ratio",
                      "Sequential Period Ratio", "Sequential Period Ratio Change %", "Last Year Same Period Ratio",
                      "Last Year Same Period Ratio Change %", "Threshold %", "Change Status"],
    "Spend Skewness": ["Period Type", "Period", "Period Start", "Bucket", "Aggregation 1", "Tactics in Aggregation 1",
                       "Top Tactics Count", "Total Aggregation 1 Spend", "Top Tactics Spend", "Skewness %",
                       "Threshold %", "Top Tactics", "Top Spend Variables", "Issue Reason", "Status"],
    "Spend_Support_Availability": ["Date", "Description", "Bucket", "Support Variable", "Spend Variable",
                                   "Support Value", "Spend Value", "Reason"],
    "Outlier_Details": ["Date", "Description", "Bucket", "Support Variable", "Spend Variable", "Support Value",
                        "Support Stream Median", "Support MAD", "Support Robust Z-score", "Support Status",
                        "Spend Value", "Spend Stream Median", "Spend MAD", "Spend Robust Z-score", "Spend Status",
                        "Outlier Reason"],
}
OTHER = {
    "Trend Line": ["Date", "Description", "Bucket", "Performance Variable", "Spend Variable", "Support", "Spend"],
    "Rules_Used": ["Code", "Rule", "Config Key", "Threshold", "Status", "Note"],
    "Spend_Concentration": ["Period Type", "Period", "Period Start", "Bucket", "Aggregation 1", "Rank", "Description",
                            "Support Variable", "Spend Variable", "Tactic Spend", "Spend Share %",
                            "Cumulative Share %", "Tactics in Aggregation 1", "Top Tactics Count",
                            "Total Aggregation 1 Spend", "Top Tactics Spend", "Concentration %", "Threshold %",
                            "Top Tactics", "Top Spend Variables", "Status"],
    "Unmapped_or_Missing": ["Support Variable", "Spend Variable", "Reason"],
}
SHEETS = ["Trend Line", *(CHANGES[t] for t in PERIOD_TYPES), "Ratio_Changes", "Spend Skewness", "Rules_Used",
          "Spend_Support_Availability", "Outlier_Details", "Spend_Concentration", "Unmapped_or_Missing", "Data"]
# Fixed widths of the reference for its two widest sheets (it does not measure tens of thousands of rows).
FIXED = {"Spend_Concentration": dict(zip("ABCDEFGHIJKLMNOPQRSTU", (14, 18, 14, 24, 24, 10, 30, 28, 28, 16, 15, 18,
                                                                    17, 18, 18, 18, 17, 14, 45, 45, 28))),
         "Spend Skewness": dict(zip("ABCDEFGHIJKLMNOPQ", (14, 18, 14, 24, 24, 17, 18, 18, 18, 17, 14, 45, 45, 75, 12,
                                                          22, 18)))}
DATE_FORMATS = {"Date": "yyyy-mm-dd", "Period Start": "dd-mmm-yyyy"}

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
REVIEW_FILL = PatternFill("solid", fgColor="BF8F00")         # the one column the analyst fills in
NA_FILL = PatternFill("solid", fgColor="D9D9D9")              # Review cell of a row with no finding
ALERT_FILL, GREEN = PatternFill("solid", fgColor="FFC7CE"), PatternFill("solid", fgColor="C6EFCE")
FLAG_FILL = PatternFill("solid", fgColor="FFEB9C")            # Data sheet: cell involved in a finding
WHITE_BOLD, RED_FONT, GREEN_FONT = (Font(color="FFFFFF", bold=True), Font(color="9C0006", bold=True),
                                    Font(color="006100", bold=True))
ALERT = (ALERT_FILL, RED_FONT)
CENTRE = Alignment(horizontal="center", vertical="center")


def header(name: str) -> list:
    """The header row a sheet is exported with (separator columns have none)."""
    cols = COLUMNS.get(name) or OTHER[name]
    return [None if c.startswith("__SEP") else c for c in cols] + (REVIEW if name in COLUMNS else [])


def review_label(status: str) -> str | None:
    return {"confirmed": "Confirmed Issue", "valid": "Valid Movement"}.get(status)


# ── cells ────────────────────────────────────────────────────────────────────
def _cell(ws, v, fill=None, font=None, fmt=None, comment=None, align=None):
    """One value for a streamed row: plain values as they are, styled / formula-like text as a WriteOnlyCell."""
    v = None if v == "" else excel_text(v)
    formula_like = isinstance(v, str) and v.startswith("=")       # text that looks like a formula stays text
    if not (fill or font or fmt or comment or align or formula_like):
        return v
    c = WriteOnlyCell(ws, value=v)
    if formula_like:
        c.data_type = "s"
    if fill:
        c.fill = fill
    if font:
        c.font = font
    if fmt:
        c.number_format = fmt
    if align:
        c.alignment = align
    if comment:
        c.comment = Comment(comment, "Phase D", width=280, height=60 + 20 * comment.count("\n"))
    return c


def _styles(name: str, row: dict) -> dict[str, tuple]:
    """Column -> (fill, font) of the alert cells of one row, as the reference highlights them."""
    if name in CHANGES.values() or name == "Ratio_Changes":
        return {"Change Status": ALERT} if row.get("Change Status") == "ALERT" else {}
    if name == "Outlier_Details":
        out = {}
        for side in ("Support", "Spend"):
            if row.get(f"{side} Status") == "OUTLIER":
                out |= {f"{side} Value": ALERT, f"{side} Robust Z-score": ALERT, f"{side} Status": ALERT}
        return out
    if name == "Spend Skewness":
        return {"Skewness %": ALERT, "Issue Reason": ALERT, "Status": ALERT}
    if name == "Spend_Concentration":
        return {"Status": ALERT, "Concentration %": ALERT} if row.get("Status") == "ALERT" else {}
    if name == "Rules_Used":
        s = row.get("Status")
        return {"Status": ALERT if s in ("INACTIVE", "NOT USED") else (GREEN, GREEN_FONT) if s == "ACTIVE" else ()}
    return {}


def _value(col: str, v):
    """(value, number format): percentages are written as fractions with a % format, as the reference does."""
    if col.endswith("%") and isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 100, "0.0%"
    return v, DATE_FORMATS.get(col) if v is not None else None


def _widths(ws, name: str, head: list, rows: list[list]) -> None:
    fixed = FIXED.get(name)
    for j, h in enumerate(head):
        letter = get_column_letter(j + 1)
        if fixed and letter in fixed:
            width = fixed[letter]
        elif h is None:
            width = 2.5
        elif h == "Review":
            width = 18
        else:
            longest = max((len(str(r[j])) for r in rows[:500] if j < len(r) and r[j] is not None), default=0)
            width = min(max(len(h), longest) + 2, 75 if h in ("Note", "Reason", "Outlier Reason") else 35)
            width = max(width, 12)
        ws.column_dimensions[letter].width = width


def _sheet(wb, name: str, rows: list[dict], limit: int, hidden: bool = False, hide_cols: tuple = ()):
    """One report sheet: blue bold header, frozen top row, filter, alert cells as the reference."""
    ws = wb.create_sheet(name)
    ws.freeze_panes = "A2"
    cols = COLUMNS.get(name) or OTHER[name]
    head = header(name)
    review = name in COLUMNS
    rows = rows[:limit]
    values = [[r.get(c) for c in cols] for r in rows]
    _widths(ws, name, head, values)
    for letter in hide_cols:
        ws.column_dimensions[letter].hidden = True
    if hidden:
        ws.sheet_state = "hidden"
    ws.append([_cell(ws, h, REVIEW_FILL if h == "Review" else HEADER_FILL, WHITE_BOLD, align=CENTRE) for h in head])
    seps = {j for j, c in enumerate(cols) if c.startswith("__SEP")}
    for r, vals in zip(rows, values):
        styled = _styles(name, r)
        line = []
        for j, (c, v) in enumerate(zip(cols, vals)):
            if j in seps:
                line.append(_cell(ws, None, HEADER_FILL))
                continue
            v, fmt = _value(c, v)
            fill, font = styled.get(c) or (None, None)
            line.append(_cell(ws, v, fill, font, fmt))
        if review:
            f = r.get("_finding")
            line += [f.id, review_label(f.status)] if f else [None, _cell(ws, None, NA_FILL)]
        ws.append(line)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{len(rows) + 1}"
    if review:
        col = get_column_letter(len(head))
        dv = DataValidation(type="list", formula1='"' + ",".join(STATUSES) + '"', allow_blank=True,
                            promptTitle="Review", showInputMessage=True,
                            prompt="Rows with a Finding_ID: Confirmed Issue, Valid Movement (not a data problem) or "
                                   "Corrected (fixed on the Data sheet)")
        dv.add(f"{col}2:{col}{max(len(rows) + 1, 2)}")
        ws.data_validations.append(dv)
    return ws


def _trend_line(wb, rows: list[dict], limit: int) -> None:
    """Every pair on every configured date. The filter starts on the first Performance Variable (other rows hidden);
    the chart shows whatever the filter shows."""
    ws = wb.create_sheet("Trend Line")
    ws.freeze_panes = "A2"
    cols = OTHER["Trend Line"]
    rows = rows[:limit]
    _widths(ws, "Trend Line", cols, [[r.get(c) for c in cols] for r in rows])
    first = rows[0]["Performance Variable"] if rows else None
    tip = ("Filter Performance Variable to one variable: the chart plots the rows the filter shows. Each configured "
           "Period Mapping date is one point; a date without data is 0.")
    ws.append([_cell(ws, h, HEADER_FILL, WHITE_BOLD, align=CENTRE, comment=tip if h == "Performance Variable" else None)
               for h in cols])
    for n, r in enumerate(rows, start=2):
        if r["Performance Variable"] != first:
            ws.row_dimensions[n].hidden = True
        ws.append([_cell(ws, r["Date"], fmt="dd-mm-yyyy"), r["Description"], r["Bucket"], r["Performance Variable"],
                   r["Spend Variable"], _cell(ws, r["Support"], fmt="#,##0.00"),
                   _cell(ws, r["Spend"], fmt="#,##0.00") if r["Spend"] is not None else None])
    last = len(rows) + 1
    ws.auto_filter.ref = f"A1:G{last}"
    if not rows:
        return
    ws.auto_filter.add_filter_column(3, [first])
    support = LineChart()
    support.title = "Support and Spend Trend"
    support.height, support.width = 11.5, 22
    support.y_axis.title = "Support"
    support.x_axis.number_format = "dd-mm-yyyy"
    support.x_axis.tickLblSkip = 8
    support.add_data(Reference(ws, min_col=6, min_row=1, max_row=last), titles_from_data=True)
    support.set_categories(Reference(ws, min_col=1, min_row=2, max_row=last))
    if any(r["Spend"] is not None for r in rows):
        spend = LineChart()
        spend.add_data(Reference(ws, min_col=7, min_row=1, max_row=last), titles_from_data=True)
        spend.y_axis.axId = 200
        spend.y_axis.title = "Spend"
        spend.y_axis.crosses = "max"
        spend.y_axis.delete = False
        support += spend
    support.x_axis.delete = support.y_axis.delete = False
    ws.add_chart(support, "I2")


def _data_marks(ds: Dataset, res: Result) -> dict:
    """Data sheet cells involved in an open or confirmed finding: yellow, with a comment naming the checks."""
    notes: dict[tuple, dict] = defaultdict(dict)
    for f in res.findings:
        if f.status in ("open", "confirmed"):
            for cell in f.cells:
                notes[cell][f"{f.code} {f.check}"] = None
    row_of = {rid: n for n, rid in enumerate(ds.row_ids)}
    col_of = {c: n + 1 for n, c in enumerate(ds.columns)}             # column 0 is Row_ID
    marks: dict[int, dict] = defaultdict(dict)
    for (rid, col), lines in notes.items():
        if rid in row_of and col in col_of:
            marks[row_of[rid]][col_of[col]] = "\n".join(lines)
    return marks


def _data(wb, ds: Dataset, res: Result) -> None:
    ws = wb.create_sheet("Data")
    ws.freeze_panes = "A2"
    head = ["Row_ID", *ds.columns]
    rows = TableRows(ds)
    for j, h in enumerate(head):
        longest = max((len(str(r[j])) for r in rows[:500] if r[j] is not None), default=0)
        ws.column_dimensions[get_column_letter(j + 1)].width = min(max(len(h), longest) + 2, 35)
    ws.append([_cell(ws, h, HEADER_FILL, WHITE_BOLD, align=CENTRE) for h in head])
    marks = _data_marks(ds, res)
    for n, r in enumerate(rows):
        m = marks.get(n, {})
        ws.append([_cell(ws, v, FLAG_FILL, comment=m[j]) if j in m else _cell(ws, v) for j, v in enumerate(r)])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{len(rows) + 1}"


def export(path: Path, ds: Dataset, res: Result, cfg: dict) -> Path:
    limit = int(cfg["max_sheet_rows"])
    wb = openpyxl.Workbook(write_only=True)
    show_rules = any(r["Status"] in ("INACTIVE", "NOT USED") for r in res.rules)
    for name in SHEETS:
        if name == "Trend Line":
            _trend_line(wb, res.sheets[name], limit)
        elif name == "Data":
            _data(wb, ds, res)
        elif name == "Rules_Used":
            _sheet(wb, name, res.rules, limit, hidden=not show_rules)
        else:
            _sheet(wb, name, res.sheets[name], limit, hidden=name == "Spend_Concentration",
                   hide_cols=("B", "C") if name in CHANGES.values() else ())
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
