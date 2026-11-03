"""Phase T (Transformations) end to end: real app and worker processes, a reactivation-style sheet (Day 1..Day 5
columns), the recipe built in the chat, undo, accept, final files, replay after an earlier phase re-runs, and a saved
recipe used again on a new run."""
import io

import openpyxl
import polars as pl
import pytest
from fastapi.testclient import TestClient

from src.api.api_server import create_app
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


def to_t(f: AgentFlow, rid: str) -> None:
    for key in "AB":
        f.run_phase(rid, key, {"llm": "off"})
        f.accept(rid, key)
    for key in "CD":
        r = f.c.post(f"/api/v1/runs/{rid}/phases/{key}/skip", json={"reason": "not weekly data"})
        assert r.status_code == 200, r.text


def final_table(f: AgentFlow, rid: str) -> pl.DataFrame:
    ids = [x["id"] for x in f.c.get(f"/api/v1/runs/{rid}/results").json()["files"] if x["id"].startswith("data-") and x["id"].endswith("-parquet")]
    assert len(ids) == 1, ids
    return pl.read_parquet(io.BytesIO(f.download(rid, ids[0])))


def build(f: AgentFlow, rid: str, *instructions: str) -> None:
    for text in instructions:
        reply = f.say(rid, text, key="T")["reply"]
        assert reply["changeset"], (text, reply["messages"][-1]["text"])
        f.apply(rid, reply["changeset"]["id"], key="T")


def test_transformations_are_optional_and_the_run_is_complete_without_them(tc, reactivation_xlsx):
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="no transform")
    to_t(tc, rid)
    v = tc.view(rid)
    assert tc.phase(rid, "T")["status"] == "ready" and v["complete"]
    assert final_table(tc, rid).height == 4                                           # the checked table, as before
    r = tc.c.post(f"/api/v1/runs/{rid}/phases/T/skip", json={})
    assert r.status_code == 200 and tc.phase(rid, "T")["skipped"] and tc.view(rid)["complete"]


def test_a_recipe_is_built_in_the_chat_previewed_applied_undone_and_published(tc, reactivation_xlsx):
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="transform")
    to_t(tc, rid)
    tc.run_phase(rid, "T")
    assert not tc.view(rid)["complete"]                                             # T started: accept it first
    reply = tc.say(rid, "unpivot Day 1 to Day 5 into rows as Day and Reactivated", key="T")["reply"]
    prev = reply["changeset"]["preview"]
    assert prev["kind"] == "transform" and prev["operations"][0]["effect"]["tables"][0]["rows_after"] == 4 * DAYS
    tc.apply(rid, reply["changeset"]["id"], key="T")
    build(tc, rid, "make Date from Month and Day",
          "pivot Bucket into columns using Reactivated by Date, City",
          "sort by Date")
    s = tc.summary(rid, "T")
    assert [st["step"] for st in s["steps"]] == [1, 2, 3, 4] and s["failed"] is None
    assert s["tables"][0]["rows"] == 2 * DAYS

    extra = tc.say(rid, "drop columns City", key="T")["reply"]["changeset"]["id"]
    tc.apply(rid, extra, key="T")
    assert len(tc.summary(rid, "T")["steps"]) == 5
    tc.undo(rid, key="T")
    assert len(tc.summary(rid, "T")["steps"]) == 4                                   # the recipe went back too

    answer = tc.say(rid, "save recipe as Reactivation monthly", key="T")["reply"]
    assert "Saved the recipe (4 step(s))" in answer["messages"][-1]["text"]
    tc.accept(rid, "T")
    assert tc.view(rid)["complete"]
    df = final_table(tc, rid)
    assert df.height == 2 * DAYS and {"Date", "31-120 days", "121-365 days"} <= set(df.columns)
    row = df.filter(pl.col("Date").cast(pl.String) == "2025-01-02").row(0, named=True)
    assert row["31-120 days"] == 2 and row["121-365 days"] == 20
    assert any(f["id"] == "transform-report" for f in tc.c.get(f"/api/v1/runs/{rid}/results").json()["files"])

    # an earlier phase runs again: T is cleared, its recipe kept, and running T replays it on the new data
    tc.start(rid, "B", {"llm": "off", "restart": True})
    tc.wait(rid, "B")
    assert tc.phase(rid, "T")["status"] == "locked"
    tc.accept(rid, "B")
    for key in "CD":
        tc.c.post(f"/api/v1/runs/{rid}/phases/{key}/skip", json={})
    tc.run_phase(rid, "T")
    assert [st["step"] for st in tc.summary(rid, "T")["steps"]] == [1, 2, 3, 4]
    assert tc.summary(rid, "T")["tables"][0]["rows"] == 2 * DAYS


def test_a_saved_recipe_is_used_on_another_run(tc, reactivation_xlsx):
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="next month")
    to_t(tc, rid)
    tc.run_phase(rid, "T")
    listed = tc.say(rid, "list recipes", key="T")["reply"]["messages"][-1]["text"]
    assert "Reactivation monthly" in listed
    reply = tc.say(rid, "use recipe Reactivation monthly", key="T")["reply"]
    assert len(reply["changeset"]["preview"]["operations"]) == 4
    tc.apply(rid, reply["changeset"]["id"], key="T")
    assert tc.summary(rid, "T")["tables"][0]["rows"] == 2 * DAYS


def test_reshaping_is_refused_in_phase_b_with_a_pointer_to_phase_t(tc, reactivation_xlsx):
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="b only")
    tc.run_phase(rid, "A", {"llm": "off"})
    tc.accept(rid, "A")
    tc.run_phase(rid, "B", {"llm": "off"})
    reply = tc.say(rid, "unpivot Day 1 to Day 5 into rows")["reply"]
    assert reply["changeset"] is None and "Phase T" in reply["messages"][-1]["text"]
    ok = tc.say(rid, "add column Tier: High if Day 5 > 10, else Low")["reply"]
    assert ok["changeset"] and ok["changeset"]["preview"]["totals"]["columns_added"] == ["Tier"]


def test_a_step_that_no_longer_fits_the_data_stops_the_replay_and_blocks_accepting(tc, reactivation_xlsx, tmp_path):
    rid = tc.create([("reactivation.xlsx", reactivation_xlsx)], name="breaks")
    to_t(tc, rid)
    tc.run_phase(rid, "T")
    build(tc, rid, "drop columns City")
    run_dir = tc.c.app.state.pipeline.store.path(rid)
    recipe = run_dir / "config" / "T_recipe.json"
    recipe.write_text(recipe.read_text().replace('"City"', '"Nope"'))
    tc.start(rid, "T", {"restart": True})
    p = tc.wait(rid, "T")
    assert p["status"] == "review" and not p["can_continue"] and "Nope" in p["blocked_reason"]
