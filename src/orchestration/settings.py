"""Service settings: the `service:` section of config/settings.yaml, overridable by environment variables.

    service:
      workspace: ./data/runs        where run folders live
      max_upload_mb: 500            total size of the files of one run
      max_review_upload_mb: 200     one edited review / config workbook
      max_concurrent_jobs: 2        phase jobs running at once (each is its own process)
      job_timeout_min: 180
      llm:
        model: llama3.2:3b
        review: near_ties           near_ties = ask the model only where the rules are unsure; all = ask on every decision
      hygiene_defaults_off: [D-14, D-27]  automatic fixes that start switched off in the UI (see docs/decisions/decision_logs.md)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS = REPO_ROOT / "config" / "settings.yaml"


@dataclass
class ServiceSettings:
    settings_path: str = str(DEFAULT_SETTINGS)
    workspace: Path = REPO_ROOT / "data" / "runs"
    max_upload_mb: int = 500
    max_review_upload_mb: int = 200
    max_concurrent_jobs: int = 2
    job_timeout_min: int = 180
    llm_model: str = "llama3.2:3b"
    llm_review: str = "all"
    hygiene_defaults_off: list[str] = field(default_factory=lambda: ["D-14", "D-27"])
    frontend_dir: Path = REPO_ROOT / "frontend" / "dist"
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])


def load_service_settings(path: str | None = None, **overrides) -> ServiceSettings:
    path = str(path or os.environ.get("DQ_SETTINGS") or DEFAULT_SETTINGS)
    raw: dict = {}
    if Path(path).exists():
        raw = (yaml.safe_load(Path(path).read_text(encoding="utf8")) or {}).get("service", {}) or {}
    s = ServiceSettings(settings_path=path)
    ws = os.environ.get("DQ_WORKSPACE") or raw.get("workspace")
    if ws:
        p = Path(ws)
        s.workspace = p if p.is_absolute() else (REPO_ROOT / p)
    for key in ("max_upload_mb", "max_review_upload_mb", "max_concurrent_jobs", "job_timeout_min"):
        if key in raw:
            setattr(s, key, int(raw[key]))
    llm = raw.get("llm") or {}
    s.llm_model = os.environ.get("DQ_LLM_MODEL") or llm.get("model") or s.llm_model
    s.llm_review = llm.get("review") or s.llm_review
    if "hygiene_defaults_off" in raw:
        s.hygiene_defaults_off = list(raw["hygiene_defaults_off"] or [])
    if raw.get("cors_origins") is not None:
        s.cors_origins = list(raw["cors_origins"])
    for k, v in overrides.items():
        setattr(s, k, v)
    s.workspace = Path(s.workspace)
    return s
