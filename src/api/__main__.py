"""Run the service:  python -m src.api   (analyst web app + Job Contract endpoints on one port)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from src.api.api_server import start_api_server
from src.orchestration.settings import DEFAULT_SETTINGS


def main() -> None:
    path = Path(os.environ.get("DQ_SETTINGS") or DEFAULT_SETTINGS)
    api = (yaml.safe_load(path.read_text(encoding="utf8")) or {}).get("api", {}) if path.exists() else {}
    api["host"] = os.environ.get("DQ_HOST") or api.get("host") or "127.0.0.1"
    api["port"] = int(os.environ.get("DQ_PORT") or api.get("port") or 8000)
    print(f"Data Pipeline service on http://{api['host']}:{api['port']}  (workspace: see service.workspace in {path.name})")
    start_api_server(api)


if __name__ == "__main__":
    main()
