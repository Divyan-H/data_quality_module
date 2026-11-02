"""Starts phase jobs as worker processes, watches them, and kills them on cancel or timeout.

One job = one `python -m src.orchestration.worker <job_dir>` process. The runner never interprets a result; it
reports the outcome to `on_finish`, which decides what it means for the run (orchestrator.Pipeline).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psutil

from src.orchestration.settings import REPO_ROOT
from src.storage.run_store import RunStore

log = logging.getLogger("dq.runner")
WORKER = "src.orchestration.worker"


@dataclass
class Job:
    id: str
    run_id: str
    phase: str
    action: str
    folder: Path
    proc: subprocess.Popen | None = None
    cancelled: bool = False
    timed_out: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    outcome: dict | None = None


class Busy(RuntimeError):
    pass


def kill_tree(pid: int) -> None:
    """Terminate a process and everything it started (Phase A's file workers, LibreOffice)."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(procs, timeout=5)


class JobRunner:
    def __init__(self, store: RunStore, settings_path: str, on_finish: Callable[[Job, dict], None],
                 max_jobs: int = 2, timeout_s: int = 3 * 3600):
        self.store, self.settings_path, self.on_finish = store, settings_path, on_finish
        self.max_jobs, self.timeout_s = max_jobs, timeout_s
        self._jobs: dict[str, Job] = {}            # run_id -> its active job
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ queries
    def active(self, run_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(run_id)

    def active_count(self) -> int:
        with self._lock:
            return len(self._jobs)

    # ------------------------------------------------------------------ launch
    def launch(self, run_id: str, phase: str, action: str, params: dict | None = None,
               upload: Path | None = None) -> Job:
        run = self.store.path(run_id)
        with self._lock:
            if run_id in self._jobs:
                raise Busy("This run already has a job in progress.")
            if len(self._jobs) >= self.max_jobs:
                raise Busy(f"The service is running its limit of {self.max_jobs} jobs; try again in a moment.")
            job_id = uuid.uuid4().hex[:12]
            folder = run / "jobs" / job_id
            folder.mkdir(parents=True)
            job = Job(job_id, run_id, phase, action, folder)
            self._jobs[run_id] = job
        request = {"job_id": job_id, "run_dir": str(run), "phase": phase, "action": action, "params": params or {},
                   "upload": str(upload) if upload else None, "settings_path": self.settings_path,
                   "state": {"id": run_id}}
        (folder / "request.json").write_text(json.dumps(request), encoding="utf8")
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
               "PYTHONPATH": str(REPO_ROOT)}
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            job.proc = subprocess.Popen([sys.executable, "-W", "ignore", "-m", WORKER, str(folder)], cwd=REPO_ROOT,
                                        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=(folder / "stderr.log").open("wb"), creationflags=flags)
        except OSError:
            with self._lock:
                self._jobs.pop(run_id, None)
            raise
        threading.Thread(target=self._watch, args=(job,), name=f"job-{job_id}", daemon=True).start()
        return job

    # ------------------------------------------------------------------ watch / cancel
    def _watch(self, job: Job) -> None:
        assert job.proc is not None
        try:
            job.proc.wait(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            job.timed_out = True
            kill_tree(job.proc.pid)
            job.proc.wait()
        outcome = self._outcome(job)
        job.outcome = outcome
        try:
            self.on_finish(job, outcome)
        except Exception:  # noqa: BLE001 - the watcher thread must not die silently
            log.exception("finishing job %s failed", job.id)
        finally:
            with self._lock:                        # only now may the run start another job
                self._jobs.pop(job.run_id, None)
            job.done.set()

    @staticmethod
    def _outcome(job: Job) -> dict:
        if job.cancelled:
            return {"ok": False, "kind": "cancelled", "code": "cancelled", "message": "Cancelled.", "problems": []}
        if job.timed_out:
            return {"ok": False, "kind": "error", "code": "timeout", "problems": [],
                    "message": "The job took longer than the allowed time and was stopped."}
        path = job.folder / "result.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf8"))
            except ValueError:
                pass
        err = ""
        try:
            err = (job.folder / "stderr.log").read_text(encoding="utf8", errors="replace").strip().splitlines()[-1]
        except (OSError, IndexError):
            pass
        return {"ok": False, "kind": "crash", "code": "worker_died", "problems": [],
                "message": f"The worker process ended unexpectedly (exit code {job.proc.returncode}). {err}".strip()}

    def cancel(self, run_id: str) -> bool:
        job = self.active(run_id)
        if job is None or job.proc is None:
            return False
        job.cancelled = True
        kill_tree(job.proc.pid)
        return True

    @staticmethod
    def wait(job: Job, timeout: float) -> dict | None:
        return job.outcome if job.done.wait(timeout) else None

    def shutdown(self) -> None:
        for job in list(self._jobs.values()):
            if job.proc and job.proc.poll() is None:
                job.cancelled = True
                kill_tree(job.proc.pid)


def kill_orphan(pid: int | None) -> None:
    """After a restart: a worker of the previous service process may still be running; stop it."""
    if not pid or not psutil.pid_exists(pid):
        return
    try:
        if WORKER in " ".join(psutil.Process(pid).cmdline()):
            kill_tree(pid)
    except (psutil.Error, OSError):
        pass
