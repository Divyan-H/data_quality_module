"""Plain-language text for operations and previews. Written by code from the typed plan, never by the model, so what
the analyst reads is exactly what will run."""
from __future__ import annotations

from .operations import (AddColumn, Aggregate, AllocateByShare, AppendRows, BucketOthers, Classify, Condition,
                         ConvertCurrency, DropColumns, DropTable, LookupJoin, MakeDate, MakePeriod, NetNextPeriod, Pivot,
                         RemoveRows, RenameColumn, ReplaceText, ReplaceValues, ResolveFindings, RowFilter, Scale, SetValue,
                         SortRows, SplitColumn, SplitRows, Unpivot)

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
    t = f" in {op.table}" if getattr(op, "table", None) else ""
    if isinstance(op, SplitColumn):
        how = f"on {_q(op.separator)}" if op.separator is not None else f"by the pattern {_q(op.pattern)}"
        return f"Split {op.column} {how} into {_list(op.into)}" + (f" in {_list(op.tables)}" if op.tables else "") + "."
    if isinstance(op, Classify):
        rules = "; ".join(f"{_q(r.value)} if " + " and ".join(condition(c) for c in r.when) for r in op.rules)
        rest = "keep the current value" if op.keep_existing else _q(op.default)
        return f"Set {op.column}: {rules}; otherwise {rest}."
    if isinstance(op, Unpivot):
        cols = _list(op.columns) if op.columns else f"the columns matching {_q(op.pattern)}"
        return f"Turn {cols}{t} into rows: column names into {_q(op.names_to)}, values into {_q(op.values_to)}."
    if isinstance(op, Pivot):
        return f"Turn the values of {op.columns_from}{t} into columns holding {op.values_from} ({op.agg}), one row per {_list(op.index)}."
    if isinstance(op, SplitRows):
        share = f", sharing {_list(op.allocate)} ({op.method.replace('_', ' ')})" if op.allocate else ""
        return f"Split rows{t} with several values in {op.column} into one row per value{share}."
    if isinstance(op, AllocateByShare):
        how = f"by {op.share}" if op.share else "equally"
        return (f"Share {_list(op.columns)}{t} across {op.category} from {op.share_table} {how}, matching "
                + ", ".join(f"{a} = {b}" for a, b in op.on.items()) + ".")
    if isinstance(op, Aggregate):
        parts = ([f"sum {_list(op.sum)}"] if op.sum else []) + ([f"average {_list(op.mean)}"] if op.mean else []) \
            + ([f"count rows as {_q(op.count_as)}"] if op.count_as else [])
        return f"Group{t} by {_list(op.by)} and " + ", ".join(parts) + "."
    if isinstance(op, BucketOthers):
        which = f"not in {_list(op.keep)}" if op.keep is not None else (
            f"not in {op.keep_from.get('table')}'s {op.keep_from.get('column', op.column)}" if op.keep_from else f"outside the top {op.top_n}")
        tail = f", then add up {_list(op.sum)} per {_list(op.sum_by)}" if op.sum_by is not None else ""
        return f"Label {op.column} values{t} {which} as {_q(op.label)}{tail}."
    if isinstance(op, NetNextPeriod):
        by = f" per {_list(op.group_by)}" if op.group_by else ""
        return f"Monthly {op.value}{t}{by}: net each month's allocation with the next month's negative return into {_q(op.output)}."
    if isinstance(op, AppendRows):
        cond = " where " + " and ".join(condition(c) for c in op.where) if op.where else ""
        return f"Add the rows of {op.source}{cond} under {op.table or 'the table'}" + (" (then drop it)" if op.drop_source else "") + "."
    if isinstance(op, LookupJoin):
        return (f"Look up {_list(op.columns)} from {op.lookup}{t} where " + ", ".join(f"{a} = {b}" for a, b in op.on.items())
                + f" (duplicates: {op.duplicates})."
                )
    if isinstance(op, ConvertCurrency):
        how = f"x {op.rate:g}" if op.rate is not None else f"by {len(op.rates or [])} date-range rate(s) on {op.date_column}"
        return f"Convert {_list(op.columns)}{t} {how}" + (f" ({op.note})" if op.note else "") + "."
    if isinstance(op, SortRows):
        keys = [f"{c}{' (descending)' if k < len(op.descending) and op.descending[k] else ''}" for k, c in enumerate(op.by)]
        return f"Sort{t} by {_list(keys)}."
    if isinstance(op, MakeDate):
        return f"Add {_q(op.name)}{t}: the date from {op.month} and the day number in {op.day}."
    if isinstance(op, MakePeriod):
        return f"Add {_q(op.name)}{t}: the {op.unit} of {op.column} (its first day)."
    if isinstance(op, DropColumns):
        return f"Drop the column(s) {_list(op.columns)}{t}."
    if isinstance(op, DropTable):
        return f"Leave the table {op.table} out of the final data."
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
