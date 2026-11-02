"""What the project tells us after the files are uploaded: the columns to expect and to check, the city list, the currency.

Today the answers live in a YAML file (config/run_inputs.yaml is the default; a run keeps its own copy at
<run>/config/run_inputs.yaml). The web app reads and writes the same file through `GET / PUT /api/v1/runs/{id}/inputs`, so the
screen that asks these questions later needs no other storage. Every module can read it with `load_for_run`.

    project: Porter
    expected_columns:                 # a column listed here and missing from a sheet is flagged (rule D-32)
      all_sheets: [Date, City, Spend]
      sheets: {Daily_Media: [Clicks]}  # a sheet name, file name or table ref
      strict: false                    # true: columns that are not listed are mentioned too
    columns_to_check:                  # which columns the cell rules look at; a table not listed is checked in full
      - table: "*"
        columns: all
      - table: Daily_Media
        columns: [Spend, Clicks]
    cities: [Bangalore, Mumbai]        # the project's own city list (rule D-30); empty = no list
    currency:
      default: INR
      rates_level: quarterly           # quarterly | daily: how the rates below are given
      rates:                           # one unit of the currency = this many units of the default currency
        USD: {2024-Q1: 83.1, 2024-Q2: 83.4}
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "config" / "run_inputs.yaml"
RUN_FILE = Path("config") / "run_inputs.yaml"          # inside a run folder
LEVELS = ("quarterly", "daily")
_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")


def empty() -> dict:
    return {"project": None, "expected_columns": {"all_sheets": [], "sheets": {}, "strict": False},
            "columns_to_check": [], "cities": [], "currency": {"default": None, "rates_level": None, "rates": {}}}


def _names(value, where: str, problems: list[str]) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(x, (str, int, float)) and str(x).strip() for x in value):
        problems.append(f"{where} must be a list of names")
        return []
    return [str(x).strip() for x in value]


def _rate_key(key, level: str, where: str, problems: list[str]) -> str | None:
    text = str(key).strip()
    if level == "quarterly":
        if not _QUARTER.match(text):
            problems.append(f"{where}: '{text}' is not a quarter like 2024-Q1")
            return None
        return text
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        problems.append(f"{where}: '{text}' is not a date like 2024-03-15")
        return None


def parse(raw) -> tuple[dict, list[str]]:
    """(normalised inputs, problems). `raw` is YAML text or an already loaded dict. Problems name what to fix; with any
    problem the inputs must not be used."""
    problems: list[str] = []
    if isinstance(raw, (str, bytes)):
        try:
            raw = yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            return empty(), [f"not valid YAML: {str(e).splitlines()[0]}"]
    if not isinstance(raw, dict):
        return empty(), ["the file must hold a mapping (project, expected_columns, columns_to_check, cities, currency)"]
    out = empty()
    unknown = sorted(set(raw) - set(out))
    if unknown:
        problems.append(f"unknown key(s): {', '.join(map(str, unknown))}")
    if raw.get("project") not in (None, ""):
        out["project"] = str(raw["project"]).strip()

    exp = raw.get("expected_columns") or {}
    if not isinstance(exp, dict):
        problems.append("expected_columns must hold all_sheets, sheets and strict")
        exp = {}
    out["expected_columns"]["all_sheets"] = _names(exp.get("all_sheets"), "expected_columns.all_sheets", problems)
    sheets = exp.get("sheets") or {}
    if not isinstance(sheets, dict):
        problems.append("expected_columns.sheets must map a sheet name to its columns")
        sheets = {}
    out["expected_columns"]["sheets"] = {str(k): _names(v, f"expected_columns.sheets.{k}", problems) for k, v in sheets.items()}
    out["expected_columns"]["strict"] = bool(exp.get("strict", False))

    choice = raw.get("columns_to_check") or []
    if not isinstance(choice, list):
        problems.append("columns_to_check must be a list of {table, columns}")
        choice = []
    for n, entry in enumerate(choice, 1):
        if not isinstance(entry, dict) or not str(entry.get("table", "")).strip():
            problems.append(f"columns_to_check item {n} needs a table (a sheet name, a file name or *)")
            continue
        cols = entry.get("columns")
        if cols in (None, "all", "All", "*"):
            names = None
        else:
            names = _names(cols, f"columns_to_check item {n} columns", problems)
        out["columns_to_check"].append({"table": str(entry["table"]).strip(), "columns": names})

    out["cities"] = _names(raw.get("cities"), "cities", problems)

    cur = raw.get("currency") or {}
    if not isinstance(cur, dict):
        problems.append("currency must hold default, rates_level and rates")
        cur = {}
    default = cur.get("default")
    if default not in (None, ""):
        if not re.fullmatch(r"[A-Za-z]{3}", str(default).strip()):
            problems.append(f"currency.default '{default}' must be a 3-letter code such as INR")
        else:
            out["currency"]["default"] = str(default).strip().upper()
    level = cur.get("rates_level")
    rates = cur.get("rates") or {}
    if rates and level not in LEVELS:
        problems.append(f"currency.rates_level must be one of {', '.join(LEVELS)} (how the rates are given), found '{level}'")
    elif level in LEVELS:
        out["currency"]["rates_level"] = level
    if rates and not isinstance(rates, dict):
        problems.append("currency.rates must map a currency code to its rates")
        rates = {}
    for code, table in (rates.items() if isinstance(rates, dict) else []):
        if not re.fullmatch(r"[A-Za-z]{3}", str(code).strip()):
            problems.append(f"currency.rates: '{code}' is not a 3-letter code")
            continue
        if not isinstance(table, dict) or not table:
            problems.append(f"currency.rates.{code} must map {'quarters' if level == 'quarterly' else 'dates'} to rates")
            continue
        clean = {}
        for k, v in table.items():
            key = _rate_key(k, level, f"currency.rates.{code}", problems) if level in LEVELS else None
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                problems.append(f"currency.rates.{code}.{k}: '{v}' must be a number above 0")
            elif key:
                clean[key] = float(v)
        out["currency"]["rates"][str(code).strip().upper()] = clean
    if out["currency"]["rates"] and not out["currency"]["default"]:
        problems.append("currency.default is needed when rates are given (the rates convert into it)")
    return out, problems


def columns_for(spec: list[dict], ref: str | None, sheet: str | None, file: str | None) -> list[str] | None:
    """The columns to check in one table: None = every column. An entry that names the table's sheet, file or ref wins
    over `*`; the first matching entry of the same kind is used."""
    names = {str(x).casefold() for x in (ref, sheet, file) if x}
    star = None
    for entry in spec or []:
        table = entry["table"].casefold()
        if table in names:
            return entry["columns"]
        if table == "*" and star is None:
            star = entry
    return star["columns"] if star else None


def rate_for(inputs: dict, code: str, on: date) -> float | None:
    """One unit of `code` in the default currency on `on`: 1.0 for the default currency itself, the rate of that day
    (daily level) or of its quarter (quarterly level), None when the rates have no entry."""
    cur = inputs["currency"]
    code = code.upper()
    if code == cur["default"]:
        return 1.0
    table = cur["rates"].get(code)
    if not table or not cur["rates_level"]:
        return None
    if cur["rates_level"] == "daily":
        return table.get(on.isoformat())
    return table.get(f"{on.year}-Q{(on.month - 1) // 3 + 1}")


def load_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf8") if Path(path).exists() else ""


def run_path(run_dir) -> Path:
    return Path(run_dir) / RUN_FILE


def load_for_run(run_dir) -> tuple[dict, str]:
    """(inputs, where they came from: "run" or "default"). A saved file with problems is not used: the default stands."""
    mine = run_path(run_dir)
    if mine.exists():
        parsed, problems = parse(mine.read_text(encoding="utf8"))
        if not problems:
            return parsed, "run"
    parsed, problems = parse(load_text(DEFAULT_PATH))
    return (parsed if not problems else empty()), "default"


def save_for_run(run_dir, raw) -> tuple[dict, list[str]]:
    """Validate and store the inputs of a run (YAML text keeps its comments; a dict is written as YAML). Nothing is saved
    when there are problems."""
    parsed, problems = parse(raw)
    if problems:
        return parsed, problems
    path = run_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = raw if isinstance(raw, str) else yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf8")
    return parsed, []


def hygiene_overrides(inputs: dict) -> dict:
    """The Phase B settings these inputs set."""
    out: dict = {}
    exp = inputs["expected_columns"]
    if exp["all_sheets"] or exp["sheets"]:
        out["expected_columns"] = exp
    if inputs["columns_to_check"]:
        out["columns_to_check"] = inputs["columns_to_check"]
    if inputs["cities"]:
        out["city_master"] = inputs["cities"]
    if inputs["currency"].get("default"):
        out["default_currency"] = inputs["currency"]["default"]
    return out
