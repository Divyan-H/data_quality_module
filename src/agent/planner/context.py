"""What a planner may know about the data: column names, a few top values per column, and the open review items
grouped by rule and column. Computed by code from the loaded tables, so every number a reply shows is real.
Cell text reaches a model only as JSON string values inside this context, never as instructions."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from src.hygiene.config import RULE_TEXT
from src.hygiene.table import Table

from ..selectors import find_column, label, text_key

TOP_VALUES = 6
MAX_GROUP_EXAMPLES = 3

# Words analysts use for review items, mapped to the rules that raise them.
RULE_WORDS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("date order", "sort"), ("D-31",)),
    (("date", "dates"), ("D-01", "D-02", "D-03", "D-25")),
    (("outlier", "outliers", "spike", "spikes", "extreme"), ("D-14",)),
    (("city", "cities", "city name", "city names", "city spelling", "city spellings"), ("D-30",)),
    (("multi value", "multi-value", "multivalue", "multiple value", "multiple values", "multi city", "multi-city", "list"), ("D-29",)),
    (("near duplicate", "near-duplicate", "near duplicates", "similar name", "similar names", "name variant",
      "name variants", "merge", "merges", "merged name", "merged names", "spelling", "spellings"), ("D-12",)),
    (("duplicate", "duplicates", "duplicate row", "duplicate rows"), ("D-19",)),
    (("unit", "units"), ("D-22",)),
    (("negative", "negatives", "negative value", "negative values", "sign"), ("D-15",)),
    (("placeholder", "placeholders", "na", "n/a", "null token", "null tokens"), ("D-18",)),
    (("case", "case variant", "case variants", "capitalisation", "capitalization"), ("D-10",)),
    (("mixed type", "mixed types", "text in number", "non-number", "non-numeric"), ("D-04",)),
    (("formula", "formulas"), ("D-05", "D-06")),
    (("error", "errors", "#ref", "#n/a", "excel error"), ("D-07", "D-08")),
    (("space", "spaces", "whitespace"), ("D-09",)),
    (("currency", "currencies"), ("D-33", "E-01")),
    (("column name", "column names", "header", "headers", "cryptic name", "cryptic names"), ("D-16", "D-11")),
    (("missing column", "missing columns", "expected column", "expected columns"), ("D-32",)),
    (("truncated", "truncation"), ("D-23",)),
    (("precision", "long number", "long numbers"), ("D-21",)),
    (("gap", "gaps", "zero fill", "isolated zero", "isolated zeros"), ("D-27",)),
    (("sum column", "sum columns", "additive", "total column", "total columns"), ("D-28",)),
]


@dataclass
class FindingGroup:
    rule: str
    column: str | None
    count: int
    cells: int
    examples: list[str] = field(default_factory=list)
    tables: set[str] = field(default_factory=set)
    severity: str = "warn"
    why: str = ""

    def as_dict(self) -> dict:
        return {"rule": self.rule, "what": RULE_TEXT.get(self.rule, ""), "column": self.column, "items": self.count,
                "cells": self.cells, "examples": self.examples, "tables": sorted(self.tables), "severity": self.severity,
                "why": self.why}


def rules_for_words(text: str) -> list[str]:
    """Rule codes an analyst's words point at ('the date fixes' -> D-01, D-02, D-03, D-25). Longest phrase first."""
    t = f" {text_key(text)} "
    out: list[str] = []
    pairs = sorted(((w, codes) for words, codes in RULE_WORDS for w in words), key=lambda p: -len(p[0]))
    for w, codes in pairs:
        if f" {w} " in t:
            t = t.replace(f" {w} ", " ")
            out += [c for c in codes if c not in out]
    return out


class PlanningContext:
    def __init__(self, tables: list[Table], phase: str = "B"):
        self.tables = tables
        self.phase = phase                      # "B": value changes only; "T": reshaping too
        self._value_index: dict[str, dict[str, set[str]]] | None = None

    # ── tables ───────────────────────────────────────────────────────────────
    @property
    def table_names(self) -> list[str]:
        return [t.ref for t in self.tables]

    def find_table(self, name: str) -> str | None:
        """A table's name (its ref) from what the analyst calls it: its name, sheet or file, or part of one."""
        from src.hygiene.cities import normalize
        n = normalize(name.strip().strip("'\""))
        if not n:
            return None
        for exact in (True, False):
            hits = [t.ref for t in self.tables
                    if any(x and (normalize(x) == n if exact else n in normalize(x)) for x in (t.ref, t.sheet, t.source_file))]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                return None
        return None

    def columns_of(self, table: str | None) -> list[str]:
        t = next((t for t in self.tables if t.ref == table), None)
        return list(t.cols) if t is not None else self.columns

    # ── columns ──────────────────────────────────────────────────────────────
    @property
    def columns(self) -> list[str]:
        return list(dict.fromkeys(c for t in self.tables for c in t.cols))

    def find_column(self, name: str) -> str | None:
        name = name.strip().strip("'\"").strip()
        if name.lower().startswith("column "):
            name = name[7:]
        if name.lower().endswith(" column"):
            name = name[:-7]
        return find_column(name.strip(), self.columns) if name.strip() else None

    def column_kind(self, col: str) -> str | None:
        return next((t.types.get(col) for t in self.tables if col in t.cols), None)

    # ── values ───────────────────────────────────────────────────────────────
    def _index(self) -> dict[str, dict[str, set[str]]]:
        """text key -> {column -> exact spellings}, over the text-like columns of every table."""
        if self._value_index is None:
            idx: dict[str, dict[str, set[str]]] = {}
            for t in self.tables:
                for c in t.cols:
                    if t.types.get(c) in ("numeric", "date"):
                        continue
                    for v in set(t.data[c]):
                        if v is not None:
                            idx.setdefault(text_key(v), {}).setdefault(c, set()).add(v)
            self._value_index = idx
        return self._value_index

    def columns_with_value(self, value: str) -> list[str]:
        """Columns where some cell's whole value is `value` (case and spaces ignored)."""
        return sorted(self._index().get(text_key(value), {}))

    def columns_containing(self, text: str) -> list[str]:
        k = text_key(text)
        return sorted({c for v, cols in self._index().items() if k in v for c in cols})

    def column_values(self, col: str) -> Counter:
        out: Counter = Counter()
        for t in self.tables:
            if col in t.cols:
                out.update(v for v in t.data[col] if v is not None)
        return out

    # ── review items ─────────────────────────────────────────────────────────
    def open_items(self) -> list[dict]:
        return [r for t in self.tables for r in t.review if r["status"] == "open"]

    def finding_groups(self) -> list[FindingGroup]:
        groups: dict[tuple, FindingGroup] = {}
        for t in self.tables:
            for r in t.review:
                if r["status"] != "open":
                    continue
                key = (r["rule"], r["column"])
                g = groups.get(key)
                if g is None:
                    g = groups[key] = FindingGroup(r["rule"], r["column"], 0, 0, severity=r.get("severity", "warn"),
                                                   why=r.get("why_flagged", ""))
                g.count += 1
                g.cells += r.get("cells") or (1 if r["row_id"] else 0)
                g.tables.add(t.ref)
                ex = r.get("current_value")
                if ex is not None and len(g.examples) < MAX_GROUP_EXAMPLES and str(ex) not in g.examples:
                    g.examples.append(str(ex))
        return sorted(groups.values(), key=lambda g: (-g.count, g.rule, g.column or ""))

    def open_rules(self) -> list[str]:
        return sorted({r["rule"] for r in self.open_items()})

    # ── for the LLM ──────────────────────────────────────────────────────────
    def pack(self, privacy: str = "full") -> dict:
        """Compact, value-light description for a prompt (kept small for a 16k-context local model)."""
        tables = []
        for t in self.tables:
            cols = []
            for c in t.cols:
                entry = {"name": c, "kind": t.types.get(c, "text")}
                if privacy == "full" and t.types.get(c) not in ("numeric",):
                    top = Counter(v for v in t.data[c] if v is not None).most_common(TOP_VALUES)
                    entry["top_values"] = [v for v, _ in top]
                cols.append(entry)
            tables.append({"table": t.ref if self.phase == "T" else label(t), "rows": len(t.data["_row_id"]), "columns": cols})
        groups = [g.as_dict() for g in self.finding_groups()[:40]]
        if privacy != "full":
            for g in groups:
                g.pop("examples", None)
        return {"phase": self.phase, "tables": tables, "open_review_items": groups}
