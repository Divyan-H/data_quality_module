"""One workbook that plants something for every recent feature, plus a Plan sheet that says what each plant should do.

    python tests/fixtures/build_e2e_workbook.py samples/e2e_feature_check.xlsx

Sheets
    Plan               what is planted and what the pipeline must do (Phase A finds it as a small table; the tests never select it)
    Daily_Media        D-29 (lists, an ambiguous comma), D-15 (negatives), D-27 (one isolated zero), D-28 (a total column)
    Monthly_Competitor Year-Month labels only: Phase C needs the monthly-to-weekly step; Sales is a sum, AOV a rate
    Weekly_TV          Phase C and D findings: a repeated value, a missing week, a long run of zeros
    City_Dates         D-30 city spellings, D-31 dates out of order, D-32 an expected column that is missing
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import openpyxl

DAILY_DAYS = 90

# (sheet, planted, expected)
PLAN = [
    ("Daily_Media", "Channel 'Search; Social' and 'Search / Display' (rows 13, 14)", "D-29 items, values left as written until answered"),
    ("Daily_Media", "Channel 'R&D' and 'AT&T' (rows 43, 44)", "NOT flagged: single names that contain an ampersand"),
    ("Daily_Media", "Spend '300;400' (row 22)", "D-29: two numbers in one cell, column stays text until answered"),
    ("Daily_Media", "Clicks '12,34' (row 32)", "D-29: ambiguous comma, proposes 12.34"),
    ("Daily_Media", "Refunds has -5 and -12 (rows 6, 16)", "D-15: one column item for Refunds, nothing changed; no other column flagged"),
    ("Daily_Media", "Orders has one isolated 0 (row 53)", "D-27 (switched on): filled with the mean of its neighbours"),
    ("Daily_Media", "All media = Search + Social + Display", "D-28: a total column, listed with its parts"),
    ("Monthly_Competitor", "36 months of Year-Month labels, no date column", "Phase C runs on weeks: Sales summed, AOV averaged"),
    ("Weekly_TV", "TV_Spend repeats 1200 for weeks 20-22", "Phase C N-08"),
    ("Weekly_TV", "Week 30 is missing", "Phase C N-12 (missing period)"),
    ("Weekly_TV", "TV_Impressions is 0 for weeks 40-49", "Phase C N-07 (no executions, 9 or more weeks)"),
    ("City_Dates", "City spellings: Bangalore / Bengaluru / BLR, Mumbai / Bombay, Pune / Poona", "D-30: one item per spelling, standard name proposed; one answer fixes every cell"),
    ("City_Dates", "Two Pune rows swapped (rows 14, 15)", "D-31: Pune is not in date order, first place named"),
    ("City_Dates", "No Impressions column (the run inputs expect it)", "D-32: the expected column is missing"),
]


def build(path) -> Path:
    path = Path(path)
    rnd = random.Random(7)
    wb = openpyxl.Workbook()
    plan = wb.active
    plan.title = "Plan"
    plan.append(["End-to-end feature check workbook"])
    plan.append(["Each row says what is planted and what the pipeline must do. Do not use this sheet as data."])
    plan.append([])
    plan.append(["Sheet", "Planted", "Expected"])
    for row in PLAN:
        plan.append(list(row))

    ws = wb.create_sheet("Daily_Media")
    ws.append(["Date", "City", "Channel", "Spend", "Clicks", "Orders", "Refunds", "Rain", "Search", "Social", "Display", "All media"])
    for i in range(DAILY_DAYS):
        a, b, c = (rnd.randint(20, 90) for _ in range(3))
        channel = "Paid"                                   # one series: the label columns split a table into series
        spend, clicks = 1000 + i * 7, 200 + i
        orders = 100 + i * 2
        refunds = 0
        if i == 11:
            channel = "Search; Social"
        if i == 12:
            channel = "Search / Display"
        if i == 41:
            channel = "R&D"
        if i == 42:
            channel = "AT&T"
        if i == 20:
            spend = "300;400"
        if i == 30:
            clicks = "12,34"
        if i == 4:
            refunds = -5
        if i == 14:
            refunds = -12
        if i == 51:
            orders = 0
        ws.append([date(2026, 1, 5) + timedelta(days=i), "Pune", channel, spend, clicks, orders, refunds,
                   0 if i % 2 else 3 + i % 4, a, b, c, a + b + c])

    ws = wb.create_sheet("Monthly_Competitor")
    ws.append(["Competitor", "City", "Year", "Month", "Year-Month", "Sales", "AOV"])
    for m in range(36):
        y, mo = 2022 + m // 12, m % 12 + 1
        for comp, base in (("Alpha", 1000), ("Beta", 700)):
            ws.append([comp, "Pune", y, mo, f"{y}-{mo:02d}", base + m * 13, round(40 + (m % 5) * 1.5 + (2 if comp == "Beta" else 0), 2)])

    ws = wb.create_sheet("Weekly_TV")
    ws.append(["Period", "Geography", "TV_Spend", "TV_Impressions"])
    for w in range(60):
        if w == 29:
            continue
        spend = 1200 if 19 <= w <= 21 else 1000 + w * 11
        impressions = 0 if 39 <= w <= 48 else 90000 + w * 700
        ws.append([date(2024, 1, 1) + timedelta(days=7 * w), "National", spend, impressions])
    ws = wb.create_sheet("City_Dates")
    ws.append(["Date", "City", "Spend", "Clicks"])
    spellings = {"Bengaluru": ["Bengaluru"] * 9 + ["Bangalore"] * 8 + ["BLR"] * 3, "Mumbai": ["Mumbai"] * 14 + ["Bombay"] * 6,
                 "Pune": ["Pune"] * 16 + ["Poona"] * 4}
    for city, names in spellings.items():
        rows = [[date(2026, 2, 1) + timedelta(days=i), names[i], 500 + i * 3, 50 + i] for i in range(20)]
        if city == "Pune":
            rows[13][0], rows[14][0] = rows[14][0], rows[13][0]            # two dates swapped
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


if __name__ == "__main__":
    out = build(sys.argv[1] if len(sys.argv) > 1 else "samples/e2e_feature_check.xlsx")
    print("wrote", out)
