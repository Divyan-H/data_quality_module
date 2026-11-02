"""Which columns (or which labelled rows) already add up to another one - found from the numbers, never from the names.

Next modules add everything up. A Total column kept beside its parts, or an "All regions" row kept beside the regions,
would be counted twice. `find_additive` takes named vectors (a column's values row by row, or a label's values period by
period) and returns the relations "total = part + part + ...", then groups them into buckets with a role per member.

How it looks for them: for each candidate total it takes the few rows with the most non-zero columns, lists the subsets of
those columns that add up to the total in that row (pruned depth-first search with a node budget), and keeps a subset only
when it also adds up in `min_share` of all the rows where everything is filled. A column that is zero in the chosen rows
is added afterwards when that makes more rows add up. Every limit that is hit is reported in `notes`; nothing is cut silently.
Negative values are not searched (a subset search needs non-negative parts): such columns are listed in `notes`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULTS = {"rel_tol": 0.005, "abs_tol": 1e-6, "min_rows": 6, "min_share": 0.98, "max_columns": 40,
            "max_parts": 12, "node_budget": 200_000, "anchors": 3, "find_copies": True}


SAMPLE_ROWS = 4000


@dataclass
class Relation:
    total: str
    parts: list[str]
    kind: str                       # "sum" (total = parts added) or "identical" (parts has the one column it copies)
    rows: int                       # rows where the total and every part are filled
    matched: int                    # ... and the parts add up to the total
    alternatives: int = 0           # other part sets that also fit (a coincidence is possible when this is > 0)

    @property
    def share(self) -> float:
        return self.matched / self.rows if self.rows else 0.0

    def as_dict(self) -> dict:
        return {"total": self.total, "parts": self.parts, "kind": self.kind, "rows": self.rows, "matched": self.matched,
                "share": round(self.share, 4), "alternatives": self.alternatives}


@dataclass
class Result:
    relations: list[Relation] = field(default_factory=list)
    buckets: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _close(a: float, b: float, c: dict) -> bool:
    return abs(a - b) <= max(c["abs_tol"], c["rel_tol"] * abs(b))


def _fit(vectors: dict, total: str, parts: list[str], c: dict) -> tuple[int, int]:
    """(rows where all are filled, rows where parts add up to the total)."""
    t, ps = vectors[total], [vectors[p] for p in parts]
    rows = matched = 0
    for i, tv in enumerate(t):
        vals = [p[i] for p in ps]
        if tv is None or any(v is None for v in vals):
            continue
        rows += 1
        matched += _close(sum(vals), tv, c)
    return rows, matched


def _subsets(values: list[tuple[str, float]], target: float, c: dict, budget: list[int]) -> list[list[str]]:
    """Subsets (2..max_parts members) of the non-zero (name, value) pairs that add up to `target`."""
    vals = sorted(values, key=lambda nv: -nv[1])
    suffix = [0.0] * (len(vals) + 1)
    for i in range(len(vals) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + vals[i][1]
    tol = max(c["abs_tol"], c["rel_tol"] * abs(target))
    found: list[list[str]] = []

    def walk(i: int, chosen: list[str], total: float) -> None:
        if budget[0] <= 0 or len(found) >= 50:
            return
        budget[0] -= 1
        if abs(total - target) <= tol and len(chosen) >= 2:
            found.append(list(chosen))
        if i == len(vals) or len(chosen) >= c["max_parts"] or total > target + tol:
            return
        if total + suffix[i] < target - tol:
            return
        chosen.append(vals[i][0])
        walk(i + 1, chosen, total + vals[i][1])
        chosen.pop()
        walk(i + 1, chosen, total)

    walk(0, [], 0.0)
    return found


def _usable(vectors: dict, c: dict, notes: list[str]) -> list[str]:
    names = []
    for name, v in vectors.items():
        vals = [x for x in v if x is not None]
        if len(vals) < c["min_rows"] or all(x == 0 for x in vals):
            continue
        if any(x < 0 for x in vals):
            notes.append(f"'{name}' has negative values and was not searched.")
            continue
        if len(set(vals)) < 3:
            continue                                         # a (nearly) constant column proves nothing
        names.append(name)
    if len(names) > c["max_columns"]:
        notes.append(f"{len(names)} usable columns; only the first {c['max_columns']} were searched.")
        names = names[: c["max_columns"]]
    return names


def _identical(vectors: dict, names: list[str], c: dict) -> tuple[list[Relation], set[str]]:
    rels, dropped = [], set()
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if b in dropped:
                continue
            rows, matched = _fit(vectors, a, [b], c)
            if rows >= c["min_rows"] and matched / rows >= c["min_share"]:
                rels.append(Relation(a, [b], "identical", rows, matched))
                dropped.add(b)
    return rels, dropped


def _find_for(vectors: dict, total: str, others: list[str], c: dict, notes: list[str]) -> Relation | None:
    t = vectors[total]
    dense = sorted((i for i, tv in enumerate(t) if tv),
                   key=lambda i: -sum(1 for o in others if vectors[o][i]))[: c["anchors"]]
    candidates: dict[tuple, list[str]] = {}
    for i in dense:
        row = [(o, vectors[o][i]) for o in others if vectors[o][i]]
        budget = [c["node_budget"]]
        for s in _subsets(row, t[i], c, budget):
            candidates[tuple(sorted(s))] = s
        if budget[0] <= 0:
            notes.append(f"'{total}': the search limit was reached; its parts may not all have been found.")
    scored = []
    for s in candidates.values():
        parts = list(s)
        rows, matched = _fit(vectors, total, parts, c)
        for _ in range(3):                                   # parts that are zero in the chosen rows
            if rows and matched / rows >= c["min_share"]:
                break
            best = max(((o, *_fit(vectors, total, parts + [o], c)) for o in others if o not in parts),
                       key=lambda x: (x[2] / x[1] if x[1] else 0), default=None)
            if best and best[1] >= c["min_rows"] and best[2] / best[1] > (matched / rows if rows else 0) + 1e-9:
                parts, rows, matched = parts + [best[0]], best[1], best[2]
            else:
                break
        if rows >= c["min_rows"] and matched / rows >= c["min_share"]:
            scored.append(Relation(total, parts, "sum", rows, matched))
    if not scored:
        return None
    scored.sort(key=lambda r: (-r.share, len(r.parts)))
    best = scored[0]
    best.alternatives = len(scored) - 1
    return best


def _drop_derivable(rels: list[Relation]) -> list[Relation]:
    """T = A + B + C is redundant when T = D + C and D = A + B are also relations: keep the coarser ones."""
    sums = [r for r in rels if r.kind == "sum"]
    by_total = {r.total: r for r in sums}
    keep = []
    for r in sums:
        derived = False
        for other in sums:
            if other is r or other.total != r.total or len(other.parts) >= len(r.parts):
                continue
            expanded = set(other.parts)
            for _ in range(len(sums)):
                grew = False
                for p in list(expanded):
                    sub = by_total.get(p)
                    if sub and sub.total != r.total and set(sub.parts) <= set(r.parts) | expanded:
                        expanded = (expanded - {p}) | set(sub.parts)
                        grew = True
                if not grew:
                    break
            if expanded == set(r.parts):
                derived = True
                break
        if not derived:
            keep.append(r)
    return [r for r in rels if r.kind != "sum"] + keep


def _buckets(rels: list[Relation]) -> list[dict]:
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in rels:
        for p in r.parts:
            parent[find(p)] = find(r.total)
    groups: dict[str, set[str]] = {}
    for r in rels:
        for n in (r.total, *r.parts):
            groups.setdefault(find(n), set()).add(n)
    out = []
    for members in groups.values():
        mine = [r for r in rels if r.total in members]
        totals = {r.total for r in mine if r.kind == "sum"}
        parts = {p for r in mine if r.kind == "sum" for p in r.parts}
        copies = {r.parts[0] for r in mine if r.kind == "identical"}
        roles = {}
        for n in sorted(members):
            roles[n] = ("duplicate" if n in copies else "total" if n in totals and n not in parts
                        else "intermediate" if n in totals else "part")
        leaves = [n for n, role in roles.items() if role == "part"]
        out.append({"members": roles, "relations": [r.as_dict() for r in mine],
                    "sum_these": leaves, "do_not_add_with_them": [n for n, role in roles.items() if role != "part"]})
    out.sort(key=lambda b: sorted(b["members"])[0])
    for i, b in enumerate(out, start=1):
        b["bucket"] = f"Bucket {i}"
    return out


def find_additive(vectors: dict[str, list], **overrides) -> Result:
    """Relations and buckets among `vectors` ({name: values row by row, None where blank}). Names are only labels."""
    c = {**DEFAULTS, **overrides}
    res = Result()
    names = _usable(vectors, c, res.notes)
    n = max((len(v) for v in vectors.values()), default=0)
    step = max(1, n // SAMPLE_ROWS)                          # the search runs on an even sample; the answer is checked on every row
    sample = {k: v[::step] for k, v in vectors.items()} if step > 1 else vectors
    identical, copies = _identical(sample, names, c) if c["find_copies"] else ([], set())
    search = [k for k in names if k not in copies]
    rels = list(identical)
    for total in search:
        found = _find_for(sample, total, [o for o in search if o != total], c, res.notes)
        if found:
            rels.append(found)
    checked = []
    for r in rels:                                           # confirm on all rows
        r.rows, r.matched = _fit(vectors, r.total, r.parts, c)
        if r.rows >= c["min_rows"] and r.share >= c["min_share"]:
            checked.append(r)
    res.relations = _drop_derivable(checked)
    res.buckets = _buckets(res.relations)
    return res
