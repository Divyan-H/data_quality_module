"""Phase C adapter: time-series / execution checks (`src/timeseries`). Every table the client config describes is
checked, each with its own report; tables it does not describe are listed with the reason and passed on unchanged."""
from __future__ import annotations

from pathlib import Path

from src.common import units
from src.common.errors import ConfigError
from src.common.granularity import assess
from src.common.values import clean_name
from src.timeseries.autoconfig import init_config, profile
from src.timeseries.config import load_client_config, load_settings
from src.timeseries.io import load_candidates
from src.timeseries.layout import resolve_layout, why_not
from src.timeseries.pipeline import OUT, run_timeseries
from src.timeseries.review import REPORT, ReviewError, apply_upload

from . import findings
from .base import friendly, ensure_workbook, Artifact, Context, Paths, Phase, PhaseError, ReviewRejected, copy_version, read_json, wipe

CONFIG_NAME = "phase_C_client_config.xlsx"
TEMPLATE_NAME = "phase_C_client_config_prefilled.xlsx"


def config_problem(e: Exception) -> ReviewRejected:
    return ReviewRejected([{"code": "config_error", "message": friendly(str(e))}], "The client config workbook was not accepted.")


def not_checked(index: dict) -> list[dict]:
    return [{"ref": e["table_ref"], "file": e["source_file"], "sheet": e["sheet"], "rows": e["rows"], "reason": e["reason"]}
            for e in index["tables"] if not e.get("folder")]


def unit_artifacts(out: Path, phase: str, prefix: str, report: str, review_text: str) -> list[Artifact]:
    """One review report (primary), findings and run file per checked table, named after the table's sheet."""
    index = units.read_index(out)
    if index is None:
        return []
    names = units.file_names(index["tables"])
    arts = []
    for e, folder in units.checked(out):
        name, stem = names[e["table_ref"]], e["folder"]
        arts += [Artifact(f"{prefix}-report-{stem}", f"{Path(report).stem}_{name}.xlsx", folder / report, "review",
                          f"{e['sheet'] or e['source_file']}: {review_text}", True),
                 Artifact(f"{prefix}-findings-{stem}", f"findings_{phase.lower()}_{name}.json", folder / "findings.json",
                          "audit", f"{e['sheet'] or e['source_file']}: every finding, one per line.", False),
                 Artifact(f"{prefix}-run-{stem}", f"run_{phase.lower()}_{name}.json", folder / "run.json", "audit",
                          f"{e['sheet'] or e['source_file']}: layout, checks, counts.", False)]
    arts.append(Artifact(f"{prefix}-config", f"client_config_{phase.lower()}.xlsx", out / "client_config.xlsx", "config",
                         "The client config this run used.", False))
    return [a for a in arts if a.path.exists()]


class TimeseriesPhase(Phase):
    key = "C"
    title = "Time-series checks"
    short = "Gaps, repeats, duplicates per series"
    describes = ("Checks every dated table, series by series (one per City, Channel ... combination): no-execution runs, "
                 "repeated values, duplicate or missing periods, out-of-order dates and duplicate variable names. "
                 "Findings are reported, never silently changed.")
    needs_config = True
    review_name = REPORT

    # -- config ------------------------------------------------------------
    def candidates(self, ctx: Context) -> list[dict]:
        cfg = load_settings(ctx.settings_path)
        out = []
        for d in load_candidates(str(ctx.paths.work), None, cfg):
            a = assess(d, cfg["dayfirst"])
            weekly, why = a["applicable_c"], a["message"]
            if d.derived:
                why = ("The data is monthly: it is turned into Monday-to-Sunday weeks before the checks (each monthly figure "
                       "split over the days of its weeks; set Sum or Average per column on the Column Setup sheet).")
            if weekly:
                try:
                    profile(d, cfg)
                except ConfigError as e:                      # a date column and numbers, but the profile could not use them
                    weekly, why = False, str(e)
            out.append({"ref": d.ref, "file": d.source_file, "sheet": d.sheet, "rows": len(d.row_ids),
                        "columns": len(d.columns), "weekly": weekly, "level": "monthly" if d.derived else a["level"],
                        "from_monthly": bool(d.derived), "note": why})
        return out

    def make_template(self, ctx: Context, table: str | None) -> Path:
        paths = ctx.paths
        paths.config.mkdir(exist_ok=True)
        dst = paths.config / TEMPLATE_NAME
        try:
            init_config(str(paths.work), str(dst), ctx.settings_path, table)
        except ConfigError as e:
            raise PhaseError("template_failed", friendly(str(e))) from e
        return dst

    def check_config(self, ctx: Context, workbook: Path, table: str | None) -> dict:
        """Which tables the config describes (and why not the others), and what Phase C will do with the first."""
        cfg = load_settings(ctx.settings_path)
        try:
            cc = load_client_config(workbook, cfg)
            tables, first = [], None
            for ds in load_candidates(str(ctx.paths.work), None, cfg, cc.rollup):
                why = why_not(ds, cc, cfg)
                tables.append({"ref": ds.ref, "file": ds.source_file, "sheet": ds.sheet, "rows": len(ds.row_ids),
                               "checked": why is None, "reason": why})
                if why is None and first is None:
                    lay = resolve_layout(ds, cc, cfg)
                    mapped = [v for v in lay.variables if clean_name(v) in cc.mapping]
                    first = {"period": lay.period, "geography": lay.geography, "series": lay.series,
                             "variables": len(lay.variables), "checked": len(mapped),
                             "ignored": [v for v in lay.variables if v not in mapped][:30]}
        except (ConfigError, FileNotFoundError) as e:
            raise config_problem(e) from e
        if first is None:
            raise config_problem(ConfigError("the client config describes none of the tables: " + "; ".join(
                f"{t['sheet'] or t['file'] or t['ref']}: {t['reason']}" for t in tables)))
        warnings = []
        if not first["checked"]:
            warnings.append("No variable has a usable Bucket on Variable Mapping, so no variable would be checked.")
        skipped = [t for t in tables if not t["checked"]]
        if skipped:
            warnings.append(f"{len(skipped)} table(s) will not be checked and go on unchanged: " + "; ".join(
                f"{t['sheet'] or t['file'] or t['ref']} ({t['reason']})" for t in skipped))
        return {"table": None, "tables": tables, "rows": sum(t["rows"] for t in tables if t["checked"]), **first,
                "min_zero_weeks": cc.min_weeks if cc.min_weeks is not None else cfg["min_zero_weeks"],
                "date_ranges": {k: [str(a) if a else None for a in v] for k, v in cc.ranges.items()},
                "warnings": warnings}

    # -- run ---------------------------------------------------------------
    def run(self, ctx: Context, params: dict) -> dict:
        paths = ctx.paths
        config = paths.config / CONFIG_NAME
        if not config.exists():
            raise PhaseError("config_missing", "Upload the filled client config workbook first.")
        wipe(paths.work / OUT)
        ctx.emit.stage("checking the tables", 0)
        try:
            run_timeseries(str(paths.work), str(paths.work), str(config), ctx.settings_path, table=params.get("table"))
        except (ConfigError, FileNotFoundError) as e:
            raise PhaseError("phase_failed", friendly(str(e))) from e
        return self.summary(ctx.run)

    def review(self, ctx: Context, upload: Path, params: dict) -> dict:
        ctx.emit.stage("applying your review and re-checking", 0)
        ensure_workbook(upload)
        try:
            result = apply_upload(ctx.paths.work, upload, load_settings(ctx.settings_path))
        except ReviewError as e:
            raise ReviewRejected([{"message": p} for p in e.problems]) from e
        except (ConfigError, FileNotFoundError) as e:
            raise PhaseError("phase_failed", friendly(str(e))) from e
        copy_version(upload, ctx.paths.uploads, "C")
        return {**self.summary(ctx.run), "applied": {k: v for k, v in result.items() if k in ("table_ref", "uploads", "totals")}}

    def clear(self, run: Path) -> None:
        wipe(Paths(run).work / OUT)

    # -- results -----------------------------------------------------------
    def summary(self, run: Path) -> dict:
        out = Paths(run).work / OUT
        index = units.read_index(out)
        if index is None:
            return {}
        tables = []
        for _, folder in units.checked(out):
            meta = read_json(folder / "run.json")
            if meta is None:
                continue
            prev = findings.preview(folder / "findings.json")
            tables.append({"table": meta["table_ref"], "source_file": meta["source_file"], "sheet": meta["sheet"],
                           "rows": meta["rows"], "layout": meta["layout"], "variables": meta["variables"],
                           "periods": meta["periods"], "dataset_checks": meta["dataset_checks"], "counts": meta["counts"],
                           "totals": meta["totals"], "uploads": meta.get("uploads", 0), "findings": prev["rows"],
                           "findings_by_code": prev["counts"], "max_sheet_rows": meta["max_sheet_rows"]})
        return {"tables": tables, "not_checked": not_checked(index)}

    def artifacts(self, run: Path) -> list[Artifact]:
        return unit_artifacts(Paths(run).work / OUT, "C", "timeseries", REPORT,
                              "one sheet per check. Mark a row 'Valid – no change', or correct the Data sheet, then upload.")
