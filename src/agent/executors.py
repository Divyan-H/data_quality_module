"""One executor per operation. Each reads the loaded tables and writes only through the EditSession.

Executors never guess. A column that no chosen table has is an error; a value that is not found is reported on the
preview as unmatched; anything that would surprise the analyst (a whole column changed, values left unmapped) becomes
a note on the preview.
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Callable

from src.hygiene.cities import normalize
from src.hygiene.config import ALIASES
from src.hygiene.rules import _num_text
from src.hygiene.table import Table
from src.hygiene.values import parse_number

from .errors import AgentError
from .operations import (SHAPE_OPS, AddColumn, Classify, RemoveRows, RenameColumn, ReplaceText, ReplaceValues,
                         ResolveFindings, RowFilter, Scale, SetValue, SplitColumn)
from .selectors import column_in, label, rows, table_matches, text_key
from .session import EditSession, value_counts

DECISION_LABEL = {"accept": "Accept", "reject": "Reject", "keep": "Keep", "set": "New Value", "split": "Split"}


def _chosen(tables: list[Table], patterns: list[str] | None) -> list[Table]:
    chosen = [t for t in tables if table_matches(t, patterns)]
    if not chosen:
        raise AgentError("no_table", f"No table matches {', '.join(patterns or [])}.")
    return chosen


def _with_column(tables: list[Table], patterns: list[str] | None, name: str) -> list[tuple[Table, str]]:
    """(table, real column name) for every chosen table that has the column; an error if none has it."""
    out = [(t, c) for t in _chosen(tables, patterns) if (c := column_in(t, name))]
    if not out:
        raise AgentError("unknown_column", f"No table has a column '{name}'.")
    return out


def _rule_codes(codes: list[str] | None) -> set[str] | None:
    """Rule codes plus their aliases (the workbook lists N-01 under D-04, and so on)."""
    if not codes:
        return None
    out = set(codes)
    for code, also in ALIASES.items():
        if code in out or out & set(also):
            out.add(code)
            out.update(also)
    return out


# ── resolve_findings ────────────────────────────────────────────────────────
def resolve_findings(s: EditSession, op: ResolveFindings) -> None:
    codes = _rule_codes(op.rules)
    label_ = DECISION_LABEL[op.decision]
    why = f"Needs_Review answered by the agent: {label_}."
    filtered = not op.where.is_everything() and bool(op.where.rows or op.where.conditions or op.where.exclude_rows)
    for t in _chosen(s.tables, op.where.tables):
        column = column_in(t, op.column) if op.column else None
        if op.column and column is None:
            continue
        index = {rid: i for i, rid in enumerate(t.data["_row_id"])}
        selected = set(rows(t, op.where)) if filtered else None
        for item in [r for r in t.review if r["status"] == "open"]:
            if codes is not None and item["rule"] not in codes:
                continue
            if column is not None and item["column"] != column:
                continue
            if op.value is not None and text_key(item.get("current_value") or "") != text_key(op.value):
                continue
            i = index.get(item["row_id"]) if item["row_id"] is not None else None
            if selected is not None and (i is None or i not in selected):
                continue
            col = item["column"]
            if op.decision == "split":
                if item["rule"] != "D-29" or not item.get("parts") or i is None or col not in t.data:
                    continue
                s.split(t, i, col, list(item["parts"]))
            elif item.get("scope") == "value" and col in t.data:          # one answer for every cell with this spelling
                answer = op.new_value if op.decision == "set" else item.get("proposed_value") if op.decision == "accept" else None
                if answer is not None:
                    for j, v in enumerate(t.data[col]):
                        if v == item["current_value"]:
                            s.edit(t, col, j, answer, why)
            elif i is not None and col in t.data:
                if op.decision == "set":
                    s.edit(t, col, i, op.new_value, why)
                elif op.decision == "accept" and item.get("proposed_value") is not None:
                    s.edit(t, col, i, item["proposed_value"], why)
                elif (op.decision == "reject" and item.get("original_value") is not None
                      and t.data[col][i] == item["current_value"]):
                    s.edit(t, col, i, item["original_value"], why)
            elif i is None and op.decision in ("set", "split"):
                continue                                                   # a column-level item has no cell to write
            s.resolve(t, item, label_, why)
    if not s.effect.items_resolved:
        s.note("No open review item matched.")


# ── set_value ───────────────────────────────────────────────────────────────
def set_value(s: EditSession, op: SetValue) -> None:
    why = f"Set by the agent: {op.column} = {op.value!r}."
    if op.where.is_everything():
        s.note(f"Every row of {op.column} is set.")
    for t, col in _with_column(s.tables, op.where.tables, op.column):
        for i in rows(t, op.where):
            s.edit(t, col, i, op.value, why)
    if not s.effect.cells:
        s.note("No cell needed changing: no row matched, or the cells already hold this value.")


# ── replace_values ──────────────────────────────────────────────────────────
def replace_values(s: EditSession, op: ReplaceValues) -> None:
    key = text_key if op.ignore_case else (lambda v: v)
    mapping = {key(k): v for k, v in op.mapping.items()}
    found: set[str] = set()
    bucket = len(op.mapping) > 1 and len({v for v in op.mapping.values()}) < len(op.mapping)
    for t in _chosen(s.tables, op.where.tables):
        if op.columns:
            cols = [c for c in (column_in(t, n) for n in op.columns) if c]
        else:                                                              # every column holding one of the values
            cols = [c for c in t.cols if any(v is not None and key(v) in mapping for v in t.data[c])]
        if not cols:
            continue
        idx = rows(t, op.where)
        for col in cols:
            vals = t.data[col]
            for i in idx:
                v = vals[i]
                if v is None:
                    continue
                k = key(v)
                if k in mapping:
                    found.add(k)
                    s.edit(t, col, i, mapping[k], f"Value replaced by the agent: {v!r} -> {mapping[k]!r}.")
            if bucket:                                                     # D-26: show what the mapping left alone
                left = [v for v in value_counts(vals) if key(v) not in mapping and v not in op.mapping.values()]
                if left:
                    s.note(f"Values left as they are in {col}: {', '.join(left[:12])}"
                           + (f" and {len(left) - 12} more" if len(left) > 12 else "") + ".")
        if not op.columns:
            s.note(f"Columns changed in {label(t)}: {', '.join(cols)}.")
    if op.columns and not any(column_in(t, n) for t in s.tables for n in op.columns):
        raise AgentError("unknown_column", f"No table has a column {', '.join(repr(c) for c in op.columns)}.")
    s.unmatched([k for k in op.mapping if key(k) not in found])


# ── replace_text ────────────────────────────────────────────────────────────
def replace_text(s: EditSession, op: ReplaceText) -> None:
    flags = re.I if op.ignore_case else 0
    try:
        pattern = re.compile(op.find if op.regex else re.escape(op.find), flags)
    except re.error as e:
        raise AgentError("bad_pattern", f"The pattern '{op.find}' is not valid: {e}") from e
    for t, col in _with_column(s.tables, op.where.tables, op.column):
        for i in rows(t, op.where):
            v = t.data[col][i]
            if v is None:
                continue
            new = pattern.sub(op.replace if op.regex else op.replace.replace("\\", "\\\\"), v)
            s.edit(t, col, i, new.strip() or None, f"Text replaced by the agent: {op.find!r} -> {op.replace!r}.")


# ── scale ───────────────────────────────────────────────────────────────────
def scale(s: EditSession, op: Scale) -> None:
    factor = Decimal(repr(op.factor))
    skipped: list[str] = []
    for name in op.columns:
        for t, col in _with_column(s.tables, op.where.tables, name):
            if t.types.get(col) not in ("numeric", None):
                s.note(f"{col} in {label(t)} is a {t.types.get(col)} column; only its number cells are scaled.")
            sym, eu = t.cfg["currency_symbols"], t.eu(col)
            for i in rows(t, op.where):
                v = t.data[col][i]
                if v is None:
                    continue
                n = parse_number(v, sym, eu)
                if n is None:
                    skipped.append(v)
                    continue
                s.edit(t, col, i, _num_text(Decimal(n) * factor), f"Scaled by the agent: x {op.factor:g}.")
    if skipped:
        s.note(f"{len(skipped)} cell(s) are not numbers and were left as they are.")
        s.unmatched(list(dict.fromkeys(skipped)))


# ── add_column ──────────────────────────────────────────────────────────────
_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b}


def compile_formula(formula: str, columns: list[str]) -> tuple[Callable[[dict], Decimal | None], list[str]]:
    """A safe arithmetic formula over columns: numbers, + - * /, brackets. Column names may contain spaces; they are
    replaced by placeholders before parsing, longest name first, so 'Spend' inside 'Spend INR' is never split."""
    text, used = formula, []
    for n, col in enumerate(sorted(columns, key=len, reverse=True)):
        for form in (f"[{col}]", col):
            pat = re.compile(rf"(?<![\w]){re.escape(form)}(?![\w])", re.I)
            if pat.search(text):
                text = pat.sub(f" __c{n}__ ", text)
                used.append(col)
                break
    names = {f"__c{n}__": col for n, col in enumerate(sorted(columns, key=len, reverse=True))}
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        raise AgentError("bad_formula", f"The formula '{formula}' could not be read. Use column names, numbers, "
                                        "+ - * / and brackets.") from e
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in names:
            raise AgentError("bad_formula", f"'{node.id}' in the formula is not a column.")
        if not isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.USub, ast.UAdd, ast.Constant, ast.Name,
                                 ast.Load, *_OPS)):
            raise AgentError("bad_formula", f"The formula '{formula}' may only use + - * / and brackets.")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise AgentError("bad_formula", f"The formula '{formula}' may only contain numbers and columns.")

    def ev(node, row: dict) -> Decimal | None:
        if isinstance(node, ast.Expression):
            return ev(node.body, row)
        if isinstance(node, ast.Constant):
            return Decimal(repr(node.value))
        if isinstance(node, ast.Name):
            return row.get(names[node.id])
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand, row)
            return None if v is None else (-v if isinstance(node.op, ast.USub) else v)
        a, b = ev(node.left, row), ev(node.right, row)
        if a is None or b is None:
            return None
        try:
            return _OPS[type(node.op)](a, b)
        except (DivisionByZero, InvalidOperation, ZeroDivisionError):
            return None

    if not used:
        raise AgentError("bad_formula", f"The formula '{formula}' uses no column of the table.")
    return (lambda row: ev(tree, row)), used


def add_column(s: EditSession, op: AddColumn) -> None:
    for t in _chosen(s.tables, op.tables):
        if column_in(t, op.name) == op.name or op.name in t.cols:
            raise AgentError("column_exists", f"{label(t)} already has a column '{op.name}'.")
        n = len(t.data["_row_id"])
        if op.value is not None:
            values = [op.value] * n
            why = f"Column added by the agent with the value {op.value!r}."
        elif op.mapping is not None:
            src = column_in(t, op.from_column or "")
            if src is None:
                raise AgentError("unknown_column", f"There is no column '{op.from_column}' in {label(t)}.")
            mapping = {text_key(k): v for k, v in op.mapping.items()}
            values = [mapping.get(text_key(v), op.default) if v is not None else op.default for v in t.data[src]]
            missing = [v for v in value_counts(t.data[src]) if text_key(v) not in mapping]
            if missing:
                s.note(f"{len(missing)} value(s) of {src} are not in the mapping and get "
                       f"{'an empty cell' if op.default is None else repr(op.default)}.")
                s.unmatched(missing)
            why = f"Column added by the agent from {src} with a mapping."
        else:
            fn, used = compile_formula(op.formula or "", list(t.cols))
            sym = t.cfg["currency_symbols"]
            parsed = {c: [parse_number(v, sym, t.eu(c)) if v is not None else None for v in t.data[c]] for c in used}
            values = []
            for i in range(n):
                row = {c: (Decimal(parsed[c][i]) if parsed[c][i] is not None else None) for c in used}
                r = fn(row)
                values.append(None if r is None else _num_text(r if r == r.to_integral_value() else r.quantize(Decimal("0.000001"))))
            blanks = sum(v is None for v in values)
            if blanks:
                s.note(f"{blanks} row(s) get an empty cell (a value is missing, not a number, or divided by zero).")
            why = f"Column added by the agent: {op.name} = {op.formula}."
        s.add_column(t, op.name, values, why)


# ── rename_column ───────────────────────────────────────────────────────────
def rename_column(s: EditSession, op: RenameColumn) -> None:
    for t, col in _with_column(s.tables, op.tables, op.column):
        if op.new_name in t.cols and op.new_name != col:
            raise AgentError("column_exists", f"{label(t)} already has a column '{op.new_name}'.")
        if col != op.new_name:
            s.rename(t, col, op.new_name)


# ── remove_rows ─────────────────────────────────────────────────────────────
def remove_rows(s: EditSession, op: RemoveRows) -> None:
    for t in _chosen(s.tables, op.where.tables):
        idx = rows(t, op.where)
        if idx and len(idx) == len(t.data["_row_id"]):
            raise AgentError("too_broad", f"That would remove every row of {label(t)}. Narrow the condition.")
        s.remove_rows(t, idx, "Row removed by the agent.")
    if not s.effect.rows_removed:
        s.note("No row matched.")


# ── split_column ────────────────────────────────────────────────────────────
def split_parts(v: str | None, op: SplitColumn) -> list[str | None]:
    """The parts of one cell, padded with None to len(op.into)."""
    n = len(op.into)
    if v is None:
        return [None] * n
    if op.pattern is not None:
        m = re.search(op.pattern, v)
        parts = list(m.groups()) if m and m.groups() else []
    else:
        parts = [p.strip() for p in (v.split(op.separator, n - 1) if op.separator.strip() else v.split(None, n - 1))]
    parts = [p if p not in ("", None) else None for p in parts][:n]
    return parts + [None] * (n - len(parts))


def split_column(s: EditSession, op: SplitColumn) -> None:
    if op.pattern is not None:
        try:
            re.compile(op.pattern)
        except re.error as e:
            raise AgentError("bad_pattern", f"The pattern '{op.pattern}' is not valid: {e}") from e
    for t, col in _with_column(s.tables, op.tables, op.column):
        clash = [n for n in op.into if n in t.cols and n != col]
        if clash:
            raise AgentError("column_exists", f"{label(t)} already has {', '.join(clash)}.")
        parts = [split_parts(v, op) for v in t.data[col]]
        unsplit = sum(1 for v, p in zip(t.data[col], parts) if v is not None and p[1] is None)
        if unsplit:
            s.note(f"{unsplit} value(s) of {col} have only one part; the other new columns are empty there.")
        for k, name in enumerate(op.into):
            if name == col:
                continue
            s.add_column(t, name, [p[k] for p in parts], f"Split from {col} by the agent.")
        if not op.keep_original and col not in op.into:
            s.note(f"{col} is kept: Phase B never removes a column (it is left out in Phase T if you drop it there).")


# ── classify ────────────────────────────────────────────────────────────────
def classify(s: EditSession, op: Classify) -> None:
    for t in _chosen(s.tables, op.tables):
        n = len(t.data["_row_id"])
        chosen: list[int | None] = [None] * n
        for k, rule in enumerate(op.rules):
            for i in rows(t, RowFilter(conditions=rule.when)):
                if chosen[i] is None:
                    chosen[i] = k
        counts = [chosen.count(k) for k in range(len(op.rules))]
        s.note("Rows per rule: " + ", ".join(f"{r.value!r}: {c}" for r, c in zip(op.rules, counts))
               + f", no rule: {chosen.count(None)}.")
        target = next((c for c in t.cols if normalize(c) == normalize(op.column)), None)   # exact name: else a new column
        if target is None:
            values = [op.rules[k].value if k is not None else op.default for k in chosen]
            s.add_column(t, op.column, values, f"Column added by the agent: {op.column} by {len(op.rules)} rule(s).")
            continue
        why = f"Classified by the agent into {target}."
        for i, k in enumerate(chosen):
            if k is not None:
                s.edit(t, target, i, op.rules[k].value, why)
            elif not op.keep_existing:
                s.edit(t, target, i, op.default, why)


EXECUTORS: dict[str, Callable] = {
    "resolve_findings": resolve_findings, "set_value": set_value, "replace_values": replace_values,
    "replace_text": replace_text, "scale": scale, "add_column": add_column, "rename_column": rename_column,
    "remove_rows": remove_rows, "split_column": split_column, "classify": classify,
}


def execute(s: EditSession, ops: list) -> None:
    for op in ops:
        if op.op in SHAPE_OPS:
            raise AgentError("needs_transform_phase",
                             f"'{op.op.replace('_', ' ')}' changes the shape of the table, so it belongs in Phase T "
                             "(Transformations), after the checks. Accept the checking phases, then open Phase T and ask "
                             "there. Value fixes, new columns and splitting a column into columns work here.")
        s.begin()
        EXECUTORS[op.op](s, op)
