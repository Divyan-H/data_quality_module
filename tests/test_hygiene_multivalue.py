"""D-29: a cell that holds more than one value is flagged, never changed, until somebody answers."""
import json

import openpyxl
import polars as pl
import pytest

from src.hygiene.config import load_config
from src.hygiene.io import typed_frame
from src.hygiene.pipeline import OUT, clean, run_hygiene
from src.hygiene.review import ReviewError, apply_upload
from src.hygiene.rules import RULES, multi_value_cells
from src.hygiene.table import Table


def make(cols: dict, overrides=None) -> Table:
    n = len(next(iter(cols.values())))
    cols = {**cols, "Line No": [str(i) for i in range(n)]}          # keeps rows from being exact duplicates
    data = {"_row_id": [f"f:0:t1:{i + 1}" for i in range(n)], "_source_row": list(range(1, n + 1))}
    return Table("f:0:t1", {**data, **cols}, load_config(overrides=overrides))


def flags(t):
    return [r for r in t.review if r["rule"] == "D-29"]


def multi_table() -> Table:
    chans = ["Search", "Social", "Display", "Video"]
    spend = [str(1000 + i * 7) for i in range(40)]
    clicks = [str(200 + i) for i in range(40)]
    ch = [chans[i % 4] for i in range(40)]
    spend[10], spend[11] = "1,200 , 1,500", "300;400"
    clicks[12] = "12,34"
    ch[20], ch[21] = "Search; Social", "Search / Display"
    return make({"Channel": ch, "Spend": spend, "Clicks": clicks})


def test_lists_in_text_and_numbers_are_flagged_and_left_as_they_are():
    t = multi_table()
    for rule in RULES:
        rule(t)
    by = {(r["column"], r["row_id"]): r for r in flags(t)}
    assert {rid for col, rid in by if col == "Channel"} == {"f:0:t1:21", "f:0:t1:22"}
    one = by[("Channel", "f:0:t1:21")]
    assert "separated by ';'" in one["why_flagged"] and one["parts"] == ["Search", "Social"]
    assert "separated by '/'" in by[("Channel", "f:0:t1:22")]["why_flagged"]
    assert "2 numbers" in by[("Spend", "f:0:t1:11")]["why_flagged"] and "2 numbers" in by[("Spend", "f:0:t1:12")]["why_flagged"]
    amb = by[("Clicks", "f:0:t1:13")]
    assert amb["proposed_value"] == "12.34" and "two numbers (12 and 34)" in amb["why_flagged"]
    assert [t.data[c][i] for c, i in (("Channel", 20), ("Spend", 10), ("Spend", 11), ("Clicks", 12))] == [
        "Search; Social", "1,200 , 1,500", "300;400", "12,34"]                   # nothing was changed
    touched = {"f:0:t1:11", "f:0:t1:12", "f:0:t1:13", "f:0:t1:21", "f:0:t1:22"}
    assert not [a for a in t.audit if a["row_id"] in touched]
    assert all(r["client_report"] for r in flags(t))


def test_single_values_that_contain_separator_characters_are_not_lists():
    t = make({"Name": ["R&D", "AT&T", "N/A", "24/7", "B2B", "Alpha", "Beta", "Gamma"] * 3,
              "Amount": ["1,200", "2,500", "3,100", "4,000", "5,250", "6,000", "7,400", "8,100"] * 3})
    multi_value_cells(t)
    assert flags(t) == []                                                       # a comma used by 3+ cells is the convention


def test_a_comma_the_column_already_uses_is_fine_but_an_isolated_one_is_asked_about():
    eu = make({"Spend": ["12,5", "7,25", "3,75", "9,5", "1,99", "10"] * 4})
    multi_value_cells(eu)
    assert flags(eu) == []
    lone = make({"Spend": [str(100 + i) for i in range(30)] + ["100,200"]})
    multi_value_cells(lone)
    one = flags(lone)
    assert len(one) == 1 and one[0]["proposed_value"] == "100200" and "thousands mark" in one[0]["why_flagged"]


def test_formatted_amounts_are_one_value_not_ambiguous():
    t = make({"Spend": [str(100 + i) for i in range(30)] + ["\u20ac99,99", "\u20b92,500", "$1,200", "1,200", "12,500"]})
    multi_value_cells(t)
    assert flags(t) == [] and not t.held                                      # a symbol or a 1-2 digit lead: one amount


def test_a_column_that_is_mostly_lists_gets_one_flag():
    t = make({"Tags": ["red; blue", "green; black", "red; green", "blue; black", "plain"] * 4})
    multi_value_cells(t)
    found = flags(t)
    assert len(found) == 1 and found[0]["scope"] == "column" and "list field" in found[0]["why_flagged"]
    assert not t.held


def test_listed_cells_are_capped_and_the_total_is_reported():
    t = make({"Channel": [f"Alpha{i}; Beta{i}" if i < 30 else f"Solo{i}" for i in range(400)]},
             {"multi_value_max_cells": 10, "multi_value_max_share": 0.9})
    multi_value_cells(t)
    found = flags(t)
    assert sum(f["scope"] == "cell" for f in found) == 10 and any("30 cells" in f["why_flagged"] for f in found)
    assert len(t.held) == 30


def test_other_rules_leave_held_cells_alone_and_the_column_stays_text():
    t = multi_table()
    clean(t)
    assert (t.data["Spend"][10], t.data["Clicks"][12], t.data["Channel"][20]) == ("1,200 , 1,500", "12,34", "Search; Social")
    ids = {"f:0:t1:11", "f:0:t1:12", "f:0:t1:13", "f:0:t1:21"}
    assert not [a for a in t.audit if a["rule"] in ("D-04", "E-01", "D-10", "D-12", "D-14", "D-22") and a["row_id"] in ids]
    df = typed_frame(t)
    assert df["Spend"].dtype == pl.String and df["Clicks"].dtype == pl.String   # nothing was silently converted


def multi_run(tmp_path):
    rows = ["Date,Channel,Spend,Clicks"]
    for i in range(40):
        ch = ["Search", "Social", "Display", "Video"][i % 4]
        spend, clicks = str(1000 + i * 7), str(200 + i)
        if i == 10:
            spend = '"300;400"'
        if i == 12:
            clicks = '"12,34"'
        if i == 20:
            ch = '"Search; Social"'
        rows.append(f"2026-01-{i % 28 + 1:02d},{ch},{spend},{clicks}")
    src = tmp_path / "multi.csv"
    src.write_text("\n".join(rows), encoding="utf8")
    run_hygiene(str(src), str(tmp_path / "run"))
    return tmp_path / "run"


def decide(out, decisions, name="edited.xlsx"):
    wb = openpyxl.load_workbook(out / OUT / "Hygiene_Review.xlsx")
    ws = wb["Needs_Review"]
    hdr = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        if row[hdr.index("Rule")].value == "D-29" and row[hdr.index("Current_Value")].value in decisions:
            row[hdr.index("Decision")].value, row[hdr.index("New_Value")].value = decisions[row[hdr.index("Current_Value")].value]
    wb.save(out / name)
    return out / name


def table_of(out):
    return pl.read_parquet(next((out / OUT).glob("tables/*/data.parquet")))


def test_answers_to_multi_value_items_in_the_review_workbook(tmp_path):
    out = multi_run(tmp_path)
    items = json.loads((out / OUT / "needs_review.json").read_text("utf8"))
    assert sum(i["rule"] == "D-29" and i["status"] == "open" for i in items) == 3
    df = table_of(out)
    assert df["Spend"].dtype == pl.String and "300;400" in df["Spend"].to_list()    # unanswered: kept as it is
    path = decide(out, {"300;400": ("New Value", "350"), "12,34": ("Accept", None), "Search; Social": ("New Value", "Search")})
    apply_upload(out, path, load_config())
    df = table_of(out)
    assert df["Spend"].dtype == pl.Int64 and df["Spend"][10] == 350                 # answered: a number column again
    assert df["Clicks"][12] == pytest.approx(12.34)                                 # Accept took the proposed reading
    assert df["Channel"][20] == "Search" and df.height == 40
    items = json.loads((out / OUT / "needs_review.json").read_text("utf8"))
    assert all(i["status"] == "resolved" for i in items if i["rule"] == "D-29")


def test_split_makes_one_row_per_value_and_the_audit_says_the_numbers_are_repeated(tmp_path):
    out = multi_run(tmp_path)
    apply_upload(out, decide(out, {"Search; Social": ("Split", None)}), load_config())
    df = table_of(out)
    assert df.height == 41 and df["Channel"][20:22].to_list() == ["Search", "Social"]
    base = df["_row_id"][20][:-3]
    assert df["_row_id"][20:22].to_list() == [base + ":s1", base + ":s2"]
    assert df["_source_row"][20] == df["_source_row"][21]                           # both lead back to the Excel row
    assert df["Date"][20] == df["Date"][21] and df["Spend"][20] == df["Spend"][21]  # the other values are repeated
    audit = json.loads((out / OUT / "remediation_audit.json").read_text("utf8"))
    splits = [a for a in audit if a["action"] == "split"]
    assert [a["new_value"] for a in splits] == ["Search", "Social"] and all("repeated" in a["note"] for a in splits)


@pytest.mark.parametrize("cell", ["300;400", "12,34"])
def test_split_is_refused_for_numbers(tmp_path, cell):
    out = multi_run(tmp_path)
    with pytest.raises(ReviewError, match="Split only applies"):
        apply_upload(out, decide(out, {cell: ("Split", None)}), load_config())


def test_gap_filling_never_overwrites_a_cell_that_holds_several_values():
    """Found by the end-to-end sheet: D-27 read a held cell as blank and filled it with the mean of its neighbours."""
    from datetime import date, timedelta
    n = 20
    data = {"_row_id": [f"f:0:t1:{i + 1}" for i in range(n)], "_source_row": list(range(1, n + 1)),
            "Date": [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)],
            "Spend": [str(100 + i) for i in range(n)], "Orders": [str(50 + i) for i in range(n)]}
    data["Spend"][8], data["Orders"][12] = "300;400", "0"
    t = Table("f:0:t1", data, load_config(overrides={"fixes": {"D-27": True, "D-14": False}}))
    for rule in RULES:
        rule(t)
    assert t.data["Spend"][8] == "300;400"                              # held: left as written, not filled
    assert t.data["Orders"][12] == "62"                                  # a real isolated zero is still filled (mean of 61 and 63)
    assert [r["column"] for r in flags(t)] == ["Spend"]
