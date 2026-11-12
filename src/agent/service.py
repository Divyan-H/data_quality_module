"""The agent's use cases: what the worker process runs for each request.

    message(text)   plan the instruction; a change becomes a stored, previewed change-set (nothing is changed)
    apply(cs_id)    apply a previewed change-set (snapshot first, so it can be undone)
    undo()          restore the snapshot of the last applied change-set
    discard(cs_id)  drop a proposal
    state()         the conversation and the change-sets, for the UI

The service talks to a `Workspace` (the phase's data) and a `Planner`; it knows nothing about HTTP, jobs or which
phase it serves. Errors the analyst can act on are AgentError; they become chat messages, not crashes.
"""
from __future__ import annotations

import logging
import re

from . import guardrails
from .errors import AgentError
from .operations import Plan
from .planner import Planner, PlanningContext
from .recipes import RecipeLibrary, replayable
from .store import AgentStore, ChangeSet, now
from .workspace import Workspace

log = logging.getLogger("dq.agent")
HISTORY_MESSAGES = 12                             # stored messages looked at when building the model's memory
UNDO_DEPTH = 10                                   # applied change-sets that keep a snapshot (older ones cannot be undone)


class AgentService:
    def __init__(self, workspace: Workspace | None, store: AgentStore, planner: Planner | None, library: RecipeLibrary | None = None,
                 audit=None, run_id: str | None = None, confirm_cells: int = guardrails.CONFIRM_CELLS):
        self.ws, self.store, self.planner = workspace, store, planner
        self.library = library or getattr(workspace, "library", None) or RecipeLibrary(None)
        self.audit, self.run_id, self.confirm_cells = audit, run_id, confirm_cells

    def _audit(self, action: str, **detail) -> None:
        if self.audit is not None:
            self.audit.record(action, self.run_id, self.store.phase, **detail)

    def _history(self) -> list[dict]:
        """The conversation so far, for the model's memory: what was said, and for proposals the typed plan and what
        became of it. The message being planned now is not part of it."""
        sets = {c.id: c for c in self.store.all()}
        turns = []
        for m in self.store.messages()[-HISTORY_MESSAGES:]:
            turn = {"role": m.role, "text": m.text if m.role == "user" else m.text.split("\n", 1)[0]}
            cs = sets.get(m.changeset) if m.kind == "proposal" and m.changeset else None
            if cs is not None:
                turn.update(ops=cs.plan, status=cs.status)
            if m.kind in ("text", "proposal", "ask"):                   # results and errors add nothing to plan from
                turns.append(turn)
        return turns

    # ── read ─────────────────────────────────────────────────────────────────
    def state(self) -> dict:
        css = self.store.all()
        last = self.store.last_applied()
        return {"messages": [m.model_dump() for m in self.store.messages()],
                "changesets": {c.id: {**c.brief(), "preview": c.preview} for c in css},
                "can_undo": bool(last and last.has_snapshot), "undo": last.brief() if last and last.has_snapshot else None}

    # ── message ──────────────────────────────────────────────────────────────
    def message(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise AgentError("empty_message", "Type what you would like to change.")
        if len(text) > 2000:
            raise AgentError("message_too_long", "Keep an instruction under 2,000 characters; split it into steps.")
        history = self._history()                                # before this message is stored
        user = self.store.add_message("user", text)
        try:
            plan = self._recipe_command(text)
        except AgentError as e:
            plan = Plan(answer=e.message)
        if plan is None:
            tables = self.ws.load()
            plan = self.planner.plan(text, PlanningContext(tables, self.ws.phase, history))
        llm = getattr(self.planner, "llm", None)
        trace = dict(llm.trace) if llm is not None and plan.source == "llm" else None
        kind = "ask" if plan.ask else "answer" if plan.answer else "plan"
        log.info("agent plan", extra={"source": plan.source, "kind": kind, "operations": [op.op for op in plan.ops],
                                      "chars": len(text)})
        self._audit("agent.planned", source=plan.source, kind=kind, operations=[op.op for op in plan.ops],
                    instruction_chars=len(text), model_calls=(trace or {}).get("calls", 0),
                    repaired=(trace or {}).get("repaired", False))
        if plan.ask:
            reply = self.store.add_message("agent", plan.ask, "ask")
            return {"messages": [user.model_dump(), reply.model_dump()], "changeset": None}
        if plan.answer:
            reply = self.store.add_message("agent", plan.answer, "answer")
            return {"messages": [user.model_dump(), reply.model_dump()], "changeset": None}
        cs_id = self.store.next_id()
        version = self.ws.version()
        try:
            preview = self.ws.preview(plan.ops, cs_id, text)
        except AgentError as e:
            reply = self.store.add_message("agent", e.message, "error")
            return {"messages": [user.model_dump(), reply.model_dump()], "changeset": None}
        if not preview["changes_anything"]:
            notes = [n for o in preview["operations"] for n in o["effect"]["notes"]]
            unmatched = [u for o in preview["operations"] for u in o["effect"]["unmatched"]]
            text_ = "Nothing would change. " + " ".join(o["text"] for o in preview["operations"])
            if notes:
                text_ += " " + " ".join(notes)
            if unmatched:
                text_ += f" Not found: {', '.join(map(str, unmatched[:10]))}."
            reply = self.store.add_message("agent", text_, "answer")
            return {"messages": [user.model_dump(), reply.model_dump()], "changeset": None}
        review = guardrails.assess(plan.ops, preview, self.confirm_cells)
        model = None
        if trace is not None and llm is not None and llm.client is not None:
            model = {"model": llm.client.model, "digest": llm.client.digest, "prompt_version": trace.get("prompt_version"),
                     "repaired": trace.get("repaired", False)}
        cs = ChangeSet(id=cs_id, phase=self.store.phase, instruction=text, source=plan.source,
                       plan=[op.model_dump() for op in plan.ops], preview=preview, base_version=version,
                       review=review, model=model)
        self.store.save(cs)
        caution = ("\nPlease check this one carefully before applying: " + " ".join(review["reasons"])
                   if guardrails.needs_confirmation(review) else "")
        reply = self.store.add_message("agent", "Here is what I would change. Nothing is applied until you click Apply.\n"
                                       + "\n".join(f"• {o['text']} {o['summary']}" for o in preview["operations"]) + caution,
                                       "proposal", cs_id)
        return {"messages": [user.model_dump(), reply.model_dump()], "changeset": {**cs.brief(), "preview": preview}}

    def _recipe_command(self, text: str) -> Plan | None:
        """Recipe commands: save this run's steps to the library, list it, show the steps, or use a saved recipe (its
        steps become an ordinary proposal: previewed, applied with a click). Phase T saves its transformation recipe;
        Phase B saves the value fixes applied in this run that do not name Excel rows."""
        t = " ".join(text.strip().rstrip(".!").split())
        saving = re.fullmatch(r"(?:save|store|keep)\s+(?:this\s+|the\s+|these\s+)?(?:recipe|fixes|steps)\s+(?:as|named|called)\s+[\"']?(.+?)[\"']?", t, re.I)
        using = re.fullmatch(r"(?:use|load|apply|run|reuse)\s+(?:the\s+)?(?:saved\s+)?recipe\s+[\"']?(.+?)[\"']?", t, re.I)
        listing = re.fullmatch(r"(?:list|show)\s+(?:the\s+|my\s+|all\s+)?(?:saved\s+)?recipes", t, re.I)
        showing = re.fullmatch(r"(?:show|list|what are)\s+(?:the\s+|my\s+)?(?:recipe|steps)(?:\s+so\s+far)?", t, re.I)
        if not (saving or using or listing or showing):
            return None
        from . import describe
        phase = self.ws.phase
        if phase == "T":
            steps, skipped = self.ws.recipe(), []
        else:
            applied = sorted((c for c in self.store.all() if c.status == "applied"), key=lambda c: (c.applied_at or "", c.id))
            steps, skipped = replayable([op for c in applied for op in c.operations()])
        if saving:
            if not steps:
                raise AgentError("empty_recipe", "There are no steps to save yet"
                                 + (" (steps that name Excel rows are left out: they fit this file only)." if skipped else "."))
            name = saving.group(1).strip()
            self.library.save(name, [op.model_dump() for op in steps], self.store.phase, phase=phase)
            note = f" {len(skipped)} step(s) naming Excel rows were left out: they fit this file only." if skipped else ""
            return Plan(answer=f"Saved the recipe ({len(steps)} step(s)) as '{name}'.{note} Say \"use recipe {name}\" "
                               "on another run to apply it.")
        if listing:
            entries = [e for e in self.library.entries() if e["phase"] == phase]
            return Plan(answer=("Saved recipes for this phase: " + ", ".join(f"{e['name']} ({e['steps']} step(s))" for e in entries)
                                + ". Say \"use recipe <name>\" to apply one.") if entries else
                        "No recipe is saved for this phase yet. Apply some changes, then say \"save recipe as <name>\".")
        if showing:
            lines = [f"{n}. {describe.operation(op)}" for n, op in enumerate(steps, 1)]
            extra = f"\n({len(skipped)} applied step(s) name Excel rows and would not be saved.)" if skipped else ""
            return Plan(answer=("The steps a saved recipe would hold:\n" + "\n".join(lines) + extra) if lines else
                        "No replayable steps yet." + extra)
        return Plan(ops=self.library.load(using.group(1), phase), source="rules")

    # ── apply ────────────────────────────────────────────────────────────────
    def _proposal(self, cs_id: str) -> ChangeSet:
        cs = self.store.get(cs_id)
        if cs is None:
            raise AgentError("no_such_changeset", f"There is no change {cs_id}.")
        if cs.status != "proposed":
            raise AgentError("not_proposed", f"Change {cs_id} is {cs.status}; only a proposed change can be applied.")
        return cs

    def _stale_others(self, keep: str | None) -> None:
        for other in self.store.all():
            if other.status == "proposed" and other.id != keep:
                other.status = "stale"
                self.store.save(other)

    def apply(self, cs_id: str, confirm: bool = False, applied_by: str = "analyst") -> dict:
        cs = self._proposal(cs_id)
        if guardrails.needs_confirmation(cs.review) and not confirm:
            raise AgentError("confirmation_required", "This change needs an explicit confirmation: "
                             + " ".join(cs.review["reasons"]) + " Check the preview, then confirm.")
        try:
            result = self.ws.apply(cs.operations(), cs.id, cs.instruction, cs.base_version, self.store.snapshot_dir(cs.id))
        except AgentError as e:
            if e.code == "stale_proposal":
                cs.status = "stale"
            else:
                cs.status, cs.error = "failed", e.message
            self.store.save(cs)
            self.store.add_message("agent", e.message, "error", cs.id)
            raise
        cs.status, cs.applied_at, cs.result, cs.has_snapshot, cs.applied_by = "applied", now(), result, True, applied_by
        self.store.save(cs)
        self._audit("agent.applied", changeset=cs.id, source=cs.source, operations=[op["op"] for op in cs.plan],
                    confirmed=bool(confirm), elevated=guardrails.needs_confirmation(cs.review),
                    cells=(cs.preview.get("totals") or {}).get("cells"), model=(cs.model or {}).get("model"))
        self._stale_others(cs.id)                        # their previews were computed on the data before this change
        self.store.prune_snapshots(UNDO_DEPTH)
        tail = (f" The recipe now has {result['steps']} step(s)." if "steps" in result
                else f" {result['open_items_after']} review item(s) are still open.")
        summary = result["summary"].rstrip()
        summary += "" if summary.endswith((".", "!", "?")) else "."
        msg = self.store.add_message("agent", f"Applied {cs.id}: {summary}"
                                     + (f" Phase B's rules then tidied {result['follow_up_fixes']} of the new values."
                                        if result.get("follow_up_fixes") else "") + tail, "result", cs.id)
        return {"changeset": cs.brief(), "message": msg.model_dump(), "result": result}

    # ── undo / discard ───────────────────────────────────────────────────────
    def undo(self) -> dict:
        cs = self.store.last_applied()
        if cs is None or not cs.has_snapshot:
            raise AgentError("nothing_to_undo", "There is no applied change that can be undone.")
        self.ws.restore(self.store.snapshot_dir(cs.id))
        cs.status, cs.undone_at, cs.has_snapshot = "undone", now(), False
        self.store.save(cs)
        self._audit("agent.undone", changeset=cs.id)
        self._stale_others(None)
        self.store.prune_snapshots(UNDO_DEPTH)
        msg = self.store.add_message("agent", f"Undid {cs.id} ({cs.instruction!r}). The data is back to how it was before it.",
                                     "result", cs.id)
        return {"changeset": cs.brief(), "message": msg.model_dump()}

    def discard(self, cs_id: str) -> dict:
        cs = self.store.get(cs_id)
        if cs is None:
            raise AgentError("no_such_changeset", f"There is no change {cs_id}.")
        if cs.status not in ("proposed", "stale"):
            raise AgentError("not_proposed", f"Change {cs_id} is {cs.status}; it cannot be discarded.")
        cs.status = "discarded"
        self.store.save(cs)
        msg = self.store.add_message("agent", f"Discarded {cs.id}. Nothing was changed.", "result", cs.id)
        return {"changeset": cs.brief(), "message": msg.model_dump()}
