"""Job Contract v1 for `data.structure` (spec 3.3, 11): maps a job body and storage onto `run_structure`.

Storage is a directory root here (the service runs locally, decision 10): every `ref` of the body is a path
below it. Events are handed to `emit`; `HttpEmitter` posts them to the body's `callback_url`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .config import ConfigInvalid, resolve_settings
from .errors import Cancelled, JobFailed
from .pipeline import Progress, run_structure
from .review_import import apply_patch, import_review
from .writer import dump_json, file_hash, hash_tree

EDIT_SCHEMA = pa.schema([(n, pa.string()) for n in
                         ("row_id", "column", "from", "to", "to_type", "edited_by", "edited_at", "review_upload")])
STAGES = ("scanning", "converting", "reading facts", "finding regions", "scoring headers", "LLM review",
          "writing tables", "writing review workbook", "finalizing")


class HttpEmitter:
    """Posts events to `callback_url` with the job token (Spec 02 6.2)."""

    def __init__(self, url: str, token: str):
        self.url, self.token = url, token

    def __call__(self, event: dict) -> None:
        req = urllib.request.Request(self.url, data=json.dumps(event).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except (urllib.error.URLError, OSError):
            pass                                  # a lost progress event must not fail the job


def load_edits(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    return pq.read_table(path).to_pylist()


def write_edits(path: Path, edits: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = {f.name: [(e.get(f.name) if f.name != "from" else e.get("from")) for e in edits] for f in EDIT_SCHEMA}
    pq.write_table(pa.table({k: pa.array(v, pa.string()) for k, v in cols.items()}, schema=EDIT_SCHEMA), path)


def _inputs(body: dict, root: Path) -> dict:
    """Resolve every input of the body to a local path and check it exists (`input_not_found`, retryable)."""
    out = {"files": [], "config": None, "previous": None, "examples": None, "review_upload": None}
    for item in body["inputs"]:
        path = root / item["ref"]
        if not path.exists():
            raise JobFailed("input_not_found", f"missing input {item['ref']}", retryable=True)
        kind = item["kind"]
        if kind == "raw_file":
            out["files"].append({"path": str(path), "source_id": item["source_id"], "file_id": item["file_id"],
                                 "relative_path": item.get("relative_path"), "content_hash": item["content_hash"]})
        elif kind == "config":
            out["config"] = json.loads(path.read_text(encoding="utf8"))
        elif kind == "previous_structure":
            out["previous"] = path
        elif kind == "examples":
            out["examples"] = str(path)
        elif kind == "review_upload":
            out["review_upload"] = str(path)
    return out


def _outputs(out: Path, mode: str) -> list[dict]:
    """The `succeeded` event's output list, each with its content hash (spec 6.1)."""
    items = [("report.json", "report"), ("inventory.json", "report"), ("review/Structural_Review.xlsx", "review"),
             ("llm/decisions.jsonl", "log"), ("llm/examples.jsonl", "log")]
    res = [{"path": p, "kind": k, "content_hash": file_hash(out / p)} for p, k in items if (out / p).exists()]
    files_hash = hash_tree(out / "files") if (out / "files").exists() else {}
    res.append({"path": "files/", "kind": "dataset", "content_hash": json.dumps(files_hash, sort_keys=True)})
    if mode == "review_import":
        res.append({"path": "review_import/", "kind": "patch",
                    "content_hash": json.dumps(hash_tree(out / "review_import"), sort_keys=True)})
    return res


def run_job(body: dict, storage_root: str | Path, emit=lambda e: None, is_cancelled=lambda: False,
            llm_transport=None) -> dict:
    """Run one job. Emits accepted / progress / log / succeeded / failed; never raises for job failures."""
    root = Path(storage_root)
    emit({"type": "accepted", "job_id": body["job_id"], "attempt": body.get("attempt", 1)})
    params = body.get("params", {})
    mode = params.get("mode", "run")
    try:
        if body.get("job_type") != "data.structure":
            raise JobFailed("internal_error", f"unsupported job type {body.get('job_type')}")
        ins = _inputs(body, root)
        out = root / body["output_prefix"]
        job = {"files": ins["files"], "config": ins["config"], "output_dir": str(out),
               "previous_dir": str(ins["previous"]) if ins["previous"] else None, "examples_path": ins["examples"],
               "full": params.get("full", False), "llm": params.get("llm"), "llm_transport": llm_transport,
               "project_id": body.get("context", {}).get("project_id", "local"), "job_id": body["job_id"],
               "progress": Progress(lambda stage, pct, detail=None: emit({"type": "progress", "stage": stage, "pct": pct}),
                                    lambda msg: emit({"type": "log", "message": msg}), is_cancelled)}
        try:
            st = resolve_settings(ins["config"])
        except ConfigInvalid as e:
            raise JobFailed("config_invalid", str(e)) from e
        existing = load_edits(root / st.cell_edits_ref["path"] if st.cell_edits_ref else None)
        patch, edits = None, existing
        if mode == "review_import":
            patch, edits = _review_import(job, ins, st, existing)
        job["edits"] = edits
        result = run_structure(job)
        if mode == "review_import":
            write_edits(out / "review_import" / "cell_edits.parquet", edits)
            dump_json(out / "review_import" / "patch.json", patch)
        event = {"type": "succeeded", "job_id": body["job_id"], "outputs": _outputs(out, mode),
                 "summary": result["summary"], "action_items": result["action_items"]}
        emit(event)
        return event
    except Cancelled:
        event = {"type": "failed", "job_id": body["job_id"], "error": {"code": "cancelled", "retryable": False,
                                                                       "message": "The job was cancelled."}}
    except JobFailed as e:
        event = {"type": "failed", "job_id": body["job_id"], "error": {
            "code": e.code, "retryable": e.retryable, "message": str(e), "details": e.details}}
    except Exception as e:  # noqa: BLE001 - anything unexpected at job level (spec 11.4)
        event = {"type": "failed", "job_id": body["job_id"], "error": {
            "code": "internal_error", "retryable": True, "message": type(e).__name__}}
    emit(event)
    return event


def _review_import(job: dict, ins: dict, st, existing: list[dict]):
    """Check the uploaded workbook against the current output; the patched settings drive the run."""
    prev = ins["previous"]
    if ins["review_upload"] is None or prev is None:
        raise JobFailed("review_import_invalid", "an import needs the uploaded workbook and the current output")
    inventory = json.loads((prev / "inventory.json").read_text(encoding="utf8"))
    patch, edits, violations = import_review(ins["review_upload"], prev, inventory, st, existing)
    if violations:
        listing = "; ".join(f"{v.code} {v.sheet} {v.cell}" for v in violations[:5])
        raise JobFailed("review_import_invalid", f"{len(violations)} violation(s): {listing}",
                        details={"violations": [v.as_dict() for v in violations[:200]]})
    job["settings_patch"] = lambda s: apply_patch(s, patch)
    return patch, edits
