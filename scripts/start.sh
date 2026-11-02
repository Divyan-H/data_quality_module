#!/usr/bin/env sh
# Start the Data Pipeline service (analyst web app + API) on http://127.0.0.1:8000
# Ollama is not started here: run `ollama serve` in its own terminal first.
set -e
cd "$(dirname "$0")/.."
if [ ! -f frontend/dist/index.html ]; then
  (cd frontend && npm ci && npm run build)
fi
exec python -m src.api
