"""The new Phase B checks (D-30 to D-33, run inputs) through the web API on samples/dq_feature_tests (see its README)."""
import io
import openpyxl
import polars as pl
import pytest

from build_dq_feature_workbook import build


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    return build(tmp_path_factory.mktemp("dqf"))


def items(summary, rule, sheet=None):
    return [i for i in summary["needs_review"] if i["rule"] == rule and (sheet is None or i["table_ref"] in sheet)]


@pytest.fixture
def run(flow, files):
    rid = flow.create([("dq_feature_test.xlsx", files["workbook"])], name="dq feature test")
    flow.run_phase(rid, "A", {"llm": "off"})
    tables = {t["sheet"]: t["ref"] for t in flow.summary(rid, "A")["tables"]}
    r = flow.c.post(f"/api/v1/runs/{rid}/phases/A/selection", json={"tables": list(tables.values())})
    assert r.status_code == 200, r.text
    flow.accept(rid, "A")
    r = flow.c.put(f"/api/v1/runs/{rid}/inputs", json={"yaml": files["inputs"].read_text(encoding="utf8")})
    assert r.status_code == 200, r.text
    flow.run_phase(rid, "B")
    return rid, tables, flow.summary(rid, "B")


def by_sheet(run, rule):
    rid, tables, b = run
    ref = {v: k for k, v in tables.items()}
    out = {}
    for i in b["needs_review"]:
        if i["rule"] == rule:
            out.setdefault(ref[i["table_ref"]], []).append(i)
    return out


def final_table(flow, rid, sheet):
    return pl.read_csv(io.BytesIO(flow.download(rid, flow.data_id(rid, "csv", sheet))), infer_schema_length=0)


def test_city_spellings_are_flagged_once_each_with_the_right_standard_and_severity(run):
    d30 = {i["current_value"]: i for i in by_sheet(run, "D-30")["City_Variants"]}
    standard = {"Bangalore": "Bengaluru", "BLR": "Bengaluru", "Banglore": "Bengaluru", "Bombay": "Mumbai", "Mum": "Mumbai", "Mumbay": "Mumbai",
                "New Delhi": "Delhi", "Dehli": "Delhi", "Dilli": "Delhi", "Hyd": "Hyderabad", "Hyderbad": "Hyderabad",
                "Madras": "Chennai", "Calcutta": "Kolkata", "Poona": "Pune", "Jaipure": "Jaipur"}
    for value, city in standard.items():
        assert d30[value]["proposed_value"] == city and d30[value]["severity"] == "warn", value
    assert d30["Bangalore"]["cells"] == 6 and d30["Bombay"]["cells"] == 5 and d30["Jaipure"]["cells"] == 1
    assert {v for v, i in d30.items() if i["severity"] == "info"} == {"Jaipur", "Delhi NCR"}     # known, but not in the client's list
    assert d30["Delhi NCR"]["proposed_value"] == "Delhi NCR"                                    # never merged into Delhi
    assert d30["Springfield"]["proposed_value"] is None and d30["Qwertyville"]["proposed_value"] is None and d30["Springfield"]["severity"] == "warn"
    assert not {"Others", "India", "Bengaluru", "Mumbai", "Delhi", "Pune"} & set(d30)           # placeholders and standard names are not flagged
    assert len(d30) == len(standard) + 4


def test_a_city_answer_changes_every_cell_with_that_spelling(flow, run):
    rid, tables, b = run
    before = final_table(flow, rid, "City_Variants")["City"].to_list()
    assert before.count("Bangalore") == 6 and before.count("Bengaluru") == 20            # flagged, nothing changed
    wb = openpyxl.load_workbook(io.BytesIO(flow.download(rid, "hygiene-review")))
    ws = wb["Needs_Review"]
    head = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        if row[head.index("Rule")].value == "D-30" and row[head.index("Current_Value")].value in ("Bangalore", "BLR", "Bombay"):
            row[head.index("Decision")].value = "Accept"
        if row[head.index("Rule")].value == "D-30" and row[head.index("Current_Value")].value == "Springfield":
            row[head.index("Decision")].value, row[head.index("New_Value")].value = "New Value", "Pune"
    buf = io.BytesIO()
    wb.save(buf)
    flow.upload_review(rid, "B", buf.getvalue())
    flow.wait(rid, "B")
    after = final_table(flow, rid, "City_Variants")["City"].to_list()
    assert after.count("Bengaluru") == 20 + 6 + 3 and after.count("Mumbai") == 20 + 5 and after.count("Pune") == 10 + 2
    assert "Bangalore" not in after and "BLR" not in after and "Bombay" not in after and "Springfield" not in after
    assert after.count("Banglore") == 2 and after.count("Mum") == 2                      # the unanswered ones stay as they were
    d30 = {i["current_value"]: i["status"] for i in flow.summary(rid, "B")["needs_review"] if i["rule"] == "D-30"}
    assert d30["Bangalore"] == d30["Bombay"] == d30["Springfield"] == "resolved" and d30["Banglore"] == "open"


def test_date_order_is_one_item_per_sheet_and_data_is_never_sorted(flow, run):
    rid, tables, b = run
    d31 = by_sheet(run, "D-31")
    assert set(d31) == {"Dates_Order", "Dates_NewestFirst"}                              # nothing on the sheets that are in order
    order = d31["Dates_Order"][0]
    assert order["severity"] == "warn" and "2 of 4 series are not in date order" in order["why_flagged"] and "Excel row 51" in order["why_flagged"]
    newest = d31["Dates_NewestFirst"][0]
    assert newest["severity"] == "info" and "newest first" in newest["why_flagged"]
    dates = final_table(flow, rid, "Dates_NewestFirst")["Date"].to_list()
    assert dates[0] > dates[1]                                                           # still newest first


def test_expected_columns_accept_different_header_styles_and_flag_only_what_is_missing(run):
    d32 = by_sheet(run, "D-32")
    assert {k: sorted(i["column"] for i in v) for k, v in d32.items()} == {
        "Cols_Missing": ["subplatform"], "Cols_Wrong": ["platform"], "Cols_AllMissing": ["city", "platform", "subplatform"]}
    assert all(i["client_report"] for v in d32.values() for i in v)                       # the client has to send them


def test_currency_symbols_are_read_as_rupees_and_other_symbols_are_flagged(flow, run):
    rid, tables, b = run
    d33 = {i["why_flagged"].split("'")[3]: i for i in by_sheet(run, "D-33")["Currency"]}
    assert set(d33) == {"₹", "$", "€"}
    assert d33["₹"]["severity"] == "info" and "read as INR" in d33["₹"]["why_flagged"]
    assert d33["$"]["severity"] == d33["€"]["severity"] == "warn" and "USD" in d33["$"]["why_flagged"] and "EUR" in d33["€"]["why_flagged"]
    spend = final_table(flow, rid, "Currency")["Spend"].to_list()
    assert spend[0] == "1000" and spend[11] == "1110" and "70" in spend and not any(c in "".join(spend) for c in "₹$€,")


def test_only_the_chosen_columns_are_checked_and_the_sheet_is_not_flagged_for_the_others(flow, run):
    rid, tables, b = run
    t = next(t for t in b["tables"] if t["ref"] == tables["Column_Choice"])
    assert t["column_choice"] == {"checked": ["Date", "City", "Spend"], "not_checked": ["Platform", "SubPlatform", "Remarks"], "not_found": []}
    assert "Column_Choice" not in by_sheet(run, "D-32")                                    # Platform and SubPlatform exist, they are just not checked
    remarks = final_table(flow, rid, "Column_Choice")["Remarks"].to_list()
    assert remarks[1] == "  padded  note "                                                  # passed on exactly as read (not trimmed)


def test_the_inputs_are_stored_with_the_run_and_nothing_else_is_flagged(flow, run):
    rid, tables, b = run
    got = flow.c.get(f"/api/v1/runs/{rid}/inputs").json()
    assert got["source"] == "run" and got["inputs"]["cities"] == ["Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Chennai", "Kolkata", "Pune"]
    assert got["inputs"]["currency"]["default"] == "INR" and len(got["tables"]) == 11
    rules = {(i["rule"]) for i in b["needs_review"]}
    assert rules == {"D-30", "D-31", "D-32", "D-33", "D-14"}                              # D-14: the $ values among rupee amounts look skewed
