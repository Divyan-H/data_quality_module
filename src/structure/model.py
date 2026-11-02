"""Shared data model: cells, sheets, A1 helpers (spec P2: Excel A1 coordinates everywhere)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

DATA_CLASSES = frozenset("NDBEFT")
LABEL_CLASSES = frozenset("LP")


def col_letter(c: int) -> str:
    s = ""
    while c > 0:
        c, rem = divmod(c - 1, 26)
        s = chr(65 + rem) + s
    return s


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def a1(r: int, c: int) -> str:
    return f"{col_letter(c)}{r}"


def range_a1(r1: int, c1: int, r2: int, c2: int) -> str:
    return a1(r1, c1) if (r1, c1) == (r2, c2) else f"{a1(r1, c1)}:{a1(r2, c2)}"


_A1 = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")


def parse_ref(ref: str) -> tuple[int, int, int, int]:
    """'B3:AF5003' -> (r1, c1, r2, c2)."""
    parts = ref.replace("$", "").split(":")
    m1 = _A1.match(parts[0])
    m2 = _A1.match(parts[-1])
    if not m1 or not m2:
        raise ValueError(f"bad A1 reference: {ref}")
    return int(m1.group(2)), col_index(m1.group(1)), int(m2.group(2)), col_index(m2.group(1))


@dataclass(slots=True)                # one per populated cell: slots keep a lakh-row sheet out of per-object dicts
class Cell:
    cls: str                       # W D P N E T L B F  (spec 4.2)
    text: Optional[str]            # canonical rendering; None = NULL (uncached formula)
    ctype: str                     # s n date datetime time b e formula_uncached text
    fmt: Optional[str] = None
    formula: bool = False
    uncached: bool = False
    error: bool = False
    style: int = 0                 # index into cellXfs, used for S-18 checks
    out_of_range: bool = False     # a date serial that had to be written as a number
    formula_text: Optional[str] = None   # '=...' of a formula with no stored result or a flagged one (kept for the findings)


@dataclass
class Sheet:
    index: int
    name: str
    key: str
    state: str = "visible"
    cells: dict = field(default_factory=dict)          # (row, col) -> Cell
    merges: list = field(default_factory=list)         # (r1, c1, r2, c2)
    hidden_rows: set = field(default_factory=set)
    hidden_cols: set = field(default_factory=set)
    row_outline: dict = field(default_factory=dict)    # row -> level
    col_outline: dict = field(default_factory=dict)    # col -> level
    freeze_row: int = 0
    freeze_col: int = 0
    protected: bool = False
    declared_dim: Optional[str] = None
    comments: dict = field(default_factory=dict)       # (row, col) -> comment text (data artifact only)
    excel_tables: list = field(default_factory=list)   # dicts: name, ref, header_rows, totals_rows, criteria
    pivots: list = field(default_factory=list)         # dicts: ref, header_row, first_data_col, cache_*
    autofilter: Optional[str] = None
    filter_criteria: bool = False
    protected_password: bool = False
    print_area: Optional[tuple] = None
    cond_formats: list = field(default_factory=list)   # {ranges, always, hides}
    validations: list = field(default_factory=list)    # {ranges, type, operator, f1, f2, allow_blank}
    drawings: list = field(default_factory=list)       # {name, box}
    formula_flags: dict = field(default_factory=dict)  # (row, col) -> {"external" | "broken"}
    hidden_style_cells: set = field(default_factory=set)  # populated cells whose style hides the value
    ragged_short: int = 0
    ragged_long: int = 0
    ragged: dict = field(default_factory=dict)         # CSV record -> (fields, expected fields)
    error: Optional[str] = None
    virtual: bool = False                              # the grid of a CSV/TSV/TXT file, not a real sheet

    def used_range(self) -> Optional[tuple[int, int, int, int]]:
        if not self.cells:
            return None
        rs = [k[0] for k in self.cells]
        cs = [k[1] for k in self.cells]
        return min(rs), min(cs), max(rs), max(cs)


@dataclass
class Workbook:
    sheets: list
    macro_enabled: bool = False
    external_links: int = 0
    names: list = field(default_factory=list)          # {name, formula, sheet_index | None}
    calc_on_load: bool = False
    structure_locked: bool = False
