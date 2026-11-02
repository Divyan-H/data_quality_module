"""Policy for LLM answers (spec 10.5, 10.6): counting votes and the checks that accept or overrule them.

The rules always own the coordinates: an answer is only ever one of the options the rules built, and
it changes anything only if it passes the checks below.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Vote:
    votes: list                       # one entry per requested vote; None = failed vote
    options: list
    question_hash: str = ""
    corrected: bool = False           # the answer is a library correction, used without calling the model
    reused_from: str | None = None
    examples: list = field(default_factory=list)
    grid: list = field(default_factory=list)          # class lines of the view (value-free)

    @property
    def majority(self) -> str | None:
        """More than half of the *requested* votes; failed votes count against every choice."""
        answered = Counter(v for v in self.votes if v is not None)
        if not answered:
            return None
        top, n = answered.most_common(1)[0]
        return top if n * 2 > len(self.votes) else None

    @property
    def unanimous(self) -> bool:
        """Every requested vote names it and at least 3 were requested; a correction counts as unanimous."""
        if self.corrected:
            return True
        maj = self.majority
        return maj is not None and len(self.votes) >= 3 and all(v == maj for v in self.votes)


def _outcome(vote: Vote, rules_choice: str, accepted: bool, overruled: bool) -> tuple[str, str]:
    """(outcome, decided_by) for the decision record (spec 10.8)."""
    maj = vote.majority
    if maj is None:
        return ("no_majority" if any(v is not None for v in vote.votes) else "no_answer"), "rules"
    if maj == rules_choice:
        return "agreed", "rules+llm"
    if overruled:
        return "overruled", "rules"
    return ("corrected", "correction") if vote.corrected else ("changed", "llm")


def resolve_choice(rv, vote: Vote, kind: str, rules_choice: str, shape_ok, table_ref: str | None = None):
    """region_class, soft_cut, footer: a change against the rules needs a unanimous vote the shape allows."""
    maj = vote.majority
    against = maj is not None and maj != rules_choice
    accepted = against and vote.unanimous and shape_ok(maj)
    used = maj if accepted else rules_choice
    outcome, decided_by = _outcome(vote, rules_choice, accepted, against and not accepted)
    rv.record(vote, kind, table_ref, rules_choice, used, outcome, decided_by)
    return used, outcome


def resolve_header(rv, vote: Vote, rules_choice: str, scores: dict, valid, params: dict,
                   table_ref: str | None = None):
    """header_row (spec 10.6). `scores` maps option id -> scorer value; `valid(choice)` is header validity.

    Returns (used, outcome, finding) where finding is None, "uncertain" (warn) or "corrected" (info)."""
    lo = params["header_min_score"]
    if rules_choice == "none":              # nothing reached the minimum: gaps are measured from it
        margin = lo - max(scores.values(), default=lo)

        def gap(c):
            return lo - scores.get(c, lo)
    else:
        rules_score = scores.get(rules_choice, lo)
        margin = rules_score - max([s for k, s in scores.items() if k != rules_choice] + [lo])

        def gap(c):
            return rules_score - (lo if c == "none" else scores.get(c, lo))
    maj = vote.majority
    near_tie = margin < params["header_tie_margin"]
    against = maj is not None and maj != rules_choice
    # Unanimous, or a plain majority when the rules themselves were in a near-tie; both need validity
    # and a gap within llm_override_margin.
    accepted = against and valid(maj) and gap(maj) <= params["llm_override_margin"] \
        and (vote.unanimous or near_tie)
    used = maj if accepted else rules_choice
    outcome, decided_by = _outcome(vote, rules_choice, accepted, against and not accepted)
    rv.record(vote, "header_row", table_ref, rules_choice, used, outcome, decided_by,
              rules_margin=round(margin, 4))
    if outcome == "agreed":
        finding = None
    elif outcome == "corrected":
        finding = "corrected"
    elif outcome in ("changed", "overruled"):
        finding = "uncertain"
    else:                                   # no majority / no answer: only a near-tie deserves a warning
        finding = "uncertain" if near_tie else None
    return used, outcome, finding
