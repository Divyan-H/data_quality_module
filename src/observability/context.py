"""The request a piece of work belongs to, carried implicitly (contextvars) so log lines and audit rows can name it."""
from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_SAFE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def new_request_id(incoming: str | None = None) -> str:
    """An id for this request: the caller's `X-Request-ID` when it is a plain token, else a fresh one."""
    return incoming if incoming and _SAFE.fullmatch(incoming) else uuid.uuid4().hex
