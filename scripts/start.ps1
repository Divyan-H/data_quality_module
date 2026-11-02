# Start the Data Pipeline service (analyst web app + API) on http://127.0.0.1:8000
#   - builds the frontend once if frontend/dist is missing
#   - Ollama is NOT started here: run `ollama serve` in its own terminal first (the app tells you if it is not reachable)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path "frontend/dist/index.html")) {
    Push-Location frontend
    npm ci
    npm run build
    Pop-Location
}
python -m src.api
