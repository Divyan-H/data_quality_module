"""Phase T's engine: every operation, applied to a run's tables as Polars frames.

    tables = {"Coupons": DataFrame, "AOV": DataFrame, ...}     # name -> frame, in order
    effect = apply_op(tables, op)                              # changes `tables` in place, returns what it did

Each frame keeps `_row_id` (and `_source_row` while rows still map 1:1 to the source) so every output row can be
traced: a row made from another gets a derived id (`<id>:u3` unpivoted, `<id>:s2` split, `<id>:a1` allocated) and
rows built by grouping get `g<n>`. Values are compared the way the analyst sees them: text ignoring case and
surrounding spaces, numbers whatever their thousands separators, dates whatever their format.

The engine never guesses: a table or column it cannot place is an AgentError the chat turns into a question.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatch

import polars as pl

from src.hygiene.cities import normalize

from .. import safe_regex
from ..errors import AgentError
from ..executors import compile_formula, split_parts
from ..operations import (AddColumn, Aggregate, AllocateByShare, AppendRows, BucketOthers, Classify, Condition,
                          ConvertCurrency, DropColumns, DropTable, LookupJoin, MakeDate, MakePeriod, NetNextPeriod, Pivot,
                          RemoveRows, RenameColumn, ReplaceText, ReplaceValues, RowFilter, Scale, SetValue, SortRows,
                          SplitColumn, SplitRows, Unpivot)
from ..selectors import as_date, find_column, text_key

SYS = ("_row_id", "_source_row")
SAMPLE_ROWS = 8
SAMPLE_COLS = 14
NUMERIC = (pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64, pl.Float32, pl.Float64)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

Tables = dict[str, pl.DataFrame]


# ── effects ─────────────────────────────────────────────────────────────────
class Effect:
    """What one step did: rows and columns before -> after per table, cells changed, notes, a sample of the result."""

    def __init__(self, tables: Tables):
        self.before = {n: (df.height, data_cols(df)) for n, df in tables.items()}
        self.cells = 0
        self.notes: list[str] = []
        self.unmatched: list[str] = []
        self.focus: str | None = None

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def finish(self, tables: Tables) -> dict:
        after = {n: (df.height, data_cols(df)) for n, df in tables.items()}
        changes = []
        for name in dict.fromkeys([*self.before, *after]):
            b, a = self.before.get(name), after.get(name)
            if b is None:
                changes.append({"table": name, "rows_before": None, "rows_after": a[0], "new": True,
                                "columns_added": a[1], "columns_removed": []})
            elif a is None:
                changes.append({"table": name, "rows_before": b[0], "rows_after": None, "dropped": True,
                                "columns_added": [], "columns_removed": []})
            elif b != a or name == self.focus:
                changes.append({"table": name, "rows_before": b[0], "rows_after": a[0],
                                "columns_added": [c for c in a[1] if c not in b[1]],
                                "columns_removed": [c for c in b[1] if c not in a[1]]})
        focus = self.focus if self.focus in tables else next((c["table"] for c in changes if c.get("rows_after")), None)
        sample = None
        if focus:
            df = tables[focus]
            cols = data_cols(df)[:SAMPLE_COLS]
            sample = {"table": focus, "columns": cols,
                      "rows": [[render(v) for v in r] for r in df.select(cols).head(SAMPLE_ROWS).iter_rows()] if cols else []}
        return {"tables": changes, "cells": self.cells, "notes": self.notes, "unmatched": self.unmatched[:50],
                "unmatched_total": len(self.unmatched), "sample": sample}


def data_cols(df: pl.DataFrame) -> list[str]:
    return [c for c in df.columns if not c.startswith("_")]


def render(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


# ── names ───────────────────────────────────────────────────────────────────
def table_name(tables: Tables, name: str | None) -> str:
    """The table `name` means: exact, ignoring case/spaces/punctuation, part of the name, or a glob. None = the
    only table. Ambiguous or unknown names are errors (the chat asks)."""
    names = list(tables)
    if name is None:
        if len(names) == 1:
            return names[0]
        raise AgentError("which_table", f"Which table? The tables are: {', '.join(names)}.")
    if name in tables:
        return name
    n = normalize(name)
    hits = [t for t in names if normalize(t) == n] or [t for t in names if n and n in normalize(t)] \
        or [t for t in names if fnmatch(t.casefold(), name.casefold())]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise AgentError("unknown_table", f"There is no table '{name}'. The tables are: {', '.join(names)}.")
    raise AgentError("which_table", f"'{name}' could mean {', '.join(hits)}. Which one?")


def tables_for(tables: Tables, patterns: list[str] | None) -> list[str]:
    if not patterns:
        return list(tables)
    return list(dict.fromkeys(table_name(tables, p) for p in patterns))


def column(df: pl.DataFrame, name: str, table: str = "") -> str:
    col = find_column(name, data_cols(df))
    if col is None:
        raise AgentError("unknown_column", f"There is no column '{name}'" + (f" in {table}" if table else "")
                         + f". The columns are: {', '.join(data_cols(df)[:40])}.")
    return col


def maybe_column(df: pl.DataFrame, name: str) -> str | None:
    return find_column(name, data_cols(df))


# ── typed views of a column ─────────────────────────────────────────────────
def is_numeric(df: pl.DataFrame, col: str) -> bool:
    return df.schema[col] in NUMERIC


def is_date(df: pl.DataFrame, col: str) -> bool:
    return df.schema[col] in (pl.Date, pl.Datetime)


def text_expr(col: str) -> pl.Expr:
    """How text is compared: trimmed, inner spaces collapsed, case-folded."""
    return pl.col(col).cast(pl.String).str.strip_chars().str.replace_all(r"\s+", " ").str.to_lowercase()


def number_expr(df: pl.DataFrame, col: str) -> pl.Expr:
    if is_numeric(df, col):
        return pl.col(col).cast(pl.Float64)
    return (pl.col(col).cast(pl.String).str.replace_all(r"[,\s₹$€£¥]", "")
            .str.replace(r"^\((.*)\)$", "-$1").cast(pl.Float64, strict=False))


def to_date(v) -> date | None:
    d = as_date(v)
    if d is None:
        return None
    try:
        return date(*d)
    except ValueError:
        return None


def date_expr(df: pl.DataFrame, col: str) -> pl.Expr:
    if is_date(df, col):
        return pl.col(col).cast(pl.Date)
    return pl.col(col).cast(pl.String).map_elements(to_date, return_dtype=pl.Date, skip_nulls=True)


def whole(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    """Number columns whose every value is a whole number are written as integers (7, not 7.0)."""
    fix = []
    for c in cols:
        if c in df.columns and df.schema[c] in (pl.Float32, pl.Float64):
            s = df[c].drop_nulls()
            if s.len() and bool(((s - s.round(0)).abs() < 1e-9).all()) and bool((s.abs() < 9e15).all()):
                fix.append(pl.col(c).round(0).cast(pl.Int64))
    return df.with_columns(fix) if fix else df


def numbers(df: pl.DataFrame, col: str) -> list[float | None]:
    return df.select(number_expr(df, col)).to_series().to_list()


# ── row filters ─────────────────────────────────────────────────────────────
def condition_expr(df: pl.DataFrame, cond: Condition, table: str = "") -> pl.Expr:
    col = column(df, cond.column, table)
    op, v = cond.op, cond.value
    raw = pl.col(col)
    empty = raw.is_null() | (raw.cast(pl.String).str.strip_chars() == "")
    if op == "empty":
        return empty
    if op == "not_empty":
        return ~empty
    if op in ("gt", "gte", "lt", "lte"):
        d = to_date(v) if not isinstance(v, (int, float)) else None
        left, right = (date_expr(df, col), pl.lit(d)) if d is not None else (number_expr(df, col), pl.lit(float(_num(v))))
        e = {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]
        return e.fill_null(False)
    key = text_expr(col)
    if op in ("eq", "ne"):
        e = key == text_key(v)
        if isinstance(v, (int, float)):
            e = e | (number_expr(df, col) == float(v))
        e = e.fill_null(False)
        return e if op == "eq" else ~e
    if op in ("in", "not_in"):
        e = key.is_in([text_key(x) for x in v]).fill_null(False)
        return e if op == "in" else ~e
    if op == "contains":
        return key.str.contains(text_key(v), literal=True).fill_null(False)
    if op == "starts_with":
        return key.str.starts_with(text_key(v)).fill_null(False)
    if op == "ends_with":
        return key.str.ends_with(text_key(v)).fill_null(False)
    if op == "matches":
        safe_regex.check(str(v))
        return raw.cast(pl.String).str.contains(f"(?i){v}").fill_null(False)
    raise AgentError("bad_condition", f"Unknown condition '{op}'.")


def _num(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except ValueError as e:
        raise AgentError("bad_condition", f"'{v}' is not a number.") from e


def mask(df: pl.DataFrame, where: RowFilter | None, table: str = "") -> pl.Expr:
    e = pl.lit(True)
    if where is None:
        return e
    if where.rows:
        if "_source_row" not in df.columns or df["_source_row"].null_count() == df.height:
            raise AgentError("no_excel_rows", f"Rows of {table} no longer match Excel rows (the table was reshaped); "
                                              "use a condition instead.")
        e = e & pl.col("_source_row").is_in([int(r) for r in where.rows]).fill_null(False)
    if where.exclude_rows and "_source_row" in df.columns:
        e = e & ~pl.col("_source_row").is_in([int(r) for r in where.exclude_rows]).fill_null(False)
    for c in where.conditions:
        e = e & condition_expr(df, c, table)
    return e


def literal_for(df: pl.DataFrame, col: str, value) -> tuple[pl.DataFrame, pl.Expr]:
    """A literal that fits the column: a number in a number column, a date in a date column; otherwise the column
    becomes text."""
    if value is None:
        return df, pl.lit(None, dtype=df.schema[col])
    if is_numeric(df, col):
        try:
            n = float(str(value).replace(",", ""))
            return df, pl.lit(n).cast(df.schema[col] if n.is_integer() or df.schema[col] in (pl.Float32, pl.Float64) else pl.Float64)
        except ValueError:
            pass
    if is_date(df, col):
        d = to_date(value)
        if d is not None:
            return df, pl.lit(d)
    if df.schema[col] != pl.String:
        df = df.with_columns(pl.col(col).map_elements(render, return_dtype=pl.String, skip_nulls=True))
    return df, pl.lit(str(value))


def count_true(df: pl.DataFrame, e: pl.Expr) -> int:
    return int(df.select(e.cast(pl.Int64).sum()).item() or 0)


def with_tables(where: RowFilter, tables: Tables) -> list[str]:
    return tables_for(tables, where.tables)


# ── value operations ────────────────────────────────────────────────────────
def set_value(tables: Tables, op: SetValue, fx: Effect) -> None:
    hit = False
    for name in with_tables(op.where, tables):
        df = tables[name]
        col = maybe_column(df, op.column)
        if col is None:
            continue
        hit = True
        m = mask(df, op.where, name)
        df, lit = literal_for(df, col, op.value)
        changes = m & ~(pl.col(col).eq_missing(lit))
        fx.cells += count_true(df, changes)
        tables[name] = df.with_columns(pl.when(m).then(lit).otherwise(pl.col(col)).alias(col))
        fx.focus = name
    if not hit:
        raise AgentError("unknown_column", f"No table has a column '{op.column}'.")


def replace_values(tables: Tables, op: ReplaceValues, fx: Effect) -> None:
    keys = {text_key(k) if op.ignore_case else k: v for k, v in op.mapping.items()}
    found: set[str] = set()
    for name in with_tables(op.where, tables):
        df = tables[name]
        cols = [c for c in (maybe_column(df, n) for n in op.columns) if c] if op.columns else \
            [c for c in data_cols(df) if df.schema[c] == pl.String]
        m = mask(df, op.where, name)
        for col in cols:
            key = text_expr(col) if op.ignore_case else pl.col(col).cast(pl.String)
            present = set(df.select(key.unique()).to_series().drop_nulls().to_list()) & set(keys)
            if not present:
                continue
            found |= present
            if df.schema[col] != pl.String:
                df = df.with_columns(pl.col(col).map_elements(render, return_dtype=pl.String, skip_nulls=True))
            hit = m & key.is_in(list(present)).fill_null(False)
            fx.cells += count_true(df, hit)
            mapped = key.replace_strict(list(present), [keys[k] for k in present], default=None, return_dtype=pl.String)
            df = df.with_columns(pl.when(hit).then(mapped).otherwise(pl.col(col)).alias(col))
            fx.focus = name
        tables[name] = df
    fx.unmatched += [k for k in op.mapping if (text_key(k) if op.ignore_case else k) not in found]


def replace_text(tables: Tables, op: ReplaceText, fx: Effect) -> None:
    pattern = op.find if op.regex else re.escape(op.find)
    if op.ignore_case:
        pattern = f"(?i){pattern}"
    safe_regex.check(pattern)
    repl = op.replace if op.regex else op.replace.replace("$", "$$")
    for name in with_tables(op.where, tables):
        df = tables[name]
        col = maybe_column(df, op.column)
        if col is None:
            continue
        m = mask(df, op.where, name)
        new = pl.col(col).cast(pl.String).str.replace_all(pattern, repl).str.strip_chars()
        new = pl.when(new == "").then(None).otherwise(new)
        fx.cells += count_true(df, m & ~new.eq_missing(pl.col(col).cast(pl.String)))
        tables[name] = df.with_columns(pl.when(m).then(new).otherwise(pl.col(col).cast(pl.String)).alias(col))
        fx.focus = name


def scale(tables: Tables, op: Scale, fx: Effect) -> None:
    for name in with_tables(op.where, tables):
        df = tables[name]
        m = mask(df, op.where, name)
        for want in op.columns:
            col = maybe_column(df, want)
            if col is None:
                continue
            n = number_expr(df, col)
            bad = count_true(df, n.is_null() & pl.col(col).is_not_null())
            if bad:
                fx.note(f"{bad} cell(s) of {col} in {name} are not numbers and were left as they are.")
            fx.cells += count_true(df, m & n.is_not_null())
            scaled = (n * op.factor).round(10)
            if bad == 0:                                   # a number column stays a number column
                df = whole(df.with_columns(pl.when(m).then(scaled).otherwise(n).alias(col)), [col])
            else:                                          # mixed: scaled numbers written as plain text, the rest kept
                text = scaled.map_elements(render, return_dtype=pl.String, skip_nulls=True)
                df = df.with_columns(pl.when(m & n.is_not_null()).then(text)
                                     .otherwise(pl.col(col).map_elements(render, return_dtype=pl.String, skip_nulls=True)).alias(col))
            fx.focus = name
        tables[name] = df


def _new_column_check(df: pl.DataFrame, name: str, table: str) -> None:
    if name in df.columns or any(normalize(c) == normalize(name) for c in data_cols(df)):
        raise AgentError("column_exists", f"{table} already has a column '{name}'.")


def add_column(tables: Tables, op: AddColumn, fx: Effect) -> None:
    for name in tables_for(tables, op.tables):
        df = tables[name]
        if op.tables is None and op.from_column and maybe_column(df, op.from_column) is None:
            continue
        if op.tables is None and op.formula and len(tables) > 1:
            try:
                compile_formula(op.formula, data_cols(df))
            except AgentError:
                continue
        _new_column_check(df, op.name, name)
        if op.value is not None:
            df = df.with_columns(pl.lit(op.value).alias(op.name))
        elif op.mapping is not None:
            src = column(df, op.from_column or "", name)
            keys = {text_key(k): v for k, v in op.mapping.items()}
            key = text_expr(src)
            df = df.with_columns(key.replace_strict(list(keys), list(keys.values()), default=op.default,
                                                    return_dtype=pl.String).alias(op.name))
            missing = df.filter(~key.is_in(list(keys)).fill_null(False) & pl.col(src).is_not_null())[src].unique().to_list()
            if missing:
                fx.note(f"{len(missing)} value(s) of {src} are not in the mapping and get "
                        f"{'an empty cell' if op.default is None else repr(op.default)}.")
                fx.unmatched += [render(v) for v in missing]
        else:
            fn, used = compile_formula(op.formula or "", data_cols(df))
            cols = {c: numbers(df, c) for c in used}
            out = []
            for i in range(df.height):
                row = {c: (Decimal(repr(cols[c][i])) if cols[c][i] is not None else None) for c in used}
                r = fn(row)
                out.append(None if r is None else float(r))
            blanks = sum(v is None for v in out)
            if blanks:
                fx.note(f"{blanks} row(s) of {name} get an empty {op.name} (a value is missing, not a number, or divided by zero).")
            df = df.with_columns(pl.Series(op.name, out, dtype=pl.Float64).round(6))
        tables[name] = df
        fx.focus = name
        fx.cells += df.height


def rename_column(tables: Tables, op: RenameColumn, fx: Effect) -> None:
    hit = False
    for name in tables_for(tables, op.tables):
        df = tables[name]
        col = maybe_column(df, op.column)
        if col is None:
            continue
        if op.new_name in df.columns and op.new_name != col:
            raise AgentError("column_exists", f"{name} already has a column '{op.new_name}'.")
        tables[name] = df.rename({col: op.new_name})
        hit = True
        fx.focus = name
    if not hit:
        raise AgentError("unknown_column", f"No table has a column '{op.column}'.")


def remove_rows(tables: Tables, op: RemoveRows, fx: Effect) -> None:
    for name in with_tables(op.where, tables):
        df = tables[name]
        try:
            m = mask(df, op.where, name)
            gone = count_true(df, m)
        except AgentError as e:
            if e.code == "unknown_column" and not op.where.tables and len(tables) > 1:
                continue
            raise
        if gone and gone == df.height:
            raise AgentError("too_broad", f"That would remove every row of {name}. Narrow the condition.")
        tables[name] = df.filter(~m)
        fx.focus = name
        if gone:
            fx.note(f"{gone} row(s) removed from {name}.")


def split_column(tables: Tables, op: SplitColumn, fx: Effect) -> None:
    for name in tables_for(tables, op.tables):
        df = tables[name]
        col = maybe_column(df, op.column)
        if col is None:
            continue
        for n in op.into:
            if n != col:
                _new_column_check(df, n, name)
        parts = [split_parts(None if v is None else render(v), op) for v in df[col].to_list()]
        new = [pl.Series(n, [p[k] for p in parts], dtype=pl.String) for k, n in enumerate(op.into)]
        df = df.with_columns(new)
        if not op.keep_original and col not in op.into:
            df = df.drop(col)
        tables[name] = df
        fx.focus = name
        fx.cells += df.height


def classify(tables: Tables, op: Classify, fx: Effect) -> None:
    for name in tables_for(tables, op.tables):
        df = tables[name]
        try:
            masks = [mask(df, RowFilter(conditions=r.when), name) for r in op.rules]
        except AgentError:
            if op.tables is None and len(tables) > 1:
                continue
            raise
        target = next((c for c in data_cols(df) if normalize(c) == normalize(op.column)), None)
        if target is not None and df.schema[target] != pl.String:
            df = df.with_columns(pl.col(target).map_elements(render, return_dtype=pl.String, skip_nulls=True))
        otherwise = pl.col(target) if (target and op.keep_existing) else pl.lit(op.default, dtype=pl.String)
        e = otherwise
        for m, r in reversed(list(zip(masks, op.rules))):
            e = pl.when(m).then(pl.lit(r.value, dtype=pl.String)).otherwise(e)
        counts = [count_true(df, m) for m in masks]
        df = df.with_columns(e.alias(target or op.column))
        fx.note("Rows per rule (first match wins): " + ", ".join(f"{r.value!r}: {c}" for r, c in zip(op.rules, counts)) + ".")
        tables[name] = df
        fx.focus = name
        fx.cells += df.height


# ── shape operations ────────────────────────────────────────────────────────
def unpivot(tables: Tables, op: Unpivot, fx: Effect) -> None:
    name = table_name(tables, op.table)
    df = tables[name]
    if op.columns:
        cols = [column(df, c, name) for c in op.columns]
    else:
        rx = safe_regex.compiled(op.pattern or "", True)
        cols = [c for c in data_cols(df) if rx.search(c)]
    if not cols:
        raise AgentError("no_columns", f"No column of {name} matches {op.pattern!r}.")
    for n in (op.names_to, op.values_to):
        if n in df.columns and n not in cols:
            raise AgentError("column_exists", f"{name} already has a column '{n}'; choose another name.")
    if len({df.schema[c] for c in cols}) > 1:                              # mixed types: compare as text
        df = df.with_columns([pl.col(c).map_elements(render, return_dtype=pl.String, skip_nulls=True) for c in cols])
    index = [c for c in df.columns if c not in cols]
    order = {c: k for k, c in enumerate(cols)}
    long = (df.with_row_index("_ord").unpivot(on=cols, index=["_ord", *index], variable_name=op.names_to, value_name=op.values_to)
            .with_columns(pl.col(op.names_to).replace_strict(order, return_dtype=pl.Int64).alias("_k"))
            .sort(["_ord", "_k"]))
    if op.drop_empty:
        v = pl.col(op.values_to)
        long = long.filter(v.is_not_null() & (v.cast(pl.String).str.strip_chars() != ""))
    long = long.with_columns((pl.col("_row_id").cast(pl.String) + ":u" + (pl.col("_k") + 1).cast(pl.String)).alias("_row_id"))
    tables[name] = long.drop(["_ord", "_k"])
    fx.focus = name
    fx.note(f"{len(cols)} column(s) turned into rows: {', '.join(cols[:6])}{' …' if len(cols) > 6 else ''}.")


def pivot(tables: Tables, op: Pivot, fx: Effect) -> None:
    name = table_name(tables, op.table)
    df = tables[name]
    index = [column(df, c, name) for c in op.index]
    on = column(df, op.columns_from, name)
    vals = column(df, op.values_from, name)
    if op.agg in ("sum", "mean"):
        df = df.with_columns(number_expr(df, vals).alias(vals))
    agg = {"sum": "sum", "mean": "mean", "first": "first", "count": "len"}[op.agg]
    wide = df.with_columns(pl.col(on).map_elements(render, return_dtype=pl.String, skip_nulls=True)) \
        .pivot(on=on, index=index, values=vals, aggregate_function=agg, maintain_order=True)
    wide = wide.with_columns(pl.format("g{}", pl.int_range(1, pl.len() + 1)).alias("_row_id"))
    tables[name] = whole(wide, [c for c in wide.columns if c not in index and c != "_row_id"])
    fx.focus = name
    fx.note(f"Values of {on} became {len(wide.columns) - len(index) - 1} column(s).")


SPLIT_DEFAULT = [",", ";", "/", "|", "&", " and "]


def split_items(v: str, separators: list[str], keep_together: list[str]) -> list[str]:
    text = v
    keep = sorted(keep_together, key=len, reverse=True)
    for n, name in enumerate(keep):
        text = re.sub(re.escape(name), f"\x00{n}\x00", text, flags=re.I)
    parts_rx = "|".join(r"\s+and\s+" if s.strip().lower() == "and" else (r"\s+" if s == " " else re.escape(s))
                        for s in separators)
    parts = re.split(parts_rx, text, flags=re.I) if parts_rx else [text]
    out = []
    for p in parts:
        p = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], p).strip()
        if p:
            out.append(p)
    return out or [v.strip()]


def split_rows(tables: Tables, op: SplitRows, fx: Effect) -> None:
    name = table_name(tables, op.table)
    df = tables[name]
    col = column(df, op.column, name)
    alloc = [column(df, c, name) for c in op.allocate]
    parts = [split_items(render(v), op.separators, op.keep_together) if v is not None else [None]
             for v in df[col].to_list()]
    n = [len(p) for p in parts]
    multi = sum(1 for k in n if k > 1)
    out = df.with_columns(pl.Series("_parts", parts, dtype=pl.List(pl.String)), pl.Series("_n", n, dtype=pl.Int64),
                          pl.int_range(pl.len()).alias("_ord"))
    out = out.explode("_parts").with_columns(pl.int_range(pl.len()).over("_ord").alias("_k"))
    out = out.with_columns(pl.col("_parts").alias(col))
    for a in alloc:
        num = number_expr(df, a)
        keep_text = df.schema[a] == pl.String and count_true(df, num.is_null() & pl.col(a).is_not_null()) > 0
        nexpr = number_expr(out, a)
        if op.method == "repeat" or keep_text:
            if keep_text:
                fx.note(f"{a} has non-numbers; they were copied to each row unchanged.")
            continue
        if op.method == "equal":
            share = nexpr / pl.col("_n")
        else:
            integral = nexpr.round(0) == nexpr
            base = (nexpr / pl.col("_n")).floor()
            rem = nexpr - base * pl.col("_n")
            share = pl.when(integral).then(base + (pl.col("_k") < rem).cast(pl.Float64)).otherwise(nexpr / pl.col("_n"))
        out = out.with_columns(pl.when(pl.col("_n") > 1).then(share).otherwise(nexpr).alias(a))
    out = out.with_columns(pl.when(pl.col("_n") > 1).then(pl.col("_row_id").cast(pl.String) + ":s" + (pl.col("_k") + 1).cast(pl.String))
                           .otherwise(pl.col("_row_id")).alias("_row_id"))
    tables[name] = whole(out.drop(["_parts", "_n", "_ord", "_k"]), alloc)
    fx.focus = name
    fx.note(f"{multi} row(s) of {name} held more than one {col}; each became one row per value"
            + (f", sharing {', '.join(alloc)} ({op.method.replace('_', ' ')})." if alloc else "."))


def _key(v) -> str | None:
    """Join key: dates as ISO, numbers without trailing .0, text ignoring case and spaces."""
    if v is None:
        return None
    d = v if isinstance(v, date) else None
    if d is None and isinstance(v, str):
        d = to_date(v) if re.match(r"^\s*\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", v) else None
    if d is not None:
        return d.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return text_key(v)


def _keys(df: pl.DataFrame, cols: list[str]) -> list[tuple]:
    data = [df[c].to_list() for c in cols]
    return [tuple(_key(col[i]) for col in data) for i in range(df.height)]


def allocate_by_share(tables: Tables, op: AllocateByShare, fx: Effect) -> None:
    name = table_name(tables, op.table)
    sname = table_name(tables, op.share_table)
    df, sh = tables[name], tables[sname]
    left_on = [column(df, a, name) for a in op.on]
    right_on = [column(sh, b, sname) for b in op.on.values()]
    cat = column(sh, op.category, sname)
    share = column(sh, op.share, sname) if op.share else None
    cols = [column(df, c, name) for c in op.columns]
    if cat in df.columns:
        raise AgentError("column_exists", f"{name} already has a column '{cat}'.")
    groups: dict[tuple, list[tuple[str, float | None]]] = {}
    shares = numbers(sh, share) if share else [None] * sh.height
    for k, c, s in zip(_keys(sh, right_on), sh[cat].to_list(), shares):
        groups.setdefault(k, []).append((render(c), s))
    for k, rows in groups.items():                                         # equal parts, or % -> fraction
        if share is None:
            groups[k] = [(c, 1 / len(rows)) for c, _ in rows]
        else:
            total = sum(s or 0 for _, s in rows)
            groups[k] = [(c, (s or 0) / 100 if total > 1.5 else (s or 0)) for c, s in rows]
    values = {c: numbers(df, c) for c in cols}
    records, used, unmatched, ids = [], set(), 0, df["_row_id"].to_list()
    base = df.drop(cols).to_dicts()
    for i, (k, row) in enumerate(zip(_keys(df, left_on), base)):
        parts = groups.get(k)
        if not parts:
            unmatched += 1
            records.append({**row, cat: op.unmatched_label, **{c: values[c][i] for c in cols}})
            continue
        used.add(k)
        frac_left = 1 - sum(f for _, f in parts)
        plan = parts + ([(op.unmatched_label, frac_left)] if frac_left > 0.001 else [])
        alloc = {c: _allocate(values[c][i], [f for _, f in plan], op.integer) for c in cols}
        for j, (city, _) in enumerate(plan):
            records.append({**row, "_row_id": f"{ids[i]}:a{j + 1}", cat: city, **{c: alloc[c][j] for c in cols}})
    if op.keep_unmatched_shares:
        extra = 0
        srows = sh.to_dicts()
        for k, srow in zip(_keys(sh, right_on), srows):
            if k not in used:
                extra += 1
                rec = {c: None for c in df.columns if c not in cols}
                rec.update({lc: srow[rc] for lc, rc in zip(left_on, right_on)})
                rec.update({"_row_id": f"{sname}:{extra}", cat: srow[cat], **{c: 0.0 for c in cols}})
                records.append(rec)
        if extra:
            fx.note(f"{extra} row(s) of {sname} had no matching row; kept with zero {', '.join(cols)}.")
    schema = {**{c: df.schema[c] for c in df.columns if c not in cols}, cat: pl.String, **{c: pl.Float64 for c in cols}}
    out = pl.DataFrame(records, schema=schema, strict=False).select([*[c for c in df.columns if c not in cols], cat, *cols])
    if "_source_row" in out.columns:
        out = out.with_columns(pl.lit(None, dtype=out.schema["_source_row"]).alias("_source_row"))
    tables[name] = whole(out, cols) if op.integer else out
    fx.focus = name
    if unmatched:
        fx.note(f"{unmatched} row(s) of {name} had no match in {sname}; their numbers are kept under "
                f"{cat} = {op.unmatched_label!r}.")


def _allocate(v: float | None, fractions: list[float], integer: bool) -> list[float | None]:
    if v is None:
        return [None] * len(fractions)
    raw = [v * f for f in fractions]
    if not integer or v != int(v):
        return [round(x, 10) for x in raw]
    floors = [int(x // 1) for x in raw]
    rest = int(v) - sum(floors)                                            # largest remainders get the leftover units
    for j in sorted(range(len(raw)), key=lambda j: (-(raw[j] - floors[j]), j))[:max(rest, 0)]:
        floors[j] += 1
    return [float(x) for x in floors]


def aggregate(tables: Tables, op: Aggregate, fx: Effect) -> None:
    name = table_name(tables, op.table)
    df = tables[name]
    by = [column(df, c, name) for c in op.by]
    sums = [column(df, c, name) for c in op.sum]
    means = [column(df, c, name) for c in op.mean]
    firsts = [column(df, c, name) for c in op.first]
    conv = [number_expr(df, c).alias(c) for c in dict.fromkeys(sums + means)]
    work = df.with_columns(conv)
    if op.blank_as_zero:
        work = work.with_columns([pl.col(c).fill_null(0) for c in dict.fromkeys(sums)])
    aggs = [pl.col(c).sum() for c in sums] + [pl.col(c).mean().alias(c if c not in sums else f"{c} (mean)") for c in means] \
        + [pl.col(c).first() for c in firsts] + ([pl.len().alias(op.count_as)] if op.count_as else [])
    out = work.group_by(by, maintain_order=True).agg(aggs)
    out = out.with_columns(pl.format("g{}", pl.int_range(1, pl.len() + 1)).alias("_row_id"))
    tables[name] = whole(out.select(["_row_id", *[c for c in out.columns if c != "_row_id"]]), sums)
    fx.focus = name
    fx.note(f"{df.height:,} row(s) of {name} added up into {out.height:,} by {', '.join(by)}.")


def bucket_others(tables: Tables, op: BucketOthers, fx: Effect) -> None:
    name = table_name(tables, op.table)
    df = tables[name]
    col = column(df, op.column, name)
    if op.keep is not None:
        keep = {text_key(v) for v in op.keep}
    elif op.keep_from is not None:
        ref = tables[table_name(tables, op.keep_from.get("table"))]
        rcol = column(ref, op.keep_from.get("column", op.column), op.keep_from.get("table", ""))
        keep = {text_key(v) for v in ref[rcol].drop_nulls().to_list()}
    else:
        top = df.group_by(text_expr(col).alias("_k")).len().sort("len", descending=True).head(op.top_n)
        keep = set(top["_k"].drop_nulls().to_list())
    other = ~text_expr(col).is_in(list(keep)).fill_null(False) & pl.col(col).is_not_null()
    n = count_true(df, other)
    also = [column(df, c, name) for c in op.also]
    cast = [pl.col(c).map_elements(render, return_dtype=pl.String, skip_nulls=True) for c in [col, *also] if df.schema[c] != pl.String]
    df = df.with_columns(cast) if cast else df
    labelled = df.with_columns([pl.when(other).then(pl.lit(op.label)).otherwise(pl.col(c)).alias(c) for c in [col, *also]])
    if op.sum_by is not None and n:
        by = [column(df, c, name) for c in op.sum_by]
        sums = [column(df, c, name) for c in op.sum]
        kept = labelled.filter(~other)
        rest = labelled.filter(other).with_columns([number_expr(df, c).fill_null(0).alias(c) for c in sums])
        keys = list(dict.fromkeys([*by, col, *also]))
        grouped = rest.group_by(keys, maintain_order=True).agg([pl.col(c).sum() for c in sums])
        grouped = grouped.with_columns(pl.format("o{}", pl.int_range(1, pl.len() + 1)).alias("_row_id"))
        kept = kept.with_columns([number_expr(df, c).alias(c) for c in sums])
        labelled = pl.concat([kept, grouped], how="diagonal_relaxed")
        fx.note(f"{n} row(s) outside the list became {grouped.height} '{op.label}' row(s) per {', '.join(by)}.")
    else:
        fx.note(f"{n} row(s) of {name} relabelled '{op.label}'.")
    fx.cells += n
    tables[name] = whole(labelled, [column(df, c, name) for c in op.sum])
    fx.focus = name


def _month(v) -> tuple[int, int] | None:
    d = v if isinstance(v, date) else to_date(v)
    if d is not None:
        return (d.year, d.month)
    if isinstance(v, str):                                                 # 'Oct-24', 'October 2024', '2024-10'
        m = re.match(r"^\s*([A-Za-z]{3,9})[\s\-/']*(\d{2,4})\s*$", v)
        if m and m.group(1)[:3].lower() in MONTHS:
            y = int(m.group(2))
            return (y + 2000 if y < 100 else y, MONTHS[m.group(1)[:3].lower()])
        m = re.match(r"^\s*(\d{4})[-/](\d{1,2})\s*$", v)
        if m and 1 <= int(m.group(2)) <= 12:
            return (int(m.group(1)), int(m.group(2)))
    return None


def net_next_period(tables: Tables, op: NetNextPeriod, fx: Effect) -> None:
    """Per group and calendar month: Allocation = the month's positive spend, Next Month Return = the exact next
    month's negative spend, Final = Allocation + Return. A month with only a negative (in a group that has both signs)
    is a return month: Final = 0. A group with only positive or only negative values keeps them."""
    name = table_name(tables, op.table)
    df = tables[name]
    val = column(df, op.value, name)
    per = column(df, op.period, name)
    by = [column(df, c, name) for c in op.group_by]
    nums = numbers(df, val)
    months = [_month(v) for v in df[per].to_list()]
    bad = sum(1 for m in months if m is None)
    if bad:
        fx.note(f"{bad} row(s) have no readable month in {per} and were left out.")
    keys = _keys(df, by) if by else [()] * df.height
    shown = df.select(by).to_dicts() if by else [{}] * df.height
    pos: dict[tuple, dict] = {}
    neg: dict[tuple, dict] = {}
    label: dict[tuple, dict] = {}
    for k, m, v, s in zip(keys, months, nums, shown):
        if m is None or v is None:
            continue
        label.setdefault(k, s)
        bucket = pos if v >= 0 else neg
        bucket.setdefault(k, {}).setdefault(m, 0.0)
        bucket[k][m] += v
    records = []
    for k in label:
        p, n = pos.get(k, {}), neg.get(k, {})
        mixed = bool(p) and bool(n)
        for m in sorted(set(p) | set(n)):
            nxt = (m[0] + (m[1] == 12), 1 if m[1] == 12 else m[1] + 1)
            alloc = p.get(m, 0.0)
            ret = n.get(nxt, 0.0) if mixed and alloc else 0.0
            if mixed:
                final = alloc + ret if alloc else 0.0
            else:
                final = alloc + n.get(m, 0.0)
            records.append({**label[k], op.period: date(m[0], m[1], 1), "Month (M/Y)": date(m[0], m[1], 1).strftime("%b-%y"),
                            "Allocation": round(alloc, 2), "Next Month Return": round(ret, 2), op.output: round(final, 2)})
    out = pl.DataFrame(records) if records else pl.DataFrame({c: [] for c in [*by, op.period, op.output]})
    out = out.with_columns(pl.format("g{}", pl.int_range(1, pl.len() + 1)).alias("_row_id"))
    tables[name] = out.select(["_row_id", *[c for c in out.columns if c != "_row_id"]])
    fx.focus = name
    fx.note(f"{df.height:,} row(s) summarised into {out.height:,} month row(s); next-month negatives netted against "
            "the month before.")


def append_rows(tables: Tables, op: AppendRows, fx: Effect) -> None:
    name = table_name(tables, op.table)
    sname = table_name(tables, op.source)
    if sname == name:
        raise AgentError("same_table", "A table cannot be appended to itself.")
    df, src = tables[name], tables[sname]
    ren = {column(src, a, sname): b for a, b in op.rename.items()}
    src = src.rename(ren)
    if op.where:
        before = src.height
        src = src.filter(mask(src, RowFilter(conditions=op.where), sname))
        fx.note(f"{src.height:,} of {before:,} row(s) of {sname} meet the condition.")
    src = src.with_columns([pl.lit(v).alias(k) for k, v in op.set.items()])
    target = df.columns
    dropped = [c for c in data_cols(src) if c not in target]
    if dropped:
        fx.note(f"Columns of {sname} not in {name} were left out: {', '.join(dropped[:10])}.")
    missing = [c for c in data_cols(df) if c not in src.columns]
    if missing:
        fx.note(f"Appended rows have no {', '.join(missing[:10])} (left empty).")
    src = src.select([c for c in target if c in src.columns])
    for c in target:                                                       # different types: compare as text
        if c in src.columns and src.schema[c] != df.schema[c] and src.schema[c] != pl.Null:
            df = df.with_columns(pl.col(c).map_elements(render, return_dtype=pl.String, skip_nulls=True)) if df.schema[c] != pl.String else df
            src = src.with_columns(pl.col(c).map_elements(render, return_dtype=pl.String, skip_nulls=True))
    if "_source_row" in src.columns:
        src = src.with_columns(pl.lit(None, dtype=src.schema["_source_row"]).alias("_source_row"))
    tables[name] = pl.concat([df, src], how="diagonal_relaxed").select(target)
    if op.drop_source:
        del tables[sname]
    fx.focus = name


def lookup_join(tables: Tables, op: LookupJoin, fx: Effect) -> None:
    name = table_name(tables, op.table)
    lname = table_name(tables, op.lookup)
    df, lk = tables[name], tables[lname]
    left_on = [column(df, a, name) for a in op.on]
    right_on = [column(lk, b, lname) for b in op.on.values()]
    bring = [column(lk, c, lname) for c in op.columns]
    index: dict[tuple, list[list]] = {}
    data = [lk[c].to_list() for c in bring]
    for i, k in enumerate(_keys(lk, right_on)):
        index.setdefault(k, []).append([col[i] for col in data])
    dup = sum(1 for v in index.values() if len(v) > 1)
    if dup:
        fx.note(f"{dup} key(s) appear more than once in {lname}; their values were "
                f"{'averaged' if op.duplicates == 'mean' else 'taken from the first row'}.")
    numeric = [is_numeric(lk, c) or (lk.schema[c] == pl.String and lk.select(number_expr(lk, c).is_not_null().all()).item())
               for c in bring]

    def pick(rows: list[list], j: int):
        vals = [r[j] for r in rows if r[j] is not None]
        if not vals:
            return None
        if op.duplicates == "mean" and numeric[j]:
            nums = [float(str(v).replace(",", "")) for v in vals]
            return sum(nums) / len(nums)
        return vals[0]

    out_cols: dict[str, list] = {}
    names = [c if c not in df.columns else f"{c} ({lname})" for c in bring]
    matched = 0
    keys = _keys(df, left_on)
    for j, n in enumerate(names):
        out_cols[n] = []
    for k in keys:
        rows = index.get(k)
        matched += rows is not None
        for j, n in enumerate(names):
            out_cols[n].append(pick(rows, j) if rows else None)
    series = [pl.Series(n, v, dtype=pl.Float64 if numeric[j] else pl.String, strict=False) for j, (n, v) in enumerate(out_cols.items())]
    tables[name] = df.with_columns(series)
    fx.focus = name
    fx.cells += matched
    if matched < df.height:
        fx.note(f"{df.height - matched:,} of {df.height:,} row(s) of {name} found no match in {lname}; their "
                f"{', '.join(names)} {'is' if len(names) == 1 else 'are'} empty.")


def convert_currency(tables: Tables, op: ConvertCurrency, fx: Effect) -> None:
    name = table_name(tables, op.table)
    df = tables[name]
    cols = [column(df, c, name) for c in op.columns]
    if op.rate is not None:
        rate = pl.lit(op.rate)
    else:
        dcol = column(df, op.date_column or "", name)
        d = date_expr(df, dcol)
        rate = pl.lit(None, dtype=pl.Float64)
        for r in reversed(op.rates or []):
            lo, hi = to_date(r.get("from")), to_date(r.get("to"))
            if lo is None or hi is None or "rate" not in r:
                raise AgentError("bad_rates", "Each rate needs from, to (dates) and rate.")
            rate = pl.when((d >= lo) & (d <= hi)).then(pl.lit(float(r["rate"]))).otherwise(rate)
        missing = count_true(df, rate.is_null())
        if missing:
            fx.note(f"{missing} row(s) have no rate for their date and were left unconverted.")
    for c in cols:
        n = number_expr(df, c)
        fx.cells += count_true(df, n.is_not_null() & rate.is_not_null())
        df = df.with_columns(pl.when(rate.is_not_null()).then((n * rate).round(6)).otherwise(n).alias(c))
    tables[name] = df
    fx.focus = name
    if op.note:
        fx.note(f"Converted {', '.join(cols)}: {op.note}.")


def sort_rows(tables: Tables, op: SortRows, fx: Effect) -> None:
    name = table_name(tables, op.table)
    df = tables[name]
    keys = []
    for c in op.by:
        col = column(df, c, name)
        if is_numeric(df, col) or is_date(df, col):
            keys.append(pl.col(col))
        elif df.select(date_expr(df, col).is_not_null().sum()).item() >= max(1, df[col].drop_nulls().len() * 0.8):
            keys.append(date_expr(df, col))
        elif df.select(number_expr(df, col).is_not_null().sum()).item() >= max(1, df[col].drop_nulls().len() * 0.8):
            keys.append(number_expr(df, col))
        else:
            keys.append(text_expr(col))
    desc = (op.descending + [False] * len(keys))[:len(keys)]
    tables[name] = df.sort(keys, descending=desc, nulls_last=True, maintain_order=True)
    fx.focus = name


def make_date(tables: Tables, op: MakeDate, fx: Effect) -> None:
    name = table_name(tables, op.table)
    df = tables[name]
    mcol, dcol = column(df, op.month, name), column(df, op.day, name)
    _new_column_check(df, op.name, name)
    out = []
    bad = 0
    for m, d in zip(df[mcol].to_list(), df[dcol].to_list()):
        ym = _month(m)
        day = re.search(r"\d+", str(d)) if d is not None else None
        value = None
        if ym and day:
            try:
                value = date(ym[0], ym[1], int(day.group()))
            except ValueError:
                bad += 1
        out.append(value)
    if bad:
        fx.note(f"{bad} row(s) are impossible dates (e.g. 30 February) and got an empty {op.name}; "
                f"'remove rows where {op.name} is empty' drops them.")
    tables[name] = df.with_columns(pl.Series(op.name, out, dtype=pl.Date))
    fx.focus = name


def make_period(tables: Tables, op: MakePeriod, fx: Effect) -> None:
    name = table_name(tables, op.table)
    df = tables[name]
    col = column(df, op.column, name)
    _new_column_check(df, op.name, name)
    every = {"month": "1mo", "quarter": "1q", "year": "1y", "week": "1w"}[op.unit]
    tables[name] = df.with_columns(date_expr(df, col).dt.truncate(every).alias(op.name))
    fx.focus = name


def drop_columns(tables: Tables, op: DropColumns, fx: Effect) -> None:
    name = table_name(tables, op.table)
    df = tables[name]
    cols = [column(df, c, name) for c in op.columns]
    if len(cols) >= len(data_cols(df)):
        raise AgentError("too_broad", f"That would drop every column of {name}.")
    tables[name] = df.drop(cols)
    fx.focus = name


def drop_table(tables: Tables, op: DropTable, fx: Effect) -> None:
    name = table_name(tables, op.table)
    if len(tables) == 1:
        raise AgentError("too_broad", "That is the only table; dropping it would leave no data.")
    del tables[name]


EXECUTORS = {
    "set_value": set_value, "replace_values": replace_values, "replace_text": replace_text, "scale": scale,
    "add_column": add_column, "rename_column": rename_column, "remove_rows": remove_rows, "split_column": split_column,
    "classify": classify, "unpivot": unpivot, "pivot": pivot, "split_rows": split_rows,
    "allocate_by_share": allocate_by_share, "aggregate": aggregate, "bucket_others": bucket_others,
    "net_next_period": net_next_period, "append_rows": append_rows, "lookup_join": lookup_join,
    "convert_currency": convert_currency, "sort_rows": sort_rows, "make_date": make_date, "make_period": make_period,
    "drop_columns": drop_columns, "drop_table": drop_table,
}


def apply_op(tables: Tables, op) -> dict:
    """Run one operation on `tables` (in place). Returns its effect; raises AgentError when it cannot run."""
    fn = EXECUTORS.get(op.op)
    if fn is None:
        raise AgentError("not_in_phase", "Review items are answered in the checking phases (B), not in Transformations.")
    fx = Effect(tables)
    try:
        fn(tables, op, fx)
    except AgentError:
        raise
    except (pl.exceptions.PolarsError, ValueError, TypeError, InvalidOperation) as e:
        raise AgentError("transform_failed", f"{op.op.replace('_', ' ')} could not run: {str(e)[:300]}") from e
    return fx.finish(tables)
