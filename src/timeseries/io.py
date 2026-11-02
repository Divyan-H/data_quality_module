"""Phase C's input: the tables of a Phase B run folder (or one csv / xlsx / parquet file). Loading and writing the table
is shared with Phase D (src/common/dataset.py)."""
from __future__ import annotations

from pathlib import Path

from src.common.dataset import iter_phase_b, load_loose
from src.common.weekly_rollup import to_weekly




def _tables(p: Path, sheet: str | None):
    if not p.exists():
        raise FileNotFoundError(f"input not found: {p}")
    if not p.is_dir():
        return load_loose(p, sheet, "a Phase B run folder")
    folder = next((f for f in (p / "phase_b_hygiene", p) if (f / "run.json").exists() and (f / "tables").is_dir()), None)
    if folder is None:
        raise FileNotFoundError(f"{p} is not a Phase B run folder (no phase_b_hygiene/run.json): give the -o "
                                "folder of Phase B, or one csv / xlsx / parquet file")
    return (d for d in iter_phase_b(folder) if not sheet or d.sheet == sheet)


def load_candidates(path, sheet: str | None = None, cfg: dict | None = None, rollup: dict | None = None,
                    with_log: bool = False):
    """The tables Phase C may check, one at a time: every table of a Phase B run folder (the run folder or its
    phase_b_hygiene/), or every non-empty sheet of one csv / xlsx / parquet file. `sheet` keeps only that sheet's.

    With `cfg` (the Phase C settings) a table whose time level is monthly arrives as a weekly table (common/weekly_rollup.py),
    `rollup` being the Column Setup choices (column -> sum | average). Every other table is returned as it was read:
    daily and weekly data are never touched. `with_log` yields (table, roll-up log or None) instead of the table."""
    for ds in _tables(Path(path), sheet):
        log = None
        if cfg is not None and cfg.get("monthly_to_weekly", True):
            done = to_weekly(ds, cfg["dayfirst"], rollup, cfg.get("average_words"))
            if done is not None:
                ds, log = done
        yield (ds, log) if with_log else ds
