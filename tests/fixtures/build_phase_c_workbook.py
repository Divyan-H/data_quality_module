"""Phase C issue workbook: 60 weekly rows with one planted case for every check (N-01 to N-15), the client config
workbook that goes with it, and EXPECTED - exactly the findings Phase C must report.

    python tests/fixtures/build_phase_c_workbook.py

writes samples/phase_c_weekly.xlsx and samples/config/phase_c_client_config.xlsx and runs Phase C into out_phase_c/.
The data's header is on row 1, column A, so Data_Validation_Report_fixed.py can read the same two files.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[2]
START = date(2024, 1, 1)                     # a Monday
WEEKS = 60
HEADER = ["Period", "Geography", "TV_Spend", "TV_Impressions", "Radio_Spend", "Radio_Spend_Copy", "Digital_Clicks",
          "Print_Spend", "Search_Spend", "TV_Spend", "Social_Spend"]
# Digital_Clicks: months whose totals match (N-09). Sep = Oct (adjacent), Jun = Dec (Jun 3 is the missing week).
MONTH_PLANTS = {(2024, 9): [101.5, 102.5, 103.5, 104.5, 105.5], (2024, 10): [120.0, 125.0, 130.0, 142.5],
                (2024, 6): [200.25, 210.25, 220.5], (2024, 12): [100.0, 110.0, 120.5, 140.25, 160.25]}
ZERO_WEEKS = [*range(35, 46), *range(16, 22), *range(23, 29), *range(50, 58)]   # TV_Impressions: 11 / 6+6 / 8 zeros

# What Phase C must report on this workbook, and nothing else (see summarize()).
EXPECTED = {
    ("N-01", "Digital_Clicks", 3), ("N-02", "Digital_Clicks", 1), ("N-02", "Radio_Spend_Copy", 1),
    ("N-03", "Digital_Clicks", 1), ("N-04", "Digital_Clicks", 1), ("N-05", "Digital_Clicks", 3),
    ("N-07", "TV_Impressions", "2024-09-02..2024-11-11"),            # 11 zero weeks; 6+6 split by a gap, 8: not
    ("N-08", "TV_Spend", "2024-04-01 2024-03-25 Sequential"),
    ("N-08", "TV_Spend", "2024-12-16 2024-10-07 Not Sequential"),    # Social_Spend's pair is before the WOW range
    ("N-09", "Digital_Clicks", "Oct 2024 Sep 2024 Sequential"),
    ("N-09", "Digital_Clicks", "Dec 2024 Jun 2024 Not Sequential"),
    ("N-10", "Radio_Spend|Radio_Spend_Copy", ""),                    # the copy is blank where Radio_Spend is 0
    ("N-11", "2024-03-04", None), ("N-12", "2024-06-03", "2024-05-27"),
    ("N-13", "2024-07-29", "2024-08-05"), ("N-14", "2024-06-10", "2024-05-27"),
    ("N-15", "TV_Spend", 2),
}


def week(k: int) -> date:
    return START + timedelta(weeks=k)


def _row(k: int) -> list:
    d = week(k)
    tv = {12: 777.5, 13: 777.5, 40: 888.25, 50: 888.25}.get(k, round(500 + 3.17 * k, 2))
    imp = 0 if k in ZERO_WEEKS else round(10000 + 101.3 * k, 1)
    radio = 0 if k == 5 else round(300 + 2.71 * k, 2)
    copy = None if k == 5 else radio
    clicks = {2: "abc", 3: None, 4: "NULL", 6: " 1200 ", 7: "1,250"}.get(k, round(1000 + 13.37 * k, 2))
    month = MONTH_PLANTS.get((d.year, d.month))
    if month:
        clicks = month[[w for w in range(WEEKS) if w != 22 and (week(w).year, week(w).month) == (d.year, d.month)].index(k)]
    printed = 50 if k in (1, 2) else round(40 + 1.11 * k, 2)          # ignored: Bucket #N/A
    social = 444.0 if k in (0, 1) else round(700 + 1.93 * k, 2)
    return [d, "National", tv, imp, radio, copy, clicks, printed, round(900 + 4.4 * k, 2), round(20 + k, 1), social]


def rows() -> list[list]:
    """Weeks 0-59 without week 22 (2024-06-03); week 9 twice (2024-03-04); weeks 30 and 31 swapped."""
    out = []
    for k in [w for w in range(WEEKS) if w != 22]:
        out.append(_row(k))
        if k == 9:
            out.append([week(9), "National", 999.99, 55555, 333.33, 333.33, 5555.55, 12, 13, 14, 15.5])
    i = next(n for n, r in enumerate(out) if r[0] == week(30))
    out[i], out[i + 1] = out[i + 1], out[i]
    return out


def build_data(path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Weekly"
    ws.append(HEADER)
    for r in rows():
        ws.append(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def build_config(path: Path, specs_header: str = "Description") -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data File"
    for r in [["Description", "Column"], ["Period", "A"], ["Geography", "B"], ["Variables", "C"]]:
        ws.append(r)
    ws = wb.create_sheet("Variable Mapping")
    ws.append(["Variables", "Description", "Bucket", "Spend Variables", "Y/N"])
    for r in [["TV_Spend", "TV spend", "Spend"], ["TV_Impressions", "TV impressions", "Support"],
              ["Radio_Spend", "Radio spend", "Spend"], ["Radio_Spend_Copy", "Radio spend (copy)", "Spend"],
              ["Digital_Clicks", "Digital clicks", "Support"], ["Print_Spend", "Print spend", "#N/A"],
              ["Social_Spend", "Social spend", "Spend"], ["Missing_Var", "Not in the data", "Spend"]]:
        ws.append(r)
    ws = wb.create_sheet("Period Mapping")
    ws.append(["Period", "Months", "Quarters", "Half Years", "Full Years"])
    for k in range(WEEKS + 10):
        d = week(k)
        ws.append([d, d.strftime("%b %Y"), f"Q{(d.month - 1) // 3 + 1} {d.year}", f"H{(d.month - 1) // 6 + 1} {d.year}",
                   str(d.year)])
    ws = wb.create_sheet("Data Check Specifications")
    ws.append([specs_header, "Threshold", "From", "To", "No of Weeks"])
    for r in [["No Executions", None, None, None, 10], ["Week on Week Duplicate", None, date(2024, 2, 1), None, None],
              ["Month on Month Duplicate", None, None, None, None], ["MOM % change", 30, None, None, None],
              ["Abnormal trend - Robust Z-score", 3.5, None, None, None]]:
        ws.append(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def summarize(findings: list[dict]) -> set[tuple]:
    """findings.json (open findings) -> the comparable form of EXPECTED."""
    out = set()
    for f in (f for f in findings if f["status"] == "open"):
        c, d = f["code"], f["detail"]
        if c in ("N-01", "N-02", "N-03", "N-04", "N-05", "N-15"):
            out.add((c, d["Variables"], d["Count"]))
        elif c == "N-07":
            out.add((c, d["Variables"], f"{d['Start Period']}..{d['End Period']}"))
        elif c == "N-08":
            out.add((c, d["Variables"], f"{d['Period']} {d['Compared Period']} {d['Comparison Type']}"))
        elif c == "N-09":
            out.add((c, d["Variables"], f"{d['Month']} {d['Compared Month']} {d['Comparison Type']}"))
        elif c == "N-10":
            out.add((c, f"{d['Variable 1']}|{d['Variable 2']}", ""))
        else:
            out.add((c, d["Period"], d["Related Period"]))
    return out


# (ID, check, what is planted in samples/phase_c_weekly.xlsx, what Phase C must do)
CATALOGUE = [
    ("N-01", "Number / Text", "Digital_Clicks: 'abc', 'NULL', '1,250' (weeks 3, 5, 8)", "3 invalid numeric values flagged"),
    ("N-02", "Missing / Blank", "Digital_Clicks week 4 blank; Radio_Spend_Copy week 6 blank", "1 blank on each variable"),
    ("N-03", "NULL / N/A", "Digital_Clicks week 5 = 'NULL'", "1 NULL/N/A value flagged"),
    ("N-04", "Spaces", "Digital_Clicks week 8 = ' 1200 '", "1 value with spaces flagged"),
    ("N-05", "Special Characters", "Digital_Clicks: 'abc', 'NULL', '1,250'", "3 values with special characters"),
    ("N-06", "Execution Weeks", "TV_Impressions has 31 zero weeks; Social_Spend is clean", "Counts: TV_Impressions 29, Social_Spend 60, "
                                                                                      "Digital_Clicks 56 (zero/blank/text not counted)"),
    ("N-07", "No Executions", "TV_Impressions: 11 zero weeks in a row; 6+6 zeros split by a week; 8 zeros (below 10)",
     "Only the 11-week run, 2024-09-02..2024-11-11"),
    ("N-08", "WOW Duplicate", "TV_Spend 777.5 in consecutive weeks; 888.25 ten weeks apart; Social_Spend pair before the From date",
     "Sequential + Not Sequential pair for TV_Spend; none for Social_Spend"),
    ("N-09", "MOM Duplicate", "Digital_Clicks: Sep = Oct 2024 totals; Jun = Dec 2024 totals", "One Sequential, one Not Sequential pair"),
    ("N-10", "Duplicate Variable Data", "Radio_Spend_Copy = Radio_Spend (zero week blank in the copy)", "Pair reported (0 and blank equal)"),
    ("N-11", "Duplicate Dates", "2024-03-04 appears twice", "1 duplicate date"),
    ("N-12", "Missing Dates", "Week of 2024-06-03 removed", "1 missing date"),
    ("N-13", "Date Order", "Weeks of 2024-07-29 and 2024-08-05 swapped", "1 out-of-order row"),
    ("N-14", "Weekly Continuity", "14-day jump after 2024-05-27", "1 irregular gap"),
    ("N-15", "Duplicate Variable Names", "TV_Spend header appears twice", "1 duplicate name, 2 columns"),
    ("CFG", "Variable selection", "Print_Spend Bucket #N/A; Search_Spend not mapped; Missing_Var mapped but absent",
     "Ignored / not-in-data statuses on Execution_Weeks"),
]


def build_catalogue(path: Path, run_out: Path) -> list[dict]:
    """Run the check of every CATALOGUE row against the Phase C output and write samples/phase_c_issue_catalogue.xlsx."""
    import json
    findings = json.loads((run_out / "findings.json").read_text("utf8"))
    got = summarize(findings)
    ws = openpyxl.load_workbook(run_out / "Data_Validation_Report.xlsx", data_only=True)["Execution_Weeks"]
    weeks = {r[2]: (r[3], r[4]) for r in ws.iter_rows(min_row=2, values_only=True)}
    rows = []
    for code, check, planted, expect in CATALOGUE:
        if code == "N-06":
            actual = {v: weeks[v][1] for v in ("TV_Impressions", "Social_Spend", "Digital_Clicks")}
            ok = actual == {"TV_Impressions": 29, "Social_Spend": 60, "Digital_Clicks": 56}
        elif code == "CFG":
            actual = {v: weeks[v][0] for v in ("Print_Spend", "Search_Spend", "Missing_Var")}
            ok = (actual["Print_Spend"].startswith("Ignored: Bucket") and actual["Search_Spend"].startswith("Ignored: not in")
                  and actual["Missing_Var"].startswith("In Variable Mapping"))
        else:
            want = {e for e in EXPECTED if e[0] == code}
            actual = sorted((e for e in got if e[0] == code), key=str)
            ok = {e for e in got if e[0] == code} == want
        rows.append({"ID": code, "Check": check, "Planted": planted, "Expected": expect,
                     "Actual": str(actual), "Status": "PASS" if ok else "FAIL"})
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Issue_Catalogue"
    cols = ["ID", "Check", "Planted", "Expected", "Actual", "Status"]
    ws.append(cols)
    for r in rows:
        ws.append([r[c] for c in cols])
    for col, w in zip("ABCDEF", (8, 26, 70, 55, 70, 10)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws2 = wb.create_sheet("How_To_Run")
    for line in ["Rebuild the data, run Phase C, fill Actual / Status:",
                 "    python tests/fixtures/build_phase_c_workbook.py",
                 "Inputs: samples/phase_c_weekly.xlsx + samples/config/phase_c_client_config.xlsx",
                 "Report: out_phase_c/phase_c_timeseries/Data_Validation_Report.xlsx",
                 "Pytest: python -m pytest tests/test_timeseries.py -q"]:
        ws2.append([line])
    wb.save(path)
    return rows


def main() -> int:
    sys.path.insert(0, str(ROOT))
    import json
    from src.timeseries.pipeline import OUT, run_timeseries
    data = build_data(ROOT / "samples" / "phase_c_weekly.xlsx")
    config = build_config(ROOT / "samples" / "config" / "phase_c_client_config.xlsx")
    run_timeseries(str(data), str(ROOT / "out_phase_c"), str(config), str(ROOT / "config" / "settings.yaml"))
    got = summarize(json.loads((ROOT / "out_phase_c" / OUT / "findings.json").read_text("utf8")))
    for item in sorted(EXPECTED | got, key=str):
        print("PASS " if item in EXPECTED and item in got else "MISSING " if item in EXPECTED else "EXTRA ", item)
    rows = build_catalogue(ROOT / "samples" / "phase_c_issue_catalogue.xlsx", ROOT / "out_phase_c" / OUT)
    print("\nIssue catalogue -> samples/phase_c_issue_catalogue.xlsx")
    for r in rows:
        print(f"  {r['Status']}  {r['ID']:5} {r['Check']}")
    return 0 if got == EXPECTED and all(r["Status"] == "PASS" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
