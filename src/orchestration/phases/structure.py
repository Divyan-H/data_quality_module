"""Phase A adapter: structural intake (`src/structure`). Raw files in, one clean table per real data region out.

First run: `run_structure` over everything under inputs/. Re-run after the analyst edits `Structural_Review.xlsx`:
the workbook is checked and turned into overrides, a table selection and cell edits (`review_import`), which are
saved with the run's structure config and applied by a second `run_structure` that reuses everything unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import openpyxl

from src.structure.config import ConfigInvalid, resolve_settings
from src.structure.errors import JobFailed
from src.structure.job import load_edits, write_edits
from src.structure.pipeline import Progress, run_structure
from src.structure.review_import import apply_patch, import_review, merge_added

from .base import Artifact, Context, Paths, Phase, ReviewRejected, PhaseError, copy_version, read_json, wipe

SKIP = {".ds_store", "thumbs.db", "desktop.ini"}
SOURCE_ID = "s-1"
REVIEW = "Structural_Review.xlsx"
CONFIG = "config.json"
EDITS = "cell_edits.parquet"
ADDED = "added_columns.json"          # columns the analyst pasted next to the tables (Phase B joins them on Row_ID)
SELECTION = "selection.json"          # the tables the analyst kept (overrides Phase A's own choice)
LLM_DEFAULTS = {"mode": "assist", "privacy": "full", "votes": 3, "review": "near_ties"}
ISSUE_LIMIT = 400
CHANGE_LIMIT = 400


def collect_inputs(inputs: Path) -> list[dict]:
    files = []
    for full in sorted(p for p in inputs.rglob("*") if p.is_file()):
        if full.name.lower() in SKIP or full.name.startswith("~$"):
            continue
        files.append({"path": str(full), "source_id": SOURCE_ID,
                      "relative_path": full.relative_to(inputs).as_posix()})
    for i, f in enumerate(files, start=1):
        f["file_id"] = f"f-{i}"
    return files


def structure_config(llm: dict | None) -> dict:
    """The run's `structure` config: the LLM section from the analyst's choice, everything else from the rule pack."""
    if not llm or llm.get("mode") == "off":
        return {"structure": {"llm": {"mode": "off"}}}
    return {"structure": {"llm": {**LLM_DEFAULTS, **{k: v for k, v in llm.items()
                                                      if k in LLM_DEFAULTS and v is not None}}}}


def chosen_tables(run: Path) -> list[str] | None:
    """The tables the analyst kept on the Phase A screen, or None when Phase A's own selection stands."""
    sel = read_json(Paths(run).phase_a_state / SELECTION)
    return sel["tables"] if sel else None


def _llm_pin(llm: dict | None) -> dict | None:
    if not llm or llm.get("mode") == "off" or not llm.get("model"):
        return None
    return {"model": llm["model"], "model_digest": llm.get("digest")}


class StructurePhase(Phase):
    key = "A"
    title = "Structural intake"
    short = "Find the real tables in each file"
    describes = ("Reads every sheet, finds where the real tables are, fixes headers, merged cells, gap rows and "
                 "footers, and gives each table permanent row ids. Anything it is unsure about is flagged for you.")
    uses_llm = True
    review_name = REVIEW

    # ------------------------------------------------------------------ run
    def _job(self, ctx: Context, out: Path, params: dict, config: dict) -> dict:
        paths = ctx.paths
        files = collect_inputs(paths.inputs)
        if not files:
            raise PhaseError("no_input", "This run has no input files.")
        emit = ctx.emit

        def on_stage(stage: str, pct: float | None, detail: str | None = None) -> None:
            emit.stage(stage, pct, detail)

        job = {"files": files, "config": config, "output_dir": str(out),
               "progress": Progress(on_stage, lambda m: emit.log(m), ctx.cancelled)}
        pin = _llm_pin(params.get("llm"))
        if pin:
            job["llm"] = pin
        return job

    def run(self, ctx: Context, params: dict) -> dict:
        paths = ctx.paths
        wipe(paths.phase_a)
        wipe(paths.phase_a_state)
        paths.phase_a_state.mkdir(parents=True)
        config = structure_config(params.get("llm"))
        (paths.phase_a_state / CONFIG).write_text(json.dumps(config, indent=1), encoding="utf8")
        ctx.emit.stage("scanning", 0)
        try:
            run_structure(self._job(ctx, paths.phase_a, params, config))
        except (JobFailed, ConfigInvalid) as e:
            raise PhaseError(getattr(e, "code", "config_invalid"), getattr(e, "message", str(e))) from e
        return self.summary(ctx.run)

    # ------------------------------------------------------------------ review upload
    def review(self, ctx: Context, upload: Path, params: dict) -> dict:
        paths = ctx.paths
        prev = paths.phase_a
        inventory = read_json(prev / "inventory.json")
        if inventory is None:
            raise PhaseError("not_run", "Phase A has not produced an output to review yet.")
        config = read_json(paths.phase_a_state / CONFIG, {})
        try:
            st = resolve_settings(config)
        except ConfigInvalid as e:
            raise PhaseError("config_invalid", str(e)) from e
        edits_path = paths.phase_a_state / EDITS
        existing = load_edits(edits_path if edits_path.exists() else None)
        ctx.emit.stage("checking the uploaded workbook", 0)
        pasted: dict = {}
        patch, edits, violations = import_review(str(upload), prev, inventory, st, existing, added=pasted)
        if violations:
            raise ReviewRejected([v.as_dict() for v in violations[:200]])
        nxt = paths.run / "phase_a_next"
        wipe(nxt)
        job = self._job(ctx, nxt, params, config)
        job.update(previous_dir=str(prev), edits=edits, settings_patch=lambda s: apply_patch(s, patch))
        try:
            run_structure(job)
        except (JobFailed, ConfigInvalid) as e:
            wipe(nxt)
            raise PhaseError(getattr(e, "code", "config_invalid"), getattr(e, "message", str(e))) from e
        kept = merge_added(read_json(prev / ADDED, {}), pasted)       # columns pasted on an earlier upload stay
        wipe(prev)
        os.replace(nxt, prev)
        if kept:
            (prev / ADDED).write_text(json.dumps(kept), encoding="utf8")     # Phase B joins them to the tables on Row_ID
        write_edits(paths.phase_a_state / EDITS, edits)
        (paths.phase_a_state / SELECTION).unlink(missing_ok=True)     # the workbook's Use column now decides
        merged = apply_patch(st, patch)
        config.setdefault("structure", {})
        config["structure"]["overrides"] = merged.overrides
        config["structure"]["selection"] = merged.selection
        (paths.phase_a_state / CONFIG).write_text(json.dumps(config, indent=1), encoding="utf8")
        copy_version(upload, paths.uploads, "A")
        return {**self.summary(ctx.run), "applied": {"overrides": len(patch.get("overrides", [])),
                                                      "selection_changed": "selection" in patch,
                                                      "cell_edits_total": len(edits),
                                                      "cell_edits_new": patch.get("cell_edits", {}).get("new", 0),
                                                      "columns_added": sum(len(c) for c in pasted.values())}}

    def clear(self, run: Path) -> None:
        p = Paths(run)
        wipe(p.phase_a), wipe(p.phase_a_state), wipe(p.run / "phase_a_next")

    # ------------------------------------------------------------------ table choice
    def select(self, run: Path, refs: list[str]) -> None:
        """Keep only `refs` for the next phases (the analyst's choice on the Phase A screen)."""
        known = {t["ref"] for t in self.summary(run).get("tables", [])}
        unknown = sorted(set(refs) - known)
        if unknown:
            raise PhaseError("unknown_table", f"Phase A found no table {', '.join(unknown)}.")
        if not refs:
            raise PhaseError("nothing_selected", "Keep at least one table: the next phases need something to clean.")
        ordered = [r for r in (t["ref"] for t in self.summary(run)["tables"]) if r in set(refs)]
        (Paths(run).phase_a_state / SELECTION).write_text(json.dumps({"tables": ordered}), encoding="utf8")

    # ------------------------------------------------------------------ reading results
    def can_continue(self, run: Path) -> tuple[bool, str | None]:
        if not any(t["selected"] for t in self.summary(run).get("tables", [])):
            return False, "No table is selected, so there is nothing to clean. Tick at least one table, or fix the blocking issues."
        return True, None

    def summary(self, run: Path) -> dict:
        out = Paths(run).phase_a
        report = read_json(out / "report.json")
        if report is None:
            return {}
        inv = read_json(out / "inventory.json", {})
        ex = report["extras"]["structure"]
        items = read_json(out / "action_items.json", [])
        chosen = chosen_tables(run)
        selected = set(chosen) if chosen is not None else {r for s in inv.get("selection", []) for r in s.get("tables", [])}
        tables = []
        for f in inv.get("files", []):
            facts = read_json(out / "files" / f["file_key"] / "file.json", {})
            for sh in facts.get("sheets", []):
                for t in sh.get("tables", []):
                    tables.append({"ref": t["table_ref"], "file": f["relative_path"], "sheet": sh["name"],
                                   "range": t["range"], "rows": t["n_rows"], "columns": len(t["columns"]),
                                   "selected": t["table_ref"] in selected, "decided_by": t.get("decided_by")})
            if facts.get("parquet"):
                p = facts["parquet"]
                ref = f"{f['file_key']}:0:t1"
                tables.append({"ref": ref, "file": f["relative_path"], "sheet": None, "range": None,
                               "rows": p["rows"], "columns": len(p["columns"]), "selected": ref in selected,
                               "decided_by": "parquet"})
        review = out / "review" / REVIEW
        issues, changes = _sheet_rows(review, "Issues", ISSUE_LIMIT), _sheet_rows(review, "Changes", CHANGE_LIMIT)
        by_status: dict[str, int] = {}
        for i in issues["rows"]:
            by_status[i["Status"]] = by_status.get(i["Status"], 0) + 1
        sev = {k: sum(1 for a in items if a["severity"] == k) for k in ("block", "warn", "info")}
        return {"files": ex["files"], "sheets": ex["sheets"], "tables_found": ex["tables"],
                "selected_tables": sum(t["selected"] for t in tables), "rows_in": report["counts"]["rows_in"],
                "rows_out": report["counts"]["rows_out"], "severity": sev,
                "blocking": [a["message"] for a in items if a["severity"] == "block"],
                "issue_counts": by_status, "issues": issues["rows"], "issues_total": issues["total"],
                "changes": changes["rows"], "changes_total": changes["total"], "tables": tables,
                "llm": ex["llm"]}

    def artifacts(self, run: Path) -> list[Artifact]:
        out = Paths(run).phase_a
        spec = [
            ("structural-review", "Structural_Review.xlsx", out / "review" / REVIEW, "review",
             "The fixed tables plus every issue found and every change made. Edit it to correct a table, then upload.", True),
            ("structure-report", "report.json", out / "report.json", "audit", "Counts, rules applied and every finding.", False),
            ("structure-inventory", "inventory.json", out / "inventory.json", "audit", "Files, sheets, tables and the selection.", False),
            ("structure-actions", "action_items.json", out / "action_items.json", "audit", "What needs the analyst's attention.", False),
            ("structure-llm-decisions", "llm_decisions.jsonl", out / "llm" / "decisions.jsonl", "audit",
             "Every question the local model was asked and how it was decided.", False),
        ]
        return [Artifact(i, label, p, kind, "A", desc, primary) for i, label, p, kind, desc, primary in spec
                if _nonempty(p)]


def _nonempty(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _sheet_rows(workbook: Path, sheet: str, limit: int) -> dict:
    """The first `limit` rows of a read-only sheet of the review workbook, as dicts keyed by its header."""
    if not workbook.exists():
        return {"rows": [], "total": 0}
    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    try:
        if sheet not in wb.sheetnames:
            return {"rows": [], "total": 0}
        it = wb[sheet].iter_rows(values_only=True)
        header = next(it, None)
        if not header:
            return {"rows": [], "total": 0}
        rows, total = [], 0
        for r in it:
            if not any(v is not None for v in r):
                continue
            total += 1
            if len(rows) < limit:
                rows.append({h: ("" if v is None else v if isinstance(v, (int, float)) else str(v))
                             for h, v in zip(header, r)})
        return {"rows": rows, "total": total}
    finally:
        wb.close()
