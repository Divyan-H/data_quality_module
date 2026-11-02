"""One exception type for the agent layer; the orchestrator maps `code` to an HTTP status."""
from __future__ import annotations


class AgentError(Exception):
    """A request the agent cannot carry out. `code` is stable (the UI and tests match on it); `message` is for people."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class StaleProposal(AgentError):
    def __init__(self, message: str = "The data changed after this proposal was made. Ask again to get a fresh preview."):
        super().__init__("stale_proposal", message)
