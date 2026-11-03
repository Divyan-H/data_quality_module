"""The analyst web API end to end: real app, real worker processes, planted data with known findings.

One module-scoped run (A -> D, LLM off) backs the happy-path assertions; the other tests make their own small runs.
"""
import io
import json
import zipfile

import openpyxl
import polars as pl
import pytest
from fastapi.testclient import TestClient

from src.api.api_server import create_app
from src.orchestration.settings import load_service_settings

from conftest import Flow


@pytest.fixture(scope="module")
def finished(tmp_path_factory, weekly_files):
    """A -> D accepted on the planted workbook. Returns (flow, run_id)."""
    ws = tmp_path_factory.mktemp("ws")
    app = create_app(None, load_service_settings(workspace=ws / "runs"))
    with TestClient(app) as c:
        f = Flow(c)
        rid = f.create([("weekly.xlsx", weekly_files["data"])], llm="off", name="planted")
        for key in "AB":
            f.run_phase(rid, key, {"llm": "off"})
            f.accept(rid, key)
        cfg = weekly_files["config"].read_bytes()
        for key in "CD":
            f.config(rid, key, cfg)
            f.run_phase(rid, key)
            f.accept(rid, key)
        yield f, rid
        app.state.pipeline.shutdown()


# ------------------------------------------------------------------ the happy path
def test_all_four_phases_complete_and_the_run_is_complete(finished):
    f, rid = finished
    v = f.view(rid)
    assert [p["status"] for p in v["phases"]] == ["accepted"] * 4 + ["ready"] and v["complete"]   # T is optional
    assert v["job"] is None


def test_phase_d_finds_exactly_the_planted_issues(finished, weekly_files):
    """With Phase B's outlier capping off (the service default) Phase D sees all 14 planted findings, not the 5 it
    sees after D-14 has capped the planted spikes (docs/dq_d_implementation_notes.md)."""
    f, rid = finished
    s = f.summary(rid, "D")
    assert s["totals"]["findings_open"] == len(weekly_files["wbk"].EXPECTED) == 14
    assert s["findings_by_code"] == {"N-16": 3, "N-17": 5, "N-18": 3, "N-19": 2, "N-20": 1}


def test_phase_b_capping_is_off_by_default_and_was_recorded(finished):
    f, rid = finished
    fixes = f.phase(rid, "B")["params"]["fixes"]
    assert fixes["D-14"] is False and fixes["D-09"] is True
    assert f.summary(rid, "B")["totals"]["cells_fixed"] == 0


def test_every_phase_summary_has_its_headline_numbers(finished):
    f, rid = finished
    a, b, c, d = (f.summary(rid, k) for k in "ABCD")
    assert a["selected_tables"] == 1 and a["rows_out"] == 158 and a["llm"]["mode"] == "off"
    assert b["tables"][0]["rows_out"] == 158
    # the default run inputs expect date, platform, subplatform and city; the sample has no sheet with all of them
    assert sum(i["rule"] != "D-32" for i in b["needs_review"]) == 3
    assert b["needs_review_total"] == 3 + sum(i["rule"] == "D-32" for i in b["needs_review"])
    assert c["rows"] == 158 and c["variables"]["in_range"] == 16 and c["variables"]["checked"] > 0 and c["dataset_checks"]["N-14"] == "FAIL"
    assert d["pairs"]["run"] > 0 and d["rows"] == 158


def test_the_llm_choice_is_recorded_per_phase(finished):
    f, rid = finished
    llm = f.phase(rid, "A")["llm"]
    assert llm["requested"] == "off" and llm["used"] is False and "without the LLM" in llm["reason"]
    assert [f.phase(rid, k)["llm"] for k in "BCD"] == [None, None, None]       # only Phase A has an LLM step


def test_results_list_every_final_file_and_each_downloads(finished):
    f, rid = finished
    res = f.c.get(f"/api/v1/runs/{rid}/results").json()
    assert res["complete"]
    ids = {x["id"] for x in res["files"]}
    assert {"structural-review", "hygiene-review", "pipeline-log"} <= ids and {f.data_id(rid, e) for e in ("csv", "parquet", "xlsx")} <= ids
    assert any(i.startswith("timeseries-report-") for i in ids) and any(i.startswith("trends-report-") for i in ids)
    assert {x["group"] for x in res["files"]} == {"Final data", "Reports", "Audit trail", "Configs used", "Logs"}
    for x in res["files"]:
        assert len(f.download(rid, x["id"])) == x["bytes"] > 0 or x["bytes"] == 0


def test_the_final_table_is_the_checked_table_with_its_row_ids(finished):
    f, rid = finished
    df = pl.read_csv(io.BytesIO(f.download(rid, f.data_id(rid, "csv"))))
    assert df.height == 158 and not [c for c in df.columns if c.startswith("_")]          # data columns only
    parquet = pl.read_parquet(io.BytesIO(f.download(rid, f.data_id(rid, "parquet"))))
    assert parquet.height == 158 and parquet.columns == df.columns
    wb = openpyxl.load_workbook(io.BytesIO(f.download(rid, f.data_id(rid, "xlsx"))))
    assert wb["data"].max_row == 159
    lineage = pl.read_parquet(io.BytesIO(f.download(rid, next(x["id"] for x in f.c.get(f"/api/v1/runs/{rid}/results").json()["files"]
                                                              if x["id"].startswith("lineage-")))))
    assert lineage.height == 158 and lineage["_row_id"].n_unique() == 158                  # permanent Row_IDs, in file order


def test_review_workbooks_open_in_excel_tooling(finished):
    f, rid = finished
    ids = [x["id"] for x in f.c.get(f"/api/v1/runs/{rid}/results").json()["files"]]
    for prefix, sheet in (("structural-review", "Structural_Findings"), ("hygiene-review", "Needs_Review"),
                          ("timeseries-report-", "Overall_Summary"), ("trends-report-", "Rules_Used")):
        art = next(i for i in ids if i.startswith(prefix))
        wb = openpyxl.load_workbook(io.BytesIO(f.download(rid, art)), read_only=True)
        assert sheet in wb.sheetnames, (art, wb.sheetnames)


def test_the_zip_holds_every_file_with_matching_checksums(finished):
    import hashlib
    f, rid = finished
    r = f.c.get(f"/api/v1/runs/{rid}/bundle")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    manifest = json.loads(z.read("MANIFEST.json"))
    assert manifest["files"] and all(hashlib.sha256(z.read(e["path"])).hexdigest() == e["sha256"] for e in manifest["files"])
    names = z.namelist()
    assert any(n.startswith("reports/A_Structural_Review") for n in names)
    assert any(n.startswith("data/") and n.endswith(".csv") for n in names) and len(names) == len(manifest["files"]) + 1


def test_live_events_were_recorded_in_order_for_every_phase(finished):
    f, rid = finished
    ev = f.c.get(f"/api/v1/runs/{rid}/events?after=0").json()
    assert [e["seq"] for e in ev["events"]] == list(range(1, len(ev["events"]) + 1))
    assert {e["phase"] for e in ev["events"]} == set("ABCD")
    stages = [e["stage"] for e in ev["events"] if e["type"] == "stage" and e["phase"] == "A"]
    assert stages[0] == "starting" and "finalizing" in stages and stages[-1] == "summarising"
    later = f.c.get(f"/api/v1/runs/{rid}/events?after={ev['cursor']}").json()
    assert later["events"] == [] and later["cursor"] == ev["cursor"]


# ------------------------------------------------------------------ ordering rules
def test_phases_cannot_be_started_out_of_order(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    r = flow.start(rid, "B", {"llm": "off"}, expect=409)
    assert r.json()["error"]["code"] == "locked"
    flow.start(rid, "C", expect=409)
    assert flow.accept(rid, "A", expect=409).json()["error"]["code"] == "not_reviewable"


def test_a_finished_phase_must_be_restarted_deliberately(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    flow.run_phase(rid, "A", {"llm": "off"})
    err = flow.start(rid, "A", {"llm": "off"}, expect=409).json()["error"]
    assert err["code"] == "restart_required"
    flow.start(rid, "A", {"llm": "off", "restart": True})
    assert flow.wait(rid, "A")["attempt"] == 2


def test_restarting_a_phase_discards_the_later_ones(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    for key in "AB":
        flow.run_phase(rid, key, {"llm": "off"})
        flow.accept(rid, key)
    flow.start(rid, "A", {"llm": "off", "restart": True})
    flow.wait(rid, "A")
    assert [p["status"] for p in flow.view(rid)["phases"]] == ["review", "locked", "locked", "locked", "locked"]
    assert not (flow.c.app.state.pipeline.store.path(rid) / "work" / "phase_b_hygiene").exists()


def test_only_one_job_runs_per_run(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    flow.start(rid, "A", {"llm": "off"})
    r = flow.c.post(f"/api/v1/runs/{rid}/phases/A/start", json={"llm": "off", "restart": True})
    assert r.status_code in (409, 429) and r.json()["error"]["code"] == "busy"
    flow.wait(rid, "A")


# ------------------------------------------------------------------ the LLM question
def test_an_unreachable_llm_asks_instead_of_silently_falling_back(flow, weekly_files, monkeypatch):
    monkeypatch.setenv("STRUCTURE_LLM_URL", "http://127.0.0.1:9")
    rid = flow.create([("weekly.xlsx", weekly_files["data"])], llm="on")
    err = flow.start(rid, "A", {}, expect=409).json()["error"]
    assert err["code"] == "llm_unavailable" and "ollama serve" in err["message"]
    assert err["llm"]["available"] is False and err["llm"]["reason"] == "llm_unreachable"
    assert flow.phase(rid, "A")["status"] == "ready"                      # nothing started, nothing changed


def test_continuing_without_the_llm_runs_the_rules_and_says_so(flow, weekly_files, monkeypatch):
    monkeypatch.setenv("STRUCTURE_LLM_URL", "http://127.0.0.1:9")
    rid = flow.create([("weekly.xlsx", weekly_files["data"])], llm="on")
    flow.start(rid, "A", {}, expect=409)
    p = flow.run_phase(rid, "A", {"llm": "off"})
    assert p["llm"] == {"requested": "off", "used": False, "model": None, "calls": 0,
                        "reason": "Continued without the LLM (rule-based fallback), as chosen."}
    assert flow.summary(rid, "A")["llm"]["mode"] == "off"
    flow.accept(rid, "A")
    flow.run_phase(rid, "B")                                               # Phase B has no LLM step: nothing to ask


def test_the_llm_is_never_needed_by_phases_b_c_and_d(flow, weekly_files, monkeypatch):
    monkeypatch.setenv("STRUCTURE_LLM_URL", "http://127.0.0.1:9")
    rid = flow.create([("weekly.xlsx", weekly_files["data"])], llm="on")
    flow.run_phase(rid, "A", {"llm": "off"})
    flow.accept(rid, "A")
    flow.run_phase(rid, "B")                                              # no llm option, no llm check
    flow.accept(rid, "B")
    flow.config(rid, "C", weekly_files["config"].read_bytes())
    flow.run_phase(rid, "C")


# ------------------------------------------------------------------ the config workbooks (C and D)
def _two_phases(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    for key in "AB":
        flow.run_phase(rid, key, {"llm": "off"})
        flow.accept(rid, key)
    return rid


def test_the_prefilled_template_fits_the_data_and_is_accepted_as_is(flow, weekly_files):
    rid = _two_phases(flow, weekly_files)
    tpl = flow.c.get(f"/api/v1/runs/{rid}/phases/C/config-template")
    assert tpl.status_code == 200 and tpl.content[:2] == b"PK"
    assert "phase_C_client_config_prefilled.xlsx" in tpl.headers["content-disposition"]
    v = flow.config(rid, "C", tpl.content).json()
    cfg = next(p for p in v["phases"] if p["key"] == "C")["config"]
    assert cfg["valid"] and cfg["preview"]["variables"] == 16 and cfg["preview"]["checked"] == 16


def test_config_step_requests_that_overlap_wait_for_each_other(flow, weekly_files):
    """The table list, the template and the config check are each a job, and a run runs one job at a time. Clicking
    Download while the table list is still loading used to fail with 'A job is in progress'; now it waits its turn."""
    import threading
    rid = _two_phases(flow, weekly_files)
    codes = {}

    def call(name, fn):
        codes[name] = fn().status_code

    template_url = f"/api/v1/runs/{rid}/phases/C/config-template"
    threads = [threading.Thread(target=call, args=("tables", lambda: flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables"))),
               threading.Thread(target=call, args=("template", lambda: flow.c.get(template_url))),
               threading.Thread(target=call, args=("template2", lambda: flow.c.get(template_url)))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == {"tables": 200, "template": 200, "template2": 200}
    assert flow.view(rid)["job"] is None
    again = flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables")                  # the table list is now remembered
    assert again.status_code == 200 and len(again.json()["tables"]) == 1


def test_phase_c_will_not_start_without_a_valid_config(flow, weekly_files):
    rid = _two_phases(flow, weekly_files)
    assert flow.start(rid, "C", expect=409).json()["error"]["code"] == "config_required"


def test_a_config_that_does_not_fit_is_refused_with_the_reason(flow, weekly_files):
    rid = _two_phases(flow, weekly_files)
    wb = openpyxl.load_workbook(weekly_files["config"])
    ws = wb["Data File"]
    for row in ws.iter_rows(min_row=2):
        if str(row[0].value).strip().lower().startswith("period"):
            row[1].value = "ZZ"                                           # a column the table does not have
    buf = io.BytesIO()
    wb.save(buf)
    err = flow.config(rid, "C", buf.getvalue(), expect=422).json()["error"]
    assert any("Period" in p["message"] for p in err["problems"])
    cfg = next(p for p in flow.view(rid)["phases"] if p["key"] == "C")["config"]
    assert not cfg["valid"] and cfg["error"]["message"]
    assert flow.start(rid, "C", expect=409).json()["error"]["code"] == "config_required"


def test_a_file_that_is_not_a_workbook_is_refused(flow, weekly_files):
    rid = _two_phases(flow, weekly_files)
    flow.config(rid, "C", b"this is not a workbook", expect=422)
    flow.config(rid, "C", b"a,b\n1,2", name="config.csv", expect=400)


def test_phase_d_previews_which_rules_will_run(flow, weekly_files):
    rid = _two_phases(flow, weekly_files)
    flow.config(rid, "C", weekly_files["config"].read_bytes())
    flow.run_phase(rid, "C")
    flow.accept(rid, "C")
    empty = flow.c.get(f"/api/v1/runs/{rid}/phases/D/config-template").content
    pv = next(p for p in flow.config(rid, "D", empty).json()["phases"] if p["key"] == "D")["config"]["preview"]
    assert not any(r["active"] for r in pv["rules"]) and "No threshold is filled in" in pv["warnings"][0]
    pv = next(p for p in flow.config(rid, "D", weekly_files["config"].read_bytes()).json()["phases"] if p["key"] == "D")["config"]["preview"]
    assert sum(r["active"] for r in pv["rules"]) == 7 and {r["name"]: r["active"] for r in pv["ratios"]} == {"TV CPM": True, "Search CPC": False}


def test_a_changed_upstream_phase_invalidates_a_stored_config(flow, weekly_files):
    rid = _two_phases(flow, weekly_files)
    flow.config(rid, "C", weekly_files["config"].read_bytes())
    flow.run_phase(rid, "C")
    flow.start(rid, "B", {"llm": "off", "restart": True})
    flow.wait(rid, "B")
    c = flow.phase(rid, "C")
    assert c["status"] == "locked" and c["config"]["uploaded"] and not c["config"]["valid"]
    flow.accept(rid, "B")
    r = flow.c.post(f"/api/v1/runs/{rid}/phases/C/config/recheck")
    assert r.status_code == 200 and flow.phase(rid, "C")["config"]["valid"]


def test_one_config_covers_every_table_and_the_analyst_can_drop_one(flow, weekly_files):
    """Two weekly sheets: one template, both checked; unticking a table on the Phase A screen leaves it out."""
    wb = openpyxl.load_workbook(weekly_files["data"])
    wb.copy_worksheet(wb["Weekly"]).title = "Weekly copy"
    buf = io.BytesIO()
    wb.save(buf)
    rid = flow.create([("two.xlsx", buf.getvalue())])
    for key in "AB":
        flow.run_phase(rid, key, {"llm": "off"})
        flow.accept(rid, key)
    tables = flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables").json()["tables"]
    assert len(tables) == 2 and all(t["weekly"] for t in tables)
    ok = flow.c.get(f"/api/v1/runs/{rid}/phases/C/config-template")
    assert ok.status_code == 200
    r = flow.c.post(f"/api/v1/runs/{rid}/phases/C/config", files={"file": ("c.xlsx", ok.content)})
    assert r.status_code == 200
    preview = next(p for p in r.json()["phases"] if p["key"] == "C")["config"]["preview"]
    assert [t["checked"] for t in preview["tables"]] == [True, True]
    flow.run_phase(rid, "C", {})
    assert len(flow.c.get(f"/api/v1/runs/{rid}/phases/C/summary").json()["tables"]) == 2
    bad = flow.c.post(f"/api/v1/runs/{rid}/phases/A/selection", json={"tables": []})
    assert bad.status_code == 409 or bad.status_code == 400


# ------------------------------------------------------------------ the review loop
def test_an_untouched_review_workbook_applies_and_keeps_the_phase_open_for_review(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    flow.run_phase(rid, "A", {"llm": "off"})
    for key, art in (("A", "structural-review"),):
        flow.upload_review(rid, key, flow.download(rid, art))
        p = flow.wait(rid, key)
        assert p["status"] == "review" and p["upload_error"] is None
        assert [u["ok"] for u in p["uploads"]] == [True]


def test_phase_b_review_applies_and_discards_later_phases(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    for key in "AB":
        flow.run_phase(rid, key, {"llm": "off"})
        flow.accept(rid, key)
    flow.config(rid, "C", weekly_files["config"].read_bytes())
    flow.run_phase(rid, "C")
    flow.upload_review(rid, "B", flow.download(rid, "hygiene-review"))
    b = flow.wait(rid, "B")
    assert b["status"] == "review" and b["uploads"][0]["ok"]
    c = flow.phase(rid, "C")
    assert c["status"] == "locked" and not c["artifacts"]                  # its input changed: run it again


def test_a_broken_workbook_is_rejected_and_changes_nothing(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    for key in "AB":
        flow.run_phase(rid, key, {"llm": "off"})
        flow.accept(rid, key)
    before = flow.summary(rid, "B")
    wb = openpyxl.Workbook()
    wb.active.title = "Whatever"
    buf = io.BytesIO()
    wb.save(buf)
    flow.upload_review(rid, "B", buf.getvalue())
    b = flow.wait(rid, "B")
    assert b["status"] == "accepted"                                       # state before the upload is kept
    assert b["upload_error"]["code"] == "upload_rejected" and b["upload_error"]["problems"]
    assert [u["ok"] for u in b["uploads"]] == [False]
    assert flow.summary(rid, "B") == before
    flow.upload_review(rid, "B", b"not a workbook")
    assert flow.wait(rid, "B")["upload_error"]["problems"]


def test_phase_a_edit_can_deselect_a_table_and_blocks_continuing(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    flow.run_phase(rid, "A", {"llm": "off"})
    wb = openpyxl.load_workbook(io.BytesIO(flow.download(rid, "structural-review")))
    ws = wb["Structural_Findings"]
    use = [c.value for c in ws[1]].index("Use") + 1
    assert ws.cell(2, use).value == "Y"
    ws.cell(2, use).value = "N"
    buf = io.BytesIO()
    wb.save(buf)
    flow.upload_review(rid, "A", buf.getvalue())
    a = flow.wait(rid, "A")
    assert a["status"] == "review" and a["upload_error"] is None
    assert flow.summary(rid, "A")["selected_tables"] == 0
    assert not a["can_continue"] and "No table is selected" in a["blocked_reason"]
    flow.accept(rid, "A", expect=409)
    # edit the workbook as it is now (the earlier download describes the earlier state)
    wb = openpyxl.load_workbook(io.BytesIO(flow.download(rid, "structural-review")))
    wb["Structural_Findings"].cell(2, use).value = "Y"
    buf = io.BytesIO()
    wb.save(buf)
    flow.upload_review(rid, "A", buf.getvalue())
    again = flow.wait(rid, "A")
    assert again["can_continue"] and again["upload_error"] is None
    flow.accept(rid, "A")


def _edit(flow, rid, artifact, mutate):
    """Download a review workbook, let `mutate(wb)` edit it the way an analyst would, return the saved bytes."""
    wb = openpyxl.load_workbook(io.BytesIO(flow.download(rid, artifact)))
    mutate(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _column(ws, name):
    return [c.value for c in ws[1]].index(name) + 1


def test_phase_a_edit_is_applied_to_the_table(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    flow.run_phase(rid, "A", {"llm": "off"})
    assert flow.summary(rid, "A")["tables"][0]["columns"] == 18

    def exclude_geography(wb):
        ws = wb["Structural_Findings"]
        ws.cell(2, _column(ws, "Exclude columns")).value = "B"          # the Geography column

    flow.upload_review(rid, "A", _edit(flow, rid, "structural-review", exclude_geography))
    a = flow.wait(rid, "A")
    assert a["upload_error"] is None and a["uploads"][0]["applied"]["overrides"] == 1
    assert flow.summary(rid, "A")["tables"][0]["columns"] == 17


def test_phase_b_decisions_change_the_cleaned_table(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    for key in "A":
        flow.run_phase(rid, key, {"llm": "off"})
        flow.accept(rid, key)
    flow.run_phase(rid, "B", {"llm": "off"})
    art = flow.data_id(rid, "parquet")
    lineage = "lineage-" + art.split("-")[1]
    before = pl.read_parquet(io.BytesIO(flow.download(rid, art)))
    item = next(i for i in flow.summary(rid, "B")["needs_review"] if i["rule"] == "D-14" and i["column"] == "Radio_GRPs")
    assert item["proposed_value"] == "220" and item["current_value"] == "676.67"     # fix is off: proposed, not applied

    def decide(wb):
        ws = wb["Needs_Review"]
        row = next(r for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value == item["row_id"] and ws.cell(r, _column(ws, "Column")).value == "Radio_GRPs")
        ws.cell(row, _column(ws, "Decision")).value = "New Value"
        ws.cell(row, _column(ws, "New_Value")).value = "250"
        data = wb["Data_1"]                                              # and a direct edit in the data sheet
        data.cell(2, _column(data, "TV_Spend")).value = 4321

    flow.upload_review(rid, "B", _edit(flow, rid, "hygiene-review", decide))
    b = flow.wait(rid, "B")
    assert b["status"] == "review" and b["upload_error"] is None and b["uploads"][0]["ok"]
    after = pl.read_parquet(io.BytesIO(flow.download(rid, art)))
    assert after.height == before.height
    rid_col = pl.read_parquet(io.BytesIO(flow.download(rid, lineage)))["_row_id"].to_list()
    assert after["Radio_GRPs"][rid_col.index(item["row_id"])] == 250
    assert after["TV_Spend"][0] == 4321 and before["TV_Spend"][0] != 4321
    audit = json.loads(flow.download(rid, "hygiene-audit"))
    assert {a["action"] for a in audit} >= {"analyst_edit"} and len(audit) == 2


def test_phase_b_decision_that_is_not_allowed_is_named_in_the_rejection(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    flow.run_phase(rid, "A", {"llm": "off"})
    flow.accept(rid, "A")
    flow.run_phase(rid, "B", {"llm": "off"})

    def bad(wb):
        ws = wb["Needs_Review"]
        ws.cell(2, _column(ws, "Decision")).value = "Maybe"

    flow.upload_review(rid, "B", _edit(flow, rid, "hygiene-review", bad))
    b = flow.wait(rid, "B")
    assert b["upload_error"]["problems"][0]["message"] == "Needs_Review row 2: Decision must be Accept, Reject, New Value or Split"
    assert b["status"] == "review" and b["uploads"][0]["ok"] is False


def test_phase_c_valid_marks_remove_findings_from_the_open_count(flow, weekly_files):
    rid = _two_phases(flow, weekly_files)
    flow.config(rid, "C", weekly_files["config"].read_bytes())
    flow.run_phase(rid, "C")
    before = flow.summary(rid, "C")["totals"]
    assert before["findings_open"] > 100 and before["findings_valid"] == 0

    def mark(wb):
        ws = wb["No_Executions"] if wb["No_Executions"].max_row > 1 else wb["Date_Integrity"]
        col = _column(ws, "Review")
        marked = 0
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, _column(ws, "Finding_ID")).value:
                ws.cell(r, col).value = "Valid – no change"
                marked += 1
        assert marked >= 1
        mark.count = marked

    flow.upload_review(rid, "C", _edit(flow, rid, flow.report_id(rid, "timeseries-report-"), mark))
    c = flow.wait(rid, "C")
    assert c["status"] == "review" and c["upload_error"] is None
    after = flow.summary(rid, "C")["totals"]
    assert after["findings_valid"] == mark.count
    assert after["findings_open"] == before["findings_open"] - mark.count


def test_phase_d_marks_are_kept_per_finding(flow, weekly_files):
    rid = _two_phases(flow, weekly_files)
    for key in "CD":
        flow.config(rid, key, weekly_files["config"].read_bytes())
        flow.run_phase(rid, key)
        if key == "C":
            flow.accept(rid, "C")
    assert flow.summary(rid, "D")["totals"]["findings_open"] == 14

    def confirm(wb):
        ws = wb["Spend_Support_Availability"]
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, _column(ws, "Finding_ID")).value:
                ws.cell(r, _column(ws, "Review")).value = "Confirmed Issue"

    flow.upload_review(rid, "D", _edit(flow, rid, flow.report_id(rid, "trends-report-"), confirm))
    d = flow.wait(rid, "D")
    assert d["upload_error"] is None
    totals = flow.summary(rid, "D")["totals"]
    assert totals["findings_confirmed"] == 2 and totals["findings_open"] == 12


def test_review_uploads_are_checked_at_the_door(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    flow.run_phase(rid, "A", {"llm": "off"})
    assert flow.upload_review(rid, "A", b"x", name="review.csv", expect=400).json()["error"]["code"] == "bad_file"
    flow.c.post(f"/api/v1/runs/{rid}/phases/B/review", files={"file": ("r.xlsx", b"x")})
    assert flow.c.post(f"/api/v1/runs/{rid}/phases/B/review", files={"file": ("r.xlsx", b"x")}).status_code == 409


# ------------------------------------------------------------------ input handling
def test_a_run_needs_files_and_only_supported_types(client):
    r = client.post("/api/v1/runs", files=[("files", ("a.exe", b"MZ"))], data={"llm": "off"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_file"
    assert client.post("/api/v1/runs", data={"llm": "off"}).status_code == 422


def test_the_size_limit_is_enforced_while_streaming(client):
    big = b"a,b\n" + b"1,2\n" * (2 * 1024 * 1024)                           # about 8 MB; the test limit is 5 MB
    r = client.post("/api/v1/runs", files=[("files", ("big.csv", big))], data={"llm": "off"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"
    assert client.get("/api/v1/runs").json() == []


def test_folder_uploads_keep_their_relative_paths(flow, client, weekly_files):
    r = client.post("/api/v1/runs", files=[("files", ("a.csv", b"x,y\n1,2\n3,4\n")), ("files", ("a.csv", b"x,y\n5,6\n7,8\n"))],
                    data={"paths": ["Client/Q1/a.csv", "Client/Q2/a.csv"], "llm": "off"})
    assert r.status_code == 201
    assert [i["path"] for i in r.json()["inputs"]] == ["Client/Q1/a.csv", "Client/Q2/a.csv"]


def test_unicode_and_spaces_in_file_names_work_end_to_end(flow, weekly_files):
    name = "Spénd – Q1 (final) €.xlsx"
    rid = flow.create([(name, weekly_files["data"])])
    assert flow.view(rid)["inputs"][0]["path"] == name
    flow.run_phase(rid, "A", {"llm": "off"})
    assert flow.summary(rid, "A")["tables"][0]["file"] == name
    flow.accept(rid, "A")
    flow.run_phase(rid, "B", {"llm": "off"})
    assert flow.summary(rid, "B")["tables"][0]["file"] == name


def test_path_traversal_in_an_upload_path_is_refused(client):
    r = client.post("/api/v1/runs", files=[("files", ("a.csv", b"x,y\n1,2\n"))], data={"paths": ["../../evil.csv"], "llm": "off"})
    assert r.status_code == 400


def test_a_corrupt_file_is_reported_by_phase_a_not_a_crash(flow):
    rid = flow.create([("broken.xlsx", b"PK\x03\x04 definitely not a workbook"), ("empty.csv", b"")])
    p = flow.run_phase(rid, "A", {"llm": "off"})
    s = flow.summary(rid, "A")
    assert s["severity"]["block"] >= 1 and s["selected_tables"] == 0
    assert not p["can_continue"]


# ------------------------------------------------------------------ safety of the HTTP surface
def test_unknown_runs_phases_and_files_are_clean_404s(flow, weekly_files, client):
    assert client.get("/api/v1/runs/0123456789abcdef").status_code == 404
    assert client.get("/api/v1/runs/not-an-id").status_code == 404
    assert client.get("/api/v1/runs/0123456789abcdef/events").json()["error"]["code"] == "run_not_found"
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    assert client.get(f"/api/v1/runs/{rid}/phases/Z/summary").status_code == 404
    assert client.post(f"/api/v1/runs/{rid}/phases/Z/start", json={}).status_code == 404
    assert client.get(f"/api/v1/runs/{rid}/phases/A/summary").status_code == 404      # nothing ran yet
    assert client.get(f"/api/v1/runs/{rid}/files/structural-review").status_code == 404


@pytest.mark.parametrize("artifact", ["..%2F..%2Fstate.json", "...", "state.json", "%2e%2e/%2e%2e/config", "A", "x" * 200])
def test_downloads_cannot_reach_other_files(flow, weekly_files, client, artifact):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    flow.run_phase(rid, "A", {"llm": "off"})
    r = client.get(f"/api/v1/runs/{rid}/files/{artifact}")
    assert r.status_code in (400, 404)


def test_start_options_are_validated(flow, weekly_files, client):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    assert client.post(f"/api/v1/runs/{rid}/phases/A/start", json={"llm": "maybe"}).status_code == 422
    assert client.post(f"/api/v1/runs/{rid}/phases/A/start", json={"llm_review": "sometimes"}).status_code == 422
    flow.run_phase(rid, "A", {"llm": "off"})
    flow.accept(rid, "A")
    err = client.post(f"/api/v1/runs/{rid}/phases/B/start", json={"llm": "off", "fixes": {"D-99": True}})
    assert err.status_code == 200                                          # accepted by the API ...
    p = flow.wait(rid, "B")
    assert p["status"] == "failed" and "D-99" in p["error"]["message"]     # ... refused by the phase, reported


def test_delete_removes_the_run_and_its_files(flow, weekly_files, client, service_settings):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    folder = service_settings.workspace / rid
    assert folder.exists()
    assert client.delete(f"/api/v1/runs/{rid}").status_code == 204
    assert not folder.exists() and client.get(f"/api/v1/runs/{rid}").status_code == 404


def test_the_run_list_shows_progress_per_phase(flow, weekly_files, client):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])], name="listed")
    flow.run_phase(rid, "A", {"llm": "off"})
    row = next(r for r in client.get("/api/v1/runs").json() if r["id"] == rid)
    assert row["name"] == "listed" and row["phases"] == {"A": "review", "B": "locked", "C": "locked", "D": "locked", "T": "locked"}
    assert row["files"] == 1 and not row["running"] and not row["complete"]


def test_options_describe_limits_and_the_fix_catalogue(client):
    o = client.get("/api/v1/options").json()
    assert o["limits"]["max_upload_mb"] == 5 and ".xlsx" in o["limits"]["extensions"]
    fixes = {f["id"]: f["default"] for f in o["hygiene_fixes"]}
    assert fixes["D-14"] is False and fixes["D-09"] is True and len(fixes) == 23 and "D-15" not in fixes
    assert fixes["D-27"] is False                                    # filling isolated zeros is opt-in (it hides them from C and D)
    assert o["llm"]["review"] == "all"
    assert isinstance(o["limits"]["legacy_converter"], bool) and ".xls" in o["limits"]["legacy_extensions"]


def test_health_and_the_job_contract_endpoints_still_work(client):
    assert client.get("/api/v1/health").json()["status"] == "ok"
    assert client.get("/health").json() == {"status": "ok"}
    assert client.post("/review/precheck", content=b"not a workbook").json()["valid"] is False


# ------------------------------------------------------------------ columns pasted into a review workbook
def _paste_column(ws, name, values):
    col = ws.max_column + 1
    ws.cell(1, col).value = name
    for r, v in enumerate(values, start=2):
        ws.cell(r, col).value = v


def test_a_column_pasted_after_phase_a_reaches_the_final_table_and_is_checked_by_phase_b(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    flow.run_phase(rid, "A", {"llm": "off"})
    rows = flow.summary(rid, "A")["tables"][0]["rows"]
    cols = flow.summary(rid, "A")["tables"][0]["columns"]
    vals = [" n/a "] + [f"owner {i}" for i in range(1, rows)]

    def paste(wb):
        ws = next(wb[n] for n in wb.sheetnames if n.startswith("T1"))
        _paste_column(ws, "Account Owner", vals)

    flow.upload_review(rid, "A", _edit(flow, rid, "structural-review", paste))
    a = flow.wait(rid, "A")
    assert a["upload_error"] is None and a["uploads"][0]["applied"]["columns_added"] == 1
    flow.accept(rid, "A")
    flow.run_phase(rid, "B", {"llm": "off"})
    df = pl.read_parquet(io.BytesIO(flow.download(rid, flow.data_id(rid, "parquet"))))
    assert df.width == cols + 1 and df.columns[-1] == "Account Owner"
    assert df["Account Owner"][0] is None and df["Account Owner"][1] == "owner 1"        # D-18 nulled the ' n/a ' placeholder


def test_a_column_pasted_after_phase_b_is_added_and_checked(flow, weekly_files):
    rid = flow.create([("weekly.xlsx", weekly_files["data"])])
    for key in "A":
        flow.run_phase(rid, key, {"llm": "off"})
        flow.accept(rid, key)
    flow.run_phase(rid, "B", {"llm": "off"})
    rows = flow.summary(rid, "B")["tables"][0]["rows_out"]
    vals = [" n/a "] + ["1,200"] * (rows - 1)

    def paste(wb):
        _paste_column(wb["Data_1"], "Budget Note", vals)

    flow.upload_review(rid, "B", _edit(flow, rid, "hygiene-review", paste))
    b = flow.wait(rid, "B")
    assert b["upload_error"] is None and b["uploads"][0]["ok"]
    df = pl.read_parquet(io.BytesIO(flow.download(rid, flow.data_id(rid, "parquet"))))
    assert df.columns[-1] == "Budget Note" and df["Budget Note"][0] is None and df["Budget Note"][1] == 1200


def test_a_retired_fix_switch_in_old_settings_is_ignored_not_an_error():
    from src.orchestration.phases.hygiene import overrides_from
    assert overrides_from({"fixes": {"D-15": True, "D-14": False}}) == {"fixes": {"D-14": False}}
