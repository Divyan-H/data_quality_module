"""Monthly tables become Monday-to-Sunday weekly tables before Phase C (src/common/weekly_rollup.py)."""
from datetime import date, timedelta

import pytest

from src.common.dataset import Dataset
from src.common.weekly_rollup import AVERAGE, SUM, guess, to_weekly
from src.timeseries.config import load_settings
from src.timeseries.io import load_candidates

WORDS = load_settings()["average_words"]


def table(rows, cols=("City", "Year-Month", "Sales", "AOV")):
    data = {"_row_id": [f"t:0:t1:r{i}" for i in range(len(rows))], "_source_row": list(range(2, len(rows) + 2))}
    for j, c in enumerate(cols):
        data[c] = [r[j] for r in rows]
    return Dataset("t:0:t1", data, list(cols), "f.csv", "Sheet1")


def weeks(ds):
    return {d: i for i, d in enumerate(ds.data["Period"])}


def test_a_month_starting_on_a_saturday_puts_two_days_in_its_first_week():
    ds = table([("Pune", "2024-06", 3000, 10.0), ("Pune", "2024-07", 3100, 20.0)])      # 1 June 2024 is a Saturday
    weekly, log = to_weekly(ds, False, None, WORDS)
    w = weeks(weekly)
    assert weekly.data["Period"][0] == date(2024, 5, 27)
    assert weekly.data["Sales"][w[date(2024, 5, 27)]] == pytest.approx(3000 * 2 / 30)    # Sat and Sun only
    assert weekly.data["Sales"][w[date(2024, 6, 3)]] == pytest.approx(3000 * 7 / 30)
    june = sum(weekly.data["Sales"][w[d]] for d in w if date(2024, 5, 27) <= d <= date(2024, 6, 24))
    assert june == pytest.approx(3000)                                                   # the weeks add back up to the month
    assert log["monthly_rows"] == 2 and log["rollup"] == {"Sales": SUM, "AOV": AVERAGE}


def test_a_week_across_a_month_boundary_takes_a_share_from_each_month():
    ds = table([("Pune", "2024-01", 3100, 10.0), ("Pune", "2024-02", 5800, 20.0)])      # 100 and 200 a day
    weekly, _ = to_weekly(ds, False, None, WORDS)
    i = weeks(weekly)[date(2024, 1, 29)]                                                  # 29-31 Jan and 1-4 Feb
    assert weekly.data["Sales"][i] == pytest.approx(3 * 100 + 4 * 200)                    # a sum: split by days, then added
    assert weekly.data["AOV"][i] == pytest.approx((3 * 10 + 4 * 20) / 7)                  # a rate: day-weighted mean, never divided
    assert sum(weekly.data["Sales"]) == pytest.approx(3100 + 5800)


def test_a_rate_is_not_split_and_the_analyst_can_override_the_guess():
    ds = table([("Pune", "2024-06", 3000, 10.0)])
    weekly, _ = to_weekly(ds, False, None, WORDS)
    assert set(weekly.data["AOV"]) == {10.0}                                              # the same in every week
    weekly, log = to_weekly(ds, False, {"aov": "sum", "Sales": "average"}, WORDS)        # case does not matter
    assert log["rollup"] == {"Sales": AVERAGE, "AOV": SUM}
    assert sum(weekly.data["AOV"]) == pytest.approx(10.0)
    assert set(weekly.data["Sales"]) == {3000.0}


def test_name_guesses_come_from_the_settings_list():
    assert guess("AOV", WORDS) == AVERAGE and guess("Margin %", WORDS) == AVERAGE and guess("Avg_Temp", WORDS) == AVERAGE
    assert guess("Orders", WORDS) == SUM and guess("Spend", WORDS) == SUM
    assert guess("Orders", ["order"]) == AVERAGE                                          # the list is configuration


def test_series_stay_separate_and_the_month_columns_are_replaced_by_week_start():
    ds = table([("Pune", "2024-06", 3000, 1.0), ("Delhi", "2024-06", 600, 2.0)])
    weekly, _ = to_weekly(ds, False, None, WORDS)
    assert weekly.columns == ["Period", "City", "Sales", "AOV"]                          # the derived date first, as for Phase D
    assert sorted(set(weekly.data["City"])) == ["Delhi", "Pune"]
    assert all(r.startswith("t:0:t1:w") for r in weekly.row_ids) and weekly.derived == "monthly"
    by = {c: sum(s for k, s in zip(weekly.data["City"], weekly.data["Sales"]) if k == c) for c in ("Pune", "Delhi")}
    assert by == {"Pune": pytest.approx(3000), "Delhi": pytest.approx(600)}


def test_the_same_month_twice_is_not_added_together():
    ds = table([("Pune", "2024-06", 3000, 1.0), ("Pune", "2024-06", 3000, 1.0)])
    weekly, _ = to_weekly(ds, False, None, WORDS)
    starts = weekly.data["Period"]
    assert len(starts) == 2 * len(set(starts))                                            # a duplicate date for Phase C to find
    assert sum(weekly.data["Sales"]) == pytest.approx(6000)


def test_a_real_monthly_date_column_works_the_same():
    months = [date(2024, m, 1) for m in (6, 7, 8, 9)]                                    # a cadence needs 3 or more dates
    ds = table([("Pune", d, 3000, 1.0) for d in months], cols=("City", "Period", "Sales", "AOV"))
    weekly, _ = to_weekly(ds, False, None, WORDS)
    assert weekly is not None and weekly.data["Period"][0] == date(2024, 5, 27)


@pytest.mark.parametrize("step", [1, 7])
def test_daily_and_weekly_tables_are_never_touched(step, tmp_path):
    start = date(2024, 1, 1)
    rows = [("Pune", start + timedelta(days=i * step), 10 + i, 1.0) for i in range(60)]
    ds = table(rows, cols=("City", "Date", "Sales", "AOV"))
    assert to_weekly(ds, False, None, WORDS) is None
    p = tmp_path / "t.csv"
    p.write_text("City,Date,Sales,AOV\n" + "\n".join(f"{c},{d},{s},{a}" for c, d, s, a in rows), encoding="utf8")
    got = list(load_candidates(p, None, load_settings()))
    assert len(got) == 1 and not got[0].derived and got[0].columns == ["City", "Date", "Sales", "AOV"] and len(got[0].row_ids) == 60


def test_the_roll_up_can_be_switched_off(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("City,Year-Month,Sales\n" + "\n".join(f"Pune,2024-{m:02d},{100 * m}" for m in range(1, 7)), encoding="utf8")
    on = list(load_candidates(p, None, load_settings()))[0]
    off = list(load_candidates(p, None, load_settings(overrides={"monthly_to_weekly": False})))[0]
    assert on.derived == "monthly" and "Year-Month" not in on.columns and "Period" in on.columns
    assert not off.derived and "Year-Month" in off.columns and len(off.row_ids) == 6


def test_a_table_with_no_measure_is_not_converted():
    ds = table([("Pune", "2024-06"), ("Pune", "2024-07")], cols=("City", "Year-Month"))
    assert to_weekly(ds, False, None, WORDS) is None
