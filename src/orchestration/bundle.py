"""The Results page: every final file of a run, and one zip with all of them.

Final data is one file set per table the analyst kept: the table as Phase T (Transformations) left it when T ran,
else as the last phase that checked it left it (D, else C, else B), with only its data columns - ready to load. Each row's lineage (permanent Row_ID and the Excel row it came
from) is kept beside it as an audit file. The reports, audit files, configs and logs are the phases' own outputs.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import openpyxl
import polars as pl
import pyarrow.parquet as papq

from src.common import units
from src.common.columns import SYSTEM
from src.storage.run_store import now

from .phases import ORDER, PHASES
from .phases.base import Artifact, Paths, read_json
from .sources import checked_sources, transformed_sources

XLSX_MAX_ROWS = 100_000
STAMP = ".built.json"


def sources(run: Path) -> list[tuple[dict, Path, str]]:
    """(table, the parquet to publish, phase that wrote it) for every final table: Phase T's outputs when it ran,
    else every table Phase B cleaned as the last checking phase left it (see src/orchestration/sources.py)."""
    return transformed_sources(run) or checked_sources(run)


def _export_one(parquet: Path, name: str, final: Path, phase: str, table: dict) -> None:
    """Streamed, batch by batch: this runs in the API process, so a table is never loaded whole."""
    source = papq.ParquetFile(parquet)
    columns = [c for c in source.schema_arrow.names if c not in SYSTEM]
    scan = pl.scan_parquet(parquet)
    scan.select(columns).sink_csv(final / f"{name}.csv")
    scan.select(columns).sink_parquet(final / f"{name}.parquet", compression="zstd")
    (final / "lineage").mkdir(exist_ok=True)
    scan.select([c for c in ("_row_id", "_source_row") if c in source.schema_arrow.names]).with_columns(
        pl.lit(table["source_file"], pl.String).alias("source_file"), pl.lit(table["sheet"], pl.String).alias("sheet"),
        pl.lit(phase).alias("last_phase")).sink_parquet(final / "lineage" / f"{name}.parquet")
    if source.metadata.num_rows <= XLSX_MAX_ROWS:
        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet("data")
        ws.append(columns)
        for batch in source.iter_batches(batch_size=10_000, columns=columns):
            for row in zip(*(c.to_pylist() for c in batch.columns)):
                ws.append([None if v is None else (v if isinstance(v, (int, float, str, bool)) else str(v)) for v in row])
        wb.save(final / f"{name}.xlsx")


def export_data(run: Path) -> list[Artifact]:
    """Build the final data files under final/ (again only when a source table changed), and list them."""
    final = Paths(run).final
    final.mkdir(exist_ok=True)
    found = sources(run)
    names = units.file_names([t for t, _, _ in found])
    want = {names[t["table_ref"]]: [str(p), p.stat().st_mtime_ns, ph] for t, p, ph in found if p.exists()}
    stamp = final / STAMP
    if read_json(stamp) != want:
        for f in [*final.glob("*.csv"), *final.glob("*.parquet"), *final.glob("*.xlsx"), *final.glob("lineage/*")]:
            f.unlink()
        for t, p, ph in found:
            if p.exists():
                _export_one(p, names[t["table_ref"]], final, ph, t)
        stamp.write_text(json.dumps(want), encoding="utf8")
    arts: list[Artifact] = []
    for t, p, ph in found:
        name = names[t["table_ref"]]
        rows = papq.ParquetFile(p).metadata.num_rows if p.exists() else 0
        where = f"{t['source_file'] or '-'} / {t['sheet'] or '-'}"
        arts += [Artifact(f"data-{name}-{ext}", f"{name}.{ext}", final / f"{name}.{ext}", "data", ph,
                          f"{where}: {rows:,} rows, data columns only" + (" (as a workbook)" if ext == "xlsx" else ""))
                 for ext in ("csv", "parquet", "xlsx") if (final / f"{name}.{ext}").exists()]
        arts.append(Artifact(f"lineage-{name}", f"lineage_{name}.parquet", final / "lineage" / f"{name}.parquet", "audit",
                             ph, f"{where}: each row's permanent Row_ID and the Excel row it came from, in file order."))
    return [a for a in arts if a.path.exists()]


def collect(run: Path) -> list[Artifact]:
    arts = export_data(run)
    for key in ORDER:
        arts += [a for a in PHASES[key].artifacts(run)]
    log = Paths(run).work / "pipeline.log"
    if log.exists():
        arts.append(Artifact("pipeline-log", "pipeline.log", log, "log", "B", "One line per step of Phases B to D."))
    for a in arts:
        a.id = a.id.lower()
    return arts


def group_of(a: Artifact) -> str:
    return {"data": "Final data", "review": "Reports", "audit": "Audit trail", "config": "Configs used", "log": "Logs"}[a.kind]


def build_zip(run: Path, name: str) -> Path:
    arts = collect(run)
    out = Paths(run).final / "data_quality_results.zip"
    files: list[dict] = []
    manifest = {"run": name, "built_at": now(), "files": files}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for a in arts:
            folder = {"data": "data", "review": "reports", "audit": "audit", "config": "config", "log": "logs"}[a.kind]
            arc = f"{folder}/{a.phase}_{a.label}" if a.kind != "data" else f"{folder}/{a.label}"
            z.write(a.path, arc)
            with a.path.open("rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            files.append({"path": arc, "phase": a.phase, "bytes": a.path.stat().st_size, "sha256": digest})
        z.writestr("MANIFEST.json", json.dumps(manifest, indent=1))
    return out
