"""Guardrails on a previewed change: which proposals need a second, explicit confirmation before they are applied.

Every change already needs a click on Apply. Some deserve more than a reflex click: a plan that removes rows, columns
or tables, one that collapses rows (aggregate), or one that rewrites a large number of cells. For those the preview
carries `review = {"level": "elevated", "reasons": [...]}` and Apply is refused unless the request says
`confirm: true` (the screen shows the reasons and asks the analyst to tick that they checked the preview).

The decision is made by code from the typed plan and the dry-run numbers; the model has no say in it.
"""
from __future__ import annotations

from . import registry

CONFIRM_CELLS = 1000             # cells changed by one change-set at or above which confirmation is required
CONFIRM_ROW_LOSS = 0.05          # a table losing more than this share of its rows requires confirmation
NORMAL, ELEVATED = "normal", "elevated"

_HIGH_RISK_TEXT = {
    "remove_rows": "It removes rows.",
    "drop_columns": "It leaves columns out of the final data.",
    "drop_table": "It leaves a whole table out of the final data.",
    "aggregate": "It replaces rows by group totals; the original rows are no longer in the result.",
}


def _row_changes(preview: dict) -> list[tuple[str, int, int]]:
    """(table, rows before, rows after) for every table a transformation preview resized."""
    out = []
    for step in preview.get("operations", []):
        for change in (step.get("effect") or {}).get("tables", []) or []:
            if isinstance(change, dict) and change.get("rows_before") is not None and change.get("rows_after") is not None:
                out.append((change["table"], change["rows_before"], change["rows_after"]))
    return out


def assess(ops: list, preview: dict, confirm_cells: int = CONFIRM_CELLS) -> dict:
    """{"level": "normal" | "elevated", "reasons": [...]} for a plan and its dry-run preview."""
    reasons: list[str] = []
    for op in ops:
        tool = registry.TOOLS.get(op.op)
        if tool is not None and tool.risk == "high":
            text = _HIGH_RISK_TEXT.get(op.op, f"'{op.op.replace('_', ' ')}' cannot be reversed by a later step.")
            if text not in reasons:
                reasons.append(text)
    totals = preview.get("totals") or {}
    cells = totals.get("cells") or sum((s.get("effect") or {}).get("cells", 0) or 0 for s in preview.get("operations", []))
    if cells >= confirm_cells:
        reasons.append(f"It changes {cells:,} cells.")
    removed = totals.get("rows_removed") or 0
    if removed and not any("removes rows" in r for r in reasons):
        reasons.append(f"It removes {removed:,} row(s).")
    for table, before, after in _row_changes(preview):
        if before and after < before * (1 - CONFIRM_ROW_LOSS):
            text = f"Table {table} goes from {before:,} to {after:,} rows."
            if text not in reasons:
                reasons.append(text)
    return {"level": ELEVATED if reasons else NORMAL, "reasons": reasons}


def needs_confirmation(review: dict | None) -> bool:
    return bool(review) and review.get("level") == ELEVATED
