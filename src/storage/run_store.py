"""Run workspaces on local disk: one folder per run, one `state.json`, one append-only `events.jsonl`.

    <root>/<run_id>/
        state.json        the run's state; written only by the API process (atomic replace, per-run lock)
        events.jsonl      live progress and log lines; appended by the job worker (the API only reads it)
        inputs/           the files the analyst uploaded, with their folder structure
        phase_a/          Phase A output          work/   Phase B, C, D output (one shared run folder)
        config/           client config workbooks (templates and uploads)
        uploads/          every review workbook the analyst uploaded
        jobs/<job_id>/    request.json, result.json, worker.log of each worker process
        final/            exports built for the Results page

A run folder is self-contained: copying it elsewhere keeps the run (state, inputs, every output, audit trail).
"""
from __future__ import annotations

import builtins
import json
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from src.security.uploads import UploadRejected, valid_run_id

STATE = "state.json"
EVENTS = "events.jsonl"
SCHEMA = 1


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunNotFound(KeyError):
    pass


class RunStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ paths
    def path(self, run_id: str) -> Path:
        try:
            p = self.root / valid_run_id(run_id)
        except UploadRejected:
            raise RunNotFound(run_id) from None
        if not (p / STATE).exists():
            raise RunNotFound(run_id)
        return p

    def _lock(self, run_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(run_id, threading.RLock())

    # ------------------------------------------------------------------ state
    def create(self, name: str, state: dict) -> str:
        run_id = uuid.uuid4().hex[:16]
        folder = self.root / run_id
        folder.mkdir(parents=True)
        for sub in ("inputs", "config", "uploads", "jobs", "final"):
            (folder / sub).mkdir()
        state = {"schema": SCHEMA, "id": run_id, "name": name, "created_at": now(), "updated_at": now(), **state}
        self._write(folder / STATE, state)
        return run_id

    def load(self, run_id: str) -> dict:
        path = self.path(run_id) / STATE
        return json.loads(path.read_text(encoding="utf8"))

    @contextmanager
    def edit(self, run_id: str) -> Iterator[dict]:
        """Read-modify-write under the run's lock; the state is saved when the block ends without an error."""
        with self._lock(run_id):
            state = self.load(run_id)
            yield state
            state["updated_at"] = now()
            self._write(self.path(run_id) / STATE, state)

    def update(self, run_id: str, fn: Callable[[dict], None]) -> dict:
        with self.edit(run_id) as state:
            fn(state)
        return state

    @staticmethod
    def _write(path: Path, state: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf8")
        for attempt in range(5):               # Windows refuses to replace a file another process is reading
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                threading.Event().wait(0.05 * (attempt + 1))

    def list(self) -> builtins.list[dict]:
        runs = []
        for p in self.root.iterdir():
            if p.is_dir() and (p / STATE).exists():
                try:
                    runs.append(json.loads((p / STATE).read_text(encoding="utf8")))
                except (OSError, ValueError):
                    continue                    # a half-written or foreign folder is not a run
        return sorted(runs, key=lambda s: s.get("created_at", ""), reverse=True)

    def delete(self, run_id: str) -> None:
        folder = self.path(run_id)
        with self._lock(run_id):
            shutil.rmtree(folder)
        with self._guard:
            self._locks.pop(run_id, None)

    # ------------------------------------------------------------------ events
    def append_event(self, run_id: str, event: dict) -> None:
        append_event(self.path(run_id), event)

    def events(self, run_id: str, after: int = 0, limit: int = 500) -> tuple[builtins.list[dict], int]:
        """Events after cursor `after` (a line count) and the new cursor."""
        path = self.path(run_id) / EVENTS
        if not path.exists():
            return [], 0
        out: builtins.list[dict] = []
        n = 0
        with path.open(encoding="utf8") as fh:
            for n, line in enumerate(fh, start=1):
                if n > after and len(out) < limit:
                    try:
                        out.append({"seq": n, **json.loads(line)})
                    except ValueError:
                        out.append({"seq": n, "type": "log", "level": "warn", "message": "(unreadable event)"})
            total = n
        cursor = out[-1]["seq"] if out else after
        return out, min(cursor, total)


_append_lock = threading.Lock()


def append_event(run_folder: Path, event: dict) -> None:
    """One JSON line per call. The worker process is the only writer of a run's events (one job per run at a time);
    the lock and a single OS-level append keep that process's threads from losing or splitting lines (on Windows,
    two Python file handles appending at once do lose lines)."""
    line = (json.dumps({"t": now(), **event}, ensure_ascii=False) + "\n").encode("utf8")
    with _append_lock:
        fd = os.open(run_folder / EVENTS, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
