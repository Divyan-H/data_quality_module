"""Pre-filled client config workbook for Phase D: profile the data, fill in everything it tells us, ask only for the rest.

    python -m src.trends.cli init-config <phase-C-run-or-file> -o client_config.xlsx

Filled from the data (green, with an Autofill_Note saying how it was guessed - all editable, nothing is locked):
  Data File           Period / Geography / Variables columns
  Variable Mapping    every numeric variable: Description, Bucket, Aggregation 1, Spend/Support type, the Spend Variables
                      pairing and Y/N (a Support variable with no spend column is still Y: Phase D handles it)
  Period Mapping      every distinct date with calendar month / quarter / half-year / year labels
Left for you (yellow): the thresholds (% changes, Robust Z-score, spend concentration), Ratio Definitions, and any
Bucket or pairing the names could not decide. Spec DP-D §3.6 forbids default thresholds, so none are invented: a rule
left blank does not run. The same workbook works for Phase C (it reads the sheets it needs and ignores the rest).
"""
from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .config import load_settings

from src.common.errors import ConfigError
from .io import load_input
from src.common.dataset import Dataset
from src.common.granularity import find_period_column, is_time_label
from src.common.values import Periods, month_label
from src.common.values import classify

AUTO = PatternFill("solid", fgColor="E2EFDA")       # green: filled from the data, check it
ASK = PatternFill("solid", fgColor="FFF2CC")        # yellow: you enter this
HEAD = PatternFill("solid", fgColor="1F4E78")
NOTE_FONT = Font(italic=True, color="595959")
GEO_WORDS = {"geography", "geo", "region", "country", "market", "dma", "state", "city", "area", "territory", "location"}
DATE_WORDS = {"date", "week", "month", "period", "day", "time"}
SPEND_WORDS = {"spend", "spends", "cost", "costs", "budget", "investment", "expense", "outlay"}
SUPPORT_WORDS = {"impressions", "impression", "clicks", "click", "views", "view", "reach", "grp", "grps", "trp", "trps",
                 "sessions", "leads", "conversions", "orders", "installs", "visits", "engagements", "plays", "support",
                 "volume", "spots", "insertions"}
GENERIC = {"total", "tot", "weekly", "wkly"}
RULES = [   # (rule, note) - every one is a threshold you enter; blank = the rule does not run
    ("MOM % change", "N-17. Enter a percentage as 30, not 0.30. Blank = this rule does not run."),
    ("QOQ % change", "N-17. Blank = this rule does not run."),
    ("HOH % change", "N-17. Blank = this rule does not run."),
    ("YOY % change", "N-17. Blank = this rule does not run."),
    ("Abnormal trend - Robust Z-score", "N-16. e.g. 3.5. Blank = this rule does not run."),
    ("Spend concentration - Top tactics count", "N-20. How many top tactics to add up (whole number). Needs the % below."),
    ("Spend concentration %", "N-20. Flag when the top tactics hold more than this share of spend. Needs the count above."),
]


def tokens(name: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced) if t]


def has_word(name: str, words: set[str]) -> bool:
    return any(t.lower() in words for t in tokens(name))


def stem(name: str) -> str:
    """Name without its metric words: 'TV_Impressions' and 'TV_Spend' both give 'tv'."""
    return " ".join(t.lower() for t in tokens(name) if t.lower() not in SPEND_WORDS | SUPPORT_WORDS | GENERIC)


def var_type(name: str) -> str:
    low = {t.lower() for t in tokens(name)}
    return "Spend" if low & SPEND_WORDS else "Support" if low & SUPPORT_WORDS else ""


def profile(ds: Dataset, cfg: dict) -> dict:
    """Which columns are the Period, Geography and numeric variables of this table."""
    cols = ds.columns
    kinds = {c: [classify(v) for v in ds.data[c]] for c in cols}
    num_share = {c: sum(k == "number" for k, _ in kinds[c]) / max(1, sum(k != "blank" for k, _ in kinds[c])) for c in cols}
    period = find_period_column(ds, cfg["dayfirst"])
    if period is None:
        raise ConfigError("no Period column found: no column of the table holds mostly dates")
    geo = next((c for c in cols if c != period and has_word(c, GEO_WORDS)), None)
    if geo is None:                                       # a low-cardinality text column before the first number
        first_num = next((c for c in cols if num_share[c] >= 0.5 and c != period), cols[-1])
        geo = next((c for c in cols[:cols.index(first_num)] if c != period and num_share[c] < 0.5
                    and len({v for v in ds.data[c] if v is not None}) <= 50), None)
    numeric = [c for c in cols if c not in (period, geo) and not is_time_label(ds, c) and num_share[c] >= 0.5
               and any(k == "number" for k, _ in kinds[c])]
    if not numeric:
        raise ConfigError("no numeric variable columns found")
    first = cols.index(numeric[0])
    if geo is not None and cols.index(geo) > first:
        first = min(first, cols.index(geo))               # Variables = that column and every later one
    return {"period": period, "geo": geo, "first_var": cols[first], "numeric": numeric}


def plan_variables(ds: Dataset, prof: dict) -> list[dict]:
    """One row per distinct variable name: description, type, bucket, spend pairing."""
    seen, rows = set(), []
    for c, raw in zip(ds.columns, ds.raw_header):
        name = " ".join(raw.split())
        if c not in prof["numeric"] or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        st = stem(name)
        rows.append({"name": name, "type": var_type(name), "stem": st,
                     "desc": " ".join(tokens(name)) or name, "bucket": st.title() if st else name})
    spends: dict[str, list[str]] = {}
    only = [r["name"] for r in rows if r["type"] == "Spend"]   # a table with one spend column pairs every Support with it
    for r in rows:
        if r["type"] == "Spend" and r["stem"]:
            spends.setdefault(r["stem"], []).append(r["name"])
    paired = set()
    for r in rows:
        r["pair"], r["pair_note"] = "", ""
        if r["type"] == "Support":
            m = spends.get(r["stem"], []) if r["stem"] else []
            if not m and len(only) == 1:
                m = only
            if len(m) == 1:
                r["pair"], r["pair_note"] = m[0], f"paired with '{m[0]}' " + ("(same name apart from the metric word)" if r["stem"] and m is not only
                                                          else "(the table's only spend variable)")
                paired.add(m[0])
            elif len(m) > 1:
                r["pair_note"] = f"several spend variables match ({', '.join(m)}): pick one, or leave blank for none"
            else:
                r["pair_note"] = "no spend variable with the same name: pick one, or leave blank (checked without spend)"
    for r in rows:
        r["yn"] = "Y" if r["type"] == "Support" else ""
        if r["type"] == "Spend" and r["name"] not in paired:
            r["pair_note"] = "a spend with no Support variable: not in any pair, so N-20 does not see it"
    return rows


def _style_header(ws, n: int) -> None:
    for c in ws[1][:n]:
        c.fill, c.font = HEAD, Font(color="FFFFFF", bold=True)
    ws.freeze_panes = "A2"


def _widths(ws, widths: dict) -> None:
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def collect(cands, cfg: dict, plan_of) -> dict:
    """What the template needs from every table it can describe, read one table at a time: the first table's columns
    (by name, so the config fits every table with the same headers), each variable once, every date, one line per
    table. Raises ConfigError when no table has a Period column and a numeric variable."""
    first, plan, dates, tables, seen, skipped = None, [], set(), [], set(), []
    for ds in cands:
        try:
            prof = profile(ds, cfg)
        except ConfigError as e:
            skipped.append(f"{ds.ref} ({ds.sheet or ds.source_file or '-'}): {e}")
            continue
        first = first or prof
        for r in plan_of(ds, prof):
            if r["name"].casefold() not in seen:
                seen.add(r["name"].casefold())
                plan.append(r)
        parse = Periods(ds.data[prof["period"]], cfg["dayfirst"])
        dates |= {d for d in map(parse, ds.data[prof["period"]]) if d}
        tables.append((ds.ref, f"{ds.source_file or '-'} / {ds.sheet or '-'} ({ds.ref}), {len(ds.row_ids)} rows"))
    if first is None:
        raise ConfigError("no table has a date column and a numeric variable: " + ("; ".join(skipped) or "no table found"))
    return {"prof": first, "plan": plan, "dates": sorted(dates), "tables": tables}


def build(found: dict, cfg: dict, path: Path) -> dict:
    """Write the pre-filled config workbook from `collect`; returns what was filled and what is left to enter."""
    prof, plan, dates = found["prof"], found["plan"], found["dates"]
    wb = openpyxl.Workbook()
    to_enter = 0

    ws = wb.active
    ws.title = "Read_Me_First"
    for line in [
            "Client config workbook - pre-filled from the data. Nothing is locked: change any cell.",
            "",
            "GREEN  = filled from your data. Check it; the Autofill_Note column says how it was decided.",
            "YELLOW = you enter this (thresholds, ratios, anything the names could not decide).",
            "White  = optional.",
            "",
            "Sheets: Data File - Variable Mapping - Period Mapping - Data Check Specifications - Ratio Definitions.",
            "A rule with a blank threshold simply does not run (Phase D spec 3.6: there are no default thresholds);",
            "Rules_Used in the report lists every rule that did not run.",
            "Percentages are entered as 30, not 0.30.",
            "One workbook describes every table below; a table whose columns it does not fit is reported and passed on.",
            *(f"Table: {line}" for _, line in found["tables"])]:
        ws.append([line])
    ws["A1"].font = Font(bold=True, size=13)
    ws["A3"].fill, ws["A4"].fill = AUTO, ASK
    ws.column_dimensions["A"].width = 110

    ws = wb.create_sheet("Data File")
    ws.append(["Description", "Column", "Autofill_Note"])
    geo = prof["geo"]
    for d, col, note in [
            ("Period", prof["period"], "the first column holding dates"),
            ("Geography", geo, "a geography-like column (Phase D does not use it)" if geo else
             "no geography-like column found (Phase D does not use it; Phase C asks for it)"),
            ("Variables", prof["first_var"], "first numeric column; it and every later column are variables")]:
        ws.append([d, col, note])
        ws.cell(ws.max_row, 2).fill = AUTO if col else PatternFill()
        ws.cell(ws.max_row, 3).font = NOTE_FONT
    _style_header(ws, 3)
    _widths(ws, {"A": 16, "B": 24, "C": 90})

    ws = wb.create_sheet("Variable Mapping")
    head = ["Variables", "Description", "Bucket", "Spend Variables", "Y/N", "Aggregation 1", "Variable_Type",
            "Autofill_Note"]
    ws.append(head)
    for r in plan:
        note = [f"type '{r['type']}' from the name" if r["type"] else "type not recognised from the name: set Y/N yourself",
                f"bucket '{r['bucket']}' = name without the metric word" if r["bucket"]
                else "no bucket guess: enter one (N-20 groups by Bucket and Aggregation 1)"]
        if r["pair_note"]:
            note.append(r["pair_note"])
        if r["bucket"] and r["yn"]:
            note.append("Aggregation 1 groups tactics for N-20 (e.g. Digital, Offline): it starts as the bucket, one "
                        "tactic per group, so change it to get a concentration check")
        ws.append([r["name"], r["desc"], r["bucket"] or None, r["pair"] or None, r["yn"] or None,
                   r["bucket"] or None, r["type"] or None, "; ".join(note)])
        row = ws.max_row
        for j in (1, 2, 3, 7):
            ws.cell(row, j).fill = AUTO
        ws.cell(row, 6).fill = ASK if r["type"] == "Spend" or r["pair"] else AUTO     # N-20 groups: you decide
        ws.cell(row, 4).fill = AUTO if r["pair"] else (ASK if r["type"] == "Support" else PatternFill())
        ws.cell(row, 5).fill = AUTO if r["yn"] else ASK
        if not r["bucket"]:
            ws.cell(row, 3).fill = ws.cell(row, 6).fill = ASK
            to_enter += 1
        if not r["yn"]:
            to_enter += 1
        ws.cell(row, 8).font = NOTE_FONT
    last = max(ws.max_row, 2)
    for formula, cells in (('"Y,N"', f"E2:E{last}"), (f"=$A$2:$A${last}", f"D2:D{last}")):
        dv = DataValidation(type="list", formula1=formula, allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(cells)
    _style_header(ws, len(head))
    _widths(ws, {"A": 26, "B": 26, "C": 16, "D": 22, "E": 6, "F": 16, "G": 14, "H": 90})

    ws = wb.create_sheet("Period Mapping")
    ws.append(["Period", "Months", "Quarters", "Half Years", "Full Years"])
    for d in dates:
        ws.append([d, month_label(d), f"Q{(d.month - 1) // 3 + 1} {d.year}", f"H{(d.month - 1) // 6 + 1} {d.year}",
                   str(d.year)])
        ws.cell(ws.max_row, 1).number_format = "yyyy-mm-dd"
        for j in range(1, 6):
            ws.cell(ws.max_row, j).fill = AUTO
    ws.cell(1, 7, "Autofill_Note").font = NOTE_FONT
    ws.cell(2, 7, "Calendar months / quarters / half-years / years. For a fiscal year, change the labels here. "
                  "Delete a date to leave it off the Trend Line.").font = NOTE_FONT
    _style_header(ws, 5)
    _widths(ws, {"A": 14, "B": 16, "C": 12, "D": 12, "E": 12, "G": 90})

    ws = wb.create_sheet("Data Check Specifications")
    head = ["Rule", "Threshold", "Autofill_Note"]
    ws.append(head)
    for rule, note in RULES:
        ws.append([rule, None, note])
        ws.cell(ws.max_row, 2).fill = ASK
        ws.cell(ws.max_row, 3).font = NOTE_FONT
        to_enter += 1
    _style_header(ws, len(head))
    _widths(ws, {"A": 40, "B": 11, "C": 90})

    ws = wb.create_sheet("Ratio Definitions")
    head = ["Ratio Name", "Numerator", "Denominator", "Threshold", "Autofill_Note"]
    ws.append(head)
    ideas = [f"{r['pair']} / {r['name']}" for r in plan if r["pair"]]
    ws.append([None, None, None, None, "N-18, optional: one row per ratio checked (e.g. CPM). Numerator and Denominator are "
                                       "variables of Variable Mapping (pick from the lists); the Threshold is this ratio's "
                                       "own % change. Leave empty for no ratio checks."
                                       + (f" Possible: {'; '.join(ideas[:6])}." if ideas else "")])
    ws.cell(2, 5).font = NOTE_FONT
    for r in range(2, 22):
        for j in range(1, 5):
            ws.cell(r, j).fill = ASK
    dv = DataValidation(type="list", formula1=f"='Variable Mapping'!$A$2:$A${last}", allow_blank=True)
    ws.add_data_validation(dv)
    dv.add("B2:C21")
    _style_header(ws, len(head))
    _widths(ws, {"A": 22, "B": 26, "C": 26, "D": 11, "E": 100})

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return {"path": str(path), "tables": [ref for ref, _ in found["tables"]], "period": prof["period"], "geography": geo,
            "first_variable": prof["first_var"], "variables": len(plan), "paired": sum(bool(r["pair"]) for r in plan),
            "marked_y": sum(r["yn"] == "Y" for r in plan), "dates": len(dates), "cells_for_you": to_enter,
            "type_not_recognised": [r["name"] for r in plan if not r["type"]]}


def init_config(input_path: str, out: str, settings: str | None = None, table: str | None = None,
                sheet: str | None = None) -> dict:
    """Profile every table under `input_path` (or those matching `table`) and write one pre-filled workbook to `out`."""
    cfg = load_settings(settings)
    cands = (d for d in load_input(input_path, sheet)[0] if not table or fnmatch(d.ref, table))
    return build(collect(cands, cfg, plan_variables), cfg, Path(out))
