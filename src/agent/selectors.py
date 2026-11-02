"""Which tables, columns and rows a change touches.

Names are never exact in client files ('city' vs 'City_Name', 'Spend (INR)'), so column names go through the same
matching Phase B uses for expected columns (case, spaces, punctuation, whole words, one or two typos). A name that
matches nothing, or more than one column, is an error the planner turns into a question; it is never guessed.
"""
from __future__ import annotations

import re
from fnmatch import fnmatch

from src.hygiene.cities import normalize
from src.hygiene.rules import find_expected_columns
from src.hygiene.table import Table
from src.hygiene.values import parse_number

from .errors import AgentError
from .operations import Condition, RowFilter


def text_key(v) -> str:
    """How values are compared in conditions and mappings: trimmed, inner spaces collapsed, case-folded."""
    return " ".join(str(v).split()).casefold()


def table_matches(t: Table, patterns: list[str] | None) -> bool:
    """A table is chosen by its ref, file name or sheet name: part of the name, or a glob ('coupon_*')."""
    if not patterns:
        return True
    names = [x for x in (t.ref, t.source_file, t.sheet) if x]
    for p in patterns:
        pk = text_key(p)
        for n in names:
            nk = text_key(n)
            if pk == nk or pk in nk or fnmatch(nk, pk) or fnmatch(nk.rsplit("/", 1)[-1], pk):
                return True
    return False


def find_column(name: str, columns: list[str]) -> str | None:
    """The column `name` means, or None. Exact, then ignoring case/spaces/punctuation, then Phase B's header matching."""
    if name in columns:
        return name
    norm = normalize(name)
    same = [c for c in columns if normalize(c) == norm]
    if len(same) == 1:
        return same[0]
    hit = find_expected_columns([name], columns, {}).get(name)
    return hit


def column_in(t: Table, name: str) -> str | None:
    return find_column(name, list(t.cols))


def require_column(t: Table, name: str) -> str:
    col = column_in(t, name)
    if col is None:
        raise AgentError("unknown_column", f"There is no column '{name}' in {label(t)}.")
    return col


def label(t: Table) -> str:
    parts = [p for p in (t.source_file, t.sheet) if p]
    return " / ".join(parts) if parts else t.ref


def _number(t: Table, col: str, v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    n = parse_number(str(v), t.cfg["currency_symbols"], t.eu(col))
    return float(n) if n is not None else None


def _test(t: Table, col: str, cond: Condition, v) -> bool:
    op = cond.op
    if op == "empty":
        return v is None or str(v).strip() == ""
    if op == "not_empty":
        return not (v is None or str(v).strip() == "")
    if op in ("gt", "gte", "lt", "lte"):
        a, b = _number(t, col, v), _number(t, col, cond.value)
        if a is None or b is None:
            return False
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    if v is None:
        return op in ("ne", "not_in")
    key = text_key(v)
    if op in ("eq", "ne"):
        target = cond.value
        equal = key == text_key(target)
        if not equal and isinstance(target, (int, float)):        # 1000 matches "1,000" and "1000.0"
            n = _number(t, col, v)
            equal = n is not None and n == float(target)
        return equal if op == "eq" else not equal
    if op in ("in", "not_in"):
        hit = key in {text_key(x) for x in cond.value}
        return hit if op == "in" else not hit
    if op == "contains":
        return text_key(cond.value) in key
    if op == "starts_with":
        return key.startswith(text_key(cond.value))
    if op == "ends_with":
        return key.endswith(text_key(cond.value))
    if op == "matches":
        try:
            return re.search(str(cond.value), str(v), re.I) is not None
        except re.error as e:
            raise AgentError("bad_pattern", f"The pattern '{cond.value}' is not valid: {e}") from e
    raise AgentError("bad_condition", f"Unknown condition '{op}'.")


def rows(t: Table, where: RowFilter) -> list[int]:
    """Row indexes (positions in the table) that the filter selects. Empty when the table itself is not chosen."""
    if not table_matches(t, where.tables):
        return []
    n = len(t.data["_row_id"])
    src = t.data.get("_source_row") or [None] * n
    keep = range(n)
    if where.rows:
        wanted = {int(r) for r in where.rows}
        keep = [i for i in keep if src[i] in wanted]
    if where.exclude_rows:
        skip = {int(r) for r in where.exclude_rows}
        keep = [i for i in keep if src[i] not in skip]
    for cond in where.conditions:
        col = column_in(t, cond.column)
        if col is None:
            return []                                    # the table does not have the column: nothing in it matches
        vals = t.data[col]
        keep = [i for i in keep if _test(t, col, cond, vals[i])]
    return list(keep)


def tables_with_columns(tables: list[Table], where_tables: list[str] | None, columns: list[str]) -> list[Table]:
    """Chosen tables that have every one of `columns`."""
    return [t for t in tables if table_matches(t, where_tables) and all(column_in(t, c) for c in columns)]
