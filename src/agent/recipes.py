"""Saved recipes: named lists of typed steps, shared by every run, so next month's file gets this month's fixes and
transformations in one previewed change.

    <workspace>/../recipes/<slug>.json   {"name", "phase", "steps": [operation, ...], "saved_from"}

A recipe belongs to the phase it was built in. Phase B recipes are the value fixes of a run's applied change-sets,
without the steps that name Excel row numbers (row 14 of this month's file is a different row next month). Phase T
recipes are the transformation recipe itself.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import TypeAdapter

from .errors import AgentError
from .operations import Operation

OPS = TypeAdapter(list[Operation])


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip()).strip("_")[:60].lower()


def row_specific(op) -> bool:
    """A step that names Excel rows: it fits this file only."""
    where = getattr(op, "where", None)
    return bool(where is not None and hasattr(where, "rows") and (where.rows or where.exclude_rows))


def replayable(ops: list) -> tuple[list, list]:
    """(steps that can run on another file, steps that cannot)."""
    keep = [op for op in ops if not row_specific(op)]
    return keep, [op for op in ops if row_specific(op)]


class RecipeLibrary:
    def __init__(self, folder: Path | str | None):
        self.folder = Path(folder) if folder else None

    def _path(self, name: str) -> Path:
        if self.folder is None:
            raise AgentError("no_library", "Saving recipes is not set up for this service.")
        slug = _slug(name)
        if not slug:
            raise AgentError("bad_name", "Give the recipe a name made of letters or digits.")
        return self.folder / f"{slug}.json"

    def entries(self) -> list[dict]:
        if self.folder is None or not self.folder.exists():
            return []
        out = []
        for p in sorted(self.folder.glob("*.json")):
            try:
                d = json.loads(p.read_text("utf8"))
            except (OSError, ValueError):
                continue
            out.append({"name": d.get("name", p.stem), "phase": d.get("phase", "T"), "steps": len(d.get("steps", []))})
        return out

    def names(self, phase: str | None = None) -> list[str]:
        return [e["name"] for e in self.entries() if phase is None or e["phase"] == phase]

    def save(self, name: str, ops: list[dict], source: str, phase: str = "T") -> Path:
        p = self._path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"name": name.strip(), "phase": phase, "steps": ops, "saved_from": source}, indent=1,
                                default=str), encoding="utf8")
        return p

    def load(self, name: str, phase: str | None = None) -> list:
        p = self._path(name)
        if not p.exists():
            have = self.names(phase)
            raise AgentError("no_such_recipe", f"There is no saved recipe '{name}'."
                             + (f" Saved recipes here: {', '.join(have)}." if have else " None is saved for this phase yet."))
        data = json.loads(p.read_text("utf8"))
        built_in = data.get("phase", "T")
        if phase is not None and built_in != phase:
            raise AgentError("wrong_phase", f"'{data.get('name', name)}' is a Phase {built_in} recipe; use it in Phase {built_in}.")
        return OPS.validate_python(data["steps"])
