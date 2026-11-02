"""Phase D adapter: trends and period-over-period change (`src/trends`). Every table the client config describes is
checked (as Phase C left it, else as Phase B did), each with its own report; the others are listed and passed on."""
from __future__ import annotations

from pathlib import Path

from src.common import units
from src.common.errors import ConfigError
from src.common.granularity import assess
from src.trends.autoconfig import init_config
from src.trends.config import THRESHOLDS, load_client_config, load_settings
from src.trends.io import load_input, resolve_layout, why_not
from src.trends.pipeline import OUT, run_trends
from src.trends.review import REPORT, ReviewError, apply_upload
from src.trends.weekly import WORKBOOK as WEEKLY_WORKBOOK, tables as weekly_tables

from . import findings
from .base import friendly, ensure_workbook, Artifact, Context, Paths, Phase, PhaseError, ReviewRejected, copy_version, read_json, wipe
from .timeseries import not_checked, unit_artifacts

CONFIG_NAME = "phase_D_client_config.xlsx"
TEMPLATE_NAME = "phase_D_client_config_prefilled.xlsx"


def config_problem(e: Exception) -> ReviewRejected:
    return ReviewRejected([{"code": "config_error", "message": friendly(str(e))}], "The client config workbook was not accepted.")


class TrendsPhase(Phase):
    key = "D"
    title = "Trends and outliers"
    short = "Abnormal trends, period change, concentration"
    describes = ("Compares periods (month, quarter, half, year) and flags abnormal trends, large percentage changes, "
                 "client-defined ratios, Spend/Support mismatches and spend concentration. Every threshold comes from "
                 "your config: a rule with no threshold does not run.")
    needs_config = True
    review_name = REPORT

    def candidates(self, ctx: Context) -> list[dict]:
        cands, _ = load_input(str(ctx.paths.work))
        cfg = load_settings(ctx.settings_path)
        out = []
        for d in cands:
            a = assess(d, cfg["dayfirst"])
            out.append({"ref": d.ref, "file": d.source_file, "sheet": d.sheet, "rows": len(d.row_ids),
                        "columns": len(d.columns), "weekly": a["applicable_d"], "level": a["level"], "note": a["message"]})
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
        """Which tables the config describes (and why not the others), and which rules will run."""
        cfg = load_settings(ctx.settings_path)
        try:
            cands, _ = load_input(str(ctx.paths.work))
            cc = load_client_config(workbook, cfg)
            tables, period = [], None
            for ds in cands:
                why = why_not(ds, cc, cfg)
                tables.append({"ref": ds.ref, "file": ds.source_file, "sheet": ds.sheet, "rows": len(ds.row_ids),
                               "checked": why is None, "reason": why})
                if why is None and period is None:
                    period = resolve_layout(ds, cc, cfg).period
        except (ConfigError, FileNotFoundError) as e:
            raise config_problem(e) from e
        if period is None:
            raise config_problem(ConfigError("the client config describes none of the tables: " + "; ".join(
                f"{t['sheet'] or t['file'] or t['ref']}: {t['reason']}" for t in tables)))
        rules = [{"key": k, "code": code, "label": label, "threshold": cc.thresholds.get(k),
                  "active": cc.thresholds.get(k) is not None} for k, (code, label, _kind) in THRESHOLDS.items()]
        ratios = [{"name": r.name, "numerator": r.numerator, "denominator": r.denominator, "threshold": r.threshold,
                   "active": r.threshold is not None} for r in cc.ratios]
        warnings = []
        if not any(r["active"] for r in rules) and not any(r["active"] for r in ratios):
            warnings.append("No threshold is filled in, so no Phase D rule would run. Only the Spend/Support "
                            "availability check (N-19) does not need a threshold.")
        elif not all(r["active"] for r in rules):
            warnings.append("Rules with an empty threshold will not run: "
                            + ", ".join(r["label"] for r in rules if not r["active"]) + ".")
        if cc.unused:
            warnings.append(f"{len(cc.unused)} row(s) on Data Check Specifications match no Phase D rule and are ignored.")
        skipped = [t for t in tables if not t["checked"]]
        if skipped:
            warnings.append(f"{len(skipped)} table(s) will not be checked and go on unchanged: " + "; ".join(
                f"{t['sheet'] or t['file'] or t['ref']} ({t['reason']})" for t in skipped))
        return {"table": None, "tables": tables, "rows": sum(t["rows"] for t in tables if t["checked"]),
                "period": period, "pairs": len(cc.pairs), "rules": rules, "ratios": ratios, "warnings": warnings}

    def run(self, ctx: Context, params: dict) -> dict:
        paths = ctx.paths
        config = paths.config / CONFIG_NAME
        if not config.exists():
            raise PhaseError("config_missing", "Upload the filled client config workbook first.")
        wipe(paths.work / OUT)
        ctx.emit.stage("checking trends", 0)
        try:
            run_trends(str(paths.work), str(paths.work), str(config), ctx.settings_path, table=params.get("table"))
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
        copy_version(upload, ctx.paths.uploads, "D")
        return {**self.summary(ctx.run), "applied": {k: v for k, v in result.items() if k in ("table_ref", "uploads", "totals")}}

    def clear(self, run: Path) -> None:
        wipe(Paths(run).work / OUT)

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
            prev = findings.preview(folder / "findings.json", only_status=None)
            tables.append({"table": meta["table_ref"], "source_file": meta["source_file"], "sheet": meta["sheet"],
                           "rows": meta["rows"], "pairs": meta["pairs"], "counts": meta["counts"], "totals": meta["totals"],
                           "inactive_rules": meta.get("inactive_rules", []), "rules": meta.get("rules", []),
                           "uploads": meta.get("uploads", 0), "findings": prev["rows"], "findings_by_code": prev["counts"]})
        out_summary = {"tables": tables, "not_checked": not_checked(index)}
        weekly = [{k: m.get(k) for k in ("table_ref", "source_file", "sheet", "rows_in", "rows_out", "measures", "exact", "unreadable", "inexact")}
                  for m, _ in weekly_tables(Paths(run).work / OUT)]
        if weekly:                                            # only monthly data has it; other runs' summaries are unchanged
            out_summary["weekly"] = weekly
        return out_summary

    def artifacts(self, run: Path):
        out = Paths(run).work / OUT
        arts = unit_artifacts(out, "D", "trends", REPORT, "mark each row Confirmed Issue, Valid Movement or Corrected, then upload.")
        for meta, folder in weekly_tables(out):
            where = meta.get("sheet") or meta.get("source_file") or meta["table_ref"]
            arts.append(Artifact(f"trends-weekly-{folder.name}", f"Weekly_Allocation_{folder.name}.xlsx", folder / WEEKLY_WORKBOOK, "audit", "D",
                                 f"{where}: the monthly figures spread over Monday-to-Sunday weeks, and how each row was split."))
        return [a for a in arts if a.path.exists()]
