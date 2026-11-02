"""Header scorer, header block and column naming (spec 4.5, 4.6)."""
from __future__ import annotations

import re
from collections import Counter

from .config import HEADER_LEXICON, UNIT_TOKENS
from .model import DATA_CLASSES, LABEL_CLASSES, Sheet, col_letter


class View:
    """Rows of one region: {row: {col: Cell}} plus the bounds."""

    def __init__(self, sheet: Sheet, coords):
        self.sheet = sheet
        self.rows: dict[int, dict] = {}
        for r, c in coords:
            self.rows.setdefault(r, {})[c] = sheet.cells[(r, c)]
        self.row_list = sorted(self.rows)
        self.cols = sorted({c for row in self.rows.values() for c in row})
        self.n_cells = sum(len(r) for r in self.rows.values())
        self.r1, self.r2 = (self.row_list[0], self.row_list[-1]) if self.row_list else (0, 0)
        self.c1, self.c2 = (self.cols[0], self.cols[-1]) if self.cols else (0, 0)

    @property
    def eff_width(self) -> int:
        return len(self.cols)

    def col_class_below(self, r: int, c: int, limit: int = 5) -> str | None:
        seen = []
        for rr in self.row_list:
            if rr <= r:
                continue
            cell = self.rows[rr].get(c)
            if cell is not None and cell.cls != "W":
                seen.append(cell.cls)
                if len(seen) >= limit:
                    break
        return Counter(seen).most_common(1)[0][0] if seen else None


def merge_at(sheet: Sheet, r: int, c: int):
    for m in sheet.merges:
        if m[0] == r and m[1] == c:
            return m
    return None


def score_row(view: View, r: int, idx: int, params: dict) -> float:
    cells = view.rows[r]
    n = len(cells)
    classes = [x.cls for x in cells.values()]
    label_cells = [x for x in cells.values() if x.cls in LABEL_CLASSES]
    coverage = n / max(view.eff_width, 1)
    label_share = len(label_cells) / n
    texts = [(x.text or "").strip().lower() for x in label_cells]
    uniq = len(set(texts)) / len(label_cells) if label_cells else 0.0
    data_share = sum(k in DATA_CLASSES for k in classes) / n
    below = brk = 0
    for c, cell in cells.items():
        dom = view.col_class_below(r, c)
        if dom is None:
            continue
        below += 1
        if cell.cls in LABEL_CLASSES and dom in DATA_CLASSES:
            brk += 1
    type_break = brk / below if below else 0.0
    score = 3.0 * coverage + 3.0 * label_share + 1.0 * uniq + 3.0 * type_break - 3.0 * data_share
    if idx == 0:
        score += 0.5
    if view.sheet.freeze_row and view.sheet.freeze_row == r:
        score += 1.5
    lex = sum(1 for t in texts if any(w in re.split(r"[^a-z0-9]+", t) or w in t for w in HEADER_LEXICON))
    score += 0.5 * min(lex, 3)
    if classes.count("P") >= 3:
        score += 3.0
    if n == 1 and view.eff_width > 2:
        score -= 3.0
    return score


def score_candidates(view: View, params: dict) -> list[tuple[int, float]]:
    rows = view.row_list[: params["max_header_scan_rows"]]
    return [(r, round(score_row(view, r, i, params), 4)) for i, r in enumerate(rows)]


def best_header(view: View, params: dict):
    """Returns (row | None, score, margin, candidates). Header needs rows after it."""
    cands = score_candidates(view, params)
    ranked = sorted(cands, key=lambda x: (-x[1], x[0]))
    if not ranked:
        return None, 0.0, 0.0, cands
    best = ranked[0]
    margin = best[1] - ranked[1][1] if len(ranked) > 1 else best[1]
    has_after = best[0] != view.row_list[-1]
    if best[1] >= params["header_min_score"] and has_after:
        return best[0], best[1], round(margin, 4), cands
    return None, best[1], round(margin, 4), cands


def is_data_row(cells: dict) -> bool:
    if not cells:
        return False
    return sum(x.cls in DATA_CLASSES for x in cells.values()) * 2 > len(cells)


def _group_row_ok(view: View, r: int, hdr: dict) -> bool:
    cells = view.rows.get(r)
    if not cells or any(x.cls not in LABEL_CLASSES for x in cells.values()):
        return False
    if len(cells) >= len(hdr):            # a group row has fewer labels than the header it groups
        return False
    spans = []
    for c in cells:
        m = merge_at(view.sheet, r, c)
        spans.append(m[3] - m[1] + 1 if m else 1)
    if len(cells) == 1:
        return 2 <= spans[0] < view.eff_width
    return any(s >= 2 for s in spans) or len(cells) < len(hdr)


def find_group_rows(view: View, h: int) -> list[int]:
    """Up to 2 rows directly above header row h (no empty row between)."""
    found: list[int] = []
    r = h - 1
    while len(found) < 2 and r in view.rows and _group_row_ok(view, r, view.rows[h]):
        found.append(r)
        r -= 1
    return list(reversed(found))


def group_labels(view: View, r: int) -> tuple[dict[int, str], bool]:
    """(col -> group label, inferred). A label spans its merge, else up to the next label;
    `inferred` is true when a span was guessed from blank cells instead of a merge (composite_ambiguous)."""
    cells = sorted(view.rows[r].items())
    out: dict[int, str] = {}
    inferred = False
    for i, (c, cell) in enumerate(cells):
        m = merge_at(view.sheet, r, c)
        inferred |= m is None
        end = m[3] if m else (cells[i + 1][0] - 1 if i + 1 < len(cells) else view.c2)
        for cc in range(c, end + 1):
            out[cc] = (cell.text or "").strip()
    return out, inferred


def unit_row(view: View, h: int) -> int | None:
    r = h + 1
    cells = view.rows.get(r)
    nxt = view.rows.get(h + 2)
    if not cells or not nxt or not is_data_row(nxt):
        return None
    for x in cells.values():
        t = (x.text or "").strip().lower()
        if x.cls not in ("L", "P", "N", "T") or len(t) > 12 or t not in UNIT_TOKENS:
            return None
    return r


def second_header_row(view: View, h: int) -> int | None:
    r = h + 1
    cells = view.rows.get(r)
    nxt = view.rows.get(h + 2)
    if not cells or not nxt or not is_data_row(nxt):
        return None
    if any(x.cls != "L" for x in cells.values()):
        return None
    if len(cells) * 2 < view.eff_width:
        return None
    return r


def collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def build_names(cols: list[dict], sep: str) -> dict:
    """Fills `name` on each column dict. Returns flags: composed, duplicates, blanks."""
    flags = {"composed": [], "duplicates": [], "blanks": []}
    base = []
    for col in cols:
        lab = collapse(col["label"]) if col["label"] and col["label"].strip() else ""
        base.append(lab)
    counts = Counter(b.lower() for b in base if b)
    names: list[str | None] = [None] * len(cols)
    for i, col in enumerate(cols):
        lab = base[i]
        if lab and counts[lab.lower()] == 1:
            names[i] = lab
    taken = {n.lower() for n in names if n}
    for i, col in enumerate(cols):
        if names[i]:
            continue
        lab = base[i] or f"col_{col['letter']}"
        if not base[i]:
            flags["blanks"].append(col["letter"])
        groups = [g for g in col["groups"] if g]
        k = 1 if groups else 0
        cand = sep.join(groups[-k:] + [lab]) if k else lab
        while k < len(groups) and cand.lower() in taken:
            k += 1
            cand = sep.join(groups[-k:] + [lab])
        if k:
            flags["composed"].append(col["letter"])
        names[i] = cand
        taken.add(cand.lower())
    seen: Counter = Counter()
    for i, n in enumerate(names):
        key = n.lower()
        seen[key] += 1
        if seen[key] > 1:
            names[i] = f"{n}__{seen[key]}"
            flags["duplicates"].append(cols[i]["letter"])
    for i, col in enumerate(cols):
        n = names[i]
        if n.startswith("_"):
            n = "src" + n
        col["name"] = n
    # uniqueness after prefixing / suffixing (case-insensitive)
    used: set = set()
    for col in cols:
        n, k = col["name"], 2
        while n.lower() in used:
            n = f"{col['name']}__{k}"
            k += 1
        col["name"] = n
        used.add(n.lower())
    return flags


def make_columns(view: View, header_rows: list[int], group_rows: list[int], unit: int | None,
                 sep: str) -> tuple[list[dict], dict]:
    """Column dicts (letter, label, groups, unit, name) for every column of the view."""
    low = header_rows[-1]
    levels = [group_labels(view, g) for g in group_rows]
    if len(header_rows) > 1:  # upper header acts as group level
        levels.append(group_labels(view, header_rows[0]))
    group_maps = [m for m, _ in levels]
    cols = []
    for c in view.cols:
        cell = view.rows.get(low, {}).get(c)
        label = cell.text if cell is not None else None
        groups = [m[c] for m in group_maps if c in m and m[c]]
        ucell = view.rows.get(unit, {}).get(c) if unit else None
        cols.append({"col": c, "letter": col_letter(c), "label": label, "groups": groups,
                     "unit": (ucell.text.strip() if ucell is not None and ucell.text else None)})
    flags = build_names(cols, sep)
    flags["inferred_groups"] = any(inferred for _, inferred in levels)
    return cols, flags
