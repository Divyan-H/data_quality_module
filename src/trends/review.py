"""Analyst review loop (workflow document, Phase D): Spend_Support_Trend_Report.xlsx out, edited workbook back in,
Phase D re-run on the uploaded data.

The analyst may, in the uploaded workbook:
  - set Review on a row with a Finding_ID: "Confirmed Issue" (a real data problem, for the client report),
    "Valid Movement" (a genuine business movement, not a data problem) or "Corrected" (fixed on the Data sheet).
    Marks are kept in review.json and hold for later runs; clearing a pre-filled mark removes it;
  - correct the Data sheet as in Phase C: edit values, add rows (Row_ID blank), delete or reorder rows; every existing
    row keeps its Row_ID.
The pre-check rejects anything else (sheets, headers, Finding_IDs, Review values, unknown or repeated Row_IDs). Phase D
then runs again on the corrected data: a finding marked Corrected that is no longer flagged is kept in findings.json as
"corrected"; one still flagged is open again. Every change and every mark is written to review_audit.json."""
from __future__ import annotations

import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl

from src.common import units
from src.common.logs import run_logger
from src.common.columns import SYSTEM
from src.common.dataset import Dataset, cell_value, dump, read_table
from .report import COLUMNS, SHEETS, STATUSES, header
from src.common.values import same_value

from src.common.values import render

REPORT = "Spend_Support_Trend_Report.xlsx"
REVIEWS = "review.json"
AUDIT = "review_audit.json"
_ALIASES = {"confirmed issue": "Confirmed Issue", "confirmed": "Confirmed Issue", "valid movement": "Valid Movement",
            "valid": "Valid Movement", "corrected": "Corrected"}


class ReviewError(ValueError):
    """The uploaded workbook does not match what was exported; `problems` lists every mismatch."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:10]))
        self.problems = problems


def keep_version(out: Path, src: Path, kind: str) -> int:
    """Keep each downloaded / uploaded report with the run (Spend_Support_Trend_Report.v<n>.<kind>.xlsx); returns n."""
    d = out / "review_versions"
    d.mkdir(exist_ok=True)
    n = len(list(d.glob(f"*.{kind}.xlsx"))) + 1
    shutil.copy(src, d / f"{Path(REPORT).stem}.v{n}.{kind}.xlsx")
    return n


def review_value(v) -> str | None | bool:
    """None = blank, one of STATUSES (any case, spacing or dash; "Valid" and "Confirmed" alone too), False = other."""
    if v is None or not str(v).strip():
        return None
    t = " ".join(re.sub(r"[‐-―−_-]", " ", str(v)).split()).casefold()
    return _ALIASES.get(t, False)


def read_json(path: Path, default):
    return json.loads(path.read_text("utf8")) if path.exists() else default


def _rows(ws) -> list[list]:
    """Every row of a (read-only) sheet, padded to the widest row as a fully loaded sheet would be."""
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    width = max(map(len, rows), default=0)
    return [r + [None] * (width - len(r)) for r in rows]


def exported_ids(findings: list[dict]) -> dict[str, list[str]]:
    """Finding_IDs each review sheet was exported with."""
    out = defaultdict(list)
    for f in findings:
        if f["listed"] and f["status"] != "corrected":
            out[f["sheet"]].append(f["finding_id"])
    return out


# ── pre-check ────────────────────────────────────────────────────────────────
def precheck(wb, findings: list[dict], ds) -> list[str]:
    """Everything wrong with an upload, or [] when it can be applied."""
    if sorted(wb.sheetnames) != sorted(SHEETS):
        return [f"sheets must be exactly {SHEETS}, found {wb.sheetnames}"]
    problems = []
    exported = exported_ids(findings)
    for sheet in COLUMNS:
        rows, head = _rows(wb[sheet]), header(sheet)
        if not rows or list(rows[0][:len(head)]) != head:
            problems.append(f"{sheet}: column headers changed (expected {[h or '' for h in head]})")
            continue
        fi = len(head) - 2
        body = [(r + [None, None])[:len(head)] for r in rows[1:]]
        ids = [str(r[fi]) for r in body if r[fi] not in (None, "")]
        if sorted(ids) != sorted(exported[sheet]):
            problems.append(f"{sheet}: rows with a Finding_ID were added or removed, or a Finding_ID was edited "
                            f"({len(exported[sheet])} finding(s) exported, {len(ids)} uploaded)")
        for n, r in enumerate(body, start=2):
            mark = review_value(r[fi + 1])
            if mark is False:
                problems.append(f"{sheet} row {n}: Review must be blank or one of {STATUSES}, found {r[fi + 1]!r}")
            elif mark and r[fi] in (None, ""):
                problems.append(f"{sheet} row {n}: Review is only for rows with a Finding_ID (flagged rows)")
    return problems + data_problems(wb["Data"], ds)


def data_problems(ws, ds: Dataset) -> list[str]:
    """Everything wrong with the uploaded Data sheet (Row_ID + the checked table's columns), or []."""
    rows = _rows(ws)
    if not rows or list(rows[0][:len(ds.columns) + 1]) != ["Row_ID", *ds.columns]:
        return ["Data: column headers changed (columns cannot be added, removed, renamed or moved)"]
    problems = []
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
    """The Data sheet -> the corrected table. Unchanged cells keep their stored value (and type); a row with a blank
    Row_ID is added as <ref>:c<n>, a missing row is deleted, and every change is written to `audit`."""
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
    return Dataset(ds.ref, new, ds.raw_header, ds.source_file, ds.sheet)


def apply_marks(wb, findings: list[dict], reviews: dict, upload: int, audit: list) -> dict:
    """Review cells of the upload -> review.json (finding id -> mark, upload, finding as exported)."""
    by_id = {f["finding_id"]: f for f in findings}
    out = dict(reviews)
    for sheet in COLUMNS:
        fi = len(header(sheet)) - 2
        for r in _rows(wb[sheet])[1:]:
            r = (r + [None, None])[:fi + 2]
            if r[fi] in (None, "") or str(r[fi]) not in by_id:
                continue
            f, new = by_id[str(r[fi])], review_value(r[fi + 1])
            old = (out.get(f["finding_id"]) or {}).get("review")
            if new == old:
                continue
            if new is None:
                out.pop(f["finding_id"], None)
                audit.append({"upload": upload, "action": "review_cleared", "finding_id": f["finding_id"],
                              "code": f["code"], "previous": old})
                continue
            snapshot = {k: v for k, v in f.items() if k not in ("status", "review", "listed", "note")}
            out[f["finding_id"]] = {"review": new, "upload": upload, "code": f["code"], "reason": f["reason"],
                                    "finding": snapshot}
            audit.append({"upload": upload, "action": "marked", "review": new, "finding_id": f["finding_id"],
                          "code": f["code"], "reason": f["reason"]})
    return out


def apply_upload(run_dir, uploaded, cfg: dict) -> dict:
    """Apply an edited Spend_Support_Trend_Report.xlsx to a finished run and re-run Phase D on the result.
    Raises ReviewError on a bad upload (nothing is changed then)."""
    from .config import load_client_config
    from .pipeline import CONFIG_COPY, OUT, check_and_save
    from .weekly import write_weekly
    if not (Path(run_dir) / OUT / "run.json").exists():
        raise FileNotFoundError(f"{run_dir} is not a Phase D run folder (no {OUT}/run.json): pass the -o folder of 'run'")
    try:
        out = units.unit_for_upload(Path(run_dir) / OUT, Path(uploaded))
    except ValueError as e:
        raise ReviewError([str(e)]) from e
    except Exception as e:  # noqa: BLE001 - not a workbook at all
        raise ReviewError([f"cannot open {uploaded}: {type(e).__name__}: {e}"]) from e
    log = run_logger(Path(run_dir), "dq.trends")
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
        problems = precheck(wb, findings, ds)
        if problems:
            log.warning("review upload %s rejected: %s", uploaded, "; ".join(problems[:5]))
            raise ReviewError(problems)
        upload = keep_version(out, Path(uploaded), "uploaded")
        audit: list[dict] = []
        corrected = apply_data(wb["Data"], ds, upload, audit)
        reviews = apply_marks(wb, findings, read_json(out / REVIEWS, {}), upload, audit)
    finally:
        wb.close()
    dump(out / REVIEWS, reviews)
    dump(out / AUDIT, read_json(out / AUDIT, []) + audit)
    log.info("review upload %s applied: %d data change(s), %d review mark(s) changed", uploaded,
             sum(a["action"] not in ("marked", "review_cleared") for a in audit),
             sum(a["action"] in ("marked", "review_cleared") for a in audit))
    cc = load_client_config(out / CONFIG_COPY, cfg)
    result = check_and_save(corrected, cc, cfg, out, log, {"input": run["input"], "uploads": upload})
    write_weekly(corrected, Path(run_dir) / OUT, cfg, log)       # the corrected monthly table, spread over weeks again
    return result
