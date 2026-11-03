"""Which parquet holds each final table of a run. Shared by the Results page (bundle) and Phase T, whose inputs are
exactly the tables the checking phases hand on."""
from __future__ import annotations

import json
from pathlib import Path

from src.common import units
from src.common.dataset import phase_b_refs
from src.trends.weekly import tables as weekly_tables

from .phases.base import Paths, read_json

TRANSFORM_OUT = "phase_t_transform"


def checked_sources(run: Path) -> list[tuple[dict, Path, str]]:
    """(table, the parquet to publish, phase that wrote it) for every table Phase B cleaned, in Phase B's order."""
    work = Paths(run).work
    b = work / "phase_b_hygiene"
    if not (b / "run.json").exists():
        return []
    later = [(ph, {e["table_ref"]: folder for e, folder in units.checked(work / folder)})
             for ph, folder in (("D", "phase_d_trends"), ("C", "phase_c_timeseries"))]
    out = []
    for ref in phase_b_refs(b):
        stem = units.stem(ref)
        rep = read_json(b / "quality_reports" / f"{stem}.json", {})
        phase, path = next(((ph, done[ref] / "tables" / stem / "data.parquet") for ph, done in later if ref in done),
                           ("B", b / "tables" / stem / "data.parquet"))
        out.append(({"table_ref": ref, "sheet": rep.get("sheet"), "source_file": rep.get("source_file")}, path, phase))
    for meta, folder in weekly_tables(work / "phase_d_trends"):      # a monthly table also published as weekly (Phase D)
        stem = units.stem(meta["weekly_ref"])
        out.append(({"table_ref": meta["weekly_ref"], "sheet": f"{meta.get('sheet') or Path(meta.get('source_file') or 'table').stem}_weekly",
                     "source_file": meta.get("source_file")}, folder / "tables" / stem / "data.parquet", "D"))
    return out


def transformed_sources(run: Path) -> list[tuple[dict, Path, str]]:
    """Phase T's output tables, when it has run (its folder is removed whenever an earlier phase changes)."""
    out = Paths(run).work / TRANSFORM_OUT
    try:
        index = json.loads((out / "run.json").read_text("utf8"))
    except (OSError, ValueError):
        return []
    return [({"table_ref": f"T:{t['name']}", "sheet": t["name"], "source_file": t.get("source_file")},
             out / "tables" / t["folder"] / "data.parquet", "T") for t in index.get("tables", [])]
