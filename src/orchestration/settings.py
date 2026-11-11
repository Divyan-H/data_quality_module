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
      auth_token: null              access token for the API (16+ chars); prefer the DQ_API_TOKEN environment variable
      retention_days: null          delete runs not changed for this many days (null = keep forever)
      min_free_disk_mb: 1024        refuse new uploads when the workspace disk has less free space than this
      agent:
        llm: on                     on = the local model plans instructions the command parser does not recognise
        model: qwen2.5:7b-instruct  the model the Fix-with-agent chat uses (default: llm.model)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS = REPO_ROOT / "config" / "settings.yaml"
ENVIRONMENTS = ("development", "test", "staging", "production")


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
    agent_llm: bool = True
    agent_model: str | None = None              # None = llm_model
    auth_token: str | None = None               # None = no access control (the service must stay on 127.0.0.1)
    retention_days: int | None = None
    min_free_disk_mb: int = 1024
    env: str = "development"                    # DQ_ENV: development | test | staging | production
    audit_db: Path | None = None                # None = <workspace>/../audit.db
    shutdown_grace_s: int = 0                   # on shutdown, let running jobs finish for this long before stopping them
    sign_in_max_failures: int = 5               # failed sign-ins from one address before it is locked out ...
    sign_in_lockout_s: int = 300                # ... for this long
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
    s.auth_token = os.environ.get("DQ_API_TOKEN") or raw.get("auth_token") or None
    if os.environ.get("DQ_RETENTION_DAYS") or raw.get("retention_days"):
        s.retention_days = int(os.environ.get("DQ_RETENTION_DAYS") or raw["retention_days"])
    if "min_free_disk_mb" in raw:
        s.min_free_disk_mb = int(raw["min_free_disk_mb"])
    agent = raw.get("agent") or {}
    if "llm" in agent:
        s.agent_llm = agent["llm"] not in (False, "off", "false", 0)
    s.agent_model = os.environ.get("DQ_AGENT_MODEL") or agent.get("model") or s.agent_model
    if "hygiene_defaults_off" in raw:
        s.hygiene_defaults_off = list(raw["hygiene_defaults_off"] or [])
    if raw.get("cors_origins") is not None:
        s.cors_origins = list(raw["cors_origins"])
    s.env = (os.environ.get("DQ_ENV") or raw.get("env") or s.env).lower()
    if s.env not in ENVIRONMENTS:
        raise ValueError(f"DQ_ENV must be one of {', '.join(ENVIRONMENTS)}; got '{s.env}'")
    if os.environ.get("DQ_SHUTDOWN_GRACE_S") or raw.get("shutdown_grace_s"):
        s.shutdown_grace_s = int(os.environ.get("DQ_SHUTDOWN_GRACE_S") or raw["shutdown_grace_s"])
    if os.environ.get("DQ_MAX_CONCURRENT_JOBS"):
        s.max_concurrent_jobs = int(os.environ["DQ_MAX_CONCURRENT_JOBS"])
    if os.environ.get("DQ_CORS_ORIGINS") is not None:
        s.cors_origins = [o.strip() for o in os.environ["DQ_CORS_ORIGINS"].split(",") if o.strip()]
    elif s.env == "production" and raw.get("cors_origins") is None:
        s.cors_origins = []                     # the built app is served from the same origin; no cross-origin access
    audit = os.environ.get("DQ_AUDIT_DB") or raw.get("audit_db")
    for k, v in overrides.items():
        setattr(s, k, v)
    s.workspace = Path(s.workspace)
    if audit and "audit_db" not in overrides:
        s.audit_db = Path(audit) if Path(audit).is_absolute() else REPO_ROOT / audit
    if s.audit_db is None:
        s.audit_db = s.workspace.parent / "audit.db"
    return s


def production_problems(s: ServiceSettings, host: str) -> list[str]:
    """What must be fixed before the service may start with DQ_ENV=production. Empty = good to go."""
    problems = []
    if not s.auth_token:
        problems.append("DQ_API_TOKEN is not set: production needs an access token (16+ characters).")
    if not os.environ.get("STRUCTURE_HASH_KEY"):
        problems.append("STRUCTURE_HASH_KEY is not set: Phase A would hash values with the public development key.")
    if os.environ.get("DQ_ALLOW_UNAUTHENTICATED") == "1":
        problems.append("DQ_ALLOW_UNAUTHENTICATED=1 is not allowed in production.")
    if os.environ.get("STRUCTURE_LLM_ALLOW_REMOTE") == "1" and not os.environ.get("DQ_ACK_REMOTE_LLM"):
        problems.append("STRUCTURE_LLM_ALLOW_REMOTE=1 lets client data leave this machine for the model. Set "
                        "DQ_ACK_REMOTE_LLM=1 as well to confirm that is intended.")
    return problems
