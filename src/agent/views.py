"""Read-only data for the Fix-with-agent screen: open issues grouped, and pages of a table's rows with each cell's
state (changed, waiting for a decision). Read straight from a phase's output files; nothing here loads a whole run
into memory or changes anything, so the API can serve it without a worker job."""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import polars as pl

from src.common.columns import SYSTEM
from src.hygiene.config import RULE_TEXT
from src.hygiene.pipeline import OUT

from .errors import AgentError

MAX_PAGE = 500
_cache: dict[Path, tuple[int, object]] = {}
_lock = threading.Lock()


def _json(path: Path):
    """JSON file contents, cached until the file changes (audit files can be large and are read on every page)."""
    st = path.stat()
    key = st.st_mtime_ns ^ st.st_size
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
    data = json.loads(path.read_text("utf8"))
    with _lock:
        _cache[path] = (key, data)
    return data


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class HygieneViews:
    def __init__(self, run_dir: Path):
        self.out = Path(run_dir) / OUT
        if not (self.out / "run.json").exists():
            raise AgentError("not_ready", "Phase B has no results yet.")

    def _reports(self) -> dict[str, dict]:
        return {r["table_ref"]: r for r in (_json(p) for p in sorted((self.out / "quality_reports").glob("*.json")))}

    def tables(self) -> list[dict]:
        refs = _json(self.out / "run.json")["tables"]
        reps = self._reports()
        return [{"ref": ref, "file": reps.get(ref, {}).get("source_file"), "sheet": reps.get(ref, {}).get("sheet"),
                 "rows": reps.get(ref, {}).get("rows_out"), "columns": list(reps.get(ref, {}).get("columns", {}))}
                for ref in refs]

    def issues(self) -> dict:
        items = _json(self.out / "needs_review.json")
        groups: dict[tuple, dict] = {}
        resolved = by_agent = 0
        for i in items:
            if i["status"] != "open":
                resolved += 1
                by_agent += str(i.get("decided_by", "")).startswith("agent:")
                continue
            key = (i["table_ref"], i["rule"], i["column"])
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"table": i["table_ref"], "rule": i["rule"], "what": RULE_TEXT.get(i["rule"], ""),
                                   "column": i["column"], "count": 0, "cells": 0, "severity": i.get("severity", "warn"),
                                   "client": bool(i.get("client_report")), "why": i.get("why_flagged", ""),
                                   "suggested": i.get("suggested_action", ""), "examples": [], "rows": []}
            g["count"] += 1
            g["cells"] += i.get("cells") or (1 if i["row_id"] else 0)
            cur = i.get("current_value")
            if cur is not None and len(g["examples"]) < 4 and str(cur) not in g["examples"]:
                g["examples"].append(str(cur))
            if i.get("source_row") is not None and len(g["rows"]) < 20:
                g["rows"].append(i["source_row"])
        out = sorted(groups.values(), key=lambda g: ({"block": 0, "warn": 1, "info": 2}.get(g["severity"], 1), -g["count"], g["rule"]))
        return {"open": sum(g["count"] for g in out), "resolved": resolved, "resolved_by_agent": by_agent,
                "groups": out, "tables": self.tables()}

    def _index(self, table: str) -> dict:
        """Per-table lookups built from the review and audit files once, and rebuilt only when either file changes
        (an apply or upload rewrites them). Paging through a big table no longer rescans millions of audit entries."""
        files = [self.out / "needs_review.json", self.out / "remediation_audit.json"]
        stamp = tuple((f.stat().st_mtime_ns, f.stat().st_size) for f in files)
        key = (self.out / "_index" / table)
        with _lock:
            hit = _cache.get(key)
            if hit and hit[0] == stamp:
                return hit[1]
        items = [i for i in _json(files[0]) if i["table_ref"] == table and i["status"] == "open"]
        flagged: dict[str, dict[str, dict]] = defaultdict(dict)
        column_items, value_items = [], []
        for i in items:
            if i["row_id"] is None:
                column_items.append({"rule": i["rule"], "column": i["column"], "why": i["why_flagged"]})
            elif i.get("scope") == "value":
                value_items.append((i["column"], i["current_value"], i))   # marked on every cell with that spelling
            else:
                flagged[i["row_id"]][i["column"]] = {"rule": i["rule"], "why": i["why_flagged"], "proposed": i.get("proposed_value")}
        changed: dict[str, dict[str, dict]] = defaultdict(dict)
        for a in _json(files[1]):
            if a["table_ref"] == table and a["row_id"] is not None and a["column"] and a["action"] in ("fixed", "analyst_edit"):
                changed[a["row_id"]][a["column"]] = {"rule": a["rule"], "old": a["old_value"], "note": a.get("note")}
        index = {"items": items, "flagged": dict(flagged), "changed": dict(changed), "column_items": column_items,
                 "value_items": value_items}
        with _lock:
            _cache[key] = (stamp, index)
        return index

    def rows(self, table: str, offset: int = 0, limit: int = 100, rule: str | None = None, column: str | None = None,
             only: str | None = None) -> dict:
        refs = _json(self.out / "run.json")["tables"]
        if table not in refs:
            raise AgentError("no_such_table", f"There is no table {table} in Phase B's results.")
        limit = max(1, min(limit, MAX_PAGE))
        path = self.out / "tables" / table.replace(":", "_") / "data.parquet"
        idx = self._index(table)
        flagged, changed, value_items = idx["flagged"], idx["changed"], idx["value_items"]
        scan = pl.scan_parquet(path)
        names = scan.collect_schema().names()
        cols = [c for c in names if c not in SYSTEM]
        if rule or column or only:                             # a filter: find the matching row positions first
            want_cols = ["_row_id"] + sorted({c for c, _, i in value_items if c in names
                                              and (not rule or i["rule"] == rule) and (not column or c == column)})
            head = scan.select(want_cols).collect()
            ids = head["_row_id"].to_list()
            keep = list(range(len(ids)))
            if rule or column:
                wanted = {i["row_id"] for i in idx["items"] if (not rule or i["rule"] == rule) and (not column or i["column"] == column)}
                for col, val, i in value_items:
                    if col in head.columns and (not rule or i["rule"] == rule) and (not column or col == column):
                        wanted |= {ids[n] for n, v in enumerate(head[col].to_list()) if _text(v) == val}
                keep = [n for n in keep if ids[n] in wanted]
            if only == "changed":
                keep = [n for n in keep if ids[n] in changed]
            elif only == "flagged":
                keep = [n for n in keep if ids[n] in flagged]
            total, positions = len(keep), keep[offset:offset + limit]
            page = (scan.with_row_index("_pos").filter(pl.col("_pos").is_in(positions)).collect()
                    if positions else scan.head(0).collect())
        else:                                                  # no filter: read just this page
            total = scan.select(pl.len()).collect().item()
            page = scan.slice(offset, limit).collect()
        ids = page["_row_id"].to_list()
        src = page["_source_row"].to_list() if "_source_row" in page.columns else [None] * page.height
        data = {c: page[c].to_list() for c in cols}
        rows = []
        for n, rid in enumerate(ids):
            values = {c: _text(data[c][n]) for c in cols}
            flags = dict(flagged.get(rid, {}))
            for col, val, i in value_items:
                if col in values and values[col] == val:
                    flags[col] = {"rule": i["rule"], "why": i["why_flagged"], "proposed": i.get("proposed_value")}
            rows.append({"row_id": rid, "excel_row": src[n], "values": values, "changed": changed.get(rid, {}),
                         "flagged": flags})
        return {"table": table, "columns": cols, "total": total, "offset": offset, "limit": limit, "rows": rows,
                "column_items": idx["column_items"]}
