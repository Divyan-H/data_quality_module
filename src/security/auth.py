"""Access control for the service: one shared access token, optional.

With no token configured the service behaves as before and must stay on 127.0.0.1 (start_api_server refuses any
other address). With `DQ_API_TOKEN` (or `service.auth_token`) set, every API call needs either

  * `Authorization: Bearer <token>`   for scripts and the platform gateway, or
  * the `dq_session` cookie           for the browser: POST /api/v1/session with the token sets it (HttpOnly,
                                      SameSite=Strict), so plain download links keep working.

The cookie holds an HMAC of the token, not the token. Guessing is made expensive twice: every failed attempt waits a
second, and an address with `max_failures` failures inside `lockout_s` is refused (429) until the window has passed.
The client address is the one uvicorn reports; behind the reverse proxy that is the real client only for proxies
listed in DQ_TRUSTED_PROXIES (uvicorn's `forwarded_allow_ips`).
The built frontend (HTML, JS, CSS) is served without a token: it holds no client data.

This is the step before real accounts (docs/service_issues_and_roadmap.md S1/S2): it stops an exposed port from
handing every run to whoever finds it. Per-user identity and tenants come with the gateway's JWT.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import threading
import time
from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

COOKIE = "dq_session"
SESSION_HOURS = 12
MIN_TOKEN_LEN = 16
FAILED_SIGN_IN_DELAY_S = 1.0
OPEN_PATHS = {"/health", "/api/v1/health", "/api/v1/ready", "/api/v1/session"}   # no client data in these; /metrics and /audit need the token
PROTECTED_PREFIXES = ("/api/", "/jobs", "/review/", "/docs", "/openapi.json", "/redoc")


log = logging.getLogger("dq.auth")


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


class Throttle:
    """Failed attempts per client address, in memory (one service instance; a restart forgets them)."""

    MAX_ADDRESSES = 10_000

    def __init__(self, max_failures: int = 5, window_s: float = 300, clock=time.monotonic):
        self.max_failures, self.window_s, self._clock = max_failures, window_s, clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, address: str) -> deque[float]:
        q = self._failures.get(address)
        if q is None:
            return deque()
        cutoff = self._clock() - self.window_s
        while q and q[0] < cutoff:
            q.popleft()
        if not q:
            self._failures.pop(address, None)
        return q

    def retry_after(self, address: str) -> int:
        """Seconds until `address` may try again; 0 = it may try now."""
        with self._lock:
            q = self._recent(address)
            if len(q) < self.max_failures:
                return 0
            return max(1, int(q[0] + self.window_s - self._clock()) + 1)

    def failed(self, address: str) -> None:
        with self._lock:
            if len(self._failures) >= self.MAX_ADDRESSES and address not in self._failures:
                for stale in [a for a in list(self._failures) if not self._recent(a)]:
                    self._failures.pop(stale, None)
                if len(self._failures) >= self.MAX_ADDRESSES:        # still full: drop the oldest entry
                    self._failures.pop(next(iter(self._failures)))
            self._failures.setdefault(address, deque(maxlen=self.max_failures)).append(self._clock())

    def succeeded(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _locked(seconds: int) -> JSONResponse:
    return JSONResponse({"error": {"code": "too_many_attempts", "retry_after_s": seconds,
                                   "message": f"Too many failed sign-ins from this address. Try again in {seconds} seconds."}},
                        status_code=429, headers={"Retry-After": str(seconds)})


class SignIn(BaseModel):
    token: str = Field(..., min_length=1, max_length=512)


def install(app, token: str | None, audit=None, max_failures: int = 5, lockout_s: float = 300) -> Throttle:
    """Guard every API path when a token is set, and add the session endpoints the browser signs in with.
    `audit` (storage.audit.AuditLog) receives sign-in outcomes; the address is recorded, never the token."""
    token = check_token(token)
    throttle = Throttle(max_failures, lockout_s)

    def note(action: str, request: Request) -> None:
        if audit is not None:
            audit.record(action, actor=client_address(request))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if token and needs_token(request.url.path) and request.method != "OPTIONS" and not authorized(request, token):
            address = client_address(request)
            if request.headers.get("authorization"):            # a wrong bearer token is a guess; a missing one is not
                wait = throttle.retry_after(address)
                if wait:
                    return _locked(wait)
                throttle.failed(address)
                log.warning("rejected bearer token", extra={"client": address})
                note("auth.bearer_rejected", request)
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
        address = client_address(request)
        wait = throttle.retry_after(address)
        if wait:
            return _locked(wait)
        if not hmac.compare_digest(body.token.encode(), token.encode()):
            throttle.failed(address)
            log.warning("failed sign-in", extra={"client": address})
            note("auth.sign_in_failed", request)
            await asyncio.sleep(FAILED_SIGN_IN_DELAY_S)
            return JSONResponse({"error": {"code": "bad_token", "message": "That access token is not right."}}, status_code=401)
        throttle.succeeded(address)
        note("auth.signed_in", request)
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
    return throttle
