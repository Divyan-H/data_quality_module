"""What the planner needs to know about a table, whichever phase it comes from.

Phase B hands the agent `hygiene.table.Table` objects; reshaping hands it `transform.workspace.FrameTable`. Both fit
this shape, so the planning context, the parser and the model prompt are written against it and nothing else.
"""
from __future__ import annotations

from typing import Protocol


class PlanTable(Protocol):
    ref: str                       # the table's stable name (Phase A's table ref, or the sheet name while reshaping)
    sheet: str | None
    source_file: str | None
    cols: list[str]                # data columns, in order (no system columns)
    types: dict[str, str]          # column -> "numeric" | "date" | "text"
    data: dict[str, list]          # column -> values as text (plus "_row_id")
    review: list[dict]             # open review items (empty while reshaping)
