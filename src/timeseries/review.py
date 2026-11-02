"""Analyst review loop (spec DP-C §6): Data_Validation_Report.xlsx out, edited workbook back in, Phase C re-run.

The analyst may, in the uploaded workbook:
  - set Review to "Valid – no change" on any findings-sheet row: that finding is not reported again (accepted.json);
  - edit values in the Data sheet, add rows (Row_ID left blank: a missing week), delete rows (a duplicate date) or
    reorder them (date order). Every existing row keeps its Row_ID.
The pre-check rejects anything else (sheets, headers, Finding_IDs, unknown or repeated Row_IDs). Phase C then runs again
on the corrected data; every change and every mark is written to review_audit.json."""
from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from pathlib import Path

import openpyxl

from src.common.columns import SYSTEM

from src.common import units
from src.common.logs import run_logger
from src.common.dataset import Dataset, cell_value, dump, read_table
from .report import COLUMNS, REVIEW, SHEETS, VALID
from src.common.values import same_value

from src.common.values import render

REPORT = "Data_Validation_Report.xlsx"


class ReviewError(ValueError):
    """The uploaded workbook does not match what was exported; `problems` lists every mismatch."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:10]))
        self.problems = problems


def keep_version(out: Path, src: Path, kind: str) -> int:
    """Keep each downloaded / uploaded report with the run (Data_Validation_Report.v<n>.<kind>.xlsx); returns n."""
    d = out / "review_versions"
    d.mkdir(exist_ok=True)
    n = len(list(d.glob(f"*.{kind}.xlsx"))) + 1
    shutil.copy(src, d / f"Data_Validation_Report.v{n}.{kind}.xlsx")
    return n


def review_mark(v) -> bool | None:
    """None = blank, True = "Valid – no change" (any dash, case or spacing; "Valid" alone too), False = anything else."""
    if v is None or not str(v).strip():
        return None
    t = " ".join(re.sub("[‐-―−]", "-", str(v)).split()).casefold()
    return t in {"valid - no change", "valid-no change", "valid no change", "valid"}


def _rows(ws) -> list[list]:
    """Every row of a (read-only) sheet, padded to the widest row as a fully loaded sheet would be."""
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    width = max(map(len, rows), default=0)
    return [r + [None] * (width - len(r)) for r in rows]


def exported_ids(findings: list[dict], limit: int) -> dict[str, list[str]]:
    """Finding_IDs each findings sheet was exported with (open findings, in order, at most `limit` a sheet)."""
    out = {s: [] for s in COLUMNS}
    for f in findings:
        if f["status"] == "open" and len(out[f["sheet"]]) < limit:
            out[f["sheet"]].append(f["finding_id"])
    return out


# ── pre-check ────────────────────────────────────────────────────────────────
def precheck(wb, run: dict, findings: list[dict], ds: Dataset) -> list[str]:
    """Everything wrong with an upload, or [] when it can be applied."""
    if sorted(wb.sheetnames) != sorted(SHEETS):
        return [f"sheets must be exactly {SHEETS}, found {wb.sheetnames}"]
    problems = []
    for sheet, ids in exported_ids(findings, run["max_sheet_rows"]).items():
        rows = _rows(wb[sheet])
        cols = COLUMNS[sheet] + REVIEW
        if not rows or list(rows[0][:len(cols)]) != cols:
            problems.append(f"{sheet}: column headers changed (expected {cols})")
            continue
        fi = len(COLUMNS[sheet])
        body = [r for r in rows[1:] if any(v is not None for v in r)]
        if sorted(str(r[fi]) for r in body) != sorted(ids):
            problems.append(f"{sheet}: rows were added or removed, or a Finding_ID was edited "
                            f"({len(ids)} finding(s) exported, {len(body)} row(s) uploaded)")
        for n, r in enumerate(rows[1:], start=2):
            if len(r) > fi + 1 and review_mark(r[fi + 1]) is False:
                problems.append(f"{sheet} row {n}: Review must be blank or '{VALID}', found {r[fi + 1]!r}")
    rows = _rows(wb["Data"])
    if not rows or list(rows[0][:len(ds.columns) + 1]) != ["Row_ID", *ds.columns]:
        return problems + ["Data: column headers changed (columns cannot be added, removed, renamed or moved)"]
    ids = [str(r[0]) for r in rows[1:] if r and r[0] not in (None, "")]
    known = set(ds.row_ids)
    unknown = [i for i in ids if i not in known]
    if unknown:
        problems.append(f"Data: unknown Row_ID(s) {unknown[:5]} - leave Row_ID blank on a new row, keep it on others")
    repeated = [i for i, n in Counter(ids).items() if n > 1]
    if repeated:
        problems.append(f"Data: Row_ID(s) {repeated[:5]} appear more than once (copy a row, then clear its Row_ID)")
    if not any(any(v not in (None, "") for v in r) for r in rows[1:]):
        problems.append("Data: no rows left")
    return problems


# ── apply ────────────────────────────────────────────────────────────────────


def apply_data(ws, ds: Dataset, upload: int, audit: list) -> Dataset:
    """The Data sheet -> the corrected table. Unchanged cells keep their stored value (and type)."""
    cols, rows = ds.columns, _rows(ws)[1:]
    index = {rid: i for i, rid in enumerate(ds.row_ids)}
    system = [c for c in ds.data if c in SYSTEM and c != "_row_id"]
    new = {c: [] for c in ds.data}
    added = max((int(m[1]) for r in ds.row_ids if (m := re.fullmatch(r".*:c(\d+)", r))), default=0)  # rows added before
    kept = []
    for r in rows:
        r = (list(r) + [None] * (len(cols) + 1))[:len(cols) + 1]
        rid, vals = r[0], [cell_value(v) for v in r[1:]]
        if rid in (None, ""):
            if all(v is None for v in vals):
                continue                                      # an empty row
            added += 1
            rid = f"{ds.ref}:c{added}"
            new["_row_id"].append(rid)
            for c in system:
                new[c].append(None)
            for c, v in zip(cols, vals):
                new[c].append(v)
            audit.append({"upload": upload, "action": "row_added", "row_id": rid,
                          "values": {c: render(v) for c, v in zip(cols, vals) if v is not None}})
            continue
        i = index[str(rid)]
        kept.append(i)
        for c in ("_row_id", *system):
            new[c].append(ds.data[c][i])
        for c, v in zip(cols, vals):
            old = ds.data[c][i]
            if same_value(old, v):
                new[c].append(old)
            else:
                new[c].append(v)
                audit.append({"upload": upload, "action": "edited", "row_id": ds.row_ids[i], "column": c,
                              "old_value": render(old), "new_value": render(v)})
    for i in sorted(set(range(len(ds.row_ids))) - set(kept)):
        audit.append({"upload": upload, "action": "row_deleted", "row_id": ds.row_ids[i],
                      "values": {c: render(ds.data[c][i]) for c in cols if ds.data[c][i] is not None}})
    if kept != sorted(kept):
        audit.append({"upload": upload, "action": "rows_reordered", "row_id": None})
    return Dataset(ds.ref, new, ds.raw_header, ds.source_file, ds.sheet, ds.derived)


def marks(wb, findings: list[dict], upload: int, audit: list) -> dict[str, dict]:
    """Findings marked "Valid – no change" in this upload -> accepted.json entries."""
    by_id = {f["finding_id"]: f for f in findings}
    out = {}
    for sheet, cols in COLUMNS.items():
        for r in _rows(wb[sheet])[1:]:
            if len(r) > len(cols) + 1 and review_mark(r[len(cols) + 1]) and str(r[len(cols)]) in by_id:
                f = by_id[str(r[len(cols)])]
                out[f["finding_id"]] = {"code": f["code"], "check": f["check"], "reason": f["reason"], "upload": upload}
                audit.append({"upload": upload, "action": "marked_valid", "finding_id": f["finding_id"],
                              "code": f["code"], "reason": f["reason"]})
    return out


def apply_upload(run_dir, uploaded, cfg: dict) -> dict:
    """Apply an edited Data_Validation_Report.xlsx to a finished run and re-run Phase C on the result.
    Raises ReviewError on a bad upload (nothing is changed then)."""
    from .config import load_client_config
    from .pipeline import CONFIG_COPY, OUT, check_and_save
    if not (Path(run_dir) / OUT / "run.json").exists():
        raise FileNotFoundError(f"{run_dir} is not a Phase C run folder (no {OUT}/run.json): pass the -o folder of 'run'")
    try:
        out = units.unit_for_upload(Path(run_dir) / OUT, Path(uploaded))
    except ValueError as e:
        raise ReviewError([str(e)]) from e
    except Exception as e:  # noqa: BLE001 - not a workbook at all
        raise ReviewError([f"cannot open {uploaded}: {type(e).__name__}: {e}"]) from e
    log = run_logger(Path(run_dir), "dq.timeseries")
    run = json.loads((out / "run.json").read_text("utf8"))
    findings = json.loads((out / "findings.json").read_text("utf8"))
    ds = read_table(out, run)
    try:
        wb = openpyxl.load_workbook(uploaded, read_only=True, data_only=True)    # streamed, never a full workbook
    except Exception as e:
        raise ReviewError([f"cannot open {uploaded}: {type(e).__name__}: {e}"]) from e
    try:
        for ws in wb.worksheets:
            ws.reset_dimensions()                             # trust the cells, not a stale <dimension> tag
        problems = precheck(wb, run, findings, ds)
        if problems:
            log.warning("review upload %s rejected: %s", uploaded, "; ".join(problems[:5]))
            raise ReviewError(problems)
        upload = keep_version(out, Path(uploaded), "uploaded")
        audit: list[dict] = []
        corrected = apply_data(wb["Data"], ds, upload, audit)
        new_marks = marks(wb, findings, upload, audit)
    finally:
        wb.close()
    accepted_path = out / "accepted.json"
    accepted = json.loads(accepted_path.read_text("utf8")) if accepted_path.exists() else {}
    accepted |= new_marks
    dump(accepted_path, accepted)
    audit_path = out / "review_audit.json"
    dump(audit_path, (json.loads(audit_path.read_text("utf8")) if audit_path.exists() else []) + audit)
    log.info("review upload %s applied: %d data change(s), %d finding(s) marked valid", uploaded,
             sum(a["action"] != "marked_valid" for a in audit), sum(a["action"] == "marked_valid" for a in audit))
    cc = load_client_config(out / CONFIG_COPY, cfg)
    return check_and_save(corrected, cc, cfg, out, log, {"input": run["input"], "uploads": upload})
