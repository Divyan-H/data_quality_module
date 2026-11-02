"""Pure value helpers shared by the rules: numbers, dates, column-name keywords. No table state here."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

EXCEL_EPOCH = date(1899, 12, 30)
ERROR_RE = re.compile(r"^#(REF!|VALUE!|N/A|DIV/0!|NAME\?|NUM!|NULL!|ERROR!)$", re.I)
MOJIBAKE_RE = re.compile(r"Ã.|â€.|Â[^\w]|Ð|Ñ|^﻿")


# ── column-name keywords ─────────────────────────────────────────────────────
def has_keyword(name: str, words: list[str]) -> bool:
    """A name token equals a keyword, or (keywords of 4+ letters) starts with it: 'weekly' ~ 'week'.
    'AccountID' / 'account_id' both give the tokens account, id."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", spaced) if t]
    return any(t == w or (len(w) >= 4 and t.startswith(w)) for t in toks for w in words)


# ── numbers ──────────────────────────────────────────────────────────────────
_NUM_RE = re.compile(r"[+-]?(?:\d[\d.,]*|\.\d+)(?:[eE][+-]?\d+)?")


def number_convention(values: list[str], symbols: str) -> bool:
    """True if the column's own evidence says comma is the decimal mark (EU). Settles an ambiguous '1,234'."""
    eu = us = False
    for v in values:
        t = v.strip().strip(symbols + "  ")
        if not re.fullmatch(r"[+-]?[\d.,]+", t):
            continue
        c, d = t.count(","), t.count(".")
        if c and d:
            eu |= t.rfind(",") > t.rfind(".")
            us |= t.rfind(".") > t.rfind(",")
        elif c == 1 and not re.fullmatch(r"[+-]?\d+,\d{3}", t):
            eu = True
        elif c > 1 or d == 1:
            us = True
    return eu and not us


def parse_number(s: str, symbols: str = "$€£₹¥", eu: bool = False) -> str | None:
    """Plain-number text ('$1,234.50' -> '1234.50', '€99,99' -> '99.99'), or None if `s` is not a number.

    Both separators: the last one is the decimal mark. Only a dot: decimal. Only a comma: decimal unless exactly
    three digits follow it; that is ambiguous ('1,234') and is read as grouping unless the column says `eu`.
    """
    t = s.strip().strip(symbols + "  ").strip()
    neg = len(t) > 2 and t[0] == "(" and t[-1] == ")"            # accounting negative: (123)
    if neg:
        t = t[1:-1].strip(symbols + " ")
    if not _NUM_RE.fullmatch(t):
        return None
    sign = "-" if neg or t[0] == "-" else ""
    body, _, exp = t.lstrip("+-").lower().partition("e")
    c, d = body.count(","), body.count(".")
    dec = grp = None
    if c and d:
        dec = "," if body.rfind(",") > body.rfind(".") else "."
        grp = "." if dec == "," else ","
        if body.count(dec) > 1:
            return None
    elif c:
        if c == 1 and (len(body.rsplit(",", 1)[1]) != 3 or eu):
            dec = ","
        else:
            grp = ","
    elif d:
        dec, grp = (".", None) if d == 1 else (None, ".")
    whole, _, frac = body.partition(dec) if dec else (body, "", "")
    if grp:
        if grp in frac or not all(len(p) == 3 for p in whole.split(grp)[1:]):
            return None                                           # '1,23,4' is not a grouped number
        whole = whole.replace(grp, "")
    out = f"{sign}{whole or '0'}" + (f".{frac}" if frac else "")
    try:
        Decimal(out)
        return format(Decimal(out + "e" + exp), "f") if exp else out
    except InvalidOperation:
        return None


def integer_digits(num: str) -> int:
    """Significant digits before the decimal point: what a float cannot hold beyond 15 (an ID or account number)."""
    return len(num.lstrip("+-").split(".")[0].lstrip("0"))


# ── dates ────────────────────────────────────────────────────────────────────
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
# shape -> (regex, field order, output template). "dmy/" and "mdy/" are the two readings of a/b/yyyy.
SHAPES = {
    "iso": (r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?", "ymd", "{y:04d}-{m:02d}-{d:02d}"),
    "dmy/": (r"(\d{1,2})/(\d{1,2})/(\d{4})", "dmy", "{d:02d}/{m:02d}/{y:04d}"),
    "mdy/": (r"(\d{1,2})/(\d{1,2})/(\d{4})", "mdy", "{m:02d}/{d:02d}/{y:04d}"),
    "dmy-": (r"(\d{1,2})-(\d{1,2})-(\d{4})", "dmy", "{d:02d}-{m:02d}-{y:04d}"),
    "dmy.": (r"(\d{1,2})\.(\d{1,2})\.(\d{4})", "dmy", "{d:02d}.{m:02d}.{y:04d}"),
    "d mon y": (r"(\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})", "dmy", "{d:02d} {mon} {y:04d}"),
    "mon d, y": (r"([A-Za-z]{3})[a-z]* (\d{1,2}), (\d{4})", "mdy", "{mon} {d:02d}, {y:04d}"),
}


def _fields(shape: str, m) -> tuple[int, int, int] | None:
    vals = dict(zip(SHAPES[shape][1], m.groups()))
    try:
        if not vals["m"].isdigit():
            vals["m"] = MONTHS[vals["m"][:3].lower()]
    except KeyError:
        return None
    return int(vals["y"]), int(vals["m"]), int(vals["d"])


def slash_dayfirst(values: list[str], default: bool) -> bool:
    """Column evidence for a/b/yyyy: a first part above 12 means day-first, a second part above 12 month-first."""
    for v in values:
        m = re.fullmatch(r"\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{4}\s*", v)
        if m and int(m[1]) > 12:
            return True
        if m and int(m[2]) > 12:
            return False
    return default


def _check(y: int, mo: int, d: int, min_year: int, max_year: int):
    if not 1 <= mo <= 12:
        return "bad_month", (y, mo, d)
    if not min_year <= y <= max_year:
        return "bad_year", (y, mo, d)
    if not 1 <= d <= 31:
        return "bad_day", (y, mo, d)
    last = calendar.monthrange(y, mo)[1]
    return ("clamp", (y, mo, last)) if d > last else ("ok", (y, mo, d))


def parse_date(s: str, dayfirst: bool, min_year: int, max_year: int):
    """(status, (y, m, d), shape). status: ok | clamp (Feb 30 -> last day) | bad_month | bad_day | bad_year | none.
    A slash date is tried in the column's reading first, then the other one (12/13/2026 can only be month-first)."""
    s = s.strip()
    slash = ["dmy/", "mdy/"] if dayfirst else ["mdy/", "dmy/"]
    first_bad = None
    for shape in ["iso", *slash, "dmy-", "dmy.", "d mon y", "mon d, y"]:
        m = re.fullmatch(SHAPES[shape][0], s)
        f = m and _fields(shape, m)
        if not f:
            continue
        status, ymd = _check(*f, min_year, max_year)
        if status in ("bad_month", "bad_day") and shape.endswith("/"):
            first_bad = first_bad or (status, ymd, shape)
            continue
        return status, ymd, shape
    return first_bad or ("none", None, None)


def format_date(ymd: tuple[int, int, int], shape: str) -> str:
    y, m, d = ymd
    return SHAPES[shape][2].format(y=y, m=m, d=d, mon=calendar.month_abbr[m])


def serial_to_ymd(n: int) -> tuple[int, int, int]:
    dt = EXCEL_EPOCH + timedelta(days=n)
    return dt.year, dt.month, dt.day


def ymd_to_serial(ymd: tuple[int, int, int]) -> int:
    return (date(*ymd) - EXCEL_EPOCH).days
