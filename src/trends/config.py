"""Phase D settings and the client config workbook (spec DP-D §2, §3.6).

Settings: built-in defaults, overridden by the `trends:` section of config/settings.yaml, then by explicit overrides.
None of them is a rule threshold: every threshold comes from the client config workbook (spec §3.6), and a rule
without its threshold does not run - Rules_Used and run.json say which.

The client config workbook is shared with Phase C. Phase D reads (sheet names and column headers are matched without
regard to case, spaces or punctuation):
  Data File                   Description | Column; rows Period and Variables (as Phase C; Geography is not used)
  Period Mapping              Period | Months | Quarters | Half Years | Full Years: the Trend Line dates and the label
                              of each date per period type (MOM / QOQ / HOH / YOY)
  Variable Mapping            Variables | Spend Variables | Y/N [| Description | Bucket | Aggregation 1]: Support ->
                              Spend pairs; only Y rows run. No Aggregation 1 column = Bucket.
  Data Check Specifications   optional. Rule (or Description) | Threshold: one row per threshold (THRESHOLDS)
  Ratio Definitions           optional. Ratio Name | Numerator | Denominator | Threshold: N-18, one row per ratio
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import openpyxl
import yaml

from src.common.errors import ConfigError  # noqa: F401

from src.common.values import Periods
from .values import norm

from src.common.values import clean_name, render

DEFAULTS = {
    "dayfirst": False,                # reading of 05/06/2026 text periods without column evidence (as Phase C)
    "client_codes": ["N-16", "N-17", "N-18", "N-19", "N-20"],   # findings the Gate puts in the client report
    "max_sheet_rows": 1_000_000,      # rows per report sheet (Excel holds 1,048,576); findings.json has every finding
    "monthly_to_weekly": True,        # a monthly table also gets a Monday-to-Sunday weekly table (extra output only)
}

PERIOD_TYPES = ["MOM", "QOQ", "HOH", "YOY"]
PERIOD_COLUMNS = {"MOM": ("months", "month"), "QOQ": ("quarters", "quarter"),
                  "HOH": ("half years", "half year"), "YOY": ("full years", "full year")}
# Every threshold of this phase (spec §4): key -> (code, label in Rules_Used, kind). All mandatory, none defaulted.
THRESHOLDS = {
    "robust_z_threshold": ("N-16", "Abnormal Trend - Robust Z-Score", "positive"),
    "mom_pct": ("N-17", "MOM % Change", "percent"),
    "qoq_pct": ("N-17", "QOQ % Change", "percent"),
    "hoh_pct": ("N-17", "HOH % Change", "percent"),
    "yoy_pct": ("N-17", "YOY % Change", "percent"),
    "top_n": ("N-20", "Spend Concentration - Top Tactics Count", "count"),
    "concentration_pct": ("N-20", "Spend Concentration %", "percent"),
}
SPEC_SHEETS = {"data check specifications", "data check spenifications", "specifications"}
# "Month on Month % change" names mom_pct as well as "MOM % change" does.
SPELLED = {"mom": ("month on month",), "qoq": ("quarter on quarter",),
           "hoh": ("half on half", "half year on half year"), "yoy": ("year on year",)}




def load_settings(path: str | None = None, overrides: dict | None = None) -> dict:
    """Defaults < `trends:` section of the YAML file < explicit overrides."""
    cfg = dict(DEFAULTS)
    for src in ((yaml.safe_load(Path(path).read_text(encoding="utf8")) or {}).get("trends", {}) if path else {},
                overrides or {}):
        for k, v in src.items():
            if k not in DEFAULTS:
                raise ValueError(f"unknown trends setting: {k}")
            cfg[k] = v
    return cfg


def rule_key(text) -> str | None:
    """The THRESHOLDS key a Data Check Specifications row names, or None. Accepts the keys themselves (`mom_pct`)
    and the reference script's labels ("MOM % change", "Abnormal trend - Robust Z-score", "Spend concentration %",
    "Spend concentration - Top tactics count"). Phase C's rows ("Month on Month Duplicate" ...) name none."""
    t = norm(str(text).replace("%", " pct "))
    toks = t.split()
    if "_".join(toks) in THRESHOLDS:
        return "_".join(toks)
    if not toks or "duplicate" in toks or "duplicates" in toks:
        return None
    if "abnormal" in toks and "trend" in toks:
        return "robust_z_threshold"
    if "concentration" in toks or "skewness" in toks:
        return "top_n" if "count" in toks or ("top" in toks and "pct" not in toks) else "concentration_pct"
    if "change" in toks or "pct" in toks:
        for p, spelled in SPELLED.items():
            if p in toks or any(s in t for s in spelled):
                return f"{p}_pct"
    return None


# ── reading sheets ───────────────────────────────────────────────────────────
def _filled(v) -> bool:
    return v is not None and str(v).strip() != ""


@dataclass
class _Sheet:
    title: str
    header: list[str]                 # normalised column labels
    rows: list[list]                  # non-empty rows under the header, padded to the header width
    formats: list[list[str]]          # number format of each value (a threshold typed as 30% is stored as 0.3)
    numbers: list[int]                # worksheet row number of each row, for messages

    def col(self, *names: str) -> int | None:
        return next((self.header.index(n) for n in names if n in self.header), None)


def read_sheet(wb, names: set[str]) -> _Sheet | None:
    """The first sheet whose normalised name is in `names`: its first non-empty row is the header. None if absent."""
    title = next((s for s in wb.sheetnames if norm(s) in names), None)
    if title is None:
        return None
    header, rows, formats, numbers = None, [], [], []
    for n, cells in enumerate(wb[title].iter_rows(), start=1):
        vals = [getattr(c, "value", None) for c in cells]
        if header is None:
            if any(_filled(v) for v in vals):
                header = [norm(v) if v is not None else "" for v in vals]
        elif any(_filled(v) for v in vals):
            width = len(header)
            rows.append((vals + [None] * width)[:width])
            formats.append(([getattr(c, "number_format", None) or "" for c in cells] + [""] * width)[:width])
            numbers.append(n)
    return _Sheet(title, header or [], rows, formats, numbers)


def _text(v) -> str:
    return (render(v) or "").strip()


# ── the workbook ─────────────────────────────────────────────────────────────
@dataclass
class PeriodAxis:
    dates: list[date]                              # configured Trend Line dates: distinct, sorted
    labels: dict[str, dict[date, str]]             # MOM / QOQ / HOH / YOY -> date -> label ('' = not grouped)


@dataclass
class Pair:
    support: str                                   # Variables (the Support / performance variable)
    spend: str                                     # Spend Variables ('' = none)
    description: str
    bucket: str
    aggregation: str                               # Aggregation 1 (N-20 grouping); Bucket when the column is absent


@dataclass
class Ratio:
    name: str
    numerator: str
    denominator: str
    threshold: float | None                        # None = not configured: this ratio is not checked (§3.6)
    row: int                                       # its row on the Ratio Definitions sheet


@dataclass
class ClientConfig:
    path: Path
    columns: dict[str, str]                        # period / geography / variables -> letter or header name
    axis: PeriodAxis
    pairs: list[Pair]                              # Y rows of Variable Mapping, in sheet order, repeats dropped
    mapped: set[str]                               # every variable named on Variable Mapping (Support or Spend)
    flagged: set[str]                              # variables named on a Y row
    thresholds: dict[str, float | int | None]      # every THRESHOLDS key; None = not configured
    ratios: list[Ratio] = field(default_factory=list)
    unused: list[tuple[str, str]] = field(default_factory=list)   # (Rule, Threshold) rows no Phase D rule reads


def load_client_config(path, cfg: dict) -> ClientConfig:
    """Read and validate the client config workbook; raises ConfigError naming the sheet and what to fix. A missing
    threshold is not an error: that rule is inactive (spec §3.6, soft requirement)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"client config workbook not found: {path}")
    try:
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    except Exception as e:
        raise ConfigError(f"cannot open the client config workbook {p.name}: {type(e).__name__}: {e}") from e
    try:
        pairs, mapped, flagged = _variable_mapping(wb)
        thresholds, unused = _specifications(wb)
        return ClientConfig(p, _data_file(wb), _period_mapping(wb, cfg), pairs, mapped, flagged, thresholds,
                            _ratios(wb, mapped), unused)
    finally:
        wb.close()


def _data_file(wb) -> dict[str, str]:
    sh = read_sheet(wb, {"data file"})
    if sh is None:
        raise ConfigError("the client config workbook needs a 'Data File' sheet (Description | Column)")
    d, c = sh.col("description"), sh.col("column")
    if d is None or c is None:
        raise ConfigError(f"'{sh.title}' needs Description and Column columns")
    cols: dict[str, str] = {}
    for r in sh.rows:
        cols.setdefault(norm(r[d] or ""), _text(r[c]))
    missing = [k.title() for k in ("period", "variables") if not cols.get(k)]
    if missing:
        raise ConfigError(f"'{sh.title}' needs a Column for: {', '.join(missing)}")
    return {k: cols.get(k, "") for k in ("period", "geography", "variables")}


def _period_mapping(wb, cfg: dict) -> PeriodAxis:
    sh = read_sheet(wb, {"period mapping"})
    if sh is None:
        raise ConfigError("the client config workbook needs a 'Period Mapping' sheet "
                          "(Period | Months | Quarters | Half Years | Full Years)")
    p = sh.col("period", "date")
    cols = {t: sh.col(*names) for t, names in PERIOD_COLUMNS.items()}
    missing = [n[0].title() for t, n in PERIOD_COLUMNS.items() if cols[t] is None]
    if p is None or missing:
        raise ConfigError(f"'{sh.title}' needs Period, Months, Quarters, Half Years and Full Years columns; "
                          f"missing: {', '.join((['Period'] if p is None else []) + missing)}")
    parse = Periods([r[p] for r in sh.rows], cfg["dayfirst"])
    labels: dict[str, dict[date, str]] = {t: {} for t in PERIOD_TYPES}
    bad = []
    for r, n in zip(sh.rows, sh.numbers):
        if not _filled(r[p]):
            continue
        d = parse(r[p])
        if d is None:
            bad.append(f"'{r[p]}' (row {n})")
        elif d not in labels["MOM"]:                       # a repeated date keeps its first labels (as the reference)
            for t in PERIOD_TYPES:
                labels[t][d] = _text(r[cols[t]])
    if bad:
        raise ConfigError(f"'{sh.title}': Period values that are not dates: {', '.join(bad[:5])}"
                          + (f" and {len(bad) - 5} more" if len(bad) > 5 else ""))
    if not labels["MOM"]:
        raise ConfigError(f"'{sh.title}' has no Period dates: the Trend Line axis would be empty")
    return PeriodAxis(sorted(labels["MOM"]), labels)


def _variable_mapping(wb) -> tuple[list[Pair], set[str], set[str]]:
    sh = read_sheet(wb, {"variable mapping"})
    if sh is None:
        raise ConfigError("the client config workbook needs a 'Variable Mapping' sheet (Variables | Spend Variables | Y/N)")
    v, s, yn = sh.col("variables", "variable", "3 3"), sh.col("spend variables", "spend variable"), sh.col("y n")
    if v is None or s is None or yn is None:
        raise ConfigError(f"'{sh.title}' needs Variables, Spend Variables and Y/N columns")
    d, b, a = sh.col("description"), sh.col("bucket"), sh.col("aggregation 1", "aggregation1")
    pairs, mapped, flagged, seen = [], set(), set(), set()
    for r in sh.rows:
        support, spend = clean_name(_text(r[v])), clean_name(_text(r[s]))
        mapped |= {x for x in (support, spend) if x}
        if not support or _text(r[yn]).upper() != "Y":
            continue
        flagged |= {x for x in (support, spend) if x}
        bucket = _text(r[b]) if b is not None else ""
        pair = Pair(support, spend, _text(r[d]) if d is not None else "", bucket,
                    _text(r[a]) if a is not None else bucket)
        key = (pair.support, pair.spend, pair.description, pair.bucket, pair.aggregation)
        if key not in seen:
            seen.add(key)
            pairs.append(pair)
    if not pairs:
        raise ConfigError(f"'{sh.title}': no variables marked Y")
    return pairs, mapped, flagged


def _threshold(v, fmt: str, kind: str, where: str):
    """A Threshold cell -> number (None when blank). Percentages are configured as 30, not 0.30; a cell formatted
    as a percentage (30% is stored as 0.3) and the text '30%' both mean 30."""
    if not _filled(v):
        return None
    if isinstance(v, bool) or isinstance(v, (date, datetime)):
        x = None
    elif isinstance(v, (int, float)):
        x = round(float(v) * 100, 10) if "%" in fmt else float(v)
    else:
        s = str(v).strip().replace("\xa0", "")
        try:
            x = float(s[:-1]) if s.endswith("%") else float(s)
        except ValueError:
            x = None
    if x is None or x != x:
        raise ConfigError(f"{where}: '{v}' is not a number")
    if kind == "count":
        if not x.is_integer() or x < 1:
            raise ConfigError(f"{where}: '{v}' is not a whole number of tactics >= 1")
        return int(x)
    if x < 0 or (kind == "positive" and x == 0):
        raise ConfigError(f"{where}: '{v}' must be {'above' if kind == 'positive' else 'at least'} 0")
    return int(x) if x.is_integer() else x


def _specifications(wb) -> tuple[dict, list]:
    """Every THRESHOLDS key -> its configured value (None = not configured), and the rows with a Threshold no Phase D
    rule reads. The sheet is optional: without it every rule but N-19 is inactive."""
    out: dict[str, float | int | None] = dict.fromkeys(THRESHOLDS)
    sh = read_sheet(wb, SPEC_SHEETS)
    if sh is None:
        return out, []
    text_cols = [i for i in (sh.col("rule"), sh.col("description")) if i is not None]
    if not text_cols:
        raise ConfigError(f"'{sh.title}' needs a Rule (or Description) column")
    t = sh.col("threshold")
    if t is None:
        return out, []
    where_of: dict[str, str] = {}
    unused = []
    for r, fmts, n in zip(sh.rows, sh.formats, sh.numbers):
        label = next((_text(r[i]) for i in text_cols if _filled(r[i])), "")
        key = next((k for k in (rule_key(r[i]) for i in text_cols if _filled(r[i])) if k), None)
        if not label or not _filled(r[t]):
            continue
        where = f"'{sh.title}' row {n} ({label}), Threshold"
        if key is None:
            unused.append((label, render(r[t])))
            continue
        value = _threshold(r[t], fmts[t], THRESHOLDS[key][2], where)
        if out[key] is not None and out[key] != value:
            raise ConfigError(f"'{sh.title}': {THRESHOLDS[key][1]} is configured twice with different thresholds "
                              f"({where_of[key]}: {out[key]}; row {n}: {value}) - keep one row")
        out[key], where_of[key] = value, f"row {n}"
    return out, unused


def _ratios(wb, mapped: set[str]) -> list[Ratio]:
    """Ratio Definitions (optional, N-18). Numerator and Denominator must be variables of Variable Mapping; a ratio
    of other ratios is out of scope for v1 (spec §3.3, open question 1)."""
    sh = read_sheet(wb, {"ratio definitions", "ratio definition", "ratios"})
    if sh is None:
        return []
    c_name, c_num, c_den = sh.col("ratio name", "ratio", "name"), sh.col("numerator"), sh.col("denominator")
    c_thr = sh.col("threshold", "threshold pct")
    if c_num is None or c_den is None:
        raise ConfigError(f"'{sh.title}' needs Numerator and Denominator columns (Ratio Name | Numerator | "
                          "Denominator | Threshold)")
    rows = []
    for r, fmts, n in zip(sh.rows, sh.formats, sh.numbers):
        name = _text(r[c_name]) if c_name is not None else ""
        num, den = clean_name(_text(r[c_num])), clean_name(_text(r[c_den]))
        if not (name or num or den):
            continue
        if not num or not den:
            raise ConfigError(f"'{sh.title}' row {n}: ratio '{name or '?'}' needs both a Numerator and a Denominator")
        thr = _threshold(r[c_thr], fmts[c_thr], "percent", f"'{sh.title}' row {n} ({name}), Threshold") \
            if c_thr is not None else None
        rows.append(Ratio(name or f"{num} / {den}", num, den, thr, n))
    names: dict[str, int] = {}
    for x in rows:
        if x.name.casefold() in names:
            raise ConfigError(f"'{sh.title}': ratio name '{x.name}' is used on rows {names[x.name.casefold()]} and "
                              f"{x.row} - each ratio needs its own name")
        names[x.name.casefold()] = x.row
    for x in rows:
        for part, var in (("Numerator", x.numerator), ("Denominator", x.denominator)):
            if var in mapped:
                continue
            if var.casefold() in names:
                raise ConfigError(f"'{sh.title}' row {x.row}: {part} '{var}' is another ratio - a ratio of ratios "
                                  "is not supported (Numerator and Denominator must be mapped variables)")
            raise ConfigError(f"'{sh.title}' row {x.row}: {part} '{var}' is not a variable of Variable Mapping")
    return rows
