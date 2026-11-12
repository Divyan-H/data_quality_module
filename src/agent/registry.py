"""The tool registry: the one place that says what the agent can do.

Each `Tool` ties together everything that used to be spread over five files: the typed input model
(`operations.py`), the phases it may run in, how risky it is, whether it takes a row filter, and the sentence the
local model reads about it. From this registry the planner builds the model's prompt and JSON schema, the
guardrails decide which changes need a second confirmation, the help text lists what is possible, and
`mcp_tools()` exports the same definitions in Model Context Protocol shape (name, description, inputSchema), so an
MCP adapter can be added later without describing the tools a second time.

Adding an operation = its pydantic model in `operations.py`, its executor in `executors.py` and/or
`transform/engine.py`, and one `Tool` here. `tests/test_agent_registry.py` fails if the three disagree.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from . import operations as ops

Risk = Literal["low", "medium", "high"]
PHASES = ("B", "T")


@dataclass(frozen=True)
class Tool:
    name: str
    model: type[BaseModel]
    phases: frozenset[str]            # where it may run: "B" (value fixes on checked data), "T" (transformations)
    risk: Risk                        # low: adds or annotates; medium: rewrites values; high: removes rows/columns/tables
    row_filter: bool                  # takes `where` (tables / rows / exclude_rows / conditions)
    summary: str                      # what the local model is told about it (also the MCP description)

    @property
    def fields(self) -> set[str]:
        return set(self.model.model_fields)


def _tool(model, phases: str, risk: Risk, summary: str, row_filter: bool = False) -> Tool:
    return Tool(model.model_fields["op"].default, model, frozenset(phases), risk, row_filter, " ".join(summary.split()))


_ALL = [
    _tool(ops.ResolveFindings, "B", "medium", """answer open review items. decision = accept (take the proposed fix) |
          reject (restore the original value) | keep (leave the value, close the item) | set (write new_value) |
          split (one row per value, D-29 only). Narrow with rules (codes like "D-02"), column, value, rows (Excel row
          numbers), exclude_rows, conditions.""", row_filter=True),
    _tool(ops.SetValue, "BT", "medium", """write `value` into `column` for the rows chosen by rows / exclude_rows /
          conditions (value null = empty).""", row_filter=True),
    _tool(ops.ReplaceValues, "BT", "medium", """`mapping` of whole cell values to new values (null = empty) in
          `columns`.""", row_filter=True),
    _tool(ops.ReplaceText, "BT", "medium", """replace the text `find` with `replace` inside cells of `column`
          (regex=true for a pattern).""", row_filter=True),
    _tool(ops.Scale, "BT", "medium", """multiply `columns` by `factor` (thousands = 1000; to divide by 100 use 0.01).""",
          row_filter=True),
    _tool(ops.AddColumn, "BT", "low", """new column `name` with either `value` (same on every row), or `from_column` +
          `mapping` (+ `default`), or `formula` over columns and numbers with + - * / and brackets."""),
    _tool(ops.RenameColumn, "BT", "low", "`column` -> `new_name`."),
    _tool(ops.RemoveRows, "BT", "high", "remove rows chosen by rows / conditions (never every row).", row_filter=True),
    _tool(ops.SplitColumn, "BT", "low", """split `column` into the new columns `into` on `separator` (or regex
          `pattern` with groups)."""),
    _tool(ops.Classify, "BT", "low", """if / else into `column`: `rules` = [{"when": [conditions], "value": ...}], first
          match wins; `default`."""),
    _tool(ops.Unpivot, "T", "medium", """`columns` (or regex `pattern`) become rows; names into `names_to`, values into
          `values_to`."""),
    _tool(ops.Pivot, "T", "medium", """values of `columns_from` become columns holding `values_from` (`agg`
          sum|mean|first|count), one row per `index`."""),
    _tool(ops.SplitRows, "T", "medium", """one row per item of the list in `column`; `allocate` number columns
          (`method` equal_integer|equal|repeat)."""),
    _tool(ops.AllocateByShare, "T", "medium", """share `columns` across `category` rows of `share_table` (`share`
          column or equal), keys `on`."""),
    _tool(ops.Aggregate, "T", "high", "group `by`, `sum` / `mean` / `first` columns, `count_as` a row count."),
    _tool(ops.BucketOthers, "T", "medium", """`column` values not in `keep` (or `keep_from` {table, column}, or outside
          `top_n`) become `label`."""),
    _tool(ops.NetNextPeriod, "T", "medium", """`value` per `group_by` and month of `period`: next month's negative nets
          this month's allocation."""),
    _tool(ops.AppendRows, "T", "medium", """rows of `source` under `table` (`rename` source->target columns, `where`
          conditions on the source)."""),
    _tool(ops.LookupJoin, "T", "low", """bring `columns` from `lookup` where keys `on` (this table's column -> lookup's
          column) match."""),
    _tool(ops.ConvertCurrency, "T", "medium", """`columns` x `rate`, or `rates` [{"from","to","rate"}] by
          `date_column`."""),
    _tool(ops.SortRows, "T", "low", "sort `by` (+ `descending`)."),
    _tool(ops.MakeDate, "T", "low", "a date column `name` from `month` and `day` columns."),
    _tool(ops.MakePeriod, "T", "low", "column `name` = the `unit` (month|quarter|year|week) that `column`'s date falls in."),
    _tool(ops.DropColumns, "T", "high", "leave `columns` out of the final data."),
    _tool(ops.DropTable, "T", "high", "leave `table` out of the final data."),
]
TOOLS: dict[str, Tool] = {t.name: t for t in _ALL}


def tools_for(phase: str) -> list[Tool]:
    """The tools a phase offers, in catalogue order."""
    return [t for t in TOOLS.values() if phase in t.phases]


def names_for(phase: str) -> list[str]:
    return [t.name for t in tools_for(phase)]


def get(name: str) -> Tool:
    return TOOLS[name]


def prompt_catalogue(phase: str) -> str:
    """The operation list of the model's system prompt: only what this phase can run."""
    return "\n".join(f"- {t.name}: {t.summary}" for t in tools_for(phase))


def mcp_tools(phase: str | None = None) -> list[dict]:
    """The registry in Model Context Protocol shape. Calling one of these through any adapter must create a
    *proposal* (a previewed change-set), never an applied change: applying stays a person's click."""
    chosen = TOOLS.values() if phase is None else tools_for(phase)
    return [{"name": f"dq_propose_{t.name}", "description": t.summary, "inputSchema": t.model.model_json_schema(),
             "annotations": {"risk": t.risk, "phases": sorted(t.phases), "destructiveHint": t.risk == "high",
                             "readOnlyHint": False}} for t in chosen]
