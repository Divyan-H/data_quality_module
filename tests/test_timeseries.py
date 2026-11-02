"""Phase C (src/timeseries) tests. Workbooks are built in tmp_path; nothing is committed."""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl
import polars as pl
import pytest

from src.timeseries.cli import main as cli
from src.timeseries.config import load_settings
from src.common import units
from src.common.errors import ConfigError
from src.timeseries import pipeline
from src.timeseries.pipeline import OUT
from src.timeseries.report import COLUMNS, SHEETS, VALID
from src.timeseries.review import ReviewError, apply_upload

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import build_phase_c_workbook as wbk  # noqa: E402

START = date(2024, 1, 1)




def run_c(input_path, run_dir, *args, **kw):
    """Run the phase and return the run.json of the one table it checked (the index lists tables, not layouts)."""
    import src.timeseries.pipeline as pipeline
    pipeline.run_timeseries(input_path, run_dir, *args, **kw)
    (folder,) = [f for _, f in units.checked(Path(run_dir) / "phase_c_timeseries")]
    return json.loads((folder / "run.json").read_text("utf8"))

def unit(run_dir: Path) -> Path:
    """The folder of the one table a single-table run checked (src/common/units.py)."""
    (folder,) = [f for _, f in units.checked(Path(run_dir) / OUT)]
    return folder

def week(k: int) -> date:
    return START + timedelta(weeks=k)


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


def client_config(path: Path, variables, specs=None, data_file=None, period_mapping=True, weeks=80) -> Path:
    """Client config workbook; `variables` = [(name, bucket)], `specs` = Data Check Specifications rows."""
    sheets = {"Data File": [["Description", "Column"],
                            *(data_file or [["Period", "A"], ["Geography", "B"], ["Variables", "C"]])],
              "Variable Mapping": [["Variables", "Description", "Bucket"], *[[n, f"{n} desc", b] for n, b in variables]]}
    if period_mapping:
        sheets["Period Mapping"] = [["Period", "Months"], *[[week(k), week(k).strftime("%b-%y")] for k in range(weeks)]]
    if specs:
        sheets["Data Check Specifications"] = specs
    return write_xlsx(path, sheets)


def run(tmp_path: Path, header, rows, variables=None, **kw):
    """Phase C on one data sheet -> (run.json, open findings by code, output folder)."""
    data = write_xlsx(tmp_path / "data.xlsx", {"Weekly": [header, *rows]})
    cfg = kw.pop("config", None) or client_config(tmp_path / "config.xlsx",
                                                  variables or [(h, "Spend") for h in header[2:]], **kw)
    res = run_c(str(data), str(tmp_path / "run"), str(cfg))
    return res, by_code(tmp_path / "run"), unit(tmp_path / "run")


def by_code(run_dir: Path) -> dict:
    out = {}
    for f in json.loads((unit(run_dir) / "findings.json").read_text("utf8")):
        if f["status"] == "open":
            out.setdefault(f["code"], []).append(f)
    return out


def series(values, k0=0) -> list[list]:
    """Weekly rows (Period, Geography, Var) for a list of values."""
    return [[week(k0 + k), "National", v] for k, v in enumerate(values)]


def distinct(n: int, base: float = 100.0) -> list[float]:
    return [round(base + 1.37 * k, 2) for k in range(n)]


# ── the planted workbook: every check, and parity with the reference report ──
def test_planted_workbook_reports_exactly_the_planted_issues(tmp_path):
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    cfg = wbk.build_config(tmp_path / "config.xlsx")
    res = run_c(str(data), str(tmp_path / "run"), str(cfg))
    got = wbk.summarize(json.loads((unit(tmp_path / "run") / "findings.json").read_text("utf8")))
    assert got == wbk.EXPECTED, f"missing {wbk.EXPECTED - got}, extra {got - wbk.EXPECTED}"
    assert res["dataset_checks"] == {"N-13": "FAIL", "N-11": "FAIL", "N-12": "FAIL", "N-14": "FAIL", "N-15": "FAIL"}
    assert res["variables"] == {"in_range": 9, "checked": 6, "ignored": 3}
    assert res["periods"] == {"total": 59, "first": "2024-01-01", "last": "2025-02-17"}


def test_specification_rows_may_be_named_in_a_rule_column(tmp_path):
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    cfg = wbk.build_config(tmp_path / "config.xlsx", specs_header="Rule")      # Phase D's column name
    run_c(str(data), str(tmp_path / "run"), str(cfg))
    got = wbk.summarize(json.loads((unit(tmp_path / "run") / "findings.json").read_text("utf8")))
    assert got == wbk.EXPECTED


def test_report_sheets_review_columns_and_marked_cells(tmp_path):
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    run_c(str(data), str(tmp_path / "run"), str(wbk.build_config(tmp_path / "config.xlsx")))
    wb = openpyxl.load_workbook(unit(tmp_path / "run") / "Data_Validation_Report.xlsx")
    assert wb.sheetnames == SHEETS
    for sheet in ("Variable_Checks", "WOW_Duplicates", "MOM_Duplicates", "No_Executions", "Duplicate_Variable_Data",
                  "Date_Integrity"):
        header = [c.value for c in wb[sheet][1]]
        assert header[-2:] == ["Finding_ID", "Review"]
        assert wb[sheet].data_validations.dataValidation[0].formula1 == f'"{VALID}"'
    errors = {r[2]: r for r in wb["Errors"].iter_rows(min_row=2, values_only=True)}
    assert errors["TV_Impressions"][3] == 29 and errors["TV_Impressions"][4] == "N-07"     # N-06 execution weeks
    weeks = {r[2]: (r[3], r[4]) for r in wb["Execution_Weeks"].iter_rows(min_row=2, values_only=True)}
    assert weeks["Social_Spend"] == ("Checked", 60)                                       # no error, still shown
    assert weeks["Print_Spend"][0] == "Ignored: Bucket '#N/A'" and weeks["Missing_Var"][1] is None
    flagged = {c.coordinate: c.comment.text for row in wb["Data"].iter_rows() for c in row if c.comment}
    assert "N-03 NULL / N/A" in flagged["H6"] and flagged["B11"] == flagged["B12"] == "N-11 Duplicate Dates"
    status = {r[0]: r[2] for r in wb["Overall_Summary"].iter_rows(min_row=2, values_only=True) if r[0]}
    assert set(status.values()) == {"FAIL"}


# ── one check at a time ──────────────────────────────────────────────────────
def test_no_executions_runs_need_consecutive_weeks(tmp_path):
    zeros = [1.0] + [0] * 9 + [2.0] + [0] * 8 + [3.0]                          # 9 zeros: flagged; 8: not (default 9)
    rows = series(zeros)
    rows += [[week(len(zeros) + k), "National", 0] for k in range(5)]          # 5 zeros, then a gap,
    rows += [[week(len(zeros) + 6 + k), "National", 0] for k in range(5)]     # 5 more: not one run of 10
    res, found, _ = run(tmp_path, ["Period", "Geography", "Var"], rows)
    runs = [(f["detail"]["Start Period"], f["detail"]["Number of Weeks"]) for f in found["N-07"]]
    assert runs == [(week(1).isoformat(), 9)]


def test_wow_reports_each_run_and_each_recurring_value_once(tmp_path):
    values = distinct(10)
    values[2] = values[3] = values[7] = 55.5
    _, found, _ = run(tmp_path, ["Period", "Geography", "Var"], series(values))
    got = sorted((f["detail"]["Period"], f["detail"]["Compared Period"], f["detail"]["Comparison Type"],
                  f["detail"]["Occurrences"]) for f in found["N-08"])
    assert got == [(week(3).isoformat(), week(2).isoformat(), "Sequential", 2),
                   (week(7).isoformat(), week(2).isoformat(), "Not Sequential", 3)]


def test_series_are_checked_separately_at_their_own_cadence(tmp_path):
    days = [date(2024, 3, 1) + timedelta(days=k) for k in range(12)]
    rows = [[d, city, round(10 + 1.37 * k + (0.5 if city == "B" else 0), 2)] for k, d in enumerate(days)
            for city in ("A", "B")]                                   # daily, two series sharing every date
    rows[6][2] = rows[8][2] = rows[10][2] = 77.25                     # A: three days running -> one Sequential run
    rows.insert(10, [days[4], "B", 99.5])                             # B: day 5 twice -> N-11
    _, found, out = run(tmp_path, ["Period", "City", "Var"], rows)
    assert sorted(found) == ["N-08", "N-11"]
    (rep,) = found["N-08"]
    assert rep["detail"]["Series"] == "A" and rep["detail"]["Occurrences"] == 3
    (dup,) = found["N-11"]
    assert dup["detail"]["Series"] == "B" and dup["detail"]["Period"] == days[4].isoformat()
    info = {r[1]: r[3] for r in openpyxl.load_workbook(out / "Data_Validation_Report.xlsx")["Overall_Summary"]
            .iter_rows(min_row=2, values_only=True)}
    assert info["Cadence"] == "daily" and info["Series"] == "2 series by City"


def test_zeros_are_never_duplicates(tmp_path):
    _, found, _ = run(tmp_path, ["Period", "Geography", "Var"], series([0, 0, 5.5, 0, 6.5, 0]))
    assert "N-08" not in found and "N-09" not in found


def test_duplicate_variable_data_ignores_zero_versus_blank(tmp_path):
    a = distinct(8)
    b = list(a)
    a[3], b[3] = 0, None                                                       # 0 and blank are both "no value"
    c = list(a)
    c[5] = 1.0                                                                 # one value differs: not a copy
    rows = [[week(k), "National", a[k], b[k], c[k], a[k]] for k in range(8)]
    _, found, _ = run(tmp_path, ["Period", "Geography", "A", "B", "C", "D"], rows)
    assert sorted((f["detail"]["Variable 1"], f["detail"]["Variable 2"]) for f in found["N-10"]) == \
        [("A", "B"), ("A", "D"), ("B", "D")]


def test_month_totals_use_period_mapping_labels(tmp_path):
    jan = [10.0, 20.0, 30.0, 40.0, 50.0]                                       # 5 Mondays in Jan 2024: 150
    feb = [36.0, 37.0, 38.0, 39.0]                                             # 4 Mondays in Feb 2024: 150
    _, found, _ = run(tmp_path, ["Period", "Geography", "Var"], series(jan + feb))
    (f,) = found["N-09"]
    assert (f["detail"]["Month"], f["detail"]["Compared Month"], f["detail"]["Comparison Type"]) == \
        ("Feb-24", "Jan-24", "Sequential")


def test_date_checks(tmp_path):
    rows = series(distinct(6))
    rows[4][0] = week(5) + timedelta(days=3)                                   # 10 days after week 3: gap, missing
    rows[5][0] = week(2)                                                       # duplicate of week 2, out of order
    _, found, _ = run(tmp_path, ["Period", "Geography", "Var"], rows)
    assert [f["detail"]["Period"] for f in found["N-11"]] == [week(2).isoformat()]
    assert [f["detail"]["Period"] for f in found["N-12"]] == [week(4).isoformat(), week(5).isoformat()]
    assert [f["detail"]["Period"] for f in found["N-13"]] == [week(2).isoformat()]
    assert [f["detail"]["Detail"] for f in found["N-14"]] == [f"17 days after {week(3)} (expected 7)"]


def test_value_checks_follow_the_spec_null_tokens(tmp_path):
    values = distinct(6)
    values[1], values[2], values[3], values[4] = "NA", " 12 ", "1,000", ""
    _, found, _ = run(tmp_path, ["Period", "Geography", "Var"], series(values))
    counts = {c: found[c][0]["detail"]["Count"] for c in ("N-01", "N-02", "N-03", "N-04", "N-05")}
    assert counts == {"N-01": 2, "N-02": 1, "N-03": 1, "N-04": 1, "N-05": 2}  # ' 12 ' is a number with spaces


def test_text_periods_and_excel_serials(tmp_path):
    rows = [["13/01/2024", "National", 1.5], ["20/01/2024", "National", 2.5], [45318, "National", 3.5]]
    _, found, out = run(tmp_path, ["Period", "Geography", "Var"], rows)        # day-first by evidence; 45318 = 27 Jan
    assert not found
    assert json.loads((out / "run.json").read_text("utf8"))["periods"]["last"] == "2024-01-27"


def test_variables_without_a_usable_bucket_are_not_checked(tmp_path):
    rows = [[week(k), "National", 5.0, "x", 7.0] for k in range(4)]
    res, found, _ = run(tmp_path, ["Period", "Geography", "A", "B", "C"], rows,
                        variables=[("A", "Spend"), ("B", "#N/A"), ("C", "0")])
    assert res["variables"] == {"in_range": 3, "checked": 1, "ignored": 2}
    assert {f["variables"][0] for f in found["N-08"]} == {"A"} and "N-01" not in found


def test_names_and_letters_both_locate_columns(tmp_path):
    rows = [[f"row {k}", week(k), "National", 1.0 + k] for k in range(5)]
    res, found, _ = run(tmp_path, ["Note", "Week Of", "Region", "Spend"], rows,
                        data_file=[["Period", "Week Of"], ["Geography", "c"], ["Variables", "Spend"]])
    assert res["layout"] == {"period": "Week Of", "geography": "Region", "variables": ["Spend"]} and not found


# ── config problems are reported, not guessed ────────────────────────────────
@pytest.mark.parametrize("kw, message", [
    ({"data_file": [["Period", "A"], ["Geography", "B"], ["Variables", "Z"]]}, "Variables is column Z"),
    ({"data_file": [["Period", "B"], ["Geography", "B"], ["Variables", "C"]]}, "holds no dates"),
    ({"data_file": [["Period", "A"], ["Variables", "C"]]}, "needs a Column for: Geography"),
    ({"period_mapping": False}, "'Period Mapping' sheet"),
    ({"specs": [["Description", "From", "No of Weeks"], ["No Executions", "soon", None]]}, "'soon' is not a date"),
    ({"specs": [["Description", "No of Weeks"], ["No Executions", "-2"]]}, "No of Weeks"),
])
def test_config_errors_say_what_to_fix(tmp_path, kw, message):
    with pytest.raises(ConfigError, match=message):
        run(tmp_path, ["Period", "Geography", "Var"], series(distinct(4)), **kw)


def test_geography_can_be_optional(tmp_path):
    data = write_xlsx(tmp_path / "d.xlsx", {"S": [["Period", "Var"], *[[week(k), 1.0 + k] for k in range(4)]]})
    cfg = client_config(tmp_path / "c.xlsx", [("Var", "Spend")], data_file=[["Period", "A"], ["Variables", "B"]])
    res = run_c(str(data), str(tmp_path / "run"), str(cfg), overrides={"require_geography": False})
    assert res["layout"]["geography"] is None and res["variables"]["checked"] == 1


def test_the_table_the_config_describes_is_picked(tmp_path):
    good = [["Period", "Geography", "Var"], *series(distinct(5))]
    data = write_xlsx(tmp_path / "d.xlsx", {"Notes": [["Comment", "Owner", "Text"], ["a", "b", "c"]], "Weekly": good})
    cfg = client_config(tmp_path / "c.xlsx", [("Var", "Spend")])
    assert run_c(str(data), str(tmp_path / "r1"), str(cfg))["sheet"] == "Weekly"
    index = units.read_index(tmp_path / "r1" / OUT)                       # Notes is listed, with the reason
    assert [(e["sheet"], e["folder"] is not None) for e in index["tables"]] == [("Notes", False), ("Weekly", True)]
    two = write_xlsx(tmp_path / "two.xlsx", {"W1": good, "W2": good})
    pipeline.run_timeseries(str(two), str(tmp_path / "r2"), str(cfg))      # every table the config describes is checked
    assert [e["sheet"] for _, e in [(0, e) for e, _ in units.checked(tmp_path / "r2" / OUT)]] == ["W1", "W2"]
    assert run_c(str(two), str(tmp_path / "r3"), str(cfg), table="*:1:t1")["sheet"] == "W2"
    with pytest.raises(ConfigError, match="describes none"):
        run_c(str(data), str(tmp_path / "r4"), str(cfg), table="*:0:t1")


# ── Phase B -> Phase C ───────────────────────────────────────────────────────
def test_phase_c_reads_phase_b_output(tmp_path):
    from src.hygiene.pipeline import run_hygiene
    lines = ["Period,Geography,Spend,Clicks,Views"] + \
        [f"{week(k)},National,{100 + 3 * k},{'NA' if k == 2 else 500 + 7 * k},{k}" for k in range(30)]
    (tmp_path / "in.csv").write_text("\n".join(lines), encoding="utf8")
    run_hygiene(str(tmp_path / "in.csv"), str(tmp_path / "run"))
    cfg = client_config(tmp_path / "c.xlsx", [("Spend", "Spend"), ("Clicks", "Support")])
    res = run_c(str(tmp_path / "run"), str(tmp_path / "run"), str(cfg))
    found = by_code(tmp_path / "run")
    assert res["source_file"] == "in.csv" and res["variables"]["checked"] == 2
    assert found["N-02"][0]["variables"] == ["Clicks"]               # Phase B turned 'NA' into NULL
    assert pl.read_parquet(next((unit(tmp_path / "run")).glob("tables/*/data.parquet")))["Spend"].dtype == pl.Int64


def test_repeated_names_from_phase_a_count_as_duplicates(tmp_path):
    df = pl.DataFrame({"_row_id": [f"k:0:t1:{n}" for n in range(1, 5)], "_source_row": [2, 3, 4, 5],
                       "Period": [week(k) for k in range(4)], "Geography": ["N"] * 4,
                       "Spend": [1.0, 2.0, 3.0, 4.0], "spend__2": [5.0, 6.0, 7.0, 8.0]})
    df.write_parquet(tmp_path / "data.parquet")
    cfg = client_config(tmp_path / "c.xlsx", [("Spend", "Spend")])
    run_c(str(tmp_path / "data.parquet"), str(tmp_path / "run"), str(cfg))
    (f,) = by_code(tmp_path / "run")["N-15"]
    assert f["detail"]["Count"] == 2 and "columns C, D" in f["reason"]


# ── review loop ──────────────────────────────────────────────────────────────
def _planted_run(tmp_path):
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    run_c(str(data), str(tmp_path / "run"), str(wbk.build_config(tmp_path / "config.xlsx")))
    return unit(tmp_path / "run") / "Data_Validation_Report.xlsx"


def _set_review(ws, predicate) -> list[str]:
    header = [c.value for c in ws[1]]
    marked = []
    for row in ws.iter_rows(min_row=2):
        values = dict(zip(header, (c.value for c in row)))
        if predicate(values):
            row[header.index("Review")].value = VALID
            marked.append(values["Finding_ID"])
    return marked


def test_marked_valid_is_not_reported_again(tmp_path):
    report = _planted_run(tmp_path)
    wb = openpyxl.load_workbook(report)
    marked = _set_review(wb["WOW_Duplicates"], lambda r: r["Comparison Type"] == "Sequential")
    marked += _set_review(wb["Date_Integrity"], lambda r: r["Code"] == "N-11")
    wb.save(tmp_path / "edited.xlsx")
    res = apply_upload(tmp_path / "run", tmp_path / "edited.xlsx", load_settings())
    found = by_code(tmp_path / "run")
    assert len(found["N-08"]) == 1 and "N-11" not in found
    assert res["counts"]["N-08"] == {"open": 1, "valid": 1} and res["dataset_checks"]["N-11"] == "PASS"
    assert set(json.loads((unit(tmp_path / "run") / "accepted.json").read_text("utf8"))) == set(marked)
    run_c(str(tmp_path / "weekly.xlsx"), str(tmp_path / "run"), str(tmp_path / "config.xlsx"))
    assert len(by_code(tmp_path / "run")["N-08"]) == 1                # a later run still honours the marks
    new = openpyxl.load_workbook(unit(tmp_path / "run") / "Data_Validation_Report.xlsx")
    assert [r[2] for r in new["Overall_Summary"].iter_rows(min_row=2, max_row=3, values_only=True)] == ["FAIL", "PASS"]


def test_data_corrections_are_rechecked(tmp_path):
    report = _planted_run(tmp_path)
    wb = openpyxl.load_workbook(report)
    ws = wb["Data"]
    header = [c.value for c in ws[1]]
    clicks = header.index("Digital_Clicks")
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=2)]
    dup = next(n for n, r in enumerate(rows) if r[header.index("TV_Spend")] == 999.99)
    del rows[dup]                                                     # N-11: drop the duplicate date row
    rows.append([None, datetime.combine(week(22), datetime.min.time()), "National",   # N-12 / N-14: the missing
                 1.11, 2.22, 3.33, 3.33, 99.9, 4.44, 5.55, 6.66, 7.77])             # week, Row_ID left blank
    rows.sort(key=lambda r: r[1])                                     # N-13: sort by Period
    for r in rows:                                                    # N-01/N-03/N-05: fix the text values
        r[clicks] = {"abc": 1234.5, "NULL": 2345.5, "1,250": 1250.0}.get(r[clicks], r[clicks])
    for i, r in enumerate(rows, start=2):                             # same row count: overwrite in place
        for j, v in enumerate(r, start=1):
            ws.cell(i, j).value = v
    wb.save(tmp_path / "edited.xlsx")
    apply_upload(tmp_path / "run", tmp_path / "edited.xlsx", load_settings())
    found = by_code(tmp_path / "run")
    assert not {"N-11", "N-12", "N-13", "N-14", "N-01", "N-03", "N-05"} & set(found)
    assert {"N-07", "N-08", "N-10", "N-15"} <= set(found)                    # untouched issues stay
    audit = json.loads((unit(tmp_path / "run") / "review_audit.json").read_text("utf8"))
    actions = {a["action"] for a in audit}
    assert actions == {"row_added", "row_deleted", "rows_reordered", "edited"}
    added = next(a for a in audit if a["action"] == "row_added")
    assert added["row_id"].endswith(":c1") and added["values"]["Period"] == week(22).isoformat()
    df = pl.read_parquet(next((unit(tmp_path / "run")).glob("tables/*/data.parquet")))
    assert df.height == 60 and df["Period"].dtype == pl.Date and df["Period"].is_sorted()
    assert len(list((unit(tmp_path / "run") / "review_versions").glob("*.xlsx"))) == 3


def test_sorting_rows_keeps_finding_ids(tmp_path):
    report = _planted_run(tmp_path)
    before = {f["finding_id"] for f in json.loads((unit(tmp_path / "run") / "findings.json").read_text("utf8"))
              if f["code"] != "N-13"}
    wb = openpyxl.load_workbook(report)
    ws = wb["Data"]
    rows = sorted(([c.value for c in r] for r in ws.iter_rows(min_row=2)), key=lambda r: r[1], reverse=True)
    for i, r in enumerate(rows, start=2):
        for j, v in enumerate(r, start=1):
            ws.cell(i, j).value = v
    wb.save(tmp_path / "edited.xlsx")
    apply_upload(tmp_path / "run", tmp_path / "edited.xlsx", load_settings())
    after = json.loads((unit(tmp_path / "run") / "findings.json").read_text("utf8"))
    assert before <= {f["finding_id"] for f in after}                 # only N-13 (the order itself) changes


def test_untouched_upload_changes_nothing(tmp_path):
    report = _planted_run(tmp_path)
    before = json.loads((unit(tmp_path / "run") / "findings.json").read_text("utf8"))
    apply_upload(tmp_path / "run", report, load_settings())
    assert json.loads((unit(tmp_path / "run") / "findings.json").read_text("utf8")) == before
    assert json.loads((unit(tmp_path / "run") / "review_audit.json").read_text("utf8")) == []


def test_capped_sheets_say_so_and_still_upload(tmp_path):
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    cfg = wbk.build_config(tmp_path / "config.xlsx")
    run_c(str(data), str(tmp_path / "run"), str(cfg), overrides={"max_sheet_rows": 1})
    report = unit(tmp_path / "run") / "Data_Validation_Report.xlsx"
    wb = openpyxl.load_workbook(report)
    assert wb["WOW_Duplicates"].max_row == 2                          # 1 of 2 findings listed
    notes = [r[3] for r in wb["Overall_Summary"].iter_rows(values_only=True) if r[1] == "Rows Not Shown"]
    assert any(n.startswith("WOW_Duplicates: 1 more") for n in notes)
    _set_review(wb["WOW_Duplicates"], lambda r: True)
    wb.save(tmp_path / "edited.xlsx")
    res = apply_upload(tmp_path / "run", tmp_path / "edited.xlsx", load_settings())
    assert res["counts"]["N-08"] == {"open": 1, "valid": 1}


@pytest.mark.parametrize("edit, message", [
    (lambda wb: wb["Data"].cell(3, 1, "nope:1"), "unknown Row_ID"),
    (lambda wb: wb["Data"].cell(3, 1, wb["Data"].cell(2, 1).value), "appear more than once"),
    (lambda wb: wb["Data"].cell(1, 3, "Region"), "Data: column headers changed"),
    (lambda wb: wb["WOW_Duplicates"].cell(2, len(COLUMNS["WOW_Duplicates"]) + 2, "looks fine"), "Review must be blank"),
    (lambda wb: wb["WOW_Duplicates"].delete_rows(2), "rows were added or removed"),
    (lambda wb: wb.remove(wb["Check_Index"]), "sheets must be exactly"),
])
def test_bad_uploads_are_rejected(tmp_path, edit, message):
    report = _planted_run(tmp_path)
    wb = openpyxl.load_workbook(report)
    edit(wb)
    wb.save(tmp_path / "edited.xlsx")
    with pytest.raises(ReviewError, match=message):
        apply_upload(tmp_path / "run", tmp_path / "edited.xlsx", load_settings())


def test_init_config_is_prefilled_editable_and_runs(tmp_path, capsys):
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    out = tmp_path / "gen.xlsx"
    assert cli(["init-config", str(data), "-o", str(out)]) == 0
    info = json.loads(capsys.readouterr().out)
    assert (info["period"], info["geography"], info["first_variable"]) == ("Period", "Geography", "TV_Spend")
    wb = openpyxl.load_workbook(out)
    assert not any(ws.protection.sheet for ws in wb.worksheets)                    # nothing locked
    df = {r[0].value: r for r in wb["Data File"].iter_rows(min_row=2)}
    assert [df[k][1].value for k in ("Period", "Geography", "Variables")] == ["Period", "Geography", "TV_Spend"]
    vm = {r[0]: r for r in wb["Variable Mapping"].iter_rows(min_row=2, values_only=True)}
    assert vm["TV_Impressions"][3] == "TV_Spend" and vm["TV_Impressions"][4] == "Y"      # paired by name
    assert vm["TV_Spend"][2] == "Tv" and "TV_Spend__2" not in vm                          # repeated header once
    spec = {r[0]: r for r in wb["Data Check Specifications"].iter_rows(min_row=2, values_only=True)}
    assert spec["No Executions"][4] == 9 and spec["MOM % change"][1] is None            # thresholds left to the user
    assert len(list(wb["Period Mapping"].iter_rows(min_row=2))) == 59 and info["cells_for_you"] >= 8
    res = run_c(str(data), str(tmp_path / "run"), str(out))                  # runs without any edit
    assert res["variables"]["checked"] >= 8 and {"N-11", "N-12", "N-13", "N-14", "N-15"} <= set(by_code(tmp_path / "run"))
    # the analyst changes a wrong guess: Print_Spend gets a blank bucket, so it is no longer checked
    ws = wb["Variable Mapping"]
    for r in ws.iter_rows(min_row=2):
        if r[0].value == "Print_Spend":
            r[2].value = "N/A"
    wb.save(out)
    assert run_c(str(data), str(tmp_path / "run2"), str(out))["variables"]["checked"] == res["variables"]["checked"] - 1


def test_cli_exit_codes(tmp_path, capsys):
    data = wbk.build_data(tmp_path / "weekly.xlsx")
    cfg = wbk.build_config(tmp_path / "config.xlsx")
    assert cli(["run", str(data), "-o", str(tmp_path / "run"), "--client-config", str(cfg)]) == 0
    assert json.loads(capsys.readouterr().out)["tables"][0]["totals"]["findings_open"] == len(wbk.EXPECTED)
    wb = openpyxl.load_workbook(unit(tmp_path / "run") / "Data_Validation_Report.xlsx")
    wb["Data"].cell(2, 1, "bad")
    wb.save(tmp_path / "bad.xlsx")
    assert cli(["review", str(tmp_path / "run"), str(tmp_path / "bad.xlsx")]) == 2
    assert cli(["run", str(data), "-o", str(tmp_path / "r2"), "--client-config", str(data)]) == 1   # not a config


# ── dates before 1900 (Windows strftime("%y") raised "format %y requires year >= 1900") ─────────────────────────────
def test_month_label_works_for_any_year():
    from datetime import date
    from src.common.values import month_label
    assert month_label(date(2022, 2, 6)) == "February '22"
    assert month_label(date(1899, 12, 31)) == "December '99"          # strftime("%y") raises for this on Windows
    assert month_label(date(1, 1, 1)) == "January '01"


def test_period_values_outside_1900_2100_are_not_dates():
    from datetime import date, datetime
    from src.common.values import Periods
    p = Periods([], False)
    for bad in (date(1899, 12, 30), datetime(1899, 12, 31, 0, 0), date(1, 1, 1), date(2101, 1, 1), 1, 2, 52, "1", 18263):
        assert p(bad) is None, bad                                    # week numbers and Excel's zero date are not periods
    assert p(date(2022, 2, 6)) == date(2022, 2, 6)
    assert Periods([], False)("06-02-2022") == date(2022, 2, 6)         # dd-mm-yyyy with dashes is always day first
    assert Periods([], True)("06/02/2022") == date(2022, 2, 6) and Periods([], False)("06/02/2022") == date(2022, 6, 2)
    assert p(44598) == date(2022, 2, 6) and p("44598") == date(2022, 2, 6)


def test_phase_b_does_not_turn_week_numbers_into_dates(tmp_path):
    """A column of 1, 2, 3 ... named 'Week' used to become 1899-12-31, 1900-01-01 ... (D-25), which Phase C then crashed on."""
    import openpyxl
    from datetime import date, timedelta
    from src.hygiene.pipeline import run_hygiene
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Week", "Date", "TV_Spend"])
    for i in range(30):
        ws.append([i + 1, (date(2022, 1, 3) + timedelta(weeks=i)).strftime("%d-%m-%Y"), 1000 + i])
    wb.save(tmp_path / "w.xlsx")
    run_hygiene(str(tmp_path / "w.xlsx"), str(tmp_path / "b"))
    audit = json.loads((tmp_path / "b" / "phase_b_hygiene" / "remediation_audit.json").read_text("utf8"))
    assert not [a for a in audit if a["column"] == "Week"]            # untouched
    assert {a["rule"] for a in audit if a["column"] == "Date"} == set()  # dd-mm-yyyy is a single consistent format


def test_phase_c_runs_on_dd_mm_yyyy_text_with_a_week_number_column(tmp_path):
    """The reported crash: dates written 06-02-2022 plus a counter column. Must run, and the Period must be the date column."""
    import openpyxl
    from datetime import date, timedelta
    from src.hygiene.pipeline import run_hygiene
    from src.timeseries.autoconfig import init_config
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Week", "Date", "Geography", "TV_Spend", "TV_Impressions"])
    for i in range(30):
        ws.append([i + 1, (date(2022, 1, 3) + timedelta(weeks=i)).strftime("%d-%m-%Y"), "US", 1000 + i * 7, 50000 + i * 91])
    wb.save(tmp_path / "w.xlsx")
    run_hygiene(str(tmp_path / "w.xlsx"), str(tmp_path / "b"))
    init_config(str(tmp_path / "b"), str(tmp_path / "cfg.xlsx"))
    run = run_c(str(tmp_path / "b"), str(tmp_path / "b"), str(tmp_path / "cfg.xlsx"))
    assert run["layout"]["period"] == "Date" and run["periods"]["first"] == "2022-01-03"
    assert run["dataset_checks"]["N-14"] == "PASS" and run["totals"]["findings_open"] == 0


def test_a_stray_1899_date_cell_in_the_period_column_does_not_crash_phase_c(tmp_path):
    """A real date cell holding Excel's zero date (1899-12-30) among 2022 dates: skipped as 'not a date', never formatted."""
    import openpyxl
    from datetime import date, timedelta
    from src.timeseries.autoconfig import init_config
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Date", "Geography", "TV_Spend"])
    for i in range(30):
        ws.append([date(2022, 1, 3) + timedelta(weeks=i), "US", 1000 + i])
    ws.append([date(1899, 12, 30), "US", 5])
    wb.save(tmp_path / "w.xlsx")
    init_config(str(tmp_path / "w.xlsx"), str(tmp_path / "cfg.xlsx"))
    run = run_c(str(tmp_path / "w.xlsx"), str(tmp_path / "run"), str(tmp_path / "cfg.xlsx"))
    assert run["periods"]["first"] == "2022-01-03"
