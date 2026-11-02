"""The project's inputs (expected columns, columns to check, city list, currency) and how Phase B uses them."""
import io
from datetime import date

import polars as pl
import pytest

from src.common.run_inputs import columns_for, hygiene_overrides, load_for_run, parse, rate_for, save_for_run
from src.hygiene.config import load_config
from src.hygiene.pipeline import clean
from src.hygiene.table import Table

GOOD = """
project: Porter
expected_columns:
  all_sheets: [Date, City, Spend]
  sheets: {Daily: [Clicks]}
columns_to_check:
  - table: "*"
    columns: all
  - table: Daily
    columns: [Spend, City]
cities: [Bangalore, Mumbai]
currency:
  default: inr
  rates_level: quarterly
  rates:
    USD: {2024-Q1: 83.1, 2024-Q2: 83.4}
"""


def test_a_good_file_is_normalised():
    got, problems = parse(GOOD)
    assert problems == [] and got["project"] == "Porter" and got["currency"]["default"] == "INR"
    assert got["expected_columns"] == {"all_sheets": ["Date", "City", "Spend"], "sheets": {"Daily": ["Clicks"]}, "strict": False}
    assert got["columns_to_check"] == [{"table": "*", "columns": None}, {"table": "Daily", "columns": ["Spend", "City"]}]
    assert got["currency"]["rates"] == {"USD": {"2024-Q1": 83.1, "2024-Q2": 83.4}} and got["cities"] == ["Bangalore", "Mumbai"]


@pytest.mark.parametrize("text,message", [
    ("project: [", "not valid YAML"),
    ("colour: red", "unknown key(s): colour"),
    ("expected_columns: {all_sheets: Date}", "all_sheets must be a list"),
    ("columns_to_check: [{columns: [A]}]", "needs a table"),
    ("currency: {default: RUPEE}", "3-letter code"),
    ("currency: {default: INR, rates: {USD: {2024-Q1: 83}}}", "rates_level must be one of"),
    ("currency: {default: INR, rates_level: quarterly, rates: {USD: {2024-03-01: 83}}}", "is not a quarter like 2024-Q1"),
    ("currency: {default: INR, rates_level: daily, rates: {USD: {2024-Q1: 83}}}", "is not a date like 2024-03-15"),
    ("currency: {default: INR, rates_level: daily, rates: {USD: {2024-03-01: -2}}}", "must be a number above 0"),
    ("currency: {rates_level: daily, rates: {USD: {2024-03-01: 83}}}", "currency.default is needed"),
])
def test_problems_say_what_to_fix(text, message):
    _, problems = parse(text)
    assert any(message in p for p in problems), problems


def test_the_columns_of_a_table_come_from_the_most_specific_entry():
    spec = parse(GOOD)[0]["columns_to_check"]
    assert columns_for(spec, "f:0:t1", "Daily", "x.xlsx") == ["Spend", "City"]
    assert columns_for(spec, "f:0:t1", "Other", "x.xlsx") is None                      # * = all
    assert columns_for([], "f", "s", "x") is None and columns_for(spec, None, "DAILY", None) == ["Spend", "City"]   # names ignore case


def test_rates_are_found_by_day_or_by_quarter_and_the_default_currency_is_one():
    q = parse(GOOD)[0]
    assert rate_for(q, "USD", date(2024, 2, 10)) == 83.1 and rate_for(q, "usd", date(2024, 5, 1)) == 83.4
    assert rate_for(q, "USD", date(2024, 8, 1)) is None and rate_for(q, "INR", date(2030, 1, 1)) == 1.0 and rate_for(q, "EUR", date(2024, 1, 1)) is None
    d = parse("currency: {default: INR, rates_level: daily, rates: {USD: {2024-03-15: 82.9}}}")[0]
    assert rate_for(d, "USD", date(2024, 3, 15)) == 82.9 and rate_for(d, "USD", date(2024, 3, 16)) is None


def test_a_run_keeps_its_own_copy_and_a_bad_file_never_replaces_it(tmp_path):
    assert load_for_run(tmp_path)[1] == "default"
    _, problems = save_for_run(tmp_path, "currency: {default: nope}")
    assert problems and load_for_run(tmp_path)[1] == "default"                           # nothing saved
    save_for_run(tmp_path, GOOD)
    inputs, source = load_for_run(tmp_path)
    assert source == "run" and inputs["project"] == "Porter"
    assert "# " not in GOOD and (tmp_path / "config" / "run_inputs.yaml").read_text(encoding="utf8") == GOOD   # the text is kept as typed


def test_hygiene_overrides_carry_only_what_was_given():
    assert hygiene_overrides(parse("project: x")[0]) == {}
    got = hygiene_overrides(parse(GOOD)[0])
    assert set(got) == {"expected_columns", "columns_to_check", "city_master", "default_currency"}


# ── Phase B with the inputs ──────────────────────────────────────────────────
def make(cols: dict, overrides=None) -> Table:
    n = len(next(iter(cols.values())))
    data = {"_row_id": [f"f:0:t1:{i + 1}" for i in range(n)], "_source_row": list(range(2, n + 2)), **cols}
    return Table("f:0:t1", data, load_config(overrides=overrides), None, "f.csv", "Daily")


def test_only_the_chosen_columns_are_checked_and_the_rest_pass_through_as_read():
    cols = {"City": ["Bombay"] * 3 + ["Mumbai"] * 10, "Orders": ["1,200", "2", "3"] + ["4"] * 10,
            "Notes": ["  padded  "] * 13, "Clicks": ["1,200"] * 13}
    t = clean(make(cols, {"fixes": {"D-14": False}, "columns_to_check": [{"table": "Daily", "columns": ["City", "Orders", "Nope"]}]}))
    assert t.data["Notes"][0] == "  padded  " and t.data["Clicks"][0] == "1,200"        # not checked: not trimmed, not converted
    assert t.data["Orders"][0] == "1200"                                                  # checked: E-01 ran
    assert [i["current_value"] for i in t.review if i["rule"] == "D-30"] == ["Bombay"]    # a checked city column is still looked at
    assert t.column_choice == {"checked": ["City", "Orders"], "not_checked": ["Notes", "Clicks"], "not_found": ["Nope"]}
    assert t.types["Clicks"] == "text" and t.types["Orders"] == "numeric"                  # a column that needs cleaning is not typed as numbers
    assert [i["column"] for i in t.review if i["rule"] == "D-32"] == ["Nope"]
    assert list(t.cols) == ["City", "Orders", "Notes", "Clicks"]                          # the table keeps every column


def test_unchosen_columns_that_are_clean_numbers_keep_their_type():
    t = clean(make({"A": ["x"] * 4, "N": ["1", "2", "3", "4"]}, {"columns_to_check": [{"table": "*", "columns": ["A"]}]}))
    assert t.types["N"] == "numeric"


# ── the API ──────────────────────────────────────────────────────────────────
CSV = "Date,City,Spend,Clicks\n" + "\n".join(
    f"2026-01-{d % 28 + 1:02d},{c},{100 + d},{200 + d}" for d, c in enumerate(["Bangalore"] * 14 + ["Bengaluru"] * 3 + ["Pune"] * 10 + ["Poona"] * 3))


def test_inputs_are_asked_after_upload_stored_with_the_run_and_used_by_phase_b(flow):
    rid = flow.create([("cities.csv", CSV.encode())])
    first = flow.c.get(f"/api/v1/runs/{rid}/inputs").json()
    assert first["source"] == "default" and first["asked"] is False and first["tables"] == [] and "expected_columns" in first["yaml"]

    bad = flow.c.put(f"/api/v1/runs/{rid}/inputs", json={"yaml": "currency: {default: nope}"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "inputs_invalid"
    assert "3-letter code" in bad.json()["error"]["problems"][0]["message"]
    assert flow.c.put(f"/api/v1/runs/{rid}/inputs", json={}).status_code == 400

    flow.run_phase(rid, "A", {"llm": "off"})
    flow.accept(rid, "A")
    answers = """
expected_columns:
  all_sheets: [Date, City, Spend, Impressions]
columns_to_check:
  - table: "*"
    columns: [City, Spend]
cities: [Bangalore, Mumbai]
currency: {default: INR, rates_level: daily, rates: {USD: {2026-01-05: 82.5}}}
"""
    saved = flow.c.put(f"/api/v1/runs/{rid}/inputs", json={"yaml": answers}).json()
    assert saved["source"] == "run" and saved["asked"] is True and saved["inputs"]["currency"]["default"] == "INR"
    assert [t["columns"] for t in saved["tables"]] == [["Date", "City", "Spend", "Clicks"]]      # what the answers can refer to

    flow.run_phase(rid, "B")
    b = flow.summary(rid, "B")
    found = {(i["rule"], i["column"], i["current_value"]) for i in b["needs_review"]}
    assert ("D-32", "Impressions", None) in found                                         # the expected column that is missing
    assert {"Bengaluru", "Pune", "Poona"} <= {v for r, c, v in found if r == "D-30"}      # the project list makes Bangalore the standard
    assert not [1 for r, c, v in found if r == "D-30" and v == "Bangalore"]
    assert b["tables"][0]["column_choice"] == {"checked": ["City", "Spend"], "not_checked": ["Date", "Clicks"], "not_found": []}
    data = pl.read_csv(io.BytesIO(flow.download(rid, flow.data_id(rid, "csv"))), infer_schema_length=0)
    assert data["City"].to_list().count("Bengaluru") == 3                                 # flagged, not changed
