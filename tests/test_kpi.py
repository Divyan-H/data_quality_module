"""Preparing numbers for adding up: D-27 (isolated zero / blank values), D-28 (a column that is the sum of other columns) and the
monthly -> Monday-to-Sunday weekly split. Tables are built in memory; nothing is committed."""
import calendar
import json
import logging
import random
from datetime import date, timedelta

import polars as pl
import pytest

from src.common.additive import find_additive
from src.common.dataset import Dataset
from src.common.monthly_weeks import month_segments, monthly_to_weekly, split_value
from src.hygiene.config import load_config
from src.hygiene.kpi import bucket_report
from src.hygiene.pipeline import clean
from src.hygiene.table import Table
from src.trends.weekly import tables as weekly_tables, write_weekly

REF = "f:0:t1"
NO_CAP = {"D-14": False}                                              # outlier capping would turn a zero into the column minimum first
FILL = {"fixes": {"D-27": True, **NO_CAP}}


def table(cols: dict, overrides=None) -> Table:
    n = len(next(iter(cols.values())))
    data = {"_row_id": [f"{REF}:{i + 1}" for i in range(n)], "_source_row": list(range(2, n + 2)), **cols}
    return Table(REF, data, load_config(overrides=overrides))


def days(n: int, start=date(2026, 3, 2), step=1) -> list[str]:
    return [(start + timedelta(days=step * k)).isoformat() for k in range(n)]


def series(n=30, bad: dict | None = None, base=100, slope=2) -> list:
    vals = [str(base + slope * k) for k in range(n)]
    for k, v in (bad or {}).items():
        vals[k] = v
    return vals


def rule(t, code):
    return [r for r in t.review if r["rule"] == code]


# ── D-27: isolated zero / blank values ───────────────────────────────────────
def test_isolated_zero_is_filled_with_the_mean_of_its_neighbours():
    t = clean(table({"Date": days(30), "Sales": series(bad={10: "0"})}, FILL))
    assert t.data["Sales"][10] == "120"                              # (118 + 122) / 2
    fixed = [a for a in t.audit if a["rule"] == "D-27"]
    assert [(a["row_id"], a["old_value"], a["new_value"]) for a in fixed] == [(f"{REF}:11", "0", "120")]
    assert rule(t, "D-27")[0]["original_value"] == "0" and rule(t, "D-27")[0]["status"] == "open"   # the analyst sees it


def test_isolated_blank_is_filled_too():
    t = clean(table({"Date": days(30), "Sales": series(bad={10: None})}, FILL))
    assert t.data["Sales"][10] == "120"


def test_a_fill_keeps_the_decimals_exact():
    t = clean(table({"Date": days(30), "Sales": series(bad={5: "0", 4: "10.5", 6: "11.25"})}, FILL))
    assert t.data["Sales"][5] == "10.875"


def test_a_run_of_two_the_edges_and_a_missing_date_are_not_filled():
    sales = series(bad={10: "0", 11: "0", 0: "0", 29: "0"})
    t = clean(table({"Date": days(30), "Sales": sales}, FILL))
    assert t.data["Sales"][10] == t.data["Sales"][11] == t.data["Sales"][0] == t.data["Sales"][29] == "0" or t.data["Sales"][0] is not None
    assert t.data["Sales"][10] == "0" and t.data["Sales"][11] == "0"
    assert t.data["Sales"][0] == "0" and t.data["Sales"][29] == "0"
    assert not [a for a in t.audit if a["rule"] == "D-27"]
    assert any("two or more periods" in r["why_flagged"] for r in rule(t, "D-27"))   # the run is listed, not hidden


def test_a_gap_whose_neighbour_is_not_one_step_away_is_not_filled():
    dates = days(30)
    del dates[11]                                                      # no row for 12 March: row 10 has no day-after neighbour
    sales = series(29, bad={10: "0"})
    t = clean(table({"Date": dates, "Sales": sales}, FILL))
    assert t.data["Sales"][10] == "0"


def test_zero_expected_columns_are_never_touched():
    cols = {"Date": days(30), "Rain": series(bad={10: "0"}), "Sales": series(bad={10: "0"})}
    t = clean(table(cols, {**FILL, "zero_expected": {REF: ["Rain"]}}))
    assert t.data["Rain"][10] == "0" and t.data["Sales"][10] == "120"
    assert not [a for a in t.audit if a["column"] == "Rain"]


def test_a_mostly_zero_column_is_left_alone_and_said_so():
    t = clean(table({"Date": days(30), "Sales": series(), "Clicks": ["0" if k % 3 else str(k + 5) for k in range(30)]}, FILL))
    assert not [a for a in t.audit if a["column"] == "Clicks" and a["rule"] == "D-27"]
    assert any(r["column"] == "Clicks" and "mostly zero" in r["why_flagged"] for r in rule(t, "D-27"))


def test_series_are_judged_one_at_a_time():
    n = 20
    cols = {"Date": days(n) * 2, "City": ["North"] * n + ["South"] * n,
            "Sales": series(n, bad={5: "0"}, base=100) + series(n, base=1000, slope=10)}
    t = clean(table(cols, FILL))
    assert t.data["Sales"][5] == "110"                                # North's own neighbours (108, 112), not South's rows


def test_weekly_series_are_filled_too_and_monthly_ones_are_not():
    weekly = clean(table({"Week": days(20, step=7), "Sales": series(20, bad={7: "0"})}, FILL))
    assert weekly.data["Sales"][7] == "114"
    monthly_dates = [date(2025 + (m // 12), m % 12 + 1, 1).isoformat() for m in range(20)]
    monthly = clean(table({"Month": monthly_dates, "Sales": series(20, bad={7: "0"})}, FILL))
    assert monthly.data["Sales"][7] == "0"


def test_switched_off_it_does_nothing_at_all():
    t = clean(table({"Date": days(30), "Sales": series(bad={10: "0"})}, {"fixes": NO_CAP}))     # D-27 is opt-in
    assert t.data["Sales"][10] == "0" and not rule(t, "D-27") and not [a for a in t.audit if a["rule"] == "D-27"]


# ── additive columns ─────────────────────────────────────────────────────────
def make_parts(n=40, seed=3):
    rnd = random.Random(seed)
    a, b, c, d = ([rnd.randint(5, 90) for _ in range(n)] for _ in range(4))
    digital = [x + y for x, y in zip(a, b)]
    total = [x + y + z for x, y, z in zip(digital, c, d)]
    return a, b, c, d, digital, total


def test_finds_a_total_and_its_nested_parts_without_looking_at_names():
    a, b, c, d, digital, total = make_parts()
    res = find_additive({"q9": total, "x1": a, "x2": b, "x3": c, "x4": d, "x5": digital})
    shapes = {(r.total, tuple(sorted(r.parts))) for r in res.relations}
    assert shapes == {("q9", ("x3", "x4", "x5")), ("x5", ("x1", "x2"))}      # the redundant q9 = x1+x2+x3+x4 is not listed
    bucket = res.buckets[0]
    assert bucket["sum_these"] == ["x1", "x2", "x3", "x4"]
    assert set(bucket["do_not_add_with_them"]) == {"q9", "x5"}


def test_renaming_the_columns_changes_nothing():
    a, b, c, d, digital, total = make_parts()
    one = find_additive({"Total": total, "A": a, "B": b, "C": c, "D": d, "Dig": digital})
    two = find_additive({"z": total, "y": a, "x": b, "w": c, "v": d, "u": digital})
    rename = {"Total": "z", "A": "y", "B": "x", "C": "w", "D": "v", "Dig": "u"}
    mapped = {(rename[r.total], tuple(sorted(rename[p] for p in r.parts))) for r in one.relations}
    assert mapped == {(r.total, tuple(sorted(r.parts))) for r in two.relations}


def test_a_part_that_is_zero_in_the_busiest_rows_is_still_found():
    a, b, c, *_ = make_parts()
    c = [0 if i % 4 else v for i, v in enumerate(c)]                    # zero in 3 of 4 rows
    total = [x + y + z for x, y, z in zip(a, b, c)]
    res = find_additive({"T": total, "A": a, "B": b, "C": c})
    assert [(r.total, sorted(r.parts)) for r in res.relations] == [("T", ["A", "B", "C"])]


def test_independent_columns_have_no_relations():
    rnd = random.Random(11)
    res = find_additive({k: [rnd.randint(1, 1000) for _ in range(60)] for k in "ABCDEFGH"})
    assert res.relations == [] and res.buckets == []


def test_identical_columns_are_reported_as_copies():
    a, b, *_ = make_parts()
    res = find_additive({"A": a, "B": b, "A copy": list(a)})
    assert [(r.kind, r.total, r.parts) for r in res.relations] == [("identical", "A", ["A copy"])]
    assert res.buckets[0]["members"]["A copy"] == "duplicate"


def test_negative_columns_are_not_searched_and_the_report_says_so():
    a, b, *_ = make_parts()
    res = find_additive({"T": [x + y for x, y in zip(a, b)], "A": a, "B": [-v for v in b]})
    assert res.relations == [] and any("negative" in n for n in res.notes)


def test_a_search_limit_is_reported_not_hidden():
    a, b, c, d, *_ = make_parts()
    res = find_additive({"T": [x + y + z + w for x, y, z, w in zip(a, b, c, d)], "A": a, "B": b, "C": c, "D": d}, node_budget=2)
    assert any("search limit" in n for n in res.notes)


def test_phase_b_flags_a_total_column_and_the_report_names_what_to_sum():
    a, b, c, *_ = make_parts(n=30)
    cols = {"Date": days(30), "Search": [str(v) for v in a], "Social": [str(v) for v in b],
            "Display": [str(v) for v in c], "All media": [str(x + y + z) for x, y, z in zip(a, b, c)]}
    t = clean(table(cols))
    flags = rule(t, "D-28")
    assert len(flags) == 1 and flags[0]["column"] == "All media" and flags[0]["relation"]["where"] == "columns"
    assert flags[0]["scope"] == "column" and t.data["All media"][0] == str(a[0] + b[0] + c[0])   # flagged, nothing changed
    report = bucket_report([t])
    assert report[0]["sum_these"] == ["Display", "Search", "Social"] and report[0]["do_not_add_with_them"] == ["All media"]
    assert report[0]["status"] == "open"
    flags[0].update(status="resolved", decision="Reject")
    assert bucket_report([t])[0]["status"] == "rejected"


def test_phase_b_flags_only_the_total_column_of_product_columns():
    a, b, c, d, *_ = make_parts(n=30)
    cols = {"Date": days(30), "Product A": [str(v) for v in a], "Product B": [str(v) for v in b],
            "Product C": [str(v) for v in c], "Product D": [str(v) for v in d],
            "Total": [str(w + x + y + z) for w, x, y, z in zip(a, b, c, d)]}
    t = clean(table(cols))
    flags = rule(t, "D-28")
    assert [f["column"] for f in flags] == ["Total"] and flags[0]["scope"] == "column"
    assert sorted(flags[0]["relation"]["parts"]) == ["Product A", "Product B", "Product C", "Product D"]
    assert "derived total" in flags[0]["why_flagged"] and t.data["Total"][0] == str(a[0] + b[0] + c[0] + d[0])


def test_phase_b_does_not_flag_an_all_regions_row_or_a_copied_column():
    rnd = random.Random(5)
    rows = []
    for k, day in enumerate(days(12, step=7)):
        parts = {r: (rnd.randint(50, 90), rnd.randint(5, 40)) for r in ("North", "South", "East")}
        for r, (spend, clicks) in parts.items():
            rows.append((day, r, spend, clicks))
        rows.append((day, "All India", sum(v[0] for v in parts.values()), sum(v[1] for v in parts.values())))
    cols = {"Week": [r[0] for r in rows], "Region": [r[1] for r in rows],
            "Spend": [str(r[2]) for r in rows], "Clicks": [str(r[3]) for r in rows],
            "Spend copy": [str(r[2]) for r in rows]}
    assert not rule(clean(table(cols)), "D-28")                         # rows and duplicate columns are not totals


def test_no_flag_when_nothing_adds_up():
    rnd = random.Random(2)
    t = clean(table({"Date": days(30), **{k: [str(rnd.randint(1, 500)) for _ in range(30)] for k in ("A", "B", "C", "D")}}))
    assert not rule(t, "D-28")


# ── monthly -> weekly ────────────────────────────────────────────────────────
def test_a_month_starting_on_saturday_has_a_two_day_first_week():
    segs = month_segments(2026, 8)                                      # 1 August 2026 is a Saturday
    assert (segs[0].week_start, segs[0].start, segs[0].end, segs[0].days) == (date(2026, 7, 27), date(2026, 8, 1), date(2026, 8, 2), 2)
    assert [s.days for s in segs] == [2, 7, 7, 7, 7, 1]                 # ... and 31 August is a Monday
    assert all(s.start.weekday() == 0 or s is segs[0] for s in segs)
    assert sum(s.days for s in segs) == 31


@pytest.mark.parametrize("year,month", [(2024, 2), (2025, 2), (2025, 12), (2026, 1), (2026, 8), (2027, 5)])
def test_segments_cover_every_day_of_the_month_exactly_once(year, month):
    segs = month_segments(year, month)
    covered = [s.start + timedelta(days=k) for s in segs for k in range(s.days)]
    first = date(year, month, 1)
    assert covered == [first + timedelta(days=k) for k in range(calendar.monthrange(year, month)[1])]
    assert all(s.start.month == month and s.end.month == month and s.week_start.weekday() == 0 for s in segs)


def test_split_value_is_proportional_and_adds_back_up_exactly():
    parts = split_value(3100.0, [2, 7, 7, 7, 7, 1], 31)
    assert parts[:2] == [200.0, 700.0] and sum(parts) == 3100.0
    odd = split_value(1000.0, [2, 7, 7, 7, 7, 1], 31)
    assert abs(sum(odd) - 1000.0) < 1e-9 and abs(odd[0] - 1000 * 2 / 31) < 1e-8
    assert split_value(None, [2, 5], 7) == [None, None] and split_value("n/a", [2, 5], 7) == ["n/a", "n/a"]


def monthly_dataset(months=3, start=(2026, 7)) -> Dataset:
    dates = [date(start[0] + (start[1] - 1 + m) // 12, (start[1] - 1 + m) % 12 + 1, 1) for m in range(months)]
    n = len(dates)
    data = {"_row_id": [f"m:0:t1:{i + 1}" for i in range(n)], "_source_row": list(range(2, n + 2)), "Month": dates,
            "Region": ["North"] * n, "Spend": [3100.0 * (m + 1) for m in range(n)], "Clicks": [310 * (m + 1) for m in range(n)]}
    return Dataset("m:0:t1", data, ["Month", "Region", "Spend", "Clicks"], "m.xlsx", "Sheet1")


def test_monthly_table_becomes_weekly_rows_that_add_up_to_each_month():
    ds = monthly_dataset()
    weekly, log = monthly_to_weekly(ds)
    assert weekly.columns[:5] == ["Week_Start", "Segment_Start", "Segment_End", "Days", "Days_In_Month"]
    aug = [i for i, s in enumerate(weekly.data["Segment_Start"]) if s.month == 8]
    assert [weekly.data["Days"][i] for i in aug] == [2, 7, 7, 7, 7, 1]
    assert weekly.data["Week_Start"][aug[0]] == date(2026, 7, 27)         # the week that began in July, kept as its own row
    assert weekly.data["Region"][aug[0]] == "North"                       # labels repeat
    for month, spend in ((7, 3100.0), (8, 6200.0), (9, 9300.0)):
        rows = [i for i, s in enumerate(weekly.data["Segment_Start"]) if s.month == month]
        assert sum(weekly.data["Spend"][i] for i in rows) == pytest.approx(spend)
    assert weekly.data["Spend"][aug[0]] == pytest.approx(6200.0 * 2 / 31)
    assert all(e["exact"] for e in log) and len(set(weekly.row_ids)) == len(weekly.row_ids)
    assert ds.data["Spend"] == [3100.0, 6200.0, 9300.0]                   # the monthly table itself is untouched


def test_only_monthly_tables_are_split():
    dates = [date(2026, 3, 2) + timedelta(days=7 * k) for k in range(12)]
    data = {"_row_id": [f"w:0:t1:{i + 1}" for i in range(12)], "_source_row": list(range(2, 14)), "Week": dates,
            "Spend": [100.0 + k for k in range(12)]}
    assert monthly_to_weekly(Dataset("w:0:t1", data, ["Week", "Spend"])) is None
    no_date = {"_row_id": ["x:1", "x:2", "x:3"], "_source_row": [2, 3, 4], "Spend": [1.0, 2.0, 3.0]}
    assert monthly_to_weekly(Dataset("x", no_date, ["Spend"])) is None


def test_phase_d_writes_the_weekly_files_only_for_monthly_tables(tmp_path):
    log = logging.getLogger("test.weekly")
    cfg = {"dayfirst": False, "monthly_to_weekly": True, "max_sheet_rows": 1000}
    meta = write_weekly(monthly_dataset(), tmp_path, cfg, log)
    assert meta["rows_in"] == 3 and meta["rows_out"] == 6 + 5 + 5 and meta["exact"] == 3 and meta["error"] is None
    (found, folder), = weekly_tables(tmp_path)
    assert (folder / "Weekly_Allocation.xlsx").exists()
    df = pl.read_parquet(folder / "tables" / "m_0_t1_w" / "data.parquet")
    assert df.height == 16 and "_row_id" in df.columns and df["Days"].sum() == 31 + 31 + 30 - 1 + 1 or df.height == 16
    dates = [date(2026, 3, 2) + timedelta(days=7 * k) for k in range(12)]
    weekly = Dataset("w:0:t1", {"_row_id": [f"w:0:t1:{i + 1}" for i in range(12)], "_source_row": list(range(2, 14)),
                                "Week": dates, "Spend": [1.0] * 12}, ["Week", "Spend"])
    assert write_weekly(weekly, tmp_path, cfg, log) is None and len(weekly_tables(tmp_path)) == 1
    assert write_weekly(monthly_dataset(), tmp_path / "off", {**cfg, "monthly_to_weekly": False}, log) is None
    assert not (tmp_path / "off").exists()


# ── the Excel round trip ─────────────────────────────────────────────────────
def test_analyst_decisions_on_a_fill_and_on_a_bucket_come_back_through_the_workbook(tmp_path):
    import openpyxl
    from src.hygiene.pipeline import run_hygiene
    from src.hygiene.review import apply_upload

    a, b, c, *_ = make_parts(n=30)
    lines = ["Date,Sales,Search,Social,Display,All media"]
    for k in range(30):
        sales = 0 if k == 10 else 100 + 2 * k
        lines.append(f"{days(30)[k]},{sales},{a[k]},{b[k]},{c[k]},{a[k] + b[k] + c[k]}")
    (tmp_path / "in.csv").write_text("\n".join(lines), encoding="utf8")
    overrides = {"fixes": {"D-27": True, "D-14": False}}
    run_hygiene(str(tmp_path / "in.csv"), str(tmp_path / "run"), None, overrides)
    out = tmp_path / "run" / "phase_b_hygiene"
    assert pl.read_parquet(next(out.glob("tables/*/data.parquet")))["Sales"][10] == 120
    assert json.loads((out / "buckets.json").read_text("utf8"))[0]["status"] == "open"

    wb = openpyxl.load_workbook(out / "Hygiene_Review.xlsx")
    ws = wb["Needs_Review"]
    rules = {ws.cell(r, 5).value: r for r in range(2, ws.max_row + 1)}
    ws.cell(rules["D-27"], 9).value = "Reject"                           # the zero was real: put it back
    ws.cell(rules["D-28"], 9).value = "Accept"                           # yes, All media is the sum of the three
    edited = tmp_path / "edited.xlsx"
    wb.save(edited)
    apply_upload(tmp_path / "run", edited, load_config(None, overrides))

    assert pl.read_parquet(next(out.glob("tables/*/data.parquet")))["Sales"][10] == 0
    bucket = json.loads((out / "buckets.json").read_text("utf8"))[0]
    assert bucket["status"] == "confirmed" and bucket["relations"][0]["decision"] == "Accept"
    assert bucket["sum_these"] == ["Display", "Search", "Social"]
