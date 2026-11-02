"""Phase C entry point: the tables + the client config workbook in, one Data_Validation_Report.xlsx per table out."""
from __future__ import annotations

import json
import logging
import shutil
from collections import Counter
from fnmatch import fnmatch
from pathlib import Path

from src.common import units
from src.common.errors import ConfigError
from src.common.logs import run_logger

from .checks import CHECKS, evaluate
from .config import ClientConfig, load_client_config, load_settings
from .io import load_candidates
from src.common.dataset import Dataset, dump, dump_lines, write_table
from .layout import resolve_layout, why_not
from .report import DATASET_CODES, export
from .review import REPORT, keep_version

OUT = "phase_c_timeseries"
CONFIG_COPY = "client_config.xlsx"            # the client config workbook the run used (re-used by review uploads)
ROLLUP_LOG = "weekly_rollup.json"             # a monthly table made weekly: the roll-up of each column and the source rows of each week


def run_timeseries(input_path: str, run_dir: str, client_config: str, settings: str | None = None,
                   overrides: dict | None = None, table: str | None = None, sheet: str | None = None) -> dict:
    """Check every table under `input_path` (a Phase B run folder, or one csv / xlsx / parquet file) that the client
    config workbook describes - or only the tables matching `table` - each in its own folder under
    <run_dir>/phase_c_timeseries/. Tables are loaded one at a time. Returns the index (src/common/units.py)."""
    cfg = load_settings(settings, overrides)
    log = run_logger(Path(run_dir), "dq.timeseries")
    cc = load_client_config(client_config, cfg)
    out = Path(run_dir) / OUT
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    for ds, rollup_log in load_candidates(input_path, sheet, cfg, cc.rollup, with_log=True):
        if table and not fnmatch(ds.ref, table):
            continue
        why = why_not(ds, cc, cfg)
        entry = {"table_ref": ds.ref, "source_file": ds.source_file, "sheet": ds.sheet, "rows": len(ds.row_ids),
                 "folder": None if why else units.stem(ds.ref), "reason": why}
        if why:
            log.info("phase C: %s (%s) not checked: %s", ds.ref, ds.sheet or ds.source_file, why)
        else:
            unit = out / entry["folder"]
            unit.mkdir(exist_ok=True)
            shutil.copy(client_config, unit / CONFIG_COPY)
            log.info("phase C: checking %s (%s / %s), %d row(s)", ds.ref, ds.source_file, ds.sheet, len(ds.row_ids))
            if rollup_log:
                log.info("phase C: %s is monthly: %d row(s) spread over %d week row(s)", ds.ref,
                         rollup_log["monthly_rows"], rollup_log["weekly_rows"])
                dump(unit / ROLLUP_LOG, rollup_log)
            entry["totals"] = check_and_save(ds, cc, cfg, unit, log, {"input": str(input_path), "uploads": 0})["totals"]
        entries.append(entry)
    if not any(e["folder"] for e in entries):
        raise ConfigError("the client config describes none of the tables: " + ("; ".join(
            f"{e['table_ref']} ({e['sheet'] or e['source_file'] or '-'}): {e['reason']}" for e in entries) or
            "no table found"))
    shutil.copy(client_config, out / CONFIG_COPY)
    return units.write_index(out, entries, client_config=CONFIG_COPY)


def check_and_save(ds: Dataset, cc: ClientConfig, cfg: dict, out: Path, log: logging.Logger, extra: dict) -> dict:
    """Run every check on `ds`, drop findings marked valid earlier, and write the report, findings and table."""
    lay = resolve_layout(ds, cc, cfg)
    if ds.derived:                                            # weekly figures calculated from monthly ones (common/weekly_rollup.py)
        cfg = {**cfg, "skip_codes": ["N-08", "N-09"], "derived": ds.derived}
    res = evaluate(ds, lay, cc, cfg)
    accepted_path = out / "accepted.json"
    accepted = json.loads(accepted_path.read_text("utf8")) if accepted_path.exists() else {}
    for f in res.findings:
        if f.id in accepted:
            f.status = "valid"
    shutil.rmtree(out / "tables", ignore_errors=True)        # a re-run must not leave an earlier table behind
    write_table(ds, out)
    keep_version(out, export(out / REPORT, ds, lay, res, cfg), "downloaded")
    dump_lines(out / "findings.json", (f.as_dict(cfg["client_codes"]) for f in res.findings))
    opened = Counter(f.code for f in res.findings if f.status == "open")
    valid = Counter(f.code for f in res.findings if f.status == "valid")
    distinct = sorted({d for d in res.dates if d})
    run = {"table_ref": ds.ref, "source_file": ds.source_file, "sheet": ds.sheet, **extra,
           **({"derived": ds.derived} if ds.derived else {}),
           "client_config": CONFIG_COPY, "report": REPORT, "rows": len(ds.row_ids),
           "layout": {"period": lay.period, "geography": lay.geography, "variables": lay.variables},
           "columns": ds.columns, "raw_header": ds.raw_header,
           "variables": {"in_range": len(lay.variables), "checked": len(res.checked),
                         "ignored": len(lay.variables) - len(res.checked)},
           "periods": {"total": len(distinct), "first": distinct[0].isoformat() if distinct else None,
                       "last": distinct[-1].isoformat() if distinct else None},
           "dataset_checks": {c: "FAIL" if opened[c] else "PASS" for c in DATASET_CODES},
           "counts": {c: {"open": opened[c], "valid": valid[c]} for c in CHECKS if opened[c] or valid[c]},
           "totals": {"findings_open": sum(opened.values()), "findings_valid": sum(valid.values())},
           "max_sheet_rows": cfg["max_sheet_rows"]}
    dump(out / "run.json", run)
    log.info("phase C done: %d variable(s) checked, %d finding(s) open, %d marked valid: %s", len(res.checked),
             sum(opened.values()), sum(valid.values()), dict(sorted(opened.items())))
    return run
