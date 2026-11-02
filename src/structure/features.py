"""Excel feature checks that need the tables (spec 4.10, 5.3): S-09, S-18, S-19, S-20, S-27.

None of these changes a table; they add annotations and value-free facts to `Table.info`.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from .model import Sheet, Workbook, parse_ref


def _inside(coord, rng) -> bool:
    return rng[0] <= coord[0] <= rng[2] and rng[1] <= coord[1] <= rng[3]


# ------------------------------------------------------------------ S-20 named ranges


def classify_names(wb: Workbook) -> list[dict]:
    """Once per file: `ref_error`, `missing_sheet`, `external` or `empty_target` (spec 5.3 S-20)."""
    by_name = {s.name: s for s in wb.sheets}
    out = []
    for n in wb.names:
        f = n["formula"]
        kind = None
        if "#REF!" in f:
            kind = "ref_error"
        elif re.search(r"\[[^\]]+\]", f):
            kind = "external"
        else:
            m = re.match(r"^'?([^'!]+)'?!(.+)$", f)
            if m and m.group(1) not in by_name:
                kind = "missing_sheet"
            elif m:
                try:
                    rng = parse_ref(m.group(2))
                    if not any(_inside(k, rng) for k in by_name[m.group(1)].cells):
                        kind = "empty_target"
                except ValueError:
                    pass
        if kind:
            out.append({"name": n["name"], "class": kind})
    return sorted(out, key=lambda x: x["name"])


# ------------------------------------------------------------------ S-19 data validation

_OPS = {
    "between": lambda v, a, b: a <= v <= b, "notBetween": lambda v, a, b: not a <= v <= b,
    "equal": lambda v, a, b: v == a, "notEqual": lambda v, a, b: v != a,
    "greaterThan": lambda v, a, b: v > a, "lessThan": lambda v, a, b: v < a,
    "greaterThanOrEqual": lambda v, a, b: v >= a, "lessThanOrEqual": lambda v, a, b: v <= a,
}


def _as_number(text: str | None, kind: str):
    """Comparable number for a cell text or bound, or None when it cannot be read as one."""
    if text is None:
        return None
    text = str(text).strip().strip('"')
    try:
        if kind == "date":
            if re.match(r"^\d{4}-\d{2}-\d{2}", text):
                return float((datetime.fromisoformat(text[:10]).date() - date(1899, 12, 30)).days)
        elif kind == "time":
            if re.match(r"^\d{1,2}:\d{2}", text):
                h, m, *s = (int(x) for x in text.split(":"))
                return (h * 3600 + m * 60 + (s[0] if s else 0)) / 86400
        return float(text)
    except ValueError:
        return None


def _list_values(dv: dict, sheet: Sheet, sheets: dict) -> set | None:
    f = (dv["f1"] or "").strip()
    if f.startswith('"'):
        return {x.strip().lower() for x in f.strip('"').split(",")}
    m = re.match(r"^(?:'?([^'!]+)'?!)?(.+)$", f)
    src = sheets.get(m.group(1)) if m and m.group(1) else sheet
    try:
        rng = parse_ref(m.group(2))
    except (ValueError, AttributeError):
        return None
    if src is None:
        return None
    return {(c.text or "").strip().lower() for k, c in src.cells.items() if _inside(k, rng)}


def _violates(dv: dict, cell, allowed: set | None) -> bool | None:
    """True/False, or None when the rule cannot be checked (custom formulas, unresolved bounds)."""
    typ, text = dv["type"], cell.text
    if typ == "list":
        return None if allowed is None else (text or "").strip().lower() not in allowed
    if typ in ("whole", "decimal", "date", "time"):
        v = _as_number(text, typ)
        a, b = _as_number(dv["f1"], typ), _as_number(dv["f2"], typ)
        if a is None or (dv["operator"] in ("between", "notBetween") and b is None):
            return None
        if v is None or (typ == "whole" and v != int(v)):
            return True
        return not _OPS[dv["operator"]](v, a, b)
    if typ == "textLength":
        a, b = _as_number(dv["f1"], "n"), _as_number(dv["f2"], "n")
        if a is None:
            return None
        return not _OPS[dv["operator"]](float(len(text or "")), a, b)
    return None


def check_validations(sheet: Sheet, table, sheets: dict) -> list[dict]:
    """Each rule with its violation count on this table; violating cells get `dv_violation` annotations."""
    out = []
    for index, dv in enumerate(sheet.validations):
        allowed = _list_values(dv, sheet, sheets) if dv["type"] == "list" else None
        touching = [(k, c) for k, c in table.cells.items()
                    if k not in table.fills and any(_inside(k, rng) for rng in dv["ranges"])]
        if not touching:
            continue                          # the rule does not touch this table
        results = [(k, _violates(dv, c, allowed)) for k, c in touching]
        bad = sorted(k for k, r in results if r)
        for r, c in bad:
            table.annotations.append({"row": r, "col": c, "kind": "dv_violation", "detail": {"type": dv["type"]}})
        out.append({"index": index, "type": dv["type"], "checked": any(r is not None for _, r in results),
                    "violations": len(bad), "cells": bad[:20]})
    return out


# ------------------------------------------------------------------ S-18 hidden values, S-27 objects


def hidden_values(sheet: Sheet, table) -> dict:
    """Populated table cells whose format hides them (`hidden`) or may hide them (`may`), spec 5.3 S-18."""
    hidden = {k for k in sheet.hidden_style_cells if k in table.cells}
    may: set = set()
    for cf in sheet.cond_formats:
        if not cf["hides"]:
            continue
        cells = {k for k in table.cells if any(_inside(k, r) for r in cf["ranges"])}
        (hidden if cf["always"] else may).update(cells)
    for r, c in hidden | may:
        table.annotations.append({"row": r, "col": c, "kind": "invisible_format",
                                  "detail": {"certain": (r, c) in hidden}})
    return {"hidden": len(hidden), "may": len(may - hidden)}


def objects_over(sheet: Sheet, table) -> list[dict]:
    coords = set(table.cells) | table.header_cells
    return [{"name": d["name"], "range": d["box"], "covered": n}
            for d in sheet.drawings if (n := sum(1 for k in coords if _inside(k, d["box"])))]


# ------------------------------------------------------------------ S-09 families

_SHEET_NOISE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b|\b(19|20)\d\d\b|\bq[1-4]\b|\bw\d{1,2}\b|\d+",
    re.I)


def _family_key(sheet_name: str) -> str:
    return " ".join(_SHEET_NOISE.sub("", sheet_name).lower().split())


def _tokens(name: str) -> set:
    """Split at spaces, underscores and case changes; lower-case; drop a plural `s`."""
    parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name.replace("_", " "))
    return {p.lower().rstrip("s") if len(p) > 3 else p.lower() for p in parts}


def _renames(a: dict, b: dict) -> tuple[list, list]:
    """Pair the columns of two tables: by name tokens first, then by position when the class matches."""
    pairs, used_b = [], set()
    left = [c for c in a["columns"]]
    for ca in left:
        for cb in b["columns"]:
            if cb["name"] in used_b:
                continue
            ta, tb = _tokens(ca["name"]), _tokens(cb["name"])
            if ca["name"] == cb["name"]:
                used_b.add(cb["name"])
                break
            if ta and tb and (ta <= tb or tb <= ta):
                pairs.append({"from": ca["name"], "to": cb["name"], "rule": "name"})
                used_b.add(cb["name"])
                break
    paired_a = {p["from"] for p in pairs}
    same = {c["name"] for c in a["columns"]} & {c["name"] for c in b["columns"]}
    for i, ca in enumerate(a["columns"]):
        if ca["name"] in paired_a or ca["name"] in same or i >= len(b["columns"]):
            continue
        cb = b["columns"][i]
        if cb["name"] not in used_b and cb["name"] not in same and ca["dominant"] == cb["dominant"]:
            pairs.append({"from": ca["name"], "to": cb["name"], "rule": "position"})
            used_b.add(cb["name"])
    extra = [c["name"] for c in b["columns"] if c["name"] not in used_b and c["name"] not in same]
    return pairs, extra


def families(tables: list[dict]) -> list[dict]:
    """Groups of tables that look like one dataset split across tabs or files (S-09).

    Each entry of `tables`: {ref, sheet_name, columns: [{name, dominant}]}, in reading order."""
    usable = [t for t in tables if len(t["columns"]) >= 2]
    parent = list(range(len(usable)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(usable):
        for j in range(i + 1, len(usable)):
            b = usable[j]
            if a["sheet_name"] == b["sheet_name"] and a["ref"].split(":")[0] == b["ref"].split(":")[0]:
                continue
            na, nb = {c["name"].strip().lower() for c in a["columns"]}, {c["name"].strip().lower() for c in b["columns"]}
            by_name = _family_key(a["sheet_name"]) == _family_key(b["sheet_name"]) and _family_key(a["sheet_name"]) != ""
            by_cols = len(na) >= 3 and len(nb) >= 3 and len(na & nb) / len(na | nb) >= 0.6
            if by_name or by_cols:
                parent[find(i)] = find(j)
    groups: dict[int, list] = {}
    for i, t in enumerate(usable):
        groups.setdefault(find(i), []).append(t)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        drift = []
        for a, b in zip(members, members[1:]):
            same_cols = [c["name"] for c in a["columns"]] == [c["name"] for c in b["columns"]]
            if not same_cols:
                pairs, extra = _renames(a, b)
                drift.append({"tables": [a["ref"], b["ref"]], "renames": pairs, "extra": extra})
        out.append({"members": [m["ref"] for m in members], "drift": drift})
    return out


def annotate(res, sheets: dict) -> None:
    """Run the feature checks on every table of a sheet result; the facts land in `Table.info`."""
    for t in res.tables:
        t.info["dv"] = check_validations(res.sheet, t, sheets)
        t.info["hidden_values"] = hidden_values(res.sheet, t)
        t.info["objects"] = objects_over(res.sheet, t)
