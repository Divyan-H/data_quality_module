"""The plain-English commands for Phase T: each documented phrasing becomes the right typed operation, the table is
inferred when only one table has the columns, and unknown names become questions."""
import pytest

from src.agent.operations import (Aggregate, AllocateByShare, AppendRows, BucketOthers, Classify, ConvertCurrency,
                                  DropTable, LookupJoin, MakeDate, MakePeriod, NetNextPeriod, Pivot, SortRows, SplitColumn,
                                  SplitRows, Unpivot)
from src.agent.planner import Planner, PlanningContext
from src.agent.transform.workspace import FrameTable

from test_transform_engine import affiliate, frame


@pytest.fixture(scope="module")
def ctx():
    tabs = {"Reactivation": frame([{"Month": "Jan-25", "Bucket": "a", "City": "Pune", **{f"Day {d}": d for d in range(1, 4)}}]),
            "Coupons": frame([{"City": "Mumbai, Pune", "Usage": 10, "Full Name": "Asha Rao", "Spend": 1, "Date": "2025-02-15", "Promo": "X"}]),
            "Affiliate": affiliate(), "Channels": frame([{"Channel": "A"}]),
            "AOV": frame([{"ORDER_DATE": "2026-01-01", "CITY_NAME": "Pune", "CURRENT_AOV": 2}]),
            "DAP Base": frame([{"Date": "2025-09-01", "DAP": 1}]), "DAP New": frame([{"DATE": "2025-09-10", "DAP": 2}])}
    return PlanningContext([FrameTable(n, d) for n, d in tabs.items()], "T")


def one(ctx, text):
    p = Planner(None).plan(text, ctx)
    assert p.ops and len(p.ops) == 1, (text, p.ask or p.answer)
    return p.ops[0]


def test_unpivot_by_range_and_by_name_prefix(ctx):
    op = one(ctx, "unpivot Day 1 to Day 3 into rows as Day and Reactivated")
    assert isinstance(op, Unpivot) and op.columns == ["Day 1", "Day 2", "Day 3"] and op.values_to == "Reactivated"
    assert op.table == "Reactivation"
    op = one(ctx, "turn the Day columns into rows")
    assert op.pattern and op.names_to == "Day" and op.table == "Reactivation"


def test_split_column_vs_split_rows(ctx):
    assert isinstance(one(ctx, "split Full Name into First and Last"), SplitColumn)
    op = one(ctx, "split City into rows sharing Usage keeping Delhi NCR together")
    assert isinstance(op, SplitRows) and op.allocate == ["Usage"] and op.keep_together == ["Delhi NCR"] and op.table == "Coupons"
    assert one(ctx, "split City into rows sharing Usage equally").method == "equal"


@pytest.mark.parametrize("text,kind,check", [
    ("pivot Bucket into columns using Day 1 by City", Pivot, lambda o: o.index == ["City"] and o.agg == "sum"),
    ("sum impressions, clicks by day, channel, network", Aggregate, lambda o: o.sum == ["impressions", "clicks"] and o.table == "Affiliate"),
    ("count rows by City as orders", Aggregate, lambda o: o.count_as == "orders"),
    ("label channel not in Channels as Others also network then sum clicks by day", BucketOthers,
     lambda o: o.keep_from == {"table": "Channels", "column": "channel"} and o.also == ["network"] and o.sum_by == ["day"]),
    ("keep top 1 channel, others as Others", BucketOthers, lambda o: o.top_n == 1),
    ("net next-month negatives in Usage by Date per City", NetNextPeriod, lambda o: o.value == "Usage" and o.group_by == ["City"]),
    ("append DAP New to DAP Base where DATE >= 2025-09-09 renaming DATE as Date", AppendRows,
     lambda o: o.table == "DAP Base" and o.source == "DAP New" and o.rename == {"DATE": "Date"} and o.where[0].op == "gte"),
    ("look up CURRENT_AOV from AOV matching Date = ORDER_DATE, City = CITY_NAME in Coupons", LookupJoin,
     lambda o: o.table == "Coupons" and o.on == {"Date": "ORDER_DATE", "City": "CITY_NAME"}),
    ("convert Spend from USD to INR at 83.2", ConvertCurrency, lambda o: o.rate == 83.2),
    ("convert Spend from USD to INR at 86.5 for Q1 2025, 85 for Q2 2025 using Date", ConvertCurrency,
     lambda o: o.rates[1] == {"from": "2025-04-01", "to": "2025-06-30", "rate": 85.0} and o.date_column == "Date"),
    ("sort Coupons by Date descending", SortRows, lambda o: o.table == "Coupons" and o.descending == [True]),
    ("make Date2 from Month and Bucket", MakeDate, lambda o: o.name == "Date2" and o.table == "Reactivation"),
    ("add Month as the month of Date", MakePeriod, lambda o: o.unit == "month" and o.name == "Month"),
    ("drop table Channels", DropTable, lambda o: o.table == "Channels"),
    ("set Tier to Gold if Spend > 100000, Silver if Spend > 50000, else Bronze", Classify,
     lambda o: [r.value for r in o.rules] == ["Gold", "Silver"] and o.default == "Bronze"),
    ("allocate Usage by CITY_NAME from AOV matching Date = ORDER_DATE keeping unmatched cities", AllocateByShare,
     lambda o: o.keep_unmatched_shares and o.share is None),
])
def test_each_documented_command(ctx, text, kind, check):
    op = one(ctx, text)
    assert isinstance(op, kind) and check(op), op


def test_unknown_tables_and_columns_are_questions(ctx):
    assert "Nope" in Planner(None).plan("look up X from AOV matching Nope", ctx).ask
    assert "no table" in Planner(None).plan("drop table Nope", ctx).ask.lower() or "could not find" in Planner(None).plan("drop table Nope", ctx).ask
