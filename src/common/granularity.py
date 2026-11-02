"""What time level is a table at (daily, weekly, monthly, quarterly, yearly, or none), and does it have numbers to check?

Phases C and D need a date column and at least one numeric variable. Phase C's checks are also weekly by definition
(consecutive-week runs, week-on-week repeats). This module answers, for any table, what it actually is, so the app can
say "this is monthly data, not weekly" and let the analyst continue instead of failing with a vague error.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import replace
from datetime import date

from src.hygiene.values import has_keyword

from .dataset import Dataset
from .values import YEARS, Periods, classify

MONTH_NAMES = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
YEAR_MONTH_TEXT = [re.compile(p) for p in (r"\d{4}[-/ ]\d{1,2}", r"\d{1,2}[-/]\d{4}", r"[A-Za-z]{3,9}[-'/ ]{1,2}\d{2,4}")]
DATE_WORDS = ["date", "week", "month", "period", "day", "time"]
ID_WORDS = ["id", "code", "key", "ref"]
AGGREGATE = re.compile(r"\s*(count|sum|avg|average|total|number|num|no)(?![a-z])", re.I)     # a name that starts like an aggregate
MIN_SHARE = 0.8


def _texts(ds: Dataset, col: str, limit: int = 5000) -> list:
    return [v for v in ds.data[col] if v is not None and str(v).strip() != ""][:limit]


def find_period_column(ds: Dataset, dayfirst: bool = False) -> str | None:
    """The first column that is mostly dates. A column of plain whole numbers counts only if its name says it is a date."""
    for c in ds.columns:
        vals = _texts(ds, c)
        if not vals:
            continue
        parse = Periods(vals, dayfirst)
        share = sum(parse(v) is not None for v in vals) / len(vals)
        all_numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)
        if share >= MIN_SHARE and (not all_numbers or has_keyword(c, DATE_WORDS)):
            return c
    return None


def _cadence(dates: list[date]) -> str:
    """Level from the median gap between the distinct dates."""
    days = sorted(set(dates))
    if len(days) < 3:
        return "unknown"
    gap = statistics.median((b - a).days for a, b in zip(days, days[1:]))
    for level, lo, hi in (("daily", 0, 2), ("weekly", 5, 9), ("monthly", 27, 32), ("quarterly", 85, 95), ("yearly", 355, 370)):
        if lo < gap <= hi:
            return level
    return "irregular"


def _is_year_column(vals: list) -> bool:
    ok = 0
    for v in vals:
        s = str(v).strip().removesuffix(".0")
        ok += s.isdigit() and 1900 <= int(s) <= 2100
    return bool(vals) and ok / len(vals) >= MIN_SHARE


def _is_month_column(vals: list) -> bool:
    ok = 0
    for v in vals:
        s = str(v).strip().removesuffix(".0").lower()
        ok += (s.isdigit() and 1 <= int(s) <= 12) or s[:3] in MONTH_NAMES
    return bool(vals) and ok / len(vals) >= MIN_SHARE


def _is_year_month_text(vals: list) -> bool:
    ok = sum(any(p.fullmatch(str(v).strip()) for p in YEAR_MONTH_TEXT) for v in vals)
    return bool(vals) and ok / len(vals) >= MIN_SHARE


def is_time_label(ds: Dataset, col: str) -> bool:
    """A Year or Month column (named so, and holding years or months): a label of time, not a measure."""
    name, vals = col.lower(), _texts(ds, col)
    return bool(vals) and (("year" in name and _is_year_column(vals)) or ("month" in name and _is_month_column(vals)))


def is_year_month_label(ds: Dataset, col: str) -> bool:
    """A column of Year-Month text (2024-06, Jun-24, 06/2024): a label of time, like a Year or Month column."""
    vals = _texts(ds, col)
    return bool(vals) and _is_year_month_text(vals)


def numeric_columns(ds: Dataset, skip: set[str]) -> list[str]:
    """Columns that hold mostly numbers (identifiers and the time columns are not measures)."""
    out = []
    for c in ds.columns:
        if c in skip or is_time_label(ds, c) or (has_keyword(c, ID_WORDS) and not AGGREGATE.match(c)):      # COUNT(CUSTOMERS.ID) is a measure
            continue
        kinds = [classify(v)[0] for v in ds.data[c]]
        filled = [k for k in kinds if k != "blank"]
        if filled and sum(k == "number" for k in filled) / len(filled) >= 0.5:
            out.append(c)
    return out


def assess(ds: Dataset, dayfirst: bool = False) -> dict:
    """{level, period, measures, applicable_c, applicable_d, message}.

    level: daily | weekly | monthly | quarterly | yearly | irregular | unknown | none
    A table with a date column gets its level from the gaps between dates; without one, a Year-Month text column, or a
    Year and a Month column, says monthly; a Year column alone says yearly.
    """
    period = find_period_column(ds, dayfirst)
    time_cols: set[str] = set()
    level, label = "none", None
    if period:
        parse = Periods(_texts(ds, period), dayfirst)
        level, label = _cadence([d for d in (parse(v) for v in _texts(ds, period)) if d]), period
        time_cols.add(period)
    else:
        by_kind = {c: _texts(ds, c) for c in ds.columns}
        ym = next((c for c, v in by_kind.items() if v and _is_year_month_text(v)), None)
        year = next((c for c, v in by_kind.items() if v and "year" in c.lower() and _is_year_column(v)), None)
        month = next((c for c, v in by_kind.items() if v and "month" in c.lower() and _is_month_column(v)), None)
        if ym:
            level, label = "monthly", ym
        elif year and month:
            level, label = "monthly", f"{year} + {month}"
        elif year:
            level, label = "yearly", year
        time_cols = {c for c in (ym, year, month) if c}
    measures = numeric_columns(ds, time_cols)
    has_date = period is not None
    return {"level": level, "period": label, "measures": len(measures),
            "applicable_c": has_date and bool(measures),          # a date column and a measure: as before, any cadence runs
            "applicable_d": has_date and bool(measures),
            "message": _message(level, label, has_date, len(measures))}


def _message(level: str, label: str | None, has_date: bool, measures: int) -> str:
    if level == "none":
        what = "No date or month/year column was found."
    elif level in ("irregular", "unknown"):
        what = f"The time column '{label}' has no clear cadence."
    elif level == "weekly":
        what = f"The data is weekly (column '{label}')."
    else:
        what = f"The data is {level}, not weekly (time column: {label})."
    extra = []
    if not has_date and level != "none":
        extra.append("It has no real date column, only year/month labels.")
    if not measures:
        extra.append("It has no numeric measure columns to trend or compare.")
    return " ".join([what, *extra])


# ── a real date for data that only has Year / Month labels ──────────────────────────────────────────────────────────
DERIVED = "Period"
_YM = [(re.compile(r"(\d{4})[-/ ](\d{1,2})"), "ym"), (re.compile(r"(\d{1,2})[-/](\d{4})"), "my"),
       (re.compile(r"([A-Za-z]{3,9})[-'/ ]{1,2}(\d{2,4})"), "name")]


def _year_month(v) -> tuple[int, int] | None:
    s = str(v).strip()
    for rx, kind in _YM:
        m = rx.fullmatch(s)
        if not m:
            continue
        a, b = m.groups()
        if kind == "ym":
            y, mo = int(a), int(b)
        elif kind == "my":
            y, mo = int(b), int(a)
        else:
            mo, y = MONTH_NAMES.get(a[:3].lower(), 0), int(b)
            y = y + 2000 if y < 100 else y
        return (y, mo) if 1 <= mo <= 12 and y in YEARS else None
    return None


def _month_number(v) -> int | None:
    s = str(v).strip().removesuffix(".0").lower()
    if s.isdigit() and 1 <= int(s) <= 12:
        return int(s)
    return MONTH_NAMES.get(s[:3])


def with_period(ds: Dataset) -> Dataset:
    """`ds` with a real date column added in front when it has none but does carry month or year labels
    (a Year-Month label, Year + Month columns, or a Year column): the first day of that month (or year).
    A table that already has a date column, or has no time labels, is returned as it is."""
    if find_period_column(ds) is not None:
        return ds
    cols = {c: _texts(ds, c) for c in ds.columns}
    ym = next((c for c, v in cols.items() if v and _is_year_month_text(v)), None)
    year = next((c for c, v in cols.items() if v and "year" in c.lower() and _is_year_column(v)), None)
    month = next((c for c, v in cols.items() if v and "month" in c.lower() and _is_month_column(v)), None)
    dates: list[date | None]
    if ym:
        pairs = [_year_month(v) if v is not None else None for v in ds.data[ym]]
        dates = [date(y, m, 1) if p else None for p in pairs for y, m in [p or (0, 0)]]
    elif year and month:
        dates = []
        for y, m in zip(ds.data[year], ds.data[month]):
            mo = _month_number(m) if m is not None else None
            yr = int(str(y).strip().removesuffix(".0")) if y is not None and str(y).strip().removesuffix(".0").isdigit() else None
            dates.append(date(yr, mo, 1) if yr in YEARS and mo else None)
    elif year:
        dates = [date(int(str(y).strip().removesuffix(".0")), 1, 1)
                 if y is not None and str(y).strip().removesuffix(".0").isdigit() and int(str(y).strip().removesuffix(".0")) in YEARS else None
                 for y in ds.data[year]]
    else:
        return ds
    name = DERIVED if DERIVED not in ds.data else f"{DERIVED} (from labels)"
    data = {}
    for k, v in ds.data.items():
        data[k] = v
        if k == "_source_row":                                  # system columns first, then the new date, then the rest
            data[name] = dates
    return replace(ds, data=data, raw_header=[name, *ds.raw_header])
