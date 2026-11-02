"""The LLM availability probe (src/llm_interface/ollama_status.py): reachable, model present, or a clear reason why not."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.llm_interface import ollama_status
from src.llm_interface.ollama_status import probe


@pytest.fixture(autouse=True)
def _no_cache():
    ollama_status._cache.clear()
    yield
    ollama_status._cache.clear()


@pytest.fixture
def fake_ollama():
    """A tiny local server that answers like Ollama's /api/version and /api/tags."""
    models = [{"name": "llama3.2:3b", "model": "llama3.2:3b", "digest": "sha256:abc123"}]

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"/api/version": {"version": "9.9.9"}, "/api/tags": {"models": models}}.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body or {}).encode())

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", models
    server.shutdown()


def test_available_when_the_model_is_installed(fake_ollama):
    url, _ = fake_ollama
    s = probe("llama3.2:3b", url, fresh=True)
    assert s.available and s.reason is None and s.version == "9.9.9" and s.digest == "sha256:abc123"
    assert s.installed == ["llama3.2:3b"]


def test_model_missing_names_the_fix(fake_ollama):
    url, _ = fake_ollama
    s = probe("qwen2.5:7b", url, fresh=True)
    assert not s.available and s.reason == "llm_model_missing"
    assert "ollama pull qwen2.5:7b" in s.message and s.installed == ["llama3.2:3b"]


def test_unreachable_server_says_to_start_ollama():
    s = probe("llama3.2:3b", "http://127.0.0.1:9", fresh=True)        # nothing listens on the discard port
    assert not s.available and s.reason == "llm_unreachable"
    assert "ollama serve" in s.message


def test_a_remote_address_is_refused_by_default(monkeypatch):
    monkeypatch.delenv("STRUCTURE_LLM_ALLOW_REMOTE", raising=False)
    s = probe("llama3.2:3b", "http://192.0.2.10:11434", fresh=True)
    assert not s.available and s.reason == "llm_url_not_loopback"


def test_the_environment_variable_sets_the_address(fake_ollama, monkeypatch):
    url, _ = fake_ollama
    monkeypatch.setenv("STRUCTURE_LLM_URL", url)
    assert probe("llama3.2:3b", fresh=True).available


def test_answers_are_cached_briefly(fake_ollama):
    url, models = fake_ollama
    assert probe("llama3.2:3b", url).available
    models.clear()
    assert probe("llama3.2:3b", url).available                       # still the cached answer
    assert not probe("llama3.2:3b", url, fresh=True).available       # a fresh probe sees the change
