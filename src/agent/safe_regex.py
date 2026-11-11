"""Regular expressions that come from an analyst or the model, made safe to run on client data.

Python's `re` backtracks without limit: a pattern such as `(a+)+$` on a 33-character cell runs for minutes, and an
apply job would hold a worker until the job timeout. Every pattern from outside therefore goes through here:

  * it must be short (`MAX_PATTERN_LEN`) and must compile;
  * each match runs with a time limit (`MATCH_TIMEOUT_S`, the `regex` library's `timeout`);
  * the first cell that hits the limit aborts the whole operation with a message the analyst can act on.

Phase T's engine matches with Polars (Rust regex, linear time) and only needs `check`.
"""
from __future__ import annotations

from functools import lru_cache

import regex

from .errors import AgentError

MAX_PATTERN_LEN = 500
MATCH_TIMEOUT_S = 0.25


class SafePattern:
    def __init__(self, pattern: str, ignore_case: bool = False):
        if len(pattern) > MAX_PATTERN_LEN:
            raise AgentError("bad_pattern", f"The pattern is longer than {MAX_PATTERN_LEN} characters; use a simpler one.")
        try:
            self._rx = regex.compile(pattern, regex.IGNORECASE if ignore_case else 0)
        except regex.error as e:
            raise AgentError("bad_pattern", f"The pattern '{pattern}' is not valid: {e}") from e
        self.pattern = pattern

    def _slow(self) -> AgentError:
        return AgentError("pattern_too_slow", f"The pattern '{self.pattern}' takes too long on this data (it backtracks "
                                              "heavily). Use a simpler pattern, for example without nested repeats.")

    def search(self, text: str):
        try:
            return self._rx.search(text, timeout=MATCH_TIMEOUT_S)
        except TimeoutError:
            raise self._slow() from None

    def sub(self, repl: str, text: str) -> str:
        try:
            return self._rx.sub(repl, text, timeout=MATCH_TIMEOUT_S)
        except TimeoutError:
            raise self._slow() from None
        except (regex.error, IndexError) as e:               # a replacement that names a group the pattern lacks
            raise AgentError("bad_pattern", f"The replacement '{repl}' does not fit the pattern: {e}") from e


@lru_cache(maxsize=256)
def compiled(pattern: str, ignore_case: bool = False) -> SafePattern:
    """Compile once per pattern (conditions are evaluated cell by cell)."""
    return SafePattern(pattern, ignore_case)


def check(pattern: str) -> None:
    """Length and syntax only, for engines that match in linear time themselves."""
    compiled(pattern)
