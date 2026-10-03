# Start the Autonomous CI/CD Healing Agent on Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\start.ps1
#
# Builds the dashboard once, then runs a single process that serves both the
# UI and the API on one port -- no second terminal, no CORS, no proxy.

param([int]$Port = 8000, [switch]$SkipBuild)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Resolve-Python {
    foreach ($c in @("python", "py")) {
        try {
            $v = & $c --version 2>&1
            # A Microsoft Store stub prints nothing and exits 0.
            if ($LASTEXITCODE -eq 0 -and "$v" -match "Python 3") { return $c }
        } catch { }
    }
    throw "Python 3.11+ not found. Install from python.org with 'Add to PATH' ticked."
}

$py = Resolve-Python
Write-Host "Using $py ($(& $py --version 2>&1))" -ForegroundColor Cyan

Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
& $py -m pip install --quiet --disable-pip-version-check -r backend/requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

if (-not $SkipBuild) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw "npm not found. Install Node 18+ from nodejs.org."
    }
    Write-Host "Building dashboard..." -ForegroundColor Cyan
    Push-Location frontend
    if (-not (Test-Path node_modules)) { npm install --no-audit --no-fund }
    npm run build
    Pop-Location
    if (-not (Test-Path frontend/dist/index.html)) { throw "Frontend build produced no output." }
}

# run.py loads .env from the repo root (API keys, memory, model settings) before
# starting uvicorn; launching uvicorn directly would silently ignore that file.
if (Test-Path (Join-Path $root ".env")) {
    Write-Host "Loading settings from .env" -ForegroundColor Cyan
} else {
    Write-Host "No .env found - running without API keys (copy .env.example to .env)" -ForegroundColor Yellow
}
$url = "http://127.0.0.1:$Port"
Write-Host "`nServing dashboard and API on $url  (Ctrl+C to stop)`n" -ForegroundColor Green
Start-Job { Start-Sleep 3; Start-Process $using:url } | Out-Null

& $py backend/run.py --port $Port
