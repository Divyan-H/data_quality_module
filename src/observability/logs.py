"""Structured logs: one JSON object per line on stderr, so a log collector can filter by run, job or request.

Rule for every caller: log identifiers, counts and codes, never cell values, file contents, prompts or tokens.
`DQ_LOG_FORMAT=text` gives plain lines for a developer's terminal; `DQ_LOG_LEVEL` sets the level (default INFO).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from .context import request_id

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"t": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
               "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        rid = request_id.get()
        if rid:
            out["request_id"] = rid
        for key, value in record.__dict__.items():           # fields passed with `extra={...}`
            if key not in _STANDARD and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def configure(level: str | None = None, fmt: str | None = None) -> None:
    """Set up the root logger once. Safe to call again (the handler is replaced, not added twice)."""
    level = (level or os.environ.get("DQ_LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or os.environ.get("DQ_LOG_FORMAT") or "json").lower()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json"
                         else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s  %(message)s"))
    handler.set_name("dq")
    root = logging.getLogger()
    for h in [h for h in root.handlers if h.get_name() == "dq"]:
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = True      # the middleware writes one access line per request
