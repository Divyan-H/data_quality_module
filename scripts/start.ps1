# Start the Data Pipeline service (analyst web app + API) on http://127.0.0.1:8000
#   - (re)builds the web app when frontend/dist is missing or older than the frontend sources
#   - Ollama is NOT started here: run `ollama serve` in its own terminal first (the app tells you if it is not reachable)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$built = "frontend/dist/index.html"
$stale = -not (Test-Path $built)
if (-not $stale) {
    $builtAt = (Get-Item $built).LastWriteTime
    $sources = @(Get-ChildItem frontend/src -Recurse -File) + @(Get-Item frontend/index.html, frontend/package.json)
    $stale = [bool]($sources | Where-Object { $_.LastWriteTime -gt $builtAt } | Select-Object -First 1)
}
if ($stale) {
    Write-Host "Building the web app..."
    Push-Location frontend
    if (-not (Test-Path node_modules) -or (Get-Item package-lock.json).LastWriteTime -gt (Get-Item node_modules).LastWriteTime) {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
    }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
    Pop-Location
}
python -m src.api
