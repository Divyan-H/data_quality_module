"""A workbook for trying the newest Phase B checks in the web app, one sheet per scenario, plus the run inputs to paste.

    python tests/fixtures/build_dq_feature_workbook.py samples/dq_feature_tests

Writes dq_feature_test.xlsx, dq_feature_test_inputs.yaml and README.md (what each sheet must show). Upload the workbook, accept
Phase A with every sheet selected, paste the YAML into the run inputs, then run Phase B.

Sheets
    City_Variants    D-30  the same city under other names, short forms and spelling mistakes (plus guards that must NOT be flagged)
    Dates_Order      D-31  one series in order, one newest-first, one with two rows swapped, one with a row out of place
    Cols_*           D-32  expected columns written in different ways, with typos, or missing
    Currency         D-33  rupee symbol, and $ and EUR symbols
    Column_Choice    run inputs: only some columns are checked, the others are passed on as read
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import openpyxl

START = date(2026, 3, 1)

CLIENT_CITIES = ["Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Chennai", "Kolkata", "Pune"]

# (spelling, how many rows); the first spelling of a group is the standard one
CITY_ROWS = [
    ("Bengaluru", 20), ("Bangalore", 6), ("BLR", 3), ("Banglore", 2),
    ("Mumbai", 20), ("Bombay", 5), ("Mum", 2), ("Mumbay", 1),
    ("Delhi", 18), ("New Delhi", 4), ("Dehli", 2), ("Dilli", 1), ("Delhi NCR", 3),
    ("Hyderabad", 15), ("Hyd", 3), ("Hyderbad", 2),
    ("Chennai", 12), ("Madras", 3),
    ("Kolkata", 12), ("Calcutta", 3),
    ("Pune", 10), ("Poona", 2),
    ("Jaipur", 6), ("Jaipure", 1),
    ("Springfield", 2), ("Qwertyville", 2),
    ("Others", 2), ("India", 2),
]

# (sheet, what is planted, what Phase B must show)
PLAN = [
    ("City_Variants", "Bangalore (6), BLR (3), Banglore (2) beside Bengaluru (20)", "D-30 warn, 3 items, proposed Bengaluru; Accept fixes all cells of that spelling"),
    ("City_Variants", "Bombay (5), Mum (2), Mumbay (1) beside Mumbai (20)", "D-30 warn, 3 items, proposed Mumbai"),
    ("City_Variants", "New Delhi (4), Dehli (2), Dilli (1) beside Delhi (18)", "D-30 warn, 3 items, proposed Delhi"),
    ("City_Variants", "Hyd (3), Hyderbad (2); Madras (3); Calcutta (3); Poona (2)", "D-30 warn, proposed Hyderabad / Chennai / Kolkata / Pune"),
    ("City_Variants", "Delhi NCR (3)", "D-30 info: a known name that is not in the client's list; NOT merged into Delhi"),
    ("City_Variants", "Jaipur (6) is a real city but not in the client's list", "D-30 info: 'a known city that is not in the project's city list'"),
    ("City_Variants", "Jaipure (1)", "D-30 warn: a misspelling of Jaipur, which is not in the client's list; proposed Jaipur"),
    ("City_Variants", "Springfield (2), Qwertyville (2)", "D-30 warn: not in the client's list, nothing proposed"),
    ("City_Variants", "Others (2), India (2)", "NOT flagged: placeholders are skipped"),
    ("Dates_Order", "Meta / Facebook / Mumbai: dates ascending", "no D-31"),
    ("Dates_Order", "Google / Search / Delhi: newest first", "counted as 'fall', not an error by itself"),
    ("Dates_Order", "Meta / Instagram / Pune: two rows swapped", "D-31 warn: one item for the sheet, '2 of 4 series are not in date order', first place named (the sheet's other series are fine)"),
    ("Dates_Order", "Google / Display / Chennai: one row moved to the end", "counted in the same D-31 item"),
    ("Dates_NewestFirst", "Every series runs newest first", "D-31 info: all series are sorted newest first"),
    ("Cols_Exact", "Date, Platform, SubPlatform, City", "no D-32"),
    ("Cols_Varied", "Week Start, Channel, Sub-Platform, City Name", "no D-32 (synonyms and longer headers)"),
    ("Cols_Typo", "Dat, Platfrom, SubPlatfrom, Cty", "no D-32 (typos)"),
    ("Cols_Missing", "No sub-platform column", "D-32: subplatform missing"),
    ("Cols_Wrong", "'Sub Platform Name' only", "D-32: platform missing; subplatform is found"),
    ("Cols_AllMissing", "Only 'Week' and 'Revenue'", "D-32: platform, subplatform and city missing; date is found"),
    ("Currency", "Spend with 12 rupee-symbol values, 2 dollar and 1 euro", "D-33 info (rupee, read as INR), D-33 warn for $ and for EUR; amounts unchanged, symbols removed"),
    ("Column_Choice", "Run inputs choose only Date, City, Spend; Remarks has padded text", "Remarks passed on exactly as read; column_choice lists checked / not checked"),
]


def dates(n, start=START):
    return [start + timedelta(days=i) for i in range(n)]


def build(out_dir) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    head = ["Date", "Platform", "SubPlatform", "City", "Spend"]

    ws = wb.create_sheet("City_Variants")
    ws.append(head)
    for city, n in CITY_ROWS:                                         # each city's rows run oldest first
        for k in range(n):
            ws.append([START + timedelta(days=k), "Meta" if k % 2 else "Google", "Social" if k % 2 else "Search", city, 400 + k * 3])

    ws = wb.create_sheet("Dates_Order")
    ws.append(head)
    series = [("Meta", "Facebook", "Mumbai"), ("Google", "Search", "Delhi"), ("Meta", "Instagram", "Pune"), ("Google", "Display", "Chennai")]
    for k, (plat, sub, city) in enumerate(series):
        days = dates(20)
        if k == 1:
            days = days[::-1]                                         # newest first
        if k == 2:
            days[8], days[9] = days[9], days[8]                       # two rows swapped
        if k == 3:
            days.append(days.pop(4))                                  # one row moved to the end
        for i, d in enumerate(days):
            ws.append([d, plat, sub, city, 100 + k * 50 + i])

    ws = wb.create_sheet("Dates_NewestFirst")
    ws.append(head)
    for plat, sub, city in series[:2]:
        for i, d in enumerate(dates(15)[::-1]):
            ws.append([d, plat, sub, city, 100 + i])

    def cols_sheet(name, header, drop=()):
        ws = wb.create_sheet(name)
        ws.append(header)
        for i, d in enumerate(dates(12)):
            row = [d, "Meta", "Facebook", "Mumbai", 100 + i]
            ws.append([v for j, v in enumerate(row) if j not in drop][:len(header)])

    cols_sheet("Cols_Exact", ["Date", "Platform", "SubPlatform", "City", "Spend"])
    cols_sheet("Cols_Varied", ["Week Start", "Channel", "Sub-Platform", "City Name", "Spend"])
    cols_sheet("Cols_Typo", ["Dat", "Platfrom", "SubPlatfrom", "Cty", "Spend"])
    cols_sheet("Cols_Missing", ["Date", "Platform", "City", "Spend"], drop=(2,))
    ws = wb.create_sheet("Cols_Wrong")
    ws.append(["Date", "Sub Platform Name", "City", "Spend"])
    for i, d in enumerate(dates(12)):
        ws.append([d, "Facebook", "Mumbai", 100 + i])
    ws = wb.create_sheet("Cols_AllMissing")
    ws.append(["Week", "Revenue"])
    for i, d in enumerate(dates(12)):
        ws.append([d, 1000 + i * 5])

    ws = wb.create_sheet("Currency")
    ws.append(head)
    for i, d in enumerate(dates(30)):
        spend = f"₹{1000 + i * 10:,}" if i < 12 else (f"${50 + i}" if i in (15, 16) else ("€70" if i == 20 else 1000 + i * 10))
        ws.append([d, "Meta", "Facebook", "Mumbai", spend])

    ws = wb.create_sheet("Column_Choice")
    ws.append(["Date", "Platform", "SubPlatform", "City", "Spend", "Remarks"])
    for i, d in enumerate(dates(12)):
        ws.append([d, "Meta", "Facebook", "Mumbai", 100 + i, "  padded  note " if i % 2 else "x"])

    paths = {"workbook": out / "dq_feature_test.xlsx", "inputs": out / "dq_feature_test_inputs.yaml", "readme": out / "README.md"}
    wb.save(paths["workbook"])
    cities = "\n".join(f"  - {c}" for c in CLIENT_CITIES)
    paths["inputs"].write_text(f"""# Paste into the run inputs after Phase A is accepted (or PUT /api/v1/runs/<id>/inputs).
project: DQ feature test

expected_columns:
  all_sheets: [date, platform, subplatform, city]

columns_to_check:
  - table: "*"
    columns: all
  - table: Column_Choice          # only these three are checked; Remarks is passed on as read
    columns: [Date, City, Spend]

# The client's own list: its spellings are the standard. Jaipur is left out on purpose.
cities:
{cities}

currency:
  default: INR
  rates_level: quarterly
  rates: {{}}
""", encoding="utf8")
    lines = ["# DQ feature test workbook", "",
             "Upload `dq_feature_test.xlsx`, select every sheet at Phase A and accept it, paste `dq_feature_test_inputs.yaml` into the "
             "run inputs, then run Phase B. Rows below say what each sheet plants and what Phase B must show.", "",
             "| Sheet | Planted | Phase B must show |", "|---|---|---|"]
    lines += [f"| {s} | {p} | {e} |" for s, p, e in PLAN]
    lines += ["", "Review checks: answer a D-30 item with Accept in the review workbook and upload it; every cell with that spelling "
                  "changes at once. Leave the client's city list out of the inputs to see the other mode (no list: unknown spellings are "
                  "compared with each other)."]
    paths["readme"].write_text("\n".join(lines) + "\n", encoding="utf8")
    return paths


if __name__ == "__main__":
    for k, p in build(sys.argv[1] if len(sys.argv) > 1 else "samples/dq_feature_tests").items():
        print(k, p)
