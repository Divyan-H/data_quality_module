"""Phase B rules that prepare the numbers for adding up in the next modules.

D-27  Opt-in (the analyst switches it on at the start of Phase B; off, it does nothing at all, so Phases C and D see the zeros).
      A zero or blank in one period of a dated series is treated as a missing value and filled with the mean of the
      period before and the period after - but only when it is a single, isolated period and both neighbours exist and
      are one step away. Longer runs, the first and last period of a series, and columns the analyst said can really be
      zero (`zero_expected`) are not touched; a column that is mostly zero is not touched either. Every fill is audited
      and queued for review (Accept keeps it, Reject restores the original, New Value replaces it).
D-28  Flags a column whose values are the sum of other columns (Product A + Product B + Product C + Product D = Total):
      it is a derived figure, not a fact of its own. Found from the numbers only (src/common/additive.py), columns only:
      no labelled rows, no copies of one column. Nothing is changed: the next modules must not add the total together
      with its parts.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from src.common.additive import find_additive

from .values import parse_date

STEPS = (1, 7)                         # daily and weekly series; monthly or irregular series are left alone


# ── helpers ─────────────────────────────────────────────────────────────────
def period_column(t) -> tuple[str | None, list]:
    """(the first column of dates, its date per row or None). Needs 80% of the filled cells to be readable dates."""
    for col in (t.context_cols or t.cols):                    # the date column counts even when it is not one of the columns to check
        if t.kind(col) != "date":
            continue
        args = t.date_args(col)
        dates = []
        for v in t.data[col]:
            status, ymd, _ = parse_date(v, *args) if v is not None else ("none", None, None)
            dates.append(date(*ymd) if status == "ok" else None)
        filled = sum(v is not None for v in t.data[col])
        if filled and sum(d is not None for d in dates) >= 0.8 * filled:
            return col, dates
    return None, []


def label_columns(t, skip: set[str]) -> list[str]:
    """Text columns with a handful of distinct values (the City / Channel columns that split a table into series)."""
    return [c for c in (t.context_cols or t.cols) if c not in skip and t.kind(c) == "text"
            and 1 < len(set(t.values(c))) <= t.cfg["max_categories"]]


def _half(a: str, b: str) -> str | None:
    """(a + b) / 2 as exact decimal text."""
    try:
        s = format((Decimal(a) + Decimal(b)) / 2, "f")
    except InvalidOperation:
        return None
    return s.rstrip("0").rstrip(".") if "." in s else s


def _series_rows(t, dates: list, labels: list[str]) -> tuple[dict, int]:
    """{series key: row indexes in date order, with a regular daily / weekly step} and the number of series left out."""
    groups: dict[tuple, list[int]] = defaultdict(list)
    for i, d in enumerate(dates):
        if d is not None:
            groups[tuple(t.data[c][i] for c in labels)].append(i)
    out, skipped = {}, 0
    for key, rows in groups.items():
        rows.sort(key=lambda i: dates[i])
        days = [dates[i] for i in rows]
        if len(set(days)) != len(days) or len(days) < 3:
            skipped += len(days) >= 3                      # repeated dates: the neighbours of a row cannot be told
            continue
        gaps = Counter((b - a).days for a, b in zip(days, days[1:]))
        step, n = gaps.most_common(1)[0]
        if step in STEPS and n >= 0.6 * (len(days) - 1):
            out[key] = (rows, step)
        else:
            skipped += 1
    return out, skipped


# ── D-27 ────────────────────────────────────────────────────────────────────
def isolated_gaps(t) -> None:
    code = "D-27"
    if not t.cfg["fixes"].get(code, False):                # opt-in: filling a zero hides it from Phases C and D, so it never happens unasked
        return
    zero_ok = set((t.cfg.get("zero_expected") or {}).get(t.ref, []))
    period, dates = period_column(t)
    if period is None:
        return
    labels = label_columns(t, {period})
    series, skipped = _series_rows(t, dates, labels)
    if skipped:
        t.flag(code, None, None, f"{skipped} series have repeated dates or no regular daily / weekly step, so their zero and "
               "blank values were not looked at.", "Check those series by hand.", sev="info")
    if not series:
        return
    for col in t.cols:
        if col == period or col in zero_ok or col in t.text_cols or t.kind(col) != "numeric":
            continue
        nums = dict(t.numbers(col))
        filled = [v for v in nums.values()]
        if len(filled) < 8:
            continue
        if sum(v == 0 for v in filled) / len(filled) >= t.cfg["zero_heavy_share"]:
            t.flag(code, col, None, f"'{col}' is mostly zero, so zeros were not treated as missing values.",
                   "If zero is a real value here, tick it as 'zero is valid' when starting Phase B; otherwise check the column.",
                   sev="info")
            continue
        in_series = sum(len(rows) for rows, _ in series.values())
        blanks = sum(i not in nums and (col, i) not in t.held for rows, _ in series.values() for i in rows)
        if blanks > t.cfg["max_blank_share"] * in_series:
            t.flag(code, col, None, f"'{col}' is blank in {blanks} of {in_series} rows, so it looks optional rather than "
                   "missing; nothing was filled.", "Fill the gaps in the Data sheet if they are missing values.", sev="info")
            continue
        left = 0
        for rows, step in series.values():
            miss = [nums.get(i) in (None, 0) and (col, i) not in t.held for i in rows]       # a cell D-29 holds is not a gap
            for k in range(1, len(rows) - 1):
                if not miss[k]:
                    continue
                before, after = rows[k - 1], rows[k + 1]
                near = (dates[rows[k]] - dates[before]).days == step and (dates[after] - dates[rows[k]]).days == step
                if near and not miss[k - 1] and not miss[k + 1] and before in nums and after in nums:
                    new = _half(t.data[col][before], t.data[col][after])
                    if new is not None:
                        what = "blank" if nums.get(rows[k]) is None else "zero"
                        t.set(col, rows[k], new, code,
                              f"Isolated {what} in one {'day' if step == 1 else 'week'} ({dates[rows[k]]}); filled with the mean of "
                              f"the period before ({t.data[col][before]}) and after ({t.data[col][after]}).", review=True)
                        continue
                left += 1                                  # a run of 2 or more periods (the first and last period are not counted)
        if left:
            t.flag(code, col, None, f"{left} zero or blank value(s) in '{col}' are in runs of two or more periods and were "
                   "not filled.", "Phase C checks long zero runs; correct the Data sheet if they are missing values.", sev="info")


# ── D-28 ────────────────────────────────────────────────────────────────────
def bucket_report(tables) -> list[dict]:
    """buckets.json: one entry per table and bucket, for the next modules. Built from the D-28 review items, so the
    analyst's Accept / Reject on Needs_Review is part of it: a rejected relation is listed but not to be acted on."""
    out: dict[tuple, dict] = {}
    for t in tables:
        for item in (i for i in t.review if i["rule"] == "D-28" and i.get("relation")):
            rel = item["relation"]
            b = out.setdefault((t.ref, rel["where"], rel["label_column"], rel["bucket"]), {
                "table_ref": t.ref, "sheet": t.sheet, "where": rel["where"], "label_column": rel["label_column"],
                "bucket": rel["bucket"], "roles": rel["roles"], "sum_these": rel["sum_these"],
                "do_not_add_with_them": rel["do_not_add_with_them"], "relations": []})
            decision = item.get("decision") or ("open" if item["status"] == "open" else item["status"])
            b["relations"].append({k: rel[k] for k in ("total", "parts", "kind", "rows", "matched", "share", "alternatives")}
                                  | {"decision": decision})
    for b in out.values():
        live = [r for r in b["relations"] if r["decision"] != "Reject"]
        if not live:
            b["status"] = "rejected"
        else:
            b["status"] = "confirmed" if all(r["decision"] == "Accept" for r in live) else "open"
    return list(out.values())


def _suggest() -> str:
    return ("Accept confirms that these add up (do not add the total together with its parts in the next modules); "
            "Reject if it is a coincidence.")


def _flag_relations(t, res) -> None:
    by_total = {b_rel["total"]: b for b in res.buckets for b_rel in b["relations"]}
    for r in res.relations:
        b = by_total[r.total]
        parts = " + ".join(f"'{p}'" for p in r.parts)
        why = (f"'{r.total}' is the sum of {parts} in {r.matched} of {r.rows} rows, so it is a derived total, not a fact of its "
               "own"
               + (f" ({r.alternatives} other combination(s) also fit, so this may be a coincidence)" if r.alternatives else "")
               + "; adding it to those columns would double count.")
        t.flag("D-28", r.total, None, why, _suggest(), current=f"{r.total} = {' + '.join(r.parts)}")
        t.review[-1]["relation"] = {"where": "columns", "label_column": None, **r.as_dict(), "bucket": b["bucket"],
                                    "roles": b["members"], "sum_these": b["sum_these"],
                                    "do_not_add_with_them": b["do_not_add_with_them"]}


def additive_columns(t) -> None:
    if not t.cfg.get("kpi_buckets", True):
        return
    n = len(t.data["_row_id"])
    vecs = {}
    for col in t.cols:
        if t.kind(col) == "numeric" and col not in t.text_cols:
            v: list = [None] * n
            for i, x in t.numbers(col):
                v[i] = x
            vecs[col] = v
    if len(vecs) >= 3:                                     # a total needs at least two parts
        res = find_additive(vecs, find_copies=False)           # a total made of other columns; copies of one column are not totals
        _flag_relations(t, res)
        for note in res.notes:
            t.flag("D-28", None, None, note, "Check by hand.", sev="info")
