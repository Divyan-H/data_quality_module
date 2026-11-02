"""Phase B rules. Each rule is `rule(t: Table)`; RULES lists them in run order.

Order matters: noise first (errors, encoding, control characters, whitespace, placeholders), so nothing later sees
'#REF!' or ' 12 '; then dates and numbers; then the fixes that change meaning (merge names, units, signs, outliers),
each also queued for review with its original value; duplicate rows after every fix, so rows the fixes made equal
collapse; the name and truncation checks last. Every rule is deterministic (no LLM)."""
from __future__ import annotations

import re
import math
import statistics
from bisect import bisect_left
from collections import Counter, defaultdict
from decimal import Decimal
from difflib import SequenceMatcher

import polars as pl

from .kpi import additive_columns, isolated_gaps, label_columns, period_column
from .values import (ERROR_RE, MOJIBAKE_RE, format_date, has_keyword, parse_date, parse_number,
                     integer_digits, serial_to_ymd, ymd_to_serial)


# ── 1. Excel errors and Phase A's cell annotations (D-05..D-08) ──────────────
def errors_and_formulas(t):
    for col in t.cols:
        for i, v in enumerate(t.data[col]):
            if v is not None and ERROR_RE.match(v.strip()):
                t.set(col, i, None, "D-07" if v.strip().upper() == "#REF!" else "D-08")
    for i, col, kind, text in t.annotations:          # Phase A already flagged these; policy is applied here
        if kind == "formula_uncached":                # no stored result: value is NULL, the formula is not evaluated
            formula = f" {text}" if text and text.startswith("=") else ""
            t.flag("D-05", col, i, f"Formula{formula} has no saved result in the file (it was saved by a tool that does "
                   "not calculate), so it is read as empty. Excel shows a value only because it recalculates on open.",
                   "Open the source file in Excel and save it (this stores the results), or type the value in New_Value.",
                   current=None)
        elif kind == "external_link":                 # reported, never auto-fixed
            t.flag("D-06", col, i, f"Formula refers to another workbook{': ' + text if text and text.startswith('=') else ''}; "
                   "its value depends on a file the pipeline does not have.",
                   "Confirm the linked workbook is available or replace the formula with values.", current=text)
        elif kind == "broken_reference":
            if t.data[col][i] is None:
                t.log("D-07", col, i, text, None)
            else:
                t.set(col, i, None, "D-07")
        elif kind == "error" and t.data[col][i] is not None:
            t.set(col, i, None, "D-08")


# ── 2. Headers, encoding, control characters, whitespace, blanks (D-11, D-20, D-13, D-09, N-02) ──
def headers(t):
    for old in list(t.cols):
        new = " ".join(old.split())
        if new != old and new and new not in t.data:
            t.rename(old, new, "D-11")


def _demojibake(s: str) -> str:
    """Re-decode text that was UTF-8 read as cp1252 (or latin-1); a leading BOM is dropped."""
    if not MOJIBAKE_RE.search(s):
        return s
    s = s.lstrip("\ufeff")
    for enc in ("cp1252", "latin-1"):
        try:
            fixed = s.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != s:
            return fixed
    return s


def _each_string(t, code, fn):
    for col in t.cols:
        for i, v in enumerate(t.data[col]):
            if v is not None and (new := fn(v)) != v:
                t.set(col, i, new, code)


def encoding(t):
    _each_string(t, "D-20", _demojibake)


def control_chars(t):
    """Non-printable characters (control codes, nbsp, zero-width) become spaces."""
    _each_string(t, "D-13", lambda s: "".join(c if c.isprintable() else " " for c in s))


def whitespace_and_blanks(t):
    _each_string(t, "D-09", lambda s: " ".join(s.split()) if s.strip() else s.strip())
    for col in t.cols:                                   # N-02: empty string is a real NULL
        for i, v in enumerate(t.data[col]):
            if v == "":
                t.set(col, i, None, "N-02")


# ── 3. Null placeholders (D-18, N-03) ────────────────────────────────────────
def placeholders(t):
    """'NA', 'N/A', 'null', '-' ... become NULL, unless the token is the column's own top value at >= 5%."""
    tokens = set(t.cfg["null_tokens"])
    for col in t.cols:
        vals = [v.strip().casefold() for v in t.values(col)]
        if not vals:
            continue
        top, top_n = Counter(vals).most_common(1)[0]
        geo = has_keyword(col, ["region", "country", "market", "territory"])      # 'NA' can be North America
        for i, v in enumerate(t.data[col]):
            tok = v.strip().casefold() if v is not None else None
            if tok in tokens and not (geo and tok == "na") and not (tok == top and top_n / len(vals) >= t.cfg["dominant_value_threshold"]):
                t.set(col, i, None, "D-18")


# ── 3b. More than one value in a cell (D-29) ─────────────────────────────────
_COMMA_THOUSANDS = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+")           # 1,200  100,200  1,234,567
_COMMA_PAIR = re.compile(r"[+-]?\d{3},\d{3}")                         # 100,200: two 3-digit numbers or one thousands figure
_COMMA_DECIMAL = re.compile(r"[+-]?\d+,(?:\d{1,2}|\d{4,})")         # 12,34  3,5  (cannot be a thousands mark)
_NUMBER_LIST = re.compile(r"\s*[;|/&]\s*|,\s+|\s+,\s*|\s+")
_SPACE_GROUPS = re.compile(r"\d{1,3}(?: \d{3})+")                    # 1 200: digits grouped by spaces, one number


def _has_letter(s: str) -> bool:
    return any(ch.isalpha() for ch in s)


def _text_list(v: str, seps: list, min_part: int):
    """(separator label, parts) when a text cell holds a list, else None. Every part must have a letter and a few
    characters, so 'R&D', 'AT&T', 'N/A', '24/7' and a URL are not lists."""
    for label, rx in seps:
        parts = [p.strip() for p in rx.split(v)]
        if len(parts) >= 2 and all(len(p) >= min_part and _has_letter(p) for p in parts):
            return label, parts
    return None


def _number_list(v: str, sym: str):
    """(separator label, parts) when a cell in a numeric column holds two or more numbers instead of one."""
    text = v.strip()
    if parse_number(text, sym) is not None or _SPACE_GROUPS.fullmatch(text):
        return None
    parts = [p for p in _NUMBER_LIST.split(text) if p]
    if len(parts) >= 2 and all(parse_number(p, sym) is not None for p in parts):
        label = next((lab for lab, ch in ((";", ";"), ("|", "|"), ("/", "/"), ("&", "&"), (",", ",")) if ch in text), "space")
        return label, parts
    return None


def _date_list(v: str, args: tuple):
    """(separator label, parts) when a cell in a date column holds several dates (not a single date)."""
    if parse_date(v, *args)[0] != "none":
        return None
    for label, rx in ((";", r";"), ("|", r"\|"), ("&", r"&"), (",", r",")):
        parts = [p.strip() for p in re.split(rx, v)]
        if len(parts) >= 2 and all(p and parse_date(p, *args)[0] in ("ok", "clamp") for p in parts):
            return label, parts
    return None


def multi_value_cells(t):
    """D-29: a cell that holds more than one value. Flag only: the value stays exactly as it is, with the separator found.
    Text: a list split by ; | / & or a comma and a space (parts need a letter and a minimum length). Numeric: two or more
    numbers in one cell, or a comma that can be a decimal mark, a thousands mark or two numbers (`12,34`, `100,200`) when
    nothing else in the column settles it (a pattern a few cells already use is the column's own convention; `1,200` and
    `$1,200` are read as one amount). Date: several dates.
    When most of a column looks like a list it is a list field: one flag for the column, not one per cell."""
    c = t.cfg
    seps = [(label, re.compile(rx)) for label, rx in c["multi_value_separators"]]
    sym, min_part = c["currency_symbols"], c["multi_value_min_part"]
    for col in t.cols:
        kind = t.kind(col)
        if kind not in ("text", "numeric", "date"):
            continue
        vals = t.data[col]
        filled = sum(v is not None for v in vals)
        if not filled:
            continue
        hits = []                                    # (row, why, suggestion, parts, proposed reading)
        stripped, thousands, decimal = [], 0, 0
        if kind == "numeric":
            stripped = [None if v is None else v.strip().strip(sym + "\u00a0 ") for v in vals]
            thousands = sum(bool(x and _COMMA_THOUSANDS.fullmatch(x)) for x in stripped)
            decimal = sum(bool(x and _COMMA_DECIMAL.fullmatch(x)) for x in stripped)
        for i, v in enumerate(vals):
            if v is None:
                continue
            if kind == "text":
                found = _text_list(v, seps, min_part)
                if found:
                    label, parts = found
                    hits.append((i, f"'{v}' holds {len(parts)} values separated by '{label}': {' | '.join(parts[:6])}.",
                                 f"Accept: it is one value, keep it. New Value: the one value to keep (the first is '{parts[0]}'). "
                                 "Split: one row per value, the row's other values repeated on each new row. Nothing was changed.",
                                 parts, None))
            elif kind == "date":
                found = _date_list(v, t.date_args(col))
                if found:
                    label, parts = found
                    hits.append((i, f"'{v}' holds {len(parts)} dates separated by '{label}'.",
                                 "Enter the one date this row belongs to as New Value. Nothing was changed.", None, None))
            else:
                found = _number_list(v, sym)
                if found:
                    label, parts = found
                    hits.append((i, f"'{v}' holds {len(parts)} numbers separated by '{label}': {' | '.join(parts[:6])}. "
                                 "A numeric column needs one number in a cell.",
                                 "Enter the correct single number as New Value. Nothing was changed; the column stays text "
                                 "until this is answered.", None, None))
                    continue
                x = stripped[i] if stripped[i] == v.strip() else None          # a currency symbol says it is one formatted amount
                if x and _COMMA_PAIR.fullmatch(x) and thousands < c["multi_value_convention_cells"]:
                    reading = parse_number(v, sym, False)
                    first, rest = x.split(",", 1)
                    hits.append((i, f"'{v}' can be read as {reading} (comma as a thousands mark) or as two numbers "
                                 f"({first} and {rest.replace(',', ' and ')}); no other cell in the column uses a thousands comma, "
                                 "so nothing settles it.",
                                 f"Accept: read it as {reading}. New Value: enter the right number. Nothing was changed.",
                                 None, reading))
                elif x and _COMMA_DECIMAL.fullmatch(x) and decimal < c["multi_value_convention_cells"]:
                    reading = parse_number(v, sym, True)
                    first, rest = x.split(",", 1)
                    hits.append((i, f"'{v}' can be read as {reading} (comma as a decimal mark) or as two numbers "
                                 f"({first} and {rest}); no other cell in the column uses a decimal comma, so nothing settles it.",
                                 f"Accept: read it as {reading}. New Value: enter the right number. Nothing was changed.",
                                 None, reading))
        if not hits:
            continue
        if len(hits) / filled > c["multi_value_max_share"]:
            t.flag("D-29", col, None, f"{len(hits)} of {filled} cells hold a list or an ambiguous comma, so this column looks like "
                   "a list field rather than single values; nothing was flagged cell by cell.",
                   "Confirm whether each cell is meant to hold one value. Nothing was changed.")
            continue
        for i, why, suggest, parts, proposed in hits[: c["multi_value_max_cells"]]:
            t.flag("D-29", col, i, why, suggest, parts=parts, proposed=proposed)
        for h in hits:
            t.held.add((col, h[0]))
        if len(hits) > c["multi_value_max_cells"]:
            t.flag("D-29", col, None, f"{len(hits)} cells hold more than one value; the first {c['multi_value_max_cells']} are "
                   "listed one by one. The others were also left as they are.",
                   "Correct them on the Data sheet, or ask the client.")


# ── 3c. City names (D-30) ────────────────────────────────────────────────────
def city_names(t):
    """D-30: values of a city column that name the same city differently: another name (Bombay / Mumbai), a short form
    (BLR), or a spelling mistake (Dehli). One item per distinct spelling for the whole column (`scope: value`), with the
    standard name proposed; nothing is changed until the item is answered, and the cells are held back from later rules.
    The matching works on distinct values only (see src/hygiene/cities.py): a hash of known names, a delete-neighbourhood
    index for close names, and a comparison of the unknown values with each other. A project list (`city_master`) makes
    its spellings the standard and flags every value that is not in it."""
    from .cities import build_index
    c = t.cfg
    idx = None
    for col in t.cols:
        if not has_keyword(col, c["city_columns"]) or t.kind(col) != "text":
            continue
        if idx is None:
            idx = build_index(c["city_names_file"], c["city_master"], c["city_gazetteer"], c["city_countries"], c["city_max_edit"],
                              c["city_known_file"])
        first: dict[str, int] = {}
        counts: Counter = Counter()
        for i, v in enumerate(t.data[col]):
            if v is not None and (col, i) not in t.held:
                counts[v] += 1
                first.setdefault(v, i)
        if not counts:
            continue
        matches = {v: idx.resolve(v) for v in counts}
        spellings: dict[str, list[str]] = defaultdict(list)
        for v, m in matches.items():
            if m:
                spellings[m.canonical].append(v)
        items = []                                             # (value, why, suggestion, proposed, severity)
        for v, m in sorted(matches.items(), key=lambda kv: -counts[kv[0]]):
            if m is None:
                continue
            others = [f"'{o}' ({counts[o]})" for o in spellings[m.canonical] if o != v]
            also = f" The column also has {', '.join(others[:4])}." if others else ""
            if m.kind == "standard" and m.in_master:
                continue                                       # already the standard name (case and spaces are D-10's)
            if not m.in_master:
                if m.kind == "standard":
                    what = f"'{v}' ({counts[v]} cells) is a known city that is not in the project's city list.{also}"
                else:
                    what = (f"'{v}' ({counts[v]} cells) is " + {"alias": "another name or a short form of", "close": "probably a spelling mistake of"}[m.kind]
                            + f" {m.canonical}, a known city that is not in the project's city list.{also}")
                items.append((v, what, f"Accept: use '{m.canonical}' anyway. New Value: the right name for all {counts[v]} cells. "
                              "Nothing was changed.", m.canonical, "info" if m.kind == "standard" else "warn"))
                continue
            what = {"alias": "another name, an old name or a short form of", "close": "probably a spelling mistake of"}.get(m.kind, "")
            items.append((v, f"'{v}' ({counts[v]} cells) is {what} '{m.canonical}'.{also}",
                          f"Accept: standardise all {counts[v]} cells to '{m.canonical}'. New Value: the right name for all of "
                          "them. Nothing was changed.", m.canonical, "warn"))
        unknown = [v for v, m in matches.items() if m is None and normalize_city(v) and normalize_city(v) not in _CITY_SKIP]
        if idx.master_mode:                                    # the project has its list: anything else is not on it
            for v in sorted(unknown, key=lambda v: -counts[v]):
                items.append((v, f"'{v}' ({counts[v]} cells) is not in the project's city list.",
                              "Accept: it is a new city. New Value: the right name for all of them. Nothing was changed.", None, "warn"))
        else:                                                  # no list: values nobody knows are compared with each other
            groups = _near_groups(unknown, counts, c["near_dup_ratio"])
            for best, variants in groups:
                for v in variants:
                    items.append((v, f"'{v}' ({counts[v]} cells) looks like the same city as '{best}' ({counts[best]} cells).",
                                  f"Accept: standardise to '{best}'. New Value: the right name for all {counts[v]} cells. "
                                  "Nothing was changed.", best, "warn"))
        items.sort(key=lambda it: it[4] == "info")              # what needs a look first, the known-but-unlisted cities last
        for v, why, suggest, proposed, sev in items[: c["city_max_items"]]:
            t.flag("D-30", col, first[v], why, suggest, sev=sev, proposed=proposed, scope="value", cells=counts[v])
            t.held.add_value(col, v)
        if len(items) > c["city_max_items"]:
            t.flag("D-30", col, None, f"{len(items)} spellings in '{col}' need a look; the first {c['city_max_items']} are listed.",
                   "Fix the rest on the Data sheet, or add the names to config/city_names.yaml and run again.")


_CITY_SKIP = {"others", "other", "unspecified", "unknown", "all", "total", "na", "nan", "none", "india", "overall", "panindia",
              "national", "notapplicable", "tbd"}


def normalize_city(value) -> str:
    from .cities import normalize
    return normalize(value)


def _near_groups(values: list[str], counts: Counter, ratio: float) -> list[tuple[str, list[str]]]:
    """Unknown spellings that look like each other (blocked by their first letters so only a few are compared):
    [(the most frequent spelling, [the other spellings])]."""
    blocks: dict[str, list[str]] = defaultdict(list)
    for v in values:
        blocks[normalize_city(v)[:2]].append(v)
    parent = {v: v for v in values}

    def root(v):
        while parent[v] != v:
            v = parent[v]
        return v

    for block in blocks.values():
        block.sort()
        for a, x in enumerate(block):
            for y in block[a + 1:]:
                if len(x) <= 40 and len(y) <= 40 and _near_duplicate(x, y, ratio):
                    parent[root(y)] = root(x)
    groups: dict[str, list[str]] = defaultdict(list)
    for v in values:
        groups[root(v)].append(v)
    out = []
    for members in groups.values():
        if len(members) > 1:
            best = max(members, key=lambda v: (counts[v], len(v), v))
            out.append((best, [v for v in members if v != best]))
    return out


# ── 3d. Dates in order (D-31) ────────────────────────────────────────────────
def date_order(t):
    """D-31: within each series (one per combination of the label columns) the dates should run in one direction. Flag
    only: the rows are not sorted (the transformation step sorts; Phase C repeats this check as N-13 for the tables it
    checks). Reports how many series are out of order with the first place, and notes a table that is sorted newest first."""
    period, dates = period_column(t)
    if period is None:
        return
    labels = label_columns(t, {period})
    series: dict[tuple, list[int]] = defaultdict(list)
    for i, d in enumerate(dates):
        if d is not None:
            series[tuple(t.data[c][i] for c in labels)].append(i)
    asc = desc = broken = 0
    first_bad = None
    for key, rows in series.items():
        if len(rows) < 3:
            continue
        ds = [dates[i] for i in rows]
        up = all(a <= b for a, b in zip(ds, ds[1:]))
        down = all(a >= b for a, b in zip(ds, ds[1:]))
        if up:
            asc += 1
        elif down:
            desc += 1
        else:
            broken += 1
            if first_bad is None:
                k = next(k for k, (a, b) in enumerate(zip(ds, ds[1:])) if a > b)
                first_bad = (key, rows[k], rows[k + 1])
    total = asc + desc + broken
    if not total:
        return
    if broken or (asc and desc):
        where = ""
        if first_bad:
            key, a, b = first_bad
            where = (f" First place: Excel row {t.data['_source_row'][b]} ({dates[b]}) comes after row "
                     f"{t.data['_source_row'][a]} ({dates[a]})" + (f" in the series {', '.join(map(str, key))}" if key else "") + ".")
        head = (f"{broken} of {total} series are not in date order" if broken else
                f"the series run in different directions ({asc} oldest first, {desc} newest first)")
        t.flag("D-31", period, None, f"'{period}': {head} ({asc} rise, {desc} fall, {broken} mixed).{where}",
               "Sort by date (the transformation step does this) or ask the data provider. Nothing was changed.")
    elif desc and not asc:
        t.flag("D-31", period, None, f"'{period}': all {desc} series are sorted newest first (dates fall down the sheet).",
               "Fine if intended; later steps that read the first row as the oldest will be wrong. Nothing was changed.", sev="info")


# ── 3e. Expected columns (D-32) ──────────────────────────────────────────────
def expected_columns(t):
    """D-32: the project expects a set of columns in every sheet (the run inputs list them, for all sheets and per sheet).
    A missing one is flagged, with the nearest column name when one is close (a typo, a renamed header). Flag only."""
    from .cities import edit_distance, max_edit, normalize
    spec = t.cfg.get("expected_columns") or {}
    wanted = list(spec.get("all_sheets") or [])
    for name, cols in (spec.get("sheets") or {}).items():
        if str(name).casefold() in {str(x).casefold() for x in (t.sheet, t.source_file, t.ref) if x}:
            wanted += list(cols or [])
    if not wanted:
        return
    names = list(dict.fromkeys(wanted))
    headers = list(t.context_cols or t.cols)
    found = find_expected_columns(names, headers, t.cfg.get("column_synonyms") or {})
    have = {normalize(c): c for c in headers}
    for col in names:
        if found.get(col):
            continue
        n = normalize(col)
        cap = max_edit(len(n))
        near = [c for k, c in have.items() if cap and edit_distance(n, k, cap) <= cap]
        hint = f" '{near[0]}' looks like it." if len(near) == 1 else ""
        t.flag("D-32", col, None, f"The expected column '{col}' is not in this sheet.{hint}",
               "Ask the client for the missing column, or rename the header if it is the same data. Nothing was changed.")
    if spec.get("strict"):
        claimed = set(found.values())
        extra = [c for c in headers if c not in claimed]
        if extra:
            t.flag("D-32", None, None, f"{len(extra)} column(s) are not on the expected list: {', '.join(extra[:12])}.",
                   "Confirm they are meant to be there. Nothing was changed.", sev="info")


def _tokens(header: str) -> list[str]:
    """Words of a header: split on punctuation, spaces and camelCase ('SubPlatform' -> sub, platform)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(header))
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.casefold()) if w]


def find_expected_columns(expected: list[str], headers: list[str], synonyms: dict) -> dict[str, str | None]:
    """Which header of a sheet is each expected column? Header names are never exact, so, in order: the same name ignoring
    case, spaces and punctuation; a listed synonym (Sub Platform = subplatform = placement); the name as whole words of a
    longer header (Platform Name); a typo (one or two edits). Each header answers for one expected column only, and a longer
    expected name is matched first so 'Sub Platform Name' is the subplatform and not the platform."""
    from .cities import edit_distance, max_edit, normalize
    norm = {h: normalize(h) for h in headers}
    syn = {e: {normalize(e)} | {normalize(s) for s in (synonyms.get(normalize(e)) or [])} for e in expected}
    out: dict[str, str | None] = {e: None for e in expected}
    claimed: set[str] = set()
    order = sorted(expected, key=lambda e: -len(normalize(e)))

    def take(e, h):
        out[e] = h
        claimed.add(h)

    for e in order:                                                     # 1. the name, then its synonyms
        for ok in (lambda n, e=e: n == normalize(e), lambda n, e=e: n in syn[e]):
            hit = next((h for h in headers if h not in claimed and ok(norm[h])), None)
            if hit:
                take(e, hit)
                break
    for e in order:                                                     # 2. the name (or a synonym) as whole words of a longer header
        if out[e]:
            continue
        for h in headers:
            if h in claimed:
                continue
            words = _tokens(h)
            joined = {"".join(words[i:j]) for i in range(len(words)) for j in range(i + 1, len(words) + 1)}
            if joined & syn[e]:
                take(e, h)
                break
    for e in order:                                                     # 3. a typo
        if out[e]:
            continue
        n = normalize(e)
        cap = max_edit(len(n))
        near = [h for h in headers if h not in claimed and cap and edit_distance(n, norm[h], cap) <= cap]
        if len(near) == 1:
            take(e, near[0])
    return out


# ── 4. Dates (D-01, D-25, D-02, D-03) ────────────────────────────────────────
def numbers_read_as_dates(t):
    """D-01: a date in a numeric column is a number Excel displayed as a date; the real value is its serial."""
    for col in t.cols:
        if t.kind(col) != "numeric":
            continue
        args = t.date_args(col)
        for i, v in enumerate(t.data[col]):
            if v is not None and (col, i) not in t.held and parse_number(v, t.cfg["currency_symbols"]) is None:
                status, ymd, _ = parse_date(v, *args)
                if status in ("ok", "clamp"):
                    t.set(col, i, str(ymd_to_serial(ymd)), "D-01")


def date_serials(t):
    """D-25: whole numbers in a date-named column are Excel date serials."""
    lo, hi = t.cfg["serial_range"]
    for col in t.cols:
        if t.kind(col) == "date" and has_keyword(col, t.cfg["date_keywords"]):
            for i, v in enumerate(t.data[col]):
                if v is not None and re.fullmatch(r"\d+(\.0+)?", v) and lo <= int(float(v)) <= hi:
                    t.set(col, i, format_date(serial_to_ymd(int(float(v))), "iso"), "D-25")


def dates(t):
    for col in t.cols:
        if t.kind(col) != "date":
            continue
        args = t.date_args(col)
        parse_all = lambda: {i: parse_date(v, *args) for i, v in enumerate(t.data[col]) if v is not None and (col, i) not in t.held}
        for i, (status, ymd, shape) in parse_all().items():
            v = t.data[col][i]
            if status == "clamp":                                   # Feb 30 -> Feb 28: no decision needed
                t.set(col, i, format_date(ymd, shape), "D-02", f"'{v}' is not a real calendar date.")
            elif status.startswith("bad"):                          # month 13, year out of range: cannot guess
                t.set(col, i, None, "D-02", f"'{v}' is not a valid date ({status[4:]}).", review=True)
        parsed = parse_all()                                         # values as they are after the D-02 fixes
        shapes = Counter(s for st, _, s in parsed.values() if st == "ok")
        if len(shapes) > 1:                                          # D-03: rewrite to the column's dominant format
            dominant = max(shapes, key=lambda s: (shapes[s], s == "iso", s))
            for i, (status, ymd, shape) in parsed.items():
                if status == "ok" and shape != dominant:
                    t.set(col, i, format_date(ymd, dominant), "D-03")
        for i, (status, _, _) in parsed.items():
            if status == "none":
                t.flag("D-03", col, i, "Value is in none of the recognised date formats.",
                       "Correct the value or enter a date.", sev="warn")


# ── 5. Numbers and mixed types (E-01, D-04, N-01) ────────────────────────────
def numbers(t):
    """In a numeric column: strip currency symbols and separators; values that are not numbers become NULL + review."""
    sym = t.cfg["currency_symbols"]
    for col in t.cols:
        kind = t.kind(col)
        if kind == "text" and 0.5 <= (share := t.numeric_share(col)) < t.cfg["numeric_min_share"]:
            t.flag("D-04", col, None, f"Column mixes numbers and text ({share:.0%} numeric); nothing was converted.",
                   "Decide whether the column is numeric or text.")
        if kind != "numeric":
            continue
        eu = t.eu(col)
        seen: Counter = Counter()
        for i, v in enumerate(t.data[col]):
            if v is None or (col, i) in t.held:
                continue
            n = parse_number(v, sym, eu)
            if n is None:
                t.set(col, i, None, "D-04", f"'{v}' is not a number in a numeric column.", review=True)
            elif n != v:
                t.set(col, i, n, "E-01" if re.search(rf"[{re.escape(sym)}, ()\u00a0]", v) else "D-04")
                for s in {ch for ch in v if ch in sym}:
                    seen[s] += 1
        currency_note(t, col, seen)


def currency_note(t, col: str, seen: Counter) -> None:
    """D-33: amounts that carry a currency symbol and no currency code are read in the project's default currency (rupees).
    A symbol of another currency is not converted (rates are not applied here): it is flagged so someone confirms it."""
    default = str(t.cfg.get("default_currency") or "INR").upper()
    codes = t.cfg.get("currency_symbol_codes") or {}
    for s, n in sorted(seen.items(), key=lambda kv: -kv[1]):
        code = codes.get(s)
        if code == default:
            t.flag("D-33", col, None, f"{n} value(s) in '{col}' carry '{s}' and no currency code; read as {default}, the project's default currency.",
                   "Nothing to do unless the client uses another currency. The symbol was removed (E-01); the amounts are unchanged.", sev="info")
        else:
            t.flag("D-33", col, None, f"{n} value(s) in '{col}' carry '{s}' ({code or 'an unknown currency'}), not the project's default {default}.",
                   f"Confirm the currency. The symbol was removed and the amounts were not converted; add the {code or s} rate to the run inputs.")


def leading_zeros(t):
    """D-24: ZIP/product codes that lost their leading zeros are padded to the column's usual length."""
    for col in t.cols:
        if not has_keyword(col, t.cfg["zero_pad_keywords"]):
            continue
        digits = {i: re.sub(r"\.0+$", "", v) for i, v in enumerate(t.data[col]) if v is not None and re.fullmatch(r"\d+(\.0+)?", v)}
        if not digits:
            continue
        width, n = Counter(len(v) for v in digits.values()).most_common(1)[0]
        # pad only with evidence: a value at the usual width keeps a leading zero, or the field is fixed-width (ZIP)
        evidence = (any(len(v) == width and v.startswith("0") for v in digits.values())
                    or has_keyword(col, t.cfg["fixed_width_keywords"]))
        if evidence and n / len(digits) >= 0.5:
            for i, v in digits.items():
                t.set(col, i, v.zfill(width), "D-24")


# ── 6. Case and duplicate rows (D-10, D-19 - which runs after section 7) ──────────────────────────────────
def case(t):
    """D-10: 'Apple' / 'apple' / 'APPLE' all become the most common spelling in the column."""
    for col in t.cols:
        if t.kind(col) != "text" or len(set(t.values(col))) > t.cfg["max_categories"]:
            continue
        groups = defaultdict(Counter)
        for i, v in enumerate(t.data[col]):
            if v is not None and (col, i) not in t.held:
                groups[v.casefold()][v] += 1
        best = {k: max(c, key=lambda s: (c[s], s)) for k, c in groups.items() if len(c) > 1}
        for i, v in enumerate(t.data[col]):
            if v is not None and (col, i) not in t.held and v.casefold() in best:
                t.set(col, i, best[v.casefold()], "D-10")


def duplicates(t):
    """D-19: drop exact duplicate rows (keep the first). Rows sharing their key columns but differing elsewhere go to review."""
    if not t.cols:
        return
    df = pl.DataFrame({c: t.data[c] for c in t.cols}, schema={c: pl.String for c in t.cols}).with_row_index("i")
    groups = df.group_by(t.cols, maintain_order=True).agg(pl.col("i")).filter(pl.col("i").list.len() > 1)
    drop = {i: g[0] for g in groups["i"].to_list() for i in g[1:]}
    if t.cfg["fixes"].get("D-19", True):
        t.drop_rows(drop, "D-19")
    else:
        for i, kept in drop.items():
            t.flag("D-19", None, i, f"Exact duplicate of row {t.data['_row_id'][kept]}.", "Accept to drop the row.",
                   sev="info", proposed="drop")
    # a key column is an id-named column whose values are (almost) all distinct; a repeated id like vehicle_id is not a key
    keys = [c for c in t.cols if has_keyword(c, t.cfg["id_keywords"]) and t.values(c)
            and len(set(t.values(c))) >= 0.95 * len(t.values(c))]
    if not keys:
        return
    seen: dict[tuple, int] = {}
    for i in range(len(t.data["_row_id"])):
        k = tuple(t.data[c][i] for c in keys)
        if None in k:
            continue
        if k in seen:
            t.flag("D-19", keys[0], i, f"Same key {dict(zip(keys, k))} as row {t.data['_row_id'][seen[k]]} but other values differ.",
                   "Decide which row is right.", sev="info")
        else:
            seen[k] = i


# ── 7. Fixes that change meaning (D-12, D-22, D-15, D-14) and column-level fixes (D-21, D-16) ──
# Merging names, rescaling, flipping signs and capping change what a value means, so each changed cell is also put on
# Needs_Review with its original value: the analyst can Reject to restore it. Each fix can be switched off in settings.
def _num_text(d: Decimal) -> str:
    """Plain number text without float noise or trailing zeros: Decimal('12.50') -> '12.5'."""
    out = format(d.normalize(), "f")
    return "0" if out in ("-0", "") else out


def near_duplicates_and_sentinels(t):
    """D-12: near-identical names are merged into the most frequent spelling; placeholder and sentinel values
    ('test', 'dummy', '99999') become NULL."""
    c = t.cfg
    for col in t.cols:
        kind, counts = t.kind(col), Counter(t.values(col))
        if not counts:
            continue
        total = sum(counts.values())
        top = counts.most_common(1)[0][0].casefold()
        canon: dict[str, str] = {}                                 # variant -> spelling it is merged into
        if kind in ("text", "id") and 1 < len(counts) <= c["max_categories"]:
            names = sorted(v for v in counts if len(v) <= 40)      # names and codes, not free text or lists
            parent = {v: v for v in names}                         # union-find over near-duplicate pairs

            def root(v):
                while parent[v] != v:
                    v = parent[v]
                return v

            for a, x in enumerate(names):
                for y in names[a + 1:]:
                    # 'Campaign 1' vs 'Campaign 2' never pair: their digits differ
                    if re.findall(r"\d+", x) == re.findall(r"\d+", y) and _near_duplicate(x, y, c["near_dup_ratio"]):
                        parent[root(y)] = root(x)
            groups = defaultdict(list)
            for v in names:
                groups[root(v)].append(v)
            for vs in groups.values():
                best = max(vs, key=lambda v: (counts[v], len(v), v))   # most frequent, then the fuller spelling
                canon |= {v: best for v in vs if v != best}
        for i, v in enumerate(t.data[col]):
            if v is None or (col, i) in t.held:
                continue
            low = v.strip().casefold()
            legit = low == top and counts[v] / total >= c["dominant_value_threshold"]
            if v in canon:
                t.set(col, i, canon[v], "D-12", f"'{v}' looks like the same value as '{canon[v]}'; merged.", review=True)
            elif kind == "text" and low in c["suspect_tokens"] and not legit:
                t.set(col, i, None, "D-12", f"'{v}' is a placeholder value; set to NULL.", review=True)
            elif kind in ("numeric", "id") and re.fullmatch(r"-?9{4,}", v):
                t.set(col, i, None, "D-12", f"'{v}' is a sentinel value; set to NULL.", review=True)


def _near_duplicate(x: str, y: str, ratio: float) -> bool:
    """Same text apart from case/spaces/punctuation, or one word misspelt ('Facebok' ~ 'Facebook').
    A different short word ('HCV' vs 'LCV') or symbol ('5% off' vs '₹5 off') is a different value, not a typo."""
    token = r"\w+|[%$€£₹¥#@&+<>=]"
    wx, wy = re.findall(token, x.casefold()), re.findall(token, y.casefold())
    if wx == wy:
        return True
    if len(wx) != len(wy) or SequenceMatcher(None, x, y).ratio() < ratio:
        return False
    return all(len(a) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.75 for a, b in zip(wx, wy) if a != b)


def _decimals(t, col) -> list[tuple[int, Decimal]]:
    """(row index, exact value) for every number in the column; Decimal avoids float noise when rescaling."""
    sym, eu = t.cfg["currency_symbols"], t.eu(col)
    return [(i, Decimal(parse_number(t.data[col][i], sym, eu))) for i, _ in t.numbers(col)]


def units(t):
    """D-22: mixed units are converted to the column's majority unit.
    Percentages: whole numbers (5) and fractions (0.05) -> whichever form most values use.
    Money: a minority of values far above the rest (cents among dollars) are divided by 100."""
    for col in t.cols:
        if t.kind(col) != "numeric":
            continue
        nums = _decimals(t, col)
        xs = [float(d) for _, d in nums]
        if len(xs) < 5 or min(xs) < 0:
            continue
        if has_keyword(col, ["pct", "percent", "rate", "ctr", "ratio"]):
            frac = sum(0 < x <= 1 for x in xs) / len(xs)
            # mostly fractions: any whole number up to 100 is a percentage typed as '3' -> 0.03 (even a single one);
            # mostly whole numbers: fractions are converted only when they are a real group (>= 5%), since 0.5% is valid
            to_fraction = frac >= 0.5
            if max(xs) > 1 and (to_fraction or frac >= 0.05):
                for i, d in nums:
                    if to_fraction and 1 < d <= 100:
                        t.set(col, i, _num_text(d / 100), "D-22", f"{d} looks like a whole-number percentage; most "
                              "values are fractions, so it was divided by 100.", review=True)
                    elif not to_fraction and 0 < d <= 1:
                        t.set(col, i, _num_text(d * 100), "D-22", f"{d} looks like a fraction; most values are "
                              "whole-number percentages, so it was multiplied by 100.", review=True)
                continue
        if not has_keyword(col, t.cfg["money_keywords"]):          # dollars-vs-cents only applies to money columns
            continue
        med = statistics.median(xs)
        pos = [x for x in xs if x > 0]
        if med and pos and max(pos) / min(pos) > t.cfg["unit_ratio"]:
            cut = t.cfg["median_multiplier"] * med
            if 0.05 <= sum(x > cut for x in xs) / len(xs) < 0.5:     # the high group is the minority: cents
                for i, d in nums:
                    if float(d) > cut:
                        t.set(col, i, _num_text(d / 100), "D-22", f"{d} is far above the other values (cents among "
                              "dollars?); divided by 100.", review=True)


def negative_values(t):
    """D-15: a numeric column that has negative values. Flag only: one item for the column (how many, how much of the column,
    where the first ones are), no value is changed. Whether a negative is valid (a refund, a credit, a return, a loss) is
    the data provider's call, not the code's. Cells held by D-29 are not numbers yet and are not counted."""
    for col in t.cols:
        if t.kind(col) != "numeric":
            continue
        nums = _decimals(t, col)
        neg = [(i, d) for i, d in nums if d < 0]
        if not neg:
            continue
        rows = [t.data["_source_row"][i] for i, _ in neg[:3]]
        shown = ", ".join(f"{d.normalize():f}" for _, d in neg[:3])
        t.flag("D-15", col, None, f"'{col}' has {len(neg)} negative value(s) ({len(neg) / len(nums):.1%} of {len(nums)} numbers), "
               f"e.g. {shown} at row {', '.join(str(r) for r in rows if r is not None) or '?'}.",
               "Confirm with the data provider whether negative values are valid in this column. Nothing was changed.")


def _series_key(t) -> list[str]:
    """Category columns (text with 2 to max_categories distinct values): together they split a long table into its
    series (City x Channel ...). Values are judged within their own series, not against unrelated ones."""
    return [col for col in (t.context_cols or t.cols)
            if t.kind(col) == "text" and 1 < len(set(t.values(col))) <= t.cfg["max_categories"]]


def outliers(t):
    """D-14: robust z-score (median/MAD) and order-of-magnitude distance from the median, within each series of the
    table (see _series_key) when the series are long enough, else over the whole column. An outlier is capped to the
    nearest value of its series that is not an outlier (winsorising); the original stays on Needs_Review.
    Capping only happens when outliers are rare (<= outlier_max_share); otherwise one flag for the column / series."""
    c = t.cfg
    key_cols = _series_key(t)
    keys = list(zip(*(t.data[k] for k in key_cols))) if key_cols else None
    for col in t.cols:
        if t.kind(col) != "numeric":
            continue
        nums = t.numbers(col)
        if len(nums) < c["min_outlier_values"]:
            continue
        groups: dict[tuple, list] = {}
        if keys:
            for i, x in nums:
                groups.setdefault(keys[i], []).append((i, x))
        enough = sum(len(g) for g in groups.values() if len(g) >= c["min_outlier_values"])
        if not keys or enough < len(nums) / 2:              # short series: the whole column is the reference
            groups = {(): nums}
        skewed = [key for key, group in groups.items() if len(group) >= c["min_outlier_values"]
                  and not _outliers_in(t, col, group, ", ".join(f"{k}={v}" for k, v in zip(key_cols, key)))]
        if skewed:                                          # one flag for the column, however many series
            where = f"{len(skewed)} of {len(groups)} series" if key_cols and groups.keys() != {()} else "the column"
            t.flag("D-14", col, None, f"{where}: many values far from the median, so the data looks skewed rather "
                   "than mistyped; nothing was capped there.", "Confirm the values are genuine.")


def _outliers_in(t, col: str, nums: list[tuple[int, float]], where: str) -> bool:
    """Cap the outliers of one population (a series, or the whole column); False when it is too skewed to judge.
    A value is an outlier when it is a magnitude slip (>= 10x or <= 1/100 of the median), or when its robust z-score
    is extreme on the plain scale and - for positive data - on the log scale too: a typing slip is extreme on both,
    the natural long tail of counts or spend only on one."""
    c = t.cfg
    xs = sorted(x for _, x in nums)
    med = statistics.median(xs)
    mad = statistics.median(abs(x - med) for x in xs)
    logs = [math.log10(x) for x in xs] if xs[0] > 0 else None
    lmed = statistics.median(logs) if logs else 0
    lmad = statistics.median(abs(y - lmed) for y in logs) if logs else 0
    scope = f"the series {where}" if where else "the column"
    found = {}
    for i, x in nums:
        why = []
        z = 0.6745 * (x - med) / mad if mad else 0
        lz = 0.6745 * (math.log10(x) - lmed) / lmad if logs and lmad else z
        if abs(z) > c["outlier_z"] and abs(lz) > c["outlier_z"]:
            why.append(f"robust z-score {z:.1f}")
        f = c["magnitude_factor"]                          # an extra digit (>= 10x) or two digits missing (<= 1/100)
        if med and x and (abs(x) >= f * abs(med) or abs(x) <= abs(med) / f ** 2):
            why.append(f"{abs(x / med):.3g}x the median of {scope}")
        if why:
            found[i] = (x, why, 100 * bisect_left(xs, x) / len(xs))
    normal = [(x, i) for i, x in nums if i not in found]
    if not normal or not found:
        return True
    if len(found) > max(1, c["outlier_max_share"] * len(nums)):   # many "outliers" = skewed data, not entry errors
        return False
    (_, lo_i), (hi, hi_i) = min(normal), max(normal)
    for i, (x, why, pct) in found.items():
        cap = t.data[col][hi_i] if x > hi else t.data[col][lo_i]
        t.set(col, i, cap, "D-14", f"Outlier: {', '.join(why)} (median {med:g}, percentile {pct:.0f}); "
              f"capped to {cap}, the nearest normal value.", review=True)
    return True


def precision(t):
    """D-21: more than 15 integer digits, or an account/phone-like identifier, cannot be stored safely as a number:
    the column is kept as text in the output (logged once per column)."""
    limit = t.cfg["max_significant_digits"]
    for col in t.cols:
        nums = [parse_number(v) for v in t.values(col)]
        nums = [n for n in nums if n is not None]
        if not nums or len(nums) < 0.8 * len(t.values(col)):
            continue
        if any(integer_digits(n) > limit for n in nums):
            why = f"values have more than {limit} digits, which a number type would round"
        elif has_keyword(col, ["phone", "mobile", "account", "card"]):
            why = "identifier-like column (account/phone number)"
        else:
            continue
        if t.cfg["fixes"].get("D-21", True):
            t.text_cols.add(col)
            t.audit.append(t._entry("D-21", col, None, action="fixed", old_value="number", new_value="text", note=why))
            t.counts["D-21"]["fixed"] += 1
        else:
            t.flag("D-21", col, None, why[0].upper() + why[1:] + ".", "Read this column as text.")


COMMON_SHORT = {"id", "no", "of", "to", "by", "in", "on", "at", "vs", "tv", "uk", "us", "eu", "pc", "ad", "cpc", "cpm",
                "ctr", "roi", "roas", "sku", "qty", "pct", "url", "gst", "vat", "tax", "zip", "pin", "otp",
                "dd", "mm", "yy", "hh"}


def column_names(t):
    """D-16: a column listed in the data dictionary with a standard name is renamed to it. Ambiguous names
    (abbreviations, generic or numbered names) and names missing from the dictionary cannot be fixed by code, so
    they go to the client. Ordinary snake_case or Title Case names are fine."""
    documented = t.cfg.get("_dictionary")                          # {name casefolded: standard name or None}
    for col in list(t.cols):
        key = col.strip().casefold()
        standard = (documented or {}).get(key)
        if standard and standard != col and standard not in t.data:
            t.rename(col, standard, "D-16")
            continue
        words = [w for w in re.split(r"[^A-Za-z0-9]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", col)) if w]
        cryptic = (not words or re.fullmatch(r"(col|column|field|unnamed)[ _:]*\d*", col, re.I)
                   or col.casefold() in {"value", "data", "misc", "other", "temp", "tmp", "x", "y", "flag"}
                   or any(len(w) <= 2 and w.isalpha() and w.lower() not in COMMON_SHORT for w in words)
                   or (re.fullmatch(r"[A-Z]{2,6}", col) and col.lower() not in COMMON_SHORT)
                   or re.search(r"_(adj|tmp)$", col) or "_x_" in col)
        if documented is not None and key not in documented:
            t.flag("D-16", col, None, f"Column '{col}' is not in the data dictionary.",
                   "Add it to the data dictionary (name, standard name) and re-run.", sev="info")
        elif cryptic and documented is None:
            t.flag("D-16", col, None, f"Column name '{col}' is ambiguous.",
                   "Add it to a data dictionary (name, standard name) and re-run.", sev="info")


def truncation(t):
    """D-23: many values stop at exactly the same length, which suggests a fixed-width source field.
    The missing characters cannot be recovered from the data, so this one stays a flag for the client."""
    p = t.cfg["truncation"]
    for col in t.cols:
        if t.kind(col) != "text":
            continue
        lens = Counter(len(v) for v in t.values(col))
        if not lens:
            continue
        longest = max(lens)
        at_max = {v for v in t.values(col) if len(v) == longest}     # one repeated phrase is a constant, not truncation
        if (longest >= p["min_length"] and len(at_max) > 1 and lens[longest] >= p["at_max_count"]
                and lens[longest] / sum(lens.values()) >= p["at_max_fraction"]):
            t.flag("D-23", col, None, f"{lens[longest]} values are exactly {longest} characters; they may be truncated.",
                   "Ask the client about the source field length.", sev="info")


RULES = [headers, errors_and_formulas, encoding, control_chars, whitespace_and_blanks, placeholders,
         multi_value_cells,                                              # D-29: flags only, and holds the cells the rules below must not touch
         city_names,                                                     # D-30: flags only, one item per spelling
         numbers_read_as_dates, date_serials, dates, numbers, leading_zeros, case,
         near_duplicates_and_sentinels, units, negative_values, outliers, precision,
         isolated_gaps,                                                  # D-27: after every value fix, before rows are dropped
         duplicates,                                                     # last fix: catches rows the fixes made equal
         additive_columns,                                               # D-28: flags only, on the final rows
         date_order, expected_columns,                                   # D-31, D-32: flag only
         column_names, truncation]
