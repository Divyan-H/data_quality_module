"""API module: the Data Pipeline service.

Two surfaces on one app:
  * Job Contract v1 for the platform gateway (spec 11, 9.4): `POST /jobs` answers at once; the job runs in a
    background thread and reports through its `callback_url`. Storage is a local directory (`STRUCTURE_STORAGE_ROOT`).
  * The analyst web app (`/api/v1/...` plus the built frontend), mounted when service settings are given.
"""
from __future__ import annotations

import os
import tempfile
import threading
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from src.orchestration.settings import ServiceSettings, load_service_settings
from src.security import auth
from src.structure.job import HttpEmitter, run_job
from src.structure.review_import import precheck

PRECHECK_MAX_MB = 200          # the review workbook the gateway sends for a synchronous check


def callback_allowed(url: str) -> bool:
    """A job callback must be http(s), and when STRUCTURE_CALLBACK_HOSTS is set its host must be on that list (the
    job token goes to that address, so an open list would let a caller send it, and requests, anywhere)."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    allowed = [h.strip().lower() for h in os.environ.get("STRUCTURE_CALLBACK_HOSTS", "").split(",") if h.strip()]
    return not allowed or parts.hostname.lower() in allowed


def create_app(storage_root: str | None = None, service: ServiceSettings | None = None) -> FastAPI:
    root = Path(storage_root or os.environ.get("STRUCTURE_STORAGE_ROOT", "./data/storage"))
    app = FastAPI(title="Data Pipeline service")
    auth.install(app, service.auth_token if service is not None else os.environ.get("DQ_API_TOKEN"))
    jobs: dict[tuple, dict] = {}           # (job_id, attempt) -> {"state", "cancel", "result"}: POST /jobs is idempotent
    lock = threading.Lock()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/jobs", status_code=202)
    def submit(body: dict) -> dict:
        key = (body.get("job_id"), body.get("attempt", 1))
        if body.get("callback_url") and not callback_allowed(str(body["callback_url"])):
            raise HTTPException(400, "callback_url is not an allowed http(s) address")
        with lock:
            if key in jobs:
                return {"job_id": key[0], "state": jobs[key]["state"]}
            state = jobs[key] = {"state": "running", "cancel": threading.Event(), "result": None}
        emit = HttpEmitter(body["callback_url"], body["job_token"]) if body.get("callback_url") else (lambda e: None)

        def work():
            state["result"] = run_job(body, root, emit, state["cancel"].is_set)
            state["state"] = state["result"]["type"]

        threading.Thread(target=work, daemon=True).start()
        return {"job_id": key[0], "state": "running"}

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict:
        hits = [s for (jid, _), s in jobs.items() if jid == job_id]
        if not hits:
            raise HTTPException(404, "unknown job")
        for s in hits:
            s["cancel"].set()
        return {"job_id": job_id, "state": "cancelling"}

    @app.post("/review/precheck")
    async def review_precheck(request: Request) -> dict:
        """The gateway's synchronous check of a review workbook sent as the raw request body (no job, no state).
        The body is streamed to disk and refused past PRECHECK_MAX_MB, so a huge request cannot fill the memory."""
        limit, size = PRECHECK_MAX_MB * 1024 * 1024, 0
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    tmp.close()
                    os.unlink(tmp.name)
                    raise HTTPException(413, f"the workbook is larger than {PRECHECK_MAX_MB} MB")
                tmp.write(chunk)
        try:
            violations = precheck(tmp.name).violations
        finally:
            os.unlink(tmp.name)
        return {"valid": not violations, "violations": [v.as_dict() for v in violations]}

    if service is not None:
        mount_web_app(app, service)                       # last: its catch-all route must not shadow the ones above
    return app


def mount_web_app(app: FastAPI, service: ServiceSettings) -> None:
    """The analyst API under /api/v1 and, when it has been built, the frontend at /."""
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    from src.api.v1 import install_error_handlers, make_router
    from src.orchestration.orchestrator import Pipeline

    pipe = Pipeline(service)
    app.state.pipeline = pipe
    app.include_router(make_router(pipe))
    install_error_handlers(app)
    if service.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=service.cors_origins, allow_methods=["*"], allow_headers=["*"])
    app.router.on_shutdown.append(pipe.shutdown)
    dist = Path(service.frontend_dir)
    if (dist / "index.html").exists():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "jobs", "review/", "health")):
                raise HTTPException(404)
            target = (dist / path).resolve()
            if path and dist.resolve() in target.parents and target.is_file():
                return FileResponse(target)
            return FileResponse(dist / "index.html")


def start_api_server(api_config: dict) -> None:
    """Start the API server with the `api` section of the settings (host, port)."""
    import uvicorn
    service = load_service_settings()
    host = api_config.get("host", "127.0.0.1")
    try:
        auth.check_token(service.auth_token)
    except auth.TokenError as e:
        raise SystemExit(str(e)) from e
    if not auth.is_loopback(host) and not service.auth_token and os.environ.get("DQ_ALLOW_UNAUTHENTICATED") != "1":
        raise SystemExit(f"Refusing to listen on {host} without an access token: anyone who can reach the port could read "
                         "and delete client data. Set DQ_API_TOKEN (16+ characters), or bind to 127.0.0.1.")
    uvicorn.run(create_app(api_config.get("storage_root"), service), host=host, port=int(api_config.get("port", 8000)))
