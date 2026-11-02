"""Region finder: Excel Tables, pivots, XY-cut with title peeling, LLM review of borderline calls,
and the key rules of spec 4.4 (`t` keys from the rules, `a` keys from the review)."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .config import FOOTER_LEXICON
from .decisions import resolve_choice
from .header import View, best_header, find_group_rows
from .model import LABEL_CLASSES, Sheet, parse_ref, range_a1


@dataclass
class Region:
    coords: list
    cls: str                       # table title key_value notes fragment
    source: str = "rules"          # rules | excel_table | pivot | override
    headerless: bool = False
    fixed_header_row: int | None = None
    group_rows: list = field(default_factory=list)
    totals_rows: int = 0
    excel_name: str | None = None
    key: str | None = None
    soft: bool = False             # came out of a single-row/column cut (S-07 soft_split)
    mismatch: int = 0              # populated cells continuing an Excel Table outside its ref (S-22)
    changed_by: str | None = None  # "llm" when a review decision changed this region
    part_names: dict = field(default_factory=dict)   # Excel Table part: column number -> column name (S-22)

    def bbox(self):
        rs = [r for r, _ in self.coords]
        cs = [c for _, c in self.coords]
        return min(rs), min(cs), max(rs), max(cs)

    def top_left(self):
        r1, c1, _, _ = self.bbox()
        return r1, c1

    def ref(self) -> str:
        return range_a1(*self.bbox())


# ---------------------------------------------------------------- leaf classification


def classify_region(sheet: Sheet, coords, params: dict) -> tuple[str, bool]:
    """Returns (class, headerless), first matching rule of spec 4.4 step 5."""
    view = View(sheet, coords)
    n, nrows, ew = view.n_cells, len(view.row_list), view.eff_width
    cells = [c for row in view.rows.values() for c in row.values()]
    if all(c.cls == "W" for c in cells):             # whitespace is populated but never data: not a table
        return "fragment", False
    if ew == 1:
        texty = sum(1 for c in cells if c.cls in ("L", "T") and len((c.text or "").split()) >= 3)
        if texty / len(cells) >= 0.6:
            return "notes", False
    if n < params["min_table_cells"] or nrows == 1:
        return "fragment", False
    h, _score, _margin, cands = best_header(view, params)
    best_score = max((s for _, s in cands), default=0.0)
    if ew == 2 and nrows <= 20 and best_score < params["header_min_score"]:
        left = [row[view.cols[0]] for row in view.rows.values() if view.cols[0] in row]
        if len(left) == nrows and all(c.cls == "L" for c in left):
            return "key_value", False
    if h is not None and (view.row_list[-1] - h) >= params["min_data_rows"]:
        return "table", False
    if nrows >= 2 and ew >= 2:
        for col in view.cols:
            cls = Counter(view.rows[r][col].cls for r in view.row_list if col in view.rows[r])
            if cls.most_common(1)[0][1] / sum(cls.values()) < 0.8:
                return "fragment", False
        return "table", True
    return "fragment", False


def best_score(sheet: Sheet, coords, params: dict) -> float:
    return max((s for _, s in best_header(View(sheet, coords), params)[3]), default=0.0)


def is_footer_text(cell) -> bool:
    t = (cell.text or "").strip().lower()
    return any(t == w or t.startswith((w + " ", w + ":")) for w in FOOTER_LEXICON)


# ---------------------------------------------------------------- XY-cut


def _gaps(values: list[int]):
    """(size, position, boundary) for each empty run between consecutive occupied indices."""
    return [(b - a - 1, a + 1, a) for a, b in zip(values, values[1:]) if b - a > 1]


def _split(coords, axis: int, boundary: int):
    return [x for x in coords if x[axis] <= boundary], [x for x in coords if x[axis] > boundary]


class Cutter:
    """Recursive cut. With a reviewer, borderline soft cuts are put to the LLM (decision `soft_cut`)."""

    MAX_LLM_ROUNDS = 3              # spec §5.4: LLM-changed cuts re-cut at most 3 times, then the rules decide

    def __init__(self, sheet: Sheet, params: dict, reviewer=None, ctx: dict | None = None):
        self.sheet, self.p, self.rv, self.ctx = sheet, params, reviewer, ctx or {}
        self.llm_rounds = 0

    def _is_table(self, coords) -> bool:
        return bool(coords) and classify_region(self.sheet, coords, self.p)[0] == "table"

    def _far_headed_table(self, coords) -> bool:
        """Far side alone is a table *with a header* whose header row is all labels.

        Tightening over the spec: a data row that happens to score 4.5 (e.g. `C,30`) must not be
        mistaken for the header of a second table."""
        if len(coords) < self.p["min_table_cells"]:
            return False
        cls, headerless = classify_region(self.sheet, coords, self.p)
        if cls != "table" or headerless:
            return False
        view = View(self.sheet, coords)
        h = best_header(view, self.p)[0]
        return h is not None and all(c.cls in LABEL_CLASSES for c in view.rows[h].values())

    def _far_all_footers(self, far) -> bool:
        for r in sorted({r for r, _ in far}):
            first = min((x for x in far if x[0] == r), key=lambda x: x[1])
            if not is_footer_text(self.sheet.cells[first]):
                return False
        return True

    def _soft_split(self, near, far, axis: int, gap_pos: int):
        """Returns (split?, decided_by_llm). Rules test first; the LLM only sees borderline gaps."""
        other = 1 - axis
        diff = abs(len({x[other] for x in near}) - len({x[other] for x in far}))
        if axis == 0 and self._far_all_footers(far):
            return False, False
        rules_split = diff >= 2 or self._far_headed_table(far)
        far_score = best_score(self.sheet, far, self.p) if len(far) >= self.p["min_table_cells"] else -99.0
        borderline = diff == 1 or abs(far_score - self.p["header_min_score"]) <= 1.0
        if not (self.rv and self.rv.active and borderline) or self.llm_rounds >= self.MAX_LLM_ROUNDS:
            return rules_split, False
        rules_choice = "split" if rules_split else "no_split"
        vote = self.rv.ask("soft_cut", scope=f"{self.ctx.get('sheet_ref', '')}:{'row' if axis == 0 else 'col'}{gap_pos}",
                           coords=near + far, options=["split", "no_split"], rules_choice=rules_choice,
                           sheet=self.sheet, ctx=self.ctx, cut=(axis, gap_pos))
        if vote is None:
            return rules_split, False

        def shape_ok(choice: str) -> bool:
            if choice == "split":
                fv = View(self.sheet, far)
                return len(fv.row_list) >= 2 and fv.eff_width >= 2 and best_header(fv, self.p)[0] is not None
            return self._is_table(near + far)

        used, _outcome = resolve_choice(self.rv, vote, "soft_cut", rules_choice, shape_ok,
                                        table_ref=f"{self.ctx.get('sheet_ref', '')}:cut{axis}{gap_pos}")
        if used != rules_choice:
            self.llm_rounds += 1        # the blocks this cut produced are cut again: one more round
        return used == "split", used != rules_choice

    def _occupied(self, coords) -> tuple[list[int], list[int]]:
        """Rows and columns the block occupies. A merged range counts with its whole extent (spec §4.3 step 4),
        so a group label merged across an empty column keeps the table in one piece."""
        rows, cols = {r for r, _ in coords}, {c for _, c in coords}
        cells = set(coords)
        for r1, c1, r2, c2 in self.sheet.merges:
            if (r1, c1) in cells:
                rows.update(range(r1, r2 + 1))
                cols.update(range(c1, c2 + 1))
        return sorted(rows), sorted(cols)

    def find_cut(self, coords):
        rows, cols = self._occupied(coords)
        rg, cg = _gaps(rows), _gaps(cols)
        hard = [(-s, 0, pos, b) for s, pos, b in rg if s >= self.p["row_gap"]] + \
               [(-s, 1, pos, b) for s, pos, b in cg if s >= self.p["col_gap"]]
        if hard:
            _, axis, _, b = min(hard)
            return (*_split(coords, axis, b), False)
        if not self.p["soft_cuts"]:
            return None
        for axis, gaps in ((0, rg), (1, cg)):
            for _size, pos, b in sorted(gaps, key=lambda g: g[1]):
                near, far = _split(coords, axis, b)
                if near and far and self._soft_split(near, far, axis, pos)[0]:
                    return near, far, True
        return None

    def peel_count(self, coords) -> int:
        byrow = Counter(r for r, _ in coords)
        rows = sorted(byrow)
        k = 0
        while k < self.p["max_title_rows"] and k + 1 < len(rows) \
                and byrow[rows[k]] == 1 and byrow[rows[k + 1]] >= 2:
            k += 1
        return k

    def cut(self, coords, soft: bool = False):
        """[(kind, coords, soft)] where kind is 'title' or 'leaf'."""
        if not coords:
            return []
        k = self.peel_count(coords)
        if k:
            rows = sorted({r for r, _ in coords})[:k]
            peeled = set(rows)
            sub = self._cut([x for x in coords if x[0] not in peeled], soft)
            # Tentative: the peel stands only if the rest still yields a table (spec 4.4 step 4.1).
            if any(kind == "leaf" and self._is_table(co) for kind, co, _ in sub):
                return [("title", [x for x in coords if x[0] == r], False) for r in rows] + sub
        return self._cut(coords, soft)

    def _cut(self, coords, soft: bool):
        parts = self.find_cut(coords)
        if parts is None:
            return [("leaf", coords, soft)]
        near, far, is_soft = parts
        return self.cut(near, soft or is_soft) + self.cut(far, soft or is_soft)


# ---------------------------------------------------------------- detection


def _carve_excel_tables(sheet: Sheet, remaining: set, regions: list) -> None:
    for et in sheet.excel_tables:
        try:
            r1, c1, r2, c2 = parse_ref(et["ref"])
        except ValueError:
            continue
        coords = [x for x in remaining if r1 <= x[0] <= r2 and c1 <= x[1] <= c2]
        if not coords:
            continue
        hdr = r1 if et["header_rows"] else None
        group_rows: list[int] = []
        if hdr is not None:  # rows directly above the table may be its group rows (spec 4.4 step 3)
            table_rows = {x[0] for x in coords}
            above = [(r, c) for (r, c) in remaining if r1 - 2 <= r < r1 and c1 <= c <= c2 and r not in table_rows]
            probe = View(sheet, coords + above)
            candidates = find_group_rows(probe, hdr)        # top-down; the climb goes bottom-up
            group_rows = []
            for g in reversed(candidates):
                if len(probe.rows[g]) == 1:                  # title rule first: a one-cell row stops the climb
                    break
                if not all(c1 <= c <= c2 for c in probe.rows[g]):
                    break
                group_rows.insert(0, g)
            coords += [x for x in above if x[0] in group_rows]
        remaining -= set(coords)
        # Cells continuing the table's column pattern just below or beside it are never merged in.
        below = sum(1 for (r, c) in remaining if r == r2 + 1 + et["totals_rows"] and c1 <= c <= c2)
        beside = sum(1 for (r, c) in remaining if c == c2 + 1 and r1 <= r <= r2)
        regions.append(Region(coords, "table", "excel_table", headerless=hdr is None, fixed_header_row=hdr,
                              group_rows=group_rows, totals_rows=et["totals_rows"], excel_name=et["name"],
                              mismatch=below + beside,
                              part_names={c1 + i: n for i, n in enumerate(et.get("columns") or []) if n}))


def _carve_pivots(sheet: Sheet, remaining: set, regions: list) -> None:
    for pv in sheet.pivots:
        try:
            r1, c1, r2, c2 = parse_ref(pv["ref"])
        except ValueError:
            continue
        coords = [x for x in remaining if r1 <= x[0] <= r2 and c1 <= x[1] <= c2]
        if len(coords) < 2:
            continue
        remaining -= set(coords)
        # `firstHeaderRow` / `firstDataRow` are 0-based offsets into the location (OOXML CT_Location).
        hdr = min(r1 + pv.get("first_header_row", max(pv["first_data_row"] - 1, 0)), r2)
        regions.append(Region(coords, "table", "pivot", fixed_header_row=hdr, excel_name=pv.get("cache_sheet")))


def _detect(sheet: Sheet, params: dict, reviewer, ctx: dict) -> list[Region]:
    remaining = set(sheet.cells)
    regions: list[Region] = []
    if params["use_table_ranges"]:
        _carve_excel_tables(sheet, remaining, regions)
    _carve_pivots(sheet, remaining, regions)
    cutter = Cutter(sheet, params, reviewer, ctx)
    for kind, coords, soft in cutter.cut(sorted(remaining)):
        if kind == "title":
            regions.append(Region(coords, "title"))
            continue
        cls, headerless = classify_region(sheet, coords, params)
        reg = Region(coords, cls, headerless=headerless, soft=soft)
        if reviewer and reviewer.active:
            _review_class(reg, sheet, params, reviewer, ctx)
        regions.append(reg)
    _key_value_preambles(sheet, regions)
    return regions


KV_PREAMBLE_ROWS = 6


def _key_value_preambles(sheet: Sheet, regions: list) -> None:
    """Rule-pack extension of spec §4.3 rule 3: a small block of `label | value` pairs above a table is a report's
    key-value preamble ("Client | Acme", "Period | Q1"), not a table. The spec's own rule needs every header score
    under the minimum, which a block made only of labels never has; without this, auto-selection would send such
    blocks to Phase B as two-row tables."""
    for reg in regions:
        if reg.cls != "table" or reg.source != "rules" or reg.changed_by:
            continue
        view = View(sheet, reg.coords)
        if view.eff_width != 2 or len(view.row_list) > KV_PREAMBLE_ROWS:
            continue
        if any((c := view.rows[r].get(view.cols[0])) is None or c.cls != "L" for r in view.row_list):
            continue
        r1, c1, r2, c2 = reg.bbox()
        if any(o is not reg and o.cls == "table" and o.bbox()[0] > r2 and o.bbox()[1] <= c2 and c1 <= o.bbox()[3]
               for o in regions):
            reg.cls, reg.headerless = "key_value", False


def _review_class(reg: Region, sheet: Sheet, params: dict, reviewer, ctx: dict) -> None:
    """LLM decision `region_class` for sizable non-tables and borderline tables (spec 4.8)."""
    view = View(sheet, reg.coords)
    if reg.cls == "table":
        asked = abs(best_score(sheet, reg.coords, params) - params["header_min_score"]) <= 2.0
    else:
        asked = reviewer.review == "all" and view.n_cells >= 8
    if not asked:
        return
    options = [c for c, ok in (("table", len(view.row_list) >= 2 and view.eff_width >= 2),
                               ("key_value", view.eff_width == 2), ("notes", view.eff_width == 1),
                               ("title", len(view.row_list) == 1), ("fragment", True)) if ok]
    scope = f"{ctx.get('sheet_ref', '')}:{reg.ref()}"
    vote = reviewer.ask("region_class", scope=scope, coords=reg.coords, options=options, rules_choice=reg.cls,
                        sheet=sheet, ctx=ctx)
    if vote is None:
        return

    def shape_ok(choice: str) -> bool:
        if choice == "table":
            return best_header(view, params)[0] is not None or classify_region(sheet, reg.coords, params)[1]
        return choice in options

    used, _ = resolve_choice(reviewer, vote, "region_class", reg.cls, shape_ok, table_ref=scope)
    if used != reg.cls:
        reg.cls, reg.changed_by = used, "llm"
        reg.headerless = used == "table" and best_header(view, params)[0] is None


def find_regions(sheet: Sheet, params: dict, reviewer=None, ctx: dict | None = None) -> list[Region]:
    """Rules pass keys the tables t1, t2, ...; the LLM pass may change them but never renumbers a key."""
    ctx = ctx or {}
    rule_regions = _detect(sheet, params, None, ctx)
    _key_in_reading_order(rule_regions, "t")
    if not (reviewer and reviewer.active):
        return sorted(rule_regions, key=lambda r: r.top_left())
    rule_keys = {r.top_left(): r.key for r in rule_regions if r.cls == "table"}
    regions = _detect(sheet, params, reviewer, ctx)
    used_keys: set = set()
    new = []
    for reg in sorted((r for r in regions if r.cls == "table"), key=lambda r: r.top_left()):
        key = rule_keys.get(reg.top_left())   # a table starting where a rules table started keeps its key
        if key and key not in used_keys:
            reg.key = key
            used_keys.add(key)
        else:
            new.append(reg)
    for i, reg in enumerate(new, start=1):
        reg.key = f"a{i}"
    return sorted(regions, key=lambda r: r.top_left())


def _key_in_reading_order(regions: list[Region], prefix: str) -> None:
    tables = sorted((r for r in regions if r.cls == "table"), key=lambda r: r.top_left())
    for i, reg in enumerate(tables, start=1):
        reg.key = f"{prefix}{i}"
