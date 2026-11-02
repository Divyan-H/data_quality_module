"""Is the local LLM (Ollama) usable? One probe, one answer the UI can show.

The pipeline calls Ollama through `src.structure.llm.client.OllamaClient`; this module only asks whether that call
can succeed (server reachable, model pulled) so a run can warn before it starts instead of failing in the middle.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass

from src.structure.llm.client import DEFAULT_URL, OllamaClient, is_loopback

DEFAULT_MODEL = "llama3.2:3b"
PROBE_TIMEOUT_S = 3.0
CACHE_TTL_S = 5.0

REASONS = {
    "llm_url_not_loopback": "The LLM address is not on this machine and remote models are not allowed "
                            "(set STRUCTURE_LLM_ALLOW_REMOTE=1 to allow it).",
    "llm_unreachable": "Ollama is not reachable. Start it with `ollama serve` in a terminal.",
    "llm_model_missing": "Ollama is running but the model is not installed. Install it with `ollama pull {model}`.",
    "llm_digest_mismatch": "The installed model does not match the pinned build.",
}


@dataclass
class LlmStatus:
    available: bool
    url: str
    model: str
    reason: str | None = None          # machine-readable: llm_unreachable | llm_model_missing | ...
    message: str = ""                  # human-readable
    version: str | None = None
    digest: str | None = None
    installed: list[str] | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class _Quick(OllamaClient):
    """The client with a short timeout: a status check must not hang the page."""

    def _http(self, path: str, payload: dict | None) -> dict:
        import json
        import urllib.request
        req = urllib.request.Request(self.url + path, data=None if payload is None else json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="GET" if payload is None else "POST")
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            return json.loads(resp.read())


_cache: dict[tuple, tuple[float, LlmStatus]] = {}


def probe(model: str | None = None, url: str | None = None, *, fresh: bool = False) -> LlmStatus:
    model = model or os.environ.get("DQ_LLM_MODEL") or DEFAULT_MODEL
    url = (url or os.environ.get("STRUCTURE_LLM_URL") or DEFAULT_URL).rstrip("/")
    key = (model, url)
    hit = _cache.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    client = _Quick(url, model, None)
    ok = client.check()
    reason = client.unavailable_reason
    status = LlmStatus(
        available=ok, url=url, model=model, reason=reason, version=client.version, digest=client.digest if ok else None,
        message=("Ollama is ready." if ok else REASONS.get(reason or "", "The LLM is not available.").format(model=model)),
        installed=_installed(client) if reason in (None, "llm_model_missing") else None)
    if not is_loopback(url) and not ok and reason is None:
        status.reason = "llm_url_not_loopback"
    _cache[key] = (time.monotonic(), status)
    return status


def _installed(client: OllamaClient) -> list[str] | None:
    try:
        return sorted(m.get("name", "") for m in client.transport("/api/tags", None).get("models", []))
    except Exception:  # noqa: BLE001 - informational only
        return None
