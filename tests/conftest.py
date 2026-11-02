"""Shared helpers for the Phase A (data.structure) tests. Fixtures are built in-memory, never committed."""
import json
from pathlib import Path

import openpyxl
import pyarrow.parquet as pq
import pytest

from src.structure.pipeline import run_structure


def run(tmp_path: Path, files, config=None, out_name="out", **extra):
    """Run Structure on files (one source, s-1) and return (result, output dir)."""
    entries = [{"path": str(p), "source_id": extra.pop("source_id", "s-1"), "file_id": f"f-{i}",
                "relative_path": p.name} for i, p in enumerate(files, 1)]
    out = tmp_path / out_name
    res = run_structure({"files": entries, "config": config or {}, "output_dir": str(out), **extra})
    return res, out


def read_json(out: Path, name: str):
    return json.loads((out / name).read_text(encoding="utf8"))


def tables(out: Path):
    return sorted(out.glob("files/*/tables/*/data.parquet"))


def rows(path):
    return pq.read_table(path).to_pylist()


def types(res):
    return {a["type"]: a["severity"] for a in res["action_items"]}


def cells(out: Path):
    return pq.read_table(next(out.glob("files/*/cells.parquet"))).to_pylist()


def save(wb: openpyxl.Workbook, path: Path) -> Path:
    wb.save(path)
    return path


def simple_sheet(ws, header=("Date", "Channel", "Spend"), n=6, row0=1, col0=1):
    """A small clean table with its header at (row0, col0)."""
    for j, h in enumerate(header):
        ws.cell(row0, col0 + j, h)
    for i in range(n):
        ws.cell(row0 + 1 + i, col0, f"2025-01-{i + 1:02d}")
        ws.cell(row0 + 1 + i, col0 + 1, "TV" if i % 2 else "Radio")
        ws.cell(row0 + 1 + i, col0 + 2, 10.5 * (i + 1))


@pytest.fixture
def make_xlsx(tmp_path):
    def _make(name="book.xlsx", builder=None):
        wb = openpyxl.Workbook()
        if builder:
            builder(wb)
        return save(wb, tmp_path / name)
    return _make


class ScriptedModel:
    """A stand-in Ollama transport. `choices` maps a decision kind to a choice or `f(options, vote_index)`;
    `fail_votes` are vote indices that answer with garbage. Every prompt is kept for canary checks."""

    SEEDS = {11: 0, 22: 1, 33: 2, 44: 3, 55: 4}

    def __init__(self, choices=None, fail_votes=(), digest="sha256:abc"):
        self.choices, self.fail_votes, self.digest = choices or {}, set(fail_votes), digest
        self.prompts, self.chats = [], 0

    def __call__(self, path, payload):
        if path == "/api/version":
            return {"version": "test"}
        if path == "/api/tags":
            return {"models": [{"name": "test-model", "digest": self.digest}]}
        self.chats += 1
        prompt = payload["messages"][0]["content"]
        self.prompts.append(prompt)
        vote = self.SEEDS[payload["options"]["seed"]]
        if vote in self.fail_votes:
            return {"message": {"content": "not json"}}
        kind = next(line.split()[1].rstrip(":") for line in prompt.splitlines() if line.startswith("d1 "))
        options = payload["format"]["properties"]["d1"]["properties"]["choice"]["enum"]
        pick = self.choices.get(kind)
        if callable(pick):
            pick = pick(options, vote)
        if pick is None:                 # default: agree with the natural reading
            pick = min((o for o in options if o.startswith("row:")), key=lambda o: int(o[4:]), default=None) \
                if kind == "header_row" else ("table" if "table" in options else options[0])
        return {"message": {"content": json.dumps({"d1": {"why": "scripted", "choice": pick}})}}


LLM_PIN = {"model": "test-model", "model_digest": "sha256:abc", "prompt_version": "structure-choice/2"}


def llm_config(**llm):
    return {"structure": {"llm": {"mode": "assist", **llm}}}


# --------------------------------------------------------------------------------------------------------------------
# Web service (src/api, src/orchestration): a real app over a temporary workspace. Phases run in real worker processes.
import sys
import time

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))


class Flow:
    """Drives the analyst API the way the browser does."""

    def __init__(self, client):
        self.c = client

    def create(self, files, llm="off", name="test run"):
        """files: [(name, bytes | Path)]; the files are read, never moved."""
        payload = [("files", (n, d.read_bytes() if isinstance(d, Path) else d)) for n, d in files]
        r = self.c.post("/api/v1/runs", files=payload, data={"llm": llm, "name": name})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def view(self, rid):
        r = self.c.get(f"/api/v1/runs/{rid}")
        assert r.status_code == 200, r.text
        return r.json()

    def phase(self, rid, key):
        return next(p for p in self.view(rid)["phases"] if p["key"] == key)

    def wait(self, rid, key, timeout=240):
        t0 = time.time()
        while time.time() - t0 < timeout:
            p = self.phase(rid, key)
            if p["status"] != "running":
                return p
            time.sleep(0.25)
        raise AssertionError(f"phase {key} still running after {timeout}s")

    def start(self, rid, key, body=None, expect=200):
        r = self.c.post(f"/api/v1/runs/{rid}/phases/{key}/start", json=body or {})
        assert r.status_code == expect, r.text
        return r

    def run_phase(self, rid, key, body=None):
        self.start(rid, key, body)
        p = self.wait(rid, key)
        assert p["status"] == "review", p
        return p

    def accept(self, rid, key, expect=200):
        r = self.c.post(f"/api/v1/runs/{rid}/phases/{key}/accept")
        assert r.status_code == expect, r.text
        return r

    def summary(self, rid, key):
        """A phase's summary. C and D list one result per checked table; with a single table its fields are merged in."""
        s = self.c.get(f"/api/v1/runs/{rid}/phases/{key}/summary").json()
        return {**s["tables"][0], **s} if key in "CD" and len(s.get("tables", [])) == 1 else s

    def report_id(self, rid, prefix):
        """Artifact id of the one per-table review report whose id starts with `prefix` (e.g. "timeseries-report-")."""
        ids = [f["id"] for f in self.c.get(f"/api/v1/runs/{rid}/results").json()["files"] if f["id"].startswith(prefix)]
        assert len(ids) == 1, ids
        return ids[0]

    def data_id(self, rid, ext, sheet=None):
        """Artifact id of a final data file (csv / parquet / xlsx) - of the table from `sheet`, or the only one."""
        ids = [f["id"] for f in self.c.get(f"/api/v1/runs/{rid}/results").json()["files"]
               if f["id"].startswith("data-") and f["id"].endswith("-" + ext)
               and (sheet is None or f["id"].startswith(f"data-{sheet.lower()}-"))]
        assert len(ids) == 1, ids
        return ids[0]

    def config(self, rid, key, data: bytes, name="config.xlsx", expect=200):
        r = self.c.post(f"/api/v1/runs/{rid}/phases/{key}/config", files={"file": (name, data)})
        assert r.status_code == expect, r.text
        return r

    def download(self, rid, artifact):
        r = self.c.get(f"/api/v1/runs/{rid}/files/{artifact}")
        assert r.status_code == 200, r.text
        return r.content

    def upload_review(self, rid, key, data: bytes, name="review.xlsx", expect=200, **form):
        r = self.c.post(f"/api/v1/runs/{rid}/phases/{key}/review", files={"file": (name, data)}, data=form)
        assert r.status_code == expect, r.text
        return r


@pytest.fixture
def service_settings(tmp_path):
    from src.orchestration.settings import load_service_settings
    return load_service_settings(workspace=tmp_path / "runs", max_upload_mb=5, max_concurrent_jobs=2)


@pytest.fixture
def client(service_settings):
    from fastapi.testclient import TestClient
    from src.api.api_server import create_app
    app = create_app(None, service_settings)
    with TestClient(app) as c:
        yield c
        app.state.pipeline.shutdown()


@pytest.fixture
def flow(client):
    return Flow(client)


@pytest.fixture(scope="session")
def weekly_files(tmp_path_factory):
    """Phase D's planted weekly workbook and its client config (14 planted findings, see build_phase_d_workbook)."""
    import build_phase_d_workbook as wbk
    d = tmp_path_factory.mktemp("weekly")
    return {"data": wbk.build_data(d / "weekly.xlsx"), "config": wbk.build_config(d / "client_config.xlsx"), "wbk": wbk}
