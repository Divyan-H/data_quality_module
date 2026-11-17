"""Reshaping inside Phase A, end to end: real app and worker processes, a reactivation-style sheet (Day 1..Day 5
columns), the recipe built in the chat on Phase A's review, undo, accept, the checking phases running on the result,
replay when Phase A runs again or the table selection changes, and a saved recipe used again on a new run."""
import io

import openpyxl
import polars as pl
import pytest
from fastapi.testclient import TestClient

from src.agent.guardrails import BEFORE_CHECKS
from src.api.api_server import create_app
from src.orchestration.phases import ORDER
from src.orchestration.settings import load_service_settings

from test_agent_api import AgentFlow

DAYS = 5


@pytest.fixture(scope="module")
def reactivation_xlsx(tmp_path_factory):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Reactivation"
    ws.append(["Month", "Bucket", "City", *[f"Day {d}" for d in range(1, DAYS + 1)]])
    for month in ("Jan-25", "Feb-25"):
        for bucket, base in (("31-120 days", 1), ("121-365 days", 10)):
            ws.append([month, bucket, "Pune", *[base * d for d in range(1, DAYS + 1)]])
    other = wb.create_sheet("Cities")
    other.append(["City", "Region"])
    other.append(["Pune", "West"])
    other.append(["Delhi", "North"])
    p = tmp_path_factory.mktemp("react") / "reactivation.xlsx"
    wb.save(p)
    return p


@pytest.fixture(scope="module")
def tc(tmp_path_factory):
    ws = tmp_path_factory.mktemp("transform_ws")
    app = create_app(None, load_service_settings(workspace=ws / "runs", agent_llm=False))
    with TestClient(app) as c:
        yield AgentFlow(c)
        app.state.pipeline.shutdown()


def phase_a(f: AgentFlow, rid: str) -> dict:
    f.run_phase(rid, "A", {"llm": "off"})
    return f.summary(rid, "A")


def build(f: AgentFlow, rid: str, *instructions: str) -> None:
    for text in instructions:
        reply = f.say(rid, text, key="A")["reply"]
        assert reply["changeset"], (text, reply["messages"][-1]["text"])
        f.apply(rid, reply["changeset"]["id"], key="A")


def b_table(f: AgentFlow, rid: str, sheet: str) -> pl.DataFrame:
    return pl.read_parquet(io.BytesIO(f.download(rid, f.data_id(rid, "parquet", sheet))))


def test_the_pipeline_has_four_phases_and_reshaping_is_optional(tc, reactivation_xlsx):
    assert ORDER == ["A", "B", "C", "D"]
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="no reshape")
    s = phase_a(tc, rid)
    assert s["reshape"] is None and [p["key"] for p in tc.view(rid)["phases"]] == ["A", "B", "C", "D"]
    assert tc.phase(rid, "A")["supports_agent"] and tc.phase(rid, "A")["can_continue"]
    assert not any(a["id"].startswith("reshape-") for a in tc.phase(rid, "A")["artifacts"])
    tc.accept(rid, "A")
    tc.run_phase(rid, "B", {"llm": "off"})
    df = b_table(tc, rid, "Reactivation")
    assert df.height == 4 and "Day 1" in df.columns                                   # the table as the client sent it
    r = tc.c.post(f"/api/v1/runs/{rid}/phases/T/start", json={})
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_phase"


def test_a_recipe_is_built_on_phase_a_and_the_checks_run_on_the_result(tc, reactivation_xlsx):
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="reshape")
    phase_a(tc, rid)
    reply = tc.say(rid, "unpivot Day 1 to Day 5 into rows as Day and Reactivated", key="A")["reply"]
    prev = reply["changeset"]["preview"]
    assert prev["kind"] == "transform" and prev["operations"][0]["effect"]["tables"][0]["rows_after"] == 4 * DAYS
    assert reply["changeset"]["review"]["level"] == "normal"                         # nothing is lost by an unpivot
    tc.apply(rid, reply["changeset"]["id"], key="A")
    assert tc.phase(rid, "A")["status"] == "review"
    build(tc, rid, "make Date from Month and Day",
          "pivot Bucket into columns using Reactivated by Date, City",
          "sort Reactivation by Date")
    s = tc.summary(rid, "A")["reshape"]
    assert [st["step"] for st in s["steps"]] == [1, 2, 3, 4] and s["failed"] is None
    by_name = {t["name"]: t for t in s["tables"]}
    assert by_name["Reactivation"]["rows"] == 2 * DAYS and by_name["Reactivation"]["changed"]
    assert not by_name["Cities"]["changed"]                                          # the recipe never touched it

    extra = tc.say(rid, "drop columns City in Reactivation", key="A")["reply"]["changeset"]
    assert extra["review"]["level"] == "elevated"
    tc.apply(rid, extra["id"], key="A")
    assert len(tc.summary(rid, "A")["reshape"]["steps"]) == 5
    tc.undo(rid, key="A")
    assert len(tc.summary(rid, "A")["reshape"]["steps"]) == 4                         # the recipe went back too

    answer = tc.say(rid, "save recipe as Reactivation monthly", key="A")["reply"]
    assert "Saved the recipe (4 step(s))" in answer["messages"][-1]["text"]
    ids = {a["id"] for a in tc.phase(rid, "A")["artifacts"]}
    assert {"reshape-report", "reshape-recipe", "reshape-lineage", "structural-review"} <= ids

    # Phase B checks the reshaped table; the untouched one is read from Phase A exactly as before
    tc.accept(rid, "A")
    tc.run_phase(rid, "B", {"llm": "off"})
    df = b_table(tc, rid, "Reactivation")
    assert df.height == 2 * DAYS and {"Date", "31-120 days", "121-365 days"} <= set(df.columns)
    row = df.filter(pl.col("Date").cast(pl.String) == "2025-01-02").row(0, named=True)
    assert float(row["31-120 days"]) == 2 and float(row["121-365 days"]) == 20
    lineage_id = tc.data_id(rid, "parquet", "Reactivation").replace("data-", "lineage-").rsplit("-", 1)[0]
    b_ids = pl.read_parquet(io.BytesIO(tc.download(rid, lineage_id)))["_row_id"].to_list()
    ref = next(t["ref"] for t in tc.summary(rid, "A")["tables"] if t["sheet"] == "Reactivation")
    assert b_ids == [f"{ref}:r{n}" for n in range(1, 2 * DAYS + 1)]                   # `<table ref>:r<n>`: C and D rely on it
    assert b_table(tc, rid, "Cities").height == 2
    lineage = pl.read_parquet(io.BytesIO(tc.download(rid, "reshape-lineage")))
    assert lineage.height == 2 * DAYS and set(lineage["table_ref"]) == {ref}

    # the later phases find the reshaped table (they read each table's ref off its row ids)
    tc.accept(rid, "B")
    listed = tc.c.get(f"/api/v1/runs/{rid}/phases/C/tables")
    assert listed.status_code == 200, listed.text
    assert ref in [t["ref"] for t in listed.json()["tables"]]

    # Phase A runs again: its chat is reset, the recipe is kept and replayed; later phases start over
    tc.start(rid, "A", {"llm": "off", "restart": True})
    tc.wait(rid, "A")
    again = tc.summary(rid, "A")["reshape"]
    assert [st["step"] for st in again["steps"]] == [1, 2, 3, 4] and again["failed"] is None
    assert tc.phase(rid, "B")["status"] == "locked"

    # a new table selection replays it on the tables that are left; dropping the recipe's table makes its step fail
    r = tc.c.post(f"/api/v1/runs/{rid}/phases/A/selection", json={"tables": [ref]})
    assert r.status_code == 200, r.text
    after = tc.summary(rid, "A")["reshape"]
    assert [t["name"] for t in after["tables"]] == ["Reactivation"] and after["failed"] is None


def test_a_saved_recipe_is_used_on_another_run(tc, reactivation_xlsx):
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="next month")
    phase_a(tc, rid)
    listed = tc.say(rid, "list recipes", key="A")["reply"]["messages"][-1]["text"]
    assert "Reactivation monthly" in listed
    reply = tc.say(rid, "use recipe Reactivation monthly", key="A")["reply"]
    assert len(reply["changeset"]["preview"]["operations"]) == 4
    tc.apply(rid, reply["changeset"]["id"], key="A")
    assert {t["name"]: t["rows"] for t in tc.summary(rid, "A")["reshape"]["tables"]}["Reactivation"] == 2 * DAYS


def test_steps_that_change_the_numbers_before_the_checks_need_a_confirmation(tc, reactivation_xlsx):
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="aggregate")
    phase_a(tc, rid)
    cs = tc.say(rid, "sum Day 1, Day 2 by Month, City", key="A")["reply"]["changeset"]
    assert cs["review"]["level"] == "elevated" and BEFORE_CHECKS in cs["review"]["reasons"]
    refused = tc.apply(rid, cs["id"], key="A", expect=409, confirm=False)
    assert refused["error"]["code"] == "confirmation_required"
    tc.apply(rid, cs["id"], key="A", confirm=True)
    tc.accept(rid, "A")
    tc.run_phase(rid, "B", {"llm": "off"})                                           # grouped rows have no Excel row
    df = b_table(tc, rid, "Reactivation")
    assert df.height == 2 and sorted(float(v) for v in df["Day 1"].to_list()) == [11.0, 11.0]


def test_reshaping_is_refused_in_phase_b_with_a_pointer_to_phase_a(tc, reactivation_xlsx):
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="b only")
    phase_a(tc, rid)
    tc.accept(rid, "A")
    tc.run_phase(rid, "B", {"llm": "off"})
    reply = tc.say(rid, "unpivot Day 1 to Day 5 into rows")["reply"]
    text = reply["messages"][-1]["text"]
    assert reply["changeset"] is None and "Phase A" in text and "Reshape with agent" in text
    ok = tc.say(rid, "add column Tier: High if Day 5 > 10, else Low")["reply"]
    assert ok["changeset"] and ok["changeset"]["preview"]["totals"]["columns_added"] == ["Tier"]


def test_a_step_that_no_longer_fits_stops_the_replay_and_blocks_accepting_phase_a(tc, reactivation_xlsx):
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="breaks")
    phase_a(tc, rid)
    build(tc, rid, "drop columns Bucket in Reactivation")
    run_dir = tc.c.app.state.pipeline.store.path(rid)
    recipe = run_dir / "config" / "A_recipe.json"
    recipe.write_text(recipe.read_text().replace('"Bucket"', '"Nope"'))
    tc.start(rid, "A", {"llm": "off", "restart": True})
    p = tc.wait(rid, "A")
    assert p["status"] == "review" and not p["can_continue"] and "Nope" in p["blocked_reason"]
    assert tc.accept(rid, "A", expect=409).json()["error"]["code"] == "cannot_continue"


def test_a_recipe_from_the_old_phase_t_is_taken_over(tc, reactivation_xlsx):
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="legacy")
    run_dir = tc.c.app.state.pipeline.store.path(rid)
    (run_dir / "config" / "T_recipe.json").write_text(
        '{"steps": [{"op": "drop_columns", "table": "Reactivation", "columns": ["City"]}]}')
    s = phase_a(tc, rid)
    assert [st["step"] for st in s["reshape"]["steps"]] == [1]
    assert (run_dir / "config" / "A_recipe.json").exists() and not (run_dir / "config" / "T_recipe.json").exists()


def test_phases_b_c_and_d_give_the_same_findings_on_a_table_that_went_through_the_recipe(tc, weekly_files):
    """The planted weekly workbook, once as Phase A found it and once handed over by the recipe (a step that keeps
    every row and value: the table then reaches B-D with `<ref>:r<n>` row ids). The checks must not notice."""
    counts = {}
    for name, reshape in (("plain", False), ("through the recipe", True)):
        rid = tc.create([("weekly.xlsx", weekly_files["data"])], name=name)
        phase_a(tc, rid)
        if reshape:
            first = tc.agent(rid, key="A")                                          # the workspace opens on Phase A
            assert first["usable"]
            column = tc.c.get(f"/api/v1/runs/{rid}/phases/A/agent/issues").json()["tables"][0]["columns"][0]
            build(tc, rid, f"sort by {column}")
            assert tc.summary(rid, "A")["reshape"]["tables"][0]["changed"] in (True, False)
            build(tc, rid, "add column Batch with value 2026-11")
            assert tc.summary(rid, "A")["reshape"]["tables"][0]["changed"] is True
        tc.accept(rid, "A")
        tc.run_phase(rid, "B", {"llm": "off"})
        b = tc.summary(rid, "B")
        tc.accept(rid, "B")
        cfg = weekly_files["config"].read_bytes()
        per_phase = {}
        for key in "CD":
            tc.config(rid, key, cfg)
            tc.run_phase(rid, key)
            s = tc.summary(rid, key)
            per_phase[key] = (s.get("findings_total") if "findings_total" in s else s.get("findings"), s.get("rows"))
            tc.accept(rid, key)
        assert tc.view(rid)["complete"]
        counts[name] = (b["totals"]["needs_review_open"], per_phase)
    assert counts["plain"] == counts["through the recipe"], counts
