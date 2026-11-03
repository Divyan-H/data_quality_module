"""Phase T's engine against the worked examples in docs/Porter_Data_Transformation_v2.docx: each Porter script's
documented behaviour, reproduced with the typed operations (no script, no model).
"""
from datetime import date

import polars as pl
import pytest

from src.agent.errors import AgentError
from src.agent.operations import (AddColumn, Aggregate, AllocateByShare, AppendRows, BucketOthers, Classify, Condition,
                                  ConvertCurrency, DropTable, LookupJoin, MakeDate, MakePeriod, NetNextPeriod, Pivot,
                                  RemoveRows, ReplaceValues, Rule, RowFilter, SortRows, SplitColumn, SplitRows, Unpivot)
from src.agent.transform.engine import apply_op, split_items


def frame(rows: list[dict], prefix="t") -> pl.DataFrame:
    df = pl.DataFrame(rows)
    return df.with_columns(pl.Series("_row_id", [f"{prefix}:{i + 2}" for i in range(df.height)]),
                           pl.Series("_source_row", list(range(2, df.height + 2)))).select(
        ["_row_id", "_source_row", *df.columns])


def run(tables, *ops):
    effects = [apply_op(tables, op) for op in ops]
    return effects[-1] if len(effects) == 1 else effects


# ── 2.2 / 2.3 coupon city split ───────────────────────────────────────────────
def test_coupon_city_split_keeps_the_total_with_integer_allocation():
    t = {"Coupons": frame([{"City": "Mumbai, Pune, Delhi NCR", "Usage": 10, "Code": "A"},
                           {"City": "Delhi", "Usage": 7, "Code": "B"},
                           {"City": "Mumbai and Pune", "Usage": None, "Code": "C"}])}
    fx = run(t, SplitRows(column="City", allocate=["Usage"], keep_together=["Delhi NCR"]))
    df = t["Coupons"]
    assert df["City"].to_list() == ["Mumbai", "Pune", "Delhi NCR", "Delhi", "Mumbai", "Pune"]
    assert df["Usage"].to_list() == [4, 3, 3, 7, None, None]                        # 10 -> 4, 3, 3; NULL stays NULL
    assert df.filter(pl.col("Code") == "A")["Usage"].sum() == 10
    assert df["_row_id"].to_list()[:4] == ["t:2:s1", "t:2:s2", "t:2:s3", "t:3"]
    assert fx["tables"][0]["rows_before"] == 3 and fx["tables"][0]["rows_after"] == 6


def test_city_lists_split_on_every_listed_separator():
    assert split_items("Mumbai; Pune / Delhi NCR & Goa", [",", ";", "/", "&"], ["Delhi NCR"]) == ["Mumbai", "Pune", "Delhi NCR", "Goa"]
    assert split_items("Delhi NCR", [",", " "], ["Delhi NCR"]) == ["Delhi NCR"]


# ── 2.4 coupon classification (lookup + rules) ───────────────────────────────
def test_coupon_classification_by_lookup_then_rules():
    t = {"Coupons": frame([{"Promo": "DIWALI50", "Desc": "50% off trucks"}, {"Promo": "NEW10", "Desc": "flat 10"},
                           {"Promo": "ZZZ", "Desc": "unknown"}]),
         "Promo Map": frame([{"Promo Name": "diwali50", "Discount Type": "Percent"},
                             {"Promo Name": "NEW10", "Discount Type": "Flat"}], "m")}
    run(t, LookupJoin(table="Coupons", lookup="Promo Map", on={"Promo": "Promo Name"}, columns=["Discount Type"]),
        Classify(column="Vehicle Segment", rules=[Rule(when=[Condition(column="Desc", op="contains", value="truck")], value="HCV")],
                 default="All"))
    df = t["Coupons"]
    assert df["Discount Type"].to_list() == ["Percent", "Flat", None]
    assert df["Vehicle Segment"].to_list() == ["HCV", "All", "All"]


# ── 2.1 influencer city allocation ────────────────────────────────────────────
def test_influencer_spend_is_allocated_by_city_percentage_with_a_blank_city_for_the_rest():
    t = {"Influencer": frame([{"Campaign": "C1", "Spend": 10000.0, "Reach": 500.0}]),
         "Audience": frame([{"Campaign": "c1", "City": "Mumbai", "Pct": 60}, {"Campaign": "C1", "City": "Pune", "Pct": 30}], "a")}
    run(t, AllocateByShare(table="Influencer", share_table="Audience", on={"Campaign": "Campaign"}, category="City",
                           share="Pct", columns=["Spend", "Reach"]))
    df = t["Influencer"]
    assert df["City"].to_list() == ["Mumbai", "Pune", None]                       # 10% unallocated -> blank City row
    assert df["Spend"].to_list() == [6000.0, 3000.0, 1000.0]
    assert df["Reach"].to_list() == [300.0, 150.0, 50.0]


# ── 2.5 / 2.6 / 2.7 affiliate ─────────────────────────────────────────────────
def affiliate():
    return frame([{"day": "2026-09-01", "channel": "A", "network": "N1", "impressions": 100, "clicks": 5, "installs": 1},
                  {"day": "2026-09-01", "channel": "A", "network": "N1", "impressions": 50, "clicks": None, "installs": 2},
                  {"day": "2026-09-01", "channel": "X", "network": "N9", "impressions": 7, "clicks": 10, "installs": 0},
                  {"day": "2026-09-01", "channel": "Y", "network": "N8", "impressions": 3, "clicks": 20, "installs": 0}])


def test_affiliate_cumulater_sums_per_day_channel_network_with_blanks_as_zero():
    t = {"Affiliate": affiliate()}
    run(t, Aggregate(by=["day", "channel", "network"], sum=["impressions", "clicks", "installs"]))
    df = t["Affiliate"]
    first = df.row(0, named=True)
    assert df.height == 3 and first["impressions"] == 150 and first["clicks"] == 5 and first["installs"] == 3


def test_affiliate_unknown_channels_become_one_others_row_per_day():
    t = {"Affiliate": affiliate(), "Channels": frame([{"Channel": "A"}], "c")}
    run(t, Aggregate(table="Affiliate", by=["day", "channel", "network"], sum=["impressions", "clicks", "installs"]),
        BucketOthers(table="Affiliate", column="channel", keep_from={"table": "Channels", "column": "Channel"}, also=["network"],
                     sum_by=["day"], sum=["impressions", "clicks", "installs"]))
    df = t["Affiliate"]
    assert df["channel"].to_list() == ["A", "Others"] and df["network"].to_list() == ["N1", "Others"]
    assert df.filter(pl.col("channel") == "Others")["clicks"].item() == 30                # 10 + 20


def test_affiliate_city_split_keeps_unmatched_city_rows_with_zeros():
    t = {"Cum": frame([{"day": "2026-09-01", "network": "N1", "impressions": 90.0}]),
         "Cities": frame([{"DATE": "2026-09-01", "NETWORK_NAME": "N1", "CITY": "Pune"},
                          {"DATE": "2026-09-01", "NETWORK_NAME": "N1", "CITY": "Goa"},
                          {"DATE": "2026-09-01", "NETWORK_NAME": "N1", "CITY": "Agra"},
                          {"DATE": "2026-09-02", "NETWORK_NAME": "N1", "CITY": "Pune"}], "c")}
    run(t, AllocateByShare(table="Cum", share_table="Cities", on={"day": "DATE", "network": "NETWORK_NAME"}, category="CITY",
                           columns=["impressions"], keep_unmatched_shares=True))
    df = t["Cum"]
    assert df["CITY"].to_list() == ["Pune", "Goa", "Agra", "Pune"]
    assert df["impressions"].to_list() == [30.0, 30.0, 30.0, 0.0]


# ── 2.8 reactivation crosstab ─────────────────────────────────────────────────
def test_reactivation_days_become_dated_rows_and_buckets_become_columns():
    rows = []
    for bucket, base in (("31-120 days", 1), ("121-365 days", 10)):
        rows.append({"Month": "Jan-25", "Inactivity Bucket": bucket, "City": "Pune", "Accumulated Base": 500,
                     **{f"Day {d}": base * d for d in range(1, 32)}})
    rows.append({"Month": "Feb-25", "Inactivity Bucket": "31-120 days", "City": "Pune", "Accumulated Base": 600,
                 **{f"Day {d}": 1 for d in range(1, 32)}})
    t = {"Reactivation": frame(rows)}
    run(t, Unpivot(pattern=r"^Day \d+$", names_to="Day", values_to="Reactivated"),
        MakeDate(month="Month", day="Day"),
        RemoveRows(where=RowFilter(conditions=[Condition(column="Date", op="empty")])),
        Pivot(index=["Date", "City", "Accumulated Base"], columns_from="Inactivity Bucket", values_from="Reactivated"))
    df = t["Reactivation"]
    assert df.height == 31 + 28                                                     # Feb 29-31 dropped
    jan2 = df.filter(pl.col("Date") == date(2025, 1, 2)).row(0, named=True)
    assert jan2["31-120 days"] == 2 and jan2["121-365 days"] == 20


# ── 2.9 OOH netting ───────────────────────────────────────────────────────────
def test_ooh_next_month_negative_is_netted_against_the_month_before():
    t = {"OOH": frame([
        {"Site": "Ahmedabad Autohoods", "Month": "2024-10-05", "Spend": 801220},
        {"Site": "Ahmedabad Autohoods", "Month": "2024-11-03", "Spend": -801220},
        {"Site": "Ahmedabad Autohoods", "Month": "2024-11-20", "Spend": 2506320},
        {"Site": "Pune Hoardings", "Month": "2024-12-01", "Spend": 1000},
        {"Site": "Pune Hoardings", "Month": "2025-01-01", "Spend": -400},
        {"Site": "Only Negative", "Month": "2024-10-01", "Spend": -50}])}
    run(t, NetNextPeriod(value="Spend", period="Month", group_by=["Site"]))
    df = t["OOH"]
    final = {(r["Site"], r["Month (M/Y)"]): r["Final Spend"] for r in df.iter_rows(named=True)}
    assert final[("Ahmedabad Autohoods", "Oct-24")] == 0                           # 801,220 - 801,220
    assert final[("Ahmedabad Autohoods", "Nov-24")] == 2506320                     # no December return
    assert final[("Pune Hoardings", "Dec-24")] == 600                              # December -> January works
    assert final[("Pune Hoardings", "Jan-25")] == 0                                # a return month
    assert final[("Only Negative", "Oct-24")] == -50                               # negative-only keeps its value


# ── 2.10 DAP consolidation ────────────────────────────────────────────────────
def test_dap_appends_only_newer_rows_and_maps_geo_ids_to_cities():
    t = {"DAP Base": frame([{"Date": "2025-09-07", "Geo Region ID": "1", "Level 0 Mapping": "2W", "DAP": 10},
                            {"Date": "2025-09-08", "Geo Region ID": "99", "Level 0 Mapping": "2W", "DAP": 11}]),
         "DAP New": frame([{"DATE": "2025-09-08", "CITY_NAME": "Pune", "VEHICLE_CATEGORY": "2W", "DAP": 5},
                           {"DATE": "2025-09-09", "CITY_NAME": "Pune", "VEHICLE_CATEGORY": "2W", "DAP": 6}], "n")}
    run(t, AddColumn(name="City", from_column="Geo Region ID", mapping={"1": "Mumbai", "2": "Delhi"}, default="unspecified",
                     tables=["DAP Base"]),
        AppendRows(table="DAP Base", source="DAP New", rename={"DATE": "Date", "CITY_NAME": "City", "VEHICLE_CATEGORY": "Level 0 Mapping"},
                   where=[Condition(column="DATE", op="gte", value="2025-09-09")]))
    df = t["DAP Base"]
    assert "DAP New" not in t and df.height == 3
    assert df["City"].to_list() == ["Mumbai", "unspecified", "Pune"]
    assert df["Geo Region ID"].to_list() == ["1", "99", None]
    assert df["_row_id"].to_list() == ["t:2", "t:3", "n:3"]                       # base order, then new rows


# ── 2.11 revenue from AOV ─────────────────────────────────────────────────────
def test_revenue_is_orders_times_matching_aov_with_duplicate_aov_averaged():
    t = {"Orders": frame([{"ORDER_DATE": "2026-01-01", "ORDER_CITY": "Pune", "VEHICLE": "2W", "SEG": "SME", "ACQ": "x"},
                          {"ORDER_DATE": "2026-01-01", "ORDER_CITY": "Pune", "VEHICLE": "2W", "SEG": "SME", "ACQ": "y"},
                          {"ORDER_DATE": "2026-01-01", "ORDER_CITY": "Goa", "VEHICLE": "2W", "SEG": "SME", "ACQ": "x"}]),
         "AOV": frame([{"ORDER_DATE": "01/01/2026", "CITY_NAME": "pune", "VEHICLE_CATEGORY": "2W", "USER_SEGMENT": "SME", "CURRENT_AOV": 200},
                       {"ORDER_DATE": "2026-01-01", "CITY_NAME": "Pune", "VEHICLE_CATEGORY": "2W", "USER_SEGMENT": "SME", "CURRENT_AOV": 300}], "a")}
    run(t, Aggregate(table="Orders", by=["ORDER_DATE", "ORDER_CITY", "VEHICLE", "SEG"], count_as="orders"),
        LookupJoin(table="Orders", lookup="AOV", columns=["CURRENT_AOV"],
                   on={"ORDER_DATE": "ORDER_DATE", "ORDER_CITY": "CITY_NAME", "VEHICLE": "VEHICLE_CATEGORY", "SEG": "USER_SEGMENT"}),
        AddColumn(name="revenue_amount", formula="orders * CURRENT_AOV", tables=["Orders"]))
    df = t["Orders"]
    assert df["revenue_amount"].to_list() == [500.0, None]                         # 2 x mean(200, 300); Goa: no AOV


# ── section 3 utilities ───────────────────────────────────────────────────────
def test_usd_to_inr_by_quarterly_rates():
    t = {"Apple": frame([{"Date": "15-02-2025", "Spend": 10}, {"Date": "15-05-2025", "Spend": 10}, {"Date": "15-05-2030", "Spend": 1}])}
    fx = run(t, ConvertCurrency(columns=["Spend"], date_column="Date", note="USD -> INR",
                                rates=[{"from": "2025-01-01", "to": "2025-03-31", "rate": 86.5},
                                       {"from": "2025-04-01", "to": "2025-06-30", "rate": 85.0}]))
    assert t["Apple"]["Spend"].to_list() == [865.0, 850.0, 1.0]
    assert any("no rate" in n for n in fx["notes"])


def test_two_sheet_merge_aligns_columns_and_sorts_by_period_and_geography():
    t = {"W2022": frame([{"Period": "2022-12-01", "Geography": "Pune", "Rain": 1.5}]),
         "W2023": frame([{"Period": "2023-01-01", "Geography": "Delhi", "Rain": 0.0, "Extra": "x"},
                         {"Period": "2022-11-01", "Geography": "Agra", "Rain": 2.0, "Extra": "y"}], "b")}
    fx = run(t, AppendRows(table="W2022", source="W2023"), SortRows(table="W2022", by=["Period", "Geography"]))[0]
    assert t["W2022"]["Geography"].to_list() == ["Agra", "Pune", "Delhi"] and "Extra" not in t["W2022"].columns
    assert any("left out" in n for n in fx["notes"])


def test_head_office_becomes_bangalore_and_split_column_and_month():
    t = {"T": frame([{"City": "Head Office", "Name": "Asha Rao", "When": "2025-10-17"}, {"City": "Pune", "Name": "Ravi", "When": "2025-11-02"}])}
    run(t, ReplaceValues(mapping={"head office": "Bangalore"}, columns=["City"]),
        SplitColumn(column="Name", into=["First", "Last"], separator=" "),
        MakePeriod(column="When", unit="month", name="Month"))
    df = t["T"]
    assert df["City"].to_list() == ["Bangalore", "Pune"]
    assert df["First"].to_list() == ["Asha", "Ravi"] and df["Last"].to_list() == ["Rao", None]
    assert df["Month"].to_list() == [date(2025, 10, 1), date(2025, 11, 1)]


def test_wrong_names_are_questions_not_guesses():
    t = {"A": frame([{"x": 1}]), "B": frame([{"y": 1}])}
    with pytest.raises(AgentError, match="Which table"):
        apply_op(t, Unpivot(columns=["x"]))
    with pytest.raises(AgentError, match="no column"):
        apply_op(t, Unpivot(table="A", columns=["nope"]))
    with pytest.raises(AgentError, match="only table"):
        apply_op({"A": t["A"]}, DropTable(table="A"))
