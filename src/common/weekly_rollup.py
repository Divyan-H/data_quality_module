"""A monthly table as a weekly one, before Phase C: one row per Monday-to-Sunday week and series.

Phase C checks weekly series. A table whose only time information is a month (a real date, or Year-Month / Year + Month
labels) is turned into a weekly table first. Each monthly figure is spread over the weeks of its month in proportion
to the days each week has in it (src/common/monthly_weeks.py: a month starting on a Saturday puts 2 days in its first
week). A week that crosses a month boundary gets a share from each month; the shares are added up into one row:

  sum      the week gets  monthly figure x days of the week in that month / days in that month, added over the months
           (orders, spend, clicks: the weeks of a month add back up to the monthly figure)
  average  the week gets the day-weighted mean of the monthly figures it touches
           (AOV, temperature, margin %: a rate is the same in every week of the month, never divided)

Which of the two a column gets is the analyst's input (the Column Setup sheet of the client config workbook); a column
not listed there is guessed from its name (`average_words`). A series that has the same month twice keeps both rows, so the
duplicate date is found by Phase C instead of being added together. The first and last week of a series can cover fewer
than 7 days; that is recorded in the log, not hidden.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import replace
from datetime import date

from .columns import SYSTEM
from .dataset import Dataset
from .granularity import assess, find_period_column, is_time_label, is_year_month_label, numeric_columns, with_period
from .monthly_weeks import month_segments
from .values import Periods

SUM, AVERAGE = "sum", "average"
ROLLUPS = {"sum": SUM, "total": SUM, "add": SUM, "average": AVERAGE, "avg": AVERAGE, "mean": AVERAGE, "rate": AVERAGE,
           "ratio": AVERAGE}


def guess(name: str, average_words: list[str]) -> str:
    """Roll-up of a column from its name: a word of `average_words` makes it an average, anything else a sum."""
    low = name.lower().replace("_", " ")
    return AVERAGE if any(w.lower() in low for w in average_words) else SUM


def types_for(ds: Dataset, measures: list[str], chosen: dict[str, str] | None, average_words: list[str]) -> dict[str, str]:
    """Roll-up of every measure column: the analyst's choice where there is one, else the name's guess."""
    chosen = {k.casefold(): v for k, v in (chosen or {}).items()}
    return {m: chosen.get(m.casefold()) or guess(m, average_words) for m in measures}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def is_monthly(ds: Dataset, dayfirst: bool = False) -> bool:
    return assess(ds, dayfirst)["level"] == "monthly"


def to_weekly(ds: Dataset, dayfirst: bool = False, chosen: dict[str, str] | None = None,
              average_words: list[str] | None = None) -> tuple[Dataset, dict] | None:
    """(weekly table, log) for a table whose time level is monthly; None for any other table (daily, weekly and the
    rest are returned by the caller unchanged). The table keeps its label columns, measures and column order; the
    date column (or the Period derived from Year-Month labels) now holds the Monday of the week, and the Year / Month
    label columns are dropped."""
    if not is_monthly(ds, dayfirst):
        return None
    ds = with_period(ds)
    period = find_period_column(ds, dayfirst)
    if period is None:
        return None
    parse = Periods([v for v in ds.data[period] if v is not None], dayfirst)
    measures = numeric_columns(ds, {period})
    if not measures:
        return None
    types = types_for(ds, measures, chosen, average_words or [])
    labels = [c for c in ds.columns if c != period and c not in measures and not is_time_label(ds, c)
              and not is_year_month_label(ds, c)]
    n = len(ds.row_ids)
    # series -> layer (k-th row of the same month) -> week start -> accumulator
    acc: dict[tuple, dict[date, dict]] = defaultdict(dict)
    seen: dict[tuple, int] = defaultdict(int)
    order: list[tuple] = []
    skipped = []
    for i in range(n):
        d = parse(ds.data[period][i]) if ds.data[period][i] is not None else None
        if d is None:
            skipped.append(ds.row_ids[i])
            continue
        key = tuple(ds.data[c][i] for c in labels)
        layer = seen[(key, d.year, d.month)]
        seen[(key, d.year, d.month)] += 1
        slot = (key, layer)
        if slot not in acc:
            order.append(slot)
        month_days = calendar.monthrange(d.year, d.month)[1]
        for seg in month_segments(d.year, d.month):
            cell = acc[slot].setdefault(seg.week_start, {"days": 0, "rows": [], "sum": defaultdict(float),
                                                          "wsum": defaultdict(float), "wdays": defaultdict(int)})
            cell["days"] += seg.days
            if ds.row_ids[i] not in cell["rows"]:
                cell["rows"].append(ds.row_ids[i])
            for m in measures:
                v = ds.data[m][i]
                if not _num(v):
                    continue
                cell["sum"][m] += v * seg.days / month_days
                cell["wsum"][m] += v * seg.days
                cell["wdays"][m] += seg.days
    # the table keeps its own column order and the date column keeps its own name, so a client config written for it still fits
    columns = [c for c in ds.columns if c == period or c in measures or c in labels]
    out: dict[str, list] = {"_row_id": [], "_source_row": [], **{c: [] for c in columns}}
    sources, short = {}, 0
    k = 0
    for slot in order:
        key, _ = slot
        for ws in sorted(acc[slot]):
            cell = acc[slot][ws]
            k += 1
            rid = f"{ds.ref}:w{k}"
            out["_row_id"].append(rid)
            out["_source_row"].append(None)
            for c, v in zip(labels, key):
                out[c].append(v)
            out[period].append(ws)
            for m in measures:
                if not cell["wdays"][m]:
                    out[m].append(None)
                elif types[m] == AVERAGE:
                    out[m].append(cell["wsum"][m] / cell["wdays"][m])
                else:
                    out[m].append(cell["sum"][m])
            sources[rid] = {"week_start": ws.isoformat(), "days": cell["days"], "from_rows": cell["rows"]}
            short += cell["days"] < 7
    for c in SYSTEM:
        if c not in out and c in ds.data:
            out[c] = [None] * len(out["_row_id"])
    data = {**{c: out[c] for c in out if c in SYSTEM}, **{c: out[c] for c in columns}}
    weekly = replace(ds, data=data, raw_header=list(columns), derived="monthly")
    log = {"rule": "Each monthly figure is spread over the Monday-to-Sunday weeks of its month by days; sums are "
                   "split, averages are day-weighted.",
           "monthly_rows": n, "weekly_rows": len(weekly.row_ids), "unreadable_rows": skipped,
           "rollup": types, "short_weeks": short, "weeks": sources}
    return weekly, log
