"""Monthly figures spread over Monday-to-Sunday weeks, in proportion to the days each week has in the month.

A week that crosses a month boundary is two separate segments (one per month), so a month that starts on a Saturday
has a first segment of 2 days (Sat, Sun) and gets 2/<days in month> of the monthly figure. The segments of a month
always add up to the monthly figure exactly (the last segment takes what the others left).

Applies only to tables whose time level is monthly (`granularity.assess`); anything else is returned as None.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .columns import SYSTEM
from .dataset import Dataset
from .granularity import assess, find_period_column, numeric_columns
from .values import Periods

NEW_COLUMNS = ["Week_Start", "Segment_Start", "Segment_End", "Days", "Days_In_Month"]
PLACES = Decimal("0.0000000001")                 # 10 decimals: the share of a segment, before the remainder is taken


@dataclass(frozen=True)
class Segment:
    week_start: date                             # the Monday of the week this segment belongs to
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def month_segments(year: int, month: int) -> list[Segment]:
    """The Monday-to-Sunday segments of one month, clipped at its first and last day."""
    last = date(year, month, calendar.monthrange(year, month)[1])
    out, day = [], date(year, month, 1)
    while day <= last:
        monday = day - timedelta(days=day.weekday())
        end = min(monday + timedelta(days=6), last)
        out.append(Segment(monday, day, end))
        day = end + timedelta(days=1)
    return out


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def split_value(value, shares: list[int], total_days: int) -> list[float | None]:
    """`value` split in proportion to `shares` (days) of `total_days`; the last part is the remainder, so the parts add up to `value`."""
    if not _is_number(value):
        return [value] * len(shares)
    whole, parts = Decimal(str(value)), []
    for d in shares[:-1]:
        parts.append((whole * d / total_days).quantize(PLACES))
    parts.append(whole - sum(parts, Decimal(0)))
    return [float(p) for p in parts]


def monthly_to_weekly(ds: Dataset, dayfirst: bool = False) -> tuple[Dataset, list[dict]] | None:
    """(weekly table, allocation log) for a table with a real date column and monthly cadence; None for any other table.

    The weekly table keeps every column of the monthly one (labels repeat on each segment) and adds Week_Start,
    Segment_Start, Segment_End, Days and Days_In_Month in front. Every numeric measure column is split by days.
    One log entry per monthly row: the figure, its segments, and whether they add back up to it."""
    a = assess(ds, dayfirst)
    period = find_period_column(ds, dayfirst)
    if a["level"] != "monthly" or period is None:
        return None
    parse = Periods([v for v in ds.data[period] if v is not None], dayfirst)
    measures = numeric_columns(ds, {period})
    cols = [c for c in ds.columns if c not in NEW_COLUMNS]
    out: dict[str, list] = {c: [] for c in ds.data}
    new: dict[str, list] = {c: [] for c in NEW_COLUMNS}
    log, skipped = [], 0
    for i in range(len(ds.row_ids)):
        d = parse(ds.data[period][i]) if ds.data[period][i] is not None else None
        if d is None:
            skipped += 1
            log.append({"row_id": ds.row_ids[i], "month": None, "status": "skipped: the date cannot be read",
                        "segments": 0, "exact": None})
            continue
        segs = month_segments(d.year, d.month)
        days_in_month = calendar.monthrange(d.year, d.month)[1]
        parts = {m: split_value(ds.data[m][i], [s.days for s in segs], days_in_month) for m in measures}
        for k, s in enumerate(segs):
            for c in ds.data:
                if c == "_row_id":
                    out[c].append(f"{ds.row_ids[i]}:w{k + 1}")
                elif c in parts:
                    out[c].append(parts[c][k])
                else:
                    out[c].append(ds.data[c][i])
            for c, v in zip(NEW_COLUMNS, (s.week_start, s.start, s.end, s.days, days_in_month)):
                new[c].append(v)
        exact = all(abs(sum(p for p in parts[m] if p is not None) - ds.data[m][i]) <= 1e-9 * max(1.0, abs(ds.data[m][i]))
                    for m in measures if _is_number(ds.data[m][i]))
        log.append({"row_id": ds.row_ids[i], "month": f"{d.year}-{d.month:02d}", "status": "split",
                    "segments": len(segs), "days": [s.days for s in segs], "exact": exact,
                    "figures": {m: ds.data[m][i] for m in measures if _is_number(ds.data[m][i])}})
    system = [c for c in ds.data if c in SYSTEM]
    data = {**{c: out[c] for c in system}, **new, **{c: out[c] for c in ds.data if c not in SYSTEM}}
    weekly = Dataset(f"{ds.ref}:w", data, [*NEW_COLUMNS, *ds.raw_header], ds.source_file, ds.sheet)
    return weekly, log
