"""Cell classes (spec 4.2) and the canonical rendering of stored values."""
from __future__ import annotations

import math
import re
from datetime import date, timedelta
from decimal import Decimal

from .model import Cell

_DATE = [
    re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$"),
    re.compile(r"^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}(\s\d{1,2}:\d{2}(:\d{2})?)?$"),
]
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_PERIOD = [
    re.compile(rf"^{_MONTH}([\s\-'/]?\d{{2,4}})?$", re.I),
    re.compile(r"^\d{4}-\d{2}$"),
    re.compile(r"^Q[1-4]([\s\-]?\d{2,4})?$", re.I),
    re.compile(r"^\d{4}[\s\-]?Q[1-4]$", re.I),
    re.compile(r"^W\d{1,2}$", re.I),
    re.compile(r"^H[12]$", re.I),
    re.compile(r"^FY\d{2,4}$", re.I),
]
_NUM = re.compile(r"^[\(\-+]?[$€£₹]?\s*(\d{1,3}([, ]\d{3})+|\d+)?(\.\d+)?([eE][-+]?\d+)?\s*%?\)?$")
_ERR = re.compile(r"^#(N/A|REF!|DIV/0!|VALUE!|NAME\?|NUM!|NULL!)$")
_URL = re.compile(r"^(https?://|www\.)\S+$|^\S+@\S+\.\S+$", re.I)
_YEARLIKE = re.compile(r"^(19\d\d|20\d\d|2100)$")

# Serial-date limits per date system: (first valid day, last valid day = 9999-12-31).
_LIMITS = {False: (1, 2_958_465), True: (0, 2_957_003)}


def classify_text(s: str) -> str:
    """First matching class of the table in spec 4.2: W D P N E T L."""
    t = s.strip()
    if not t:
        return "W"
    if any(p.match(t) for p in _DATE):
        return "D"
    if any(p.match(t) for p in _PERIOD):
        return "P"
    if any(ch.isdigit() for ch in t) and _NUM.match(t):
        return "N"
    if _ERR.match(t):
        return "E"
    if len(t) > 60 or _URL.match(t):
        return "T"
    return "L"


def render_number(x: float) -> str:
    """Shortest decimal that round-trips the stored double: plain for 1e-6 <= |x| < 1e15, else `1.5E-07`."""
    if x != x or x in (math.inf, -math.inf):
        return str(x)
    if float(x).is_integer() and abs(x) < 1e15:
        return str(int(x))
    r = repr(float(x))
    if 1e-6 <= abs(x) < 1e15:
        return format(Decimal(r), "f") if ("e" in r or "E" in r) else r
    return r.upper()


def fmt_kind(fmt: str | None) -> str:
    """'date' | 'datetime' | 'time' | 'elapsed' | '' from an Excel number format code."""
    if not fmt or fmt == "General" or fmt == "@":
        return ""
    if re.search(r"\[(h|m|s)+\]", fmt.lower()):
        return "elapsed"
    f = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]|_.|\*.', "", fmt.lower())
    has_d = bool(re.search(r"[dy]", f)) or "mmm" in f
    has_t = bool(re.search(r"h|s|am/pm", f))
    if has_d and has_t:
        return "datetime"
    return "date" if has_d else ("time" if has_t else "")


def render_serial(x: float, kind: str, date1904: bool) -> str | None:
    """ISO 8601 text of a serial, rounded to the millisecond; None when it must be written as a number.

    1900 system: serials 1-59 map as Excel maps them, 60 (the non-existent 29 Feb 1900) is a number."""
    day = math.floor(x)
    ms = round((x - day) * 86_400_000)
    if kind == "time":
        h, rest = divmod(ms % 86_400_000, 3_600_000)
        m, rest = divmod(rest, 60_000)
        s, milli = divmod(rest, 1000)
        return f"{h:02}:{m:02}:{s:02}" + (f".{milli:03}" if milli else "")
    first, last = _LIMITS[date1904]
    if day < first or day > last or (not date1904 and day == 60):
        return None
    if ms == 86_400_000:
        day, ms = day + 1, 0
    if date1904:
        d = date(1904, 1, 1) + timedelta(days=day)
    else:
        d = date(1899, 12, 31) + timedelta(days=day) if day < 60 else date(1899, 12, 30) + timedelta(days=day)
    if kind == "date":
        return d.isoformat()
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, milli = divmod(rest, 1000)
    return f"{d.isoformat()}T{h:02}:{m:02}:{s:02}" + (f".{milli:03}" if milli else "")


def number_cell(x: float, fmt: str | None, date1904: bool, style: int = 0) -> Cell:
    """A stored number, rendered as a date only under a date/time format (never from its display text)."""
    kind = fmt_kind(fmt)
    if kind in ("date", "datetime", "time"):
        text = render_serial(x, kind, date1904)
        if text is not None:
            return Cell("D", text, kind, fmt, style=style)
        return Cell("N", render_number(x), "n", fmt, style=style, out_of_range=True)
    return Cell("N", render_number(x), "n", fmt, style=style)


def text_cell(s: str, fmt: str | None = None, style: int = 0) -> Cell | None:
    """A stored string, exactly as written; empty strings are not populated cells."""
    if s == "":
        return None
    s = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), s)
    if s.startswith("#") and _ERR.match(s.strip()):
        return Cell("E", s, "e", fmt, style=style, error=True)
    return Cell(classify_text(s), s, "s", fmt, style=style)


def mark_period_years(cells: dict) -> None:
    """Year headers (whole numbers 1900-2100, ascending in a row) become class P."""
    by_row: dict[int, list] = {}
    for (r, c), cell in cells.items():
        if cell.cls == "N" and cell.text and _YEARLIKE.match(cell.text.strip()):
            by_row.setdefault(r, []).append((c, cell))
    for lst in by_row.values():
        lst.sort(key=lambda x: x[0])
        years = [int(cell.text) for _, cell in lst]
        if len(years) >= 2 and any(years[i] < years[j] for i in range(len(years)) for j in range(i + 1, len(years))):
            for _, cell in lst:
                cell.cls = "P"
