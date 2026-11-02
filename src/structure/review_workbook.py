"""Analyst review loop (spec 9): export `Structural_Review.xlsx`, check an uploaded copy, turn edits into a patch.

Findings edits become config overrides and selection selectors, cell edits become an edit overlay; both are
applied again on every run (spec 8.4, 8.5). Cell values live only in the workbook and in Parquet files.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

import openpyxl
import pyarrow.parquet as pq
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill, Protection

from .config import Settings
from .findings import spans
from .review_issues import write_changes, write_issues

META = "_meta"
FIXED_SHEETS = {"README", "Structural_Findings", "Issues", "Changes", "Excluded_Cells", META}
MAX_CELL_CHARS = 32_767
MAX_UPLOAD_BYTES = 200 << 20
NEW_ROWS = 20                                   # blank rows at the end of Structural_Findings
EXCLUDED_CELLS_LIMIT = 5_000
GREY = PatternFill("solid", fgColor="D9D9D9")
GREY_ITALIC = Font(italic=True, color="808080")
FIND_COLS = ["Table", "File", "Sheet", "Review sheet", "Detected range", "Detected header rows", "Header candidates",
             "Decided by", "Findings", "Comments", "Use", "Status", "Header rows", "Data range", "Exclude rows",
             "Exclude columns", "Note"]
LOCKED = FIND_COLS[:10]
EDITABLE = FIND_COLS[10:]
ERROR_CODES = {"#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#GETTING_DATA"}
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ESCAPED = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _escape(s: str) -> str:
    """Characters XML cannot hold become `_xHHHH_`; literal escape-looking text is protected (spec 9.1)."""
    s = _ESCAPED.sub(lambda m: "_x005F_x" + m.group(1) + "_", s)
    return _ILLEGAL.sub(lambda m: f"_x{ord(m.group()):04X}_", s)


def sha_of(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


@dataclass
class Violation:
    code: str
    sheet: str
    cell: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "sheet": self.sheet, "cell": self.cell, "message": self.message}


# ------------------------------------------------------------------ export


def _header_text(t: dict) -> str:
    rows = t["group_rows"] + t["header_rows"] + ([t["unit_row"]] if t["unit_row"] else [])
    return spans(rows) if rows else "none"


def _editable_defaults(t: dict, selected: bool) -> dict:
    return {"Use": "Y" if selected else "N", "Status": "table",
            "Header rows": spans(t["header_rows"]).replace(", ", ",") if t["header_rows"] else "none",
            "Data range": t["range"], "Exclude rows": "", "Exclude columns": "", "Note": ""}


def _unlocked(ws, value, protection):
    """A cell the analyst may edit on a protected write-only sheet."""
    cell = WriteOnlyCell(ws, value=value)
    cell.protection = protection
    return cell


def _cells_of(fdir: Path):
    path = fdir / "cells.parquet"
    return pq.read_table(path).to_pylist() if path.exists() else []


def _excel_value(text: str | None, kind: str):
    """(value, number_format) preserving the source type: numbers stay numbers, dates stay dates (spec 9.1)."""
    if text is None:
        return None, None
    try:
        if kind == "n":
            return float(text), "General"
        if kind == "b":
            return text == "TRUE", None
        if kind == "date":
            return date.fromisoformat(text), "yyyy-mm-dd"
        if kind == "datetime":
            return datetime.fromisoformat(text), "yyyy-mm-ddThh:mm:ss.000" if "." in text else "yyyy-mm-ddThh:mm:ss"
        if kind == "time":
            return time.fromisoformat(text), "hh:mm:ss"
    except ValueError:
        pass
    return _escape(text), "@" if re.match(r"^[\d.,+\-eE]+$", text) else None      # `@` keeps `00123` as text


def _write_table_sheet(wb, name: str, t: dict, tdir: Path, cells: list[dict], max_rows: int) -> dict:
    """One selected table: Row_ID in column A, the table's columns after it. Returns its `_meta` entry."""
    data = pq.read_table(tdir / "data.parquet")
    ws = wb.create_sheet(name)
    ws.freeze_panes = "B2"                                        # write-only sheets: set up before the first row
    cols = t["columns"]
    ws.append(["Row_ID"] + [c["name"] for c in cols])
    sheet_index = int(t["table_ref"].split(":")[1])      # cells.parquet holds every sheet: match the sheet too
    exceptions = {(a["row"], a["col"]): json.loads(a["detail"])["type"] for a in cells
                  if a["kind"] == "type_exception" and a["table_key"] == t["key"] and a["sheet_index"] == sheet_index}
    n = min(data.num_rows, max_rows)
    row_ids, source_rows = data.column("_row_id").to_pylist()[:n], data.column("_source_row").to_pylist()[:n]
    hidden = data.column("_row_hidden").to_pylist()[:n]
    columns = {c["name"]: data.column(c["name"]).to_pylist()[:n] for c in cols}
    unlocked = Protection(locked=False)
    for i in range(n):
        row = [row_ids[i]]
        for c in cols:
            text = columns[c["name"]][i]
            dominant = c["source_type"]["dominant"]
            kind = exceptions.get((source_rows[i], c["col"]), dominant)
            cell = WriteOnlyCell(ws)
            if kind == "formula_uncached" or text is None and dominant == "formula_uncached":
                cell.fill = GREY                                  # uncached formula: empty, grey (spec 9.1)
                cell.protection = Protection(locked=True)
            elif text is not None and len(text) > MAX_CELL_CHARS:   # exported cut, greyed, locked, never an edit
                cell.value, cell.fill = _escape(text[:MAX_CELL_CHARS]), GREY
            else:
                value, fmt = _excel_value(text, kind)
                cell.value = value
                if fmt:
                    cell.number_format = fmt
                if kind == "e" and value in ERROR_CODES:        # only a real error literal may be an error cell
                    cell.data_type = "e"
                cell.protection = unlocked
                if hidden[i]:
                    cell.font = GREY_ITALIC
            row.append(cell)
        ws.append(row)
    return {"review_sheet": name, "content_hash": hash_file(tdir / "data.parquet"), "n_rows": n,
            "cut": data.num_rows > n, "columns": [c["name"] for c in cols], "row_ids_hash": sha_of(row_ids)}


def hash_file(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def export_review(out: Path, inventory: dict, processed: dict, per_source: dict, st: Settings, job_id,
                  findings: list[dict]) -> Path:
    """Write review/Structural_Review.xlsx for the finished output (spec §10.1), plus the Issues and Changes
    sheets: every finding with its status (Warning / Fixed / Identified) and every structural change."""
    wb = openpyxl.Workbook(write_only=True)           # rows go to disk as they are written: a lakh-row sheet is not held
    wb.properties.created = wb.properties.modified = datetime(2000, 1, 1)     # same inputs, same bytes (P6)
    readme = wb.create_sheet("README")
    findings_ws = wb.create_sheet("Structural_Findings")
    findings_ws.freeze_panes = "A2"
    findings_ws.append(FIND_COLS)
    by_key = {fr.file_key: fr for fr in processed.values()}
    paths = {f["file_key"]: f["relative_path"] for f in inventory["files"]}
    sheet_names = {(fr.file_key, s["index"]): s["name"] for fr in processed.values() for s in fr.sheets}
    counts = write_issues(wb.create_sheet("Issues"), findings, paths, sheet_names)
    n_changes = write_changes(wb.create_sheet("Changes"), out, inventory, sheet_names)
    selected = {r for v in per_source.values() for r in v[1]["tables"]}
    finds: dict[str, list] = {}
    for _sid, (_frs, _sel, _fams, fs, *_r) in per_source.items():
        for f in fs:
            if f.get("table_ref"):
                finds.setdefault(f["table_ref"], []).append(f"{f['code']}{'.' + f['check'] if f['check'] else ''} ({f['severity']})")
    meta: list[tuple[str, dict]] = [("job", {"job_id": job_id, "config_version": st.version, "schema": "dp.structure.review/1"})]
    n_selected, row_no = 0, 1
    cells_cache: dict = {}
    unlocked = Protection(locked=False)
    for f in inventory["files"]:
        fr = by_key.get(f["file_key"])
        if fr is None or fr.blocked:
            continue
        fdir = out / "files" / f["file_key"]
        cells_cache[f["file_key"]] = cells = _cells_of(fdir)
        tables = [(None, {"table_ref": f"{f['file_key']}:0:t1", "key": "t1", "range": "", "header_rows": [], "group_rows": [],
                          "unit_row": None, "candidates": [], "decided_by": "parquet", "columns": [], "parquet": True})] \
            if fr.parquet is not None else [(s, t) for s in fr.sheets for t in s["tables"]]
        for s, t in tables:
            sel = t["table_ref"] in selected
            comments = "; ".join(f"{a['ref']} {json.loads(a['detail']).get('text', '')}" for a in cells
                                 if a["kind"] == "comment" and a["table_key"] == t["key"]
                                 and s is not None and a["sheet_index"] == s["index"])
            review_sheet = ""
            if sel and not t.get("parquet"):
                n_selected += 1
                review_sheet = f"T{n_selected} {s['key']}"[:31]
                tdir = fdir / "tables" / f"s{s['index']:02d}_{t['key']}"
                entry = _write_table_sheet(wb, review_sheet, t, tdir, cells, st.params["review_max_rows"])
                meta.append(("export", {"table_ref": t["table_ref"], **entry}))
            defaults = _editable_defaults(t, sel) if not t.get("parquet") else {"Use": "Y" if sel else "N", "Status": "", "Header rows": "", "Data range": "", "Exclude rows": "", "Exclude columns": "", "Note": ""}
            locked = [t["table_ref"], f["relative_path"], s["name"] if s else "", review_sheet, t["range"], _header_text(t),
                      " · ".join(f"{r} ({sc})" for r, sc in sorted(t["candidates"], key=lambda x: -x[1])[:3]),
                      t["decided_by"], "; ".join(finds.get(t["table_ref"], [])), comments]
            row_no += 1
            row = locked + [defaults[c] for c in EDITABLE]
            findings_ws.append([_unlocked(findings_ws, v, unlocked) if name in EDITABLE
                                and not (t.get("parquet") and name not in ("Use", "Note")) else v
                                for name, v in zip(FIND_COLS, row)])
            meta.append(("table", {"table_ref": t["table_ref"], "layout_hash": t.get("layout_hash", ""), "row": row_no,
                                   "parquet": bool(t.get("parquet"))}))
            meta.append(("find", {"row": row_no, "locked_hash": sha_of(*[str(x) for x in locked]), "editable": defaults}))
    for _ in range(NEW_ROWS):                                         # blank rows define new tables
        row_no += 1
        findings_ws.append([_unlocked(findings_ws, None, unlocked) if name in EDITABLE or name in ("File", "Sheet") else None
                            for name in FIND_COLS])
        meta.append(("find", {"row": row_no, "locked_hash": sha_of(*[""] * (len(LOCKED) - 2)), "editable": {}, "new": True}))
    excl = wb.create_sheet("Excluded_Cells")
    excl.append(["File", "Sheet", "Ref", "Kind", "Value"])
    shown, seen = 0, set()
    for f in inventory["files"]:
        if f["file_key"] in seen:
            continue
        seen.add(f["file_key"])
        for a in cells_cache.get(f["file_key"], []):
            if a["category"] == "placement" and shown < EXCLUDED_CELLS_LIMIT:
                excl.append([f["relative_path"], sheet_names.get((f["file_key"], a["sheet_index"]), a["sheet_index"]),
                             a["ref"], a["kind"], _escape(a["value"] or "")])
                shown += 1
    for line in ("Structural_Review.xlsx: download, edit, upload.", f"Written by job {job_id or 'local run'}, config version {st.version if st.version is not None else '-'}.",
                 f"Issues: {counts['Warning']} warning(s) to look at, {counts['Fixed']} fixed, "
                 f"{counts['Identified']} identified. Changes: {n_changes} structural change(s).",
                 "Issues lists every structural issue found. Status Warning = needs a look; Fixed = Phase A changed the "
                 "table to resolve it (Changes shows exactly what); Identified = reported, data left as stored.",
                 "The table sheets (T1, T2, ...) hold the fixed tables, one per selected table, Row_ID in column A.",
                 "Edit only the Use, Status, Header rows, Data range, Exclude rows, Exclude columns and Note columns of "
                 "Structural_Findings, and cells of the table sheets. Blank rows at the end define new tables. "
                 "Issues, Changes and Excluded_Cells are read-only.",
                 "To add a column of your own, type a new header directly after "
                 "the last column and fill the values on the same rows. Phase B joins it to the table on Row_ID and "
                 "checks it. Headers must be new, and the existing header row must stay as it is.",
                 "Tables larger than the review row limit are exported up to that row."):
        readme.append([line])
    ms = wb.create_sheet(META)
    ms.sheet_state = "hidden"
    lines = [json.dumps([k, v], sort_keys=True) for k, v in meta]
    for line in lines:
        ms.append([line])
    ms.append([json.dumps(["hash", sha_of(*lines)])])
    path = out / "review" / "Structural_Review.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_deterministic(wb, path)
    return path


def save_deterministic(wb, path: Path) -> None:
    """Save with a fixed timestamp on every zip entry: openpyxl stamps the current time, so two saves of the
    same workbook differ whenever they straddle a clock second (P6: same inputs, same bytes)."""
    import io
    import zipfile
    buf = io.BytesIO()
    wb.save(buf)
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    with src, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            fixed = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            fixed.compress_type, fixed.external_attr = zipfile.ZIP_DEFLATED, info.external_attr
            data = src.read(info.filename)
            if info.filename == "docProps/core.xml":     # openpyxl sets `modified` to now on every save
                data = re.sub(rb"(<dcterms:modified[^>]*>)[^<]*", rb"\g<1>2000-01-01T00:00:00Z", data)
            dst.writestr(fixed, data)
