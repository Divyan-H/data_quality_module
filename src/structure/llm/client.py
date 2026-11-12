"""Ollama client (spec 10.2, 10.5): loopback only, digest-pinned, structured output, nothing logged."""
from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://localhost:11434"
CALL_TIMEOUT_S = 60
NUM_CTX, NUM_PREDICT = 16_384, 512
# Seeds and temperature of each vote; option order differs per vote (as built, reversed, rotated).
VOTE_SEEDS = [11, 22, 33, 44, 55]
VOTE_TEMPERATURE = 0.3


def is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_allowed(url: str) -> bool:
    """May prompts be sent to `url`? Yes for this machine. Any other host must be named in DQ_LLM_ALLOWED_HOSTS
    (a comma-separated list of exact host names, e.g. the `ollama` container of the Compose file). The older
    STRUCTURE_LLM_ALLOW_REMOTE=1 allows every host and is kept only for existing set-ups; prefer the list."""
    if is_loopback(url):
        return True
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    allowed = {h.strip().lower() for h in os.environ.get("DQ_LLM_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return host in allowed or os.environ.get("STRUCTURE_LLM_ALLOW_REMOTE") == "1"


def vote_settings(votes: int, index: int) -> tuple[int, float]:
    """(seed, temperature) of vote `index`; a single vote is deterministic (spec 10.5)."""
    return (VOTE_SEEDS[0], 0.0) if votes == 1 else (VOTE_SEEDS[index], VOTE_TEMPERATURE)


def option_order(options: list[str], index: int) -> list[str]:
    """As built, reversed, rotated by one; the pattern repeats for votes 4 and 5."""
    k = index % 3
    if k == 0:
        return list(options)
    if k == 1:
        return list(reversed(options))
    return options[1:] + options[:1]


class OllamaClient:
    """`transport(path, payload | None) -> dict` can be replaced in tests; the default uses urllib."""

    def __init__(self, url: str | None, model: str, digest: str | None, transport=None):
        self.url = (url or os.environ.get("STRUCTURE_LLM_URL") or DEFAULT_URL).rstrip("/")
        self.model, self.digest = model, digest
        self.transport = transport or self._http
        self.version: str | None = None
        self.unavailable_reason: str | None = None

    def _http(self, path: str, payload: dict | None) -> dict:
        req = urllib.request.Request(
            self.url + path, data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="GET" if payload is None else "POST")
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as resp:
            return json.loads(resp.read())

    def check(self) -> bool:
        """Loopback rule, then model digest. A different model is never used silently."""
        if not host_allowed(self.url):
            self.unavailable_reason = "llm_url_not_loopback"
            return False
        try:
            self.version = self.transport("/api/version", None).get("version")
            tags = self.transport("/api/tags", None).get("models", [])
        except (urllib.error.URLError, OSError, ValueError):
            self.unavailable_reason = "llm_unreachable"
            return False
        found = next((m for m in tags if m.get("name") == self.model or m.get("model") == self.model), None)
        if found is None:
            self.unavailable_reason = "llm_model_missing"
            return False
        if self.digest and found.get("digest", "").split(":")[-1] != self.digest.split(":")[-1]:
            self.unavailable_reason = "llm_digest_mismatch"
            return False
        self.digest = self.digest or found.get("digest")    # unpinned: record the build that answers
        return True

    def chat(self, prompt: str, schema: dict, seed: int, temperature: float, num_predict: int = NUM_PREDICT,
             system: str | None = None, prompt_version: str | None = None) -> dict | None:
        """One vote. Retries once on a transport error or an unparsable answer; None counts as no answer.
        `num_predict` and `system` are for callers that need a longer answer or a separate system prompt (the agent);
        `prompt_version` is not sent to the model: it labels the call in the audit log."""
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        payload = {"model": self.model, "stream": False, "format": schema, "messages": messages,
                   "options": {"num_ctx": NUM_CTX, "num_predict": num_predict, "seed": seed,
                               "temperature": temperature}}
        for _attempt in range(2):
            try:
                content = self.transport("/api/chat", payload)["message"]["content"]
                return json.loads(content)
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
                continue
        return None
