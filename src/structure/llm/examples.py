"""Example library (spec 10.7): built-in worked examples plus the project's Analyst corrections.

Every record is value-free: class grids, options and answers only."""
from __future__ import annotations

import hashlib
import json

# Built-in examples of the rule pack; `grid` rows use the class letters of spec 10.4.
BUILTIN = [
    {"id": "builtin-001", "kind": "header_row", "grid": ["L", "L L L", "D N N", "D N N"], "answer": "row:2",
     "options": ["row:1", "row:2", "row:3", "none"]},
    {"id": "builtin-002", "kind": "header_row", "grid": ["L L L", "D N N", "D N N", "D N N"], "answer": "row:1",
     "options": ["row:1", "row:2", "row:3", "none"]},
    {"id": "builtin-003", "kind": "header_row", "grid": ["L ~ L ~", "L L L L", "L N N N", "L N N N"],
     "answer": "row:2", "options": ["row:1", "row:2", "row:3", "none"]},
    {"id": "builtin-004", "kind": "region_class", "grid": ["L .", "L N", "L N"], "answer": "key_value",
     "options": ["table", "key_value", "fragment"]},
    {"id": "builtin-005", "kind": "region_class", "grid": ["T", "T", "T"], "answer": "notes",
     "options": ["notes", "fragment"]},
    {"id": "builtin-006", "kind": "soft_cut", "grid": ["L L", "N N", ".", "L L", "N N"], "answer": "split",
     "options": ["split", "no_split"]},
    {"id": "builtin-007", "kind": "footer", "grid": ["L N N", "L N N", "L N N"], "answer": "footer",
     "options": ["footer", "data"]},
]


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def correction_id(question_hash: str, right: str) -> str:
    """Same correction, same id: the platform stores a repeated correction once."""
    return "corr-" + hashlib.sha256(f"{question_hash}|{right}".encode()).hexdigest()[:12]


class Library:
    def __init__(self, records: list[dict] | None = None):
        self.records = [r for r in (records or []) if r.get("type") == "analyst_correction"]
        self.new: list[dict] = []           # corrections this job wants added to the library

    @classmethod
    def load(cls, path: str | None) -> "Library":
        if not path:
            return cls()
        with open(path, encoding="utf8") as f:
            return cls([json.loads(line) for line in f if line.strip()])

    def find_correction(self, question_hash: str) -> str | None:
        """Right answer for an identical question; the most recent correction wins."""
        hits = [r for r in self.records if r.get("question_hash") == question_hash]
        return hits[-1]["right"] if hits else None

    def add_correction(self, record: dict) -> None:
        record = {**record, "type": "analyst_correction",
                  "id": correction_id(record["question_hash"], record["right"])}
        if not any(r["id"] == record["id"] for r in self.records + self.new):
            self.new.append(record)

    def nearest(self, kind: str, grid: list[str], k: int = 3) -> list[dict]:
        """Up to k examples of the same kind, corrections first, at least one built-in (spec 10.7)."""
        def rank(r):
            return (-jaccard(grid, r["grid"]), r.get("id", ""))
        corr = sorted((r for r in self.records if r["kind"] == kind), key=rank)
        built = sorted((r for r in BUILTIN if r["kind"] == kind), key=rank)
        picked = corr[: max(k - 1, 0)] + built[:1]
        for r in corr[max(k - 1, 0):] + built[1:]:
            if len(picked) >= k:
                break
            picked.append(r)
        return picked[:k]


def format_examples(examples: list[dict]) -> str:
    out = []
    for e in examples:
        out.append(f"EXAMPLE ({e['kind']}): " + " / ".join(e["grid"]) + f" -> {e.get('answer') or e.get('right')}")
    return "\n".join(out)
