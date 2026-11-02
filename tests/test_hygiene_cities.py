"""D-30 city names, D-31 date order and D-32 expected columns (Phase B)."""
import json
import time

import openpyxl
import polars as pl
import pytest

from src.hygiene.cities import build_index, edit_distance, normalize
from src.hygiene.config import load_config
from src.hygiene.pipeline import OUT, clean, run_hygiene
from src.hygiene.review import apply_upload
from src.hygiene.rules import RULES, city_names, date_order, expected_columns
from src.hygiene.table import Table


def make(cols: dict, overrides=None) -> Table:
    n = len(next(iter(cols.values())))
    cols = {**cols, "Line No": [str(i) for i in range(n)]}
    data = {"_row_id": [f"f:0:t1:{i + 1}" for i in range(n)], "_source_row": list(range(2, n + 2))}
    return Table("f:0:t1", {**data, **cols}, load_config(overrides=overrides), None, "f.csv", "Sheet1")


def items(t, rule):
    return [r for r in t.review if r["rule"] == rule]


# ── the matcher ──────────────────────────────────────────────────────────────
def test_normalise_ignores_case_accents_spaces_and_punctuation():
    assert normalize("New-Delhi ") == normalize("NEW DELHI") == normalize("new delhi") == "newdelhi"
    assert normalize("Bogotá") == "bogota"


def test_edit_distance_counts_a_swap_as_one_edit_and_stops_early():
    assert edit_distance("mumbia", "mumbai", 2) == 1 and edit_distance("dehli", "delhi", 2) == 1
    assert edit_distance("pune", "delhi", 2) == 3                                   # above the cap: cap + 1


@pytest.mark.parametrize("value,canonical,kind", [
    ("Bombay", "Mumbai", "alias"), ("BLR", "Bengaluru", "alias"), ("Bangalore", "Bengaluru", "alias"),
    ("Dehli", "Delhi", "alias"), ("Hyderbad", "Hyderabad", "alias"), ("Mumbay", "Mumbai", "alias"),
    ("Banglorr", "Bengaluru", "close"), ("Ahmedabadd", "Ahmedabad", "close"), ("Chennnnai", "Chennai", "close"),
    ("Mumbai", "Mumbai", "standard"), ("pune", "Pune", "standard")])
def test_known_names_short_forms_and_misspellings_resolve(value, canonical, kind):
    m = build_index("config/city_names.yaml").resolve(value)
    assert m is not None and (m.canonical, m.kind) == (canonical, kind)


def test_unknown_and_non_city_values_are_not_guessed():
    idx = build_index("config/city_names.yaml")
    assert idx.resolve("Springfield") is None and idx.resolve("Others") is None and idx.resolve("N/A") is None
    assert idx.resolve("Pan India") is None and idx.resolve("") is None


def test_delhi_ncr_is_its_own_city_and_a_close_miss_of_two_cities_is_not_guessed():
    idx = build_index("config/city_names.yaml")
    assert idx.resolve("Delhi NCR").canonical == "Delhi NCR" and idx.resolve("New Delhi").canonical == "Delhi"
    assert idx.resolve("Kolkat") is None or idx.resolve("Kolkat").canonical == "Kolkata"


def test_a_project_list_makes_its_spelling_the_standard():
    idx = build_index("config/city_names.yaml", master=["Bangalore", "Mumbai"])
    assert idx.master_mode and idx.resolve("Bengaluru").canonical == "Bangalore" and idx.resolve("BLR").canonical == "Bangalore"
    assert idx.resolve("Pune").in_master is False                                   # a known city, but not on the project's list


def test_the_index_is_cached_and_a_lookup_is_fast():
    t0 = time.perf_counter()
    idx = build_index("config/city_names.yaml")
    built = time.perf_counter() - t0
    assert build_index("config/city_names.yaml") is idx                             # built once per process
    t0 = time.perf_counter()
    for i in range(2000):
        idx.resolve(f"Bangalorr{i % 7}")
    assert (time.perf_counter() - t0) / 2000 < 0.005 and built < 2.0                # milliseconds, not seconds


def test_a_gazetteer_file_widens_the_index(tmp_path):
    g = tmp_path / "g.csv"
    g.write_text("name,canonical,country\nSpringfield,Springfield,US\nSpringfeld,Springfield,US\nKoln,Cologne,DE\n", encoding="utf8")
    idx = build_index("config/city_names.yaml", gazetteer=str(g), countries=["US"])
    assert idx.resolve("Springfeld").canonical == "Springfield" and idx.resolve("Koln") is None      # DE rows were filtered out


# ── the rule ─────────────────────────────────────────────────────────────────
def city_table():
    cities = ["Mumbai"] * 30 + ["Bombay"] * 3 + ["Bengaluru"] * 20 + ["Bangalore"] * 5 + ["BLR"] * 2 + ["Delhi"] * 10 + ["Dehli"]
    cities += ["Qwertyville"] * 6 + ["Qwertyvile"] * 2 + ["Others", "Springfield"]
    return make({"City": cities, "Spend": [str(100 + i) for i in range(len(cities))]})


def test_same_city_other_names_short_forms_and_spelling_mistakes_are_flagged_once_per_spelling():
    t = city_table()
    city_names(t)
    got = {i["current_value"]: i for i in items(t, "D-30")}
    assert set(got) == {"Bombay", "Bangalore", "BLR", "Dehli", "Qwertyvile"}
    assert got["Bombay"]["proposed_value"] == "Mumbai" and got["Bombay"]["scope"] == "value" and got["Bombay"]["cells"] == 3
    assert got["Bangalore"]["proposed_value"] == "Bengaluru" and "'Bengaluru' (20)" in got["Bangalore"]["why_flagged"]   # says what else is there
    assert got["Qwertyvile"]["proposed_value"] == "Qwertyville" and "same city" in got["Qwertyvile"]["why_flagged"]          # found from the data alone
    assert t.data["City"][30] == "Bombay"                                                                          # nothing was changed
    assert all(i["client_report"] for i in got.values())


def test_standard_names_others_and_unknown_single_cities_are_not_flagged():
    t = make({"City": ["Mumbai", "Pune", "Others", "Springfield", "Pune", "Mumbai"] * 5})
    city_names(t)
    assert items(t, "D-30") == []


def test_only_a_city_column_is_looked_at():
    t = make({"Channel": ["Bombay", "Bangalore"] * 10})
    city_names(t)
    assert items(t, "D-30") == []


def test_with_a_project_list_everything_else_is_flagged():
    t = make({"City": ["Bangalore"] * 10 + ["Bengaluru"] * 3 + ["Pune"] * 4 + ["Atlantis"] * 2}, {"city_master": ["Bangalore", "Mumbai"]})
    city_names(t)
    got = {i["current_value"]: i for i in items(t, "D-30")}
    assert set(got) == {"Bengaluru", "Pune", "Atlantis"} and got["Bengaluru"]["proposed_value"] == "Bangalore"
    assert "not in the project's city list" in got["Pune"]["why_flagged"] and got["Atlantis"]["proposed_value"] is None


def test_known_but_unlisted_cities_are_a_low_priority_note_and_unknown_ones_are_not():
    cols = {"City": ["Bangalore"] * 10 + ["Pune"] * 4 + ["Jamnagar"] * 3 + ["Jamnagr"] * 2 + ["Atlantis"] * 2 + ["Mumbay"] * 2}
    t = make(cols, {"city_master": ["Bangalore", "Mumbai"]})
    city_names(t)
    got = {i["current_value"]: i for i in items(t, "D-30")}
    assert got["Pune"]["severity"] == got["Jamnagar"]["severity"] == "info" and "known city" in got["Pune"]["why_flagged"]
    assert got["Jamnagr"]["severity"] == "warn" and got["Jamnagr"]["proposed_value"] == "Jamnagar"      # a typo of a known city
    assert got["Atlantis"]["severity"] == "warn" and got["Mumbay"]["severity"] == "warn" and got["Mumbay"]["proposed_value"] == "Mumbai"
    order = [i["current_value"] for i in items(t, "D-30")]
    assert order.index("Atlantis") < order.index("Pune") and order.index("Jamnagr") < order.index("Jamnagar")   # what needs a look first


def test_the_fallback_list_never_overrides_the_alias_file():
    t = make({"City": ["Bangalore"] * 3 + ["Bengaluru"] * 3}, {"city_master": ["Mumbai"]})
    city_names(t)
    assert {i["current_value"]: i["proposed_value"] for i in items(t, "D-30")} == {"Bangalore": "Bengaluru", "Bengaluru": "Bengaluru"}


def test_held_cities_are_left_alone_by_the_other_rules():
    t = city_table()
    clean(t)
    assert t.data["City"][30] == "Bombay" and t.data["City"][-3] != "Qwertyville"                                       # D-10 / D-12 did not merge them


def test_answering_once_changes_every_cell_with_that_spelling(tmp_path):
    rows = ["Date,City,Spend"] + [f"2026-01-{d % 28 + 1:02d},{c},{100 + d}" for d, c in
                                 enumerate(["Mumbai"] * 20 + ["Bombay"] * 6 + ["Pune"] * 20 + ["Poona"] * 2)]
    (tmp_path / "in.csv").write_text("\n".join(rows), encoding="utf8")
    run_hygiene(str(tmp_path / "in.csv"), str(tmp_path / "run"))
    base = tmp_path / "run" / OUT
    needs = json.loads((base / "needs_review.json").read_text("utf8"))
    assert sorted(i["current_value"] for i in needs if i["rule"] == "D-30" and i["status"] == "open") == ["Bombay", "Poona"]
    assert pl.read_parquet(next(base.glob("tables/*/data.parquet")))["City"].to_list().count("Bombay") == 6     # unanswered: untouched
    wb = openpyxl.load_workbook(base / "Hygiene_Review.xlsx")
    ws = wb["Needs_Review"]
    hdr = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        if row[hdr.index("Rule")].value == "D-30":
            if row[hdr.index("Current_Value")].value == "Bombay":
                row[hdr.index("Decision")].value = "Accept"                       # takes the proposed name for all 6 cells
            else:
                row[hdr.index("Decision")].value, row[hdr.index("New_Value")].value = "New Value", "Pune City"
    wb.save(tmp_path / "edited.xlsx")
    apply_upload(tmp_path / "run", tmp_path / "edited.xlsx", load_config())
    cities = pl.read_parquet(next(base.glob("tables/*/data.parquet")))["City"].to_list()
    assert cities.count("Mumbai") == 26 and cities.count("Pune City") == 2 and "Bombay" not in cities and "Poona" not in cities


# ── D-31 ─────────────────────────────────────────────────────────────────────
def dated(dates, **extra):
    return make({"Date": dates, "Spend": [str(100 + i) for i in range(len(dates))], **extra})


def test_dates_in_order_are_fine_and_a_break_is_found_with_its_place():
    ok = dated([f"2026-01-{d:02d}" for d in range(1, 11)])
    date_order(ok)
    assert items(ok, "D-31") == []
    bad = dated([f"2026-01-{d:02d}" for d in (1, 2, 3, 9, 4, 5, 6, 7)])
    date_order(bad)
    found = items(bad, "D-31")
    assert len(found) == 1 and found[0]["scope"] == "column" and "Excel row 6" in found[0]["why_flagged"]
    assert bad.data["Date"][3] == "2026-01-09"                                      # nothing was sorted


def test_a_table_sorted_newest_first_is_noted_not_blamed():
    t = dated([f"2026-01-{d:02d}" for d in range(10, 0, -1)])
    date_order(t)
    found = items(t, "D-31")
    assert len(found) == 1 and found[0]["severity"] == "info" and "newest first" in found[0]["why_flagged"]


def test_order_is_judged_inside_each_series():
    cities = ["Pune"] * 5 + ["Delhi"] * 5
    dates = [f"2026-01-{d:02d}" for d in list(range(1, 6)) + list(range(1, 6))]       # each series is in order, the table is not sorted overall
    t = dated(dates, City=cities)
    date_order(t)
    assert items(t, "D-31") == []


# ── D-32 ─────────────────────────────────────────────────────────────────────
def test_missing_expected_columns_are_flagged_with_the_nearest_name():
    t = make({"Date": ["2026-01-01"] * 3, "Cost": ["1", "2", "3"], "City": ["Pune"] * 3},
             {"expected_columns": {"all_sheets": ["date", "Spend", "Impressions"], "sheets": {"Sheet1": ["City"], "Other": ["Clicks"]}}})
    expected_columns(t)
    found = {i["column"]: i for i in items(t, "D-32")}
    assert set(found) == {"Spend", "Impressions"}                                    # City is there; Clicks is for another sheet
    assert "looks like" not in found["Impressions"]["why_flagged"]
    assert all(i["client_report"] for i in found.values())                           # the client has to send the column


def headers_found(headers, expected=("date", "platform", "subplatform", "city")):
    from src.hygiene.config import DEFAULTS
    from src.hygiene.rules import find_expected_columns
    return find_expected_columns(list(expected), headers, DEFAULTS["column_synonyms"])


def test_header_names_do_not_have_to_be_exact():
    got = headers_found(["Week Start", "Channel", "Sub-Platform", "City Name", "Spend"])
    assert got == {"date": "Week Start", "platform": "Channel", "subplatform": "Sub-Platform", "city": "City Name"}
    got = headers_found(["order_date", "PLATFORM", "SubPlatform", "Cty"])                       # case, underscores, camelCase, a typo
    assert got == {"date": "order_date", "platform": "PLATFORM", "subplatform": "SubPlatform", "city": "Cty"}


def test_a_longer_header_belongs_to_the_longer_expected_name_and_missing_ones_are_none():
    got = headers_found(["Platform Name", "Sub Platform Name", "Date"])
    assert got == {"date": "Date", "platform": "Platform Name", "subplatform": "Sub Platform Name", "city": None}
    got = headers_found(["Sub Platform", "Date", "Spend"])                                      # subplatform alone is not the platform
    assert got["platform"] is None and got["subplatform"] == "Sub Platform" and got["city"] is None


def test_the_default_run_inputs_expect_date_platform_subplatform_city():
    from src.common.run_inputs import DEFAULT_PATH, load_text, parse
    got = parse(load_text(DEFAULT_PATH))[0]
    assert got["expected_columns"]["all_sheets"] == ["date", "platform", "subplatform", "city"] and got["currency"]["default"] == "INR"


# ── D-33 currency symbols ────────────────────────────────────────────────────
def test_a_rupee_symbol_alone_is_read_as_rupees_and_another_symbol_is_flagged():
    t = make({"Spend": ["₹1,200", "₹300", "$50", "400", "500", "600", "700", "800"]})
    clean(t)
    got = {i["why_flagged"].split("'")[3]: i for i in items(t, "D-33")}
    assert set(got) == {"₹", "$"} and got["₹"]["severity"] == "info" and "read as INR" in got["₹"]["why_flagged"]
    assert got["$"]["severity"] == "warn" and "USD" in got["$"]["why_flagged"] and "not converted" in got["$"]["suggested_action"] or "not" in got["$"]["suggested_action"]
    assert t.data["Spend"][0] == "1200" and t.data["Spend"][2] == "50"                           # symbols removed, amounts unchanged


def test_the_default_currency_comes_from_the_run_inputs():
    t = make({"Spend": ["$100", "$200", "$300", "$400", "$500"]}, {"default_currency": "USD"})
    clean(t)
    assert [i["severity"] for i in items(t, "D-33")] == ["info"] and "read as USD" in items(t, "D-33")[0]["why_flagged"]


def test_names_match_ignoring_case_spaces_and_punctuation_and_strict_lists_extras():
    t = make({"Order Date": ["x"], "SPEND_INR": ["1"]}, {"expected_columns": {"all_sheets": ["order_date", "Spend INR"], "strict": True}})
    expected_columns(t)
    assert [i["column"] for i in items(t, "D-32")] == [None] and "Line No" in items(t, "D-32")[0]["why_flagged"]
    quiet = make({"A": ["x"]})
    expected_columns(quiet)
    assert items(quiet, "D-32") == []                                                # no expected list: no check
