"""Upload and identifier validation for the web service.

Everything that arrives from a browser is untrusted: file names, relative paths, run ids, artifact ids.
Nothing here touches the disk; callers decide where a validated name is written.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

# Formats Phase A can read (src/structure/pipeline.EXT_FORMAT). Archives are refused: Phase A reports them as
# unsupported content, so failing early at the door gives the analyst a clearer message.
DATA_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".xlsb", ".ods", ".csv", ".tsv", ".txt", ".parquet"}
LEGACY_EXTENSIONS = {".xls", ".xlsb", ".ods"}        # need the LibreOffice converter (Phase A, spec 3.1)
WORKBOOK_EXTENSIONS = {".xlsx"}                 # review workbooks and client config workbooks are always .xlsx

MAX_NAME_LEN = 180
MAX_PATH_DEPTH = 8
_RUN_ID = re.compile(r"^[0-9a-f]{12,32}$")
_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"|?*\\]')
_ARTIFACT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class UploadRejected(ValueError):
    """The upload is refused; the message is safe to show the analyst."""


def valid_run_id(value: str) -> str:
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise UploadRejected("unknown run")
    return value


def valid_artifact_id(value: str) -> str:
    if not isinstance(value, str) or not _ARTIFACT_ID.fullmatch(value):
        raise UploadRejected("unknown file")
    return value


def safe_filename(name: str | None, allowed: set[str]) -> str:
    """The last path component of `name`, if it is a plain file name with an allowed extension."""
    base = PurePosixPath((name or "").replace("\\", "/")).name.strip().strip(".")
    if not base or len(base) > MAX_NAME_LEN or _BAD_CHARS.search(base):
        raise UploadRejected("the file name is empty, too long or has characters that are not allowed")
    stem, dot, ext = base.rpartition(".")
    if not dot or f".{ext.lower()}" not in allowed:
        raise UploadRejected(f"'{base}' is not a supported file type (allowed: {', '.join(sorted(allowed))})")
    if stem.split(".")[0].lower() in _RESERVED:
        raise UploadRejected(f"'{base}' uses a reserved file name")
    return base


def safe_relative_path(path: str | None, name: str) -> str:
    """A folder-relative path ('Client A/2026/spend.xlsx') made of plain components; falls back to the file name."""
    if not path:
        return name
    parts = [p for p in PurePosixPath(path.replace("\\", "/")).parts if p not in ("", "/")]
    if not parts or len(parts) > MAX_PATH_DEPTH:
        raise UploadRejected("the folder path of an uploaded file is empty or nested too deeply")
    for part in parts:
        if part in (".", "..") or _BAD_CHARS.search(part) or len(part) > MAX_NAME_LEN:
            raise UploadRejected("the folder path of an uploaded file has a part that is not allowed")
    return "/".join(parts)


def unique_name(name: str, taken: set[str]) -> str:
    """`name`, or `name (2)` etc. when the relative path is already used in this run."""
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    n = 2
    while f"{stem} ({n}).{ext}" in taken:
        n += 1
    return f"{stem} ({n}).{ext}"
