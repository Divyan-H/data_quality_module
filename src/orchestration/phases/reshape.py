"""Reshaping inside Phase A: the optional recipe that changes the shape of the tables Phase A found, before the
checking phases see them (`src/agent/transform`).

    phase_a/            what Phase A detected (never touched here)
    phase_a_shaped/     the recipe's result: tables/, run.json, lineage.parquet, Transform_Report.xlsx
    config/A_recipe.json  the steps; kept when Phase A is run again, so the same steps replay on the new tables

Phase B loads the result through `src/hygiene/io.py::shaped_tables`: tables the recipe did not touch are read from
`phase_a/` exactly as before. The recipe is built in the chat on Phase A's review screen ("Reshape with agent").
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.common import units

from .base import Artifact, Paths, PhaseError, read_json, wipe

SHAPED = "phase_a_shaped"
RECIPE = "A_recipe.json"
LEGACY_RECIPE = "T_recipe.json"            # runs made when transformations were a phase of their own, after Phase D
REPORT = "Transform_Report.xlsx"


def out(run: Path) -> Path:
    return Paths(run).run / SHAPED


def recipe_path(run: Path) -> Path:
    """The run's recipe file. A recipe written by the old Phase T is taken over once (its steps name the same tables)."""
    config = Paths(run).config
    path, legacy = config / RECIPE, config / LEGACY_RECIPE
    if not path.exists() and legacy.exists():
        os.replace(legacy, path)
    return path


def has_steps(run: Path) -> bool:
    return bool((read_json(recipe_path(run)) or {}).get("steps"))


def inputs(run: Path, tables: list[dict]) -> list[dict]:
    """The selected Phase A tables as the recipe's starting point. `tables` is Phase A's summary list
    ({ref, file, sheet, selected}); names are the ones the final files use (sheet name, made unique)."""
    paths: dict[str, Path] = {}
    for p in sorted(Paths(run).phase_a.glob("**/tables/*/data.parquet")):
        cols = read_json(p.parent / "columns.json") or {}
        paths[cols.get("table_ref", p.parent.name)] = p
    chosen = [t for t in tables if t.get("selected") and t["ref"] in paths]
    names = units.file_names([{"table_ref": t["ref"], "sheet": t.get("sheet"), "source_file": t.get("file")} for t in chosen])
    return [{"name": names[t["ref"]], "path": str(paths[t["ref"]]), "source_file": t.get("file"), "sheet": t.get("sheet"),
             "ref": t["ref"]} for t in chosen]


def workspace(run: Path, tables: list[dict], params: dict):
    from src.agent.transform.workspace import TransformWorkspace
    found = inputs(run, tables)
    if not found:
        raise PhaseError("not_ready", "Phase A has no selected table to reshape.")
    return TransformWorkspace(out(run), recipe_path(run), found, params.get("recipes_dir"))


def replay(run: Path, tables: list[dict], params: dict) -> None:
    """Run the recipe (possibly empty) on Phase A's current tables. A step that no longer fits stops the replay; the
    result then says which step, and Phase A cannot be accepted until it is undone or replaced."""
    from src.agent.errors import AgentError
    wipe(out(run))
    if not inputs(run, tables):
        return                                               # nothing selected: Phase A itself reports that
    out(run).mkdir(parents=True, exist_ok=True)
    try:
        workspace(run, tables, params).run_recipe()
    except AgentError as e:
        raise PhaseError(e.code, e.message) from e


def summary(run: Path) -> dict | None:
    """What the recipe did, for Phase A's summary; None when no step has been applied."""
    index = read_json(out(run) / "run.json")
    if index is None or not (index.get("steps") or index.get("failed")):
        return None
    return {"tables": [{"name": t["name"], "rows": t["rows"], "columns": len(t["columns"]), "made_here": not t["from_input"],
                        "changed": t.get("changed", True)} for t in index["tables"]],
            "steps": [{"step": s["step"], "text": s["text"], "summary": s["summary"], "notes": s["effect"]["notes"]}
                      for s in index["steps"]],
            "failed": index.get("failed")}


def blocked(run: Path) -> str | None:
    f = (read_json(out(run) / "run.json") or {}).get("failed")
    if f:
        return (f"Step {f['step']} of the reshaping recipe does not run on these tables: {f['message']} "
                "Open Reshape with agent and undo or change it first.")
    return None


def artifacts(run: Path) -> list[Artifact]:
    if summary(run) is None:
        return []
    folder = out(run)
    spec = [("reshape-report", REPORT, folder / REPORT, "review",
             "Every reshaping step with its result, and the first rows of each resulting table.", False),
            ("reshape-recipe", "recipe.json", folder / "recipe.json", "config",
             "The reshaping steps, as data: save it in the chat ('save recipe as …') to use it on another run.", False),
            ("reshape-lineage", "lineage.parquet", folder / "lineage.parquet", "audit",
             "For every row of a reshaped table: the row of the source table it came from.", False)]
    return [Artifact(i, label, p, kind, "A", desc, primary) for i, label, p, kind, desc, primary in spec if p.exists()]


def dump_recipe(run: Path) -> list[dict]:
    return json.loads(recipe_path(run).read_text("utf8")).get("steps", []) if recipe_path(run).exists() else []
