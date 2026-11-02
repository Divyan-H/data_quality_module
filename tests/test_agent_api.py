"""Fix-with-agent end to end: real app, real worker processes, the planted weekly workbook.

The flow an analyst follows: Phase B finishes -> they type an instruction -> a preview comes back (nothing changed) ->
Apply -> the phase is re-checked and later phases are discarded -> Undo puts the data back. The model is switched off
here: these tests cover the deterministic path (command parser), which must work without a GPU.
"""
import io
import json

import polars as pl
import pytest
from fastapi.testclient import TestClient

from src.api.api_server import create_app
from src.orchestration.settings import load_service_settings

from conftest import Flow


class AgentFlow(Flow):
    def agent(self, rid, key="B"):
        r = self.c.get(f"/api/v1/runs/{rid}/phases/{key}/agent")
        assert r.status_code == 200, r.text
        return r.json()

    def say(self, rid, text, key="B", expect=200):
        r = self.c.post(f"/api/v1/runs/{rid}/phases/{key}/agent/messages", json={"text": text})
        assert r.status_code == expect, r.text
        return r.json()

    def apply(self, rid, cs, key="B", expect=200):
        r = self.c.post(f"/api/v1/runs/{rid}/phases/{key}/agent/changesets/{cs}/apply")
        assert r.status_code == expect, r.text
        if expect == 200:
            p = self.wait(rid, key)
            assert p["status"] == "review", p
            return p
        return r.json()

    def undo(self, rid, key="B", expect=200):
        r = self.c.post(f"/api/v1/runs/{rid}/phases/{key}/agent/undo")
        assert r.status_code == expect, r.text
        return self.wait(rid, key) if expect == 200 else r.json()

    def table(self, rid):
        art = self.data_id(rid, "parquet")
        df = pl.read_parquet(io.BytesIO(self.download(rid, art)))
        ids = pl.read_parquet(io.BytesIO(self.download(rid, "lineage-" + art.split("-")[1])))["_row_id"].to_list()
        return df, ids


@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    ws = tmp_path_factory.mktemp("agent_ws")
    app = create_app(None, load_service_settings(workspace=ws / "runs", agent_llm=False))
    with TestClient(app) as c:
        yield c
        app.state.pipeline.shutdown()


@pytest.fixture
def af(app_client):
    return AgentFlow(app_client)


@pytest.fixture
def phase_b(af, weekly_files):
    """A run with Phase A accepted and Phase B in review (its outlier capping D-14 off, so D-14 proposals wait)."""
    rid = af.create([("weekly.xlsx", weekly_files["data"])], name="agent")
    af.run_phase(rid, "A", {"llm": "off"})
    af.accept(rid, "A")
    af.run_phase(rid, "B", {"llm": "off"})
    return rid


def _outlier(af, rid):
    return next(i for i in af.summary(rid, "B")["needs_review"]
                if i["rule"] == "D-14" and i["column"] == "Radio_GRPs" and i["status"] == "open")


def test_a_fresh_phase_has_an_empty_agent_session_and_grouped_issues(af, phase_b):
    s = af.agent(phase_b)
    assert s["usable"] and s["messages"] == [] and s["changesets"] == {} and s["undo"] is None
    assert s["llm"]["enabled"] is False
    issues = af.c.get(f"/api/v1/runs/{phase_b}/phases/B/agent/issues").json()
    assert issues["open"] == af.summary(phase_b, "B")["needs_review_total"]
    assert any(g["rule"] == "D-14" and g["column"] == "Radio_GRPs" for g in issues["groups"])
    ref = issues["tables"][0]["ref"]
    page = af.c.get(f"/api/v1/runs/{phase_b}/phases/B/agent/rows", params={"table": ref, "rule": "D-14", "column": "Radio_GRPs"}).json()
    assert page["total"] >= 1 and all("Radio_GRPs" in r["flagged"] for r in page["rows"])


def test_an_instruction_is_previewed_applied_audited_and_undone(af, phase_b):
    item = _outlier(af, phase_b)
    before, ids = af.table(phase_b)
    row = ids.index(item["row_id"])
    assert str(before["Radio_GRPs"][row]) == "676.67"

    reply = af.say(phase_b, "accept the outlier fixes in Radio_GRPs")["reply"]
    cs = reply["changeset"]
    assert cs["status"] == "proposed" and reply["messages"][1]["kind"] == "proposal"
    assert cs["preview"]["totals"]["cells"] >= 1 and cs["preview"]["totals"]["items_resolved"] >= 1
    after_preview, _ = af.table(phase_b)
    assert after_preview.equals(before)                                   # a preview changes nothing

    p = af.apply(phase_b, cs["id"])
    assert p["agent"]["applied"] == 1 and p["agent_error"] is None
    after, ids = af.table(phase_b)
    assert float(after["Radio_GRPs"][ids.index(item["row_id"])]) == 220.0
    audit = json.loads(af.download(phase_b, "hygiene-audit"))
    mine = [a for a in audit if a["rule"] == "agent"]
    assert mine and all(a["changeset"] == cs["id"] and a["action"] == "analyst_edit" for a in mine)
    assert not any(i["rule"] == "D-14" and i["column"] == "Radio_GRPs" and i["status"] == "open"
                   for i in af.summary(phase_b, "B")["needs_review"])
    state = af.agent(phase_b)
    assert state["changesets"][cs["id"]]["status"] == "applied" and state["undo"]["id"] == cs["id"]

    af.undo(phase_b)
    restored, ids = af.table(phase_b)
    assert restored.equals(before)
    state = af.agent(phase_b)
    assert state["changesets"][cs["id"]]["status"] == "undone" and state["undo"] is None
    assert any(i["rule"] == "D-14" and i["status"] == "open" for i in af.summary(phase_b, "B")["needs_review"])


def test_value_edits_by_condition_and_a_new_column(af, phase_b):
    df, _ = af.table(phase_b)
    reply = af.say(phase_b, "add column Source with value Client")["reply"]
    af.apply(phase_b, reply["changeset"]["id"])
    after, _ = af.table(phase_b)
    assert "Source" in after.columns and set(after["Source"].to_list()) == {"Client"}
    assert after.height == df.height


def test_questions_and_unknown_names_get_answers_not_changes(af, phase_b):
    help_ = af.say(phase_b, "help")["reply"]
    assert help_["changeset"] is None and help_["messages"][1]["kind"] == "answer"
    ask = af.say(phase_b, "set Cityy to Delhi in row 3")["reply"]
    assert ask["changeset"] is None and ask["messages"][1]["kind"] == "ask" and "Cityy" in ask["messages"][1]["text"]
    summary = af.say(phase_b, "summary")["reply"]
    assert "open" in summary["messages"][1]["text"]
    assert len(af.agent(phase_b)["messages"]) == 6


def test_applying_one_proposal_makes_older_previews_stale(af, phase_b):
    a = af.say(phase_b, "add column Tag1 with value A")["reply"]["changeset"]["id"]
    b = af.say(phase_b, "add column Tag2 with value B")["reply"]["changeset"]["id"]
    af.apply(phase_b, a)
    assert af.agent(phase_b)["changesets"][b]["status"] == "stale"
    err = af.apply(phase_b, b, expect=409)
    assert err["error"]["code"] == "not_proposed"


def test_discard_drops_a_proposal_without_changing_data(af, phase_b):
    cs = af.say(phase_b, "add column Tag with value A")["reply"]["changeset"]["id"]
    r = af.c.post(f"/api/v1/runs/{phase_b}/phases/B/agent/changesets/{cs}/discard")
    assert r.status_code == 200 and r.json()["changesets"][cs]["status"] == "discarded"
    df, _ = af.table(phase_b)
    assert "Tag" not in df.columns


def test_an_applied_change_discards_later_phases(af, phase_b, weekly_files):
    af.accept(phase_b, "B")
    af.config(phase_b, "C", weekly_files["config"].read_bytes())
    af.run_phase(phase_b, "C")
    cs = af.say(phase_b, "add column Tag with value A")["reply"]["changeset"]["id"]
    b = af.apply(phase_b, cs)
    assert b["status"] == "review"
    c = af.phase(phase_b, "C")
    assert c["status"] == "locked" and not c["artifacts"]


def test_a_review_upload_after_agent_changes_keeps_history_but_ends_undo(af, phase_b):
    cs = af.say(phase_b, "add column Tag with value A")["reply"]["changeset"]["id"]
    af.apply(phase_b, cs)
    af.upload_review(phase_b, "B", af.download(phase_b, "hygiene-review"))
    p = af.wait(phase_b, "B")
    assert p["status"] == "review" and p["uploads"][0]["ok"]
    s = af.agent(phase_b)
    assert s["undo"] is None and s["changesets"][cs]["status"] == "applied"
    assert af.undo(phase_b, expect=409)["error"]["code"] == "nothing_to_undo"


def test_restarting_the_phase_clears_the_conversation(af, phase_b):
    af.say(phase_b, "help")
    af.start(phase_b, "B", {"llm": "off", "restart": True})
    af.wait(phase_b, "B")
    assert af.agent(phase_b)["messages"] == []


def test_the_agent_is_refused_where_it_cannot_act(af, phase_b, weekly_files):
    r = af.c.get(f"/api/v1/runs/{phase_b}/phases/C/agent")
    assert r.status_code == 400 and r.json()["error"]["code"] == "agent_not_available"
    fresh = af.create([("weekly.xlsx", weekly_files["data"])], name="fresh")
    r = af.c.post(f"/api/v1/runs/{fresh}/phases/B/agent/messages", json={"text": "help"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_reviewable"
    r = af.c.post(f"/api/v1/runs/{phase_b}/phases/B/agent/changesets/cs-9999/apply")
    assert r.status_code == 404
    r = af.c.post(f"/api/v1/runs/{phase_b}/phases/B/agent/messages", json={"text": ""})
    assert r.status_code == 422
    assert af.c.get("/api/v1/options").json()["agent"]["phases"] == ["B"]
