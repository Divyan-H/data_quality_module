"""Phase D issue workbook: 157 weekly rows (2023-01-02 .. 2025-12-29) with a planted case for every rule (N-16 to
N-20), the client config workbook that goes with it, and EXPECTED - exactly the findings Phase D must report.

    python tests/fixtures/build_phase_d_workbook.py

writes samples/phase_d_weekly.xlsx and samples/config/phase_d_client_config.xlsx, runs Phase D into out_phase_d/ and
writes samples/phase_d_issue_catalogue.xlsx. The header is on row 1, column A and the Period Mapping labels are the ones
Spend_Support_Trend_Report.py derives itself ("March 2024", "2024Q1", "2024 H1", "2024"), so the reference script can
read the same two files.

The base series cannot trip a rule: every variable wobbles +-10% in a 7-week cycle (MAD 6.7%: |z| stays near 1), and
month totals move at most ~32% between 4- and 5-week months, under the 40% MOM threshold. Each plant is sized to cross
only the thresholds listed for it in PLANTS.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[2]
START, WEEKS = date(2023, 1, 2), 157                 # Mondays 2023-01-02 .. 2025-12-29
EXTRA_CONFIGURED = 4                                 # Jan 2026 in Period Mapping, no data: zero on the Trend Line
OFF_AXIS = date(2025, 12, 31)                        # a data row on a date Period Mapping does not have

# Variable Mapping rows: Variables, Spend Variables, Y/N, Description, Bucket, Aggregation 1
MAPPING = [
    ["TV_Impressions", "TV_Spend", "Y", "TV", "Offline", "Video"],
    ["Radio_GRPs", "Radio_Spend", "Y", "Radio", "Offline", "Audio"],
    ["Search_Clicks", "Search_Spend", "Y", "Paid Search", "Digital", "Performance"],
    ["Display_Impressions", "Display_Spend", "Y", "Display", "Digital", "Performance"],
    ["Social_Impressions", "Social_Spend", "Y", "Social", "Digital", "Performance"],
    ["Video_Views", "Video_Spend", "Y", "Online Video", "Digital", "Performance"],
    ["Cinema_Admits", None, "Y", "Cinema", "Offline", "Video"],             # Support only
    ["OOH_Faces", "OOH_Spend", "Y", "Out of Home", "Offline", "OOH"],       # OOH_Spend is not in the data
    ["Podcast_Downloads", "Podcast_Spend", "Y", "Podcast", "Offline", "Audio"],   # neither is in the data
    ["Print_Insertions", "Print_Spend", "N", "Print", "Offline", "Print"],  # N: not checked
]
# Weekly base value and wobble phase of each data column. A pair shares its phase, so TV CPM is constant.
BASE = {"TV_Impressions": (100000, 0), "TV_Spend": (5000, 0), "Radio_GRPs": (200, 1), "Radio_Spend": (600, 1),
        "Search_Clicks": (4000, 2), "Search_Spend": (700, 2), "Display_Impressions": (50000, None),
        "Display_Spend": (500, 3), "Social_Impressions": (80000, 4), "Social_Spend": (450, 4),
        "Video_Views": (30000, 5), "Video_Spend": (350, 5), "Cinema_Admits": (900, 6), "OOH_Faces": (120, 0),
        "Print_Insertions": (12, 1), "Print_Spend": (300, 1)}
HEADER = ["Period", "Geography", *BASE]
THRESHOLDS = [["MOM % change", 40], ["QOQ % change", 35], ["HOH % change", 25], ["YOY % change", 20],
              ["Abnormal trend - Robust Z-score", 3.5], ["Spend concentration - Top tactics count", 2],
              ["Spend concentration %", 70], ["CPM % change", 15]]            # the reference's CPM row: NOT USED
RATIOS = [["TV CPM", "TV_Spend", "TV_Impressions", 20], ["Search CPC", "Search_Spend", "Search_Clicks", None]]

# (code, what is planted, what Phase D must report)
PLANTS = [
    ("N-16 N-17 N-20", "Search_Spend +4000 on 2024-03-11 (one week, ~6.7x)",
     "N-16 Spend outlier; N-17 MOM March 2024 (+143%), April 2024 (-49%), March 2025 (-49% vs LY), QOQ 2024Q1 (+44%); "
     "N-20 MOM March 2024 Digital / Performance (top 2 = 73% > 70%). QOQ 2024Q2 -31% < 35%, H1 +22% < 25%: no flag"),
    ("N-16 N-17", "Radio_GRPs x3.5 on 2024-06-10 (one week)",
     "N-16 Support outlier; N-17 MOM June 2024 (+68%); July 2024 (5 weeks) -23%: no flag"),
    ("N-16", "Display_Impressions constant 50000, 50500 on 2024-08-12",
     "N-16 Support outlier by the MAD = 0 fallback"),
    ("N-18", "February 2024: TV_Spend x1.15, TV_Impressions x0.9 (TV CPM x1.28)",
     "N-18 TV CPM MOM February 2024 (+27.8%), March 2024 (-21.7%), February 2025 (-21.7% vs LY); "
     "N-17 not flagged (TV_Impressions -28% < 40%)"),
    ("N-19", "Social_Impressions 0 on 2024-01-15; Social_Spend blank on 2024-07-15",
     "N-19 Spend without Support, Support without Spend (both in 5-week months: no N-17)"),
    ("CFG", "Search CPC ratio without Threshold; 'CPM % change' row; OOH_Spend, Podcast_* not in the data; "
            "Print N; a row on 2025-12-31 (not in Period Mapping); Jan 2026 configured without data",
     "Rules_Used: Search CPC INACTIVE, CPM NOT USED (sheet shown); Unmapped_or_Missing: 2 pairs + 1 date; "
     "Jan 2026 is 0 on the Trend Line and NOT ASSESSED - ZERO TOTAL SPEND on Spend_Concentration"),
]

# What Phase D must report on this workbook, and nothing else (see summarize()).
EXPECTED = {
    ("N-16", "Search_Clicks", "2024-03-11", "Spend"),
    ("N-16", "Radio_GRPs", "2024-06-10", "Support"),
    ("N-16", "Display_Impressions", "2024-08-12", "Support (MAD=0 fallback)"),
    ("N-17", "Search_Clicks", "MOM", "March 2024"), ("N-17", "Search_Clicks", "MOM", "April 2024"),
    ("N-17", "Search_Clicks", "MOM", "March 2025"), ("N-17", "Search_Clicks", "QOQ", "2024Q1"),
    ("N-17", "Radio_GRPs", "MOM", "June 2024"),
    ("N-18", "TV CPM", "MOM", "February 2024"), ("N-18", "TV CPM", "MOM", "March 2024"),
    ("N-18", "TV CPM", "MOM", "February 2025"),
    ("N-19", "Social_Impressions", "2024-01-15", "Spend available but Support unavailable"),
    ("N-19", "Social_Impressions", "2024-07-15", "Support available but Spend unavailable"),
    ("N-20", "Digital / Performance", "MOM", "March 2024"),
}


def week(k: int) -> date:
    return START + timedelta(weeks=k)


def wobble(k: int, phase: int | None) -> float:
    """+-10% in seven steps, repeating every 7 weeks: any 4 or 5 weeks in a row average within 3.3%."""
    return 1.0 if phase is None else 1 + 0.1 * (((3 * k + phase) % 7) - 3) / 3


def value(col: str, k: int):
    base, phase = BASE[col]
    v = base * wobble(k, phase)
    d = week(k)
    if col == "Search_Spend" and d == date(2024, 3, 11):
        v += 4000
    if col == "Radio_GRPs" and d == date(2024, 6, 10):
        v *= 3.5
    if col == "Display_Impressions" and d == date(2024, 8, 12):
        v = 50500
    if (d.year, d.month) == (2024, 2):
        v *= {"TV_Spend": 1.15, "TV_Impressions": 0.9}.get(col, 1)
    if col == "Social_Impressions" and d == date(2024, 1, 15):
        v = 0
    if col == "Social_Spend" and d == date(2024, 7, 15):
        v = None
    return round(v, 2) if v is not None else None


def rows() -> list[list]:
    out = [[week(k), "National", *(value(c, k) for c in BASE)] for k in range(WEEKS)]
    return out + [[OFF_AXIS, "National", *(value(c, WEEKS - 1) for c in BASE)]]


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


def period_rows(weeks: int = WEEKS + EXTRA_CONFIGURED) -> list[list]:
    out = []
    for k in range(weeks):
        d = week(k)
        out.append([d, d.strftime("%B %Y"), f"{d.year}Q{(d.month - 1) // 3 + 1}", f"{d.year} H{(d.month - 1) // 6 + 1}",
                    str(d.year)])
    return out


def build_config(path: Path, thresholds=THRESHOLDS, ratios=RATIOS) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data File"
    for r in [["Description", "Column"], ["Period", "A"], ["Geography", "B"], ["Variables", "C"]]:
        ws.append(r)
    ws = wb.create_sheet("Variable Mapping")
    ws.append(["Variables", "Spend Variables", "Y/N", "Description", "Bucket", "Aggregation 1"])
    for r in MAPPING:
        ws.append(r)
    ws = wb.create_sheet("Period Mapping")
    ws.append(["Period", "Months", "Quarters", "Half Years", "Full Years"])
    for r in period_rows():
        ws.append(r)
    ws = wb.create_sheet("Data Check Specifications")
    ws.append(["Rule", "Threshold", "From", "To", "No of Weeks"])
    ws.append(["No Executions", None, None, None, 9])                       # Phase C's row: Phase D ignores it
    for r in thresholds:
        ws.append(r)
    if ratios is not None:
        ws = wb.create_sheet("Ratio Definitions")
        ws.append(["Ratio Name", "Numerator", "Denominator", "Threshold"])
        for r in ratios:
            ws.append(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def summarize(findings: list[dict]) -> set[tuple]:
    """findings.json (open findings) -> the comparable form of EXPECTED."""
    out = set()
    for f in (f for f in findings if f["status"] == "open"):
        c, d = f["code"], f["detail"]
        if c == "N-16":
            side = "Support" if d["Support Status"] == "OUTLIER" else "Spend"
            out.add((c, d["Support Variable"], d["Date"], side + (" (MAD=0 fallback)" if "MAD=0" in d["Outlier Reason"]
                                                                  else "")))
        elif c == "N-17":
            out.add((c, d["Support Variable"], f["sheet"][:3], d["Period"]))
        elif c == "N-18":
            out.add((c, d["Ratio Name"], d["Period Type"], d["Period"]))
        elif c == "N-19":
            out.add((c, d["Support Variable"], d["Date"], d["Reason"]))
        else:
            out.add((c, f"{d['Bucket']} / {d['Aggregation 1']}", d["Period Type"], d["Period"]))
    return out


def build_catalogue(path: Path, run_out: Path) -> list[dict]:
    """Check every PLANTS row against the Phase D output and write samples/phase_d_issue_catalogue.xlsx."""
    import json
    findings = json.loads((run_out / "findings.json").read_text("utf8"))
    got = summarize(findings)
    wb = openpyxl.load_workbook(run_out / "Spend_Support_Trend_Report.xlsx")
    rules = {r[1]: r[4] for r in wb["Rules_Used"].iter_rows(min_row=2, values_only=True)}
    unmapped = [r[2] for r in wb["Unmapped_or_Missing"].iter_rows(min_row=2, values_only=True)]
    conc = [r for r in wb["Spend_Concentration"].iter_rows(min_row=2, values_only=True)]
    trend = [r for r in wb["Trend Line"].iter_rows(min_row=2, values_only=True)]
    planted = {"N-16 N-17 N-20": "Search", "N-16 N-17": "Radio", "N-16": "Display", "N-18": "TV CPM", "N-19": "Social"}
    rows = []
    for code, plant, expect in PLANTS:
        if code == "CFG":
            actual = {"Search CPC": rules.get("Ratio: Search CPC (Search_Spend / Search_Clicks)"),
                      "CPM row": rules.get("CPM % change"), "Rules_Used shown": wb["Rules_Used"].sheet_state,
                      "unmapped": len(unmapped),
                      "2026 zero": sum(1 for r in trend if str(r[0])[:4] == "2026" and r[5] == 0),
                      "2026 not assessed": sum(1 for r in conc if str(r[1]).endswith("2026") and r[-1].startswith(
                          "NOT ASSESSED - ZERO"))}
            ok = (actual["Search CPC"] == "INACTIVE" and actual["CPM row"] == "NOT USED"
                  and actual["Rules_Used shown"] == "visible" and actual["unmapped"] == 3
                  and actual["2026 zero"] == EXTRA_CONFIGURED * 8 and actual["2026 not assessed"] > 0)
        else:
            want = {e for e in EXPECTED if planted[code] in e[1]}
            mine = {e for e in got if planted[code] in e[1]}
            actual, ok = sorted(mine, key=str), mine == want
        rows.append({"ID": code, "Planted": plant, "Expected": expect, "Actual": str(actual),
                     "Status": "PASS" if ok else "FAIL"})
    out = openpyxl.Workbook()
    ws = out.active
    ws.title = "Issue_Catalogue"
    cols = ["ID", "Planted", "Expected", "Actual", "Status"]
    ws.append(cols)
    for r in rows:
        ws.append([r[c] for c in cols])
    for col, w in zip("ABCDE", (14, 60, 80, 90, 10)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws2 = out.create_sheet("How_To_Run")
    for line in ["Rebuild the data, run Phase D, fill Actual / Status:",
                 "    python tests/fixtures/build_phase_d_workbook.py",
                 "Inputs: samples/phase_d_weekly.xlsx + samples/config/phase_d_client_config.xlsx",
                 "Report: out_phase_d/phase_d_trends/Spend_Support_Trend_Report.xlsx",
                 "Pytest: python -m pytest tests/test_trends.py -q"]:
        ws2.append([line])
    out.save(path)
    return rows


def main() -> int:
    sys.path.insert(0, str(ROOT))
    import json
    from src.trends.pipeline import OUT, run_trends
    data = build_data(ROOT / "samples" / "phase_d_weekly.xlsx")
    config = build_config(ROOT / "samples" / "config" / "phase_d_client_config.xlsx")
    run_trends(str(data), str(ROOT / "out_phase_d"), str(config), str(ROOT / "config" / "settings.yaml"))
    got = summarize(json.loads((ROOT / "out_phase_d" / OUT / "findings.json").read_text("utf8")))
    for item in sorted(EXPECTED | got, key=str):
        print("PASS " if item in EXPECTED and item in got else "MISSING " if item in EXPECTED else "EXTRA ", item)
    rows = build_catalogue(ROOT / "samples" / "phase_d_issue_catalogue.xlsx", ROOT / "out_phase_d" / OUT)
    print("\nIssue catalogue -> samples/phase_d_issue_catalogue.xlsx")
    for r in rows:
        print(f"  {r['Status']}  {r['ID']:15} {r['Planted'][:70]}")
    return 0 if got == EXPECTED and all(r["Status"] == "PASS" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
