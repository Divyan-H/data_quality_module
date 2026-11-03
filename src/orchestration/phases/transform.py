"""Phase T adapter: Transformations (`src/agent/transform`). The checked tables in, reshaped final tables out.

Optional: an analyst who needs no reshaping skips it, and the final files are the checked tables, as before. Its
recipe lives in the run's config folder, so when an earlier phase changes and T is cleared, running T again replays
the same steps on the new data.
"""
from __future__ import annotations

from pathlib import Path

from src.common import units
from src.orchestration.sources import TRANSFORM_OUT, checked_sources

from .base import Artifact, Context, Paths, Phase, PhaseError, read_json, wipe

REPORT = "Transform_Report.xlsx"
RECIPE = "T_recipe.json"


def inputs(run: Path) -> list[dict]:
    """The checked tables Phase T starts from, named the way the final files name them."""
    found = checked_sources(run)
    names = units.file_names([t for t, _, _ in found])
    return [{"name": names[t["table_ref"]], "path": str(p), "source_file": t.get("source_file"), "sheet": t.get("sheet"),
             "ref": t["table_ref"]} for t, p, _ in found if p.exists()]


class TransformPhase(Phase):
    key = "T"
    title = "Transformations"
    short = "Reshape and combine the checked data"
    describes = ("Optional. Reshape the checked tables for loading: turn Day 1…31 columns into rows, split multi-city rows "
                 "and share their numbers, add up by day and channel, net next-month returns, append history, look up "
                 "AOV, convert currency. You say what to do in the chat; each step is previewed and kept in a recipe that "
                 "runs again on next month's file. Skip it if the checked tables are already in the shape you load.")
    review_name = REPORT
    optional = True
    supports_agent = True

    @staticmethod
    def out(run: Path) -> Path:
        return Paths(run).work / TRANSFORM_OUT

    @staticmethod
    def recipe_path(run: Path) -> Path:
        return Paths(run).config / RECIPE

    def workspace(self, run: Path, params: dict):
        from src.agent.transform.workspace import TransformWorkspace
        found = inputs(run)
        if not found:
            raise PhaseError("not_ready", "There are no checked tables to transform. Accept Phase B first.")
        return TransformWorkspace(self.out(run), self.recipe_path(run), found, params.get("recipes_dir"))

    def run(self, ctx: Context, params: dict) -> dict:
        from src.agent.errors import AgentError
        wipe(self.out(ctx.run))
        self.out(ctx.run).mkdir(parents=True, exist_ok=True)
        ctx.emit.stage("running the recipe", 10)
        try:
            self.workspace(ctx.run, params).run_recipe()
        except AgentError as e:
            raise PhaseError(e.code, e.message) from e
        return self.summary(ctx.run)

    def review(self, ctx: Context, upload: Path, params: dict) -> dict:
        raise PhaseError("not_supported", "Phase T has no workbook to upload: its report is for reading. Change the "
                                          "recipe with Transform with agent.")

    def clear(self, run: Path) -> None:
        wipe(self.out(run))                                   # the recipe stays: running T again replays it

    def summary(self, run: Path) -> dict:
        index = read_json(self.out(run) / "run.json")
        if index is None:
            return {}
        return {"tables": [{"name": t["name"], "rows": t["rows"], "columns": len(t["columns"]), "made_here": not t["from_input"]}
                           for t in index["tables"]],
                "inputs": index.get("inputs", []),
                "steps": [{"step": s["step"], "text": s["text"], "summary": s["summary"], "notes": s["effect"]["notes"]}
                          for s in index["steps"]],
                "failed": index.get("failed")}

    def artifacts(self, run: Path) -> list[Artifact]:
        out = self.out(run)
        spec = [("transform-report", REPORT, out / REPORT, "review",
                 "Every step of the recipe with its result, and the first rows of each output table.", True),
                ("transform-recipe", "recipe.json", out / "recipe.json", "config",
                 "The steps, as data: load it on another run with 'use recipe' after saving it, or keep it for the record.", False)]
        return [Artifact(i, label, p, kind, "T", desc, primary) for i, label, p, kind, desc, primary in spec if p.exists()]

    def can_continue(self, run: Path) -> tuple[bool, str | None]:
        index = read_json(self.out(run) / "run.json") or {}
        f = index.get("failed")
        if f:
            return False, f"Step {f['step']} of the recipe does not run on this data: {f['message']} Undo or change it first."
        return True, None

    def agent_workspace(self, ctx: Context, params: dict):
        return self.workspace(ctx.run, params)

    def agent_views(self, run: Path):
        from src.agent.transform.workspace import TransformViews
        return TransformViews(self.out(run))
