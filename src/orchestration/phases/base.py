"""What every phase adapter shares: the run's folder layout, event emitting, errors, and the adapter interface.

An adapter is the only code that knows how to call one phase (A to D). The orchestrator and the API see only this
interface, so a later phase (E mapping, G checks, F loading) is added by writing one more adapter.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.storage.run_store import append_event


# ------------------------------------------------------------------ errors
class PhaseError(Exception):
    """A failure the analyst can act on. `code` is stable, `message` is safe to show."""

    def __init__(self, code: str, message: str, problems: list | None = None):
        super().__init__(message)
        self.code, self.message, self.problems = code, message, problems or []


class ReviewRejected(PhaseError):
    """An uploaded workbook or config failed its pre-check; nothing was changed."""

    def __init__(self, problems: list, message: str = "The uploaded file was not accepted; nothing was changed."):
        super().__init__("upload_rejected", message, problems)


# ------------------------------------------------------------------ layout of one run folder
class Paths:
    def __init__(self, run: Path):
        self.run = Path(run)
        self.inputs = self.run / "inputs"
        self.phase_a = self.run / "phase_a"                    # Phase A output (wiped by every run)
        self.phase_a_state = self.run / "phase_a_state"        # structure config + analyst cell edits (kept across uploads)
        self.work = self.run / "work"                          # Phases B, C and D share this run folder
        self.config = self.run / "config"
        self.uploads = self.run / "uploads"
        self.final = self.run / "final"

    def subdir(self, name: str) -> Path:
        p = self.work / name
        return p


# ------------------------------------------------------------------ events
class Emitter:
    """Writes live events to the run's `events.jsonl` (what the UI shows while a job runs)."""

    def __init__(self, run: Path, job_id: str, phase: str):
        self.run, self.job_id, self.phase = Path(run), job_id, phase

    def _put(self, **event) -> None:
        append_event(self.run, {"job": self.job_id, "phase": self.phase, **event})

    def stage(self, name: str, pct: float | None = None, detail: str | None = None) -> None:
        self._put(type="stage", stage=name, pct=pct, detail=detail)

    def log(self, message: str, level: str = "info") -> None:
        self._put(type="log", level=level, message=message[:600])

    def progress(self, done: int, total: int, label: str = "") -> None:
        self._put(type="progress", done=done, total=total, label=label)


class EventLogHandler(logging.Handler):
    """Forwards a phase's own log lines (`dq.*` loggers) to the UI as log events."""

    def __init__(self, emit: Emitter):
        super().__init__(logging.INFO)
        self.emit_to = emit
        self.total = 0
        self.done = 0

    def emit(self, record: logging.LogRecord) -> None:   # noqa: A003 - logging API
        msg = record.getMessage()
        self.emit_to.log(msg, "warn" if record.levelno >= logging.WARNING else "info")
        m = re.match(r"phase B: (\d+) table", msg)
        if m:
            self.total, self.done = int(m.group(1)), 0
        elif self.total and re.search(r": \d+ rows in, \d+ out", msg):
            self.done += 1
            self.emit_to.progress(self.done, self.total, "tables cleaned")


@dataclass
class Context:
    run: Path
    emit: Emitter
    settings_path: str                                  # config/settings.yaml
    state: dict = field(default_factory=dict)           # snapshot of state.json when the job started
    cancelled: callable = lambda: False

    @property
    def paths(self) -> Paths:
        return Paths(self.run)


@dataclass
class Artifact:
    id: str                       # stable download id, e.g. "structural-review"
    label: str
    path: Path                    # absolute
    kind: str                     # review | data | audit | log | config
    phase: str
    description: str = ""
    primary: bool = False         # the file the analyst downloads to edit at this phase


# ------------------------------------------------------------------ the interface
class Phase:
    key: str = ""                 # "A" .. "D"
    title: str = ""
    short: str = ""               # one line for the stepper
    describes: str = ""           # what the phase does, for the start screen
    uses_llm: bool = False
    needs_config: bool = False    # client config workbook is filled in and uploaded before the run
    review_name: str = ""         # file name of the review workbook the analyst edits

    # -- lifecycle -----------------------------------------------------
    def run(self, ctx: Context, params: dict) -> dict:
        raise NotImplementedError

    def review(self, ctx: Context, upload: Path, params: dict) -> dict:
        """Apply an edited review workbook. Raises ReviewRejected; on success outputs are rewritten."""
        raise NotImplementedError

    def clear(self, run: Path) -> None:
        """Remove this phase's outputs (a restart, or an upstream change)."""
        raise NotImplementedError

    # -- reading results -----------------------------------------------
    def summary(self, run: Path) -> dict:
        raise NotImplementedError

    def artifacts(self, run: Path) -> list[Artifact]:
        raise NotImplementedError

    def review_workbook(self, run: Path) -> Path | None:
        return next((a.path for a in self.artifacts(run) if a.primary), None)

    def can_continue(self, run: Path) -> tuple[bool, str | None]:
        """May the analyst move on to the next phase? (False, reason) blocks the Continue button."""
        return True, None

    # -- start-screen choices about columns (Phase B: where zero is a valid value) ------------
    def columns(self, ctx: Context) -> list[dict]:
        return []

    # -- config phases (C, D) ------------------------------------------
    def candidates(self, ctx: Context) -> list[dict]:
        return []

    def make_template(self, ctx: Context, table: str | None) -> Path:
        raise NotImplementedError

    def check_config(self, ctx: Context, workbook: Path, table: str | None) -> dict:
        raise NotImplementedError

    # -- Fix-with-agent (src/agent) ---------------------------------------
    supports_agent: bool = False

    def agent_workspace(self, ctx: Context, params: dict):
        """The phase's data as an agent Workspace (preview / apply / restore). Only phases with supports_agent."""
        raise PhaseError("agent_not_available", f"The agent is not available for Phase {self.key} yet.")

    def agent_views(self, run: Path):
        """Read-only views for the agent screen (grouped issues, pages of rows)."""
        raise PhaseError("agent_not_available", f"The agent is not available for Phase {self.key} yet.")


# ------------------------------------------------------------------ small shared helpers
def friendly(message: str) -> str:
    """Phase messages were written for a command line; the web app has no `--table` flag."""
    message = message.replace("'Variable Mapping': no variables marked Y",
                              "Phase D compares each Support variable (impressions, clicks, sales ...) with its Spend variable. In the "
                              "'Variable Mapping' sheet type Y next to a Support variable and name its Spend variable in 'Spend Variables'")
    return (message.replace("Pick one with --table", "Choose the table to check above")
            .replace("none matches --table", "none matches the chosen table").replace("--table", "the table choice"))



def ensure_workbook(path: Path) -> None:
    """A review upload must at least open as an .xlsx; the phases' own pre-checks assume it does."""
    import openpyxl
    try:
        openpyxl.load_workbook(path, read_only=True).close()
    except Exception as e:  # noqa: BLE001 - zip errors, bad XML, wrong format: all mean "not a workbook"
        raise ReviewRejected([{"code": "unreadable", "message": "The file cannot be opened as an Excel (.xlsx) workbook. "
                               "Download the workbook again and edit that copy."}]) from e



def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return default


def read_lines(path: Path, limit: int | None = None) -> list[dict]:
    out = []
    try:
        with Path(path).open(encoding="utf8") as fh:
            for line in fh:
                if line.strip():
                    out.append(json.loads(line))
                    if limit and len(out) >= limit:
                        break
    except (OSError, ValueError):
        pass
    return out


def wipe(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def copy_version(src: Path, folder: Path, label: str) -> Path:
    """Keep every uploaded workbook: <folder>/<label>_<timestamp>_<name>."""
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / f"{label}_{time.strftime('%Y%m%d-%H%M%S')}_{src.name}"
    shutil.copy(src, dst)
    return dst
