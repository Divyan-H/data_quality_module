"""One table being cleaned: values as text lists, plus the audit and review records the rules write to."""
from __future__ import annotations

from src.common.columns import SYSTEM
from collections import Counter, defaultdict

from .values import has_keyword, number_convention, parse_date, parse_number, slash_dayfirst

# Findings that need an answer from the client rather than an analyst decision (they feed the client report).
CLIENT_RULES = {"D-06", "D-15", "D-16", "D-21", "D-22", "D-23", "D-29", "D-30", "D-32"}


def sample(vals: list, n: int = 2000) -> list:
    """Evenly spaced subset: column-type decisions do not need every row."""
    return vals if len(vals) <= n else vals[:: len(vals) // n + 1]


class Held:
    """Cells a rule has flagged and left exactly as written; no later rule changes or reads them.
    `cells` are single cells (column, row index); `values` hold every cell of a column that has that exact text
    (one city spelling flagged once for the whole column, however many cells carry it)."""

    def __init__(self, table):
        self._t, self.cells, self.values = table, set(), set()

    def add(self, key: tuple[str, int]) -> None:
        self.cells.add(key)

    def add_value(self, col: str, value: str) -> None:
        self.values.add((col, value))

    def __contains__(self, key) -> bool:
        col, i = key
        return key in self.cells or (bool(self.values) and (col, self._t.data[col][i]) in self.values)

    def __len__(self) -> int:
        return len(self.cells)

    def __bool__(self) -> bool:
        return bool(self.cells or self.values)

    def clear(self) -> None:
        self.cells.clear()
        self.values.clear()

    def reindex(self, keep: list[int]) -> None:
        """Rows were removed: `keep` lists the old index of every row that is left, in order."""
        new = {old: n for n, old in enumerate(keep)}
        self.cells = {(c, new[i]) for c, i in self.cells if i in new}


class Table:
    def __init__(self, ref: str, data: dict[str, list], cfg: dict, layout_id: str | None = None,
                 source_file: str | None = None, sheet: str | None = None):
        self.ref, self.cfg, self.layout_id = ref, cfg, layout_id
        self.source_file, self.sheet = source_file, sheet          # where the table came from (for reports)
        self.data = data                                    # column name -> list[str | None]
        self.cols = [c for c in data if c not in SYSTEM]    # data columns only; system columns ride along
        self.rows_in = len(data["_row_id"])
        self.annotations: list[tuple[int, str, str, str | None]] = []   # (row index, column, kind, cached text)
        self.audit: list[dict] = []
        self.review: list[dict] = []
        self.counts: dict[str, Counter] = defaultdict(Counter)
        self.types: dict[str, str] = {}                     # column -> kind, set when the run finishes
        self.text_cols: set[str] = set()                    # columns D-21 keeps as text (never cast to a number)
        self.held = Held(self)                              # cells D-29 / D-30 flagged: no later rule changes or reads them
        self.column_choice: dict | None = None              # when only some columns are checked: which, which not, which were not found
        self.context_cols: list[str] | None = None          # when only some columns are checked: all columns, for rules that split by label

    # ── writing ──────────────────────────────────────────────────────────────
    def _entry(self, code, col, i, **kw):
        return {"table_ref": self.ref, "row_id": None if i is None else self.data["_row_id"][i],
                "source_row": None if i is None else self.data["_source_row"][i], "column": col, "rule": code, **kw}

    def set(self, col: str, i: int, new, code: str, why: str = "", review: bool = False) -> None:
        """Change one cell and audit it. A rule switched off in `fixes` flags the proposed change instead.
        `review=True` also queues the change for a human (e.g. a value nulled because it could not be converted)."""
        old = self.data[col][i]
        if new == old:
            return
        if self.cfg["fixes"].get(code, True):
            self.data[col][i] = new
            self.audit.append(self._entry(code, col, i, action="fixed", old_value=old, new_value=new, note=why))
            self.counts[code]["fixed"] += 1
            if review:
                self.flag(code, col, i, why, "Accept keeps the fix; Reject restores the original value; "
                          "New Value supplies a replacement.", current=new, original=old)
        else:
            self.flag(code, col, i, why or f"{code} would change this value", f"Accept to apply: {new!r}",
                      current=old, proposed=new)

    def log(self, code: str, col: str, i: int, old, new) -> None:
        """Audit a change that was already made upstream (e.g. Phase A nulled an uncached formula)."""
        self.audit.append(self._entry(code, col, i, action="fixed", old_value=old, new_value=new))
        self.counts[code]["fixed"] += 1

    def flag(self, code: str, col: str | None, i: int | None, why: str, suggest: str = "", sev: str = "warn",
             current=None, original=None, proposed=None, parts: list | None = None, scope: str | None = None,
             cells: int | None = None) -> None:
        """Queue one issue for review; the value is left as it is. `scope="value"` is one item for every cell of the column
        that has this exact text (`cells` of them); `i` is the first of them."""
        if current is None and i is not None and col in self.data:
            current = self.data[col][i]
        self.review.append(self._entry(
            code, col, i, scope=scope or ("cell" if i is not None else "column"), current_value=current,
            original_value=original, proposed_value=proposed, why_flagged=why, suggested_action=suggest,
            parts=parts, cells=cells, severity=sev, client_report=code in CLIENT_RULES, status="open"))
        self.counts[code]["flagged"] += 1

    def drop_rows(self, drop: dict[int, int], code: str) -> None:
        """Remove rows {index: index of the row kept}; each gets a tombstone naming the surviving Row_ID."""
        for i, kept in drop.items():
            self.audit.append(self._entry(code, None, i, action="dropped", old_value=self.data["_row_id"][kept],
                                          new_value=None))
            self.counts[code]["fixed"] += 1
        keep = [i for i in range(len(self.data["_row_id"])) if i not in drop]
        self.held.reindex(keep)                                       # held cells follow their rows
        for c in self.data:
            self.data[c] = [self.data[c][i] for i in keep]

    def rename(self, old: str, new: str, code: str) -> None:
        """Rename a column (keeps its position) and update earlier audit/review entries so they name the new column."""
        if not self.cfg["fixes"].get(code, True):
            self.flag(code, old, None, f"Column '{old}' would be renamed to '{new}'.", "Accept to rename.", proposed=new)
            return
        self.data = {(new if c == old else c): v for c, v in self.data.items()}
        self.cols = [new if c == old else c for c in self.cols]
        for e in self.audit + self.review:
            if e["column"] == old:
                e["column"] = new
        self.audit.append(self._entry(code, new, None, action="renamed", old_value=old, new_value=new))
        self.counts[code]["fixed"] += 1

    def hold_open_multi_values(self) -> None:
        """After a reload: what still-open D-29 and D-30 items flagged stays held (rebuilt from the review items)."""
        index = {rid: i for i, rid in enumerate(self.data["_row_id"])}
        for r in self.review:
            if r["status"] != "open" or r["column"] not in self.data:
                continue
            if r["rule"] == "D-29" and r["row_id"] in index:
                self.held.add((r["column"], index[r["row_id"]]))
            elif r["rule"] == "D-30" and r.get("scope") == "value":
                self.held.add_value(r["column"], r["current_value"])

    # ── reading ──────────────────────────────────────────────────────────────
    def values(self, col: str) -> list[str]:
        return [v for v in self.data[col] if v is not None]

    def dayfirst(self, col: str) -> bool:
        return slash_dayfirst(self.values(col), self.cfg["dayfirst"])

    def date_args(self, col: str) -> tuple:
        return self.dayfirst(col), self.cfg["min_year"], self.cfg["max_year"]

    def eu(self, col: str) -> bool:
        return number_convention(self.values(col), self.cfg["currency_symbols"])

    def kind(self, col: str) -> str:
        """empty | id | date | numeric | text. Recomputed from the current values every time (nothing is cached)."""
        c = self.cfg
        vals = sample(self.values(col))
        if not vals:
            return "empty"
        if has_keyword(col, c["id_keywords"]):
            return "id"
        args = self.date_args(col)
        if sum(parse_date(v, *args)[0] != "none" for v in vals) / len(vals) >= 0.5:
            return "date"
        lo, hi = c["serial_range"]
        if (has_keyword(col, c["date_keywords"]) and not has_keyword(col, c["numeric_keywords"])
                and sum(v.isdigit() and lo <= int(v) <= hi for v in vals) / len(vals) >= 0.8):
            return "date"                                   # a column of Excel date serials
        # numeric only when numbers clearly dominate; a half-and-half column is text (coercing is the costlier mistake)
        return "numeric" if self.numeric_share(col, vals) >= c["numeric_min_share"] else "text"

    def numeric_share(self, col: str, vals: list[str] | None = None) -> float:
        vals = sample(self.values(col)) if vals is None else vals
        eu, sym = self.eu(col), self.cfg["currency_symbols"]
        return sum(parse_number(v, sym, eu) is not None for v in vals) / len(vals) if vals else 0.0

    def numbers(self, col: str) -> list[tuple[int, float]]:
        """(row index, value) for every cell that parses as a number (cells held by D-29 are not numbers yet)."""
        eu, sym = self.eu(col), self.cfg["currency_symbols"]
        out = []
        for i, v in enumerate(self.data[col]):
            n = parse_number(v, sym, eu) if v is not None and (col, i) not in self.held else None
            if n is not None:
                out.append((i, float(n)))
        return out
