"""A phase's editable data, seen by the agent: load, preview (dry run), apply, snapshot and restore.

`Workspace` is the interface; each phase that supports the agent provides one. Phase B's `HygieneWorkspace` reuses the
review-upload machinery of `src/hygiene/review.py` (load_clean_tables, recheck, add_columns, split_rows, save), so a
change made from the chat and the same change typed into Hygiene_Review.xlsx end in the same tables, audit and review
items. The orchestration layer builds the workspace (it knows the run's settings); this module knows nothing of jobs
or HTTP.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from src.hygiene.pipeline import OUT, save
from src.hygiene.review import add_columns, load_clean_tables, recheck, split_rows
from src.hygiene.table import Table

from . import describe
from .errors import AgentError, StaleProposal
from .executors import execute
from .session import AGENT_RULE, EditSession

SNAPSHOT_SKIP = {"review_versions"}            # kept in place across restores: the history of downloads and uploads


class Workspace(ABC):
    phase: str = ""

    @abstractmethod
    def version(self) -> str:
        """Changes whenever the phase's data or review items change (any apply, upload or re-run)."""

    @abstractmethod
    def load(self) -> list[Table]:
        ...

    @abstractmethod
    def preview(self, ops: list, changeset_id: str, instruction: str | None) -> dict:
        ...

    @abstractmethod
    def apply(self, ops: list, changeset_id: str, instruction: str | None, expected_version: str,
              snapshot_to: Path | None) -> dict:
        ...

    @abstractmethod
    def restore(self, snapshot: Path) -> None:
        ...


class HygieneWorkspace(Workspace):
    phase = "B"

    def __init__(self, run_dir: Path, cfg: dict):
        self.run_dir = Path(run_dir)
        self.out = self.run_dir / OUT
        self.cfg = cfg
        if not (self.out / "run.json").exists():
            raise AgentError("not_ready", "Phase B has no results to work on yet. Run it first.")

    # ── state ────────────────────────────────────────────────────────────────
    def version(self) -> str:
        h = hashlib.sha256()
        for name in ("run.json", "needs_review.json", "remediation_audit.json"):
            p = self.out / name
            st = p.stat() if p.exists() else None
            h.update(f"{name}:{st.st_size if st else -1}:{st.st_mtime_ns if st else -1};".encode())
        return h.hexdigest()[:16]

    def load(self) -> list[Table]:
        return load_clean_tables(self.out, self.cfg)

    # ── preview / apply ──────────────────────────────────────────────────────
    def _run(self, tables: list[Table], ops: list, changeset_id: str, instruction: str | None) -> tuple[EditSession, dict]:
        before = {t.ref: len(t.audit) for t in tables}
        s = EditSession(tables, changeset_id, instruction)
        execute(s, ops)
        answered = s.close_answered()
        for t in tables:                                    # what the review upload does after the edits, in its order
            recheck(t, s.changed[t.ref])
            if t.ref in s.added:
                add_columns(t, s.added[t.ref])
            split_rows(t, s.splits.get(t.ref, {}))
            t.held.clear()
            t.hold_open_multi_values()
        follow_up = sum(1 for t in tables for a in t.audit[before[t.ref]:] if a["rule"] != AGENT_RULE)
        effects = [e.as_dict() for e in s.effects]
        totals = {k: sum(e[k] for e in effects) for k in ("cells", "items_resolved", "rows_removed", "rows_added")}
        totals["items_resolved"] += answered
        totals["columns_added"] = [c for e in effects for c in e["columns_added"]]
        totals["columns_renamed"] = [r for e in effects for r in e["columns_renamed"]]
        totals["tables"] = sorted({r for e in effects for r in e["tables"]})
        open_after = sum(1 for t in tables for r in t.review if r["status"] == "open")
        preview = {
            "operations": [{"text": describe.operation(op), "effect": e, "summary": describe.effect_line(e)}
                           for op, e in zip(ops, effects)],
            "totals": totals, "summary": describe.effect_line(totals),
            "items_answered_by_edits": answered, "follow_up_fixes": follow_up, "open_items_after": open_after,
            "changes_anything": any(e["cells"] or e["items_resolved"] or e["rows_removed"] or e["rows_added"]
                                    or e["columns_added"] or e["columns_renamed"] for e in effects) or bool(answered),
        }
        return s, preview

    def preview(self, ops: list, changeset_id: str, instruction: str | None) -> dict:
        _, preview = self._run(self.load(), ops, changeset_id, instruction)
        return preview

    def apply(self, ops: list, changeset_id: str, instruction: str | None, expected_version: str,
              snapshot_to: Path | None) -> dict:
        if self.version() != expected_version:
            raise StaleProposal()
        tables = self.load()
        _, result = self._run(tables, ops, changeset_id, instruction)
        if snapshot_to is not None:
            self._snapshot(snapshot_to)
        run = json.loads((self.out / "run.json").read_text("utf8"))
        save(self.run_dir, tables, run["unreadable"], self.cfg, run.get("skipped", []))
        return result

    # ── undo ─────────────────────────────────────────────────────────────────
    def _snapshot(self, dest: Path) -> None:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.out, dest, ignore=lambda d, names: [n for n in names if Path(d) == self.out and n in SNAPSHOT_SKIP])

    def restore(self, snapshot: Path) -> None:
        if not (snapshot / "run.json").exists():
            raise AgentError("no_snapshot", "The saved state for this change is missing; it cannot be undone.")
        for p in self.out.iterdir():
            if p.name in SNAPSHOT_SKIP:
                continue
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        for p in snapshot.iterdir():
            dst = self.out / p.name
            shutil.copytree(p, dst) if p.is_dir() else shutil.copy2(p, dst)
