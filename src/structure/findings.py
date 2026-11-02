"""Findings and action items (spec 5), derived from value-free facts (`facts.py`).

Messages hold structural text and counts only (P7, I-07): file and sheet names, labels, coordinates.
"""
from __future__ import annotations

import hashlib
import re

from .model import range_a1

CLIENT_REPORT = {f"S-{n:02d}" for n in (9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 25, 26, 27, 28)}
ALWAYS = {"sheet_error", "sheet_too_large"}      # never downgraded by selection


class Findings(list):
    """Findings for one scope. `prefix` is `{source_id}` or `{source_id}/{file_key}` (spec 5.1 rule 6)."""

    def __init__(self, prefix: str):
        super().__init__()
        self.prefix = prefix

    def add(self, code: str, check: str | None, scope: str, severity: str, message: str, *,
            sheet_index: int | None = None, table_key: str | None = None, table_ref: str | None = None,
            codes: list | None = None, locations: list | None = None, suffix: str | None = None,
            scope_id: str | None = None, fixed: bool = False) -> None:
        """`fixed`: Phase A changed the table's structure to resolve this (shown as "Fixed" in the review workbook)."""
        if scope_id is None:
            scope_id = {"table": f"{self.prefix}:{sheet_index}:{table_key}",
                        "sheet": f"{self.prefix}:{sheet_index}"}.get(scope, self.prefix)
        fid = f"{code}.{check}@{scope_id}" if check else f"{code}@{scope_id}"
        if suffix:
            fid += f"#{suffix}"
        self.append({
            "id": fid, "code": code, "check": check, "codes": codes or [code], "scope": scope,
            "sheet_index": sheet_index, "table_ref": table_ref, "severity": severity, "message": message,
            "client_report": code in CLIENT_REPORT, "locations": locations or [], "fixed": fixed})


def spans(rows: list[int]) -> str:
    """[40, 41, 4040] -> '40-41, 4040'."""
    out: list[str] = []
    start = prev = None
    for r in sorted(rows):
        if start is None:
            start = prev = r
        elif r == prev + 1:
            prev = r
        else:
            out.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = r
    if start is not None:
        out.append(f"{start}-{prev}" if prev != start else str(start))
    return ", ".join(out)


# ------------------------------------------------------------------ file level


def file_findings(fs: Findings, file_facts: dict) -> None:
    for name, sev, msg in file_facts["issues"]:
        fs.add(name, None, "file", sev, msg)
    if file_facts["names"]:
        fs.add("S-20", "names", "file", "info",
               "Named ranges pointing nowhere: "
               + ", ".join(f"{n['name']} ({n['class']})" for n in file_facts["names"]) + ".",
               locations=file_facts["names"])
    if file_facts.get("external_links"):
        fs.add("S-26", "external_links", "file", "info",
               f"The workbook links to {file_facts['external_links']} external workbook(s); formulas that use them "
               f"keep their stored results, which may be stale.")
    if file_facts["calc_on_load"]:
        fs.add("S-26", "recalc_on_open", "file", "info",
               "The workbook is set to recalculate on opening; stored results may differ from what Excel shows.")
    if file_facts["structure_locked"]:
        fs.add("S-23", "workbook_protected", "file", "info", "The workbook structure is locked.")


# ------------------------------------------------------------------ sheet and table level


def sheet_findings(fs: Findings, s: dict, params: dict) -> None:
    label = f"'{s['name']}'"

    def add(code, check, sev, msg, **x):
        fs.add(code, check, "sheet", sev, msg, sheet_index=s["index"], **x)

    if s["error"]:
        add("sheet_too_large" if s["error"] == "sheet_too_large" else "sheet_error", None, "warn",
            f"Sheet {label} was not read ({s['error']}); its populated cells are unread.")
        return
    if not s.get("virtual") and (re.search(r"[^A-Za-z0-9 _\-]", s["name"]) or s["name"] != s["name"].strip()
                                 or len(s["name"]) > 31):              # a CSV has no sheet name to check
        add("S-24", "sheet_name", "info",
            f"Sheet {label} has special characters, edge whitespace or is long; references use key '{s['key']}'.",
            fixed=True)
    if s["state"] != "visible":
        add("S-25", "hidden_sheet", "warn",
            f"Sheet {label} is {s['state']}; it was read, but its tables are never auto-selected.")
    if s["protected"]:
        add("S-23", "protected", "warn",
            f"Sheet {label} is protected. Its table was restructured in the extracted copy; acknowledge to confirm.")
    tables = s["tables"]
    if s["populated"] and s["empty_pct"] > params["sheet_sparsity_pct"]:
        add("S-06", "sparse_sheet", "info",
            f"Sheet {label} is {s['empty_pct']}% empty within {s['used_range']}: {len(tables)} table(s), "
            f"{s['regions'].get('fragment', 0)} fragment(s).")
    if len(tables) > 1:
        add("S-07", "tables", "info", f"Sheet {label} holds {len(tables)} tables: "
            + ", ".join(f"{t['key']} ({t['range']}, header {t['header_rows'][-1] if t['header_rows'] else 'none'})"
                        for t in tables) + ".", fixed=True)
    if not tables:
        add("sheet_no_table", None, "info", f"Sheet {label} has no table.")
    elif s["freeze_row"] and tables[0]["header_rows"] and s["freeze_row"] != tables[0]["header_rows"][-1]:
        add("S-15", "freeze_mismatch", "info",
            f"Sheet {label} is frozen after row {s['freeze_row']}, but the header of {tables[0]['key']} is row "
            f"{tables[0]['header_rows'][-1]}.")
    for o in s["orphans"]:
        fs.add("override_orphaned", None, "config", "warn",
               f"An override or cell edit for {o['table_ref']} no longer matches the detected layout or value "
               f"({o['reason']}); it was not applied.", scope_id=f"config/{o['table_ref']}", suffix=o.get("row_id"))
    for t in tables:
        _table_findings(fs, s, t, label, params)


def _table_findings(fs: Findings, s: dict, t: dict, label: str, params: dict) -> None:
    kw = {"sheet_index": s["index"], "table_key": t["key"], "table_ref": t["table_ref"]}
    inf, hdr = t["info"], t["header_rows"]

    def add(code, check, sev, msg, **x):
        fs.add(code, check, "table", sev, msg, **kw, **x)

    where = f"Table {t['key']} in {label}"
    tl = t["range"].split(":")[0]
    start_col = re.match(r"[A-Z]+", tl).group()
    if inf["preamble_rows"] or start_col != "A" or (hdr and hdr[0] > 1):
        pre = inf["preamble_rows"]
        add("S-01", "placement", "info", f"{where} starts at {tl}"
            + (f" (header row {hdr[-1]}, first column {start_col})" if hdr else "")
            + (f"; rows {pre[0]}-{pre[-1]} above it hold preamble." if pre else "."), codes=["S-01", "S-02", "S-04"],
            fixed=True)
    if inf["header_finding"] == "uncertain":
        cands = ", ".join(f"{r} ({sc})" for r, sc in sorted(t["candidates"], key=lambda x: -x[1])[:3])
        add("S-01", "uncertain_header", "warn", f"{where}: the header row is uncertain (candidates {cands}); used "
            f"{hdr[-1] if hdr else 'none'}, decided by {t['decided_by']}.")
    elif inf["header_finding"] == "corrected":
        add("S-01", "uncertain_header", "info", f"{where}: header row taken from an Analyst correction.")
    if inf["no_header"]:
        add("S-01", "no_header", "warn", f"{where} has no header; columns are named by letter.")
    if inf["blanks"]:
        add("S-01", "blank_header", "info", f"{where}: columns {', '.join(inf['blanks'])} have no header label.", fixed=True)
    if inf["groups"] or inf["unit"] or inf["second_header"]:
        add("S-03", "groups", "info", f"{where} has a multi-row header (group rows, unit row or second header row).",
            fixed=True)
    if inf["composite_ambiguous"]:
        add("S-03", "composite_ambiguous", "warn",
            f"{where}: composite names for columns {', '.join(inf['composed'])} rely on inferred group spans.")
    if inf["duplicates"]:
        add("S-03", "duplicate_header", "info",
            f"{where}: duplicate header labels in columns {', '.join(inf['duplicates'])} were numbered.", fixed=True)
    if inf["wide_time"]:
        add("S-03", "wide_time_layout", "info", f"{where} has time across its columns; it is not unpivoted here.")
    if t["dropped"] or inf["sparse"] or inf["header_only"]:
        add("S-05", "columns", "info",
            f"{where}: dropped {', '.join(d['letter'] for d in t['dropped']) or 'none'}; "
            f"header-only {', '.join(inf['header_only']) or 'none'}; sparse "
            + (", ".join(f"{x['letter']} ({x['fill_pct']}% filled)" for x in inf["sparse"]) or "none") + ".",
            fixed=bool(t["dropped"]))
    if inf["soft"]:
        add("S-07", "soft_split", "warn", f"{where} came from a single empty row or column cut.")
    if t["gap_rows"]:
        add("S-08", "gap_rows", "info", f"{where}: empty rows {spans(t['gap_rows'])} inside the data were removed.",
            fixed=True)
    classes = inf.get("merge_classes") or {}
    if classes:
        counts = ", ".join(f"{n} {k}" for k, n in classes.items())
        add("S-10", "merges", "info", f"{where}: merged ranges ({counts}); "
            + (f"{inf['merge_fills']} cell(s) filled from merged labels." if inf.get("merge_fills") else "nothing filled."),
            fixed=bool(inf.get("merge_fills")))
    if inf.get("numeric_merges"):
        add("S-10", "numeric_merge", "warn",
            f"{where}: numeric merged ranges {', '.join(inf['numeric_merges'])} were not filled.")
    if t["footer_rows"]:
        add("footer_rows", None, "info", f"{where}: rows {spans(t['footer_rows'])} were set aside as footers.", fixed=True)
    if inf["interior_totals"]:
        add("interior_totals", None, "info", f"{where}: total rows {spans(inf['interior_totals'])} inside the data were kept.")
    # S-11, S-12: the parser reads hidden rows/columns like any other (P3); the warning says the output holds
    # data that was invisible in Excel, which the client may have hidden on purpose.
    hidden = [r for r in t["hidden_rows"] if r not in inf["filtered_rows"]]
    if hidden:
        add("S-11", "hidden_rows", "warn",
            f"{len(hidden)} hidden row(s) ({spans(hidden)}) of {label} {t['key']} contain data and are included.")
    hidden_cols = [c["letter"] for c in t["columns"] if c["hidden"]]
    if hidden_cols:
        add("S-12", "hidden_cols", "warn",
            f"Hidden column(s) {', '.join(hidden_cols)} of {label} {t['key']} hold data and are included.")
    if t["outline_levels"]:
        aligned = inf.get("outline_totals", [])
        add("S-13", "row_outline", "info", f"{where} has outlined rows at level(s) {t['outline_levels']}; kept with their level"
            + (f"; total rows {spans(aligned)} close outlined groups (likely subtotals)." if aligned else "."))
    if any(c["outline_level"] for c in t["columns"]):
        add("S-14", "col_outline", "info", f"{where} has outlined columns; kept with their level.")
    _filter_print(add, t, label)
    hv = inf.get("hidden_values")
    if hv and (hv["hidden"] or hv["may"]):
        add("S-18", "hidden_values", "warn",
            f"{where}: {hv['hidden']} cell(s) are hidden and {hv['may']} may be hidden by their format; "
            f"values are included.")
    for dv in inf.get("dv", []):
        add("S-19", "validation", "info", f"{where}: validation ({dv['type']}) has {dv['violations']} violating cell(s)"
            + ("" if dv["checked"] else " (not checked)") + ".", suffix=dv["type"])
    if t["source"] == "pivot":
        src = f" Its cache source is sheet '{t['excel_name']}'; select that table instead." if t["excel_name"] else ""
        add("S-21", "pivot", "warn", f"{where} is a pivot table; it is never auto-selected.{src}")
    if inf["static_summary"]:
        add("S-21", "suspected_static_summary", "info", f"{where} looks like a static pivot-style summary (low confidence).")
    if t["source"] == "excel_table":
        add("S-22", "excel_table", "info", f"{where} is Excel Table '{t['excel_name']}' ({t['range']})"
            + ("; blank header labels were taken from the table definition." if inf.get("part_names_used") else "."),
            fixed=True)
        if inf["mismatch"]:
            add("S-22", "table_object_mismatch", "warn",
                f"Data continues outside the range of Excel Table '{t['excel_name']}' ({inf['mismatch']} cell(s)); "
                f"kept as a separate region.")
    _formula_findings(add, t, label, params)
    for obj in inf.get("objects", []):
        add("S-27", "objects", "info", f"{obj['name']} over {range_a1(*obj['range'])} covers {obj['covered']} "
            f"populated cell(s) of {t['key']} in {label}.", suffix=obj["name"])
    if t["comments"]:
        add("S-28", "comments", "info", f"{len(t['comments'])} comment(s) in {t['key']} ({', '.join(t['comments'][:5])}). "
            f"Comments can carry corrections; review them in the Structural_Review workbook.")


def _formula_findings(add, t: dict, label: str, params: dict) -> None:
    """S-26: uncached results (block at `uncached_block_pct` of a column), external and broken references."""
    unc = [c for c in t["columns"] if c["uncached"]]
    ext, brk = t["external_refs"], t["broken_refs"]
    if not (unc or ext or brk):
        return
    n = t["n_rows"]
    block = any(c["uncached"] * 100 >= params["uncached_block_pct"] * n for c in unc)
    locs = [{"col": c["letter"], "label": c["label"], "check": "uncached", "count": c["uncached"], "of": n} for c in unc]
    locs += [{"ref": r, "check": "external_link"} for r in ext] + [{"ref": r, "check": "broken_reference"} for r in brk]
    if unc:
        c = max(unc, key=lambda x: x["uncached"])
        msg = (f"Column {c['letter']} '{c['name']}' in {label} {t['key']}: {c['uncached']:,} of {n:,} cells are formulas "
               f"with no stored result. Open the file in Excel, let it recalculate, save and upload again, or exclude "
               f"the column.")
    else:
        msg = f"Table {t['key']} in {label} has formulas with external links or broken references."
    add("S-26", "formulas", "block" if block else "warn", msg, locations=locs)


def _filter_print(add, t: dict, label: str) -> None:
    if t["filter_ref"]:
        hidden = t["info"]["filtered_rows"]
        if hidden:
            add("S-16", "filtered_rows", "warn", f"An active filter on {t['filter_ref']} hides {len(hidden)} row(s) of "
                f"{t['key']} in {label}; all rows are included.")
        else:
            add("S-16", "filter", "info", f"Filter buttons on {t['filter_ref']}; no rows of {t['key']} in {label} are filtered.")
    ov = t["print_overlap"]
    if ov:
        add("S-17", "print_area", "info",
            f"Print area {range_a1(*ov['range'])} covers {ov['rows']:,} of {ov['of_rows']:,} data rows and {ov['cols']} "
            f"of {ov['of_cols']} columns of {t['key']}. All rows and columns are included.")


# ---------------------------------------------------------------- severity and action items


def _touches(f: dict, selected_refs: set) -> bool:
    """Does a table/sheet finding concern a selected table (or the sheet of one)?"""
    if f["scope"] == "table":
        return f["table_ref"] in selected_refs
    if f["scope"] == "sheet":
        scope_id = f["id"].split("@", 1)[1].split("#")[0]      # {source}/{file_key}:{sheet}
        return any(ref.startswith(scope_id.split("/", 1)[1] + ":") for ref in selected_refs)
    return False


def effective_severity(f: dict, selected_refs: set, resolved: bool) -> str:
    """Scope decides severity: table and sheet findings keep theirs only on a selected table (spec 5.1)."""
    if f["code"] in ALWAYS:
        return f["severity"]
    if f["scope"] in ("table", "sheet"):
        return f["severity"] if resolved and _touches(f, selected_refs) else "info"
    if f["code"] == "csv_ragged":
        file_key = f["id"].split("@", 1)[1].split("/", 1)[-1]
        return "warn" if any(r.startswith(file_key + ":") for r in selected_refs) else "info"
    return f["severity"]


def is_action_item(f: dict, selected_refs: set) -> bool:
    if f["scope"] in ("file", "job", "source", "config") or f["code"] in ALWAYS:
        return True
    return _touches(f, selected_refs)


def action_item(f: dict) -> dict:
    typ = f"structure.{f['code']}.{f['check']}" if f["check"] else f"structure.{f['code']}"
    return {"id": "ai-" + hashlib.sha256(f["id"].encode()).hexdigest()[:12], "type": typ,
            "severity": f["severity"], "message": f["message"],
            "target": {"screen": "structure", "anchor": f["id"]}}
