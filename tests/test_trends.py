"""Phase D (src/trends) tests. Workbooks are built in tmp_path; nothing is committed."""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import openpyxl
import polars as pl
import pytest

from src.trends.cli import main as cli
from src.trends.config import load_settings, rule_key
from src.common import units
from src.common.errors import ConfigError
from src.trends.pipeline import OUT
from src.trends.report import SHEETS, STATUSES
from src.trends.review import ReviewError, apply_upload

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import build_phase_d_workbook as wbk  # noqa: E402

ALL = [["MOM % change", 40], ["QOQ % change", 35], ["HOH % change", 25], ["YOY % change", 20],
       ["Abnormal trend - Robust Z-score", 3.5], ["Spend concentration - Top tactics count", 2],
       ["Spend concentration %", 70]]
PAIR = [["Imp", "Spend", "Y", "TV", "Offline", "Video"]]




def run_c(input_path, run_dir, *args, **kw):
    """Run the phase and return the run.json of the one table it checked (the index lists tables, not layouts)."""
    import src.timeseries.pipeline as pipeline
    pipeline.run_timeseries(input_path, run_dir, *args, **kw)
    (folder,) = [f for _, f in units.checked(Path(run_dir) / "phase_c_timeseries")]
    return json.loads((folder / "run.json").read_text("utf8"))


def run_d(input_path, run_dir, *args, **kw):
    """Run the phase and return the run.json of the one table it checked (the index lists tables, not layouts)."""
    import src.trends.pipeline as pipeline
    pipeline.run_trends(input_path, run_dir, *args, **kw)
    (folder,) = [f for _, f in units.checked(Path(run_dir) / "phase_d_trends")]
    return json.loads((folder / "run.json").read_text("utf8"))

def unit(run_dir: Path) -> Path:
    """The folder of the one table a single-table run checked (src/common/units.py)."""
    (folder,) = [f for _, f in units.checked(Path(run_dir) / OUT)]
    return folder

def write_xlsx(path: Path, sheets: dict) -> Path:
    """{sheet: [header, *rows]} -> xlsx."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


def labels(d: date) -> list:
    return [d, d.strftime("%B %Y"), f"{d.year}Q{(d.month - 1) // 3 + 1}", f"{d.year} H{(d.month - 1) // 6 + 1}",
            str(d.year)]


def months(first: date, n: int) -> list[date]:
    return [date(first.year + (first.month - 1 + k) // 12, (first.month - 1 + k) % 12 + 1, 1) for k in range(n)]


def client_config(path: Path, pairs=PAIR, thresholds=ALL, ratios=None, dates=(), period_rows=None, specs=True,
                  data_file=None, period_header=("Period", "Months", "Quarters", "Half Years", "Full Years")) -> Path:
    sheets = {"Data File": [["Description", "Column"], *(data_file or [["Period", "A"], ["Variables", "B"]])],
              "Variable Mapping": [["Variables", "Spend Variables", "Y/N", "Description", "Bucket", "Aggregation 1"],
                                   *pairs],
              "Period Mapping": [list(period_header), *(period_rows or [labels(d) for d in dates])]}
    if specs:
        sheets["Data Check Specifications"] = [["Rule", "Threshold"], *thresholds]
    if ratios is not None:
        sheets["Ratio Definitions"] = [["Ratio Name", "Numerator", "Denominator", "Threshold"], *ratios]
    return write_xlsx(path, sheets)


def run(tmp_path: Path, header, rows, **kw):
    """Phase D on one data sheet -> (run.json, open findings by code, output folder). The Period Mapping holds the
    data's dates unless `dates` / `period_rows` say otherwise."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = write_xlsx(tmp_path / "data.xlsx", {"Weekly": [header, *rows]})
    if "dates" not in kw:
        kw["dates"] = sorted({r[0] for r in rows})
    cfg = client_config(tmp_path / "config.xlsx", **kw)
    res = run_d(str(data), str(tmp_path / "run"), str(cfg))
    return res, by_code(tmp_path / "run"), unit(tmp_path / "run")


def by_code(run_dir: Path, status: str = "open") -> dict:
    out = {}
    for f in json.loads((unit(run_dir) / "findings.json").read_text("utf8")):
        if f["status"] == status:
            out.setdefault(f["code"], []).append(f)
    return out


def sheet_rows(out: Path, name: str) -> list[dict]:
    ws = openpyxl.load_workbook(out / "Spend_Support_Trend_Report.xlsx")[name]
    rows = list(ws.iter_rows(values_only=True))
    return [dict(zip(rows[0], r)) for r in rows[1:]]


def monthly(values: list, first=date(2024, 1, 1), spend=None) -> list[list]:
    """Monthly rows (Period, Imp[, Spend])."""
    return [[d, v] + ([spend[k]] if spend else []) for k, (d, v) in enumerate(zip(months(first, len(values)), values))]


# ── the planted workbook: every rule, the reference layout ──────────────────
def _planted(tmp_path, **kw):
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    cfg = wbk.build_config(tmp_path / "config.xlsx")
    return run_d(str(data), str(tmp_path / "run"), str(cfg), **kw), unit(tmp_path / "run")


def test_planted_workbook_reports_exactly_the_planted_issues(tmp_path):
    res, out = _planted(tmp_path)
    got = wbk.summarize(json.loads((out / "findings.json").read_text("utf8")))
    assert got == wbk.EXPECTED, f"missing {wbk.EXPECTED - got}, extra {got - wbk.EXPECTED}"
    assert res["pairs"] == {"mapped": 9, "run": 8, "with_spend": 6}
    assert res["axis"]["rows_outside_axis"] == 1 and res["axis"]["dates_with_data"] == 157
    assert res["inactive_rules"] == ["N-18 Ratio: Search CPC (Search_Spend / Search_Clicks): no threshold configured: "
                                     "this ratio is not checked"]
    rules = {r["Rule"]: (r["Threshold"], r["Status"]) for r in res["rules"]}
    assert rules["MOM % Change"] == (40, "ACTIVE") and rules["CPM % change"] == ("15", "NOT USED")
    assert rules["Spend/Support Availability"][1] == "ALWAYS ON"
    unmapped = [r["Reason"] for r in sheet_rows(out, "Unmapped_or_Missing")]
    assert unmapped[:2] == ["Spend variable not found; Support changes were still calculated.",
                            "Support variable not found in input file: Podcast_Downloads"]
    assert unmapped[2].startswith("1 row(s) on 1 Period date(s) not in Period Mapping") and "2025-12-31" in unmapped[2]


def test_report_layout(tmp_path):
    _, out = _planted(tmp_path)
    wb = openpyxl.load_workbook(out / "Spend_Support_Trend_Report.xlsx")
    assert wb.sheetnames == SHEETS
    assert {n: wb[n].sheet_state for n in SHEETS if wb[n].sheet_state != "visible"} == {"Spend_Concentration": "hidden"}
    mom = wb["MOM_Changes"]
    head = [c.value for c in mom[1]]
    assert head[3] is None and head[-2:] == ["Finding_ID", "Review"] and "CPM Change %" not in head
    assert mom.column_dimensions["B"].hidden and mom.column_dimensions["C"].hidden
    row = next(r for r in mom.iter_rows(min_row=2) if r[head.index("Support Variable")].value == "Search_Clicks"
               and r[head.index("Period")].value == "March 2024")
    pct = row[head.index("Sequential Period Spend Change %")]
    assert pct.number_format == "0.0%" and 1 < pct.value < 2                     # +150% written as 1.5
    assert row[head.index("Change Status")].fill.fgColor.rgb.endswith("FFC7CE")
    assert row[head.index("Finding_ID")].value.startswith("N-17-")
    passing = next(r for r in mom.iter_rows(min_row=2) if r[head.index("Change Status")].value == "PASS")
    assert passing[head.index("Finding_ID")].value is None
    assert mom.data_validations.dataValidation[0].formula1 == '"' + ",".join(STATUSES) + '"'
    trend = wb["Trend Line"]
    first = trend.cell(2, 4).value
    hidden = {trend.cell(r, 4).value for r in range(2, trend.max_row + 1) if trend.row_dimensions[r].hidden}
    assert first == "TV_Impressions" and first not in hidden and len(hidden) == 7
    assert [f.filters.filter for f in trend.auto_filter.filterColumn] == [["TV_Impressions"]]
    assert len(trend._charts) == 1
    flagged = {c.comment.text for row in wb["Data"].iter_rows() for c in row if c.comment}
    assert flagged == {"N-16 Abnormal Trend", "N-19 Spend/Support Availability"}


# ── one rule at a time ───────────────────────────────────────────────────────
def test_abnormal_trend_robust_z_needs_five_values_and_ignores_zeros(tmp_path):
    values = [100, 102, 98, 101, 99, 0, 0, 500]                     # 6 non-zero values: 500 is an outlier
    rows = [[date(2024, 1, 1) + timedelta(weeks=k), v, 50.0 + k] for k, v in enumerate(values)]
    _, found, _ = run(tmp_path / "a", ["Period", "Imp", "Spend"], rows)
    (f,) = found["N-16"]
    assert f["detail"]["Support Value"] == 500 and f["detail"]["Spend Status"] == "PASS"
    assert f["detail"]["Support Stream Median"] == 100.5                        # zeros are not in the median
    short = [[d, v if k > 3 else 0, s] for k, (d, v, s) in enumerate(rows)]    # 4 non-zero values: no statistics
    _, found, _ = run(tmp_path / "b", ["Period", "Imp", "Spend"], short)
    assert "N-16" not in found


def test_a_spend_shared_by_several_pairs_is_flagged_once(tmp_path):
    spend = [50.0 + k for k in range(7)] + [5000.0]                              # the last week's Spend spikes
    rows = [[date(2024, 1, 1) + timedelta(weeks=k), 100.0 + k, 200.0 + 2 * k, s] for k, s in enumerate(spend)]
    pairs = [["Imp", "Spend", "Y", "TV", "B", "A"], ["Clicks", "Spend", "Y", "TV", "B", "A"]]
    _, found, _ = run(tmp_path, ["Period", "Imp", "Clicks", "Spend"], rows, pairs=pairs,
                      data_file=[["Period", "A"], ["Variables", "B"]])
    (f,) = found["N-16"]                                                        # not one per pair
    assert f["detail"]["Spend Value"] == 5000 and f["detail"]["Support Status"] == "PASS"


def test_period_changes_compare_the_same_period_last_year_only_with_history(tmp_path):
    values = [100] * 18
    values[14] = 300                                                            # March 2025
    _, found, out = run(tmp_path, ["Period", "Imp"], monthly(values), pairs=[["Imp", None, "Y", "TV", "B", "A"]])
    mom = {r["Period"]: r for r in sheet_rows(out, "MOM_Changes")}
    assert all(r["Last Year Same Period"] is None for p, r in mom.items() if p.endswith("2024"))
    assert mom["March 2025"]["Last Year Same Period"] == "March 2024"
    assert mom["March 2025"]["Spend Variable"] is None and mom["March 2025"]["Current Spend"] is None
    assert [(r["Period"], r["Sequential Period"], r["Last Year Same Period"]) for r in sheet_rows(out, "HOH_Changes")] \
        == [("2024 H2", "2024 H1", None), ("2025 H1", "2024 H2", "2024 H1")]
    assert [(r["Period"], r["Last Year Same Period"]) for r in sheet_rows(out, "YOY_Changes")] == [("2025", "2024")]
    assert sorted(f["detail"]["Period"] for f in found["N-17"] if f["sheet"] == "MOM_Changes") == \
        ["April 2025", "March 2025"]
    assert mom["April 2025"]["Sequential Period Support Change %"] == pytest.approx(-2 / 3)   # -66.7%


def test_change_threshold_is_met_at_the_threshold(tmp_path):
    _, found, _ = run(tmp_path, ["Period", "Imp"], monthly([100, 140, 140]), thresholds=[["MOM % change", 40]],
                      pairs=[["Imp", None, "Y", "TV", "B", "A"]])
    assert [f["detail"]["Period"] for f in found["N-17"]] == ["February 2024"]


def test_fiscal_period_labels_group_and_find_last_year(tmp_path):
    def fiscal(d):
        fy, p = d.year + (d.month >= 4), (d.month - 4) % 12 + 1
        return [d, f"FY{fy % 100} P{p:02d}", f"FY{fy % 100} Q{(p - 1) // 3 + 1}", f"FY{fy % 100} H{(p - 1) // 6 + 1}",
                f"FY{fy % 100}"]
    dates = months(date(2023, 4, 1), 24)                                        # FY24 and FY25
    values = [100] * 24
    values[11] = 400                                                            # March 2024 = FY24 P12, FY24 Q4
    _, found, out = run(tmp_path, ["Period", "Imp"], [[d, v] for d, v in zip(dates, values)],
                        period_rows=[fiscal(d) for d in dates], pairs=[["Imp", None, "Y", "TV", "B", "A"]])
    qoq = {r["Period"]: r for r in sheet_rows(out, "QOQ_Changes")}
    assert qoq["FY24 Q4"]["Current Support"] == 600 and qoq["FY24 Q4"]["Change Status"] == "ALERT"
    assert qoq["FY25 Q1"]["Sequential Period"] == "FY24 Q4" and qoq["FY25 Q1"]["Last Year Same Period"] == "FY24 Q1"
    assert [(r["Period"], r["Last Year Same Period"]) for r in sheet_rows(out, "YOY_Changes")] == [("FY25", "FY24")]


def test_ratio_changes_use_period_totals_and_their_own_threshold(tmp_path):
    imp = [1000] * 6
    spend = [10, 10, 10, 13, 10, 10]                                            # CPM +30% in April, -23% in May
    _, found, out = run(tmp_path, ["Period", "Imp", "Spend"], monthly(imp, spend=spend),
                        thresholds=[["MOM % change", 40]], ratios=[["CPM", "Spend", "Imp", 25]])
    rows = [r for r in sheet_rows(out, "Ratio_Changes") if r["Period Type"] == "MOM"]
    assert [r["Change Status"] for r in rows] == ["PASS", "PASS", "ALERT", "PASS", "PASS"]
    assert rows[2]["Current Ratio"] == pytest.approx(0.013) and "N-17" not in found
    assert [f["detail"]["Period"] for f in found["N-18"]] == ["April 2024"]


def test_availability_flags_exactly_one_side(tmp_path):
    rows = [[date(2024, 1, 1) + timedelta(weeks=k), s, x] for k, (s, x) in
            enumerate([(10, 5), (0, 5), (10, None), (0, 0), (None, None), (10, 0)])]
    _, found, _ = run(tmp_path, ["Period", "Imp", "Spend"], rows, thresholds=[])
    assert [(f["detail"]["Date"], f["detail"]["Reason"]) for f in found["N-19"]] == [
        ("2024-01-08", "Spend available but Support unavailable"),
        ("2024-01-15", "Support available but Spend unavailable"),
        ("2024-02-05", "Support available but Spend unavailable")]


def test_concentration_statuses(tmp_path):
    pairs = [[f"S{i}", f"X{i}", "Y", f"Tactic {i}", "Digital", "Perf"] for i in range(3)] + \
            [["S9", "X9", "Y", "Lonely", "Digital", "Other"]]
    dates = months(date(2024, 1, 1), 3)
    spends = {0: (60, 20, 20), 1: (70, 15, 15), 2: (0, 0, 0)}                  # top 2: 80% (= limit), 85%, nothing
    rows = [[d, 1, spends[k][0], 1, spends[k][1], 1, spends[k][2], 1, 5] for k, d in enumerate(dates)]
    _, found, out = run(tmp_path, ["Period", "S0", "X0", "S1", "X1", "S2", "X2", "S9", "X9"], rows, pairs=pairs,
                        thresholds=[["Spend concentration - Top tactics count", 2], ["Spend concentration %", 80]],
                        dates=dates)
    status = {(r["Period"], r["Aggregation 1"]): r["Status"] for r in sheet_rows(out, "Spend_Concentration")
              if r["Rank"] == 1 and r["Period Type"] in ("MOM", "OVERALL")}
    assert status == {("January 2024", "Perf"): "PASS", ("February 2024", "Perf"): "ALERT",
                      ("March 2024", "Perf"): "NOT ASSESSED - ZERO TOTAL SPEND",
                      ("Full Input Range", "Perf"): "ALERT",              # 130 + 35 of 200
                      **{(m, "Other"): "NOT ASSESSED - INSUFFICIENT TACTICS"
                         for m in ("January 2024", "February 2024", "March 2024", "Full Input Range")}}
    (f,) = [f for f in found["N-20"] if f["detail"]["Period Type"] == "MOM"]
    assert f["detail"]["Top Spend Variables"] == "X0; X1" and f["detail"]["Skewness %"] == pytest.approx(85)


def test_text_periods_and_excel_serials(tmp_path):
    rows = [["13/01/2024", 1.0, 2.0], ["20/01/2024", 1.5, 2.5], [45318, 2.0, 3.0],     # day-first by evidence;
            ["31/02/2024", 9.0, 9.0], ["Feb 3, 2024", 2.5, 3.5]]                       # 45318 = 27 Jan; no Feb 31
    res, _, _ = run(tmp_path, ["Period", "Imp", "Spend"], rows,
                    dates=[date(2024, 1, 13), date(2024, 1, 20), date(2024, 1, 27), date(2024, 2, 3)])
    assert res["axis"]["dates_with_data"] == 4 and res["axis"]["rows_not_dated"] == 1


def test_dates_outside_period_mapping_are_left_out_and_configured_dates_are_zero(tmp_path):
    rows = [[date(2024, 1, 1) + timedelta(weeks=k), 100.0 + k, 10.0] for k in range(6)]
    configured = [r[0] for r in rows[:5]] + [date(2024, 2, 12), date(2024, 2, 19)]   # week 6 not, two empty weeks
    res, _, out = run(tmp_path, ["Period", "Imp", "Spend"], rows, dates=configured)
    trend = sheet_rows(out, "Trend Line")
    assert [r["Support"] for r in trend] == [100, 101, 102, 103, 104, 0, 0]
    assert res["axis"]["rows_outside_axis"] == 1 and res["axis"]["configured_dates"] == 7


# ── spec §3.6: no defaults, inactive rules visible ──────────────────────────
def test_rules_without_a_threshold_do_not_run_and_are_listed(tmp_path):
    rows = [[date(2024, 1, 1) + timedelta(weeks=k), 100.0 if k != 6 else 9000.0, 10.0 if k != 3 else 0]
            for k in range(30)]
    res, found, out = run(tmp_path, ["Period", "Imp", "Spend"], rows, thresholds=[["MOM % change", 40]])
    assert set(found) == {"N-17", "N-19"}                                       # 9000 is no outlier without a threshold
    assert sheet_rows(out, "Outlier_Details") == [] and sheet_rows(out, "QOQ_Changes") == []
    status = {r["Config Key"]: r["Status"] for r in res["rules"]}
    assert status == {"robust_z_threshold": "INACTIVE", "mom_pct": "ACTIVE", "qoq_pct": "INACTIVE",
                      "hoh_pct": "INACTIVE", "yoy_pct": "INACTIVE", "top_n": "INACTIVE",
                      "concentration_pct": "INACTIVE", "Ratio Definitions": "NOT CONFIGURED", "": "ALWAYS ON"}
    assert len(res["inactive_rules"]) == 5                                      # N-16, QOQ, HOH, YOY, N-20 once
    wb = openpyxl.load_workbook(out / "Spend_Support_Trend_Report.xlsx")
    assert wb["Rules_Used"].sheet_state == "visible"
    no_sheet, found, _ = run(tmp_path / "none", ["Period", "Imp", "Spend"], rows, specs=False)
    assert set(found) == {"N-19"} and len(no_sheet["inactive_rules"]) == 6         # MOM too


def test_rules_used_is_hidden_when_every_rule_is_configured(tmp_path):
    rows = [[date(2024, 1, 1) + timedelta(weeks=k), 100.0, 10.0] for k in range(10)]
    res, _, out = run(tmp_path, ["Period", "Imp", "Spend"], rows)
    assert res["inactive_rules"] == []
    assert openpyxl.load_workbook(out / "Spend_Support_Trend_Report.xlsx")["Rules_Used"].sheet_state == "hidden"


@pytest.mark.parametrize("text, key", [
    ("MOM % change", "mom_pct"), ("mom_pct", "mom_pct"), ("Quarter on Quarter % Change", "qoq_pct"),
    ("Half Year on Half Year % change", "hoh_pct"), ("YOY % change", "yoy_pct"),
    ("Abnormal Trend — Robust Z-Score", "robust_z_threshold"), ("robust_z_threshold", "robust_z_threshold"),
    ("Spend concentration - Top tactics count", "top_n"), ("top_n", "top_n"),
    ("Spend concentration %", "concentration_pct"), ("Month on Month Duplicate", None), ("MOM Duplicate", None),
    ("No Executions", None), ("CPM % change", None)])
def test_rule_names(text, key):
    assert rule_key(text) == key


def test_threshold_cells_typed_as_percentages(tmp_path):
    cfg = client_config(tmp_path / "c.xlsx", thresholds=[["MOM % change", 0.4], ["QOQ % change", "35%"]],
                        dates=[date(2024, 1, 1)])
    wb = openpyxl.load_workbook(cfg)
    wb["Data Check Specifications"]["B2"].number_format = "0%"                 # 0.4 shown as 40%
    wb.save(cfg)
    from src.trends.config import load_client_config
    th = load_client_config(cfg, load_settings()).thresholds
    assert th["mom_pct"] == 40 and th["qoq_pct"] == 35


@pytest.mark.parametrize("thresholds, message", [
    ([["MOM % change", "lots"]], "'lots' is not a number"),
    ([["Spend concentration - Top tactics count", 2.5]], "whole number of tactics"),
    ([["YOY % change", -5]], "must be at least 0"),
    ([["Abnormal trend - Robust Z-score", 0]], "must be above 0"),
    ([["MOM % change", 30], ["mom_pct", 40]], "configured twice"),
])
def test_bad_thresholds_say_what_to_fix(tmp_path, thresholds, message):
    with pytest.raises(ConfigError, match=message):
        run(tmp_path, ["Period", "Imp", "Spend"], monthly([1, 2], spend=[1, 2]), thresholds=thresholds)


@pytest.mark.parametrize("ratios, message", [
    ([["CPM", "Spend", "Imp", 20], ["CPM of CPM", "CPM", "Imp", 20]], "a ratio of ratios is not supported"),
    ([["CPC", "Spend", "Clicks", 20]], "Denominator 'Clicks' is not a variable of Variable Mapping"),
    ([["CPM", "Spend", None, 20]], "needs both a Numerator and a Denominator"),
    ([["CPM", "Spend", "Imp", 20], ["cpm", "Imp", "Spend", 20]], "ratio name 'cpm' is used on rows 2 and 3"),
])
def test_ratio_definition_errors(tmp_path, ratios, message):
    with pytest.raises(ConfigError, match=message):
        run(tmp_path, ["Period", "Imp", "Spend"], monthly([1, 2], spend=[1, 2]), ratios=ratios)


def test_ratios_on_switched_off_or_absent_variables_are_inactive(tmp_path):
    pairs = PAIR + [["Clicks", "Cost", "N", "Search", "Digital", "Perf"], ["Views", "Fee", "Y", "Video", "D", "P"]]
    res, _, out = run(tmp_path, ["Period", "Imp", "Spend", "Clicks", "Cost"], [[d, 1, 2, 3, 4] for d in months(
        date(2024, 1, 1), 3)], pairs=pairs, ratios=[["CPC", "Cost", "Clicks", 20], ["CPV", "Fee", "Views", 20]])
    assert res["inactive_rules"] == [
        "N-18 Ratio: CPC (Cost / Clicks): Numerator 'Cost' is not on a Y row of Variable Mapping: this ratio is not checked",
        "N-18 Ratio: CPV (Fee / Views): Numerator 'Fee' not found in input file: this ratio is not checked"]
    assert "Ratio 'CPV': Numerator 'Fee' not found in input file; the ratio is not checked." in \
        [r["Reason"] for r in sheet_rows(out, "Unmapped_or_Missing")]


@pytest.mark.parametrize("kw, message", [
    ({"pairs": [["Imp", "Spend", "N", "TV", "B", "A"]]}, "no variables marked Y"),
    ({"period_header": ("Date", "Months", "Quarters", "Half Years")}, "missing: Full Years"),
    ({"period_rows": [labels(date(2024, 1, 1)), ["soon", "x", "x", "x", "x"]]}, "'soon' \\(row 3\\)"),
    ({"data_file": [["Period", "A"]]}, "needs a Column for: Variables"),
    ({"data_file": [["Period", "A"], ["Variables", "Z"]]}, "Variables is column Z"),
])
def test_config_errors_say_what_to_fix(tmp_path, kw, message):
    with pytest.raises(ConfigError, match=message):
        run(tmp_path, ["Period", "Imp", "Spend"], monthly([1, 2], spend=[1, 2]), **kw)


# ── Phase C -> Phase D ───────────────────────────────────────────────────────
def test_phase_d_reads_phase_c_output_and_its_client_config(tmp_path):
    from src.timeseries.pipeline import run_timeseries
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    cfg = wbk.build_config(tmp_path / "config.xlsx")                            # one workbook for both phases
    run_timeseries(str(data), str(tmp_path / "run"), str(cfg))
    res = run_d(str(tmp_path / "run"), str(tmp_path / "run"))              # no --client-config: Phase C's copy
    got = wbk.summarize(json.loads((unit(tmp_path / "run") / "findings.json").read_text("utf8")))
    assert got == wbk.EXPECTED and res["source_file"] == "weekly.xlsx"
    table = pl.read_parquet(next((unit(tmp_path / "run")).glob("tables/*/data.parquet")))
    assert table.height == 158 and table["_row_id"][0].endswith(":1")


def test_a_folder_without_phase_c_output_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError, match="neither a Phase C nor a Phase B result"):
        run_d(str(tmp_path), str(tmp_path / "run"), str(wbk.build_config(tmp_path / "c.xlsx")))


# ── review loop ──────────────────────────────────────────────────────────────
def _report(tmp_path):
    _planted(tmp_path)
    return unit(tmp_path / "run") / "Spend_Support_Trend_Report.xlsx"


def _mark(ws, predicate, mark) -> list[str]:
    head = [c.value for c in ws[1]]
    marked = []
    for row in ws.iter_rows(min_row=2):
        values = dict(zip(head, (c.value for c in row)))
        if values["Finding_ID"] and predicate(values):
            row[head.index("Review")].value = mark
            marked.append(values["Finding_ID"])
    return marked


def _data_cell(ws, period: date, column: str):
    head = [c.value for c in ws[1]]
    row = next(r for r in ws.iter_rows(min_row=2) if r[head.index("Period")].value.date() == period)
    return row[head.index(column)]


def test_review_marks_are_kept_prefilled_and_can_be_cleared(tmp_path):
    report = _report(tmp_path)
    wb = openpyxl.load_workbook(report)
    valid = _mark(wb["Outlier_Details"], lambda r: r["Support Variable"] == "Radio_GRPs", "valid movement")
    confirmed = _mark(wb["MOM_Changes"], lambda r: r["Period"] == "March 2024", "Confirmed Issue")
    wb.save(tmp_path / "edited.xlsx")
    res = apply_upload(tmp_path / "run", tmp_path / "edited.xlsx", load_settings())
    assert res["counts"]["N-16"] == {"open": 2, "confirmed": 0, "valid": 1, "corrected": 0}
    assert res["counts"]["N-17"]["confirmed"] == 1 and res["totals"]["findings_open"] == 12
    assert set(json.loads((unit(tmp_path / "run") / "review.json").read_text("utf8"))) == set(valid + confirmed)
    run_d(str(tmp_path / "weekly.xlsx"), str(tmp_path / "run"), str(tmp_path / "config.xlsx"))
    assert len(by_code(tmp_path / "run", "valid")["N-16"]) == 1                # a later run keeps the marks
    wb = openpyxl.load_workbook(report)
    reviews = {r["Finding_ID"]: r["Review"] for r in sheet_rows(report.parent, "Outlier_Details")}
    assert reviews[valid[0]] == "Valid Movement"                               # pre-filled in the next download
    _mark(wb["Outlier_Details"], lambda r: r["Finding_ID"] in valid, None)
    wb.save(tmp_path / "cleared.xlsx")
    res = apply_upload(tmp_path / "run", tmp_path / "cleared.xlsx", load_settings())
    assert res["counts"]["N-16"]["valid"] == 0 and res["counts"]["N-17"]["confirmed"] == 1
    audit = json.loads((unit(tmp_path / "run") / "review_audit.json").read_text("utf8"))
    assert [a["action"] for a in audit] == ["marked", "marked", "review_cleared"]


def test_corrected_findings_are_rechecked(tmp_path):
    report = _report(tmp_path)
    wb = openpyxl.load_workbook(report)
    fixed = _mark(wb["Spend_Support_Availability"], lambda r: r["Date"].date() == date(2024, 7, 15), "Corrected")
    _data_cell(wb["Data"], date(2024, 7, 15), "Social_Spend").value = 450      # the missing spend, supplied
    unfixed = _mark(wb["Outlier_Details"], lambda r: r["Support Variable"] == "Display_Impressions", "Corrected")
    wb.save(tmp_path / "edited.xlsx")
    res = apply_upload(tmp_path / "run", tmp_path / "edited.xlsx", load_settings())
    assert res["counts"]["N-19"] == {"open": 1, "confirmed": 0, "valid": 0, "corrected": 1}
    findings = {f["finding_id"]: f for f in json.loads((unit(tmp_path / "run") / "findings.json").read_text("utf8"))}
    assert findings[fixed[0]]["status"] == "corrected" and not findings[fixed[0]]["listed"]
    assert findings[unfixed[0]]["status"] == "open" and "still flagged" in findings[unfixed[0]]["note"]
    audit = json.loads((unit(tmp_path / "run") / "review_audit.json").read_text("utf8"))
    assert [a["action"] for a in audit] == ["edited", "marked", "marked", "corrected_still_flagged"]
    assert audit[0]["column"] == "Social_Spend" and audit[0]["new_value"] == "450"
    df = pl.read_parquet(next((unit(tmp_path / "run")).glob("tables/*/data.parquet")))
    assert df.filter(pl.col("Period") == date(2024, 7, 15))["Social_Spend"][0] == 450


def test_untouched_upload_changes_nothing(tmp_path):
    report = _report(tmp_path)
    before = json.loads((unit(tmp_path / "run") / "findings.json").read_text("utf8"))
    apply_upload(tmp_path / "run", report, load_settings())
    assert json.loads((unit(tmp_path / "run") / "findings.json").read_text("utf8")) == before
    assert json.loads((unit(tmp_path / "run") / "review_audit.json").read_text("utf8")) == []
    assert len(list((unit(tmp_path / "run") / "review_versions").glob("*.xlsx"))) == 3


def _first_pass_row(wb):
    ws = wb["MOM_Changes"]
    head = [c.value for c in ws[1]]
    return next(r for r in ws.iter_rows(min_row=2) if r[head.index("Change Status")].value == "PASS"), head


@pytest.mark.parametrize("edit, message", [
    (lambda wb: _first_pass_row(wb)[0][-1].__setattr__("value", "Valid Movement"), "only for rows with a Finding_ID"),
    (lambda wb: _mark(wb["Outlier_Details"], lambda r: True, "fine by me"), "Review must be blank or one of"),
    (lambda wb: wb["Spend Skewness"].cell(2, 16, "N-20-edited"), "a Finding_ID was edited"),
    (lambda wb: wb["Outlier_Details"].delete_rows(2), "rows with a Finding_ID were added or removed"),
    (lambda wb: wb["Data"].cell(1, 3, "Region"), "Data: column headers changed"),
    (lambda wb: wb["Data"].cell(3, 1, "nope:1"), "unknown Row_ID"),
    (lambda wb: wb.remove(wb["Rules_Used"]), "sheets must be exactly"),
])
def test_bad_uploads_are_rejected(tmp_path, edit, message):
    report = _report(tmp_path)
    wb = openpyxl.load_workbook(report)
    edit(wb)
    wb.save(tmp_path / "edited.xlsx")
    with pytest.raises(ReviewError, match=message):
        apply_upload(tmp_path / "run", tmp_path / "edited.xlsx", load_settings())


def test_capped_sheets_still_upload(tmp_path):
    _planted(tmp_path, overrides={"max_sheet_rows": 2})
    report = unit(tmp_path / "run") / "Spend_Support_Trend_Report.xlsx"
    findings = json.loads((unit(tmp_path / "run") / "findings.json").read_text("utf8"))
    assert sum(not f["listed"] for f in findings) == 9          # 4 MOM, 1 QOQ, 3 ratio, 1 outlier beyond row 2
    wb = openpyxl.load_workbook(report)
    assert wb["MOM_Changes"].max_row == 3
    _mark(wb["Outlier_Details"], lambda r: True, "Valid Movement")
    wb.save(tmp_path / "edited.xlsx")
    res = apply_upload(tmp_path / "run", tmp_path / "edited.xlsx", load_settings(overrides={"max_sheet_rows": 2}))
    assert res["counts"]["N-16"]["valid"] == 2


def test_cli_exit_codes(tmp_path, capsys):
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    cfg = wbk.build_config(tmp_path / "config.xlsx")
    assert cli(["run", str(data), "-o", str(tmp_path / "run"), "--client-config", str(cfg)]) == 0
    assert json.loads(capsys.readouterr().out)["tables"][0]["totals"]["findings_open"] == len(wbk.EXPECTED)
    wb = openpyxl.load_workbook(unit(tmp_path / "run") / "Spend_Support_Trend_Report.xlsx")
    wb["Data"].cell(2, 1, "bad")
    wb.save(tmp_path / "bad.xlsx")
    assert cli(["review", str(tmp_path / "run"), str(tmp_path / "bad.xlsx")]) == 2
    assert cli(["run", str(data), "-o", str(tmp_path / "r2"), "--client-config", str(data)]) == 1   # not a config
    assert cli(["run", str(data), "-o", str(tmp_path / "r3")]) == 1                                  # no config at all


# ── pre-filled client config (init-config) ───────────────────────────────────
def test_init_config_fills_what_the_data_tells_and_asks_for_the_rest(tmp_path):
    from src.trends.autoconfig import ASK, AUTO, init_config
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    info = init_config(str(data), str(tmp_path / "cfg.xlsx"))
    assert (info["period"], info["geography"], info["first_variable"]) == ("Period", "Geography", "TV_Impressions")
    assert info["variables"] == 16 and info["paired"] == 7 and info["marked_y"] == 7 and info["dates"] == 158
    wb = openpyxl.load_workbook(tmp_path / "cfg.xlsx")
    assert wb.sheetnames == ["Read_Me_First", "Data File", "Variable Mapping", "Period Mapping",
                             "Data Check Specifications", "Ratio Definitions"]
    assert not any(ws.protection.sheet for ws in wb.worksheets)                 # nothing is locked
    df = {r[0]: r[1] for r in wb["Data File"].iter_rows(min_row=2, values_only=True)}
    assert df == {"Period": "Period", "Geography": "Geography", "Variables": "TV_Impressions"}
    vm = {r[0].value: r for r in wb["Variable Mapping"].iter_rows(min_row=2)}
    tv = vm["TV_Impressions"]
    assert [c.value for c in tv[:6]] == ["TV_Impressions", "TV Impressions", "Tv", "TV_Spend", "Y", "Tv"]
    assert tv[0].fill.fgColor.rgb.endswith(AUTO.fgColor.rgb) and "paired with 'TV_Spend'" in tv[7].value
    assert vm["Cinema_Admits"][6].value is None and vm["Cinema_Admits"][4].value is None     # type unknown: yours
    assert vm["Cinema_Admits"][4].fill.fgColor.rgb.endswith(ASK.fgColor.rgb)
    assert vm["TV_Impressions"][5].fill.fgColor.rgb.endswith(ASK.fgColor.rgb)                # N-20 grouping: yours
    assert vm["TV_Spend"][4].value is None and vm["TV_Spend"][6].value == "Spend"
    specs = {r[0]: r[1] for r in wb["Data Check Specifications"].iter_rows(min_row=2, values_only=True)}
    assert len(specs) == 7 and set(specs.values()) == {None}                    # no threshold is invented
    assert "Possible: TV_Spend / TV_Impressions" in wb["Ratio Definitions"]["E2"].value


def test_a_pre_filled_workbook_runs_once_the_yellow_cells_are_entered(tmp_path):
    from src.trends.autoconfig import init_config
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    init_config(str(data), str(tmp_path / "cfg.xlsx"))
    blank = run_d(str(data), str(tmp_path / "blank"), str(tmp_path / "cfg.xlsx"))
    assert blank["totals"]["findings_open"] == 2 and len(blank["inactive_rules"]) == 6      # only N-19 runs
    wb = openpyxl.load_workbook(tmp_path / "cfg.xlsx")
    for row in wb["Data Check Specifications"].iter_rows(min_row=2):
        row[1].value = {"Abnormal trend - Robust Z-score": 3.5, "Spend concentration - Top tactics count": 2,
                        "Spend concentration %": 70}.get(row[0].value, 40)
    ratio = wb["Ratio Definitions"]
    for j, v in enumerate(["TV CPM", "TV_Spend", "TV_Impressions", 20], start=1):
        ratio.cell(2, j).value = v
    wb.save(tmp_path / "cfg.xlsx")
    res = run_d(str(data), str(tmp_path / "run"), str(tmp_path / "cfg.xlsx"))
    assert res["inactive_rules"] == [] and set(res["counts"]) == {"N-16", "N-17", "N-18", "N-19"}   # N-20: one
    assert res["counts"]["N-17"]["open"] > 0                    # tactic per group until Aggregation 1 is edited
    assert res["counts"]["N-16"]["open"] == 3 and res["counts"]["N-19"]["open"] == 2


def test_init_config_cli(tmp_path, capsys):
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    assert cli(["init-config", str(data), "-o", str(tmp_path / "cfg.xlsx")]) == 0
    assert "yellow cells (" in capsys.readouterr().err
    notes = write_xlsx(tmp_path / "notes.xlsx", {"S": [["Comment", "Text"], ["a", "b"]]})
    assert cli(["init-config", str(notes), "-o", str(tmp_path / "x.xlsx")]) == 1
