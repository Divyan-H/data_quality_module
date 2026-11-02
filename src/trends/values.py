"""Phase D's label matching. Everything else (rendering, numbers, Period dates, names, ids) is in src/common/values.py."""
from __future__ import annotations

import re


def norm(v) -> str:
    """Sheet / column / rule label for matching: lower case, punctuation and spaces collapsed."""
    return re.sub(r"[^a-z0-9]+", " ", str(v).replace(" ", " ").lower()).strip()
