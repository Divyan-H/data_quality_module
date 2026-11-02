"""What time level a table is at (src/common/granularity.py): weekly data is checked, anything else is named and can be skipped."""
from datetime import date, timedelta

import pytest

from src.common.dataset import Dataset
from src.common.granularity import assess, find_period_column


def table(**cols) -> Dataset:
    n = len(next(iter(cols.values())))
    data = {"_row_id": [f"t:{i}" for i in range(1, n + 1)], "_source_row": list(range(2, n + 2)), **cols}
    return Dataset("t:0:t1", data, list(cols), "f.xlsx", "Sheet1")


def months(n, start=(2022, 1)):
    y, m = start
    out = []
    for _ in range(n):
        out.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def test_weekly_dates_with_a_measure_are_applicable_to_phase_c_and_d():
    a = assess(table(Date=[date(2024, 1, 1) + timedelta(weeks=i) for i in range(30)], Spend=list(range(30))))
    assert a["level"] == "weekly" and a["period"] == "Date" and a["measures"] == 1
    assert a["applicable_c"] and a["applicable_d"] and "is weekly" in a["message"]


def test_weekly_data_with_some_missing_weeks_is_still_weekly():
    weeks = [date(2024, 1, 1) + timedelta(weeks=i) for i in range(30) if i not in (5, 6, 20)]
    assert assess(table(Date=weeks, Spend=list(range(len(weeks)))))["level"] == "weekly"


@pytest.mark.parametrize("dates,level", [
    ([date(2024, 1, 1) + timedelta(days=i) for i in range(40)], "daily"),
    (months(24), "monthly"),
    ([date(2022, 1, 1), date(2022, 4, 1), date(2022, 7, 1), date(2022, 10, 1), date(2023, 1, 1), date(2023, 4, 1)], "quarterly"),
    ([date(2018 + i, 1, 1) for i in range(8)], "yearly"),
])
def test_other_levels_are_named_but_never_blocked(dates, level):
    """A date column and a measure is all Phases C and D need: daily, monthly ... data runs as it always did."""
    a = assess(table(Date=dates, Sales=list(range(len(dates)))))
    assert a["level"] == level and a["applicable_c"] is True and a["applicable_d"] is True
    assert f"is {level}, not weekly" in a["message"]                   # said, not enforced


def test_your_table_year_month_labels_and_no_measures():
    """Competitor, Segment, City, Year, Month, Year-Month and nothing numeric to measure."""
    n = 36
    a = assess(table(Competitor=["A", "B", "C"] * 12, Segment=["x", "y"] * 18, City=["Pune", "Delhi", "Mumbai"] * 12,
                     Year=[2022 + i // 12 for i in range(n)], Month=[i % 12 + 1 for i in range(n)],
                     **{"Year-Month": [f"{2022 + i // 12}-{i % 12 + 1:02d}" for i in range(n)]}))
    assert a["level"] == "monthly" and a["period"] == "Year-Month" and a["measures"] == 0
    assert a["applicable_c"] is False and a["applicable_d"] is False
    assert "monthly, not weekly" in a["message"] and "no real date column" in a["message"] and "no numeric measure" in a["message"]


def test_year_and_month_columns_without_a_combined_label_are_monthly():
    n = 24
    a = assess(table(Year=[2022 + i // 12 for i in range(n)], Month=["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                                                                  "Sep", "Oct", "Nov", "Dec"] * 2, Orders=list(range(n))))
    assert a["level"] == "monthly" and a["period"] == "Year + Month" and a["measures"] == 1
    assert a["applicable_c"] is False and a["applicable_d"] is False   # no real date column yet


@pytest.mark.parametrize("label", ["Apr-22", "April 2022", "2022/04", "04/2022"])
def test_month_labels_in_text_are_monthly(label):
    a = assess(table(Period=[label] * 6, Sales=list(range(6))))
    assert a["level"] == "monthly" and a["period"] == "Period"


def test_a_year_column_alone_is_yearly():
    a = assess(table(Year=[2019, 2020, 2021, 2022] * 3, Brand=["a", "b", "c"] * 4, Sales=list(range(12))))
    assert a["level"] == "yearly" and a["period"] == "Year" and a["measures"] == 1


def test_no_time_column_at_all():
    a = assess(table(Brand=["a", "b", "c"], Sales=[1, 2, 3]))
    assert a["level"] == "none" and a["period"] is None and not a["applicable_c"] and not a["applicable_d"]
    assert "No date or month/year column" in a["message"]


def test_identifiers_and_small_counters_are_not_dates_or_measures():
    """The region-id table behind the strftime crash: ids 1..40 are not dates, the real date column is found."""
    n = 60
    ds = table(GEO_REGION_ID=[str(i % 40 + 1) for i in range(n)], CITY=["Pune"] * n,
               REG_DATE_FORMATED=[(date(2022, 4, 1) + timedelta(days=i)).isoformat() for i in range(n)],
               **{"COUNT(CUSTOMERS.ID)": [1] * n})
    assert find_period_column(ds) == "REG_DATE_FORMATED"
    a = assess(ds)
    assert a["level"] == "daily" and a["period"] == "REG_DATE_FORMATED"
    assert a["measures"] == 1                                            # COUNT(CUSTOMERS.ID) is a measure; GEO_REGION_ID is not


def test_week_counter_column_is_not_a_period():
    ds = table(Week=list(range(1, 31)), Spend=list(range(30)))
    assert find_period_column(ds) is None                                # 1..30 are not Excel dates (serials start at 1950)
    a = assess(ds)
    assert a["level"] == "none" and not a["applicable_c"] and not a["applicable_d"]


def test_too_few_dates_to_tell_is_unknown_and_still_allowed():
    a = assess(table(Date=[date(2024, 1, 1), date(2024, 1, 8)], Spend=[1, 2]))
    assert a["level"] == "unknown" and a["applicable_c"] is True


# ── building a real date from labels ─────────────────────────────────────────────────────────────────────────────
from src.common.granularity import with_period


def test_year_month_labels_become_month_start_dates():
    ds = table(Brand=["A"] * 5, **{"Year-Month": ["2022-01", "2022-02", "Mar-22", "04/2022", "April 2022"]}, Sales=[1, 2, 3, 4, 5])
    out = with_period(ds)
    assert out.columns[:2] == ["Period", "Brand"] and out.raw_header[0] == "Period"
    assert out.data["Period"] == [date(2022, m, 1) for m in (1, 2, 3, 4, 4)]
    assert out.data["_row_id"] == ds.data["_row_id"] and out.data["Sales"] == ds.data["Sales"]     # nothing else moves
    a = assess(out)
    assert a["level"] == "monthly" and a["period"] == "Period" and a["applicable_d"]


def test_year_and_month_columns_and_a_year_column_get_dates_too():
    ym = with_period(table(Year=[2022, 2022, 2023], Month=["Jan", "Dec", "7"], Sales=[1, 2, 3]))
    assert ym.data["Period"] == [date(2022, 1, 1), date(2022, 12, 1), date(2023, 7, 1)]
    yr = with_period(table(Year=[2020, 2021, 2022, 2023], Sales=[1, 2, 3, 4]))
    assert yr.data["Period"] == [date(y, 1, 1) for y in (2020, 2021, 2022, 2023)] and assess(yr)["level"] == "yearly"


def test_unreadable_labels_become_empty_dates_not_errors():
    out = with_period(table(**{"Year-Month": ["2022-01", "2022-13", None, "2022-03", "2022-04"]}, Sales=[1, 2, 3, 4, 5]))
    assert out.data["Period"] == [date(2022, 1, 1), None, None, date(2022, 3, 1), date(2022, 4, 1)]


def test_a_table_that_already_has_dates_or_has_no_time_labels_is_left_alone():
    dated = table(Date=[date(2024, 1, 1) + timedelta(weeks=i) for i in range(5)], Spend=list(range(5)))
    assert with_period(dated) is dated
    plain = table(Brand=["a", "b"], Sales=[1, 2])
    assert with_period(plain) is plain


def test_an_existing_column_called_period_is_not_overwritten():
    ds = table(Period=["x", "y", "z"], Year=[2021, 2022, 2023], Sales=[1, 2, 3])
    out = with_period(ds)
    assert out.data["Period"] == ["x", "y", "z"] and out.data["Period (from labels)"] == [date(y, 1, 1) for y in (2021, 2022, 2023)]
