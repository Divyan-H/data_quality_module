"""One editing pass over a phase's loaded tables.

Every executor writes through an `EditSession`, never into a table directly. The session does two jobs:
  1. it makes the change and writes the audit entry exactly as the review-workbook path does (same fields, same
     `analyst_edit` action, rule `agent`, plus the change-set id), and
  2. it records what each operation did (counts, before/after samples, values it could not match), which is the
     preview the analyst approves.
The same plan runs twice: once on a throw-away load for the preview, once on a fresh load to apply. Both runs are
deterministic, so what is applied is what was shown (the base version check in the workspace guards the rest).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from src.hygiene.table import Table

AGENT_RULE = "agent"
SAMPLE_LIMIT = 8


@dataclass
class OpEffect:
    """What one operation did."""
    cells: int = 0
    items_resolved: int = 0
    rows_removed: int = 0
    rows_added: int = 0
    columns_added: list[str] = field(default_factory=list)
    columns_renamed: list[list[str]] = field(default_factory=list)
    tables: set[str] = field(default_factory=set)
    samples: list[dict] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def touched(self) -> bool:
        return bool(self.cells or self.items_resolved or self.rows_removed or self.rows_added or self.columns_added
                    or self.columns_renamed)

    def as_dict(self) -> dict:
        return {"cells": self.cells, "items_resolved": self.items_resolved, "rows_removed": self.rows_removed,
                "rows_added": self.rows_added, "columns_added": self.columns_added,
                "columns_renamed": self.columns_renamed, "tables": sorted(self.tables), "samples": self.samples,
                "unmatched": self.unmatched[:50], "unmatched_total": len(self.unmatched), "notes": self.notes}


class EditSession:
    def __init__(self, tables: list[Table], changeset_id: str, instruction: str | None = None):
        self.tables = tables
        self.changeset_id = changeset_id
        self.instruction = instruction
        self.effects: list[OpEffect] = []
        self.changed: dict[str, set[tuple[str, int]]] = {t.ref: set() for t in tables}
        self.added: dict[str, dict[str, list]] = {}                      # columns to add at finish (checked by the rules)
        self.splits: dict[str, dict[int, tuple[str, list[str]]]] = {}   # D-29 splits, applied last
        self.removed: dict[str, set[int]] = {}

    # ── bookkeeping ──────────────────────────────────────────────────────────
    def begin(self) -> OpEffect:
        self.effects.append(OpEffect())
        return self.effects[-1]

    @property
    def effect(self) -> OpEffect:
        return self.effects[-1]

    def _sample(self, t: Table, col: str | None, i: int | None, old, new, what: str | None = None) -> None:
        e = self.effect
        if len(e.samples) < SAMPLE_LIMIT:
            e.samples.append({"table": t.ref, "file": t.source_file, "sheet": t.sheet,
                              "excel_row": None if i is None else t.data["_source_row"][i],
                              "row_id": None if i is None else t.data["_row_id"][i],
                              "column": col, "old": old, "new": new, **({"what": what} if what else {})})

    def _audit(self, t: Table, col: str | None, i: int | None, action: str, old, new, why: str) -> None:
        entry = t._entry(AGENT_RULE, col, i, action=action, old_value=old, new_value=new, note=why)
        entry["changeset"] = self.changeset_id
        t.audit.append(entry)

    # ── writes ───────────────────────────────────────────────────────────────
    def edit(self, t: Table, col: str, i: int, new, why: str) -> bool:
        """Change one cell (audited, re-checked when applied). Returns False when the value is already `new`."""
        old = t.data[col][i]
        if new == old:
            return False
        t.data[col][i] = new
        self._audit(t, col, i, "analyst_edit", old, new, why)
        self.changed[t.ref].add((col, i))
        self.effect.cells += 1
        self.effect.tables.add(t.ref)
        self._sample(t, col, i, old, new)
        return True

    def resolve(self, t: Table, item: dict, decision: str, why: str) -> None:
        """Close one review item with the decision recorded on it (as an uploaded Needs_Review answer does)."""
        item["status"], item["decision"] = "resolved", decision
        item["decided_by"] = f"agent:{self.changeset_id}"
        self.effect.items_resolved += 1
        self.effect.tables.add(t.ref)

    def add_column(self, t: Table, name: str, values: list, why: str) -> None:
        self.added.setdefault(t.ref, {})[name] = values
        self.effect.columns_added.append(name)
        self.effect.tables.add(t.ref)
        for i, v in enumerate(values):
            if v is not None:
                self._sample(t, name, i, None, v)
            if len(self.effect.samples) >= SAMPLE_LIMIT:
                break

    def rename(self, t: Table, old: str, new: str) -> None:
        """Rename a column in place: data, column list, types and every earlier audit / review entry."""
        t.data = {(new if c == old else c): v for c, v in t.data.items()}
        t.cols = [new if c == old else c for c in t.cols]
        if old in t.types:
            t.types[new] = t.types.pop(old)
        if old in t.text_cols:
            t.text_cols.discard(old)
            t.text_cols.add(new)
        for e in t.audit + t.review:
            if e["column"] == old:
                e["column"] = new
        self.changed[t.ref] = {(new if c == old else c, i) for c, i in self.changed[t.ref]}
        self._audit(t, new, None, "renamed", old, new, f"Column renamed by the agent: {old} -> {new}.")
        self.effect.columns_renamed.append([old, new])
        self.effect.tables.add(t.ref)

    def remove_rows(self, t: Table, idxs: list[int], why: str) -> None:
        """Remove rows now (so later operations of the same plan see the table without them). Each gets an audit entry
        holding its values, and its open review items are closed."""
        if not idxs:
            return
        drop = set(idxs)
        ids = {t.data["_row_id"][i] for i in drop}
        for i in sorted(drop):
            values = {c: t.data[c][i] for c in t.cols if t.data[c][i] is not None}
            self._audit(t, None, i, "removed", None, None, why)
            t.audit[-1]["values"] = values
            self._sample(t, None, i, ", ".join(f"{k}={v}" for k, v in list(values.items())[:4]), None, "row removed")
        for item in t.review:
            if item["status"] == "open" and item["row_id"] in ids:
                item["status"], item["decision"] = "resolved", "Row removed"
                item["decided_by"] = f"agent:{self.changeset_id}"
        keep = [i for i in range(len(t.data["_row_id"])) if i not in drop]
        t.held.reindex(keep)
        new_index = {old: n for n, old in enumerate(keep)}
        self.changed[t.ref] = {(c, new_index[i]) for c, i in self.changed[t.ref] if i in new_index}
        if t.ref in self.splits:
            self.splits[t.ref] = {new_index[i]: v for i, v in self.splits[t.ref].items() if i in new_index}
        for name, vals in self.added.get(t.ref, {}).items():            # columns this plan adds follow their rows
            self.added[t.ref][name] = [vals[i] for i in keep]
        for c in t.data:
            t.data[c] = [t.data[c][i] for i in keep]
        self.effect.rows_removed += len(drop)
        self.effect.tables.add(t.ref)

    def split(self, t: Table, i: int, col: str, parts: list[str]) -> None:
        self.splits.setdefault(t.ref, {})[i] = (col, parts)
        self.effect.rows_added += max(len(parts) - 1, 0)
        self.effect.tables.add(t.ref)
        self._sample(t, col, i, t.data[col][i], " | ".join(parts), "split into rows")

    def close_answered(self) -> int:
        """Open review items whose cell this session wrote are answered by that write (decision 'New Value'); a
        spelling item (scope 'value') is answered once no cell of its column holds the spelling any more."""
        n = 0
        for t in self.tables:
            changed = self.changed[t.ref]
            if not changed:
                continue
            cols = {c for c, _ in changed}
            index = {rid: i for i, rid in enumerate(t.data["_row_id"])}
            for item in t.review:
                col = item["column"]
                if item["status"] != "open" or col not in cols:
                    continue
                if item.get("scope") == "value":
                    done = item["current_value"] not in t.data[col]
                else:
                    i = index.get(item["row_id"])
                    done = i is not None and (col, i) in changed
                if done:
                    item["status"], item["decision"] = "resolved", "New Value"
                    item["decided_by"] = f"agent:{self.changeset_id}"
                    n += 1
        return n

    def unmatched(self, values) -> None:
        for v in values:
            if v not in self.effect.unmatched:
                self.effect.unmatched.append(v)

    def note(self, text: str) -> None:
        if text not in self.effect.notes:
            self.effect.notes.append(text)


def value_counts(values: list) -> Counter:
    return Counter(v for v in values if v is not None)
