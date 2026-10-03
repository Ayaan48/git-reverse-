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

$env:PYTHONPATH = Join-Path $root "backend"
$url = "http://127.0.0.1:$Port"
Write-Host "`nServing dashboard and API on $url  (Ctrl+C to stop)`n" -ForegroundColor Green
Start-Job { Start-Sleep 3; Start-Process $using:url } | Out-Null

& $py -m uvicorn healing_agent.app:app --host 127.0.0.1 --port $Port
