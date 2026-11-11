"""HTTP middleware: request id, access log, security headers, and a last-resort error answer.

Written as plain ASGI (not BaseHTTPMiddleware) so streamed downloads and uploads pass through untouched.
"""
from __future__ import annotations

import logging
import time

from . import metrics
from .context import new_request_id, request_id

log = logging.getLogger("dq.http")

# The SPA loads only its own script, style and data; React sets inline style attributes, hence 'unsafe-inline' for
# styles only. Nothing may frame the app (clickjacking) and the browser must not guess content types.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"content-security-policy", CSP.encode()),
]
HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
NO_STORE_PREFIXES = ("/api/", "/jobs", "/review/")           # answers that carry client data are never cached
QUIET_PATHS = {"/health", "/api/v1/health", "/api/v1/ready", "/api/v1/metrics"}


class HttpHardening:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin1"): v.decode("latin1") for k, v in scope.get("headers", [])}
        rid = new_request_id(headers.get("x-request-id"))
        token = request_id.set(rid)
        path, method = scope.get("path", ""), scope.get("method", "")
        https = scope.get("scheme") == "https" or headers.get("x-forwarded-proto") == "https"
        started, status, sent = time.monotonic(), 500, False

        async def send_wrapper(message):
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status, sent = message["status"], True
                out = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server"]
                present = {k.lower() for k, _ in out}
                out += [h for h in SECURITY_HEADERS if h[0] not in present]
                if https:
                    out.append(HSTS)
                if path.startswith(NO_STORE_PREFIXES) and b"cache-control" not in present:
                    out.append((b"cache-control", b"no-store"))
                out.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": out}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled error", extra={"method": method, "path": path})
            if sent:                                           # headers already left: nothing more can be said
                raise
            await _plain_500(send_wrapper, rid)
        finally:
            ms = round((time.monotonic() - started) * 1000, 1)
            metrics.inc("dq_http_requests_total", "HTTP requests by method and status class",
                        method=method, status=f"{status // 100}xx")
            if path not in QUIET_PATHS:
                log.log(logging.WARNING if status >= 500 else logging.INFO, "request",
                        extra={"method": method, "path": path, "status": status, "ms": ms})
            request_id.reset(token)


async def _plain_500(send, rid: str) -> None:
    body = (b'{"error":{"code":"internal_error","message":"Something went wrong on the server. '
            b'Quote the request id when you report it.","request_id":"' + rid.encode() + b'"}}')
    await send({"type": "http.response.start", "status": 500,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
