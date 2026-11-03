"""Access control for the service: one shared access token, optional.

With no token configured the service behaves as before and must stay on 127.0.0.1 (start_api_server refuses any
other address). With `DQ_API_TOKEN` (or `service.auth_token`) set, every API call needs either

  * `Authorization: Bearer <token>`   for scripts and the platform gateway, or
  * the `dq_session` cookie           for the browser: POST /api/v1/session with the token sets it (HttpOnly,
                                      SameSite=Strict), so plain download links keep working.

The cookie holds an HMAC of the token, not the token. Failed sign-ins are slowed down to make guessing expensive.
The built frontend (HTML, JS, CSS) is served without a token: it holds no client data.

This is the step before real accounts (docs/service_issues_and_roadmap.md S1/S2): it stops an exposed port from
handing every run to whoever finds it. Per-user identity and tenants come with the gateway's JWT.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

COOKIE = "dq_session"
SESSION_HOURS = 12
MIN_TOKEN_LEN = 16
FAILED_SIGN_IN_DELAY_S = 1.0
OPEN_PATHS = {"/health", "/api/v1/health", "/api/v1/ready", "/api/v1/session"}   # no client data in these
PROTECTED_PREFIXES = ("/api/", "/jobs", "/review/", "/docs", "/openapi.json", "/redoc")


class TokenError(ValueError):
    pass


def check_token(token: str | None) -> str | None:
    """None (no access control) or a token long enough to resist guessing."""
    if token is None or token == "":
        return None
    if len(token) < MIN_TOKEN_LEN:
        raise TokenError(f"The access token must be at least {MIN_TOKEN_LEN} characters long.")
    return token


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def session_value(token: str) -> str:
    return hmac.new(token.encode(), b"dq-session-v1", hashlib.sha256).hexdigest()


def authorized(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer " and hmac.compare_digest(header[7:].strip().encode(), token.encode()):
        return True
    cookie = request.cookies.get(COOKIE)
    return bool(cookie) and hmac.compare_digest(cookie.encode(), session_value(token).encode())


def needs_token(path: str) -> bool:
    return path not in OPEN_PATHS and path.startswith(PROTECTED_PREFIXES)


class SignIn(BaseModel):
    token: str = Field(..., min_length=1, max_length=512)


def install(app, token: str | None) -> None:
    """Guard every API path when a token is set, and add the session endpoints the browser signs in with."""
    token = check_token(token)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if token and needs_token(request.url.path) and request.method != "OPTIONS" and not authorized(request, token):
            return JSONResponse({"error": {"code": "auth_required", "message": "Sign in with the service's access token."}},
                                status_code=401)
        return await call_next(request)

    router = APIRouter(prefix="/api/v1")

    @router.get("/session")
    def session(request: Request) -> dict:
        return {"auth_required": token is not None, "signed_in": token is None or authorized(request, token)}

    @router.post("/session")
    async def sign_in(body: SignIn, request: Request):
        if token is None:
            return {"auth_required": False, "signed_in": True}
        if not hmac.compare_digest(body.token.encode(), token.encode()):
            await asyncio.sleep(FAILED_SIGN_IN_DELAY_S)
            return JSONResponse({"error": {"code": "bad_token", "message": "That access token is not right."}}, status_code=401)
        resp = JSONResponse({"auth_required": True, "signed_in": True})
        resp.set_cookie(COOKIE, session_value(token), max_age=SESSION_HOURS * 3600, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https", path="/")
        return resp

    @router.delete("/session")
    def sign_out():
        resp = JSONResponse({"auth_required": token is not None, "signed_in": token is None})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    app.include_router(router)
