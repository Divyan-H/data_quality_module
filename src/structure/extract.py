"""Structural fixer: a region becomes a Table (spec 4.6, 4.7, 4.9); overrides, edits and invariants."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from collections.abc import MutableMapping
from dataclasses import dataclass, field

from .decisions import resolve_choice, resolve_header
from .header import (
    View, best_header, build_names, find_group_rows, make_columns, score_candidates, second_header_row,
    unit_row,
)
from .model import LABEL_CLASSES, Cell, Sheet, col_index, col_letter, parse_ref, range_a1
from .regions import Region, find_regions, is_footer_text


class InvariantViolation(Exception):
    """I-01..I-06 broken: a pipeline bug, the job fails rather than write wrong tables (spec 4.11)."""


@dataclass
class Table:
    key: str
    table_ref: str
    range: str
    source: str
    excel_name: str | None
    headerless: bool
    header_rows: list
    group_rows: list
    unit_row: int | None
    decided_by: str
    score: float
    margin: float
    candidates: list
    columns: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    data_rows: list = field(default_factory=list)
    cells: MutableMapping = field(default_factory=dict)   # (row, col) -> Cell, merge fills and edits included
    fills: set = field(default_factory=set)           # coords in `cells` that did not exist in the file
    edited: set = field(default_factory=set)          # coords whose value is an Analyst edit (I-02 exempt)
    header_cells: set = field(default_factory=set)
    footer_rows: list = field(default_factory=list)
    totals_rows: list = field(default_factory=list)
    gap_rows: list = field(default_factory=list)
    annotations: list = field(default_factory=list)   # rows of cells.parquet (category annotation)
    audit: list = field(default_factory=list)         # rows of audit.parquet
    placements: dict = field(default_factory=dict)    # coord -> (kind, region ref)
    info: dict = field(default_factory=dict)          # value-free facts that drive findings
    layout_hash: str = ""

    def real_data_cells(self) -> set:
        return {k for k in self.cells if k not in self.fills}


class TableCells(MutableMapping):
    """The data cells of a table: a view onto the sheet's cells (rows `data_rows`, columns `cols`) plus its own
    additions (merge fills, analyst edits). Nothing is copied, so a lakh-row table costs no second index."""

    def __init__(self, base: dict, data_rows: list, cols: set):
        self.base, self.rows, self.cols, self.own = base, set(data_rows), cols, {}
        self._order = list(data_rows)

    def _inside(self, key) -> bool:
        return key[0] in self.rows and key[1] in self.cols and key in self.base

    def __getitem__(self, key):
        if key in self.own:
            return self.own[key]
        if self._inside(key):
            return self.base[key]
        raise KeyError(key)

    def __setitem__(self, key, cell) -> None:
        self.own[key] = cell

    def __delitem__(self, key) -> None:
        if key not in self.own:
            raise KeyError(key)
        del self.own[key]

    def __contains__(self, key) -> bool:
        return key in self.own or self._inside(key)

    def __iter__(self):
        base = self.base
        cols = sorted(self.cols)
        for r in self._order:
            for c in cols:
                if (r, c) in base and (r, c) not in self.own:
                    yield (r, c)
        yield from self.own

    def __len__(self) -> int:
        return sum(1 for _ in self)


@dataclass
class SheetResult:
    sheet: Sheet
    regions: list
    tables: list
    placements: dict          # coord -> (kind, region ref)
    populated: int
    used_range: str | None
    empty_pct: float
    error: str | None = None
    orphans: list = field(default_factory=list)       # overrides / edits that no longer apply


def layout_hash(t: Table) -> str:
    """Hash of range, header rows and labels: an override is only applied to the layout it was made for."""
    payload = [t.range, t.header_rows, t.group_rows, [c["label"] for c in t.columns]]
    return "sha256:" + hashlib.sha256(json.dumps(payload).encode()).hexdigest()


# ------------------------------------------------------------------ footers and totals


def _num(cell) -> float | None:
    if cell is None or cell.cls not in ("N", "P") or cell.text is None:
        return None
    try:
        return float(cell.text)
    except ValueError:
        return None


def _matches_sums(view: View, cells: dict, above: list[int]) -> bool:
    """Every number of the row equals the sum or the average of its column over `above` (0.5%)."""
    nums = {c: _num(x) for c, x in cells.items() if _num(x) is not None}
    if not nums:
        return False
    for c, v in nums.items():
        col = [n for n in (_num(view.rows[a].get(c)) for a in above) if n is not None]
        if not col:
            return False
        if not any(abs(v - t) <= 0.005 * max(abs(t), 1e-9) for t in (sum(col), sum(col) / len(col))):
            return False
    return True


def detect_footers(view: View, data_rows: list[int], ask=None) -> list[int]:
    """Trailing rows that pass both footer tests (spec 4.7). `ask(row)` may overrule a row that passes
    exactly one test. Guard: never take all the data."""
    if len(data_rows) < 2:
        return []
    body = data_rows[:-5] if len(data_rows) > 5 else data_rows[:1]
    # Rule-pack extension of spec §4.6: a column counts as "typed" when >= 80% of its body cells are numbers
    # OR dates, so a `Total` label in a date column breaks the pattern like one in a number column does.
    numeric_cols = {c for c in view.cols
                    if (vals := [view.rows[r][c] for r in body if c in view.rows[r]])
                    and sum(v.cls in ("N", "D") for v in vals) / len(vals) >= 0.8}
    footers: list[int] = []
    for r in reversed(data_rows[-5:]):
        cells = view.rows[r]
        first = cells[min(cells)]
        note = first.cls in ("L", "T") and len((first.text or "").split()) >= 3 and (r - 1) not in view.rows
        test1 = is_footer_text(first) or note
        test2 = (any(x.cls in ("L", "T") and c in numeric_cols for c, x in cells.items())
                 or len(cells) * 2 < len(view.cols)
                 or _matches_sums(view, cells, [x for x in data_rows if x < r and x not in footers]))
        is_footer = test1 and test2
        if not is_footer and test1 != test2 and ask is not None:
            is_footer = ask(r)
        if not is_footer:
            break
        footers.append(r)
    return sorted(footers) if len(footers) < len(data_rows) else []


def interior_totals(view: View, data_rows: list[int], footers: list[int]) -> list[int]:
    """Total rows inside the data whose numbers equal the sums since the previous such row (kept, listed)."""
    found, since = [], []
    for r in data_rows:
        if r in footers:
            continue
        cells = view.rows[r]
        if is_footer_text(cells[min(cells)]) and since and _matches_sums(view, cells, since):
            found.append(r)
            since = []
        else:
            since.append(r)
    return found


# ------------------------------------------------------------------ header decision


def header_valid(view: View, row: int, params: dict, footer_rows) -> bool:
    """Header validity of spec 10.6."""
    cells = view.rows.get(row)
    if not cells or row in footer_rows:
        return False
    if len(cells) < 2 and view.eff_width > 1:
        return False
    if sum(c.cls in LABEL_CLASSES for c in cells.values()) * 2 < len(cells):
        return False
    return sum(1 for r in view.row_list if r > row) >= params["min_data_rows"]


def _decide_header(view: View, region: Region, params: dict, rv, ctx: dict, ov: dict | None):
    """Returns (header_row | None, score, margin, candidates, decided_by, finding)."""
    cands = score_candidates(view, params)
    if ov and ov.get("header_rows") is not None:
        rows = ov["header_rows"]
        return (rows[-1] if rows else None), 0.0, 0.0, cands, "override", None
    if region.fixed_header_row is not None:
        src = "pivot" if region.source == "pivot" else "excel_table"
        return region.fixed_header_row, dict(cands).get(region.fixed_header_row, 0.0), 0.0, cands, src, None
    if region.headerless:
        h, score, margin = None, 0.0, 0.0
    else:
        h, score, margin, cands = best_header(view, params)
    near_tie = h is not None and margin < params["header_tie_margin"] and len(cands) > 1
    decided_by, finding = "rules", ("uncertain" if near_tie else None)   # the rules decided a close call alone
    if rv and rv.active and (rv.review == "all" or near_tie or abs(score - params["header_min_score"]) <= 2.0):
        options = [f"row:{r}" for r, _ in cands] + ["none"]
        rules_choice = f"row:{h}" if h is not None else "none"
        vote = rv.ask("header_row", scope=ctx["table_scope"], coords=region.coords, options=options,
                      rules_choice=rules_choice, sheet=view.sheet, ctx=ctx)
        if vote is not None:
            scores = {f"row:{r}": s for r, s in cands}
            footers = set(detect_footers(view, [r for r in view.row_list if h is not None and r > h]))
            headerless_ok = region.headerless

            def valid(choice: str) -> bool:
                return headerless_ok if choice == "none" else \
                    header_valid(view, int(choice.split(":")[1]), params, footers)

            used, outcome, finding = resolve_header(rv, vote, rules_choice, scores, valid, params,
                                                    table_ref=ctx["table_scope"])
            h = None if used == "none" else int(used.split(":")[1])
            if outcome in ("changed", "corrected"):
                decided_by = "correction" if outcome == "corrected" else "llm"
            elif outcome == "agreed":
                decided_by = "rules+llm"
    return h, score, margin, cands, decided_by, finding


# ------------------------------------------------------------------ table build


def _placement_kind(cells: dict) -> str:
    if len(cells) == 1:
        return "title"
    if len(cells) == 2 and cells[min(cells)].cls == "L":
        return "key_value"
    return "preamble"


def _audit(t: Table, step: str, code: str, action: str, ref: str, count: int, before=None, after=None,
           decided_by: str = "rules", note: str = "") -> None:
    t.audit.append({"step": step, "code": code, "action": action, "ref": ref, "count": count,
                    "before": before or {}, "after": after or {}, "decided_by": decided_by, "note": note})


def _filtered_rows(sheet: Sheet, rows: list[int], cols: list[int]) -> set:
    """Rows hidden inside an AutoFilter that has criteria (S-16) and that overlaps this table's columns."""
    ranges = []
    if sheet.autofilter and sheet.filter_criteria:
        ranges.append(parse_ref(sheet.autofilter))
    ranges += [parse_ref(t["ref"]) for t in sheet.excel_tables if t["criteria"]]
    lo, hi = min(cols), max(cols)
    ranges = [a for a in ranges if a[1] <= hi and lo <= a[3]]
    return {r for r in rows if r in sheet.hidden_rows and any(a[0] <= r <= a[2] for a in ranges)}


def build_table(sheet: Sheet, region: Region, params: dict, file_key: str, rv=None,
                ov: dict | None = None, ctx: dict | None = None) -> Table | None:
    """Returns None when the region turns out to hold no data row (I-06: reclassified as fragment)."""
    ctx = dict(ctx or {})
    ctx["table_scope"] = f"{file_key}:{sheet.index}:{region.key}"
    view = View(sheet, region.coords)
    h, score, margin, cands, decided_by, hdr_finding = _decide_header(view, region, params, rv, ctx, ov)
    header_rows: list[int] = []
    group_rows: list[int] = []
    unit = None
    if ov and ov.get("header_rows"):
        header_rows = list(ov["header_rows"])
    elif h is not None:
        group_rows = region.group_rows or find_group_rows(view, h)
        header_rows = [h]
        unit = unit_row(view, h)
        sec = second_header_row(view, h) if unit is None else None
        if sec is not None:
            header_rows.append(sec)
    hdr_end = max(header_rows + ([unit] if unit else [])) if header_rows else view.r1 - 1
    top = min(group_rows + header_rows) if header_rows else view.r1

    t = Table(key=region.key or "t?", table_ref=ctx["table_scope"], range="", source=region.source,
              excel_name=region.excel_name, headerless=not header_rows, header_rows=header_rows,
              group_rows=group_rows, unit_row=unit, decided_by=decided_by, score=score, margin=margin,
              candidates=cands)
    t.info = {"header_finding": hdr_finding, "no_header": not header_rows, "soft": region.soft,
              "mismatch": region.mismatch, "excel_name": region.excel_name}

    # Rows above the header block become placements: title, key-value or preamble (S-01, S-04).
    pre_rows = [r for r in view.row_list if r < top]
    for r in pre_rows:
        kind = _placement_kind(view.rows[r])
        for c in view.rows[r]:
            t.placements[(r, c)] = (kind, region.ref())
    if pre_rows:
        _audit(t, "preamble_out", "S-01", "move_preamble", f"{pre_rows[0]}:{pre_rows[-1]}", len(pre_rows),
               after={"rows": pre_rows})
    t.info["preamble_rows"] = pre_rows
    t.info["static_summary"] = _static_summary(view, pre_rows, header_rows)

    # Data rows, footers, interior totals, gaps.
    data_rows = [r for r in view.row_list if r > hdr_end]
    footers: list[int] = []
    if region.totals_rows:
        footers = data_rows[-region.totals_rows:]
        t.totals_rows = list(footers)
    elif params["footers"]:
        def ask_footer(r: int) -> bool:
            if not (rv and rv.active):
                return False
            vote = rv.ask("footer", scope=f"{ctx['table_scope']}:row{r}", coords=region.coords,
                          options=["footer", "data"], rules_choice="data", sheet=sheet, ctx=ctx, focus_row=r)
            if vote is None:
                return False
            used, _ = resolve_choice(rv, vote, "footer", "data", lambda c: c == "footer",
                                     table_ref=ctx["table_scope"])
            return used == "footer"
        footers = detect_footers(view, data_rows, ask_footer)
    t.footer_rows = footers
    for r in footers:
        kind = "totals_row" if r in t.totals_rows else "footer"
        for c in view.rows[r]:
            t.placements[(r, c)] = (kind, range_a1(r, min(view.rows[r]), r, max(view.rows[r])))
    if footers:
        _audit(t, "footers_totals", "S-22" if t.totals_rows else "S-01",
               "exclude_totals" if t.totals_rows else "exclude_footer", f"{footers[0]}:{footers[-1]}", len(footers))
    footer_set = set(footers)
    data_rows = [r for r in data_rows if r not in footer_set]
    t.info["interior_totals"] = interior_totals(view, data_rows, footers)

    # Analyst exclusions (override, spec 8.4): the cells become placements, keys never change.
    excl_rows = sorted(set(ov.get("exclude_rows", [])) & set(data_rows)) if ov else []
    for r in excl_rows:
        for c in view.rows[r]:
            t.placements[(r, c)] = ("excluded_row", range_a1(r, c, r, c))
    if excl_rows:
        _audit(t, "overrides", "override", "apply_override", ",".join(map(str, excl_rows)), len(excl_rows),
               decided_by="override", note="exclude_rows")
    excl_set = set(excl_rows)
    data_rows = [r for r in data_rows if r not in excl_set]
    if not data_rows:
        return None
    t.data_rows = data_rows
    t.gap_rows = [r for r in range(data_rows[0], data_rows[-1] + 1)
                  if r not in view.rows and r not in footer_set and r not in excl_set] \
        if params["gap_rows"] else []
    if t.gap_rows:
        _audit(t, "gap_rows", "S-08", "remove_gap_rows", f"{t.gap_rows[0]}:{t.gap_rows[-1]}", len(t.gap_rows),
               after={"rows": t.gap_rows})

    _select_columns(t, view, sheet, params, ov, data_rows, group_rows, header_rows, unit, region.part_names)
    if not t.columns:
        return None
    _fill_merges(t, sheet, params, data_rows, group_rows + header_rows + ([unit] if unit else []), pre_rows)
    _finish_columns(sheet, t, data_rows, params)
    t.info["filtered_rows"] = sorted(_filtered_rows(sheet, data_rows, [c["col"] for c in t.columns]))
    # S-21 heuristic, second form: one row per category plus a grand-total footer.
    first_col = min(c["col"] for c in t.columns)
    keys = [t.cells[(r, first_col)].text for r in data_rows if (r, first_col) in t.cells]
    grand = any((view.rows[r][min(view.rows[r])].text or "").strip().lower().startswith("grand total") for r in footers)
    t.info["static_summary"] = t.info["static_summary"] or (grand and len(keys) == len(set(keys)) == len(data_rows))
    # S-13: interior totals that sit right below an outlined group are the group's subtotals.
    t.info["outline_totals"] = [r for r in t.info["interior_totals"]
                                if sheet.row_outline.get(r - 1) and not sheet.row_outline.get(r)]
    for r in data_rows:                    # CSV records with too few or too many fields are kept and annotated
        if r in sheet.ragged:
            n, expected = sheet.ragged[r]
            t.annotations.append({"row": r, "col": min(n, expected) if n < expected else expected + 1,
                                  "kind": "ragged_short" if n < expected else "ragged_long",
                                  "detail": {"fields": n, "expected": expected}})
    last_col = max(c["col"] for c in t.columns)
    t.range = range_a1(top, first_col, max(data_rows + footers), last_col)
    if hdr_finding == "corrected":
        t.info["header_finding"] = "corrected"
    if header_rows and (header_rows[0] > 1 or first_col > 1 or pre_rows):
        _audit(t, "header_block", "S-01", "promote_header", str(header_rows[-1]), 1,
               before={"first_row": view.r1}, after={"header_row": header_rows[-1]}, decided_by=decided_by)
    t.layout_hash = layout_hash(t)
    return t


def _static_summary(view: View, pre_rows: list[int], header_rows: list[int]) -> bool:
    """S-21 heuristic (low confidence): pivot-like wording in the title, header or preamble."""
    words = ("pivot", "summary", "last refreshed", "as of")
    texts = [(c.text or "").lower() for r in pre_rows + header_rows for c in view.rows.get(r, {}).values()]
    return any(w in t for t in texts for w in words)


def _select_columns(t: Table, view: View, sheet: Sheet, params: dict, ov, data_rows, group_rows, header_rows,
                    unit, part_names: dict | None = None) -> None:
    """S-05: drop a column only when its label is blank and it holds no data. Names, groups and units.

    `part_names` (S-22): an Excel Table's own column names, used when a header cell is blank or the table has
    no header row at all."""
    part_names = part_names or {}
    head_rows = group_rows + header_rows + ([unit] if unit else [])
    data_cols = {c for r in data_rows for c in view.rows[r]}
    own_cols = sorted(data_cols | {c for r in head_rows for c in view.rows.get(r, {})})
    if own_cols:   # a spacer column with no cell at all still lies inside the table: it is dropped and listed (S-05)
        own_cols = list(range(own_cols[0], own_cols[-1] + 1))
    excl_cols = {col_index(x) if isinstance(x, str) else x for x in (ov or {}).get("exclude_columns", [])}
    low = header_rows[-1] if header_rows else None
    kept, dropped = [], []
    for c in own_cols:
        label = view.rows.get(low, {}).get(c) if low else None
        blank = (label is None or not (label.text or "").strip()) and c not in part_names
        if c in excl_cols:
            for r in head_rows + data_rows:
                if c in view.rows.get(r, {}):
                    t.placements[(r, c)] = ("excluded_column", range_a1(r, c, r, c))
        elif c in data_cols or not blank or not params["drop_empty_columns"]:
            kept.append(c)
        else:
            dropped.append(c)
            for r in head_rows:
                if c in view.rows.get(r, {}):
                    t.placements[(r, c)] = ("preamble", range_a1(r, c, r, c))
    t.dropped = [{"letter": col_letter(c), "reason": "S-05: blank label, no data"} for c in dropped]
    if dropped:
        _audit(t, "columns", "S-05", "drop_columns", ",".join(col_letter(c) for c in dropped), len(dropped))
    if excl_cols & set(own_cols):
        _audit(t, "overrides", "override", "apply_override", ",".join(col_letter(c) for c in sorted(excl_cols)),
               len(excl_cols), decided_by="override", note="exclude_columns")
    kept_set = set(kept)
    t.header_cells = {(r, c) for r in head_rows for c in view.rows.get(r, {}) if c in kept_set}
    t.cells = TableCells(sheet.cells, data_rows, kept_set)
    first_below = {}                                          # make_columns needs the columns, not the data rows
    for r in data_rows:
        for c in view.rows[r]:
            if c in kept_set:
                first_below.setdefault(c, (r, c))
    tview = View(sheet, list(t.header_cells) + list(first_below.values()))
    if header_rows:
        cols, flags = make_columns(tview, header_rows, group_rows, unit, params["group_separator"])
    else:  # no header: columns are named by letter (S-01 no_header)
        cols = [{"col": c, "letter": col_letter(c), "label": None, "groups": [], "unit": None} for c in tview.cols]
        flags = build_names(cols, params["group_separator"])
    if part_names and any(not (c["label"] or "").strip() and c["col"] in part_names for c in cols):
        for c in cols:
            if not (c["label"] or "").strip() and c["col"] in part_names:
                c["label"] = part_names[c["col"]]
        inferred = flags.get("inferred_groups", False)
        flags = build_names(cols, params["group_separator"])      # names follow the filled-in labels
        flags["inferred_groups"] = inferred
        t.info["part_names_used"] = True
    t.columns = [c for c in cols if c["col"] in kept_set]
    upper_hdr = view.rows.get(header_rows[0], {}) if header_rows else {}
    t.info.update({
        "composed": flags["composed"], "duplicates": flags["duplicates"], "blanks": flags["blanks"],
        "groups": bool(group_rows), "unit": unit is not None, "second_header": len(header_rows) > 1,
        "composite_ambiguous": bool(flags["composed"]) and (flags.get("inferred_groups", False)
                                                            or len(group_rows) + len(header_rows) >= 3),
        "wide_time": sum(x.cls == "P" for x in upper_hdr.values()) >= 3})
    if group_rows or unit or len(header_rows) > 1:
        _audit(t, "header_block", "S-03", "attach_groups" if group_rows else "attach_units",
               f"{min(group_rows + header_rows)}:{max(header_rows + ([unit] if unit else []))}",
               len(group_rows) + len(header_rows))
    if flags["composed"]:
        _audit(t, "header_block", "S-03", "compose_names", ",".join(flags["composed"]), len(flags["composed"]))


def merge_role(m: tuple, t: Table, head_rows: list[int], pre_rows: list[int]) -> str | None:
    """Where a merged range lies relative to one table: title, header, data or crossing; None if it misses it."""
    r1, c1, r2, c2 = m
    kept = [c["col"] for c in t.columns]
    lo_c, hi_c = min(kept), max(kept)
    top, bottom = min(head_rows + pre_rows + t.data_rows), max(t.data_rows)
    if r2 < top or r1 > bottom or c2 < lo_c or c1 > hi_c:
        return None
    rows = set(range(r1, r2 + 1))
    if rows <= set(pre_rows):
        return "title"
    if rows <= set(head_rows):
        return "header"
    if rows <= set(t.data_rows) | set(t.gap_rows) and lo_c <= c1 and c2 <= hi_c:
        return "data"
    return "crossing"


def _fill_merges(t: Table, sheet: Sheet, params: dict, data_rows: list[int], head_rows: list[int],
                 pre_rows: list[int]) -> None:
    """S-10: each merge touching the table is classed title / header / data / crossing (spec §6.2).

    Only `data` merges change anything: text, date and boolean labels are copied across the range; a numeric
    merge keeps its value on the anchor row only (repeating an amount would multiply totals)."""
    dset, kept = set(data_rows), {c["col"] for c in t.columns}
    classes: dict[str, int] = {}
    for (r1, c1, r2, c2) in sheet.merges:
        cls = merge_role((r1, c1, r2, c2), t, head_rows, pre_rows)
        if cls is None:
            continue                                          # does not touch this table
        classes[cls] = classes.get(cls, 0) + 1
        if cls != "data" or (r1, c1) not in sheet.cells:
            continue
        anchor, ref = sheet.cells[(r1, c1)], range_a1(r1, c1, r2, c2)
        if anchor.ctype not in ("s", "text", "date", "datetime", "time", "b"):
            if anchor.ctype == "n" and not params["fill_numeric"]:
                t.info.setdefault("numeric_merges", []).append(ref)
                t.annotations.append({"row": r1, "col": c1, "kind": "merge_numeric", "detail": {"range": ref}})
                _audit(t, "merged_cells", "S-10", "keep_numeric_merge", ref, (r2 - r1 + 1) * (c2 - c1 + 1))
                continue
            if anchor.ctype != "n":
                continue                                      # errors / uncached formulas are never copied
        elif not params["fill_labels"]:
            continue
        n = 0
        for rr in range(r1, r2 + 1):
            for cc in range(c1, c2 + 1):
                if (rr, cc) == (r1, c1) or rr not in dset or cc not in kept or (rr, cc) in sheet.cells:
                    continue
                t.cells[(rr, cc)] = copy.copy(anchor)
                t.fills.add((rr, cc))
                t.annotations.append({"row": rr, "col": cc, "kind": "merge_fill", "detail": {"range": ref}})
                n += 1
        if n:
            t.info["merge_fills"] = t.info.get("merge_fills", 0) + n
            _audit(t, "merged_cells", "S-10", "fill_merge", ref, n)
    t.info["merge_classes"] = dict(sorted(classes.items()))


def _finish_columns(sheet: Sheet, t: Table, data_rows: list[int], params: dict) -> None:
    """Per-column type histogram, profile, flags and annotations (spec 6.6, 6.7)."""
    n_rows = len(data_rows)
    for i, col in enumerate(t.columns, start=1):
        c = col["col"]
        real = [(r, t.cells[(r, c)]) for r in data_rows if (r, c) in t.cells and (r, c) not in t.fills]
        hist = Counter(v.ctype for _, v in real)
        dominant = hist.most_common(1)[0][0] if hist else "s"
        fmts = Counter(v.fmt for _, v in real if v.fmt and v.ctype not in ("s", "text"))
        distinct = {v.text for r in data_rows if (v := t.cells.get((r, c))) is not None and v.text is not None}
        flags = []
        if len(hist) > 1:
            flags.append("type_exception")
        if hist.get("formula_uncached"):
            flags.append("S-26")
        for r, v in real:
            if len(hist) > 1 and v.ctype != dominant:
                t.annotations.append({"row": r, "col": c, "kind": "type_exception",
                                      "detail": {"type": v.ctype, "dominant": dominant}})
            if v.uncached:
                t.annotations.append({"row": r, "col": c, "kind": "formula_uncached",
                                      "detail": {"formula": v.formula_text} if v.formula_text else {}})
            if v.error:
                t.annotations.append({"row": r, "col": c, "kind": "error", "detail": {}})
            if v.out_of_range:
                t.annotations.append({"row": r, "col": c, "kind": "date_out_of_range", "detail": {}})
            if (r, c) in sheet.comments:
                t.annotations.append({"row": r, "col": c, "kind": "comment",
                                      "detail": {"text": sheet.comments[(r, c)]}})
            if (r, c) in sheet.formula_flags:
                for flag in sorted(sheet.formula_flags[(r, c)]):
                    t.annotations.append({"row": r, "col": c,
                                          "kind": "external_link" if flag == "external" else "broken_reference",
                                          "detail": {"formula": t.cells[(r, c)].formula_text}
                                          if t.cells[(r, c)].formula_text else {}})
        fill_pct = round(100.0 * len(real) / n_rows, 2) if n_rows else 0.0
        col.update({
            "position": i, "hidden": c in sheet.hidden_cols, "outline_level": sheet.col_outline.get(c, 0),
            "source_type": {"dominant": dominant, "histogram": dict(sorted(hist.items())),
                            "number_format": fmts.most_common(1)[0][0] if fmts else "General"},
            "profile": {"populated": len(real), "fill_pct": fill_pct,
                        "values": sum(1 for _, v in real if v.text is not None),
                        "distinct_estimate": len(distinct)},
            "flags": flags, "uncached": hist.get("formula_uncached", 0)})
    t.info["sparse"] = [{"letter": c["letter"], "fill_pct": c["profile"]["fill_pct"]} for c in t.columns
                        if 0 < c["profile"]["populated"] and 100 - c["profile"]["fill_pct"] >= params["sparse_column_pct"]]
    t.info["header_only"] = [c["letter"] for c in t.columns if c["profile"]["populated"] == 0]


# ------------------------------------------------------------------ cell edits (spec 8.5)


def apply_cell_edits(t: Table, edits: list[dict], orphans: list) -> None:
    """Applied at fixer step 8. An edit whose row/column vanished, or whose current value is no longer
    `from`, is not applied: it is reported as orphaned instead."""
    names = {c["name"]: c["col"] for c in t.columns}
    type_map = {"s": ("L", "s"), "n": ("N", "n"), "d": ("D", "date"), "b": ("B", "b")}
    for e in edits:
        row = int(e["row_id"].rsplit(":", 1)[1])
        col = names.get(e["column"])
        cell = t.cells.get((row, col)) if col is not None and row in t.data_rows else None
        if cell is None and not (col is not None and row in t.data_rows):
            orphans.append({"table_ref": t.table_ref, "row_id": e["row_id"], "column": e["column"], "reason": "gone"})
            continue
        current = cell.text if cell is not None else None
        if current != e.get("from"):
            orphans.append({"table_ref": t.table_ref, "row_id": e["row_id"], "column": e["column"],
                            "reason": "changed"})
            continue
        cls, ctype = type_map.get(e.get("to_type", "s"), ("L", "s"))
        t.cells[(row, col)] = Cell(cls, e.get("to"), ctype, cell.fmt if cell else None)
        t.edited.add((row, col))
        if cell is None or (row, col) in t.fills:
            t.fills.add((row, col))     # the coordinate holds no populated cell in the file (I-01)
        t.annotations.append({"row": row, "col": col, "kind": "analyst_edit", "detail": {}})
        _audit(t, "overrides", "analyst_edit", "apply_cell_edit", f"{col_letter(col)}{row}", 1,
               before={"value": current}, after={"value": e.get("to")}, decided_by="analyst")


# ------------------------------------------------------------------ sheet analysis


def normalize_override(o: dict) -> dict:
    """Expand `"40-41"` row ranges and `"AD-AE"` column ranges into explicit lists."""
    out = dict(o)
    rows: list[int] = []
    for item in o.get("exclude_rows") or []:
        a, _, b = str(item).partition("-")
        rows += range(int(a), int(b or a) + 1)
    cols: list[int] = []
    for item in o.get("exclude_columns") or []:
        a, _, b = str(item).partition("-")
        cols += range(col_index(a), col_index(b or a) + 1)
    out["exclude_rows"], out["exclude_columns"] = rows, cols
    return out


def _tables_for(sheet, regions, params, file_key, rv, ctx, overrides_by_key):
    """Build tables, applying overrides after detection has keyed and hashed them (spec 4.4 step 7)."""
    tables, placements = [], {}
    for reg in regions:
        rng = reg.ref()
        if reg.cls == "table":
            t = build_table(sheet, reg, params, file_key, rv, overrides_by_key.get(reg.key), ctx)
            if t is not None:
                tables.append(t)
                placements.update(t.placements)
                continue
            reg.cls = "fragment"                       # I-06: never dropped, reclassified
        kind = "excluded_table" if reg.changed_by == "override" else reg.cls
        for coord in reg.coords:
            placements[coord] = (kind, rng)
    return tables, placements


def _apply_overrides(sheet, regions, params, file_key, rv, ctx, overrides, orphans):
    """Returns (regions, overrides_by_key). Layout hashes of detected tables are checked against `base`."""
    prefix = f"{file_key}:{sheet.index}:"
    mine = [o for o in overrides if o["table"].startswith(prefix)]
    by_key = {reg.key: reg for reg in regions if reg.cls == "table"}
    active: dict[str, dict] = {}
    claim: dict[str, set] = {}
    for o in mine:
        key = o["table"][len(prefix):]
        reg = by_key.get(key)
        if o.get("base") is not None or not key.startswith("o"):
            if reg is None:
                orphans.append({"table_ref": o["table"], "reason": "table_not_found"})
                continue
            probe = build_table(sheet, reg, params, file_key, None, None, ctx)
            if probe is None or probe.layout_hash != o["base"]:
                orphans.append({"table_ref": o["table"], "reason": "layout_changed"})
                continue
        if o.get("status") == "not_table":
            if reg is not None:
                reg.cls, reg.changed_by = "fragment", "override"
            continue
        active[key] = o
        if o.get("range"):
            r1, c1, r2, c2 = parse_ref(o["range"])
            cells = [k for k in sheet.cells if r1 <= k[0] <= r2 and c1 <= k[1] <= c2]
            claim[key] = set(cells)
    # A range takes its cells away from every other region; the leftovers give up their key.
    for key, cells in claim.items():
        reg = by_key.get(key)
        if reg is None:
            reg = Region([], "table", "override", key=key)
            regions.append(reg)
            by_key[key] = reg
        reg.coords = sorted(cells)
        reg.source = "override" if reg.source == "rules" else reg.source
        reg.fixed_header_row = None
        reg.headerless = False
    taken = set().union(*claim.values()) if claim else set()
    for reg in list(regions):
        if reg.key in claim or not taken:
            continue
        rest = [c for c in reg.coords if c not in taken]
        if len(rest) != len(reg.coords):
            reg.coords = rest
            if not rest:
                regions.remove(reg)
            elif reg.cls == "table":
                from .regions import classify_region
                if classify_region(sheet, rest, params)[0] != "table":
                    reg.cls, reg.key = "fragment", None
    return regions, active


def _audit_region_changes(sheet: Sheet, regions: list, tables: list) -> None:
    """Audit what the region finder did (spec §9.8): each peeled title against the table it sat above, and
    each table cut out of a sheet that held several."""
    if not tables:
        return
    boxes = {t.key: parse_ref(t.range) for t in tables}
    for reg in (r for r in regions if r.cls == "title"):
        r1, c1, _r2, c2 = reg.bbox()
        below = [t for t in tables if boxes[t.key][0] > r1 and boxes[t.key][1] <= c2 and c1 <= boxes[t.key][3]]
        if below:
            t = min(below, key=lambda x: boxes[x.key][0])
            _audit(t, "preamble_out", "S-01", "peel_title", reg.ref(), 1, note="title row moved to Excluded_Cells")
            cells = set(reg.coords)                       # S-10: the title's merge counts for this table
            titles = sum(1 for m in sheet.merges if (m[0], m[1]) in cells)
            if titles:
                classes = t.info.setdefault("merge_classes", {})
                classes["title"] = classes.get("title", 0) + titles
                t.info["merge_classes"] = dict(sorted(classes.items()))
    if len(tables) > 1:
        for t in tables:
            _audit(t, "resolve_range", "S-07", "split_table", t.range, len(tables),
                   note=f"one of {len(tables)} tables in the sheet")


def analyze_sheet(sheet: Sheet, params: dict, file_key: str, rv=None, overrides=(), edits=None) -> SheetResult:
    used = sheet.used_range()
    if sheet.error:
        # Spec §6.3: the sheet is listed with its error and every cell read before it failed is accounted
        # as an `unread` placement, so nothing vanishes (K-14).
        placements = {k: ("unread", None) for k in sheet.cells}
        return SheetResult(sheet, [], [], placements, len(sheet.cells),
                           range_a1(*used) if used else None, 0.0, sheet.error)
    if used is None:
        return SheetResult(sheet, [], [], {}, 0, None, 0.0, None)
    r1, c1, r2, c2 = used
    empty_pct = round(100.0 * (1 - len(sheet.cells) / ((r2 - r1 + 1) * (c2 - c1 + 1))), 1)
    ctx = {"sheet_ref": f"{file_key}:{sheet.index}", "sheet_name": sheet.name, "path": rv.path if rv else ""}
    orphans: list = []
    regions = find_regions(sheet, params, rv, ctx)
    regions, active = _apply_overrides(sheet, regions, params, file_key, rv, ctx,
                                       [normalize_override(o) for o in overrides], orphans)
    tables, placements = _tables_for(sheet, regions, params, file_key, rv, ctx, active)
    _audit_region_changes(sheet, regions, tables)
    for t in tables:
        if t.key in active:
            _audit(t, "overrides", "override", "apply_override", t.range, 1, decided_by="override")
        if edits:
            apply_cell_edits(t, [e for e in edits if e["row_id"].startswith(t.table_ref + ":")], orphans)
    _check_invariants(sheet, tables, placements)
    return SheetResult(sheet, regions, tables, placements, len(sheet.cells), range_a1(r1, c1, r2, c2),
                       empty_pct, orphans=orphans)


ROW_ID = re.compile(r"^[0-9a-f]{12,16}:\d{1,4}:[toa]\d{1,4}:\d{1,10}$")
MAX_ROW, MAX_COL = 1_048_576, 16_384


def _check_invariants(sheet: Sheet, tables: list, placements: dict) -> None:
    """Invariants I-01 to I-05 of spec §4.8; a violation is a pipeline bug and fails the job."""
    claimed: Counter = Counter()
    for t in tables:
        claimed.update(t.header_cells)
        claimed.update(t.real_data_cells())
    claimed.update(placements.keys())
    if set(claimed) != set(sheet.cells) or any(v != 1 for v in claimed.values()):
        missing = len(set(sheet.cells) - set(claimed))
        dup = sum(1 for v in claimed.values() if v != 1)
        raise InvariantViolation(f"I-01 sheet {sheet.index}: {missing} unaccounted, {dup} double-counted")
    r1, c1, r2, c2 = sheet.used_range()
    for t in tables:
        where = f"sheet {sheet.index} table {t.key}"
        # I-02 value fidelity: every data cell is the source cell's canonical rendering, bar fills and edits.
        for k, cell in t.cells.items():
            if k not in t.fills and k not in t.edited and cell.text != sheet.cells[k].text:
                raise InvariantViolation(f"I-02 {where}: cell {col_letter(k[1])}{k[0]} differs from the source")
        # I-03 coordinates: every table cell lies inside the used range.
        for r, c in list(t.cells) + list(t.header_cells):
            if not (r1 <= r <= r2 and c1 <= c <= c2):
                raise InvariantViolation(f"I-03 {where}: {col_letter(c)}{r} lies outside the used range")
        # I-04 Row_ID: unique, well-formed, `_source_row` strictly increasing.
        ids = [f"{t.table_ref}:{r}" for r in t.data_rows]
        ordered = all(a < b for a, b in zip(t.data_rows, t.data_rows[1:]))
        if len(set(ids)) != len(ids) or not ordered or not all(ROW_ID.match(i) for i in ids):
            raise InvariantViolation(f"I-04 {where}: Row_IDs are not unique, ordered and well-formed")
        # I-05 names.
        names = [c["name"].lower() for c in t.columns]
        if len(set(names)) != len(names) or any(not n or n.startswith("_") for n in names):
            raise InvariantViolation(f"I-05 {where}: bad column names")
    for r, c in placements:
        if not (r1 <= r <= r2 and c1 <= c <= c2):
            raise InvariantViolation(f"I-03 sheet {sheet.index}: placement {col_letter(c)}{r} lies outside the used range")
    # I-03, other coordinates: every recorded feature lies within Excel's grid.
    boxes = [d["box"] for d in sheet.drawings] + ([sheet.print_area] if sheet.print_area else []) + sheet.merges
    points = list(sheet.comments) + [(r, 1) for r in sheet.hidden_rows] + [(1, c) for c in sheet.hidden_cols]
    for a, b, c, d in boxes:
        points += [(a, b), (c, d)]
    if any(not (1 <= r <= MAX_ROW and 1 <= c <= MAX_COL) for r, c in points):
        raise InvariantViolation(f"I-03 sheet {sheet.index}: a feature coordinate lies outside Excel's grid")
