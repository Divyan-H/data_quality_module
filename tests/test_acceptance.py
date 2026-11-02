"""Acceptance: the six steps the service exists for, on data whose right answers are known.

 1. a home screen that takes files          4. Phase C: download a pre-filled config, fill it, upload, then it runs
 2. Phase A: run, download, edit, continue  5. Phase D: same
 3. Phase B: same                           6. every final file is downloadable and traceable to the input

The final data is compared with the input workbook cell by cell; the audit files explain every difference.
"""
import io
import json
import socket
import threading
import time
import zipfile
from pathlib import Path

import openpyxl
import polars as pl
import pytest
import uvicorn

from src.api.api_server import create_app
from src.orchestration.settings import load_service_settings
from src.structure.readers import find_libreoffice

from conftest import Flow

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def filled(template: bytes, **thresholds) -> bytes:
    """What the analyst does with the downloaded template: type thresholds into the yellow cells."""
    wb = openpyxl.load_workbook(io.BytesIO(template))
    ws = wb["Data Check Specifications"]
    for row in ws.iter_rows(min_row=2):
        label = str(row[0].value or "").lower()
        for key, value in thresholds.items():
            if key in label:
                row[1].value = value
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture(scope="module")
def journey(tmp_path_factory, weekly_files):
    """The whole flow, once, the way the analyst does it: templates downloaded, filled and uploaded, nothing pre-built."""
    ws = tmp_path_factory.mktemp("acceptance")
    app = create_app(None, load_service_settings(workspace=ws / "runs"))
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        f = Flow(c)
        rid = f.create([("weekly.xlsx", weekly_files["data"])], llm="off", name="acceptance")
        steps = {}
        f.run_phase(rid, "A", {"llm": "off"})
        f.accept(rid, "A")
        f.run_phase(rid, "B")
        f.accept(rid, "B")
        tpl_c = c.get(f"/api/v1/runs/{rid}/phases/C/config-template").content
        steps["c_preview"] = next(p for p in f.config(rid, "C", tpl_c).json()["phases"] if p["key"] == "C")["config"]["preview"]
        f.run_phase(rid, "C")
        f.accept(rid, "C")
        tpl_d = c.get(f"/api/v1/runs/{rid}/phases/D/config-template").content
        steps["d_empty"] = next(p for p in f.config(rid, "D", tpl_d).json()["phases"] if p["key"] == "D")["config"]["preview"]
        mine = filled(tpl_d, **{"mom %": 40, "qoq %": 35, "hoh %": 25, "yoy %": 20, "robust": 3.5})
        steps["d_preview"] = next(p for p in f.config(rid, "D", mine).json()["phases"] if p["key"] == "D")["config"]["preview"]
        f.run_phase(rid, "D")
        steps["d_summary"] = f.summary(rid, "D")
        f.accept(rid, "D")
        yield f, rid, steps
        app.state.pipeline.shutdown()


# ── 4 and 5: the config steps ────────────────────────────────────────────────
def test_phase_c_template_is_prefilled_from_the_data_and_ready_to_use(journey):
    _, _, s = journey
    p = s["c_preview"]
    assert p["period"] == "Period" and p["variables"] == 16 and p["checked"] == 16 and not p["warnings"]


def test_an_empty_phase_d_config_is_accepted_but_says_nothing_will_run(journey):
    _, _, s = journey
    assert not any(r["active"] for r in s["d_empty"]["rules"]) and "No threshold" in s["d_empty"]["warnings"][0]


def test_thresholds_the_analyst_types_are_what_phase_d_uses(journey):
    _, _, s = journey
    active = {r["key"]: r["threshold"] for r in s["d_preview"]["rules"] if r["active"]}
    assert active == {"mom_pct": 40, "qoq_pct": 35, "hoh_pct": 25, "yoy_pct": 20, "robust_z_threshold": 3.5}
    d = s["d_summary"]
    assert d["findings_by_code"].get("N-17") and d["findings_by_code"].get("N-16")
    assert "N-18" not in d["findings_by_code"] and "N-20" not in d["findings_by_code"]      # no threshold typed: those rules did not run
    assert any("N-20" in r for r in d["inactive_rules"])


# ── 6: final files ───────────────────────────────────────────────────────────
def test_final_data_equals_the_input_cell_by_cell(journey, weekly_files):
    """Nothing was fixed (D-14 is off and the sample has no other issue), so the final table must equal the input."""
    f, rid, _ = journey
    final = pl.read_csv(io.BytesIO(f.download(rid, f.data_id(rid, "csv"))), infer_schema_length=1000)
    src = openpyxl.load_workbook(weekly_files["data"]).active
    header = [c.value for c in src[1]]
    assert final.height == src.max_row - 1
    lineage = pl.read_parquet(io.BytesIO(f.download(rid, "lineage-" + f.data_id(rid, "csv").split("-")[1])))
    assert lineage.height == final.height
    for rid_, excel_row in lineage.select(["_row_id", "_source_row"]).iter_rows():
        assert int(rid_.rsplit(":", 1)[1]) == excel_row            # Row_ID ends in the real Excel row
    by_row = dict(zip(lineage["_source_row"], final.iter_rows()))
    cols = final.columns
    for excel_row in (2, 40, 100, src.max_row):
        out = by_row[excel_row]
        for name in ("TV_Spend", "Search_Clicks", "Social_Impressions"):
            want = src.cell(excel_row, header.index(name) + 1).value
            got = out[cols.index(name)]
            assert (want is None and got is None) or float(want) == pytest.approx(float(got)), (excel_row, name)


def test_every_difference_is_in_the_audit(journey):
    f, rid, _ = journey
    assert json.loads(f.download(rid, "hygiene-audit")) == []             # nothing was changed silently or otherwise


def test_zip_contains_exactly_what_the_results_page_lists(journey):
    f, rid, _ = journey
    listed = f.c.get(f"/api/v1/runs/{rid}/results").json()["files"]
    z = zipfile.ZipFile(io.BytesIO(f.c.get(f"/api/v1/runs/{rid}/bundle").content))
    manifest = json.loads(z.read("MANIFEST.json"))["files"]
    assert len(manifest) == len(listed)
    names = {Path(m["path"]).name.split("_", 1)[-1] if m["path"].startswith(("reports", "audit", "config", "logs")) else Path(m["path"]).name
             for m in manifest}
    assert {"Structural_Review.xlsx", "Hygiene_Review.xlsx", "pipeline.log"} <= names
    assert any(n.startswith("Data_Validation_Report_") for n in names) and any(n.startswith("Spend_Support_Trend_Report_") for n in names)
    assert any(n.endswith(".csv") for n in names)


def test_the_pipeline_log_tells_the_story_of_phases_b_to_d(journey):
    f, rid, _ = journey
    log = f.download(rid, "pipeline-log").decode()
    assert "phase B done" in log and "phase C done" in log and "phase D done" in log


# ── 2 and 3: the review loop on data with known problems ─────────────────────
def test_formula_problems_reach_the_analyst_with_the_real_excel_row(flow):
    """Phase B's planted workbook has formulas with no saved result (D-05) and a link to another workbook (D-06):
    the review workbook must name the formula and the input's real Excel row, through the whole service."""
    import sys
    sys.path.insert(0, str(FIXTURES))
    import build_phase_b_workbook as wbk
    import tempfile
    d = Path(tempfile.mkdtemp())
    xlsx = d / "issues.xlsx"
    wbk.build_workbook(xlsx)
    rid = flow.create([("issues.xlsx", xlsx)])
    flow.run_phase(rid, "A", {"llm": "off"})
    flow.accept(rid, "A")
    flow.run_phase(rid, "B")
    wb = openpyxl.load_workbook(io.BytesIO(flow.download(rid, "hygiene-review")))
    src = openpyxl.load_workbook(xlsx).active
    rows = list(wb["Needs_Review"].iter_rows(values_only=True))
    assert rows[0][:2] == ("Row_ID", "Excel_Row")
    for row_id, excel_row, *_ in rows[1:]:
        if row_id is not None:                                       # column-level items name no row
            assert int(row_id.rsplit(":", 1)[1]) == excel_row
    d05 = [r for r in rows if r[4] == "D-05"]
    d06 = [r for r in rows if r[4] == "D-06"]
    assert d05 and d06
    total = [c.value for c in src[1]].index("Total") + 1
    assert all(str(src.cell(r[1], total).value).startswith("=") for r in d05 + d06)
    assert "=H32*2" in d05[0][6] and "does not calculate" in d05[0][6]
    assert all("[budget.xlsx]" in r[6] and "depends on a file the pipeline does not have" in r[6] for r in d06)


def test_two_files_in_one_run_each_become_tables_and_the_analyst_picks_one_for_c(flow, weekly_files):
    wb = openpyxl.load_workbook(weekly_files["data"])
    ws = wb.active
    ws["A1"].value = "Period"
    buf = io.BytesIO()
    wb.save(buf)
    rid = flow.create([("one.xlsx", weekly_files["data"]), ("two.xlsx", buf.getvalue())])
    flow.run_phase(rid, "A", {"llm": "off"})
    assert flow.summary(rid, "A")["selected_tables"] == 2 and flow.summary(rid, "A")["files"] == 2
    flow.accept(rid, "A")
    flow.run_phase(rid, "B")
    assert len(flow.summary(rid, "B")["tables"]) == 2
    flow.accept(rid, "B")
    tables = flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables").json()["tables"]
    assert {t["file"] for t in tables} == {"one.xlsx", "two.xlsx"}
    ok = flow.c.get(f"/api/v1/runs/{rid}/phases/C/config-template")           # one template for both tables
    assert ok.status_code == 200
    r = flow.c.post(f"/api/v1/runs/{rid}/phases/C/config", files={"file": ("c.xlsx", ok.content)})
    assert r.status_code == 200
    flow.run_phase(rid, "C")
    assert {t["table"] for t in flow.c.get(f"/api/v1/runs/{rid}/phases/C/summary").json()["tables"]} == {t["ref"] for t in tables}


# ── legacy formats ───────────────────────────────────────────────────────────
@pytest.mark.skipif(find_libreoffice() is None, reason="LibreOffice is not installed")
@pytest.mark.parametrize("name", ["excel_plain_legacy.xls"])
def test_legacy_xls_is_converted_and_cleaned(flow, name):
    rid = flow.create([(name, FIXTURES / name)])
    p = flow.run_phase(rid, "A", {"llm": "off"})
    s = flow.summary(rid, "A")
    assert s["selected_tables"] >= 1 and s["severity"]["block"] == 0 and p["can_continue"]
    assert any("Converted from xls" in i["What was found"] for i in s["issues"])
    flow.accept(rid, "A")
    flow.run_phase(rid, "B")


@pytest.mark.skipif(find_libreoffice() is None, reason="LibreOffice is not installed")
def test_ods_is_converted_and_gives_the_same_table_as_the_xlsx(flow, weekly_files, tmp_path):
    import subprocess
    subprocess.run([find_libreoffice(), "--headless", f"-env:UserInstallation=file:///{(tmp_path / 'p').as_posix()}",
                    "--convert-to", "ods", "--outdir", str(tmp_path), str(weekly_files["data"])], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    ods = tmp_path / (weekly_files["data"].stem + ".ods")
    a = flow.create([("weekly.ods", ods)])
    b = flow.create([("weekly.xlsx", weekly_files["data"])])
    for rid in (a, b):
        flow.run_phase(rid, "A", {"llm": "off"})
    sa, sb = flow.summary(a, "A"), flow.summary(b, "A")
    assert (sa["tables"][0]["rows"], sa["tables"][0]["columns"]) == (sb["tables"][0]["rows"], sb["tables"][0]["columns"]) == (158, 18)


@pytest.mark.skipif(find_libreoffice() is None, reason="LibreOffice is not installed")
def test_an_encrypted_legacy_file_is_blocked_with_a_clear_reason(flow):
    rid = flow.create([("locked.xls", FIXTURES / "excel_encrypted_legacy.xls")])
    p = flow.run_phase(rid, "A", {"llm": "off"})
    s = flow.summary(rid, "A")
    assert s["selected_tables"] == 0 and not p["can_continue"]
    assert any("password" in b.lower() for b in s["blocking"])


def test_without_libreoffice_a_legacy_file_is_reported_not_crashed(flow, monkeypatch, tmp_path):
    """Phase A's own report when the converter is missing (the home screen refuses these files earlier)."""
    from src.structure import readers
    from src.structure.errors import FileUnreadable
    monkeypatch.setattr(readers, "find_libreoffice", lambda: None)
    with pytest.raises(FileUnreadable, match="needs LibreOffice"):
        readers.convert_to_xlsx(str(FIXTURES / "excel_plain_legacy.xls"), str(tmp_path))


def test_options_tell_the_home_screen_whether_legacy_files_can_be_read(client):
    lim = client.get("/api/v1/options").json()["limits"]
    assert lim["legacy_converter"] is (find_libreoffice() is not None)
    assert set(lim["legacy_extensions"]) == {".xls", ".xlsb", ".ods"}


# ── 1: the app itself is served ──────────────────────────────────────────────
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not (ROOT / "frontend" / "dist" / "index.html").exists(), reason="frontend not built")
def test_a_real_server_serves_the_app_and_the_api_on_one_port(tmp_path):
    import httpx
    port = _free_port()
    app = create_app(None, load_service_settings(workspace=tmp_path / "runs"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/api/v1/health").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        home = httpx.get(base + "/")
        assert home.status_code == 200 and "<div id=\"root\">" in home.text
        js = next(a for a in home.text.split('"') if a.startswith("/assets/") and a.endswith(".js"))
        assert httpx.get(base + js).status_code == 200
        assert httpx.get(base + "/run/abc/B").status_code == 200            # client-side routes fall back to the app
        assert httpx.get(base + "/api/v1/nothing").status_code == 404       # the API does not
        assert httpx.get(base + "/health").json() == {"status": "ok"}
    finally:
        server.should_exit = True
        t.join(10)


# ── data that is not weekly: say so, skip what does not apply, carry on ──────────────────────────────────────────
def _monthly_book(path, measures: bool, real_date: bool):
    """Competitor / Segment / City / Year / Month / Year-Month (+ optionally a real date and Sales, Spend)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    head = ["Competitor", "Segment", "City"] + (["Period"] if real_date else []) + ["Year", "Month", "Year-Month"] + (["Sales", "Spend"] if measures else [])
    ws.append(head)
    from datetime import date as d
    for i in range(36):
        y, m = 2022 + i // 12, i % 12 + 1
        for comp in ("A", "B"):
            ws.append([comp, "Retail", "Pune"] + ([d(y, m, 1)] if real_date else []) + [y, m, f"{y}-{m:02d}"]
                      + ([1000 + i * 13 + (5 if comp == "B" else 0), 200 + i] if measures else []))
    wb.save(path)
    return path


def _through_b(flow, path, name="monthly.xlsx"):
    rid = flow.create([(name, path)])
    for key in "AB":
        flow.run_phase(rid, key, {"llm": "off"})
        flow.accept(rid, key)
    return rid


def test_a_table_with_only_year_and_month_labels_skips_c_and_d_and_still_delivers(flow, tmp_path):
    rid = _through_b(flow, _monthly_book(tmp_path / "m.xlsx", measures=False, real_date=False))
    c = flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables").json()["tables"]
    assert [(t["weekly"], t["level"]) for t in c] == [(False, "monthly")]
    assert "monthly, not weekly" in c[0]["note"] and "no numeric measure" in c[0]["note"]
    r = flow.c.post(f"/api/v1/runs/{rid}/phases/C/skip", json={"reason": c[0]["note"]})
    assert r.status_code == 200
    ph = next(p for p in r.json()["phases"] if p["key"] == "C")
    assert ph["status"] == "accepted" and ph["skipped"] and "monthly, not weekly" in ph["skip_reason"]
    assert next(p for p in r.json()["phases"] if p["key"] == "D")["status"] == "ready"     # D unlocked
    d = flow.c.get(f"/api/v1/runs/{rid}/phases/D/tables").json()["tables"]               # D reads Phase B's table now
    assert [(t["weekly"], t["level"]) for t in d] == [(False, "monthly")]
    assert flow.c.post(f"/api/v1/runs/{rid}/phases/D/skip", json={}).status_code == 200
    v = flow.view(rid)
    assert v["complete"] and [p["skipped"] for p in v["phases"]] == [False, False, True, True]
    res = flow.c.get(f"/api/v1/runs/{rid}/results").json()
    ids = {x["id"] for x in res["files"]}
    assert res["complete"] and {flow.data_id(rid, "csv"), "hygiene-review", "structural-review"} <= ids
    assert not [i for i in ids if i.startswith(("timeseries-report", "trends-report"))]                            # nothing pretends C or D ran
    assert {x["phase"] for x in res["files"] if x["kind"] == "data"} == {"B"}   # labelled with the phase that made it
    final = pl.read_csv(io.BytesIO(flow.download(rid, flow.data_id(rid, "csv"))))
    assert final.height == 72 and {"Competitor", "Year-Month"} <= set(final.columns)    # the cleaned Phase B table
    z = zipfile.ZipFile(io.BytesIO(flow.c.get(f"/api/v1/runs/{rid}/bundle").content))
    assert not [n for n in z.namelist() if "Data_Validation" in n or "Spend_Support" in n]
    assert flow.c.get(f"/api/v1/runs/{rid}/phases/C/summary").status_code == 404


def test_skipping_follows_the_order_and_only_applies_to_c_and_d(flow, weekly_files):
    rid = flow.create([("w.xlsx", weekly_files["data"])])
    assert flow.c.post(f"/api/v1/runs/{rid}/phases/A/skip", json={}).json()["error"]["code"] == "not_skippable"
    assert flow.c.post(f"/api/v1/runs/{rid}/phases/C/skip", json={}).json()["error"]["code"] == "locked"
    for key in "AB":
        flow.run_phase(rid, key, {"llm": "off"})
        flow.accept(rid, key)
    flow.config(rid, "C", weekly_files["config"].read_bytes())
    flow.run_phase(rid, "C")
    err = flow.c.post(f"/api/v1/runs/{rid}/phases/C/skip", json={}).json()["error"]
    assert err["code"] == "has_results"                                                  # a finished phase is accepted, not skipped


def test_a_skipped_phase_can_still_be_run_afterwards(flow, weekly_files):
    rid = flow.create([("w.xlsx", weekly_files["data"])])
    for key in "AB":
        flow.run_phase(rid, key, {"llm": "off"})
        flow.accept(rid, key)
    assert flow.c.post(f"/api/v1/runs/{rid}/phases/C/skip", json={}).status_code == 200
    flow.config(rid, "C", weekly_files["config"].read_bytes())
    flow.start(rid, "C", {"restart": True})
    c = flow.wait(rid, "C")
    assert c["status"] == "review" and c["skipped"] is False and c["skip_reason"] is None


def test_weekly_data_is_not_flagged_as_inapplicable(flow, weekly_files):
    rid = _through_b(flow, weekly_files["data"], "w.xlsx")
    t = flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables").json()["tables"]
    assert [(x["weekly"], x["level"]) for x in t] == [(True, "weekly")]


def test_monthly_data_with_a_real_date_is_not_blocked_by_c_and_d_can_run(flow, tmp_path):
    rid = _through_b(flow, _monthly_book(tmp_path / "m2.xlsx", measures=True, real_date=True))
    c = flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables").json()["tables"]
    assert [(t["weekly"], t["level"]) for t in c] == [(True, "monthly")]               # named monthly, still allowed
    flow.c.post(f"/api/v1/runs/{rid}/phases/C/skip", json={})                           # the analyst may still choose to skip
    d = flow.c.get(f"/api/v1/runs/{rid}/phases/D/tables").json()["tables"]
    assert [(t["weekly"], t["level"]) for t in d] == [(True, "monthly")]
    tpl = flow.c.get(f"/api/v1/runs/{rid}/phases/D/config-template").content
    msg = flow.config(rid, "D", tpl, expect=422).json()["error"]["problems"][0]["message"]
    assert "Support variable" in msg and "Spend Variables" in msg                       # says what to fill, not a code error
    wb = openpyxl.load_workbook(io.BytesIO(tpl))
    ws = wb["Variable Mapping"]
    head = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        if row[head.index("Variables")].value == "Sales":
            row[head.index("Spend Variables")].value = "Spend"
            row[head.index("Y/N")].value = "Y"
    buf = io.BytesIO()
    wb.save(buf)
    pv = next(p for p in flow.config(rid, "D", filled(buf.getvalue(), **{"mom %": 5, "yoy %": 20})).json()["phases"] if p["key"] == "D")["config"]["preview"]
    assert pv["pairs"] == 1
    flow.run_phase(rid, "D")
    s = flow.summary(rid, "D")
    assert s["pairs"]["run"] == 1 and s["rows"] == 72
    flow.accept(rid, "D")
    assert flow.view(rid)["complete"]


def test_monthly_labels_with_measures_get_a_derived_date_and_phase_d_runs(flow, tmp_path):
    """Year-Month labels and numbers but no date column (the shape of a monthly competitor export): C turns it into
    weeks and may run; here C is skipped, and D builds a month-start Period from the labels and runs on it."""
    rid = _through_b(flow, _monthly_book(tmp_path / "m3.xlsx", measures=True, real_date=False))
    assert flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables").json()["tables"][0]["weekly"] is True
    flow.c.post(f"/api/v1/runs/{rid}/phases/C/skip", json={})
    d = flow.c.get(f"/api/v1/runs/{rid}/phases/D/tables").json()["tables"]
    assert [(t["weekly"], t["level"]) for t in d] == [(True, "monthly")]                 # applicable now: a Period is derived
    tpl = flow.c.get(f"/api/v1/runs/{rid}/phases/D/config-template").content
    wb = openpyxl.load_workbook(io.BytesIO(tpl))
    assert [r[:2] for r in wb["Data File"].iter_rows(min_row=2, values_only=True)] == [("Period", "Period"), ("Geography", "City"), ("Variables", "Sales")]
    assert [r[0] for r in wb["Variable Mapping"].iter_rows(min_row=2, values_only=True)] == ["Sales", "Spend"]   # Year and Month are labels, not measures
    ws = wb["Variable Mapping"]
    head = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        if row[head.index("Variables")].value == "Sales":
            row[head.index("Spend Variables")].value = "Spend"
            row[head.index("Y/N")].value = "Y"
    buf = io.BytesIO()
    wb.save(buf)
    cfg = next(p for p in flow.config(rid, "D", filled(buf.getvalue(), **{"mom %": 5, "yoy %": 20})).json()["phases"] if p["key"] == "D")["config"]
    assert cfg["preview"]["period"] == "Period" and cfg["preview"]["pairs"] == 1
    flow.run_phase(rid, "D")
    s = flow.summary(rid, "D")
    assert s["rows"] == 72 and s["pairs"] == {"mapped": 1, "run": 1, "with_spend": 1}
    assert s["counts"]["N-17"]["open"] == 24 and set(s["counts"]) == {"N-17"}           # only MOM and YOY had thresholds
    flow.accept(rid, "D")
    final = pl.read_csv(io.BytesIO(flow.download(rid, flow.data_id(rid, "csv", "Sheet"))))
    assert final.height == 72 and "Period" in final.columns and final["Period"][0] == "2022-01-01"
    assert {x["phase"] for x in flow.c.get(f"/api/v1/runs/{rid}/results").json()["files"] if x["kind"] == "data"} == {"D"}
    # monthly data also comes out as Monday-to-Sunday weeks (an extra file; the monthly table above is unchanged)
    weekly = pl.read_csv(io.BytesIO(flow.download(rid, flow.data_id(rid, "csv", "Sheet_weekly"))))
    assert {"Week_Start", "Segment_Start", "Days", "Days_In_Month"} <= set(weekly.columns) and weekly.height > final.height
    assert weekly["Sales"].sum() == pytest.approx(final["Sales"].sum())          # nothing gained or lost by the split
    assert flow.summary(rid, "D")["weekly"][0]["rows_in"] == 72
    assert any(x["id"].startswith("trends-weekly-") and x["kind"] == "audit" for x in flow.c.get(f"/api/v1/runs/{rid}/results").json()["files"])


def test_monthly_labels_are_made_weekly_for_phase_c_and_d_keeps_the_monthly_table(flow, tmp_path):
    """The user's case: a crosstab with only Year-Month labels. Phase C runs on Monday-to-Sunday weeks calculated from the
    months; Phase D still checks the monthly figures."""
    rid = _through_b(flow, _monthly_book(tmp_path / "m4.xlsx", measures=True, real_date=False))
    t = flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables").json()["tables"][0]
    assert t["weekly"] is True and t["from_monthly"] is True and t["level"] == "monthly" and "Column Setup" in t["note"]
    wb = openpyxl.load_workbook(io.BytesIO(flow.c.get(f"/api/v1/runs/{rid}/phases/C/config-template").content))
    assert [r[:2] for r in wb["Data File"].iter_rows(min_row=2, values_only=True)] == [
        ("Period", "Period"), ("Geography", "City"), ("Variables", "Sales")]
    assert [r[:2] for r in wb["Column Setup"].iter_rows(min_row=2, max_row=3, values_only=True)] == [("Sales", "Sum"), ("Spend", "Sum")]
    wb["Column Setup"]["B3"] = "Average"                                               # the analyst: Spend is a rate here
    buf = io.BytesIO()
    wb.save(buf)
    flow.config(rid, "C", buf.getvalue())
    flow.run_phase(rid, "C")
    s = flow.summary(rid, "C")
    assert s["rows"] > 72 and s["layout"]["period"] == "Period"                  # weeks, not the 72 monthly rows
    assert "N-08" not in s["counts"] and "N-09" not in s["counts"]                     # repeats by construction are not findings
    assert s["dataset_checks"]["N-12"] == "PASS" and s["dataset_checks"]["N-11"] == "PASS"   # a continuous weekly calendar
    flow.accept(rid, "C")
    d = flow.c.get(f"/api/v1/runs/{rid}/phases/D/tables").json()["tables"]
    assert [(x["weekly"], x["level"]) for x in d] == [(True, "monthly")]               # D reads the monthly table, not C's weeks
    assert d[0]["rows"] == 72


def test_daily_data_is_allowed_through_phase_c_and_runs(flow, tmp_path):
    """Daily time series (dd-mm-yyyy dates) must go through Phase C: nothing may refuse it for not being weekly."""
    from datetime import date, timedelta
    lines = ["Date (DD-MM-YYYY),Geography,Impressions,Spend,Taps"]
    for i in range(120):
        d = date(2024, 1, 1) + timedelta(days=i)
        lines.append(f"{d.strftime('%d-%m-%Y')},IN,{10000 + i * 37 % 900},{500 + i * 3 % 70},{300 + i * 7 % 50}")
    (tmp_path / "daily.csv").write_text(chr(10).join(lines), encoding="utf8")
    rid = _through_b(flow, tmp_path / "daily.csv", "daily.csv")
    t = flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables").json()["tables"]
    assert [(x["weekly"], x["level"]) for x in t] == [(True, "daily")]                  # offered, not refused
    tpl = flow.c.get(f"/api/v1/runs/{rid}/phases/C/config-template")
    assert tpl.status_code == 200
    flow.config(rid, "C", tpl.content)
    flow.run_phase(rid, "C")
    s = flow.summary(rid, "C")
    assert s["rows"] == 120 and s["layout"]["period"] == "Date (DD-MM-YYYY)" and s["variables"]["in_range"] == 3


def _daily_book(path):
    """40 days for one city: a Rain column and a Sales column each with one isolated zero (day 11), and Search + Social +
    Display + All media where All media is their sum."""
    import random
    from datetime import date as d, timedelta
    rnd = random.Random(4)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Date", "City", "Sales", "Rain", "Search", "Social", "Display", "All media"])
    for k in range(40):
        a, b, c = (rnd.randint(20, 90) for _ in range(3))
        ws.append([d(2026, 3, 2) + timedelta(days=k), "Pune", 0 if k == 10 else 100 + 2 * k, 0 if k == 10 else 5 + k % 4, a, b, c, a + b + c])
    wb.save(path)
    return path


def test_zero_valid_columns_are_chosen_on_the_start_screen_and_isolated_zeros_filled_and_totals_flagged(flow, tmp_path):
    rid = flow.create([("daily.xlsx", _daily_book(tmp_path / "daily.xlsx"))])
    flow.run_phase(rid, "A", {"llm": "off"})
    assert flow.c.get(f"/api/v1/runs/{rid}/phases/B/columns").status_code == 409          # Phase A not accepted yet
    flow.accept(rid, "A")
    tables = flow.c.get(f"/api/v1/runs/{rid}/phases/B/columns").json()["tables"]
    assert len(tables) == 1 and tables[0]["dated"] and tables[0]["period"] == "Date"
    cols = {c["name"]: c for c in tables[0]["columns"]}
    assert {"Sales", "Rain", "Search", "All media"} <= set(cols) and "Date" not in cols
    assert cols["Rain"]["zeros"] == 1 and cols["Rain"]["likely_zero_valid"] is False           # one zero in 40 is not "mostly zero"
    ref = tables[0]["ref"]
    flow.run_phase(rid, "B", {"fixes": {"D-27": True}, "zero_expected": {ref: ["Rain"]}})
    p = flow.phase(rid, "B")
    assert p["params"]["fixes"]["D-27"] is True and p["params"]["zero_expected"] == {ref: ["Rain"]}
    s = flow.summary(rid, "B")
    by_rule = {r["rule"]: r for r in s["by_rule"]}
    assert by_rule["D-27"]["fixed"] == 1 and s["zero_expected"] == {ref: ["Rain"]}
    final = pl.read_csv(io.BytesIO(flow.download(rid, flow.data_id(rid, "csv"))))
    assert final["Sales"][10] == 120 and final["Rain"][10] == 0                         # filled from 118 and 122 / kept: zero is valid
    items = [i for i in s["needs_review"] if i["rule"] == "D-27"]
    assert [(i["column"], i["original_value"], i["current_value"]) for i in items] == [("Sales", "0", "120")]   # shown to the analyst
    assert [(b["bucket"], sorted(b["sum_these"]), b["do_not_add_with_them"]) for b in s["buckets"]] ==         [("Bucket 1", ["Display", "Search", "Social"], ["All media"])]
    assert "hygiene-buckets" in {f["id"] for f in flow.c.get(f"/api/v1/runs/{rid}/results").json()["files"]}
    review = openpyxl.load_workbook(io.BytesIO(flow.download(rid, "hygiene-review")))
    assert {"D-27", "D-28"} <= {r[4] for r in review["Needs_Review"].iter_rows(min_row=2, values_only=True)}
    assert any(r[0] and "Zero is a valid value" in str(r[0]) for r in review["Summary"].iter_rows(values_only=True))


def test_a_bad_zero_expected_choice_is_refused(flow, tmp_path):
    rid = flow.create([("daily.xlsx", _daily_book(tmp_path / "daily.xlsx"))])
    flow.run_phase(rid, "A", {"llm": "off"})
    flow.accept(rid, "A")
    assert flow.c.post(f"/api/v1/runs/{rid}/phases/B/start", json={"zero_expected": {"t": "Rain"}}).status_code == 422
