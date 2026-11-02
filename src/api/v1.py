"""The analyst-facing HTTP API (`/api/v1`): runs, phases, review uploads, config workbooks, downloads, live events.

Thin on purpose: validate the request, call `Pipeline`, shape the answer. No phase logic lives here.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from src.orchestration.orchestrator import Pipeline, PipelineError

CHUNK = 1 << 20
MEDIA = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".json": "application/json",
         ".jsonl": "application/x-ndjson", ".csv": "text/csv", ".parquet": "application/vnd.apache.parquet",
         ".zip": "application/zip", ".log": "text/plain"}


class StartBody(BaseModel):
    llm: str | None = Field(None, pattern="^(on|off)$", description="'off' = continue without the LLM (rule-based fallback)")
    llm_review: str | None = Field(None, pattern="^(near_ties|all)$")
    fixes: dict[str, bool] | None = None
    zero_expected: dict[str, list[str]] | None = Field(None, description="Phase B: {table ref: columns where zero is a valid value}")
    restart: bool = False


class SelectionBody(BaseModel):
    tables: list[str] = Field(..., max_length=1000)


class InputsBody(BaseModel):
    yaml: str | None = Field(None, max_length=200_000, description="the answers as YAML text (comments are kept)")
    inputs: dict | None = Field(None, description="or as a JSON object with the same keys")


class SkipBody(BaseModel):
    reason: str | None = Field(None, max_length=300)


async def save_upload(upload: UploadFile, limit: int, tmp: Path) -> Path:
    """Stream an upload to a temp file, stopping as soon as it passes `limit` bytes."""
    tmp.mkdir(parents=True, exist_ok=True)
    fd = tempfile.NamedTemporaryFile(dir=tmp, delete=False)
    size = 0
    try:
        while chunk := await upload.read(CHUNK):
            size += len(chunk)
            if size > limit:
                raise PipelineError(413, "too_large", f"'{upload.filename}' is larger than the allowed {limit // 2**20} MB.")
            fd.write(chunk)
    except BaseException:
        fd.close()
        Path(fd.name).unlink(missing_ok=True)
        raise
    fd.close()
    return Path(fd.name)


def file_response(path: Path, name: str) -> FileResponse:
    return FileResponse(path, filename=name, media_type=MEDIA.get(Path(name).suffix.lower(), "application/octet-stream"))


def make_router(pipe: Pipeline) -> APIRouter:
    r = APIRouter(prefix="/api/v1")
    tmp_dir = pipe.settings.workspace / ".incoming"
    max_total = pipe.settings.max_upload_mb * 1024 * 1024
    max_review = pipe.settings.max_review_upload_mb * 1024 * 1024

    @r.get("/health")
    def health() -> dict:
        return {"status": "ok", "active_jobs": pipe.runner.active_count()}

    @r.get("/options")
    def options() -> dict:
        return pipe.options()

    @r.get("/llm/status")
    def llm_status(fresh: bool = False) -> dict:
        return pipe.llm_status(fresh)

    # ------------------------------------------------------------------ runs
    @r.get("/runs")
    def list_runs() -> list[dict]:
        return pipe.list_runs()

    @r.post("/runs", status_code=201)
    async def create_run(files: list[UploadFile] = File(...), paths: list[str] = Form(default=[]),
                         name: str = Form(default=""), llm: str = Form(default="on")) -> dict:
        saved: list[tuple[str, Path]] = []
        budget = max_total
        try:
            for i, f in enumerate(files):
                tmp = await save_upload(f, budget, tmp_dir)
                budget -= tmp.stat().st_size
                saved.append((paths[i] if i < len(paths) and paths[i] else (f.filename or ""), tmp))
            run_id = pipe.create_run(name, saved, llm)
        except BaseException:
            for _, p in saved:
                p.unlink(missing_ok=True)
            raise
        return pipe.view(run_id)

    @r.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        return pipe.view(run_id)

    @r.delete("/runs/{run_id}", status_code=204)
    def delete_run(run_id: str) -> None:
        pipe.delete_run(run_id)

    @r.get("/runs/{run_id}/events")
    def events(run_id: str, after: int = Query(0, ge=0)) -> dict:
        return pipe.events(run_id, after)

    # ------------------------------------------------------------------ phases
    @r.get("/runs/{run_id}/phases/{key}/summary")
    def summary(run_id: str, key: str) -> dict:
        return pipe.summary(run_id, key)

    @r.post("/runs/{run_id}/phases/{key}/start")
    def start(run_id: str, key: str, body: StartBody | None = None) -> dict:
        body = body or StartBody()
        return pipe.start(run_id, key, body.model_dump(exclude_none=True))

    @r.post("/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict:
        return pipe.cancel(run_id)

    @r.post("/runs/{run_id}/phases/{key}/skip")
    def skip(run_id: str, key: str, body: SkipBody | None = None) -> dict:
        return pipe.skip(run_id, key, body.reason if body else None)

    @r.get("/runs/{run_id}/inputs")
    def get_inputs(run_id: str) -> dict:
        return pipe.get_inputs(run_id)

    @r.put("/runs/{run_id}/inputs")
    def put_inputs(run_id: str, body: InputsBody) -> dict:
        if (body.yaml is None) == (body.inputs is None):
            raise PipelineError(400, "bad_inputs", "Send either yaml or inputs.")
        return pipe.set_inputs(run_id, body.yaml if body.yaml is not None else body.inputs)

    @r.post("/runs/{run_id}/phases/A/selection")
    def select_tables(run_id: str, body: SelectionBody) -> dict:
        return pipe.select_tables(run_id, body.tables)

    @r.post("/runs/{run_id}/phases/{key}/accept")
    def accept(run_id: str, key: str) -> dict:
        return pipe.accept(run_id, key)

    @r.post("/runs/{run_id}/phases/{key}/review")
    async def upload_review(run_id: str, key: str, file: UploadFile = File(...), llm: str = Form(default="")) -> dict:
        tmp = await save_upload(file, max_review, tmp_dir)
        try:
            return pipe.upload_review(run_id, key, tmp, file.filename or "", {"llm": llm} if llm == "off" else None)
        finally:
            tmp.unlink(missing_ok=True)

    @r.get("/runs/{run_id}/phases/{key}/columns")
    def columns(run_id: str, key: str) -> dict:
        return {"tables": pipe.columns(run_id, key)}

    # ------------------------------------------------------------------ client config workbooks (C, D)
    @r.get("/runs/{run_id}/phases/{key}/tables")
    def tables(run_id: str, key: str) -> dict:
        return {"tables": pipe.candidates(run_id, key)}

    @r.get("/runs/{run_id}/phases/{key}/config-template")
    def config_template(run_id: str, key: str, table: str | None = None):
        path, name = pipe.config_template(run_id, key, table)
        return file_response(path, name)

    @r.post("/runs/{run_id}/phases/{key}/config")
    async def upload_config(run_id: str, key: str, file: UploadFile = File(...), table: str = Form(default="")) -> dict:
        tmp = await save_upload(file, max_review, tmp_dir)
        try:
            return pipe.upload_config(run_id, key, tmp, file.filename or "", table or None)
        finally:
            tmp.unlink(missing_ok=True)

    @r.post("/runs/{run_id}/phases/{key}/config/recheck")
    def recheck_config(run_id: str, key: str, table: str | None = None) -> dict:
        return pipe.upload_config(run_id, key, None, None, table)

    # ------------------------------------------------------------------ files
    @r.get("/runs/{run_id}/results")
    def results(run_id: str) -> dict:
        return pipe.results(run_id)

    @r.get("/runs/{run_id}/files/{artifact_id}")
    def download(run_id: str, artifact_id: str):
        path, name = pipe.artifact(run_id, artifact_id)
        return file_response(path, name)

    @r.get("/runs/{run_id}/bundle")
    def bundle_zip(run_id: str):
        path, name = pipe.bundle_zip(run_id)
        return file_response(path, name)

    return r


def install_error_handlers(app) -> None:
    from src.security.uploads import UploadRejected
    from src.storage.run_store import RunNotFound

    @app.exception_handler(PipelineError)
    async def _pipeline(_: Request, e: PipelineError):
        return JSONResponse({"error": {"code": e.code, "message": e.message, **e.extra}}, status_code=e.status)

    @app.exception_handler(RunNotFound)
    async def _missing(_: Request, e: RunNotFound):
        return JSONResponse({"error": {"code": "run_not_found", "message": "This run does not exist (it may have been deleted)."}},
                            status_code=404)

    @app.exception_handler(UploadRejected)
    async def _rejected(_: Request, e: UploadRejected):
        return JSONResponse({"error": {"code": "bad_request", "message": str(e)}}, status_code=400)
