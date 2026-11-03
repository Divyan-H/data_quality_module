"""Operational safeguards: readiness for a load balancer, refusing uploads when the disk is nearly full, and deleting
runs past the retention period (never one with a job in progress)."""
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from src.api.api_server import create_app
from src.orchestration.orchestrator import Pipeline
from src.orchestration.settings import load_service_settings


def test_ready_reports_its_checks(client):
    r = client.get("/api/v1/ready")
    body = r.json()
    assert r.status_code == 200 and body["ready"] and body["checks"]["workspace_writable"]
    assert {"free_disk_mb", "disk_ok", "jobs", "llm_available"} <= set(body["checks"])


def test_a_nearly_full_disk_refuses_new_runs_and_reports_not_ready(tmp_path):
    app = create_app(None, load_service_settings(workspace=tmp_path / "runs", min_free_disk_mb=10**9))
    with TestClient(app) as c:
        assert c.get("/api/v1/ready").status_code == 503
        r = c.post("/api/v1/runs", files=[("files", ("a.csv", b"x,y\n1,2\n"))], data={"llm": "off"})
        assert r.status_code == 507 and r.json()["error"]["code"] == "disk_full"
        app.state.pipeline.shutdown()


def _age(pipe: Pipeline, rid: str, days: int) -> None:
    path = pipe.store.path(rid) / "state.json"
    st = json.loads(path.read_text())
    st["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    path.write_text(json.dumps(st))


def test_runs_past_the_retention_period_are_deleted(tmp_path):
    pipe = Pipeline(load_service_settings(workspace=tmp_path / "runs", retention_days=30))
    try:
        files = []
        for name in ("old.csv", "new.csv"):
            f = tmp_path / name
            f.write_text("x,y\n1,2\n")
            files.append(f)
        old = pipe.create_run("old", [("old.csv", files[0])])
        new = pipe.create_run("new", [("new.csv", files[1])])
        _age(pipe, old, 45)
        _age(pipe, new, 5)
        assert pipe.purge_expired() == [old]
        assert [r["id"] for r in pipe.list_runs()] == [new]
    finally:
        pipe.shutdown()


def test_without_a_retention_period_nothing_is_deleted(tmp_path):
    pipe = Pipeline(load_service_settings(workspace=tmp_path / "runs"))
    try:
        f = tmp_path / "a.csv"
        f.write_text("x\n1\n")
        rid = pipe.create_run("a", [("a.csv", f)])
        _age(pipe, rid, 3650)
        assert pipe.purge_expired() == [] and pipe.list_runs()
    finally:
        pipe.shutdown()
