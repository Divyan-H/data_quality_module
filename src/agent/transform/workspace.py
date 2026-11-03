"""Phase T's data, seen by the agent: the run's checked tables, a recipe of steps that reshapes them, and the result.

    <run>/config/T_recipe.json          the recipe: an ordered list of typed operations (kept when earlier phases re-run)
    <run>/work/phase_t_transform/       the output: tables/<folder>/data.parquet, run.json (tables + step log),
                                        Transform_Report.xlsx, recipe.json (a copy, for the Results page)

Running the phase replays the whole recipe on fresh inputs, so a re-checked month gets the same transformations.
The agent adds steps: a preview runs them on the current output, Apply appends them to the recipe and saves the
new output (snapshot first, for Undo). Recipes can be saved to a library and used again on another run.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import openpyxl
import polars as pl
from pydantic import TypeAdapter

from ..errors import AgentError, StaleProposal
from ..operations import Operation
from ..workspace import Workspace
from . import engine
from .describe import step_line, transform_summary

OPS = TypeAdapter(list[Operation])
REPORT = "Transform_Report.xlsx"
REPORT_ROWS = 5000


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:60] or "table"


class FrameTable:
    """A Phase T table in the shape the planner context reads (columns, kinds, values as text)."""

    def __init__(self, name: str, df: pl.DataFrame, source_file: str | None = None):
        self.ref = self.sheet = name
        self.source_file = source_file
        self.cols = engine.data_cols(df)
        self.types = {c: ("numeric" if engine.is_numeric(df, c) else "date" if engine.is_date(df, c) else "text") for c in self.cols}
        self.data = {c: [engine.render(v) for v in df[c].to_list()] for c in self.cols}
        self.data["_row_id"] = df["_row_id"].to_list() if "_row_id" in df.columns else [None] * df.height
        self.review: list[dict] = []
        self.audit: list[dict] = []


class RecipeLibrary:
    """Saved recipes shared by every run: <workspace>/../recipes/<name>.json."""

    def __init__(self, folder: Path | None):
        self.folder = Path(folder) if folder else None

    def _path(self, name: str) -> Path:
        if self.folder is None:
            raise AgentError("no_library", "Saving recipes is not set up for this service.")
        slug = safe(name.strip()).lower()
        if not slug or slug == "table":
            raise AgentError("bad_name", "Give the recipe a name made of letters or digits.")
        return self.folder / f"{slug}.json"

    def names(self) -> list[str]:
        if self.folder is None or not self.folder.exists():
            return []
        return sorted(json.loads(p.read_text("utf8")).get("name", p.stem) for p in self.folder.glob("*.json"))

    def save(self, name: str, ops: list[dict], source: str) -> Path:
        p = self._path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"name": name.strip(), "steps": ops, "saved_from": source}, indent=1), encoding="utf8")
        return p

    def load(self, name: str) -> list:
        p = self._path(name)
        if not p.exists():
            have = self.names()
            raise AgentError("no_such_recipe", f"There is no saved recipe '{name}'."
                             + (f" Saved recipes: {', '.join(have)}." if have else " None is saved yet."))
        return OPS.validate_python(json.loads(p.read_text("utf8"))["steps"])


class TransformWorkspace(Workspace):
    phase = "T"
    supports_recipes = True

    def __init__(self, out: Path, recipe: Path, inputs: list[dict], library: Path | None = None):
        """`inputs`: [{"name", "path", "source_file", "sheet", "ref"}] - the checked tables, in order."""
        self.out, self.recipe_path, self.inputs = Path(out), Path(recipe), inputs
        self.library = RecipeLibrary(library)
        if not inputs:
            raise AgentError("not_ready", "There are no checked tables to transform yet.")

    # ── state ────────────────────────────────────────────────────────────────
    def version(self) -> str:
        h = hashlib.sha256()
        for p in (self.out / "run.json", self.recipe_path):
            st = p.stat() if p.exists() else None
            h.update(f"{p.name}:{st.st_size if st else -1}:{st.st_mtime_ns if st else -1};".encode())
        return h.hexdigest()[:16]

    def recipe(self) -> list:
        if not self.recipe_path.exists():
            return []
        return OPS.validate_python(json.loads(self.recipe_path.read_text("utf8")).get("steps", []))

    def _write_recipe(self, ops: list) -> None:
        self.recipe_path.parent.mkdir(parents=True, exist_ok=True)
        self.recipe_path.write_text(json.dumps({"steps": [o.model_dump(exclude_defaults=False) for o in ops]}, indent=1),
                                    encoding="utf8")

    def _index(self) -> dict | None:
        try:
            return json.loads((self.out / "run.json").read_text("utf8"))
        except (OSError, ValueError):
            return None

    def read_inputs(self) -> engine.Tables:
        tables: engine.Tables = {}
        for t in self.inputs:
            df = pl.read_parquet(t["path"])
            keep = [c for c in df.columns if not c.startswith("_") or c in engine.SYS]
            tables[t["name"]] = df.select(keep)
        return tables

    def load_frames(self) -> engine.Tables:
        index = self._index()
        if index is None:
            return self.read_inputs()
        return {t["name"]: pl.read_parquet(self.out / "tables" / t["folder"] / "data.parquet") for t in index["tables"]}

    def load(self) -> list[FrameTable]:
        meta = {t["name"]: t.get("source_file") for t in self.inputs}
        return [FrameTable(n, df, meta.get(n)) for n, df in self.load_frames().items()]

    # ── running steps ────────────────────────────────────────────────────────
    @staticmethod
    def _run(tables: engine.Tables, ops: list, changeset: str | None, start: int) -> tuple[list[dict], dict | None]:
        steps, failed = [], None
        for k, op in enumerate(ops):
            try:
                effect = engine.apply_op(tables, op)
            except AgentError as e:
                failed = {"step": start + k + 1, "text": step_line(op), "message": e.message, "code": e.code}
                break
            steps.append({"step": start + k + 1, "op": op.model_dump(), "text": step_line(op),
                          "summary": transform_summary(effect), "effect": effect, "changeset": changeset})
        return steps, failed

    def run_recipe(self) -> dict:
        """The phase run: every recipe step on fresh inputs. A step that cannot run stops the replay (the steps before
        it are kept); the analyst fixes or undoes it from the chat."""
        tables = self.read_inputs()
        steps, failed = self._run(tables, self.recipe(), None, 0)
        self._save(tables, steps, failed)
        return self._index()

    def preview(self, ops: list, changeset_id: str, instruction: str | None) -> dict:
        tables = self.load_frames()
        index = self._index() or {"steps": []}
        effects = []
        for op in ops:
            effects.append(engine.apply_op(tables, op))                      # AgentError -> the chat shows it
        return {"kind": "transform",
                "operations": [{"text": step_line(op), "summary": transform_summary(e), "effect": e} for op, e in zip(ops, effects)],
                "summary": "; ".join(transform_summary(e) for e in effects),
                "steps_before": len(index.get("steps", [])),
                "tables_after": [{"name": n, "rows": df.height, "columns": len(engine.data_cols(df))} for n, df in tables.items()],
                "changes_anything": True}

    def apply(self, ops: list, changeset_id: str, instruction: str | None, expected_version: str,
              snapshot_to: Path | None) -> dict:
        if self.version() != expected_version:
            raise StaleProposal()
        tables = self.load_frames()
        index = self._index() or {"steps": []}
        if index.get("failed"):
            raise AgentError("recipe_failed", f"Step {index['failed']['step']} of the recipe does not run on this data "
                                              f"({index['failed']['message']}). Undo it before adding steps.")
        done = list(index.get("steps", []))
        steps, failed = self._run(tables, ops, changeset_id, len(done))
        if failed:
            raise AgentError(failed["code"], failed["message"])
        if snapshot_to is not None:
            self._snapshot(snapshot_to)
        self._save(tables, done + steps, None)
        self._write_recipe(self.recipe() + list(ops))
        return {"summary": "; ".join(s["summary"] for s in steps), "steps": len(done) + len(steps),
                "tables": [{"name": n, "rows": df.height} for n, df in tables.items()], "follow_up_fixes": 0,
                "open_items_after": 0}

    # ── output ───────────────────────────────────────────────────────────────
    def _save(self, tables: engine.Tables, steps: list[dict], failed: dict | None) -> None:
        meta = {t["name"]: t for t in self.inputs}
        tdir = self.out / "tables"
        shutil.rmtree(tdir, ignore_errors=True)
        entries, used = [], set()
        for name, df in tables.items():
            folder, n = safe(name), 1
            while folder.lower() in used:
                n += 1
                folder = f"{safe(name)}_{n}"
            used.add(folder.lower())
            (tdir / folder).mkdir(parents=True, exist_ok=True)
            df.write_parquet(tdir / folder / "data.parquet")
            m = meta.get(name, {})
            entries.append({"name": name, "folder": folder, "rows": df.height, "columns": engine.data_cols(df),
                            "source_file": m.get("source_file"), "from_input": name in meta})
        index = {"tables": entries, "steps": steps, "failed": failed,
                 "inputs": [{"name": t["name"], "source_file": t.get("source_file"), "sheet": t.get("sheet")} for t in self.inputs]}
        (self.out / "run.json").write_text(json.dumps(index, ensure_ascii=False, default=str, indent=1), encoding="utf8")
        (self.out / "recipe.json").write_text(json.dumps({"steps": [s["op"] for s in steps]}, indent=1, default=str), encoding="utf8")
        self._report(tables, index)

    def _report(self, tables: engine.Tables, index: dict) -> None:
        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet("Steps")
        ws.append(["Step", "What it does", "Result", "Notes", "Change"])
        for s in index["steps"]:
            ws.append([s["step"], s["text"], s["summary"], " ".join(s["effect"]["notes"]), s.get("changeset")])
        if index.get("failed"):
            f = index["failed"]
            ws.append([f["step"], f["text"], "FAILED", f["message"], None])
        ws = wb.create_sheet("Tables")
        ws.append(["Table", "Rows", "Columns", "From the checked data"])
        for t in index["tables"]:
            ws.append([t["name"], t["rows"], ", ".join(t["columns"]), "yes" if t["from_input"] else "made here"])
        for n, (name, df) in enumerate(tables.items(), 1):
            ws = wb.create_sheet(f"Data_{n}")
            cols = engine.data_cols(df)
            ws.append(cols)
            for row in df.select(cols).head(REPORT_ROWS).iter_rows():
                ws.append([engine.render(v) if not isinstance(v, (int, float)) else v for v in row])
            if df.height > REPORT_ROWS:
                ws.append([f"… {df.height - REPORT_ROWS:,} more rows in the final files"])
        wb.save(self.out / REPORT)

    # ── undo ─────────────────────────────────────────────────────────────────
    def _snapshot(self, dest: Path) -> None:
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        if self.out.exists():
            shutil.copytree(self.out, dest / "out")
        if self.recipe_path.exists():
            shutil.copy2(self.recipe_path, dest / "recipe.json")

    def restore(self, snapshot: Path) -> None:
        if not snapshot.exists():
            raise AgentError("no_snapshot", "The saved state for this change is missing; it cannot be undone.")
        shutil.rmtree(self.out, ignore_errors=True)
        if (snapshot / "out").exists():
            shutil.copytree(snapshot / "out", self.out)
        if (snapshot / "recipe.json").exists():
            shutil.copy2(snapshot / "recipe.json", self.recipe_path)
        else:
            self.recipe_path.unlink(missing_ok=True)

    # ── recipes ──────────────────────────────────────────────────────────────
    def save_recipe(self, name: str, source: str) -> str:
        steps = [o.model_dump() for o in self.recipe()]
        if not steps:
            raise AgentError("empty_recipe", "The recipe has no steps yet; nothing to save.")
        self.library.save(name, steps, source)
        return f"Saved the recipe ({len(steps)} step(s)) as '{name.strip()}'. Say \"use recipe {name.strip()}\" on another run to apply it."


class TransformViews:
    """Read-only data for the agent screen in Phase T: the recipe's steps, the output tables, pages of rows."""

    def __init__(self, out: Path):
        self.out = Path(out)
        try:
            self.index = json.loads((self.out / "run.json").read_text("utf8"))
        except (OSError, ValueError) as e:
            raise AgentError("not_ready", "Phase T has not run yet.") from e

    def issues(self) -> dict:
        tables = [{"ref": t["name"], "file": t.get("source_file"), "sheet": t["name"], "rows": t["rows"], "columns": t["columns"]}
                  for t in self.index["tables"]]
        steps = [{k: s.get(k) for k in ("step", "text", "summary", "changeset")} | {"notes": s["effect"]["notes"]}
                 for s in self.index["steps"]]
        return {"open": 0, "resolved": 0, "resolved_by_agent": 0, "groups": [], "tables": tables, "steps": steps,
                "failed": self.index.get("failed")}

    def rows(self, table: str, offset: int = 0, limit: int = 100, rule=None, column=None, only=None) -> dict:
        entry = next((t for t in self.index["tables"] if t["name"] == table), None)
        if entry is None:
            raise AgentError("no_such_table", f"There is no table {table} in Phase T's output.")
        limit = max(1, min(limit, 500))
        df = pl.read_parquet(self.out / "tables" / entry["folder"] / "data.parquet")
        cols = engine.data_cols(df)
        page = df.slice(offset, limit)
        ids = page["_row_id"].to_list() if "_row_id" in page.columns else [None] * page.height
        src = page["_source_row"].to_list() if "_source_row" in page.columns else [None] * page.height
        rows = [{"row_id": rid, "excel_row": s, "values": {c: engine.render(v) for c, v in zip(cols, vals)},
                 "changed": {}, "flagged": {}} for rid, s, vals in zip(ids, src, page.select(cols).iter_rows())]
        return {"table": table, "columns": cols, "total": df.height, "offset": offset, "limit": limit, "rows": rows,
                "column_items": []}
