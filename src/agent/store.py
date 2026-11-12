"""Change-sets and the conversation, as files in the run folder: <run>/agent/<phase>/.

    session.json              the messages, in order
    changesets/cs-0001.json   one change-set: instruction, typed plan, preview, status, outcome
    snapshots/cs-0001/        the phase output as it was before cs-0001 was applied (for Undo)

Files rather than a database because the run folder is already the unit of storage, backup and deletion for
everything else in a run (state.json, audit, uploads). Writes are atomic (temp file + rename); one job per run at a
time (the job runner's lock) means there is a single writer.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter

from .operations import Operation

Status = Literal["proposed", "applied", "discarded", "undone", "stale", "failed"]
_OPS = TypeAdapter(list[Operation])
MAX_MESSAGES = 1000


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChangeSet(BaseModel):
    id: str
    phase: str
    status: Status = "proposed"
    instruction: str
    source: str                                  # rules | llm
    plan: list[dict]                             # typed operations, dumped
    preview: dict
    base_version: str
    created_at: str = Field(default_factory=now)
    applied_at: str | None = None
    undone_at: str | None = None
    error: str | None = None
    result: dict | None = None
    has_snapshot: bool = False
    review: dict | None = None                   # guardrails.assess: {"level", "reasons"}; elevated = confirm to apply
    applied_by: str | None = None                # who confirmed the apply (the access model has one shared identity today)
    model: dict | None = None                    # for model-made plans: {"model", "digest", "prompt_version", "repaired"}

    def operations(self) -> list:
        return _OPS.validate_python(self.plan)

    def brief(self) -> dict:
        return {"id": self.id, "status": self.status, "instruction": self.instruction, "source": self.source,
                "summary": self.preview.get("summary"), "created_at": self.created_at, "applied_at": self.applied_at,
                "undone_at": self.undone_at, "error": self.error, "review": self.review}


class Message(BaseModel):
    id: int
    role: Literal["user", "agent"]
    kind: Literal["text", "proposal", "ask", "answer", "result", "error"] = "text"
    text: str
    changeset: str | None = None
    at: str = Field(default_factory=now)


def _write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class AgentStore:
    def __init__(self, run: Path, phase: str):
        self.root = Path(run) / "agent" / phase
        self.phase = phase

    # ── messages ─────────────────────────────────────────────────────────────
    def messages(self) -> list[Message]:
        p = self.root / "session.json"
        if not p.exists():
            return []
        return [Message(**m) for m in json.loads(p.read_text("utf8")).get("messages", [])]

    def add_message(self, role: str, text: str, kind: str = "text", changeset: str | None = None) -> Message:
        msgs = self.messages()
        msg = Message(id=(msgs[-1].id + 1) if msgs else 1, role=role, kind=kind, text=text, changeset=changeset)
        msgs = (msgs + [msg])[-MAX_MESSAGES:]
        _write(self.root / "session.json", json.dumps({"messages": [m.model_dump() for m in msgs]}, ensure_ascii=False, indent=1))
        return msg

    # ── change-sets ──────────────────────────────────────────────────────────
    def _cs_path(self, cs_id: str) -> Path:
        if not cs_id.startswith("cs-") or not cs_id[3:].isdigit():
            raise KeyError(cs_id)
        return self.root / "changesets" / f"{cs_id}.json"

    def next_id(self) -> str:
        d = self.root / "changesets"
        n = max((int(p.stem[3:]) for p in d.glob("cs-*.json")), default=0) if d.exists() else 0
        return f"cs-{n + 1:04d}"

    def save(self, cs: ChangeSet) -> None:
        _write(self._cs_path(cs.id), cs.model_dump_json(indent=1))

    def get(self, cs_id: str) -> ChangeSet | None:
        try:
            p = self._cs_path(cs_id)
        except KeyError:
            return None
        return ChangeSet.model_validate_json(p.read_text("utf8")) if p.exists() else None

    def all(self) -> list[ChangeSet]:
        d = self.root / "changesets"
        return [ChangeSet.model_validate_json(p.read_text("utf8")) for p in sorted(d.glob("cs-*.json"))] if d.exists() else []

    def last_applied(self) -> ChangeSet | None:
        applied = [c for c in self.all() if c.status == "applied"]
        return max(applied, key=lambda c: (c.applied_at or "", c.id)) if applied else None

    # ── snapshots ────────────────────────────────────────────────────────────
    def snapshot_dir(self, cs_id: str) -> Path:
        self._cs_path(cs_id)
        return self.root / "snapshots" / cs_id

    def prune_snapshots(self, keep: int) -> None:
        """Keep the snapshots of the newest `keep` applied change-sets; older ones can no longer be undone."""
        applied = sorted((c for c in self.all() if c.status == "applied"), key=lambda c: (c.applied_at or "", c.id))
        for cs in applied[:-keep] if keep else applied:
            d = self.root / "snapshots" / cs.id
            if d.exists():
                shutil.rmtree(d)
            if cs.has_snapshot:
                cs.has_snapshot = False
                self.save(cs)
        for d in (self.root / "snapshots").glob("cs-*") if (self.root / "snapshots").exists() else []:
            cs = self.get(d.name)
            if cs is None or cs.status != "applied":                    # undone / failed: nothing to go back to
                shutil.rmtree(d)
