"""The LLM reviewer (spec 10): asks, votes, reuses stored answers, keeps the value-free decision record.

Prompts exist only in memory. The record written to `llm/decisions.jsonl` holds hashes, options and
outcomes, never a cell value.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from dataclasses import dataclass

from ..decisions import Vote
from . import view as viewmod
from .client import OllamaClient, option_order, vote_settings
from .examples import Library, format_examples

CALL_BUDGET = 300                 # each vote is one call (spec 10.10)
TIME_CAP_S = 75 * 60
CONSECUTIVE_FAILURES = 5


@dataclass
class LLMConfig:
    mode: str = "assist"
    privacy: str = "full"
    votes: int = 3
    review: str = "all"
    model: str = ""
    digest: str | None = None
    prompt_version: str = viewmod.PROMPT_VERSION


def project_hash_key(secret: str, project_id: str) -> bytes:
    """Per-project key: a question hash cannot be used to test guesses, nor match across projects."""
    return hmac.new(secret.encode(), project_id.encode(), hashlib.sha256).digest()


class Reviewer:
    def __init__(self, cfg: LLMConfig, params: dict, client: OllamaClient | None, hash_key: bytes,
                 previous: list[dict] | None = None, previous_job: str | None = None,
                 library: Library | None = None, path: str = ""):
        self.cfg, self.params, self.client, self.key = cfg, params, client, hash_key
        self.library = library or Library()
        self.prior = {r["decision_id"]: r for r in (previous or [])}      # by id, for Analyst corrections
        self.previous = {r["question_hash"]: r for r in (previous or []) if r.get("outcome") != "no_answer"}
        self.previous_job, self.path = previous_job, path
        self.review = cfg.review
        self.decisions: dict[str, dict] = {}
        self.calls = self.batches = self.left_to_rules = self.parsed = 0
        self.llm_seconds = 0.0
        self.latencies: list[float] = []            # per call, for the benchmark's p95 (spec 10.9)
        self.failures_in_row = 0
        self.fallback_reason: str | None = None
        self._checked = False

    @property
    def active(self) -> bool:
        return self.cfg.mode == "assist"

    # ------------------------------------------------------------------ asking

    def _hash(self, view_text: str, kind: str, options: list[str]) -> str:
        body = json.dumps({"view": view_text, "options": options, "privacy": self.cfg.privacy,
                           "prompt_version": self.cfg.prompt_version, "kind": kind}, sort_keys=True)
        return "hmac-sha256:" + hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()

    def _server_ok(self) -> bool:
        """Check the server once, before the first call; a down server means the rules decide."""
        if self.fallback_reason:
            return False
        if not self._checked:
            self._checked = True
            if self.client is None or not self.client.check():
                self.fallback_reason = getattr(self.client, "unavailable_reason", None) or "llm_unavailable"
                return False
            self.cfg.digest = self.client.digest
        return True

    def ask(self, kind: str, scope: str, coords, options: list[str], rules_choice: str, sheet, ctx: dict,
            cut=None, focus_row=None) -> Vote | None:
        """Returns a Vote, or None when the question must go to the rules (server down, budget spent)."""
        text, grid = viewmod.build_view(sheet, coords, ctx, self.cfg.privacy, self.params, cut, focus_row)
        qhash = self._hash(text, kind, options)
        fix = self.library.find_correction(qhash)
        if fix is not None and fix in options:      # an Analyst correction needs no model call
            return Vote([fix], options, qhash, corrected=True, grid=grid)
        old = self.previous.get(qhash)
        if old and old.get("votes_setting") == self.cfg.votes and old.get("model_digest") == self.cfg.digest:
            return Vote(old["votes"], options, qhash, reused_from=old.get("reused_from") or self.previous_job,
                        examples=old.get("examples", []), grid=grid)
        if not self._server_ok() or self._out_of_budget():
            self.left_to_rules += 1
            return None
        examples = self.library.nearest(kind, grid)
        example_text = format_examples(examples)
        answers = []
        self.batches += 1
        for i in range(self.cfg.votes):
            answers.append(self._one_vote(kind, options, text, example_text, i))
            if self.fallback_reason:
                break
        answers += [None] * (self.cfg.votes - len(answers))
        return Vote(answers, options, qhash, examples=[e["id"] for e in examples], grid=grid)

    def _out_of_budget(self) -> bool:
        if self.calls + self.cfg.votes > CALL_BUDGET:
            self.fallback_reason = "llm_budget_reached"
        elif self.llm_seconds >= TIME_CAP_S:
            self.fallback_reason = "llm_time_cap_reached"
        return self.fallback_reason is not None

    def _one_vote(self, kind: str, options: list[str], view_text: str, example_text: str, index: int):
        order = option_order(options, index)
        schema = {"type": "object", "required": ["d1"], "properties": {"d1": {
            "type": "object", "required": ["why", "choice"],
            "properties": {"why": {"type": "string", "maxLength": 160}, "choice": {"type": "string", "enum": order}}}}}
        prompt = viewmod.build_prompt(view_text, [{"id": "d1", "kind": kind, "options": order}], example_text)
        seed, temp = vote_settings(self.cfg.votes, index)
        start = time.monotonic()
        answer = self.client.chat(prompt, schema, seed, temp)
        self.latencies.append(time.monotonic() - start)
        self.llm_seconds += self.latencies[-1]
        self.calls += 1
        try:
            d1 = answer["d1"]
            # `why` only proves an answer was given; it can quote values, so it is never kept.
            choice = d1["choice"] if isinstance(d1.get("why"), str) and d1["choice"] in options else None
        except (TypeError, KeyError, AttributeError):
            choice = None
        self.parsed += choice is not None
        self.failures_in_row = 0 if choice is not None else self.failures_in_row + 1
        if self.failures_in_row >= CONSECUTIVE_FAILURES:
            self.fallback_reason = "llm_failed_5_calls"
        return choice

    # ------------------------------------------------------------------ record

    def record(self, vote: Vote, kind: str, table_ref: str | None, rules_choice: str, used: str,
               outcome: str, decided_by: str, rules_margin: float | None = None) -> None:
        did = f"{kind}@{table_ref}"
        self.decisions[did] = {
            "decision_id": did, "question_hash": vote.question_hash, "kind": kind, "table_ref": table_ref,
            "options": vote.options, "rules_choice": rules_choice, "rules_margin": rules_margin,
            "votes": vote.votes, "majority": vote.majority, "unanimous": vote.unanimous,
            "outcome": outcome, "used": used, "decided_by": decided_by,
            "model": self.cfg.model, "model_digest": self.cfg.digest,
            "ollama_version": getattr(self.client, "version", None),
            "prompt_version": self.cfg.prompt_version, "privacy": self.cfg.privacy,
            "votes_setting": self.cfg.votes, "examples": vote.examples, "reused_from": vote.reused_from,
            "grid": vote.grid,
        }

    def carry(self, records: list[dict]) -> None:
        """Decisions of a reused file are copied so answers survive any number of re-runs (spec 10.8)."""
        for r in records:
            self.decisions.setdefault(r["decision_id"], r)

    def p95_seconds(self) -> float:
        ordered = sorted(self.latencies)
        return ordered[max(math.ceil(0.95 * len(ordered)) - 1, 0)] if ordered else 0.0

    def summary(self) -> dict:
        outcomes = [d["outcome"] for d in self.decisions.values()]
        count = outcomes.count
        return {"mode": self.cfg.mode, "privacy": self.cfg.privacy, "review": self.cfg.review,
                "votes": self.cfg.votes, "model": self.cfg.model, "questions": len(self.decisions),
                "batches": self.batches, "calls": self.calls, "parsed": self.parsed,
                "latency_p95_s": round(self.p95_seconds(), 3),
                "reused": sum(1 for d in self.decisions.values() if d["reused_from"]),
                "agreed": count("agreed"), "changed": count("changed"), "overruled": count("overruled"),
                "corrected": count("corrected"), "no_majority": count("no_majority"),
                "no_answer": count("no_answer"), "fallback": self.fallback_reason is not None}
