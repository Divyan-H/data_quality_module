"""API module: the Data Pipeline service.

Two surfaces on one app:
  * Job Contract v1 for the platform gateway (spec 11, 9.4): `POST /jobs` answers at once; the job runs in a
    background thread and reports through its `callback_url`. Storage is a local directory (`STRUCTURE_STORAGE_ROOT`).
  * The analyst web app (`/api/v1/...` plus the built frontend), mounted when service settings are given.

Cross-cutting behaviour lives in middleware, outermost first: `HttpHardening` (request id, access log, security
headers, last-resort 500), CORS (development only), the access-token guard.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import urllib.parse
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from src.observability import logs
from src.observability.context import request_id
from src.observability.http import HttpHardening
from src.orchestration.settings import ServiceSettings, load_service_settings, production_problems
from src.security import auth
from src.storage.audit import AuditLog
from src.structure.job import HttpEmitter, run_job
from src.structure.review_import import precheck

log = logging.getLogger("dq.api")
PRECHECK_MAX_MB = 200          # the review workbook the gateway sends for a synchronous check
MAX_REMEMBERED_JOBS = 500      # finished Job Contract jobs kept for idempotent re-submission


class JobBody(BaseModel):
    """Job Contract v1 request. Unknown fields are kept (the contract may grow); the ones the service acts on are typed."""
    model_config = ConfigDict(extra="allow")

    job_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    attempt: int = Field(1, ge=1, le=1000)
    job_type: str = Field(..., max_length=64)
    output_prefix: str = Field(..., min_length=1, max_length=1024)
    inputs: list[dict] = Field(default_factory=list, max_length=10_000)
    params: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)
    callback_url: str | None = Field(None, max_length=2048)
    job_token: str | None = Field(None, max_length=4096)


def callback_allowed(url: str) -> bool:
    """A job callback must be http(s), and when STRUCTURE_CALLBACK_HOSTS is set its host must be on that list (the
    job token goes to that address, so an open list would let a caller send it, and requests, anywhere)."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    allowed = [h.strip().lower() for h in os.environ.get("STRUCTURE_CALLBACK_HOSTS", "").split(",") if h.strip()]
    return not allowed or parts.hostname.lower() in allowed


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)


def install_common_error_handlers(app: FastAPI) -> None:
    """One error shape for every refusal: {"error": {"code", "message", ...}}."""

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: RequestValidationError):
        problems = [{"field": ".".join(str(p) for p in err.get("loc", ())[1:]), "message": err.get("msg", "")}
                    for err in e.errors()[:20]]
        return _error(422, "bad_request", "The request was not valid.", problems=problems)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: HTTPException):
        code = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 413: "too_large"}.get(e.status_code, "error")
        return _error(e.status_code, code, str(e.detail))


def create_app(storage_root: str | None = None, service: ServiceSettings | None = None) -> FastAPI:
    root = Path(storage_root or os.environ.get("STRUCTURE_STORAGE_ROOT", "./data/storage"))
    # The interactive docs pages load scripts from a CDN, which the Content-Security-Policy forbids on purpose;
    # the machine-readable contract stays at /openapi.json (token-protected like the rest).
    app = FastAPI(title="Data Pipeline service", version="1.1.0", docs_url=None, redoc_url=None)
    audit = AuditLog(service.audit_db if service is not None else None)
    app.state.audit = audit
    app.state.throttle = auth.install(
        app, service.auth_token if service is not None else os.environ.get("DQ_API_TOKEN"), audit,
        service.sign_in_max_failures if service is not None else 5, service.sign_in_lockout_s if service is not None else 300)
    install_common_error_handlers(app)
    jobs: OrderedDict[tuple, dict] = OrderedDict()   # (job_id, attempt) -> {"state", "cancel"}: POST /jobs is idempotent
    lock = threading.Lock()

    def remember(key: tuple, state: dict) -> None:
        """Keep the table bounded: forget the oldest finished jobs first (a running job is never forgotten)."""
        jobs[key] = state
        for old in [k for k, s in jobs.items() if s["state"] != "running"][:max(0, len(jobs) - MAX_REMEMBERED_JOBS)]:
            jobs.pop(old, None)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/jobs", status_code=202)
    def submit(body: JobBody) -> dict:
        key = (body.job_id, body.attempt)
        if body.callback_url:
            if not callback_allowed(body.callback_url):
                raise HTTPException(400, "callback_url is not an allowed http(s) address")
            if not body.job_token:
                raise HTTPException(400, "job_token is required with callback_url")
        with lock:
            if key in jobs:
                return {"job_id": body.job_id, "state": jobs[key]["state"]}
            state = {"state": "running", "cancel": threading.Event()}
            remember(key, state)
        emit = HttpEmitter(body.callback_url, body.job_token or "") if body.callback_url else (lambda e: None)
        payload = body.model_dump()
        rid = request_id.get()

        def work():
            request_id.set(rid)
            try:
                state["state"] = run_job(payload, root, emit, state["cancel"].is_set)["type"]
            except Exception:  # noqa: BLE001 - run_job reports job failures itself; this is a defect in the service
                log.exception("job contract worker failed", extra={"job_id": body.job_id})
                state["state"] = "failed"
            audit.record("gateway_job.finished", actor="gateway", job=body.job_id, attempt=body.attempt, outcome=state["state"])

        audit.record("gateway_job.accepted", actor="gateway", job=body.job_id, attempt=body.attempt, job_type=body.job_type)
        threading.Thread(target=work, name=f"gateway-{body.job_id}", daemon=True).start()
        return {"job_id": body.job_id, "state": "running"}

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict:
        with lock:
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
        mount_web_app(app, service, audit)                # last: its catch-all route must not shadow the ones above
    app.add_middleware(HttpHardening)                     # added last = outermost: every answer gets the headers
    return app


def mount_web_app(app: FastAPI, service: ServiceSettings, audit: AuditLog) -> None:
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
        app.add_middleware(CORSMiddleware, allow_origins=service.cors_origins, allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type"])
    app.router.on_shutdown.append(lambda: pipe.shutdown(service.shutdown_grace_s))
    dist = Path(service.frontend_dir)
    if (dist / "index.html").exists():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "jobs", "review/", "health")):
                raise HTTPException(404, "There is nothing at this address.")
            target = (dist / path).resolve()
            if path and dist.resolve() in target.parents and target.is_file():
                return FileResponse(target)
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})   # always the current build


def start_api_server(api_config: dict) -> None:
    """Start the API server with the `api` section of the settings (host, port)."""
    import uvicorn
    logs.configure()
    service = load_service_settings()
    host = api_config.get("host", "127.0.0.1")
    try:
        auth.check_token(service.auth_token)
    except auth.TokenError as e:
        raise SystemExit(str(e)) from e
    if service.env == "production":
        problems = production_problems(service, host)
        if problems:
            raise SystemExit("Refusing to start with DQ_ENV=production:\n  - " + "\n  - ".join(problems))
    if not auth.is_loopback(host) and not service.auth_token and os.environ.get("DQ_ALLOW_UNAUTHENTICATED") != "1":
        raise SystemExit(f"Refusing to listen on {host} without an access token: anyone who can reach the port could read "
                         "and delete client data. Set DQ_API_TOKEN (16+ characters), or bind to 127.0.0.1.")
    log.info("starting", extra={"env": service.env, "host": host, "port": int(api_config.get("port", 8000)),
                                "auth": bool(service.auth_token), "workspace": str(service.workspace)})
    uvicorn.run(create_app(api_config.get("storage_root"), service), host=host, port=int(api_config.get("port", 8000)),
                log_config=None, access_log=False, server_header=False,
                # Behind the reverse proxy the client address and scheme come from X-Forwarded-*; trust them only
                # from the proxies named here (default: this machine).
                proxy_headers=True, forwarded_allow_ips=os.environ.get("DQ_TRUSTED_PROXIES", "127.0.0.1"),
                timeout_graceful_shutdown=max(5, service.shutdown_grace_s + 5))
