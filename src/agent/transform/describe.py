"""Plain text for Phase T steps and their effects (written by code from the typed step, never by the model)."""
from __future__ import annotations

from .. import describe


def step_line(op) -> str:
    return describe.operation(op)


def transform_summary(effect: dict) -> str:
    """'Coupons: 3 -> 6 rows, +City_Share; AOV dropped' from an engine effect."""
    parts = []
    for t in effect["tables"]:
        if t.get("dropped"):
            parts.append(f"{t['table']} left out")
            continue
        rows = (f"{t['rows_after']:,} rows (new)" if t.get("new") else
                f"{t['rows_before']:,} → {t['rows_after']:,} rows" if t["rows_before"] != t["rows_after"] else f"{t['rows_after']:,} rows")
        cols = []
        if t["columns_added"]:
            cols.append("+" + ", +".join(t["columns_added"][:6]) + (" …" if len(t["columns_added"]) > 6 else ""))
        if t["columns_removed"]:
            cols.append("−" + ", −".join(t["columns_removed"][:6]) + (" …" if len(t["columns_removed"]) > 6 else ""))
        parts.append(f"{t['table']}: {rows}" + (f", {'; '.join(cols)}" if cols else ""))
    if effect.get("cells"):
        parts.append(f"{effect['cells']:,} value(s) written")
    return "; ".join(parts) or "Nothing changed."
