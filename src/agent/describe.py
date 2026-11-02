"""Plain-language text for operations and previews. Written by code from the typed plan, never by the model, so what
the analyst reads is exactly what will run."""
from __future__ import annotations

from .operations import (AddColumn, Condition, RemoveRows, RenameColumn, ReplaceText, ReplaceValues, ResolveFindings,
                         RowFilter, Scale, SetValue)

_OP_WORDS = {"eq": "is", "ne": "is not", "in": "is one of", "not_in": "is not one of", "contains": "contains",
             "starts_with": "starts with", "ends_with": "ends with", "empty": "is empty", "not_empty": "is not empty",
             "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "matches": "matches"}


def _q(v) -> str:
    return "empty" if v is None else f"'{v}'"


def _list(items, n: int = 6) -> str:
    items = [str(i) for i in items]
    head = ", ".join(items[:n])
    return head + (f" and {len(items) - n} more" if len(items) > n else "")


def condition(c: Condition) -> str:
    if c.op in ("empty", "not_empty"):
        return f"{c.column} {_OP_WORDS[c.op]}"
    v = _list(c.value) if isinstance(c.value, list) else _q(c.value) if isinstance(c.value, str) else c.value
    return f"{c.column} {_OP_WORDS[c.op]} {v}"


def where(w: RowFilter) -> str:
    parts = []
    if w.tables:
        parts.append(f"in {_list(w.tables)}")
    if w.rows:
        parts.append(f"on Excel row{'s' if len(w.rows) > 1 else ''} {_list(w.rows, 10)}")
    if w.conditions:
        parts.append("where " + " and ".join(condition(c) for c in w.conditions))
    if w.exclude_rows:
        parts.append(f"except row{'s' if len(w.exclude_rows) > 1 else ''} {_list(w.exclude_rows, 10)}")
    return (" " + " ".join(parts)) if parts else ""


def operation(op) -> str:
    if isinstance(op, ResolveFindings):
        what = {"accept": "Accept the proposed fix for", "reject": "Reject the automatic fix (restore the original) for",
                "keep": "Keep the current value and close", "set": f"Set the value to {_q(op.new_value)} for",
                "split": "Split into one row per value"}[op.decision]
        scope = "every open review item"
        if op.rules:
            scope += f" of {_list(op.rules)}"
        if op.column:
            scope += f" in {op.column}"
        if op.value is not None:
            scope += f" with the value {_q(op.value)}"
        return f"{what} {scope}{where(op.where)}."
    if isinstance(op, SetValue):
        return f"Set {op.column} to {_q(op.value)}{where(op.where) or ' on every row'}."
    if isinstance(op, ReplaceValues):
        pairs = _list([f"{_q(k)} → {_q(v)}" for k, v in op.mapping.items()], 8)
        cols = f" in {_list(op.columns)}" if op.columns else " in every column that has them"
        return f"Replace {pairs}{cols}{where(op.where)}."
    if isinstance(op, ReplaceText):
        kind = "the pattern" if op.regex else "the text"
        return f"In {op.column}, replace {kind} {_q(op.find)} with {_q(op.replace or None) if op.replace else 'nothing'}{where(op.where)}."
    if isinstance(op, Scale):
        how = f"Divide {_list(op.columns)} by {1 / op.factor:g}" if abs(op.factor) < 1 else f"Multiply {_list(op.columns)} by {op.factor:g}"
        return f"{how}{where(op.where)}."
    if isinstance(op, AddColumn):
        if op.value is not None:
            src = f"with the value {_q(op.value)} on every row"
        elif op.mapping is not None:
            src = f"from {op.from_column} using {len(op.mapping)} mapping(s)" + (f", others {_q(op.default)}" if op.default else "")
        else:
            src = f"= {op.formula}"
        return f"Add a column {_q(op.name)} {src}" + (f" in {_list(op.tables)}" if op.tables else "") + "."
    if isinstance(op, RenameColumn):
        return f"Rename the column {_q(op.column)} to {_q(op.new_name)}" + (f" in {_list(op.tables)}" if op.tables else "") + "."
    if isinstance(op, RemoveRows):
        return f"Remove the rows{where(op.where)}."
    return op.op


def effect_line(e: dict) -> str:
    """'312 cells in 2 tables, 4 review items closed' from an effect dict."""
    parts = []
    if e["cells"]:
        parts.append(f"{e['cells']:,} cell{'s' if e['cells'] != 1 else ''} changed")
    if e["items_resolved"]:
        parts.append(f"{e['items_resolved']:,} review item{'s' if e['items_resolved'] != 1 else ''} closed")
    if e["rows_removed"]:
        parts.append(f"{e['rows_removed']:,} row{'s' if e['rows_removed'] != 1 else ''} removed")
    if e["rows_added"]:
        parts.append(f"{e['rows_added']:,} row{'s' if e['rows_added'] != 1 else ''} added by splitting")
    if e["columns_added"]:
        parts.append(f"column{'s' if len(e['columns_added']) > 1 else ''} {_list(e['columns_added'])} added")
    if e["columns_renamed"]:
        parts.append("renamed " + _list(f"{a} → {b}" for a, b in e["columns_renamed"]))
    if not parts:
        return "Nothing would change."
    n = len(e["tables"])
    return ", ".join(parts) + (f" in {n} tables" if n > 1 else "") + "."
