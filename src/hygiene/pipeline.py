"""Phase B entry point: Phase A tables in, value-clean tables + reports + review workbook out."""
from __future__ import annotations

import shutil
from pathlib import Path

from src.common.columns import SYSTEM
from src.common.dataset import dump, dump_lines
from src.common.logs import run_logger
from src.common.run_inputs import columns_for

from . import review
from .config import load_config
from .io import load_tables, read_frame, write_table
from .kpi import bucket_report
from .report import build_report
from .rules import RULES
from .table import Table
from .values import parse_number

OUT = "phase_b_hygiene"
BUCKETS = "buckets.json"


def clean(t: Table) -> Table:
    """Run every rule in order, then record each column's final kind (used to type the output). When the run inputs choose
    the columns to check, the rules see only those (the others still split a table into series); the rest are passed on as
    they were read."""
    chosen = columns_for(t.cfg.get("columns_to_check") or [], t.ref, t.sheet, t.source_file)
    unchecked: list[str] = []
    if chosen is not None:
        want, have = {c.casefold() for c in chosen}, {c.casefold() for c in t.cols}
        unchecked = [c for c in t.cols if c.casefold() not in want]
        unknown = [c for c in chosen if c.casefold() not in have]
        t.context_cols = list(t.cols)
        t.cols = [c for c in t.cols if c.casefold() in want]
        t.column_choice = {"checked": list(t.cols), "not_checked": unchecked, "not_found": unknown}
        for c in unknown:
            t.flag("D-32", c, None, f"The column '{c}' was chosen to be checked but is not in this sheet.",
                   "Check the spelling in the run inputs, or ask the client for the column. Nothing was changed.", sev="info")
    for rule in RULES:
        rule(t)
    if chosen is not None:
        t.cols = [c for c in t.data if c not in SYSTEM]
    t.types = {c: t.kind(c) for c in t.cols}
    sym = t.cfg["currency_symbols"]
    for c in unchecked:                                           # not checked: a number column only if nothing in it needs cleaning
        vals = t.values(c)
        t.types[c] = "numeric" if vals and all(parse_number(v, sym) == v.strip() for v in vals) else "text"
    return t


def save(run_dir: Path, tables: list[Table], unreadable: list[dict], cfg: dict, skipped: list[str] = ()) -> dict:
    """Write everything Phase B produces under <run_dir>/phase_b_hygiene/."""
    out = Path(run_dir) / OUT
    for stale in ("tables", "quality_reports"):                   # a re-run must not leave tables of an earlier run
        shutil.rmtree(out / stale, ignore_errors=True)
    reports = [build_report(t, cfg) for t in tables]
    for t, r in zip(tables, reports):
        write_table(t, out)
        dump(out / "quality_reports" / f"{t.ref.replace(':', '_')}.json", r)
    audit = [a for t in tables for a in t.audit]
    needs = [i for t in tables for i in t.review]
    dump_lines(out / "remediation_audit.json", audit)             # one entry per changed cell: can be millions
    dump_lines(out / "needs_review.json", needs)
    review.export(out, tables)
    buckets = bucket_report(tables)
    if buckets:                                                   # only when something adds up; a clean run has no file
        dump(out / BUCKETS, buckets)
    else:
        (out / BUCKETS).unlink(missing_ok=True)
    run = {"tables": [r["table_ref"] for r in reports], "unreadable": unreadable, "skipped": list(skipped),
           "totals": {"cells_fixed": sum(a["action"] == "fixed" for a in audit),
                      "rows_dropped": sum(a["action"] == "dropped" for a in audit),
                      "needs_review_open": sum(i["status"] == "open" for i in needs)}}
    if cfg.get("zero_expected"):                                  # the analyst's "zero is a valid value" columns, kept with the run
        run["zero_expected"] = cfg["zero_expected"]
    dump(out / "run.json", run)
    return run


def run_hygiene(input_path: str, run_dir: str, settings: str | None = None, overrides: dict | None = None,
                sheet: str | None = None, tables: list[str] | None = None, all_tables: bool = False) -> dict:
    """Clean every selected table found under `input_path` (a Phase A output folder or a single file)."""
    cfg = load_config(settings, overrides)
    log = run_logger(Path(run_dir), "dq.hygiene")
    if cfg["data_dictionary"]:                                    # D-16: documented names [-> standard names]
        df = read_frame(Path(cfg["data_dictionary"]))
        names = df[df.columns[0]].to_list()
        std = df[df.columns[1]].to_list() if df.width > 1 else [None] * len(names)
        cfg["_dictionary"] = {str(n).strip().casefold(): (s.strip() if s and s.strip() else None)
                              for n, s in zip(names, std) if n}
    if not Path(input_path).exists():
        raise FileNotFoundError(f"input not found: {input_path} (give a Phase A output folder or a csv/xlsx/parquet file)")
    found, unreadable, skipped = load_tables(Path(input_path), cfg, sheet, tables, all_tables)
    if not (found or unreadable or skipped):
        raise FileNotFoundError(f"no tables found in {input_path}: expected a Phase A output folder "
                                "(files/*/tables/*/data.parquet) or csv/xlsx/parquet files")
    log.info("phase B: %d table(s) to clean, %d skipped (not selected / filtered), %d unreadable",
             len(found), len(skipped), len(unreadable))
    for u in unreadable:
        log.warning("unreadable: %s (%s)", u["file"], u["error"])
    for t in found:
        clean(t)
        log.info("%s: %d rows in, %d out, %d fixes, %d to review", t.ref, t.rows_in, len(t.data["_row_id"]),
                 sum(a["action"] != "analyst_edit" for a in t.audit), len(t.review))
    run = save(Path(run_dir), found, unreadable, cfg, skipped)
    log.info("phase B done: %s", run["totals"])
    return run
