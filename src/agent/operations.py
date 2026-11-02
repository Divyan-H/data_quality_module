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


Operation = Annotated[Union[ResolveFindings, SetValue, ReplaceValues, ReplaceText, Scale, AddColumn, RenameColumn,
                            RemoveRows], Field(discriminator="op")]
OPERATION_TYPES = (ResolveFindings, SetValue, ReplaceValues, ReplaceText, Scale, AddColumn, RenameColumn, RemoveRows)
OPERATION_NAMES = [t.model_fields["op"].default for t in OPERATION_TYPES]


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
