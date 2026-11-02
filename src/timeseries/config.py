"""Phase C settings and the client config workbook.

Settings: built-in defaults, overridden by the `timeseries:` section of config/settings.yaml, then by explicit overrides.

The client config workbook is shared with Phase D. Phase C reads four sheets of it (names and column headers are
matched without regard to case, spaces or punctuation):
  Data File                   Description | Column; rows Period, Geography, Variables. Column is a letter (A = the
                              table's first column) or a header name. Variables = that column and every one after it.
  Variable Mapping            Variables | Bucket [| Description]. A variable is checked only with a usable Bucket.
  Period Mapping              Period | Months: the month label of each date (MOM_Duplicates).
  Data Check Specifications   optional. Description (or Rule) | From | To | No of Weeks: date ranges for the
                              No Executions, Week on Week and Month on Month rows, and N-07's run length.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import openpyxl
import yaml

from src.common.errors import ConfigError  # noqa: F401

from src.common.values import Periods
from src.common.values import clean_name, render

DEFAULTS = {
    "min_zero_weeks": 9,              # N-07 run length in periods; the workbook's "No of Weeks" overrides it
    "min_repeat_run": 2,              # N-08: periods a fractional value must hold in a row to be flagged
    "min_repeat_run_whole": 3,        # N-08: the same for a whole number (counts repeat by chance more often)
    "dayfirst": False,                # reading of 05/06/2026 text periods without column evidence (reference: month first)
    "require_geography": True,        # the Data File sheet must map Geography (a label column of each series)
    "ignore_buckets": ["", "0", "#N/A", "N/A", "NA", "NULL", "NONE"],   # a variable with such a Bucket is not checked
    "null_tokens": ["NULL", "N/A", "NA", "NONE", "NAN"],                 # N-03, case-insensitive
    "client_codes": ["N-07", "N-10", "N-12"],   # findings the Gate puts in the client report
    "max_sheet_rows": 100_000,        # rows per findings sheet; every finding is in findings.json regardless
    "monthly_to_weekly": True,        # a table that is monthly is turned into a weekly one before the checks
    "average_words": ["avg", "average", "mean", "rate", "ratio", "pct", "percent", "margin", "aov", "price", "index",
                      "share", "ctr", "cpc", "cpm", "roas", "temperature", "temp"],   # a column with such a word in its
                                          # name is averaged, not split, when a monthly table becomes weekly
}

# Data Check Specifications rows Phase C uses: code -> (word sets, any of which names the row; abbreviation that names
# it together with "duplicate"). "Week on Week duplicates" and "WOW Duplicate" both name the N-08 row.
SPEC_RULES = {
    "N-07": ([("no", "executions"), ("no", "execution")], None),
    "N-08": ([("week", "on", "week")], "wow"),
    "N-09": ([("month", "on", "month")], "mom"),
}
SPEC_SHEETS = {"data check specifications", "data check spenifications", "specifications"}




def load_settings(path: str | None = None, overrides: dict | None = None) -> dict:
    """Defaults < `timeseries:` section of the YAML file < explicit overrides."""
    cfg = dict(DEFAULTS)
    for src in ((yaml.safe_load(Path(path).read_text(encoding="utf8")) or {}).get("timeseries", {}) if path else {},
                overrides or {}):
        for k, v in src.items():
            if k not in DEFAULTS:
                raise ValueError(f"unknown timeseries setting: {k}")
            cfg[k] = v
    return cfg


def norm(v) -> str:
    """Sheet / column / rule label for matching: lower case, punctuation and spaces collapsed."""
    return re.sub(r"[^a-z0-9]+", " ", str(v).replace("\xa0", " ").lower()).strip()


@dataclass
class _Sheet:
    title: str
    header: list[str]                 # normalised column labels
    rows: list[list]                  # non-empty rows under the header, padded to the header width

    def col(self, *names: str) -> int | None:
        return next((self.header.index(n) for n in names if n in self.header), None)


def _filled(v) -> bool:
    return v is not None and str(v).strip() != ""


def read_sheet(wb, names: set[str]) -> _Sheet | None:
    """The first sheet whose normalised name is in `names`: its first non-empty row is the header. None if absent."""
    title = next((s for s in wb.sheetnames if norm(s) in names), None)
    if title is None:
        return None
    header, rows = None, []
    for vals in wb[title].iter_rows(values_only=True):
        if header is None:
            if any(_filled(v) for v in vals):
                header = [norm(v) if v is not None else "" for v in vals]
        elif any(_filled(v) for v in vals):
            rows.append((list(vals) + [None] * len(header))[:len(header)])
    return _Sheet(title, header or [], rows)


@dataclass
class ClientConfig:
    path: Path
    columns: dict[str, str]                     # period / geography / variables -> letter or header name
    mapping: dict[str, dict]                    # variable name -> {"bucket", "description"} (first row wins)
    month_by_text: dict[str, str]               # Period Mapping: period text -> month label
    month_by_date: dict[date, str]              # Period Mapping: date -> month label
    ranges: dict[str, tuple] = field(default_factory=dict)   # N-07 / N-08 / N-09 -> (from, to); None = open
    min_weeks: int | None = None                # N-07 run length from the workbook (None = settings default)
    rollup: dict[str, str] = field(default_factory=dict)     # Column Setup: column -> "sum" | "average" (monthly tables)


def load_client_config(path, cfg: dict) -> ClientConfig:
    """Read and validate the client config workbook; raises ConfigError naming the sheet and what is missing."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"client config workbook not found: {path}")
    try:
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    except Exception as e:
        raise ConfigError(f"cannot open the client config workbook {p.name}: {type(e).__name__}: {e}") from e
    try:
        by_text, by_date = _period_mapping(wb, cfg)
        ranges, min_weeks = _specifications(wb, cfg)
        return ClientConfig(p, _data_file(wb, cfg), _variable_mapping(wb), by_text, by_date, ranges, min_weeks,
                            _column_setup(wb))
    finally:
        wb.close()


def _data_file(wb, cfg: dict) -> dict[str, str]:
    sh = read_sheet(wb, {"data file"})
    if sh is None:
        raise ConfigError("the client config workbook needs a 'Data File' sheet (Description | Column)")
    d, c = sh.col("description"), sh.col("column")
    if d is None or c is None:
        raise ConfigError(f"'{sh.title}' needs Description and Column columns")
    cols: dict[str, str] = {}
    for r in sh.rows:
        cols.setdefault(norm(r[d] or ""), (render(r[c]) or "").strip())
    required = ["period", "variables"] + (["geography"] if cfg["require_geography"] else [])
    missing = [k.title() for k in required if not cols.get(k)]
    if missing:
        raise ConfigError(f"'{sh.title}' needs a Column for: {', '.join(missing)}")
    return {k: cols.get(k, "") for k in ("period", "geography", "variables")}


def _variable_mapping(wb) -> dict[str, dict]:
    sh = read_sheet(wb, {"variable mapping"})
    if sh is None:
        raise ConfigError("the client config workbook needs a 'Variable Mapping' sheet (Variables | Bucket)")
    v, b, d = sh.col("variables", "variable", "3 3"), sh.col("bucket"), sh.col("description")
    if v is None or b is None:
        raise ConfigError(f"'{sh.title}' needs Variables and Bucket columns")
    out: dict[str, dict] = {}
    for r in sh.rows:
        name = clean_name(render(r[v]))
        if name and name not in out:
            out[name] = {"bucket": (render(r[b]) or "").strip(),
                         "description": (render(r[d]) or "").strip() if d is not None else ""}
    return out


def _period_mapping(wb, cfg: dict) -> tuple[dict, dict]:
    sh = read_sheet(wb, {"period mapping"})
    if sh is None:
        raise ConfigError("the client config workbook needs a 'Period Mapping' sheet (Period | Months)")
    p, m = sh.col("period", "date"), sh.col("months", "month")
    if p is None or m is None:
        raise ConfigError(f"'{sh.title}' needs Period and Months columns")
    parse = Periods([r[p] for r in sh.rows], cfg["dayfirst"])
    by_text, by_date = {}, {}
    for r in sh.rows:
        label = (render(r[m]) or "").strip()
        if label:
            by_text.setdefault((render(r[p]) or "").strip(), label)
            if (d := parse(r[p])) is not None:
                by_date.setdefault(d, label)
    return by_text, by_date


def _column_setup(wb) -> dict[str, str]:
    """Column Setup (optional): Column | Roll-up. How each measure becomes weekly when the table is monthly: sum (the
    monthly figure is split over its weeks) or average (a rate, the same in every week). Blank = guessed from the name."""
    from src.common.weekly_rollup import ROLLUPS
    sh = read_sheet(wb, {"column setup"})
    if sh is None:
        return {}
    c, r = sh.col("column", "variables", "variable"), sh.col("roll up", "rollup", "roll up type", "aggregation")
    if c is None or r is None:
        raise ConfigError(f"'{sh.title}' needs Column and Roll-up columns")
    out: dict[str, str] = {}
    for row in sh.rows:
        name, kind = clean_name(render(row[c])), norm(row[r]) if _filled(row[r]) else ""
        if not name or not kind:
            continue
        if kind not in ROLLUPS:
            raise ConfigError(f"'{sh.title}', {name}: Roll-up '{row[r]}' must be Sum or Average")
        out[name] = ROLLUPS[kind]
    return out


def _find_rule(sh: _Sheet, text_cols: list[int], phrases, abbrev) -> list | None:
    """The specification row a rule is configured on; a row that also says "duplicate" wins over one that does not."""
    hits = []
    for r in sh.rows:
        toks = " ".join(norm(r[i]) for i in text_cols if r[i] is not None).split()
        if any(all(w in toks for w in ph) for ph in phrases) or (abbrev and abbrev in toks and "duplicate" in toks):
            hits.append(("duplicate" not in toks, r))
    return min(hits, key=lambda h: h[0])[1] if hits else None


def _spec_date(v, cfg: dict, where: str) -> date | None:
    if not _filled(v):
        return None
    if isinstance(v, (date, datetime)):
        return v.date() if isinstance(v, datetime) else v
    d = Periods([v], cfg["dayfirst"])(v)
    if d is None:
        raise ConfigError(f"{where}: '{v}' is not a date")
    return d


def _specifications(wb, cfg: dict) -> tuple[dict, int | None]:
    """Date ranges for N-07 / N-08 / N-09 and N-07's run length; the sheet is optional."""
    sh = read_sheet(wb, SPEC_SHEETS)
    if sh is None:
        return {}, None
    text_cols = [i for i in (sh.col("description"), sh.col("rule")) if i is not None]
    if not text_cols:
        raise ConfigError(f"'{sh.title}' needs a Description (or Rule) column")
    f, t = sh.col("from"), sh.col("to")
    w = sh.col("no of weeks", "number of weeks", "no of week", "weeks", "min weeks")
    ranges, min_weeks = {}, None
    for code, (phrases, abbrev) in SPEC_RULES.items():
        row = _find_rule(sh, text_cols, phrases, abbrev)
        if row is None:
            continue
        label = next(str(row[i]) for i in text_cols if _filled(row[i]))
        lo = _spec_date(row[f], cfg, f"'{sh.title}', {label}, From") if f is not None else None
        hi = _spec_date(row[t], cfg, f"'{sh.title}', {label}, To") if t is not None else None
        if lo or hi:
            ranges[code] = (lo, hi)
        if code == "N-07" and w is not None and _filled(row[w]):
            try:
                min_weeks = int(float(str(row[w]).strip()))
            except ValueError:
                min_weeks = 0
            if min_weeks < 1:
                raise ConfigError(f"'{sh.title}', {label}, No of Weeks: '{row[w]}' is not a whole number of weeks >= 1")
    return ranges, min_weeks
