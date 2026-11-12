"""Fix-with-agent inside the pipeline: when the chat may act, which job runs it, and what an applied change means for
the run's state.

    message  -> short worker job ("agent_message"), the request waits: plan + preview, nothing changes
    apply    -> worker job ("agent_apply"), like a review upload: the phase goes running -> review, later phases
                are discarded because their input changed
    undo     -> worker job ("agent_undo"), same state handling as apply
    discard, state, issues, rows -> answered here, no job (they only read, or flip one proposal's status)

Agent actions are allowed while a phase is in review or accepted, the same as uploading an edited review workbook.
The model is never asked to accept a phase, restart it, or load anything; those stay buttons.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from src.agent.errors import AgentError
from src.agent.store import AgentStore
from src.llm_interface.ollama_status import probe
from src.orchestration.phases.base import PhaseError
from src.orchestration.runner import Busy
from src.storage.run_store import now

if TYPE_CHECKING:
    from src.orchestration.orchestrator import Pipeline

REVIEW, ACCEPTED, RUNNING, FAILED = "review", "accepted", "running", "failed"
JOB_ACTIONS = ("agent_apply", "agent_undo")
HTTP = {"no_such_changeset": 404, "no_such_table": 404, "not_proposed": 409, "stale_proposal": 409,
        "nothing_to_undo": 409, "not_ready": 409, "agent_not_available": 400, "empty_message": 400,
        "message_too_long": 400, "confirmation_required": 409, "pattern_too_slow": 400, "bad_pattern": 400}


# ── worker side ─────────────────────────────────────────────────────────────
def build_service(phase, ctx, params: dict):
    """The AgentService for one worker job (imports stay out of the API process's hot path)."""
    from src.agent.planner import LlmPlanner, Planner
    from src.agent.service import AgentService
    from src.structure.llm.client import OllamaClient
    llm = params.get("agent_llm") or {}
    planner = Planner(LlmPlanner(OllamaClient(None, llm["model"], None)) if llm.get("mode") == "on" else None)
    from src.agent.recipes import RecipeLibrary
    from src.storage.audit import AuditLog
    return AgentService(phase.agent_workspace(ctx, params), AgentStore(ctx.run, phase.key), planner,
                        RecipeLibrary(params.get("recipes_dir")), audit=AuditLog(params.get("audit_db")),
                        run_id=Path(ctx.run).name)


def run_action(phase, ctx, action: str, params: dict) -> dict:
    """What the worker calls for an agent action. AgentError becomes a PhaseError the runner reports."""
    try:
        svc = build_service(phase, ctx, params)
        if action == "agent_message":
            return svc.message(params.get("text", ""))
        if action == "agent_apply":
            return svc.apply(params.get("changeset", ""), confirm=bool(params.get("confirm")))
        if action == "agent_undo":
            return svc.undo()
    except AgentError as e:
        raise PhaseError(e.code, e.message) from e
    raise PhaseError("bad_action", f"unknown agent action {action}")


def reset_store(run: Path, key: str, wipe: bool) -> None:
    """The phase's data changed outside the agent. wipe=True (the phase re-ran or was cleared): forget the
    conversation and change-sets. wipe=False (a review workbook was applied): keep the history, but proposals are
    stale and older snapshots can no longer be restored without losing the upload."""
    store = AgentStore(run, key)
    if not store.root.exists():
        return
    if wipe:
        shutil.rmtree(store.root, ignore_errors=True)
        return
    for cs in store.all():
        changed = False
        if cs.status == "proposed":
            cs.status, changed = "stale", True
        if cs.has_snapshot:
            cs.has_snapshot, changed = False, True
        if changed:
            store.save(cs)
    shutil.rmtree(store.root / "snapshots", ignore_errors=True)


# ── API side ────────────────────────────────────────────────────────────────
class AgentController:
    def __init__(self, pipe: "Pipeline"):
        self.pipe = pipe

    def _error(self, e: Exception):
        from src.orchestration.orchestrator import PipelineError
        if isinstance(e, (AgentError, PhaseError)):
            return PipelineError(HTTP.get(e.code, 400), e.code, e.message)
        return e

    def _adapter(self, key: str):
        from src.orchestration.orchestrator import PipelineError
        adapter = self.pipe._phase(key)
        if not adapter.supports_agent:
            raise PipelineError(400, "agent_not_available", f"The agent is not available for Phase {key} yet.")
        return adapter

    def _usable(self, st: dict, key: str) -> None:
        from src.orchestration.orchestrator import PipelineError
        if st.get("job"):
            raise PipelineError(409, "busy", "A job is in progress. Wait for it to finish.")
        if st["phases"][key]["status"] not in (REVIEW, ACCEPTED):
            raise PipelineError(409, "not_reviewable", f"Phase {key} has no results to work on yet.")

    def recipes_dir(self) -> str:
        """Saved transformation recipes, shared by every run (beside the runs folder, not inside it)."""
        return str(Path(self.pipe.settings.workspace).parent / "recipes")

    def _llm_params(self) -> dict:
        s = self.pipe.settings
        model = s.agent_model or s.llm_model
        return {"mode": "on" if s.agent_llm else "off", "model": model}

    def llm_status(self) -> dict:
        p = self._llm_params()
        if p["mode"] == "off":
            return {"enabled": False, "available": False, "model": None,
                    "message": "The model is switched off in settings; the chat understands its built-in commands only."}
        st = probe(p["model"])
        return {"enabled": True, "available": st.available, "model": p["model"],
                "message": ("The local model is ready for free wording." if st.available else
                            st.message + " The chat still understands its built-in commands.")}

    # ── reads ────────────────────────────────────────────────────────────────
    def state(self, run_id: str, key: str) -> dict:
        self._adapter(key)
        st = self.pipe.store.load(run_id)
        run = self.pipe.store.path(run_id)
        store = AgentStore(run, key)
        css = store.all()
        last = store.last_applied()
        ph = st["phases"][key]
        job = st.get("job")
        return {"phase": key, "status": ph["status"],
                "usable": ph["status"] in (REVIEW, ACCEPTED) and not job,
                "working": bool(job and job.get("phase") == key and str(job.get("action", "")).startswith("agent")),
                "job_action": job.get("action") if job else None,
                "messages": [m.model_dump() for m in store.messages()],
                "changesets": {c.id: {**c.brief(), "preview": c.preview} for c in css},
                "undo": last.brief() if last and last.has_snapshot else None,
                "error": ph.get("agent_error"), "llm": self.llm_status()}

    def issues(self, run_id: str, key: str) -> dict:
        adapter = self._adapter(key)
        try:
            return adapter.agent_views(self.pipe.store.path(run_id)).issues()
        except (AgentError, PhaseError) as e:
            raise self._error(e) from e

    def rows(self, run_id: str, key: str, table: str, offset: int, limit: int, rule: str | None, column: str | None,
             only: str | None) -> dict:
        adapter = self._adapter(key)
        try:
            return adapter.agent_views(self.pipe.store.path(run_id)).rows(table, offset, limit, rule, column, only)
        except (AgentError, PhaseError) as e:
            raise self._error(e) from e

    # ── actions ──────────────────────────────────────────────────────────────
    def message(self, run_id: str, key: str, text: str) -> dict:
        from src.orchestration.orchestrator import PipelineError
        self._adapter(key)
        st = self.pipe.store.load(run_id)
        self._usable(st, key)
        params = {**st["phases"][key]["params"], "text": text, "agent_llm": self._llm_params(),
                  "recipes_dir": self.recipes_dir(), "audit_db": str(self.pipe.settings.audit_db)}
        outcome = self.pipe._sync_job(run_id, key, "agent_message", params)
        if not outcome.get("ok"):
            raise PipelineError(HTTP.get(outcome.get("code"), 500 if outcome.get("kind") == "crash" else 400),
                                outcome.get("code") or "agent_failed", outcome.get("message") or "The agent failed.")
        return {"reply": outcome["data"], "state": self.state(run_id, key)}

    def _launch(self, run_id: str, key: str, action: str, extra: dict) -> dict:
        from src.orchestration.orchestrator import PipelineError
        self._adapter(key)
        with self.pipe.store.edit(run_id) as st:
            self._usable(st, key)
            ph = st["phases"][key]
            params = {**ph["params"], **extra, "agent_llm": {"mode": "off"}, "recipes_dir": self.recipes_dir(),
                      "audit_db": str(self.pipe.settings.audit_db)}
            try:
                job = self.pipe.runner.launch(run_id, key, action, params)
            except Busy as e:
                raise PipelineError(429, "busy", str(e)) from e
            ph["_agent"] = {"was": ph["status"], "action": action}
            ph["agent_error"] = None
            ph["status"] = RUNNING
            st["job"] = {"id": job.id, "phase": key, "action": action, "started_at": now(), "pid": job.proc.pid}
        return self.pipe.view(run_id)

    def apply(self, run_id: str, key: str, changeset: str, confirm: bool = False) -> dict:
        from src.agent import guardrails
        from src.orchestration.orchestrator import PipelineError
        cs = AgentStore(self.pipe.store.path(run_id), key).get(changeset)
        if cs is None:
            raise PipelineError(404, "no_such_changeset", f"There is no change {changeset}.")
        if cs.status != "proposed":
            raise PipelineError(409, "not_proposed", f"Change {changeset} is {cs.status}; only a proposed change can be applied.")
        if guardrails.needs_confirmation(cs.review) and not confirm:
            raise PipelineError(409, "confirmation_required", "This change needs an explicit confirmation: "
                                + " ".join(cs.review["reasons"]), {"reasons": cs.review["reasons"]})
        view = self._launch(run_id, key, "agent_apply", {"changeset": changeset, "confirm": bool(confirm)})
        self.pipe.audit.record("agent.apply_requested", run_id, key, changeset=changeset, source=cs.source,
                               operations=[op.get("op") for op in cs.plan])
        return view

    def undo(self, run_id: str, key: str) -> dict:
        from src.orchestration.orchestrator import PipelineError
        last = AgentStore(self.pipe.store.path(run_id), key).last_applied()
        if last is None or not last.has_snapshot:
            raise PipelineError(409, "nothing_to_undo", "There is no applied change that can be undone.")
        view = self._launch(run_id, key, "agent_undo", {})
        self.pipe.audit.record("agent.undo_requested", run_id, key, changeset=last.id)
        return view

    def discard(self, run_id: str, key: str, changeset: str) -> dict:
        from src.agent.service import AgentService
        self._adapter(key)
        st = self.pipe.store.load(run_id)
        if st.get("job"):
            from src.orchestration.orchestrator import PipelineError
            raise PipelineError(409, "busy", "A job is in progress. Wait for it to finish.")
        svc = AgentService(None, AgentStore(self.pipe.store.path(run_id), key), None)   # discard needs no data
        try:
            svc.discard(changeset)
        except AgentError as e:
            raise self._error(e) from e
        self.pipe.audit.record("agent.discarded", run_id, key, changeset=changeset)
        return self.state(run_id, key)

    # ── job outcome (runner's watcher thread, inside the state lock) ──────────
    def finished(self, st: dict, run: Path, job, outcome: dict) -> None:
        ph = st["phases"][job.phase]
        was = ph.pop("_agent", None) or {}
        if outcome["ok"]:
            ph["status"] = REVIEW                                   # results changed: look again, accept again
            ph["error"] = ph["agent_error"] = None
            rec = ph["agent"] = ph.get("agent") or {"applied": 0, "undone": 0, "last_at": None}
            rec["applied" if job.action == "agent_apply" else "undone"] += 1
            rec["last_at"] = now()
            self.pipe._clear_from(st, run, job.phase, include_self=False)          # later phases saw the old data
        else:
            crashed = outcome["kind"] == "crash"
            ph["status"] = FAILED if crashed else was.get("was", REVIEW)
            ph["agent_error"] = {"code": outcome.get("code"), "message": outcome["message"], "at": now()}
            if crashed:
                ph["error"] = {"code": outcome.get("code"), "message": outcome["message"]}
