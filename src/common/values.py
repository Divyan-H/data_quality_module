"""Value helpers shared by the phases: one text form per value, numeric tests, Excel names and finding ids."""
from __future__ import annotations

import calendar
import hashlib
import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils import column_index_from_string, get_column_letter

from src.hygiene.values import EXCEL_EPOCH, parse_date, slash_dayfirst

NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")    # a numeric text value (whole value, after trimming)
SERIALS = range(18264, 60001)                                # Excel date serials accepted in a Period column (1950 .. 2064)
YEARS = range(1900, 2101)                                   # years accepted for any Period value (strftime also needs >= 1900 on Windows)
MAX_CELL = 32000                                            # Excel holds at most 32,767 characters per cell


def render(v) -> str | None:
    """One text form per value: dates as ISO, whole floats without '.0', no scientific notation, '' as None."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        if math.isnan(v):
            return None
        return str(int(v)) if v.is_integer() else format(Decimal(repr(v)), "f")
    return str(v)


def excel_text(v):
    """A value as it can be written to a cell: control characters removed, long text cut at MAX_CELL."""
    if not isinstance(v, str):
        return v
    v = ILLEGAL_CHARACTERS_RE.sub("", v)
    return v if len(v) <= MAX_CELL else v[:MAX_CELL] + " ..."


def classify(v) -> tuple[str, float | None]:
    """('blank' | 'number' | 'text', value). A typed number is a number; text is a number only when it matches
    NUMBER_RE after trimming (no thousands separators, currency or exponent - Phase B removes those)."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "blank", None
    if isinstance(v, (bool, date)):
        return "text", None
    if isinstance(v, (int, float)):
        return ("number", float(v)) if math.isfinite(v) else ("text", None)
    s = str(v).strip()
    if not s:
        return "blank", None
    return ("number", float(s)) if NUMBER_RE.fullmatch(s) else ("text", None)


def number(x: float):
    """A number for display: 12.0 -> 12."""
    return int(x) if float(x).is_integer() else x


def clean_name(s) -> str:
    """Header / variable name with whitespace collapsed: how names are matched against Variable Mapping."""
    return " ".join(str(s).split()) if s is not None else ""


def letter(index: int) -> str:
    """0-based column index -> Excel letter."""
    return get_column_letter(index + 1)


def letter_index(ref: str) -> int:
    """Excel letter -> 0-based column index."""
    return column_index_from_string(ref.upper()) - 1


def finding_id(code: str, key: tuple) -> str:
    """Stable id of a finding: the same issue in the same data gives the same id on every run."""
    return f"{code}-{hashlib.sha1(repr((code, key)).encode('utf8')).hexdigest()[:12]}"


def same_value(old, new) -> bool:
    """Is the uploaded value what was exported? Excel keeps 15 significant digits, and the export cleaned text."""
    if old is None or new is None:
        return old is None and new is None
    nums = [isinstance(x, (int, float)) and not isinstance(x, bool) for x in (old, new)]
    if all(nums):
        return f"{float(old):.15g}" == f"{float(new):.15g}"
    return excel_text(render(old)) == render(new)


class Periods:
    """Reads a Period column: date / datetime cells, Excel serials, or text dates. a/b/yyyy text is read day- or
    month-first from the column's own evidence (a part above 12), else `dayfirst`."""

    def __init__(self, values, dayfirst: bool):
        self.dayfirst = slash_dayfirst([v for v in values if isinstance(v, str)], dayfirst)

    def __call__(self, v) -> date | None:
        if isinstance(v, (date, datetime)):                        # a real date cell: 1899-12-30 (Excel's zero) is not a period
            d = v.date() if isinstance(v, datetime) else v
            return d if d.year in YEARS else None
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, str) and v.strip().isdigit():
            v = int(v.strip())
        if isinstance(v, (int, float)):
            ok = math.isfinite(v) and float(v).is_integer() and int(v) in SERIALS
            return EXCEL_EPOCH + timedelta(days=int(v)) if ok else None
        status, ymd, _ = parse_date(str(v), self.dayfirst, 1900, 2100)
        return date(*ymd) if status == "ok" else None


def month_label(d: date) -> str:
    """"January '24" for a date; built by hand because strftime("%y") fails before 1900 on Windows."""
    return f"{calendar.month_name[d.month]} '{d.year % 100:02d}"
