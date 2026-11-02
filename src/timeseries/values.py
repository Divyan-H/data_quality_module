"""Pure value helpers for Phase C: numeric validity, period dates, text rendering, finding ids. No table state here."""
from __future__ import annotations

import re

from src.common.values import render

SPECIAL_RE = re.compile(r"[^0-9+\-.]")                      # N-05: anything a plain number does not contain


def text_of(v) -> str:
    """The text a value check looks at: strings exactly as stored (spaces kept), anything else rendered."""
    return v if isinstance(v, str) else (render(v) or "")


