"""Import of an edited `Structural_Review.xlsx` (spec 9.2-9.4): checks, patch, cell edits.

`precheck` needs only the workbook (the gateway runs it synchronously); `import_review` also needs the
current Structure output and returns the patch to merge into the `structure` config section.
"""
from __future__ import annotations

import glob
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from pathlib import Path

import openpyxl
import pyarrow.parquet as pq

from .classify import fmt_kind, render_number
from .config import Settings
from .model import parse_ref
from .review_workbook import (
    EDITABLE, FIND_COLS, FIXED_SHEETS, LOCKED, MAX_CELL_CHARS, MAX_UPLOAD_BYTES, META, Violation, hash_file, sha_of,
)


@dataclass
class Review:
    """A parsed upload: its `_meta`, its violations, and the open workbook."""
    meta: dict = field(default_factory=lambda: {"job": {}, "table": [], "export": [], "find": []})
    violations: list = field(default_factory=list)
    workbook: object = None


def _text(v) -> str:
    return "" if v is None else str(v)


def _violation(review: Review, code: str, sheet: str, cell: str, message: str) -> None:
    review.violations.append(Violation(code, sheet, cell, message))


# ------------------------------------------------------------------ checks that need only the workbook


def _open(path: str, review: Review):
    if Path(path).stat().st_size > MAX_UPLOAD_BYTES:
        _violation(review, "review_too_large", "", "", "The workbook is larger than 200 MB.")
        return None
    try:
        return openpyxl.load_workbook(path, data_only=True)
    except Exception:  # noqa: BLE001 - anything unreadable is a broken upload
        _violation(review, "review_base_mismatch", "", "", "The workbook cannot be opened.")
        return None


def _read_meta(wb, review: Review) -> bool:
    if META not in wb.sheetnames:
        _violation(review, "review_base_mismatch", META, "", "The _meta sheet is missing.")
        return False
    lines = [r[0] for r in wb[META].iter_rows(values_only=True) if r[0]]
    try:
        entries = [json.loads(x) for x in lines]
        ok = entries[-1][0] == "hash" and entries[-1][1] == sha_of(*lines[:-1])
    except (ValueError, IndexError, TypeError):
        ok = False
    if not ok:
        _violation(review, "review_base_mismatch", META, "", "The _meta sheet does not match its hash.")
        return False
    for kind, value in entries[:-1]:
        if kind == "job":
            review.meta["job"] = value
        else:
            review.meta[kind].append(value)
    return True


_ROWS = r"\d+(-\d+)?(\s*,\s*\d+(-\d+)?)*"
_COLS = r"[A-Za-z]{1,3}(-[A-Za-z]{1,3})?(\s*,\s*[A-Za-z]{1,3}(-[A-Za-z]{1,3})?)*"


def _valid_edit(name: str, v: str) -> bool:
    if v == "" or name == "Note":
        return True
    if name == "Use":
        return v.upper() in ("Y", "N")
    if name == "Status":
        return v.lower() in ("table", "not a table")
    if name == "Header rows":
        return bool(re.fullmatch(r"none|\d+(-\d+)?", v.lower()))
    if name == "Data range":
        try:
            parse_ref(v)
            return True
        except ValueError:
            return False
    return bool(re.fullmatch(_ROWS if name == "Exclude rows" else _COLS, v))


def _check_findings_sheet(wb, review: Review) -> None:
    ws = wb["Structural_Findings"]
    parquet_rows = {t["row"] for t in review.meta["table"] if t["parquet"]}
    for f in review.meta["find"]:
        row = [_text(ws.cell(f["row"], j).value) for j in range(1, len(FIND_COLS) + 1)]
        locked = row[:len(LOCKED)]
        if f.get("new"):
            locked = locked[:1] + locked[3:]                # File and Sheet are editable on new rows
        where = f"row {f['row']}"
        if sha_of(*locked) != f["locked_hash"]:
            _violation(review, "review_locked_edited", "Structural_Findings", where, "A locked column was changed.")
        for name, value in zip(EDITABLE, (x.strip() for x in row[len(LOCKED):])):
            if not _valid_edit(name, value):
                _violation(review, "review_value_invalid", "Structural_Findings", where, f"{name} cannot be read.")
            elif f["row"] in parquet_rows and name not in ("Use", "Note") and value != f["editable"].get(name, ""):
                _violation(review, "review_locked_edited", "Structural_Findings", where,
                           f"{name} cannot be edited on a Parquet table.")


def added_column_names(first_row: list, columns: list[str]) -> tuple[list[str], str | None]:
    """(names of the columns the analyst pasted to the right of the table, what is wrong with them). A new column needs
    its own header, directly after the last column, that repeats no existing name and does not start with `_`."""
    tail = list(first_row[len(columns) + 1:])
    while tail and (tail[-1] is None or _text(tail[-1]).strip() == ""):
        tail.pop()
    names, taken = [], {c.casefold() for c in ["Row_ID", *columns]}
    for h in tail:
        name = " ".join(_text(h).split())
        if not name:
            return [], "A new column needs a header, with no empty header cell between the columns."
        if name.casefold() in taken or name.startswith("_"):
            return [], f"The new column '{name}' repeats an existing column name or starts with '_'."
        taken.add(name.casefold())
        names.append(name)
    return names, None


def read_added_columns(review: Review, prev_dir: Path) -> dict[str, dict[str, dict[str, str]]]:
    """{table_ref: {new column: {Row_ID: text}}} for the columns pasted into the table sheets (blank cells left out).
    They are not part of Phase A's tables: they are kept beside them and joined on Row_ID when Phase B reads the table."""
    out: dict[str, dict[str, dict[str, str]]] = {}
    for e in review.meta["export"]:
        ws = review.workbook[e["review_sheet"]]
        names, _ = added_column_names(list(next(ws.iter_rows(min_row=1, max_row=1, values_only=True))), e["columns"])
        if not names:
            continue
        ids = pq.read_table(_table_dir(prev_dir, e["table_ref"]) / "data.parquet", columns=["_row_id"]).column(0).to_pylist()
        base = len(e["columns"]) + 2
        out[e["table_ref"]] = {name: {rid: text for i, rid in enumerate(ids[:e["n_rows"]])
                                      if (text := _cell_value(ws.cell(i + 2, base + k))[0]) is not None}
                               for k, name in enumerate(names)}
    return out


def merge_added(existing: dict, new: dict) -> dict:
    """Columns pasted on an earlier upload stay; a column pasted again under the same name replaces the old one."""
    merged = {ref: dict(cols) for ref, cols in existing.items()}
    for ref, cols in new.items():
        merged.setdefault(ref, {}).update(cols)
    return merged


def precheck(path: str) -> Review:
    """What the workbook alone can show (spec 9.4)."""
    review = Review()
    wb = _open(path, review)
    if wb is None or not _read_meta(wb, review):
        return review
    review.workbook = wb
    expected = FIXED_SHEETS | {e["review_sheet"] for e in review.meta["export"]}
    for name in sorted(expected - set(wb.sheetnames)):
        _violation(review, "review_sheet_missing", name, "", "A sheet was removed.")
    for name in sorted(set(wb.sheetnames) - expected):
        _violation(review, "review_sheet_unexpected", name, "", "A sheet was added.")
    for e in review.meta["export"]:
        if e["review_sheet"] in wb.sheetnames:
            ws = wb[e["review_sheet"]]
            first = list(next(ws.iter_rows(min_row=1, max_row=1, values_only=True)))
            known = len(e["columns"]) + 1
            if [_text(x) for x in first[:known]] != ["Row_ID"] + e["columns"]:
                _violation(review, "review_columns_changed", e["review_sheet"], "1",
                           "The header row differs in names, order or count.")
                continue
            names, problem = added_column_names(first, e["columns"])
            if problem:
                _violation(review, "review_columns_changed", e["review_sheet"], "1", problem)
            for k, name in enumerate(names):
                if not any(ws.cell(i, known + 1 + k).value not in (None, "") for i in range(2, e["n_rows"] + 2)):
                    _violation(review, "review_columns_changed", e["review_sheet"], "1",
                               f"The new column '{name}' has no values.")
    if "Structural_Findings" in wb.sheetnames:
        _check_findings_sheet(wb, review)
    return review


# ------------------------------------------------------------------ checks against the current output


def _table_dir(prev_dir: Path, ref: str) -> Path:
    file_key, sheet, key = ref.split(":")
    return prev_dir / "files" / file_key / "tables" / f"s{int(sheet):02d}_{key}"


def _server_checks(review: Review, prev_dir: Path, tables: dict) -> None:
    """Row_IDs and staleness need the table data and the current output (spec 9.3, 9.4)."""
    for e in review.meta["export"]:
        ref, sheet = e["table_ref"], e["review_sheet"]
        data_path = _table_dir(prev_dir, ref) / "data.parquet"
        if ref not in tables or not data_path.exists() or hash_file(data_path) != e["content_hash"]:
            _violation(review, "review_base_stale", sheet, "", "The table changed since the export.")
            continue
        ws = review.workbook[sheet]
        ids = pq.read_table(data_path, columns=["_row_id"]).column(0).to_pylist()[:e["n_rows"]]
        got = [ws.cell(i, 1).value for i in range(2, ws.max_row + 1) if ws.cell(i, 1).value is not None]
        if got != ids:
            _violation(review, "review_row_ids_changed", sheet, "A",
                       "Row_ID values were edited, or rows were added, removed or reordered.")
        extra = any(ws.cell(i, j).value is not None for i in range(e["n_rows"] + 2, ws.max_row + 1)
                    for j in range(1, len(e["columns"]) + 2))
        if extra and len(got) <= e["n_rows"]:
            _violation(review, "review_too_large", sheet, "", "The sheet has more cells than the export had.")
    for t in review.meta["table"]:
        current = tables.get(t["table_ref"])
        if current is None or (not t["parquet"] and current.get("layout_hash") != t["layout_hash"]):
            _violation(review, "review_base_stale", "Structural_Findings", f"row {t['row']}",
                       "The table layout changed since the export.")


# ------------------------------------------------------------------ cell edits


def _cell_value(cell) -> tuple[str | None, str]:
    """(canonical text, type s/n/d/b) of an uploaded cell, by the rendering rules of spec 4.2."""
    v = cell.value
    if v is None or v == "":
        return None, "s"
    if isinstance(v, bool):
        return ("TRUE" if v else "FALSE"), "b"
    if isinstance(v, (int, float)):
        return render_number(float(v)), "n"
    if isinstance(v, datetime):
        kind = fmt_kind(cell.number_format)
        if kind == "date" or (kind != "datetime" and v.time() == time(0)):
            return v.date().isoformat(), "d"
        v = v.replace(microsecond=(v.microsecond // 1000) * 1000)
        return v.isoformat(timespec="milliseconds" if v.microsecond else "seconds"), "d"
    if isinstance(v, date):
        return v.isoformat(), "d"
    if isinstance(v, time):
        return v.strftime("%H:%M:%S"), "d"
    return str(v), "s"


def _same_at_excel_precision(text: str | None, typ: str, current: str | None) -> bool:
    """Excel saves numbers with 15 significant digits, so a value like 0.30000000000000004 comes back as 0.3
    after a plain open-and-save. That is not an analyst's edit."""
    if typ != "n" or text is None or current is None:
        return False
    try:
        return float(f"{float(text):.15g}") == float(f"{float(current):.15g}")
    except ValueError:
        return False


def _cell_edits(review: Review, prev_dir: Path, edited_by: str, upload: str) -> list[dict]:
    """Only a different *rendering* counts as an edit, so opening and saving in Excel changes nothing."""
    edits, stamp = [], datetime.now().isoformat(timespec="seconds")
    for e in review.meta["export"]:
        data = pq.read_table(_table_dir(prev_dir, e["table_ref"]) / "data.parquet")
        ws = review.workbook[e["review_sheet"]]
        ids = data.column("_row_id").to_pylist()[:e["n_rows"]]
        for j, name in enumerate(e["columns"], start=2):
            current = data.column(name).to_pylist()[:e["n_rows"]]
            for i, row_id in enumerate(ids):
                text, typ = _cell_value(ws.cell(i + 2, j))
                if current[i] is not None and len(current[i]) > MAX_CELL_CHARS:
                    continue                                       # cut cells are never edits
                if text != current[i] and not _same_at_excel_precision(text, typ, current[i]):
                    edits.append({"row_id": row_id, "column": name, "from": current[i], "to": text, "to_type": typ,
                                  "edited_by": edited_by, "edited_at": stamp, "review_upload": upload})
    return edits


def merge_edits(existing: list[dict], new: list[dict]) -> list[dict]:
    """A second edit of a cell keeps the file's original value as `from`; editing back drops the edit."""
    merged = {(e["row_id"], e["column"]): e for e in existing}
    for e in new:
        key = (e["row_id"], e["column"])
        if key in merged:
            e = {**e, "from": merged[key]["from"]}
        if e["to"] == e["from"]:
            merged.pop(key, None)
        else:
            merged[key] = e
    return sorted(merged.values(), key=lambda e: (e["row_id"], e["column"]))


# ------------------------------------------------------------------ patch (spec 9.3 step 2)


def _header_rows(text: str) -> list[int]:
    if text.lower() == "none":
        return []
    a, _, b = text.partition("-")
    return list(range(int(a), int(b or a) + 1))


def _selector(entry: dict, sheet_name: str | None, key: str) -> dict:
    """Patterns, not file keys, so a selection survives next month's upload (spec 8.3)."""
    sel = {"file": glob.escape(entry["relative_path"] or "*"), "table": key}
    if sheet_name is not None:
        sel["sheet"] = {"name": sheet_name, "match": "exact"}
    return sel


def _row_values(ws, row: int) -> dict:
    return {name: _text(ws.cell(row, j).value).strip() for j, name in enumerate(FIND_COLS, start=1)}


def build_patch(review: Review, inventory: dict, sheets: dict, st: Settings) -> dict:
    """Findings edits -> selection selectors and overrides. `sheets`: file_key -> sheet facts. Value-free."""
    ws = review.workbook["Structural_Findings"]
    tables = {t["row"]: t for t in review.meta["table"]}
    files = {f["file_key"]: f for f in inventory["files"]}
    by_path = {f["relative_path"]: f for f in inventory["files"]}
    overrides: dict[str, dict] = {}
    notes, new_selectors, use_changed = [], {}, False
    taken = {o["table"] for o in st.overrides}
    for f in review.meta["find"]:
        vals = _row_values(ws, f["row"])
        t = tables.get(f["row"])
        if t is None:                                               # a blank row that defines a new table
            entry = by_path.get(vals["File"])
            sheet = next((x for x in (sheets.get(entry["file_key"], []) if entry else [])
                          if x["name"].strip() == vals["Sheet"]), None)
            if not (vals["File"] or vals["Sheet"] or vals["Data range"]):
                continue
            if entry is None or sheet is None or not vals["Data range"]:
                _violation(review, "review_value_invalid", "Structural_Findings", f"row {f['row']}",
                           "A new table needs a file, a sheet and a data range.")
                continue
            n = 1
            while f"{entry['file_key']}:{sheet['index']}:o{n}" in taken:
                n += 1
            ref = f"{entry['file_key']}:{sheet['index']}:o{n}"
            taken.add(ref)
            overrides[ref] = {"table": ref, "base": None, "status": "table", "range": vals["Data range"],
                              **({"header_rows": _header_rows(vals["Header rows"])} if vals["Header rows"] else {})}
            if vals["Use"].upper() == "Y":
                new_selectors.setdefault(entry["source_id"], []).append(_selector(entry, sheet["name"], f"o{n}"))
            continue
        defaults = f["editable"]
        if vals["Use"] and vals["Use"].upper() != defaults["Use"].upper():
            use_changed = True
        if vals["Note"]:
            notes.append({"table": t["table_ref"], "note": vals["Note"]})
        changed = {k: v for k, v in vals.items() if k in EDITABLE and k not in ("Use", "Note") and v != defaults.get(k, "")}
        if changed and not t["parquet"]:
            ov = {"table": t["table_ref"], "base": t["layout_hash"]}
            if "Status" in changed:
                ov["status"] = "not_table" if changed["Status"].lower() == "not a table" else "table"
            if "Header rows" in changed:
                ov["header_rows"] = _header_rows(changed["Header rows"])
            if "Data range" in changed:
                ov["range"] = changed["Data range"]
            for key, field_name in (("Exclude rows", "exclude_rows"), ("Exclude columns", "exclude_columns")):
                if key in changed:
                    ov[field_name] = [x.strip() for x in changed[key].split(",") if x.strip()]
            overrides[t["table_ref"]] = ov
    patch: dict = {"overrides": sorted(overrides.values(), key=lambda o: o["table"]), "notes": notes}
    if use_changed or new_selectors:
        patch["selection"] = _selection(review, ws, files, sheets, new_selectors)
    return patch


def _selection(review: Review, ws, files: dict, sheets: dict, extra: dict) -> dict:
    """The whole selection of every source that has a table row, rebuilt from the Use column."""
    out: dict[str, list] = {sid: list(v) for sid, v in extra.items()}
    for t in review.meta["table"]:
        file_key, sheet_index, key = t["table_ref"].split(":")
        entry = files[file_key]
        out.setdefault(entry["source_id"], [])
        if _row_values(ws, t["row"])["Use"].upper() == "Y":
            name = None if t["parquet"] else next(s["name"] for s in sheets[file_key] if s["index"] == int(sheet_index))
            out[entry["source_id"]].append(_selector(entry, name, "only" if t["parquet"] else key))
    return out


def apply_patch(st: Settings, patch: dict, cell_edits_ref: dict | None = None) -> Settings:
    """Merge a review patch into the `structure` section (the platform's job on finalizing, spec 9.3.6)."""
    overrides = {o["table"]: o for o in st.overrides}
    overrides.update({o["table"]: o for o in patch.get("overrides", [])})
    return replace(st, overrides=sorted(overrides.values(), key=lambda o: o["table"]),
                   selection={**st.selection, **patch.get("selection", {})},
                   cell_edits_ref=cell_edits_ref or st.cell_edits_ref)


# ------------------------------------------------------------------ entry point


def import_review(path: str, prev_dir: Path, inventory: dict, st: Settings, existing_edits: list[dict],
                  edited_by: str = "analyst", added: dict | None = None) -> tuple[dict, list[dict], list[Violation]]:
    """Full check and patch. Nothing is applied when there are violations (up to 200 are returned).
    `added`, when given, is filled with the columns the analyst pasted next to the tables (see `read_added_columns`)."""
    review = precheck(path)
    if review.violations or review.workbook is None:
        return {}, existing_edits, review.violations
    sheets, tables = {}, {}
    for f in inventory["files"]:
        facts = json.loads((prev_dir / "files" / f["file_key"] / "file.json").read_text(encoding="utf8"))
        sheets[f["file_key"]] = facts["sheets"]
        tables.update({t["table_ref"]: t for s in facts["sheets"] for t in s["tables"]})
        if facts.get("parquet"):
            tables[f"{f['file_key']}:0:t1"] = {"layout_hash": ""}
    _server_checks(review, prev_dir, tables)
    patch = build_patch(review, inventory, sheets, st)
    if review.violations:
        return {}, existing_edits, review.violations[:200]
    new_edits = _cell_edits(review, prev_dir, edited_by, Path(path).name)
    edits = merge_edits(existing_edits, new_edits)
    patch["cell_edits"] = {"count": len(edits), "new": len(new_edits)}
    if added is not None:
        added.update(read_added_columns(review, prev_dir))
    return patch, edits, []
