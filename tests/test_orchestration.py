"""Orchestration: job processes (cancel, crash, timeout, limits), recovery after a restart, and the pieces behind the API."""
import json
import time

import psutil
import pytest
from fastapi.testclient import TestClient

from src.api.api_server import create_app
from src.orchestration import bundle
from src.orchestration.orchestrator import Pipeline, PipelineError
from src.orchestration.phases import ORDER, PHASES, findings, following, previous
from src.orchestration.phases.structure import collect_inputs, structure_config
from src.orchestration.runner import JobRunner, kill_orphan
from src.orchestration.settings import load_service_settings
from src.storage.run_store import RunStore

from conftest import Flow


def big_csv(rows=250_000) -> bytes:
    """Large enough that Phase A is still busy a second after it starts."""
    head = "Period,Geography," + ",".join(f"V{i}" for i in range(10)) + "\n"
    body = "".join(f"2024-01-{1 + r % 28:02d},US," + ",".join(str((r * 7 + c) % 1000) for c in range(10)) + "\n" for r in range(rows))
    return (head + body).encode()


@pytest.fixture(scope="module")
def big():
    return big_csv()


@pytest.fixture
def slow_client(tmp_path):
    settings = load_service_settings(workspace=tmp_path / "runs", max_upload_mb=100, max_concurrent_jobs=1)
    app = create_app(None, settings)
    with TestClient(app) as c:
        yield c, Flow(c)
        app.state.pipeline.shutdown()


def wait_until(fn, timeout=60, step=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


@pytest.fixture
def weekly_copy(weekly_files, tmp_path):
    """Pipeline.create_run moves the files it is given (they are upload temp files), so hand it a copy."""
    import shutil
    dst = tmp_path / "upload.xlsx"
    shutil.copy(weekly_files["data"], dst)
    return dst


def pids_gone(pids):
    return all(not psutil.pid_exists(p) or psutil.Process(p).status() == psutil.STATUS_ZOMBIE for p in pids)


# ------------------------------------------------------------------ cancel / crash / timeout
def test_cancel_stops_the_worker_and_the_phase_can_be_started_again(slow_client, big, weekly_files):
    c, flow = slow_client
    rid = flow.create([("big.csv", big)])
    flow.start(rid, "A", {"llm": "off"})
    job = wait_until(lambda: flow.view(rid)["job"])
    wait_until(lambda: psutil.pid_exists(job["pid"]))
    r = c.post(f"/api/v1/runs/{rid}/cancel")
    assert r.status_code == 200
    p = next(x for x in r.json()["phases"] if x["key"] == "A")
    assert p["status"] == "cancelled" and p["error"]["code"] == "cancelled"
    assert flow.view(rid)["job"] is None and pids_gone([job["pid"]])
    assert not (c.app.state.pipeline.store.path(rid) / "phase_a" / "report.json").exists()
    # starting again works (a different, small file this time)
    rid2 = flow.create([("w.xlsx", weekly_files["data"])])
    flow.run_phase(rid2, "A", {"llm": "off"})


def test_cancel_with_nothing_running_is_a_clean_conflict(slow_client, weekly_files):
    c, flow = slow_client
    rid = flow.create([("w.xlsx", weekly_files["data"])])
    r = c.post(f"/api/v1/runs/{rid}/cancel")
    assert r.status_code == 409 and r.json()["error"]["code"] == "nothing_running"


def test_a_killed_worker_fails_the_phase_with_a_reason_and_can_be_retried(slow_client, big, weekly_files):
    c, flow = slow_client
    rid = flow.create([("big.csv", big)])
    flow.start(rid, "A", {"llm": "off"})
    job = wait_until(lambda: flow.view(rid)["job"])
    psutil.Process(job["pid"]).kill()                                   # out of memory, crash, someone ends the task
    p = flow.wait(rid, "A", timeout=30)
    assert p["status"] == "failed" and p["error"]["code"] == "worker_died"
    assert flow.view(rid)["job"] is None
    flow.start(rid, "A", {"llm": "off"})                                # failed phases restart without a confirmation
    c.post(f"/api/v1/runs/{rid}/cancel")


def test_a_job_that_runs_too_long_is_stopped(slow_client, big):
    c, flow = slow_client
    c.app.state.pipeline.runner.timeout_s = 1
    rid = flow.create([("big.csv", big)])
    flow.start(rid, "A", {"llm": "off"})
    p = flow.wait(rid, "A", timeout=30)
    assert p["status"] == "failed" and p["error"]["code"] == "timeout"


def test_the_service_runs_only_as_many_jobs_as_allowed(slow_client, big, weekly_files):
    c, flow = slow_client                                              # max_concurrent_jobs = 1
    first = flow.create([("big.csv", big)])
    second = flow.create([("w.xlsx", weekly_files["data"])])
    flow.start(first, "A", {"llm": "off"})
    r = flow.start(second, "A", {"llm": "off"}, expect=429)
    assert r.json()["error"]["code"] == "busy" and flow.phase(second, "A")["status"] == "ready"
    c.post(f"/api/v1/runs/{first}/cancel")
    wait_until(lambda: flow.view(first)["job"] is None)
    flow.run_phase(second, "A", {"llm": "off"})


def test_deleting_a_running_run_stops_its_worker(slow_client, big):
    c, flow = slow_client
    rid = flow.create([("big.csv", big)])
    flow.start(rid, "A", {"llm": "off"})
    job = wait_until(lambda: flow.view(rid)["job"])
    assert c.delete(f"/api/v1/runs/{rid}").status_code == 204
    wait_until(lambda: pids_gone([job["pid"]]))
    assert c.get(f"/api/v1/runs/{rid}").status_code == 404


# ------------------------------------------------------------------ restart of the service
def test_a_run_left_running_by_a_dead_service_is_marked_interrupted(service_settings, weekly_copy):
    p1 = Pipeline(service_settings)
    rid = p1.create_run("r", [("w.xlsx", weekly_copy)], "off")

    def fake_running(s):
        s["phases"]["A"].update(status="running", started_at="2026-01-01T00:00:00+00:00")
        s["job"] = {"id": "deadbeef", "phase": "A", "action": "run", "started_at": "x", "pid": 2_000_000_000}
    p1.store.update(rid, fake_running)
    p2 = Pipeline(service_settings)                                    # the service starts again
    v = p2.view(rid)
    a = v["phases"][0]
    assert a["status"] == "failed" and a["error"]["code"] == "interrupted" and v["job"] is None
    p2.start(rid, "A", {"llm": "off"})                                  # and the analyst can simply run it again
    wait_until(lambda: p2.view(rid)["phases"][0]["status"] == "review", timeout=120)


def test_an_interrupted_review_upload_keeps_the_phases_old_results_visible(service_settings, weekly_copy):
    p = Pipeline(service_settings)
    rid = p.create_run("r", [("w.xlsx", weekly_copy)], "off")
    p.store.update(rid, lambda s: (s["phases"]["A"].update(status="review"),
                                   s.update(job={"id": "x", "phase": "A", "action": "review", "started_at": "x", "pid": None})))
    v = Pipeline(service_settings).view(rid)
    assert v["job"] is None and v["phases"][0]["status"] == "review"


def test_kill_orphan_ignores_unrelated_and_missing_processes():
    kill_orphan(None)
    kill_orphan(2_000_000_000)
    me = psutil.Process().pid
    kill_orphan(me)                                                    # not a worker: left alone
    assert psutil.pid_exists(me)


# ------------------------------------------------------------------ runner outcomes (no processes)
@pytest.mark.parametrize("files,cancelled,timed_out,code", [
    ({"result.json": json.dumps({"ok": True, "data": {}})}, False, False, None),
    ({"result.json": "{broken"}, False, False, "worker_died"),
    ({}, False, False, "worker_died"),
    ({}, True, False, "cancelled"),
    ({}, False, True, "timeout"),
])
def test_outcome_of_a_job_from_what_it_left_behind(tmp_path, files, cancelled, timed_out, code):
    from src.orchestration.runner import Job

    class P:
        returncode = -9

    job = Job("j", "r", "A", "run", tmp_path, proc=P(), cancelled=cancelled, timed_out=timed_out)
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf8")
    (tmp_path / "stderr.log").write_text("MemoryError\n", encoding="utf8")
    out = JobRunner._outcome(job)
    assert (out.get("code") if not out["ok"] else None) == code
    if code == "worker_died":
        assert "MemoryError" in out["message"] and "-9" in out["message"]


# ------------------------------------------------------------------ the state machine
def test_phase_order_helpers():
    assert ORDER == ["A", "B", "C", "D", "T"] and set(PHASES) == set(ORDER)
    assert previous("A") is None and previous("C") == "B" and previous("T") == "D"
    assert following("B") == ["C", "D", "T"] and following("D") == ["T"] and following("T") == []


def test_only_phases_c_and_d_need_a_config_and_only_a_uses_the_llm():
    assert {k for k in ORDER if PHASES[k].needs_config} == {"C", "D"}
    assert {k for k in ORDER if PHASES[k].uses_llm} == {"A"}
    assert {k for k in ORDER if PHASES[k].optional} == {"T"}                        # Transformations can be skipped
    assert {k for k in ORDER if PHASES[k].supports_agent} == {"B", "T"}


def test_unknown_phase_is_an_error_not_a_crash(service_settings, weekly_copy):
    p = Pipeline(service_settings)
    rid = p.create_run("r", [("w.xlsx", weekly_copy)], "off")
    with pytest.raises(PipelineError) as e:
        p.start(rid, "E")
    assert e.value.status == 404


def test_a_run_needs_at_least_one_file(service_settings):
    with pytest.raises(PipelineError) as e:
        Pipeline(service_settings).create_run("empty", [])
    assert e.value.code == "no_files"


def test_the_workspace_survives_a_restart(service_settings, weekly_copy):
    p1 = Pipeline(service_settings)
    rid = p1.create_run("kept", [("w.xlsx", weekly_copy)], "off")
    p1.start(rid, "A", {"llm": "off"})
    wait_until(lambda: p1.view(rid)["phases"][0]["status"] == "review", timeout=120)
    p2 = Pipeline(service_settings)
    v = p2.view(rid)
    assert v["name"] == "kept" and v["phases"][0]["status"] == "review" and p2.summary(rid, "A")["rows_out"] == 158
    assert [r["id"] for r in p2.list_runs()] == [rid]


# ------------------------------------------------------------------ pieces
def test_collect_inputs_keeps_folders_and_skips_clutter(tmp_path):
    (tmp_path / "Client" / "Q1").mkdir(parents=True)
    for rel in ("Client/Q1/a.xlsx", "Client/b.csv", "~$lock.xlsx", "Thumbs.db", ".DS_Store"):
        (tmp_path / rel).write_bytes(b"x")
    got = collect_inputs(tmp_path)
    assert sorted(f["relative_path"] for f in got) == ["Client/Q1/a.xlsx", "Client/b.csv"]
    assert [f["file_id"] for f in got] == ["f-1", "f-2"] and {f["source_id"] for f in got} == {"s-1"}


def test_structure_config_follows_the_analysts_llm_choice():
    assert structure_config({"mode": "off"}) == {"structure": {"llm": {"mode": "off"}}}
    assert structure_config(None)["structure"]["llm"]["mode"] == "off"
    on = structure_config({"mode": "assist", "model": "m", "digest": "d", "review": "near_ties", "privacy": "labels_only"})
    assert on["structure"]["llm"] == {"mode": "assist", "privacy": "labels_only", "votes": 3, "review": "near_ties"}


def test_findings_preview_samples_every_code_and_handles_empty_files(tmp_path):
    p = tmp_path / "findings.json"
    items = [{"finding_id": f"N-08-{i}", "code": "N-08", "status": "open"} for i in range(500)]
    items += [{"finding_id": "N-14-1", "code": "N-14", "status": "open"}, {"finding_id": "N-09-1", "code": "N-09", "status": "valid"}]
    p.write_text("[\n" + ",\n".join(json.dumps(i) for i in items) + "\n]\n", encoding="utf8")
    prev = findings.preview(p, per_code=5)
    assert prev["counts"] == {"N-08": 500, "N-14": 1} and prev["total"] == 502
    assert [r["code"] for r in prev["rows"]].count("N-08") == 5 and any(r["code"] == "N-14" for r in prev["rows"])
    assert findings.preview(p, only_status=None)["counts"]["N-09"] == 1
    for text in ("[]\n", "", "[\n]\n", "{broken"):
        p.write_text(text, encoding="utf8")
        assert findings.preview(p) == {"counts": {}, "rows": [], "total": 0}
    assert findings.preview(tmp_path / "missing.json")["total"] == 0


def test_bundle_of_a_run_with_no_results_is_empty_but_valid(service_settings, weekly_copy):
    p = Pipeline(service_settings)
    rid = p.create_run("r", [("w.xlsx", weekly_copy)], "off")
    assert bundle.collect(p.store.path(rid)) == []
    assert p.results(rid) == {"complete": False, "files": [], "phases": {"A": "ready", "B": "locked", "C": "locked", "D": "locked", "T": "locked"}}
    path, name = p.bundle_zip(rid)
    assert name == "data_quality_results.zip" and path.exists()


def test_a_store_outside_the_workspace_cannot_be_reached_through_the_pipeline(service_settings):
    p = Pipeline(service_settings)
    with pytest.raises(Exception):
        p.view("../../etc")
    assert isinstance(p.store, RunStore)
