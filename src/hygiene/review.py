"""Analyst review loop: Hygiene_Review.xlsx out, edited workbook back in (download -> edit -> upload).

Workbook sheets: Summary (fixed / to-review counts per table and rule), Needs_Review (Decision / New_Value are
editable), Auto_Fix_Log (every change with its reason, read-only), Tables (sheet index), and one Data_<n> sheet per
table: values may be edited directly; changed cells are green, cells waiting for a decision yellow, each with a comment
saying what was done. Every exported and uploaded version is kept."""
from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import openpyxl
import polars as pl
from openpyxl.cell import WriteOnlyCell
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from src.common.columns import SYSTEM
from src.common.logs import run_logger

from .config import ALIASES, RULE_TEXT
from .table import Table
from .values import ERROR_RE, parse_date, parse_number

# Needs_Review columns: the first READ_ONLY come from the run and must come back unchanged; the analyst fills the last two.
NEEDS = ["Row_ID", "Excel_Row", "Table", "Column", "Rule", "Current_Value", "Why_Flagged", "Suggested_Action",
         "Decision", "New_Value"]
READ_ONLY = 8
FIXLOG = ["Row_ID", "Excel_Row", "Table", "Column", "Rule", "Action", "Old_Value", "New_Value", "Why"]
SUMMARY = ["Table", "Source_File", "Sheet", "Rule", "Also", "What_It_Does", "Fixed", "Needs_Review"]
SHEETS = ["Summary", "Needs_Review", "Auto_Fix_Log", "Tables"]       # then one Data_<n> per table
FIXED_FILL = PatternFill("solid", fgColor="C6EFCE")                  # green: changed by the pipeline
REVIEW_FILL = PatternFill("solid", fgColor="FFEB9C")                 # yellow: waiting for a decision


def _fixed_part(item: dict) -> list:
    """The read-only cells of one Needs_Review row, in NEEDS order."""
    return [item["row_id"], item.get("source_row"), item["table_ref"], item["column"], item["rule"],
            item["current_value"], item["why_flagged"], item["suggested_action"]]
DECISIONS = {"Accept", "Reject", "New Value", "Split"}   # Split: only on a D-29 text item (one row per value)


class ReviewError(ValueError):
    """The uploaded workbook does not match what was exported; `problems` lists every mismatch."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:10]))
        self.problems = problems


def render(v) -> str | None:
    """One text form for a cell value, so an edit is detected by comparing renderings."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _keep_version(out: Path, src: Path, kind: str) -> None:
    """Keep each downloaded/uploaded workbook with the run (Hygiene_Review.v<n>.<kind>.xlsx)."""
    d = out / "review_versions"
    d.mkdir(exist_ok=True)
    n = len(list(d.glob(f"*.{kind}.xlsx"))) + 1
    shutil.copy(src, d / f"Hygiene_Review.v{n}.{kind}.xlsx")


# ── export ───────────────────────────────────────────────────────────────────
def _short(v, n: int = 60) -> str:
    s = "NULL" if v is None else repr(v)
    return s if len(s) <= n else s[: n - 3] + "..."


def _summary_sheet(ws, tables: list[Table]) -> None:
    """One row per table and rule: what the rule does, how many cells it fixed, how many items wait for a decision."""
    ws.append(_styled(ws, SUMMARY, bold=True))
    for t in tables:
        open_items = Counter(r["rule"] for r in t.review if r["status"] == "open")
        fixed = Counter(a["rule"] for a in t.audit)
        for code in sorted(set(fixed) | set(open_items)):
            ws.append([t.ref, t.source_file, t.sheet, code, ", ".join(ALIASES.get(code, [])), RULE_TEXT.get(code, ""),
                       fixed[code], open_items[code]])
    ws.append([])
    for t in tables:                                            # the analyst's "zero is a valid value" choice, never changed by D-27
        zero_ok = (t.cfg.get("zero_expected") or {}).get(t.ref)
        if zero_ok:
            ws.append(["Zero is a valid value (not filled by D-27)", t.ref, ", ".join(zero_ok)])
    ws.append(["Legend", "Data_<n> sheets: green cell = changed by the pipeline, yellow cell = waiting for a decision "
               "on Needs_Review. Hover a coloured cell for what was done and why."])
    ws.append(["Add a column", "On a Data_<n> sheet, type a new header directly after the last column and fill its values on "
               "the same rows (do not change the rows or Row_IDs). On upload the column is added to the table and checked "
               "with the cell rules; what they change or flag shows on Needs_Review."])


def _data_marks(t: Table) -> dict[int, dict[int, tuple[PatternFill, Comment]]]:
    """Excel row -> column -> (fill, comment) for every fixed / flagged cell of a Data sheet: what was done and why."""
    row_of = {rid: n + 2 for n, rid in enumerate(t.data["_row_id"])}       # Excel row of each Row_ID
    col_of = {c: n + 3 for n, c in enumerate(t.cols)}                       # columns 1-2: _row_id, _excel_row
    notes: dict[tuple[int, int], list[str]] = defaultdict(list)
    review_cells = set()
    for a in t.audit:
        r = 1 if a["row_id"] is None else row_of.get(a["row_id"])         # column-level -> header cell
        if r and a["column"] in col_of:
            why = a.get("note") or RULE_TEXT.get(a["rule"], "")
            notes[(r, col_of[a["column"]])].append(f"{a['rule']}: {_short(a['old_value'])} -> {_short(a['new_value'])}. {why}")
    for i in (i for i in t.review if i["status"] == "open"):
        r = 1 if i["row_id"] is None else row_of.get(i["row_id"])
        if r and i["column"] in col_of:
            review_cells.add((r, col_of[i["column"]]))
            notes[(r, col_of[i["column"]])].append(f"{i['rule']} needs review: {i['why_flagged']}")
    marks: dict[int, dict[int, tuple[PatternFill, Comment]]] = defaultdict(dict)
    for (r, c), lines in notes.items():
        text = "\n".join(lines[:6]) + (f"\n... {len(lines) - 6} more" if len(lines) > 6 else "")
        marks[r][c] = (REVIEW_FILL if (r, c) in review_cells else FIXED_FILL,
                       Comment(text, "Phase B", width=320, height=min(60 + 30 * len(lines[:6]), 240)))
    return marks


def _styled(ws, values: list, marks: dict[int, tuple[PatternFill, Comment]] | None = None, bold: bool = False) -> list:
    """One row for a write-only sheet: plain values, except the cells that carry a font, fill or comment."""
    if not bold and not marks:
        return values
    row = []
    for c, v in enumerate(values, start=1):
        mark = marks.get(c) if marks else None
        if not bold and mark is None:
            row.append(v)
            continue
        cell = WriteOnlyCell(ws, value=v)
        if bold:
            cell.font = Font(bold=True)
        if mark:
            cell.fill, cell.comment = mark
        row.append(cell)
    return row


def _sheet(wb, title: str):
    ws = wb.create_sheet(title)
    ws.freeze_panes = "A2"                                      # bold, frozen header row on every sheet
    return ws


def export(out: Path, tables: list[Table]) -> Path:
    """Write Hygiene_Review.xlsx: Summary, Needs_Review, Auto_Fix_Log, Tables and one Data_<n> sheet per table.
    Written in write-only mode: rows go to disk as they are appended, so a lakh-row table never sits in openpyxl."""
    from .io import typed_frame
    wb = openpyxl.Workbook(write_only=True)
    summary = _sheet(wb, "Summary")
    _summary_sheet(summary, tables)

    ws = _sheet(wb, "Needs_Review")
    ws.append(_styled(ws, NEEDS, bold=True))
    n_open = 0
    for r in (i for t in tables for i in t.review if i["status"] == "open"):
        ws.append([*_fixed_part(r), None, None])
        n_open += 1
    dv = DataValidation(type="list", formula1='"Accept,Reject,New Value,Split"', allow_blank=True)
    ws.data_validations.append(dv)
    dv.add(f"I2:I{max(n_open + 1, 2)}")                        # the Decision column

    log = _sheet(wb, "Auto_Fix_Log")
    log.append(_styled(log, FIXLOG, bold=True))
    for a in (a for t in tables for a in t.audit):
        log.append([a["row_id"], a.get("source_row"), a["table_ref"], a["column"], a["rule"], a["action"],
                    a["old_value"], a["new_value"], a.get("note") or RULE_TEXT.get(a["rule"], "")])

    index = _sheet(wb, "Tables")
    index.append(_styled(index, ["Sheet", "Table", "Source_File", "Source_Sheet"], bold=True))
    for n, t in enumerate(tables, 1):
        index.append([f"Data_{n}", t.ref, t.source_file, t.sheet])
        sheet = _sheet(wb, f"Data_{n}")
        marks = _data_marks(t)
        sheet.append(_styled(sheet, ["_row_id", "_excel_row", *t.cols], marks.get(1), bold=True))   # _excel_row: the row in the input file
        df = typed_frame(t)
        for r, row in enumerate(df.select(["_row_id", "_source_row", *t.cols]).iter_rows(), start=2):
            sheet.append(_styled(sheet, [v.isoformat() if isinstance(v, date) else v for v in row], marks.get(r)))
    path = out / "Hygiene_Review.xlsx"
    wb.save(path)
    _keep_version(out, path, "downloaded")
    return path


# ── pre-check ────────────────────────────────────────────────────────────────
def _iter_rows(ws, width: int):
    """Rows of a read-only sheet, each padded or cut to `width` (a read-only row ends at its last filled cell)."""
    for r in ws.iter_rows(values_only=True):
        yield (list(r) + [None] * width)[:width]


def _rows(ws, width: int) -> list[list]:
    return list(_iter_rows(ws, width))


def _count_rows(ws) -> int:
    return sum(1 for _ in ws.iter_rows(values_only=True))


def precheck(wb, tables: list[Table]) -> list[str]:
    """The upload must have the same sheets, headers, rows and Row_IDs; only Decision/New_Value (and data values) may change."""
    problems = []
    want = [*SHEETS, *[f"Data_{n}" for n in range(1, len(tables) + 1)]]
    if wb.sheetnames != want:
        return [f"sheets must be exactly {want}, found {wb.sheetnames}"]
    open_items = [r for t in tables for r in t.review if r["status"] == "open"]
    rows = _rows(wb["Needs_Review"], len(NEEDS)) or [[]]
    if rows[0][:len(NEEDS)] != NEEDS:
        problems.append("Needs_Review: column headers changed")
    elif len(rows) - 1 != len(open_items):
        problems.append(f"Needs_Review: expected {len(open_items)} rows, found {len(rows) - 1}")
    else:
        for n, (row, item) in enumerate(zip(rows[1:], open_items), start=2):
            if [render(v) for v in row[:READ_ONLY]] != [render(v) for v in _fixed_part(item)]:
                problems.append(f"Needs_Review row {n}: a read-only cell was edited or the row was moved")
            decision, new = row[READ_ONLY], render(row[READ_ONLY + 1])
            if decision not in (None, *DECISIONS):
                problems.append(f"Needs_Review row {n}: Decision must be Accept, Reject, New Value or Split")
            if decision == "Split" and not (item["rule"] == "D-29" and item.get("parts") and item["scope"] == "cell"):
                problems.append(f"Needs_Review row {n}: Split only applies to a cell that holds a list of text values (rule D-29)")
            if decision == "New Value" and (new is None or item["scope"] == "column"):
                problems.append(f"Needs_Review row {n}: New Value needs a value and only applies to cell-level items")
    split_rows_seen = Counter(item["row_id"] for item, row in zip(open_items, rows[1:]) if row[READ_ONLY] == "Split")
    problems += [f"Needs_Review: row {rid} is split on more than one column; split one column at a time"
                 for rid, n in split_rows_seen.items() if n > 1]
    audit = [a for t in tables for a in t.audit]
    if _count_rows(wb["Auto_Fix_Log"]) - 1 != len(audit):
        problems.append("Auto_Fix_Log: rows added or removed")
    for n, t in enumerate(tables, 1):
        header = ["_row_id", "_excel_row", *t.cols]
        extra, bad = new_headers(wb[f"Data_{n}"], t)                       # columns pasted to the right of the table
        problems += [f"Data_{n}: {p}" for p in bad]
        if bad:
            continue
        rows = _iter_rows(wb[f"Data_{n}"], len(header) + len(extra) + 1)  # one extra cell shows a header that was skipped
        if next(rows, None) != header + extra + [None]:
            problems.append(f"Data_{n}: column headers changed")
            continue
        filled = [False] * len(extra)
        ids = []
        for r in rows:
            ids.append(r[0])
            for k in range(len(extra)):
                filled[k] = filled[k] or render(r[len(header) + k]) is not None
        if ids != t.data["_row_id"]:
            problems.append(f"Data_{n}: rows were added, removed or reordered (Row_ID must be unchanged)")
        problems += [f"Data_{n}: the new column '{h}' has no values" for h, f in zip(extra, filled) if not f]
    return problems


def new_headers(ws, t: Table) -> tuple[list[str], list[str]]:
    """(names of the columns the analyst pasted to the right of the table, problems with them). A new column needs a unique
    header, directly after the last column, and a value for the rows it applies to (rows may stay blank)."""
    known = len(t.cols) + 2
    first = next(ws.iter_rows(values_only=True, max_row=1), ())
    tail = list(first[known:])
    while tail and (tail[-1] is None or str(tail[-1]).strip() == ""):
        tail.pop()
    names, bad = [], []
    taken = {c.casefold() for c in (*t.cols, *SYSTEM)}
    for k, h in enumerate(tail, start=1):
        name = None if h is None else " ".join(str(h).split())
        if not name:
            bad.append(f"a new column needs a header (header cell {k} after the table is empty)")
        elif name.casefold() in taken:
            bad.append(f"the new column '{name}' repeats an existing column name")
        else:
            taken.add(name.casefold())
            names.append(name)
    return names, bad


# ── import ───────────────────────────────────────────────────────────────────
def load_clean_tables(out: Path, cfg: dict) -> list[Table]:
    """Rebuild the tables of a finished run from its typed parquet, report, audit and review files."""
    audit = json.loads((out / "remediation_audit.json").read_text("utf8"))
    needs = json.loads((out / "needs_review.json").read_text("utf8"))
    tables = []
    for ref in json.loads((out / "run.json").read_text("utf8"))["tables"]:      # same order as the exported Data_<n> sheets
        stem = ref.replace(":", "_")
        rep = json.loads((out / "quality_reports" / f"{stem}.json").read_text("utf8"))
        df = pl.read_parquet(out / "tables" / stem / "data.parquet")
        data = {c: df[c].to_list() if c in SYSTEM else [render(v) for v in df[c].to_list()] for c in df.columns}
        t = Table(ref, data, cfg, rep.get("layout_id"), rep.get("source_file"), rep.get("sheet"))
        t.rows_in = rep["rows_in"]
        t.types = {c: v["type"] for c, v in rep["columns"].items()}
        t.audit = [a for a in audit if a["table_ref"] == t.ref]
        t.review = [r for r in needs if r["table_ref"] == t.ref]
        t.text_cols = {a["column"] for a in t.audit if a["rule"] == "D-21" and a["action"] == "fixed"}
        t.hold_open_multi_values()                                    # cells D-29 flagged and nobody has answered yet
        t.counts.update({k: {"fixed": v["fixed"], "flagged": v["flagged"]} for k, v in rep["counts_by_rule"].items()})
        tables.append(t)
    return tables


def _edit(t: Table, col: str, i: int, new, why: str) -> None:
    old = t.data[col][i]
    if new != old:
        t.data[col][i] = new
        t.audit.append(t._entry("analyst", col, i, action="analyst_edit", old_value=old, new_value=new, note=why))


def split_rows(t: Table, splits: dict[int, tuple[str, list[str]]]) -> None:
    """Decision Split on a D-29 item: the row becomes one row per value of the cell. The other columns are copied to every new
    row, so a number on the row is repeated (the audit says so). New Row_IDs are <old Row_ID>:s1, :s2 ...; the Excel
    row (_source_row) stays the same, so each new row still leads back to its source row."""
    if not splits:
        return
    n = len(t.data["_row_id"])
    new = {c: [] for c in t.data}
    renamed = {}
    for i in range(n):
        if i not in splits:
            for c in t.data:
                new[c].append(t.data[c][i])
            continue
        col, parts = splits[i]
        old_id, old_value = t.data["_row_id"][i], t.data[col][i]
        for k, part in enumerate(parts, start=1):
            for c in t.data:
                new[c].append(part if c == col else f"{old_id}:s{k}" if c == "_row_id" else t.data[c][i])
            t.audit.append(t._entry("D-29", col, None, action="split", old_value=old_value, new_value=part,
                                    note=f"Row {old_id} split into {len(parts)} rows by the analyst; the row's other values are "
                                         "repeated on each new row (check totals before using them)."))
            t.audit[-1]["row_id"], t.audit[-1]["source_row"] = f"{old_id}:s{k}", t.data["_source_row"][i]
            t.counts["D-29"]["fixed"] += 1
        renamed[old_id] = f"{old_id}:s1"
    t.data = new
    for r in t.review:                                            # open items of a split row follow its first new row
        if r["status"] == "open" and r["row_id"] in renamed:
            r["row_id"] = renamed[r["row_id"]]


def add_columns(t: Table, new: dict[str, list]) -> None:
    """Append columns the analyst pasted in and check them with the Phase B cell rules, exactly as if they had been in the
    table from the start. The rules see only the new columns (the table's text columns still split a long table into
    series for the outlier check). Rules that look across the whole table are not repeated: D-19 (duplicate rows), D-27
    (filling zeros) and D-28 (columns that add up), because they could drop rows or change columns the analyst already
    reviewed, and D-32 (expected columns), which judges the whole sheet and would flag its missing columns again."""
    from .rules import RULES, additive_columns, duplicates, expected_columns, isolated_gaps
    skip = {additive_columns, duplicates, isolated_gaps, expected_columns}
    sub = Table(t.ref, {**{c: t.data[c] for c in SYSTEM if c in t.data}, **{c: t.data[c] for c in t.cols}, **new},
                t.cfg, t.layout_id, t.source_file, t.sheet)
    sub.context_cols = list(t.cols)
    sub.cols = list(new)                                              # only these are checked
    for rule in RULES:
        if rule not in skip:
            rule(sub)
    for c in sub.cols:                                                # D-11 may have trimmed a header
        t.data[c] = sub.data[c]
        t.cols.append(c)
        t.types[c] = sub.kind(c)
        t.audit.append(t._entry("analyst", c, None, action="column_added", old_value=None,
                                new_value=f"{sum(v is not None for v in sub.data[c])} value(s)",
                                note="Column added by the analyst in the review workbook and checked with the Phase B rules."))
    t.audit += sub.audit
    t.review += sub.review
    t.text_cols |= sub.text_cols
    for code, n in sub.counts.items():
        t.counts[code].update(n)


def recheck(t: Table, cells: set[tuple[str, int]]) -> None:
    """Re-validate only the changed cells against their column's type, with the same fixes the run applies."""
    c = t.cfg
    for col, i in sorted(cells, key=lambda x: (x[1], x[0])):
        v = t.data[col][i]
        if v is None:
            continue
        clean = " ".join(v.split())
        if not clean or ERROR_RE.match(clean) or clean.casefold() in c["null_tokens"]:
            t.set(col, i, None, "D-18" if clean else "N-02")
            continue
        t.set(col, i, clean, "D-09")
        kind = t.types.get(col)
        if kind == "numeric":
            n = parse_number(clean, c["currency_symbols"], t.eu(col))
            if n is None:
                t.set(col, i, None, "D-04", f"'{clean}' is not a number in a numeric column.", review=True)
            else:
                t.set(col, i, n, "E-01")
        elif kind == "date":
            status, ymd, shape = parse_date(clean, *t.date_args(col))
            if status == "none" or status.startswith("bad"):
                t.set(col, i, None, "D-02", f"'{clean}' is not a valid date.", review=True)


def apply_upload(run_dir: Path, uploaded: Path, cfg: dict) -> dict:
    """Apply an edited Hygiene_Review.xlsx to a finished run and rewrite the run's outputs. Raises ReviewError on a bad upload."""
    from .pipeline import OUT, save
    out = Path(run_dir) / OUT
    if not (out / "run.json").exists():
        raise FileNotFoundError(f"{run_dir} is not a Phase B run folder (no {OUT}/run.json): pass the -o folder of 'run'")
    log = run_logger(Path(run_dir), "dq.hygiene")
    tables = load_clean_tables(out, cfg)
    wb = openpyxl.load_workbook(uploaded, read_only=True, data_only=True)     # streamed: never the whole workbook in memory
    try:
        for ws in wb.worksheets:
            ws.reset_dimensions()                             # trust the cells, not a stale <dimension> tag
        problems = precheck(wb, tables)
        if problems:
            log.warning("review upload %s rejected: %s", uploaded, "; ".join(problems[:5]))
            raise ReviewError(problems)
        _keep_version(out, Path(uploaded), "uploaded")
        changed: dict[str, set] = {t.ref: set() for t in tables}
        added: dict[str, dict[str, list]] = {}                     # columns pasted into the Data sheets
        # 1. values edited directly in the Data sheets
        for n, t in enumerate(tables, 1):
            extra, _ = new_headers(wb[f"Data_{n}"], t)
            rows = _iter_rows(wb[f"Data_{n}"], len(t.cols) + 2 + len(extra))
            next(rows)
            fresh = {h: [] for h in extra}
            for i, row in enumerate(rows):
                for col, v in zip(t.cols, row[2:]):
                    if render(v) != t.data[col][i]:
                        _edit(t, col, i, render(v), "edited in Data sheet")
                        changed[t.ref].add((col, i))
                for h, v in zip(extra, row[len(t.cols) + 2:]):
                    fresh[h].append(render(v))
            if fresh:
                added[t.ref] = fresh
        decisions = _rows(wb["Needs_Review"], len(NEEDS))[1:]
    finally:
        wb.close()
    by_ref = {t.ref: t for t in tables}
    index = {t.ref: {rid: i for i, rid in enumerate(t.data["_row_id"])} for t in tables}
    # 2. decisions on review items
    splits: dict[str, dict[int, tuple[str, list[str]]]] = {}
    open_items = [r for t in tables for r in t.review if r["status"] == "open"]
    for item, row in zip(open_items, decisions):
        decision, new = row[READ_ONLY], render(row[READ_ONLY + 1])
        if decision is None:
            continue
        t = by_ref[item["table_ref"]]
        col = item["column"]
        i = index[t.ref].get(item["row_id"])
        if decision == "Split" and i is not None and col is not None:
            splits.setdefault(t.ref, {})[i] = (col, item["parts"])
        elif item.get("scope") == "value" and col is not None:          # D-30: one answer for every cell with this spelling
            answer = new if decision == "New Value" else item["proposed_value"] if decision == "Accept" else None
            if answer is not None:
                for j, v in enumerate(t.data[col]):
                    if v == item["current_value"]:
                        _edit(t, col, j, answer, "analyst answer for every cell with this value")
                        changed[t.ref].add((col, j))
        elif i is not None and col is not None:
            if decision == "New Value":
                _edit(t, col, i, new, "analyst new value")
                changed[t.ref].add((col, i))
            elif decision == "Accept" and item["proposed_value"] is not None:
                _edit(t, col, i, item["proposed_value"], "analyst accepted the proposed fix")
                changed[t.ref].add((col, i))
            elif decision == "Reject" and item["original_value"] is not None and t.data[col][i] == item["current_value"]:
                _edit(t, col, i, item["original_value"], "analyst rejected the automatic fix")   # kept as is: not re-checked
        item["status"], item["decision"] = "resolved", decision
    # 3. re-check only what changed
    for t in tables:
        recheck(t, changed[t.ref])
        if t.ref in added:
            add_columns(t, added[t.ref])                          # new columns get the full Phase B cell rules
        split_rows(t, splits.get(t.ref, {}))                      # last: rows change only after every cell edit is checked
        t.held.clear()
        t.hold_open_multi_values()                                # answered items are no longer held: the column can be typed again
    log.info("review upload %s applied: %d cell(s) changed and re-checked, %d column(s) added", uploaded,
             sum(map(len, changed.values())), sum(map(len, added.values())))
    run = json.loads((out / "run.json").read_text("utf8"))
    return save(Path(run_dir), tables, run["unreadable"], cfg, run.get("skipped", []))
