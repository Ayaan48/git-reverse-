#!/usr/bin/env bash
# Start the Autonomous CI/CD Healing Agent (macOS / Linux).
#
#   ./scripts/start.sh [port]
#
# Builds the dashboard once, then runs a single process serving both the UI
# and the API on one port.
set -euo pipefail

PORT="${1:-8000}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

command -v python3 >/dev/null || { echo "python3 not found (need 3.11+)"; exit 1; }
echo "Using $(python3 --version)"

echo "Installing Python dependencies..."
python3 -m pip install --quiet --disable-pip-version-check -r backend/requirements.txt

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  command -v npm >/dev/null || { echo "npm not found (need Node 18+)"; exit 1; }
  echo "Building dashboard..."
  ( cd frontend && [ -d node_modules ] || npm install --no-audit --no-fund; npm run build )
  [ -f frontend/dist/index.html ] || { echo "Frontend build produced no output."; exit 1; }
fi

echo
echo "Serving dashboard and API on http://127.0.0.1:${PORT}  (Ctrl+C to stop)"
echo
PYTHONPATH="$ROOT/backend" exec python3 -m uvicorn healing_agent.app:app \
  --host 127.0.0.1 --port "$PORT"
