"""The contract: every change the agent can make, as a typed, validated model.

A plan is a list of these. Both planners (the command parser and the LLM) must produce them, and nothing else can reach
the data: an operation that is not listed here cannot be expressed, and every field is validated before a preview is
built. Adding a capability means adding a model here and an executor in `executors.py`.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ConditionOp = Literal["eq", "ne", "in", "not_in", "contains", "starts_with", "ends_with", "empty", "not_empty",
                      "gt", "gte", "lt", "lte", "matches"]
Decision = Literal["accept", "reject", "keep", "set", "split"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


class Condition(_Model):
    """One test on a column's value. Text tests ignore case and surrounding spaces; gt/lt compare numbers."""
    column: str
    op: ConditionOp = "eq"
    value: str | float | int | list[str | float | int] | None = None

    @model_validator(mode="after")
    def _value_fits_op(self):
        if self.op in ("empty", "not_empty"):
            self.value = None
        elif self.value is None:
            raise ValueError(f"condition '{self.op}' on {self.column} needs a value")
        elif self.op in ("in", "not_in") and not isinstance(self.value, list):
            self.value = [self.value]
        return self


class RowFilter(_Model):
    """Which rows a change touches. Every part given must hold (AND); an empty filter means every row.
    `tables` matches a table ref, file name or sheet name (case-insensitive, part of the name is enough).
    `rows` and `exclude_rows` are Excel row numbers, as the analyst sees them in the workbook."""
    tables: list[str] | None = None
    rows: list[int] | None = None
    exclude_rows: list[int] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)

    def is_everything(self) -> bool:
        return not (self.tables or self.rows or self.exclude_rows or self.conditions)


# ── operations ──────────────────────────────────────────────────────────────
class ResolveFindings(_Model):
    """Answer open review items in bulk: the chat form of the Needs_Review Decision column.
    accept = take the proposed / automatic value; reject = restore the original value; keep = leave the value as it
    is and close the item; set = write `new_value`; split = one row per value (D-29 items only)."""
    op: Literal["resolve_findings"] = "resolve_findings"
    decision: Decision
    rules: list[str] | None = None            # rule codes, e.g. ["D-02"]; None = any rule
    column: str | None = None
    value: str | None = None                  # only items whose current value is this text
    new_value: str | None = None
    where: RowFilter = Field(default_factory=RowFilter)

    @field_validator("rules")
    @classmethod
    def _codes(cls, v):
        return [c.strip().upper() for c in v] if v else v

    @model_validator(mode="after")
    def _set_needs_value(self):
        if self.decision == "set" and self.new_value is None:
            raise ValueError("decision 'set' needs new_value")
        return self


class SetValue(_Model):
    """Write one value into a column for every row the filter selects (None = make the cells empty)."""
    op: Literal["set_value"] = "set_value"
    column: str
    value: str | None
    where: RowFilter = Field(default_factory=RowFilter)


class ReplaceValues(_Model):
    """Map whole cell values to new ones: 'Head Office' -> 'Bangalore', several spellings -> one bucket (D-26), or a
    value -> None (empty). `columns` None means every text column that contains one of the values."""
    op: Literal["replace_values"] = "replace_values"
    mapping: dict[str, str | None]
    columns: list[str] | None = None
    ignore_case: bool = True
    where: RowFilter = Field(default_factory=RowFilter)

    @field_validator("mapping")
    @classmethod
    def _non_empty(cls, v):
        if not v:
            raise ValueError("mapping needs at least one value")
        return v


class ReplaceText(_Model):
    """Replace part of the text inside cells (plain text, or a regular expression when `regex` is true)."""
    op: Literal["replace_text"] = "replace_text"
    column: str
    find: str
    replace: str = ""
    regex: bool = False
    ignore_case: bool = True
    where: RowFilter = Field(default_factory=RowFilter)

    @field_validator("find")
    @classmethod
    def _find(cls, v):
        if v == "":
            raise ValueError("find must not be empty")
        return v


class Scale(_Model):
    """Multiply numeric cells by a factor ('this file is in thousands' = 1000; divide = 1 / n)."""
    op: Literal["scale"] = "scale"
    columns: list[str]
    factor: float
    where: RowFilter = Field(default_factory=RowFilter)

    @field_validator("factor")
    @classmethod
    def _factor(cls, v):
        if v == 0:
            raise ValueError("factor must not be 0")
        return v


class AddColumn(_Model):
    """Add a column. Exactly one source: a constant `value`, a `mapping` of another column's values (a lookup), or a
    `formula` over columns and numbers (+ - * / and brackets, e.g. 'Orders * AOV')."""
    op: Literal["add_column"] = "add_column"
    name: str
    value: str | None = None
    from_column: str | None = None
    mapping: dict[str, str | None] | None = None
    default: str | None = None                # for a mapping: the value when the key is not listed (None = empty)
    formula: str | None = None
    tables: list[str] | None = None

    @model_validator(mode="after")
    def _one_source(self):
        n = sum(x is not None for x in (self.value, self.mapping, self.formula))
        if n != 1:
            raise ValueError("add_column needs exactly one of value, mapping or formula")
        if self.mapping is not None and not self.from_column:
            raise ValueError("a mapping needs from_column")
        if not self.name.strip() or self.name.strip().startswith("_"):
            raise ValueError("the column name must not be empty or start with '_'")
        return self


class RenameColumn(_Model):
    op: Literal["rename_column"] = "rename_column"
    column: str
    new_name: str
    tables: list[str] | None = None

    @field_validator("new_name")
    @classmethod
    def _name(cls, v):
        v = v.strip()
        if not v or v.startswith("_"):
            raise ValueError("the new name must not be empty or start with '_'")
        return v


class RemoveRows(_Model):
    """Remove the rows the filter selects. Each removed row keeps an audit entry with its values."""
    op: Literal["remove_rows"] = "remove_rows"
    where: RowFilter

    @model_validator(mode="after")
    def _needs_a_filter(self):
        if not (self.where.rows or self.where.conditions):
            raise ValueError("remove_rows needs rows or conditions; it never removes every row")
        return self


class SplitColumn(_Model):
    """Split one column's text into new columns: by a separator ('Full Name' on ' ' -> First, Last), or by a regular
    expression whose groups give the parts. Missing parts are empty; with a separator the last part keeps the rest."""
    op: Literal["split_column"] = "split_column"
    column: str
    into: list[str]
    separator: str | None = None
    pattern: str | None = None
    keep_original: bool = True
    tables: list[str] | None = None

    @model_validator(mode="after")
    def _how(self):
        if (self.separator is None) == (self.pattern is None):
            raise ValueError("split_column needs exactly one of separator or pattern")
        if len(self.into) < 2 or any(not n.strip() or n.strip().startswith("_") for n in self.into):
            raise ValueError("split_column needs at least two new column names (not starting with '_')")
        return self


class Rule(_Model):
    """One branch of a classification: every condition must hold."""
    when: list[Condition]
    value: str | None


class Classify(_Model):
    """If / else: write the first matching rule's value into `column` (a new or an existing column).
    'Tier = Gold if Spend > 100000, Silver if Spend > 50000, else Bronze'. Rows no rule matches get `default`."""
    op: Literal["classify"] = "classify"
    column: str
    rules: list[Rule]
    default: str | None = None
    keep_existing: bool = False        # an existing column: keep its value where no rule matches (instead of default)
    tables: list[str] | None = None

    @field_validator("rules")
    @classmethod
    def _rules(cls, v):
        if not v:
            raise ValueError("classify needs at least one rule")
        return v


# ── transformations (Phase T): these change a table's shape ─────────────────
class _TableOp(_Model):
    table: str | None = None           # the table to change; None = the only table


class Unpivot(_TableOp):
    """Wide to long: value columns become rows (Day 1 ... Day 31 -> one row per day). `columns` lists them, or
    `pattern` (a regular expression) picks them; every other column is repeated on each new row."""
    op: Literal["unpivot"] = "unpivot"
    columns: list[str] | None = None
    pattern: str | None = None
    names_to: str = "Variable"
    values_to: str = "Value"
    drop_empty: bool = True

    @model_validator(mode="after")
    def _which(self):
        if not self.columns and not self.pattern:
            raise ValueError("unpivot needs columns or a pattern")
        return self


class Pivot(_TableOp):
    """Long to wide: each distinct value of `columns_from` becomes a column holding `values_from` (aggregated)."""
    op: Literal["pivot"] = "pivot"
    index: list[str]
    columns_from: str
    values_from: str
    agg: Literal["sum", "first", "mean", "count"] = "sum"


class SplitRows(_TableOp):
    """One row per item of a list cell ('Mumbai, Pune' -> two rows). Number columns in `allocate` are shared out:
    equal_integer keeps the total exactly (10 over 3 -> 4, 3, 3), equal divides, repeat copies."""
    op: Literal["split_rows"] = "split_rows"
    column: str
    separators: list[str] = Field(default_factory=lambda: [",", ";", "/", "|", "&", " and "])
    keep_together: list[str] = Field(default_factory=list)   # names that contain a separator but are one item
    allocate: list[str] = Field(default_factory=list)
    method: Literal["equal_integer", "equal", "repeat"] = "equal_integer"


class AllocateByShare(_TableOp):
    """Share each row's numbers across the matching rows of a share table (city audience %, city split rows).
    Shares are read as percentages when they add up to more than 1.5 per key; no share column = equal parts.
    Rows with no match keep their numbers under `unmatched_label` in the category column."""
    op: Literal["allocate_by_share"] = "allocate_by_share"
    share_table: str
    on: dict[str, str]                 # this table's column -> the share table's column
    category: str                      # the share table's column the rows are split by (e.g. City)
    share: str | None = None           # the share table's share column (None = equal split)
    columns: list[str]
    integer: bool = False
    unmatched_label: str | None = None
    keep_unmatched_shares: bool = False   # share rows with no matching row are kept with zero numbers (affiliate rule)


class Aggregate(_TableOp):
    """Group rows and add up: 'sum Impressions, Clicks by Day, Channel, Network'. Non-numbers count as 0 when
    `blank_as_zero` (the affiliate rule); `mean` averages; `count_as` adds a row count."""
    op: Literal["aggregate"] = "aggregate"
    by: list[str]
    sum: list[str] = Field(default_factory=list)
    mean: list[str] = Field(default_factory=list)
    first: list[str] = Field(default_factory=list)
    count_as: str | None = None
    blank_as_zero: bool = True

    @model_validator(mode="after")
    def _something(self):
        if not (self.sum or self.mean or self.first or self.count_as):
            raise ValueError("aggregate needs columns to sum, average, keep, or a count")
        return self


class BucketOthers(_TableOp):
    """Relabel values not on a keep list (or outside the top N) as 'Others'; optionally add them up per `by`."""
    op: Literal["bucket_others"] = "bucket_others"
    column: str
    keep: list[str] | None = None
    keep_from: dict[str, str] | None = None     # {"table": ..., "column": ...}: the keep list from a reference table
    top_n: int | None = None
    label: str = "Others"
    also: list[str] = Field(default_factory=list)   # other columns set to the label on those rows (e.g. Network)
    sum_by: list[str] | None = None                 # then add the numbers up per these columns + the label
    sum: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _which(self):
        if self.keep is None and self.keep_from is None and self.top_n is None:
            raise ValueError("bucket_others needs keep, keep_from or top_n")
        return self


class NetNextPeriod(_TableOp):
    """OOH returns: a negative value in the exact next month is the return of this month's allocation.
    Adds Allocation, Next Month Return and Final Spend columns (per `group_by`); see docs for the full rule."""
    op: Literal["net_next_period"] = "net_next_period"
    value: str
    period: str                        # a date (or month) column; months are compared by calendar month
    group_by: list[str] = Field(default_factory=list)
    output: str = "Final Spend"


class AppendRows(_TableOp):
    """Stack another table under this one (DAP history + new data; two yearly sheets). Columns are matched by name
    (after `rename`, source -> target); missing ones are empty. `where` keeps only some source rows."""
    op: Literal["append_rows"] = "append_rows"
    source: str
    rename: dict[str, str] = Field(default_factory=dict)
    where: list[Condition] = Field(default_factory=list)
    set: dict[str, str | None] = Field(default_factory=dict)   # constant values for the appended rows
    drop_source: bool = True


class LookupJoin(_TableOp):
    """Bring columns in from a lookup table where keys match (AOV by Date + City + Vehicle + Segment). Duplicate
    lookup keys are averaged (numbers) or the first is kept; text keys match ignoring case and spaces."""
    op: Literal["lookup_join"] = "lookup_join"
    lookup: str
    on: dict[str, str]                 # this table's column -> the lookup table's column
    columns: list[str]
    duplicates: Literal["mean", "first"] = "mean"


class ConvertCurrency(_TableOp):
    """Multiply money columns by an exchange rate: one `rate`, or `rates` by date range (quarterly USD -> INR)."""
    op: Literal["convert_currency"] = "convert_currency"
    columns: list[str]
    rate: float | None = None
    rates: list[dict] | None = None    # [{"from": "2025-01-01", "to": "2025-03-31", "rate": 83.1}, ...]
    date_column: str | None = None
    note: str | None = None            # e.g. "USD -> INR"

    @model_validator(mode="after")
    def _rate(self):
        if (self.rate is None) == (self.rates is None):
            raise ValueError("convert_currency needs exactly one of rate or rates")
        if self.rates is not None and not self.date_column:
            raise ValueError("rates by date need date_column")
        return self


class SortRows(_TableOp):
    op: Literal["sort_rows"] = "sort_rows"
    by: list[str]
    descending: list[bool] = Field(default_factory=list)


class MakeDate(_TableOp):
    """A date from a month column and a day column ('Jan-25' + 'Day 12' -> 2025-01-12). Impossible dates (30 Feb)
    leave the cell empty; such rows can be removed afterwards."""
    op: Literal["make_date"] = "make_date"
    month: str
    day: str
    name: str = "Date"


class MakePeriod(_TableOp):
    """The month / quarter / year / week a date falls in, as its first day (2025-10-17 -> 2025-10-01)."""
    op: Literal["make_period"] = "make_period"
    column: str
    unit: Literal["month", "quarter", "year", "week"] = "month"
    name: str = "Month"


class DropColumns(_TableOp):
    op: Literal["drop_columns"] = "drop_columns"
    columns: list[str]


class DropTable(_TableOp):
    """Leave a table out of the final data (e.g. a lookup sheet once it has been joined)."""
    op: Literal["drop_table"] = "drop_table"


VALUE_TYPES = (ResolveFindings, SetValue, ReplaceValues, ReplaceText, Scale, AddColumn, RenameColumn, RemoveRows,
               SplitColumn, Classify)
SHAPE_TYPES = (Unpivot, Pivot, SplitRows, AllocateByShare, Aggregate, BucketOthers, NetNextPeriod, AppendRows,
               LookupJoin, ConvertCurrency, SortRows, MakeDate, MakePeriod, DropColumns, DropTable)
OPERATION_TYPES = VALUE_TYPES + SHAPE_TYPES
Operation = Annotated[Union[OPERATION_TYPES], Field(discriminator="op")]
OPERATION_NAMES = [t.model_fields["op"].default for t in OPERATION_TYPES]
VALUE_OPS = {t.model_fields["op"].default for t in VALUE_TYPES}
SHAPE_OPS = {t.model_fields["op"].default for t in SHAPE_TYPES}


class Plan(_Model):
    """What a planner returns. Exactly one of: `ops` (a change to preview), `ask` (one question back), or `answer`
    (a reply that changes nothing, e.g. help or a summary; numbers in it come from code)."""
    ops: list[Operation] = Field(default_factory=list)
    ask: str | None = None
    answer: str | None = None
    source: Literal["rules", "llm", "none"] = "rules"

    @model_validator(mode="after")
    def _one_kind(self):
        if sum(bool(x) for x in (self.ops, self.ask, self.answer)) != 1:
            raise ValueError("a plan has exactly one of ops, ask or answer")
        return self
