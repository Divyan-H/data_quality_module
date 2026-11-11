"""Service hardening: security headers, request ids, error shape, sign-in lockout, Job Contract validation, the audit
database, metrics, graceful drain, and the production start-up checks."""
import json
import multiprocessing
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.api.api_server import MAX_REMEMBERED_JOBS, create_app
from src.observability import logs, metrics
from src.observability.context import new_request_id, request_id
from src.orchestration.runner import Busy
from src.orchestration.settings import load_service_settings, production_problems
from src.security.auth import Throttle
from src.storage.audit import MIGRATIONS, AuditLog

from conftest import Flow

TOKEN = "correct-horse-battery-staple"
SMALL = b"Date,Spend\n2025-01-01,5\n2025-01-02,6\n2025-01-03,7\n"


@pytest.fixture
def app_client(tmp_path):
    settings = load_service_settings(workspace=tmp_path / "runs", agent_llm=False)
    app = create_app(str(tmp_path / "storage"), settings)
    with TestClient(app) as c:
        yield app, c
        app.state.pipeline.shutdown()


@pytest.fixture
def secured(tmp_path):
    settings = load_service_settings(workspace=tmp_path / "runs", agent_llm=False, auth_token=TOKEN,
                                     sign_in_max_failures=3, sign_in_lockout_s=60)
    app = create_app(None, settings)
    with TestClient(app) as c:
        yield app, c
        app.state.pipeline.shutdown()


# ── headers, request ids, error shape ───────────────────────────────────────────────────────────────────────────
def test_every_answer_carries_the_security_headers(app_client, secured):
    _, c = app_client
    for r in (c.get("/api/v1/health"), c.get("/api/v1/runs/0123456789abcdef"), c.post("/jobs", json={})):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"
        assert "server" not in r.headers
    refused = secured[1].get("/api/v1/runs")                              # the 401 from the token guard as well
    assert refused.status_code == 401 and refused.headers["x-frame-options"] == "DENY"


def test_hsts_only_over_https(app_client):
    _, c = app_client
    assert "strict-transport-security" not in c.get("/api/v1/health").headers
    assert "max-age" in c.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"}).headers["strict-transport-security"]


def test_request_id_is_generated_or_taken_from_a_well_formed_header(app_client):
    _, c = app_client
    assert len(c.get("/api/v1/health").headers["x-request-id"]) == 32
    assert c.get("/api/v1/health", headers={"X-Request-ID": "trace-1234.abcd"}).headers["x-request-id"] == "trace-1234.abcd"
    hostile = c.get("/api/v1/health", headers={"X-Request-ID": "x\"><script>"}).headers["x-request-id"]
    assert hostile != "x\"><script>" and len(hostile) == 32
    assert new_request_id("short") != "short"


def test_an_unexpected_error_answers_json_with_the_request_id_and_no_internals(app_client):
    app, c = app_client

    def boom(*a, **k):
        raise RuntimeError("secret internal detail /etc/passwd")

    app.state.pipeline.list_runs = boom
    r = c.get("/api/v1/runs", headers={"X-Request-ID": "trace-500-case"})
    assert r.status_code == 500
    body = r.json()["error"]
    assert body["code"] == "internal_error" and body["request_id"] == "trace-500-case"
    assert "secret" not in r.text and "passwd" not in r.text and "Traceback" not in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_refusals_share_one_error_shape(app_client):
    _, c = app_client
    for r in (c.post("/jobs", json={"job_id": "x"}), c.get("/api/v1/nothing-here"), c.post("/jobs/unknown/cancel"),
              c.get("/api/v1/runs/0123456789abcdef"), c.put("/api/v1/runs/0123456789abcdef/inputs", json={"yaml": 5})):
        err = r.json()["error"]
        assert r.status_code >= 400 and isinstance(err["code"], str) and isinstance(err["message"], str), r.text
    for page in ("/docs", "/redoc"):                                      # no Swagger / ReDoc page (CSP forbids their CDN scripts)
        assert "swagger" not in c.get(page).text.lower() and "redoc" not in c.get(page).text.lower()
    assert c.get("/openapi.json").status_code == 200


# ── sign-in lockout ─────────────────────────────────────────────────────────────────────────────────────────────
def test_throttle_counts_failures_inside_the_window():
    t = [0.0]
    th = Throttle(max_failures=3, window_s=60, clock=lambda: t[0])
    for _ in range(2):
        th.failed("1.2.3.4")
    assert th.retry_after("1.2.3.4") == 0
    th.failed("1.2.3.4")
    assert 55 <= th.retry_after("1.2.3.4") <= 61 and th.retry_after("5.6.7.8") == 0
    t[0] = 61
    assert th.retry_after("1.2.3.4") == 0                                 # the window has passed
    th.failed("1.2.3.4")
    th.succeeded("1.2.3.4")
    assert th.retry_after("1.2.3.4") == 0 and th._failures == {}


def test_throttle_stays_bounded():
    th = Throttle(max_failures=3, window_s=60)
    th.MAX_ADDRESSES = 50
    for n in range(500):
        th.failed(f"10.0.{n // 250}.{n % 250}")
    assert len(th._failures) <= 50


def test_sign_in_locks_out_after_repeated_failures_and_records_them(secured, monkeypatch):
    app, c = secured
    monkeypatch.setattr("src.security.auth.FAILED_SIGN_IN_DELAY_S", 0)
    for _ in range(3):
        assert c.post("/api/v1/session", json={"token": "wrong-token-000000"}).status_code == 401
    locked = c.post("/api/v1/session", json={"token": TOKEN})             # even the right token waits now
    assert locked.status_code == 429 and locked.json()["error"]["code"] == "too_many_attempts"
    assert int(locked.headers["retry-after"]) > 0
    actions = [e["action"] for e in app.state.audit.events()]
    assert actions.count("auth.sign_in_failed") == 3 and "auth.signed_in" not in actions
    assert TOKEN not in json.dumps(app.state.audit.events()) and "wrong-token" not in json.dumps(app.state.audit.events())


def test_wrong_bearer_tokens_are_throttled_but_a_missing_token_is_not(secured):
    _, c = secured
    for _ in range(10):
        assert c.get("/api/v1/runs").status_code == 401                   # no header: a visitor, not a guess
    assert c.get("/api/v1/runs", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    for _ in range(3):
        assert c.get("/api/v1/runs", headers={"Authorization": "Bearer guess-guess-guess"}).status_code == 401
    assert c.get("/api/v1/runs", headers={"Authorization": "Bearer guess-guess-guess"}).status_code == 429
    assert c.get("/api/v1/runs", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200   # real callers pass
    assert c.get("/api/v1/metrics").status_code == 401 and c.get("/api/v1/audit").status_code == 401


# ── Job Contract ────────────────────────────────────────────────────────────────────────────────────────────────
def test_job_contract_rejects_malformed_bodies(app_client):
    _, c = app_client
    good = {"job_id": "j-1", "job_type": "data.structure", "output_prefix": "out", "inputs": []}
    for bad in ({}, {**good, "job_id": "../x"}, {**good, "attempt": 0}, {**good, "job_id": "a" * 200},
                {**good, "callback_url": "file:///etc/passwd", "job_token": "t"},
                {**good, "callback_url": "http://example.com/cb"}):         # a callback without a token
        r = c.post("/jobs", json=bad)
        assert r.status_code in (400, 422), (bad, r.text)
        assert "error" in r.json()


def test_job_table_is_bounded_and_resubmission_is_idempotent(app_client, monkeypatch):
    app, c = app_client
    monkeypatch.setattr("src.api.api_server.run_job", lambda body, root, emit, cancelled: {"type": "succeeded"})
    body = {"job_type": "data.structure", "output_prefix": "out", "inputs": []}
    for n in range(MAX_REMEMBERED_JOBS + 40):
        assert c.post("/jobs", json={**body, "job_id": f"job-{n}"}).status_code == 202
    last = f"job-{MAX_REMEMBERED_JOBS + 39}"
    import time
    for _ in range(50):
        if c.post("/jobs", json={**body, "job_id": last}).json()["state"] == "succeeded":
            break
        time.sleep(0.05)
    assert c.post("/jobs", json={**body, "job_id": last}).json()["state"] == "succeeded"       # remembered
    assert c.post("/jobs/job-0/cancel").status_code == 404                                       # the oldest was forgotten
    con = sqlite3.connect(app.state.audit.path)
    assert con.execute("SELECT COUNT(*) FROM audit_events WHERE action = 'gateway_job.accepted'").fetchone()[0] == MAX_REMEMBERED_JOBS + 40


# ── audit database ──────────────────────────────────────────────────────────────────────────────────────────────
def test_a_run_leaves_an_audit_trail_without_cell_values(app_client):
    app, c = app_client
    flow = Flow(c)
    rid = flow.create([("spend.csv", b"Date,Spend,Secret\n2025-01-01,5,TOPSECRETVALUE\n2025-01-02,6,x\n")])
    flow.start(rid, "A", {"llm": "off"})
    flow.wait(rid, "A")
    assert c.post(f"/api/v1/runs/{rid}/phases/A/accept").status_code == 200
    assert c.post(f"/api/v1/runs/{rid}/phases/A/accept", headers={"X-Request-ID": "trace-accept-2"}).status_code == 200
    assert c.post(f"/api/v1/runs/{rid}/phases/C/accept").status_code == 409                  # refused: must not be recorded
    assert c.delete(f"/api/v1/runs/{rid}").status_code == 204
    events = c.get(f"/api/v1/audit?run_id={rid}").json()["events"]
    actions = [e["action"] for e in reversed(events)]
    assert actions == ["run.created", "phase.started", "job.finished", "phase.accepted", "phase.accepted", "run.deleted"]
    by = {e["action"]: e for e in events}
    assert by["phase.started"]["phase"] == "A" and by["job.finished"]["detail"]["outcome"] == "ok"
    assert by["run.created"]["detail"]["files"] == 1 and len(by["run.created"]["request_id"]) == 32
    assert events[1]["request_id"] == "trace-accept-2"
    assert "TOPSECRETVALUE" not in json.dumps(events)
    assert c.get("/api/v1/audit?run_id=../etc").status_code == 422


def _write_many(path: str, worker: int) -> None:
    log = AuditLog(path)
    for n in range(100):
        log.record("stress", f"{worker:016x}", n=n)


def test_audit_survives_concurrent_writers_from_several_processes(tmp_path):
    path = str(tmp_path / "audit.db")
    AuditLog(path)
    procs = [multiprocessing.get_context("spawn").Process(target=_write_many, args=(path, w)) for w in range(4)]
    [p.start() for p in procs]
    [p.join(60) for p in procs]
    assert all(p.exitcode == 0 for p in procs)
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 400
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_audit_schema_migrates_once_and_refuses_a_newer_file(tmp_path):
    path = tmp_path / "audit.db"
    first = AuditLog(path)
    first.record("x", "0123456789abcdef")
    assert AuditLog(path).schema_version() == len(MIGRATIONS) and len(AuditLog(path).events()) == 1   # reopening keeps rows
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    con.close()
    with pytest.raises(RuntimeError, match="newer than this build"):
        AuditLog(path)


def test_audit_rows_are_capped_and_llm_calls_are_summarised(tmp_path):
    log = AuditLog(tmp_path / "a.db")
    log.record("big", "0123456789abcdef", blob="v" * 10_000)
    assert log.events()[0]["detail"] == {"truncated": True, "keys": ["blob"]}
    log.llm_call(purpose="agent.plan", model="m:7b", latency_ms=1200, outcome="ok", prompt_chars=900, answer_chars=80)
    log.llm_call(purpose="agent.plan", model="m:7b", latency_ms=800, outcome="ok")
    log.llm_call(purpose="agent.plan", model="m:7b", latency_ms=30000, outcome="no_answer")
    summary = log.llm_summary()
    assert summary["total"] == 3
    assert {(r["outcome"], r["n"], r["avg_ms"]) for r in summary["by_purpose_outcome"]} == {("ok", 2, 1000), ("no_answer", 1, 30000)}
    with pytest.raises(sqlite3.IntegrityError):                            # the outcome vocabulary is a constraint
        sqlite3.connect(tmp_path / "a.db").execute(
            "INSERT INTO llm_calls (at, purpose, model, latency_ms, outcome) VALUES ('t','p','m',1,'made-up')")
    assert AuditLog(None).events() == [] and AuditLog(None).record("noop") is None        # disabled = silent no-op


# ── metrics, logs, drain, production checks ─────────────────────────────────────────────────────────────────────
def test_metrics_are_prometheus_text_with_fixed_label_vocabularies(app_client):
    _, c = app_client
    metrics.reset()
    flow = Flow(c)
    rid = flow.create([("spend.csv", SMALL)])
    flow.start(rid, "A", {"llm": "off"})
    flow.wait(rid, "A")
    text = c.get("/api/v1/metrics").text
    assert 'dq_jobs_started_total{action="run",phase="A"} 1' in text
    assert 'dq_jobs_finished_total{action="run",outcome="ok",phase="A"} 1' in text
    assert "# TYPE dq_jobs_active gauge" in text and "dq_workspace_free_mb" in text
    assert rid not in text                                                # ids never become label values


def test_json_logs_carry_the_request_id_and_extra_fields(capsys):
    import logging
    logs.configure("INFO", "json")
    token = request_id.set("trace-log-test-1")
    try:
        logging.getLogger("dq.test").info("hello", extra={"run_id": "abc", "count": 3})
    finally:
        request_id.reset(token)
        logging.getLogger().handlers = [h for h in logging.getLogger().handlers if h.get_name() != "dq"]
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["msg"] == "hello" and line["request_id"] == "trace-log-test-1" and line["run_id"] == "abc" and line["count"] == 3
    assert line["level"] == "info" and line["logger"] == "dq.test"


def test_draining_refuses_new_jobs(app_client):
    app, c = app_client
    pipe = app.state.pipeline
    rid = Flow(c).create([("spend.csv", SMALL)])
    assert pipe.runner.drain(0) is True
    with pytest.raises(Busy, match="shutting down"):
        pipe.runner.launch(rid, "A", "run", {})
    r = c.post(f"/api/v1/runs/{rid}/phases/A/start", json={"llm": "off"})
    assert r.status_code == 429 and c.get(f"/api/v1/runs/{rid}").json()["phases"][0]["status"] == "ready"


def test_production_start_up_checks(tmp_path, monkeypatch):
    for var in ("STRUCTURE_HASH_KEY", "DQ_ALLOW_UNAUTHENTICATED", "STRUCTURE_LLM_ALLOW_REMOTE", "DQ_ACK_REMOTE_LLM", "DQ_ENV"):
        monkeypatch.delenv(var, raising=False)
    s = load_service_settings(workspace=tmp_path / "runs")
    assert s.env == "development" and s.audit_db == tmp_path / "audit.db"
    assert len(production_problems(s, "0.0.0.0")) == 2                     # no token, no hash key
    monkeypatch.setenv("STRUCTURE_HASH_KEY", "k" * 32)
    s = load_service_settings(workspace=tmp_path / "runs", auth_token=TOKEN)
    assert production_problems(s, "0.0.0.0") == []
    monkeypatch.setenv("STRUCTURE_LLM_ALLOW_REMOTE", "1")                  # data would leave the machine: say so
    assert "leave this machine" in production_problems(s, "0.0.0.0")[0]
    monkeypatch.setenv("DQ_ACK_REMOTE_LLM", "1")
    assert production_problems(s, "0.0.0.0") == []
    monkeypatch.setenv("DQ_ENV", "production")
    assert load_service_settings(workspace=tmp_path / "runs").cors_origins == []
    monkeypatch.setenv("DQ_ENV", "prod")
    with pytest.raises(ValueError, match="DQ_ENV"):
        load_service_settings(workspace=tmp_path / "runs")
