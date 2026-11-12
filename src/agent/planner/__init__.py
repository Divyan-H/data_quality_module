"""Instruction -> Plan. The command parser answers first (deterministic, no model); the local LLM handles the rest.

    plan = Planner(llm).plan(text, PlanningContext(tables))

Either planner may return a question instead of a plan; neither ever returns anything but typed operations, a
question, or a reply that changes nothing.
"""
from __future__ import annotations

from .. import registry
from ..operations import Plan
from .context import PlanningContext
from .llm import LlmPlanner
from .parser import HELP, CommandParser

NOT_UNDERSTOOD = ("I did not understand that as a change I can make. " + HELP.split("\n", 1)[1])


class Planner:
    def __init__(self, llm: LlmPlanner | None = None, privacy: str = "full"):
        self.llm, self.privacy = llm, privacy

    def plan(self, text: str, ctx: PlanningContext) -> Plan:
        """The plan for one instruction. Whoever made it (parser or model), a plan may only hold tools of this phase."""
        return self._in_phase(self._plan(text, ctx), ctx)

    @staticmethod
    def _in_phase(plan: Plan, ctx: PlanningContext) -> Plan:
        allowed = set(registry.names_for(ctx.phase))
        outside = [op.op for op in plan.ops if op.op not in allowed]
        if not outside:
            return plan
        name = outside[0].replace("_", " ")
        if ctx.phase == "T":
            return Plan(answer=f"'{name}' answers review items, which belong to the checking phases (B), not to "
                               "Transformations. Open that phase's review to use it.", source=plan.source)
        return Plan(answer=f"'{name}' changes the shape of the table, so it belongs in Phase T (Transformations), after "
                           "the checks. Accept the checking phases, then open Phase T and ask there. Value fixes, new "
                           "columns and splitting a column into columns work here.", source=plan.source)

    def _plan(self, text: str, ctx: PlanningContext) -> Plan:
        parser = CommandParser(ctx)
        plan = parser.parse(text)
        if plan is not None:
            return plan
        if self.llm is not None:
            plan = self.llm.plan(text, ctx, self.privacy)
            if plan is not None:
                if plan.answer in ("help", "summary"):          # the model classified it; code writes the reply
                    return parser.parse("help" if plan.answer == "help" else "summary") or Plan(answer=HELP)
                return plan
            if not self.llm.available():
                return Plan(answer="I did not understand that as one of the commands I know, and the local model "
                                   f"is not available ({self.llm.unavailable_reason}) to help with free wording. "
                                   + HELP.split("\n", 1)[1], source="none")
        return Plan(answer=NOT_UNDERSTOOD, source="none")


__all__ = ["Planner", "PlanningContext", "LlmPlanner", "CommandParser", "HELP"]
