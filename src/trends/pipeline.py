"""Phase D entry point: the tables as Phase C left them (or one file) + the client config workbook in, one
Spend_Support_Trend_Report.xlsx per table out."""
from __future__ import annotations

import logging
import shutil
from collections import Counter
from fnmatch import fnmatch
from pathlib import Path

from src.common import units

from src.common.logs import run_logger

from .checks import CHECKS, evaluate
from .config import ClientConfig, load_client_config, load_settings
from src.common.errors import ConfigError
from .io import load_input, resolve_layout, why_not
from src.common.dataset import Dataset, dump, dump_lines, write_table
from .report import STATUS_OF, export
from .review import AUDIT, REPORT, REVIEWS, keep_version, read_json
from .weekly import write_weekly

OUT = "phase_d_trends"
CONFIG_COPY = "client_config.xlsx"            # the client config workbook the run used (re-used by review uploads)
STATES = ["open", "confirmed", "valid", "corrected"]


def run_trends(input_path: str, run_dir: str, client_config: str | None = None, settings: str | None = None,
               overrides: dict | None = None, table: str | None = None, sheet: str | None = None) -> dict:
    """Check every table under `input_path` (a run folder after Phase C or B, or one csv / xlsx / parquet file) that
    the client config workbook describes - or only the tables matching `table` - each in its own folder under
    <run_dir>/phase_d_trends/. Without `client_config`, Phase C's copy of the (shared) client config workbook is used.
    Returns the index (src/common/units.py)."""
    cfg = load_settings(settings, overrides)
    log = run_logger(Path(run_dir), "dq.trends")
    cands, phase_c_config = load_input(input_path, sheet)
    client_config = client_config or phase_c_config
    if client_config is None:
        raise ConfigError("no client config workbook: pass one (--client-config) or give a Phase C run folder")
    cc = load_client_config(client_config, cfg)
    out = Path(run_dir) / OUT
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    for ds in cands:
        if table and not fnmatch(ds.ref, table):
            continue
        write_weekly(ds, out, cfg, log)                      # monthly tables only; an extra file set, nothing below depends on it
        why = why_not(ds, cc, cfg)
        entry = {"table_ref": ds.ref, "source_file": ds.source_file, "sheet": ds.sheet, "rows": len(ds.row_ids),
                 "folder": None if why else units.stem(ds.ref), "reason": why}
        if why:
            log.info("phase D: %s (%s) not checked: %s", ds.ref, ds.sheet or ds.source_file, why)
        else:
            unit = out / entry["folder"]
            unit.mkdir(exist_ok=True)
            shutil.copy(client_config, unit / CONFIG_COPY)
            log.info("phase D: checking %s (%s / %s), %d row(s)", ds.ref, ds.source_file, ds.sheet, len(ds.row_ids))
            entry["totals"] = check_and_save(ds, cc, cfg, unit, log, {"input": str(input_path), "uploads": 0})["totals"]
        entries.append(entry)
    if not any(e["folder"] for e in entries):
        raise ConfigError("the client config describes none of the tables: " + ("; ".join(
            f"{e['table_ref']} ({e['sheet'] or e['source_file'] or '-'}): {e['reason']}" for e in entries) or
            "no table found"))
    shutil.copy(client_config, out / CONFIG_COPY)
    return units.write_index(out, entries, client_config=CONFIG_COPY)


def apply_reviews(res, out: Path, upload: int) -> list[dict]:
    """Analyst marks (review.json) onto this run's findings. Returns the findings marked Corrected that are no longer
    flagged (kept in findings.json as "corrected"); one still flagged is open again and its mark is dropped."""
    reviews = read_json(out / REVIEWS, {})
    present = {f.id for f in res.findings}
    reopened = []
    for f in res.findings:
        mark = reviews.get(f.id)
        if not mark:
            continue
        if mark["review"] == "Corrected":
            f.note = f"marked Corrected in upload {mark['upload']}, still flagged after the re-run"
            reopened.append({"upload": upload, "action": "corrected_still_flagged", "finding_id": f.id,
                             "code": f.code, "reason": f.reason})
            del reviews[f.id]
        else:
            f.status, f.review = STATUS_OF[mark["review"]], mark["review"]
    if reopened:
        dump(out / REVIEWS, reviews)
        dump(out / AUDIT, read_json(out / AUDIT, []) + reopened)
    return [{**m["finding"], "status": "corrected", "review": "Corrected", "listed": False,
             "note": f"marked Corrected in upload {m['upload']}: no longer flagged"}
            for fid, m in reviews.items() if m["review"] == "Corrected" and fid not in present]


def check_and_save(ds: Dataset, cc: ClientConfig, cfg: dict, out: Path, log: logging.Logger, extra: dict) -> dict:
    """Run every active rule on `ds`, apply the analyst's marks, and write the report, findings and table."""
    lay = resolve_layout(ds, cc, cfg)
    res = evaluate(ds, lay, cc, cfg)
    resolved = apply_reviews(res, out, extra["uploads"])
    shutil.rmtree(out / "tables", ignore_errors=True)        # a re-run must not leave an earlier table behind
    write_table(ds, out)
    keep_version(out, export(out / REPORT, ds, res, cfg), "downloaded")
    findings = [f.as_dict(cfg["client_codes"]) for f in res.findings] + resolved
    dump_lines(out / "findings.json", findings)
    counts = {c: Counter(f["status"] for f in findings if f["code"] == c) for c in CHECKS}
    run = {"table_ref": ds.ref, "source_file": ds.source_file, "sheet": ds.sheet, **extra,
           "client_config": CONFIG_COPY, "report": REPORT, "rows": len(ds.row_ids),
           "layout": {"period": lay.period, "variables": lay.variables},
           "columns": ds.columns, "raw_header": ds.raw_header, "axis": res.axis, "pairs": res.pairs,
           "rules": res.rules, "inactive_rules": res.inactive,
           "counts": {c: {s: n[s] for s in STATES} for c, n in counts.items() if n},
           "totals": {f"findings_{s}": sum(n[s] for n in counts.values()) for s in STATES},
           "max_sheet_rows": cfg["max_sheet_rows"]}
    dump(out / "run.json", run)
    for line in res.inactive:
        log.warning("phase D rule inactive - %s", line)
    log.info("phase D done: %d pair(s) checked, %d finding(s) open, %d confirmed, %d valid, %d corrected: %s",
             res.pairs["run"], *(run["totals"][f"findings_{s}"] for s in STATES),
             {c: n["open"] for c, n in sorted(counts.items()) if n["open"]})
    return run
