"""Phase B adapter: cell-level data hygiene (`src/hygiene`). Phase A's selected tables in, value-clean typed tables out."""
from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from src.common.run_inputs import hygiene_overrides, load_for_run
from src.hygiene.config import FIXES, RULE_TEXT, load_config
from src.hygiene.io import load_tables
from src.hygiene.kpi import period_column
from src.hygiene.pipeline import BUCKETS, OUT, run_hygiene
from src.hygiene.review import ReviewError, apply_upload
from src.hygiene.values import has_keyword

from .base import ensure_workbook, Artifact, Context, Paths, Phase, PhaseError, ReviewRejected, copy_version, read_json, wipe
from .structure import chosen_tables

REVIEW = "Hygiene_Review.xlsx"
PREVIEW_ROWS = 300


RETIRED_FIXES = {"D-15"}      # no longer switchable (flag-only): a run started before that may still carry it in its settings
NAME_HINTS = ["weather", "rain", "snow", "temp", "impression", "spend", "media", "grp", "trp"]   # a tip only; the numbers decide the tick


def overrides_from(params: dict) -> dict:
    """The hygiene config overrides for a run: the fix switches and the 'zero is a valid value' columns from the analyst's start options."""
    out: dict = {}
    if params.get("fixes"):
        unknown = sorted(set(params["fixes"]) - set(FIXES) - RETIRED_FIXES)
        if unknown:
            raise PhaseError("bad_option", f"unknown automatic fix(es): {', '.join(unknown)}")
        out["fixes"] = {k: bool(v) for k, v in params["fixes"].items() if k not in RETIRED_FIXES}
    zero = params.get("zero_expected")
    if zero:
        if not isinstance(zero, dict) or not all(isinstance(k, str) and isinstance(v, list) and all(isinstance(c, str) for c in v)
                                                 for k, v in zero.items()):
            raise PhaseError("bad_option", "zero_expected must map a table to the list of its columns where zero is a valid value")
        out["zero_expected"] = {k: sorted(set(v)) for k, v in zero.items() if v}
    return out


class HygienePhase(Phase):
    key = "B"
    title = "Cell-level hygiene"
    short = "Fix and flag individual values"
    describes = ("Checks every value: numbers stored as text, impossible dates, stray spaces, case variants, outliers, "
                 "duplicates, formula errors. Safe fixes are applied and logged; doubtful ones wait for your decision.")
    review_name = REVIEW

    def columns(self, ctx: Context) -> list[dict]:
        """Per table Phase B will clean: its numeric columns with how many zeros / blanks they hold, so the analyst can tick
        the ones where zero is a real value (weather, impressions, spend ...). D-27 fills isolated zeros in all the others."""
        paths = ctx.paths
        if not (paths.phase_a / "inventory.json").exists():
            raise PhaseError("not_ready", "Phase A has no output to read.")
        cfg = load_config(ctx.settings_path)
        chosen = chosen_tables(ctx.run)
        found, _, _ = load_tables(paths.phase_a, cfg, tables=chosen, all_tables=chosen is not None)
        out = []
        for t in found:
            period, _ = period_column(t)
            cols = []
            for col in t.cols:
                if col == period or t.kind(col) != "numeric":
                    continue
                nums = [v for _, v in t.numbers(col)]
                zeros = sum(v == 0 for v in nums)
                cols.append({"name": col, "numbers": len(nums), "zeros": zeros, "blanks": len(t.data[col]) - len(nums),
                             "zero_share": round(zeros / len(nums), 3) if nums else 0.0,
                             "likely_zero_valid": bool(nums) and zeros / len(nums) >= cfg["zero_heavy_share"],
                             "name_hint": has_keyword(col, NAME_HINTS)})
            out.append({"ref": t.ref, "file": t.source_file, "sheet": t.sheet, "rows": len(t.data["_row_id"]),
                        "dated": period is not None, "period": period, "columns": cols, "column_names": list(t.cols)})
        return out

    def run(self, ctx: Context, params: dict) -> dict:
        paths = ctx.paths
        if not (paths.phase_a / "inventory.json").exists():
            raise PhaseError("not_ready", "Phase A has no output to clean.")
        wipe(paths.work / OUT)
        paths.work.mkdir(exist_ok=True)
        ctx.emit.stage("cleaning tables", 0)
        chosen = chosen_tables(ctx.run)                             # the analyst's tick boxes, else Phase A's choice
        try:
            run_hygiene(str(paths.phase_a), str(paths.work), ctx.settings_path,
                        {**overrides_from(params), **hygiene_overrides(load_for_run(ctx.run)[0])},
                        tables=chosen, all_tables=chosen is not None)
        except (ValueError, FileNotFoundError) as e:
            raise PhaseError("phase_failed", str(e)) from e
        return self.summary(ctx.run)

    def review(self, ctx: Context, upload: Path, params: dict) -> dict:
        paths = ctx.paths
        ctx.emit.stage("applying your review", 0)
        ensure_workbook(upload)
        try:
            result = apply_upload(paths.work, upload, self.config_for(ctx, params))
        except ReviewError as e:
            raise ReviewRejected([{"message": p} for p in e.problems]) from e
        except FileNotFoundError as e:
            raise PhaseError("not_run", str(e)) from e
        copy_version(upload, paths.uploads, "B")
        return {**self.summary(ctx.run), "applied": result}

    def clear(self, run: Path) -> None:
        wipe(Paths(run).work / OUT)

    # -- Fix-with-agent ------------------------------------------------
    supports_agent = True

    def config_for(self, ctx: Context, params: dict) -> dict:
        """The hygiene settings a run's review and agent changes use: the run's fix switches and run inputs."""
        return load_config(ctx.settings_path, {**overrides_from(params), **hygiene_overrides(load_for_run(ctx.run)[0])})

    def agent_workspace(self, ctx: Context, params: dict):
        from src.agent.workspace import HygieneWorkspace
        return HygieneWorkspace(ctx.paths.work, self.config_for(ctx, params))

    def agent_views(self, run: Path):
        from src.agent.views import HygieneViews
        return HygieneViews(Paths(run).work)

    def summary(self, run: Path) -> dict:
        out = Paths(run).work / OUT
        meta = read_json(out / "run.json")
        if meta is None:
            return {}
        reports = [read_json(p, {}) for p in sorted((out / "quality_reports").glob("*.json"))]
        by_rule: dict[str, dict] = {}
        for r in reports:
            for code, c in r.get("counts_by_rule", {}).items():
                agg = by_rule.setdefault(code, {"rule": code, "what": RULE_TEXT.get(code, ""), "fixed": 0, "flagged": 0})
                agg["fixed"] += c["fixed"]
                agg["flagged"] += c["flagged"]
        needs = read_json(out / "needs_review.json", [])
        audit = read_json(out / "remediation_audit.json", [])
        return {
            "tables": [{"ref": r["table_ref"], "file": r.get("source_file"), "sheet": r.get("sheet"),
                        "rows_in": r["rows_in"], "rows_out": r["rows_out"], "needs_review": r["needs_review_count"],
                        "summary": r.get("summary", ""), "column_choice": r.get("column_choice")} for r in reports],
            "totals": meta["totals"], "unreadable": meta.get("unreadable", []), "skipped": meta.get("skipped", []),
            "by_rule": sorted(by_rule.values(), key=lambda r: -(r["fixed"] + r["flagged"])),
            "needs_review": [{k: i.get(k) for k in ("table_ref", "row_id", "column", "rule", "why_flagged", "current_value",
                                                    "original_value", "proposed_value", "severity", "client_report", "status",
                                                    "scope", "cells")}
                             for i in needs[:PREVIEW_ROWS]],
            "needs_review_total": len(needs),
            "fixes": [{k: a.get(k) for k in ("table_ref", "row_id", "column", "rule", "action", "old_value", "new_value", "note")}
                      for a in audit[:PREVIEW_ROWS]],
            "fixes_total": len(audit),
            "client_questions": sum(1 for i in needs if i.get("client_report")),
            "buckets": read_json(out / BUCKETS, []),                     # columns / rows that add up (D-28); [] when none
            "zero_expected": meta.get("zero_expected", {}),
        }

    def artifacts(self, run: Path) -> list[Artifact]:
        out = Paths(run).work / OUT
        spec = [
            ("hygiene-review", REVIEW, out / REVIEW, "review",
             "Summary, Needs_Review, Auto_Fix_Log and the cleaned data. Decide each flagged item, edit values, upload.", True),
            ("hygiene-audit", "remediation_audit.json", out / "remediation_audit.json", "audit", "Every change made: old value, new value, rule.", False),
            ("hygiene-needs-review", "needs_review.json", out / "needs_review.json", "audit", "Every flagged cell with the reason.", False),
            ("hygiene-run", "run_b.json", out / "run.json", "audit", "Tables cleaned, skipped, unreadable, totals.", False),
            ("hygiene-buckets", "buckets.json", out / BUCKETS, "audit",
             "Columns (or labelled rows) that add up to another: what to add, and what not to add together.", False),
        ]
        return [Artifact(i, label, p, kind, "B", desc, primary) for i, label, p, kind, desc, primary in spec if p.exists()]


def fix_catalogue(default_off: Collection[str] = frozenset()) -> list[dict]:
    """The automatic fixes the analyst can switch, for the Phase B start screen."""
    return [{"id": k, "what": RULE_TEXT.get(k, ""), "default": k not in default_off} for k in FIXES]
