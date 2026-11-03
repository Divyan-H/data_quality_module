"""The whole web flow on samples/e2e_feature_check.xlsx: one sheet per group of recent features (see its Plan sheet).

    Daily_Media          Phase B: D-29 multi-value cells, D-15 negatives, D-27 gap fill, D-28 totals, review answers, Phase C on daily data
    Monthly_Competitor   Phase C on a monthly table with Year-Month labels only (the weekly step), Sum / Average per column
    Weekly_TV            Phase C findings on weekly data (repeat, missing week, run of zeros)
"""
import io
import zipfile

import openpyxl
import polars as pl
import pytest

from build_e2e_workbook import build


@pytest.fixture(scope="module")
def workbook(tmp_path_factory):
    return build(tmp_path_factory.mktemp("e2e") / "e2e_feature_check.xlsx")


def start(flow, workbook, sheet):
    """A new run on the workbook with only `sheet` kept at Phase A; Phase A accepted."""
    rid = flow.create([("e2e_feature_check.xlsx", workbook)], name=sheet)
    flow.run_phase(rid, "A", {"llm": "off"})
    tables = {t["sheet"]: t for t in flow.summary(rid, "A")["tables"]}
    assert {"Daily_Media", "Monthly_Competitor", "Weekly_TV", "City_Dates"} <= set(tables)           # (Plan is a small table too: never selected)
    r = flow.c.post(f"/api/v1/runs/{rid}/phases/A/selection", json={"tables": [tables[sheet]["ref"]]})
    assert r.status_code == 200, r.text
    flow.accept(rid, "A")
    return rid, tables[sheet]["ref"]


def items(summary, rule):
    return [i for i in summary["needs_review"] if i["rule"] == rule]


def final_table(flow, rid, sheet):
    return pl.read_csv(io.BytesIO(flow.download(rid, flow.data_id(rid, "csv", sheet))), infer_schema_length=0)


# ── Daily_Media: Phase B features ────────────────────────────────────────────
def test_daily_sheet_phase_b_flags_and_review_answers(flow, workbook):
    rid, ref = start(flow, workbook, "Daily_Media")
    flow.run_phase(rid, "B", {"fixes": {"D-27": True}, "zero_expected": {ref: ["Rain", "Refunds"]}})
    b = flow.summary(rid, "B")

    # D-29: lists and the ambiguous comma are flagged, the single names with an ampersand are not
    d29 = items(b, "D-29")
    assert {(i["column"], i["current_value"]) for i in d29} == {
        ("Channel", "Search; Social"), ("Channel", "Search / Display"), ("Spend", "300;400"), ("Clicks", "12,34")}
    assert not [i for i in b["needs_review"] if i["current_value"] in ("R&D", "AT&T")]
    # D-15: only the column with negatives, one item
    assert [(i["column"], i["row_id"]) for i in items(b, "D-15")] == [("Refunds", None)]      # one item for the column, no row
    # D-28: the total column is listed with its parts
    assert [bk["sum_these"] for bk in b["buckets"]] == [["Display", "Search", "Social"]]
    # D-27: the isolated zero in Orders was filled; the zero-expected columns were not touched
    data = final_table(flow, rid, "Daily_Media")
    assert data.height == 90 and data["Orders"][51] == "202"                           # mean of 200 and 204 (rows 50 and 52)
    # nothing flagged was changed behind the flag
    assert data["Spend"][20] == "300;400" and data["Clicks"][30] == "12,34" and data["Channel"][11] == "Search; Social"
    assert data["Refunds"][4] == "-5" and data["Refunds"][14] == "-12"

    # the analyst answers in the review workbook: New Value, Accept, Split
    wb = openpyxl.load_workbook(io.BytesIO(flow.download(rid, "hygiene-review")))
    ws = wb["Needs_Review"]
    head = [c.value for c in ws[1]]
    answers = {"300;400": ("New Value", "350"), "12,34": ("Accept", None), "Search; Social": ("Split", None),
               "Search / Display": ("New Value", "Search")}
    for row in ws.iter_rows(min_row=2):
        if row[head.index("Rule")].value == "D-29":
            row[head.index("Decision")].value, row[head.index("New_Value")].value = answers[row[head.index("Current_Value")].value]
    buf = io.BytesIO()
    wb.save(buf)
    flow.upload_review(rid, "B", buf.getvalue())
    flow.wait(rid, "B")
    data = final_table(flow, rid, "Daily_Media")
    assert data.height == 91                                                            # one row was split
    assert data["Channel"][11:13].to_list() == ["Search", "Social"] and data["Channel"][13] == "Search"
    spend = data["Spend"].cast(pl.Float64, strict=False)
    assert spend.null_count() == 0 and 350.0 in spend.to_list()                        # Spend is a number column again
    assert data["Clicks"].cast(pl.Float64, strict=False).null_count() == 0 and "12.34" in data["Clicks"].to_list()
    assert [i["status"] for i in items(flow.summary(rid, "B"), "D-29")] == ["resolved"] * 4      # every D-29 item was answered
    flow.accept(rid, "B")

    # Phase C on daily data: nothing refuses it
    cands = flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables").json()["tables"]
    assert [(t["weekly"], t["level"], t["from_monthly"]) for t in cands] == [(True, "daily", False)]
    tpl = flow.c.get(f"/api/v1/runs/{rid}/phases/C/config-template").content
    assert "Column Setup" not in openpyxl.load_workbook(io.BytesIO(tpl)).sheetnames    # only monthly tables get it
    flow.config(rid, "C", tpl)
    flow.run_phase(rid, "C")
    c = flow.summary(rid, "C")
    assert c["rows"] == 91 and c["layout"]["period"] == "Date"
    flow.accept(rid, "C")
    assert flow.c.post(f"/api/v1/runs/{rid}/phases/D/skip", json={}).status_code == 200
    v = flow.view(rid)
    assert v["complete"] and [p["skipped"] for p in v["phases"]] == [False, False, False, True, False]
    z = zipfile.ZipFile(io.BytesIO(flow.c.get(f"/api/v1/runs/{rid}/bundle").content))
    assert "MANIFEST.json" in z.namelist() and z.testzip() is None


def test_unanswered_flags_leave_the_data_exactly_as_it_came(flow, workbook):
    rid, _ = start(flow, workbook, "Daily_Media")
    flow.run_phase(rid, "B")
    flow.accept(rid, "B")                                                               # accepting a phase does not answer its items
    data = final_table(flow, rid, "Daily_Media")
    assert data.height == 90 and data["Spend"][20] == "300;400" and data["Clicks"][30] == "12,34"
    assert data["Orders"][51] == "0"                                                    # D-27 is off by default: the zero stays


# ── Monthly_Competitor: the weekly step before Phase C ───────────────────────
def test_monthly_labels_become_weeks_for_phase_c_with_sum_and_average(flow, workbook):
    rid, _ = start(flow, workbook, "Monthly_Competitor")
    flow.run_phase(rid, "B")
    flow.accept(rid, "B")
    t = flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables").json()["tables"][0]
    assert t["weekly"] and t["from_monthly"] and t["level"] == "monthly" and "Column Setup" in t["note"]
    wb = openpyxl.load_workbook(io.BytesIO(flow.c.get(f"/api/v1/runs/{rid}/phases/C/config-template").content))
    setup = {r[0]: r[1] for r in wb["Column Setup"].iter_rows(min_row=2, values_only=True) if r[0] in ("Sales", "AOV")}
    assert setup == {"Sales": "Sum", "AOV": "Average"}                                  # guessed from the names
    assert [r[:2] for r in wb["Data File"].iter_rows(min_row=2, values_only=True)][:2] == [("Period", "Period"), ("Geography", "City")]
    buf = io.BytesIO()
    wb.save(buf)
    flow.config(rid, "C", buf.getvalue())
    flow.run_phase(rid, "C")
    c = flow.summary(rid, "C")
    assert c["rows"] > 72 and "N-08" not in c["counts"] and "N-09" not in c["counts"]
    assert c["dataset_checks"]["N-11"] == "PASS" and c["dataset_checks"]["N-12"] == "PASS"       # a continuous weekly calendar
    flow.accept(rid, "C")
    d = flow.c.get(f"/api/v1/runs/{rid}/phases/D/tables").json()["tables"]
    assert d[0]["rows"] == 72 and d[0]["level"] == "monthly"                            # D still reads the monthly table
    assert flow.c.post(f"/api/v1/runs/{rid}/phases/D/skip", json={}).status_code == 200
    weekly = final_table(flow, rid, "Monthly_Competitor")                               # the last phase that ran: Phase C's weeks
    sales = weekly["Sales"].cast(pl.Float64)
    assert sales.sum() == pytest.approx(sum(1000 + m * 13 for m in range(36)) + sum(700 + m * 13 for m in range(36)))
    assert weekly["AOV"].cast(pl.Float64).max() < 60                                    # an average stays an average: never divided
    assert {"Competitor", "City", "Period", "Sales", "AOV"} == set(weekly.columns) - {"_row_id"} or \
        {"Competitor", "City", "Period", "Sales", "AOV"} <= set(weekly.columns)


# ── Weekly_TV: Phase C findings ──────────────────────────────────────────────
def test_weekly_sheet_phase_c_findings(flow, workbook):
    rid, _ = start(flow, workbook, "Weekly_TV")
    flow.run_phase(rid, "B")
    flow.accept(rid, "B")
    flow.config(rid, "C", flow.c.get(f"/api/v1/runs/{rid}/phases/C/config-template").content)
    flow.run_phase(rid, "C")
    c = flow.summary(rid, "C")
    counts = {code: n["open"] for code, n in c["counts"].items()}
    assert counts.get("N-08", 0) >= 1 and counts.get("N-12", 0) == 1 and counts.get("N-07", 0) == 1, counts
    flow.accept(rid, "C")


# ── City_Dates: the project's inputs, city spellings, date order, an expected column ─────────────────────────────────
def test_city_spellings_date_order_and_expected_columns_through_the_web_flow(flow, workbook):
    rid, _ = start(flow, workbook, "City_Dates")
    answers = "expected_columns:" + chr(10) + "  all_sheets: [Date, City, Spend, Impressions]" + chr(10)
    saved = flow.c.put(f"/api/v1/runs/{rid}/inputs", json={"yaml": answers}).json()
    assert saved["asked"] is True and saved["tables"][0]["columns"] == ["Date", "City", "Spend", "Clicks"]
    flow.run_phase(rid, "B")
    b = flow.summary(rid, "B")
    d30 = {i["current_value"]: i for i in items(b, "D-30")}
    assert set(d30) == {"Bangalore", "BLR", "Bombay", "Poona"}                           # one item per spelling, not per cell
    assert d30["Bangalore"]["proposed_value"] == "Bengaluru" and d30["Bangalore"]["cells"] == 8 and d30["BLR"]["cells"] == 3
    assert d30["Bombay"]["proposed_value"] == "Mumbai" and d30["Poona"]["proposed_value"] == "Pune"
    d31 = items(b, "D-31")
    assert len(d31) == 1 and d31[0]["column"] == "Date" and "Pune" in d31[0]["why_flagged"]
    assert [(i["column"], i["row_id"]) for i in items(b, "D-32")] == [("Impressions", None)]
    data = final_table(flow, rid, "City_Dates")
    assert data["City"].to_list().count("Bombay") == 6 and data["Date"][0] == "2026-02-01"      # flagged, nothing changed, nothing sorted

    wb = openpyxl.load_workbook(io.BytesIO(flow.download(rid, "hygiene-review")))
    ws = wb["Needs_Review"]
    head = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        if row[head.index("Rule")].value == "D-30":
            row[head.index("Decision")].value = "Accept"                                # one answer per spelling
    buf = io.BytesIO()
    wb.save(buf)
    flow.upload_review(rid, "B", buf.getvalue())
    flow.wait(rid, "B")
    cities = final_table(flow, rid, "City_Dates")["City"].to_list()
    assert sorted(set(cities)) == ["Bengaluru", "Mumbai", "Pune"] and cities.count("Bengaluru") == 20
    flow.accept(rid, "B")
