"""Value-free facts of a processed file (spec §9.2): everything findings, selection and the sheet JSON need.

Facts are written to `file.json`, so a reused file (spec §11.5) yields the same findings without being read
again. They hold structure, counts, coordinates and structural text only (I-07).
"""
from __future__ import annotations

import json
import re

from .extract import SheetResult, Table, merge_role
from .model import a1, col_letter, parse_ref, range_a1


def _plain(obj):
    """JSON-safe copy: tuples become lists, sets become sorted lists."""
    return json.loads(json.dumps(obj, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else list(o)))


def _runs(values: list[int]) -> list[list[int]]:
    """[40, 41, 4040] -> [[40, 41], [4040, 4040]]."""
    out: list[list[int]] = []
    for v in sorted(values):
        if out and v == out[-1][1] + 1:
            out[-1][1] = v
        else:
            out.append([v, v])
    return out


def _level_runs(levels: dict[int, int]) -> list[tuple[int, int, int]]:
    """{row: level} -> [(first, last, level)] for consecutive rows at the same level."""
    out: list[list[int]] = []
    for k in sorted(levels):
        if out and k == out[-1][1] + 1 and levels[k] == out[-1][2]:
            out[-1][1] = k
        else:
            out.append([k, k, levels[k]])
    return [tuple(x) for x in out]


def table_facts(sheet, t: Table) -> dict:
    cols = [{k: c[k] for k in ("name", "label", "letter", "col", "groups", "unit", "hidden", "outline_level",
                                "source_type", "profile", "flags", "uncached", "position")} for c in t.columns]
    filter_ref = next((e["ref"] for e in sheet.excel_tables if e["name"] == t.excel_name), None)
    if filter_ref is None and sheet.autofilter:            # a sheet filter belongs to the table it overlaps
        f, tb = parse_ref(sheet.autofilter), parse_ref(t.range)
        if f[0] <= tb[2] and tb[0] <= f[2] and f[1] <= tb[3] and tb[1] <= f[3]:
            filter_ref = sheet.autofilter
    print_overlap = None
    if sheet.print_area:
        p = sheet.print_area
        rc = sum(1 for r in t.data_rows if p[0] <= r <= p[2])
        cc = sum(1 for c in t.columns if p[1] <= c["col"] <= p[3])
        if rc and cc:
            print_overlap = {"range": p, "rows": rc, "of_rows": len(t.data_rows), "cols": cc, "of_cols": len(t.columns)}
    flagged = lambda flag: sorted(a1(*k) for k, v in sheet.formula_flags.items() if flag in v and k in t.cells)  # noqa: E731
    top = min(t.group_rows + t.header_rows + t.data_rows[:1])
    return _plain({
        "key": t.key, "table_ref": t.table_ref, "range": t.range, "source": t.source,
        "excel_name": t.excel_name, "header_rows": t.header_rows, "group_rows": t.group_rows,
        "unit_row": t.unit_row, "decided_by": t.decided_by, "candidates": t.candidates, "margin": t.margin,
        "n_rows": len(t.data_rows), "first_row": t.data_rows[0], "last_row": t.data_rows[-1],
        "span_rows": max(t.data_rows + t.footer_rows) - top + 1,
        "columns": cols, "dropped": t.dropped, "gap_rows": t.gap_rows, "footer_rows": t.footer_rows,
        "totals_rows": t.totals_rows, "hidden_rows": [r for r in t.data_rows if r in sheet.hidden_rows],
        "outline_levels": sorted({sheet.row_outline[r] for r in t.data_rows if r in sheet.row_outline}),
        "filter_ref": filter_ref, "print_overlap": print_overlap,
        "external_refs": flagged("external"), "broken_refs": flagged("broken"),
        "comments": sorted(a1(a["row"], a["col"]) for a in t.annotations if a["kind"] == "comment"),
        "info": t.info, "layout_hash": t.layout_hash,
    })


def _merge_roles(res: SheetResult) -> list[dict]:
    """Each merged range with its role relative to the tables (spec §9.4 `features.merged`)."""
    out = []
    for m in sorted(res.sheet.merges):
        role = None
        for t in res.tables:
            head = t.group_rows + t.header_rows + ([t.unit_row] if t.unit_row else [])
            role = merge_role(m, t, head, t.info.get("preamble_rows", []))
            if role:
                break
        if role is None:                  # outside every table: a peeled title, or a merge in a placement
            role = "title" if res.placements.get((m[0], m[1]), ("",))[0] == "title" else "outside"
        out.append({"ref": range_a1(*m), "role": role})
    return out


def _features(res: SheetResult) -> dict:
    """Workbook features of one sheet, in the shape of spec §9.4."""
    sh = res.sheet
    dv_hits: dict[int, int] = {}
    for t in res.tables:
        for dv in t.info.get("dv", []):
            dv_hits[dv["index"]] = dv_hits.get(dv["index"], 0) + dv["violations"]
    validations = []
    for i, dv in enumerate(sh.validations):
        f1 = (dv["f1"] or "").strip()
        source = "literal" if f1.startswith('"') else ("range" if re.search(r"[A-Za-z]+\$?\d", f1) else "formula")
        validations.append({"ref": " ".join(range_a1(*r) for r in dv["ranges"]), "type": dv["type"], "source": source,
                            "options": len(f1.strip('"').split(",")) if source == "literal" else None,
                            "violations": dv_hits.get(i, 0)})
    formula_cells = [k for k, c in sh.cells.items() if c.formula]
    return {
        "freeze": {"rows": sh.freeze_row, "cols": sh.freeze_col, "top_left": a1(sh.freeze_row + 1, sh.freeze_col + 1)}
        if sh.freeze_row or sh.freeze_col else None,
        "auto_filter": {"ref": sh.autofilter, "criteria": sh.filter_criteria} if sh.autofilter else None,
        "print_area": range_a1(*sh.print_area) if sh.print_area else None,
        "protection": {"sheet": sh.protected, "password": sh.protected_password},
        "merged": _merge_roles(res),
        "hidden_rows": _runs(list(sh.hidden_rows)),
        "hidden_cols": [[col_letter(a), col_letter(b)] for a, b in _runs(list(sh.hidden_cols))],
        "row_outline": [{"rows": [a, b], "level": lv} for a, b, lv in _level_runs(sh.row_outline)],
        "col_outline": [{"cols": [col_letter(a), col_letter(b)], "level": lv} for a, b, lv in _level_runs(sh.col_outline)],
        "conditional_formats": [{"ref": " ".join(range_a1(*r) for r in cf["ranges"]), "rule": cf.get("type"),
                                 "always": cf["always"], "hides_values": cf["hides"]} for cf in sh.cond_formats],
        "data_validations": validations,
        "excel_tables": [{k: e[k] for k in ("name", "ref", "header_rows", "totals_rows")} for e in sh.excel_tables],
        "pivots": [{k: p[k] for k in ("ref", "cache_sheet", "cache_ref")} for p in sh.pivots],
        "drawings": [{"name": d["name"], "ref": range_a1(*d["box"])} for d in sh.drawings],
        "comments": len(sh.comments),
        "formulas": {"total": len(formula_cells), "uncached": sum(1 for k in formula_cells if sh.cells[k].uncached),
                     "external": sum(1 for v in sh.formula_flags.values() if "external" in v),
                     "broken_reference": sum(1 for v in sh.formula_flags.values() if "broken" in v)},
    }


def sheet_facts(res: SheetResult) -> dict:
    sh = res.sheet
    regions: dict[str, int] = {}
    for r in res.regions:
        regions[r.cls] = regions.get(r.cls, 0) + 1
    return _plain({
        "index": sh.index, "name": sh.name, "key": sh.key, "state": sh.state, "error": res.error,
        "virtual": sh.virtual,
        "protected": sh.protected, "populated": res.populated, "empty_pct": res.empty_pct,
        "used_range": res.used_range, "declared_dimension": sh.declared_dim, "freeze_row": sh.freeze_row,
        "region_list": [{"ref": r.ref(), "class": r.cls, "table_key": r.key, "source": r.source} for r in res.regions],
        "regions": regions, "orphans": res.orphans, "features": _features(res),
        "tables": [table_facts(sh, t) for t in res.tables],
    })


def sheet_json(s: dict, file_key: str, findings_by_table: dict, llm: dict) -> dict:
    """The value-free `dp.structure.sheet/1` document of spec §9.4, built from a sheet's facts.

    `findings_by_table`: table_ref -> finding ids; `llm`: {mode, privacy, review, decisions, overruled}."""
    tables = []
    for t in s["tables"]:
        cands = [{"row": r, "score": sc} for r, sc in t["candidates"]]
        tables.append({
            "table_key": t["key"], "table_ref": t["table_ref"], "range": t["range"], "source": t["source"],
            "excel_table": t["excel_name"] if t["source"] == "excel_table" else None,
            "header": {"rows": t["header_rows"], "group_rows": t["group_rows"], "unit_row": t["unit_row"],
                       "decided_by": t["decided_by"], "candidates": cands, "margin": t["margin"]},
            "data": {"first_row": t["first_row"], "last_row": t["last_row"], "rows": t["n_rows"],
                     "gap_rows": t["gap_rows"], "footer_rows": t["footer_rows"], "totals_rows": t["totals_rows"]},
            "columns": len(t["columns"]), "dropped_columns": [d["letter"] for d in t["dropped"]],
            "outputs": {k: f"files/{file_key}/{v}" for k, v in (t.get("outputs") or {}).items()},
            "findings": sorted(findings_by_table.get(t["table_ref"], [])),
        })
    return {
        "schema": "dp.structure.sheet/1", "file_key": file_key, "sheet_index": s["index"],
        "sheet_name": s["name"], "sheet_key": s["key"], "state": s["state"], "error": s["error"],
        "declared_dimension": s["declared_dimension"], "used_range": s["used_range"],
        "populated": s["populated"], "empty_pct": s["empty_pct"], "features": s["features"],
        "regions": [{k: v for k, v in r.items() if v is not None or k == "table_key"} for r in s["region_list"]],
        "tables": tables, "llm": llm,
    }
