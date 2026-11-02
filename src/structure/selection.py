"""Table selection (spec 8.3) and layout groups: which detected tables feed each source."""
from __future__ import annotations

import hashlib
import re
from fnmatch import fnmatch


def layout_id(names: list[str]) -> str:
    """Tables whose normalised column-name sets are equal share a layout; column order does not matter."""
    norm = "\n".join(sorted(" ".join(n.lower().split()) for n in names))
    return "l-" + hashlib.sha256(norm.encode()).hexdigest()[:12]


def _sheet_matches(sel, sheet) -> bool:
    if sel is None:
        return True
    if isinstance(sel, str):
        sel = {"name": sel, "match": "trimmed"}
    if "index" in sel:
        return sheet["index"] == sel["index"]
    mode, want = sel.get("match", "trimmed"), sel["name"]
    if mode == "exact":
        return sheet["name"] == want
    if mode == "regex":
        return re.search(want, sheet["name"]) is not None
    return sheet["name"].strip() == want.strip()


def table_columns(fr) -> dict[str, list[str]]:
    """table_ref -> column names, for every table of a file."""
    if fr.parquet is not None:
        return {f"{fr.file_key}:0:t1": fr.parquet_names}
    return {t["table_ref"]: [c["name"] for c in t["columns"]] for s in fr.sheets for t in s["tables"]}


def _match(selector: dict, fr) -> list[str]:
    """Table refs one selector picks in one file (spec 8.3 table)."""
    key = selector.get("table", "only")
    if fr.parquet is not None:
        return [f"{fr.file_key}:0:t1"] if key in ("only", "t1") else []
    hits = []
    for s in fr.sheets:
        if s["state"] != "visible" and not selector.get("include_hidden"):
            continue
        if not _sheet_matches(selector.get("sheet"), s):
            continue
        for t in s["tables"]:
            if t["source"] == "pivot" and key != t["key"]:   # a pivot is only matched by naming its key
                continue
            if key == "only":
                if len(s["tables"]) == 1:
                    hits.append(t["table_ref"])
            elif t["key"] == key:
                hits.append(t["table_ref"])
    return hits


def _auto_candidates(fr) -> list[str]:
    """Auto-selection: tables on visible sheets, never a pivot; a Parquet file's only table counts."""
    if fr.parquet is not None:
        return [f"{fr.file_key}:0:t1"]
    return [t["table_ref"] for s in fr.sheets if s["state"] == "visible"
            for t in s["tables"] if t["source"] != "pivot"]


def resolve_selection(source_id: str, entries: list[dict], frs: list, selectors: list[dict] | None, fs) -> dict:
    """{status, by, tables, names}; adds block findings for required, missing or ambiguous selections."""
    names: dict[str, list[str]] = {}
    for fr in frs:
        names.update(table_columns(fr))
    base = {"source_id": source_id, "names": names}
    if selectors is not None:                 # an empty list is a choice too (every table switched off), not "unset"
        chosen, failed = [], False
        if not selectors:
            fs.add("selection_not_found", None, "source", "block",
                   f"Source {source_id} has no table selected: every table is switched off.", scope_id=source_id)
        for i, selector in enumerate(selectors):
            for entry, fr in zip(entries, frs):
                if fr.blocked or not fnmatch(entry.get("relative_path") or "", selector.get("file", "*")):
                    continue
                hits = _match(selector, fr)
                where, path = f"{source_id}/{fr.file_key}", entry.get("relative_path")
                if not hits:
                    failed = True
                    fs.add("selection_not_found", None, "source", "block",
                           f"A selector of source {source_id} matches no table in {path}.",
                           scope_id=where, suffix=str(i))
                elif len(hits) > 1 and not selector.get("many"):
                    failed = True
                    fs.add("selection_ambiguous", None, "source", "block",
                           f"A selector of source {source_id} matches {len(hits)} tables in {path}: "
                           + ", ".join(hits) + ".", scope_id=where, suffix=str(i))
                else:
                    chosen += hits
        status = "resolved" if chosen and not failed else ("not_found" if not chosen else "ambiguous")
        return {**base, "status": status, "by": "config", "tables": sorted(set(chosen))}
    # Deviation from spec §8.5 (by request): with no selectors, every table on a visible sheet is selected, so a
    # sheet holding several tables yields one output table (and one review sheet) per table instead of a
    # `selection_required` block. Tables on hidden sheets and pivots stay opt-in, as the spec says.
    picked = [ref for fr in frs if not fr.blocked for ref in _auto_candidates(fr)]
    if not picked:
        fs.add("selection_not_found", None, "source", "block", f"Source {source_id} has no table.")
        return {**base, "status": "not_found", "by": "auto", "tables": []}
    return {**base, "status": "resolved", "by": "auto", "tables": sorted(picked)}


def layout_groups(sel: dict, families: list[dict]) -> list[dict]:
    """Group the selected tables by layout; `family` names the S-09 family a group belongs to."""
    groups: dict[str, dict] = {}
    for ref in sel["tables"]:
        cols = sel["names"][ref]
        groups.setdefault(layout_id(cols), {"tables": [], "names": cols})["tables"].append(ref)
    out = []
    for lid, g in sorted(groups.items()):
        fam = next((i for i, f in enumerate(families) if set(g["tables"]) & set(f["members"])), None)
        out.append({"layout_id": lid, "tables": sorted(g["tables"]), "columns": len(g["names"]),
                    "family": f"family-{fam + 1}" if fam is not None else None, "names": g["names"]})
    return out


def layout_differences(groups: list[dict]) -> list[dict]:
    """Names each group lacks or adds compared with the largest group (for `multiple_layouts`)."""
    base = max(groups, key=lambda g: (len(g["tables"]), g["columns"]))
    ref = {n.lower() for n in base["names"]}
    return [{"layout_id": g["layout_id"], "missing": sorted(ref - {n.lower() for n in g["names"]}),
             "extra": sorted({n.lower() for n in g["names"]} - ref)} for g in groups if g is not base]
