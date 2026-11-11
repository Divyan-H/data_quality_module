"""Regression tests for the defects found in the November 2026 audit (docs/audit/01_initial_assessment.md)."""
import time

import pytest
from fastapi.testclient import TestClient

from src.agent import safe_regex
from src.agent.errors import AgentError
from src.api.api_server import create_app
from src.orchestration.settings import load_service_settings
from src.structure.job import run_job

from conftest import Flow
from test_orchestration import big_csv, wait_until

SMALL = b"Date,Spend\n2025-01-01,5\n2025-01-02,6\n2025-01-03,7\n"


# ── F-01: Job Contract refs must stay inside the storage root ───────────────────────────────────────────────────
def job_body(ref: str, output_prefix: str) -> dict:
    return {"job_id": "j1", "job_type": "data.structure", "output_prefix": output_prefix, "params": {"llm": {"mode": "off"}},
            "inputs": [{"kind": "raw_file", "ref": ref, "source_id": "s", "file_id": "f", "content_hash": "sha256:0"}]}


@pytest.mark.parametrize("ref_kind", ["absolute", "dotdot"])
def test_job_cannot_read_an_input_outside_the_storage_root(tmp_path, ref_kind):
    root, outside = tmp_path / "storage", tmp_path / "outside"
    root.mkdir(), outside.mkdir()
    (outside / "secret.csv").write_bytes(SMALL)
    ref = str(outside / "secret.csv") if ref_kind == "absolute" else "../outside/secret.csv"
    ev = run_job(job_body(ref, "out"), root)
    assert ev["type"] == "failed" and ev["error"]["code"] == "input_invalid"
    assert not (root / "out").exists()                                   # refused before anything was written


@pytest.mark.parametrize("prefix", ["../outside/written", "/tmp/dq_audit_should_not_exist", "", "."])
def test_job_cannot_write_its_output_outside_the_storage_root(tmp_path, prefix):
    root, outside = tmp_path / "storage", tmp_path / "outside"
    root.mkdir(), outside.mkdir()
    (root / "in.csv").write_bytes(SMALL)
    ev = run_job(job_body("in.csv", prefix), root)
    assert ev["type"] == "failed" and ev["error"]["code"] == "input_invalid"
    assert list(outside.iterdir()) == [] and sorted(p.name for p in root.iterdir()) == ["in.csv"]


# ── F-02: a refused restart must leave the phase's results alone ────────────────────────────────────────────────
def test_restart_refused_at_the_job_limit_keeps_the_results(tmp_path):
    settings = load_service_settings(workspace=tmp_path / "runs", max_upload_mb=100, max_concurrent_jobs=1, agent_llm=False)
    app = create_app(None, settings)
    with TestClient(app) as c:
        flow = Flow(c)
        try:
            done = flow.create([("small.csv", SMALL)])
            flow.start(done, "A", {"llm": "off"})
            flow.wait(done, "A")
            before = [a["id"] for a in c.get(f"/api/v1/runs/{done}").json()["phases"][0]["artifacts"]]
            assert before
            busy = flow.create([("big.csv", big_csv())])
            flow.start(busy, "A", {"llm": "off"})                                    # takes the service's only job slot
            wait_until(lambda: c.get(f"/api/v1/runs/{busy}").json()["job"])
            r = c.post(f"/api/v1/runs/{done}/phases/A/start", json={"llm": "off", "restart": True})
            assert r.status_code == 429 and r.json()["error"]["code"] == "busy"
            phase = c.get(f"/api/v1/runs/{done}").json()["phases"][0]
            assert phase["status"] == "review"
            assert [a["id"] for a in phase["artifacts"]] == before       # nothing was deleted
            assert c.get(f"/api/v1/runs/{done}/files/{before[0]}").status_code == 200
        finally:
            app.state.pipeline.shutdown()


# ── F-03: patterns from the analyst or the model cannot hang a worker ───────────────────────────────────────────
def test_a_backtracking_pattern_is_stopped_quickly():
    t0 = time.monotonic()
    with pytest.raises(AgentError) as e:
        safe_regex.compiled(r"(a|aa)+$").search("a" * 40 + "!")     # exponential in both `re` and `regex`
    assert e.value.code == "pattern_too_slow" and time.monotonic() - t0 < 3


def test_patterns_are_length_limited_and_syntax_checked():
    with pytest.raises(AgentError) as e:
        safe_regex.check("a" * (safe_regex.MAX_PATTERN_LEN + 1))
    assert e.value.code == "bad_pattern"
    with pytest.raises(AgentError):
        safe_regex.check("(unclosed")
    assert safe_regex.compiled(r"^\d{4}-\d{2}$").search("2025-01") is not None
    assert safe_regex.compiled("delhi", True).search("New DELHI") is not None
    assert safe_regex.compiled(r"(\w+)@(\w+)").sub(r"\2:\1", "a@b") == "b:a"
