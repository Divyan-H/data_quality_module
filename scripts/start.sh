#!/usr/bin/env sh
# Start the Data Pipeline service (analyst web app + API) on http://127.0.0.1:8000
#   - (re)builds the web app when frontend/dist is missing or older than the frontend sources
#   - Ollama is not started here: run `ollama serve` in its own terminal first (the app says if it is not reachable)
set -e
cd "$(dirname "$0")/.."
if [ ! -f frontend/dist/index.html ] || [ -n "$(find frontend/src frontend/index.html frontend/package.json -newer frontend/dist/index.html -print -quit)" ]; then
  echo "Building the web app..."
  (cd frontend && { [ -d node_modules ] && [ node_modules -nt package-lock.json ] || npm ci; } && npm run build)
fi
exec python -m src.api
