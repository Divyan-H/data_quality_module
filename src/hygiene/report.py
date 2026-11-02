"""Per-table quality report: counts by rule, per-column summary, and a short summary text (built from the counts)."""
from __future__ import annotations

from .config import ALIASES
from .table import Table


def template_summary(r: dict) -> str:
    fixed = sum(c["fixed"] for c in r["counts_by_rule"].values())
    top = sorted(r["counts_by_rule"].items(), key=lambda kv: -(kv[1]["fixed"] + kv[1]["flagged"]))[:3]
    worst = ", ".join(f"{k} ({v['fixed']} fixed, {v['flagged']} flagged)" for k, v in top) or "no issues"
    return (f"{r['rows_in']} rows in, {r['rows_out']} out. {fixed} automatic fixes; {r['needs_review_count']} items "
            f"need review. Most active rules: {worst}.")


def build_report(t: Table, cfg: dict) -> dict:
    counts = {}
    for code, c in sorted(t.counts.items()):
        counts[code] = {"fixed": c["fixed"], "flagged": c["flagged"], "also": ALIASES.get(code, [])}
    r = {
        "table_ref": t.ref, "source_file": t.source_file, "sheet": t.sheet, "layout_id": t.layout_id, "rows_in": t.rows_in, "rows_out": len(t.data["_row_id"]), "counts_by_rule": counts,
        "columns": {c: {"type": t.types.get(c, "empty"), "nulls": sum(v is None for v in t.data[c])} for c in t.cols},
        "needs_review_count": sum(i["status"] == "open" for i in t.review),
    }
    if t.column_choice is not None:
        r["column_choice"] = t.column_choice
    r["summary"] = template_summary(r)
    return r
