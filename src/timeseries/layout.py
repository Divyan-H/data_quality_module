"""Which table Phase C checks, and which of its columns are the Period, Geography and Variables (Data File sheet)."""
from __future__ import annotations

from dataclasses import dataclass

from src.common.dataset import locate

from .config import ClientConfig

from src.common.errors import ConfigError
from src.common.dataset import Dataset
from src.common.values import Periods
from src.common.values import clean_name, letter


@dataclass
class Layout:
    period: str                       # column names in the table
    geography: str | None
    series: list[str]                 # label columns: before the Variables range (Period excepted), plus Geography
    variables: list[str]              # the Variables range: that column and every later one with a header
    raw: dict[str, str]               # variable column -> header text in the source (repeats kept, for N-15)
    letters: dict[str, str]           # variable column -> its letter in the table (A = first column)
    repeated: set[str]                # variable columns that repeat an earlier header (named name__2 ...)


def resolve_layout(ds: Dataset, cc: ClientConfig, cfg: dict) -> Layout:
    """Map the Data File sheet onto the table. A letter counts the table's columns (A = its first column)."""
    cols, raw = ds.columns, ds.raw_header
    if not ds.row_ids:
        raise ConfigError(f"table {ds.ref} has no rows to check")
    p = locate(cc.columns["period"], "Period", ds)
    g = locate(cc.columns["geography"], "Geography", ds) if cc.columns.get("geography") else None
    v = locate(cc.columns["variables"], "Variables", ds)
    period = cols[p]
    parse = Periods(ds.data[period], cfg["dayfirst"])
    if not any(parse(x) for x in ds.data[period]):
        raise ConfigError(f"the Period column '{period}' (column {letter(p)}) holds no dates: check the Data File "
                          "sheet (a letter counts the checked table's columns, A = its first column)")
    idx = [i for i in range(v, len(cols)) if raw[i].strip() and i not in (p, g)]
    seen: set[str] = set()
    repeated = set()
    for i in idx:
        key = clean_name(raw[i]).casefold()
        if key in seen and cols[i] != clean_name(raw[i]):
            repeated.add(cols[i])
        seen.add(key)
    series = [cols[i] for i in range(len(cols)) if i != p and (i < v or i == g)]
    return Layout(period, cols[g] if g is not None else None, series, [cols[i] for i in idx],
                  {cols[i]: raw[i] for i in idx}, {cols[i]: letter(i) for i in idx}, repeated)


def why_not(ds: Dataset, cc: ClientConfig, cfg: dict) -> str | None:
    """Why the client config does not describe this table (no Period column of mostly dates, or no variable of
    Variable Mapping), or None when it does."""
    try:
        lay = resolve_layout(ds, cc, cfg)
    except ConfigError as e:
        return str(e)
    parse = Periods(ds.data[lay.period], cfg["dayfirst"])
    vals = [x for x in ds.data[lay.period] if x is not None]
    if not vals or sum(parse(x) is not None for x in vals) / len(vals) < 0.5:
        return f"fewer than half of the '{lay.period}' values are dates"
    if not any(clean_name(c) in cc.mapping for c in lay.variables):
        return "none of its variables is in Variable Mapping"
    return None

