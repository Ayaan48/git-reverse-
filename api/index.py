"""Vercel serverless entrypoint.

Vercel's Python runtime serves an exported ASGI ``app`` directly, so this
module only has to make the backend package importable and re-export it.

Two runtime differences from a normal host are handled here:

* Only the single function file lives in ``api/`` at invocation time -- the
  rest of the repository is bundled only if named by ``includeFiles`` in
  ``vercel.json``. That is why ``backend/**`` is listed there; without it this
  import fails and the platform reports an opaque FUNCTION_INVOCATION_FAILED.
* The filesystem is read-only apart from ``/tmp``.

If the import fails anyway, a small diagnostic app is served instead of
letting the function crash, so the cause is visible at /api/health rather than
buried in the platform's logs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Serverless filesystems are read-only apart from /tmp.
os.environ.setdefault("HEALING_AGENT_WORKSPACE", "/tmp/healing-agent-workspaces")

try:
    from healing_agent.app import app
except Exception as exc:  # noqa: BLE001 - must not crash the function
    import json
    import traceback

    _detail = {
        "error": "backend_import_failed",
        "message": str(exc)[:500],
        "type": type(exc).__name__,
        "hint": (
            "The backend package was not bundled with the function. Confirm "
            "vercel.json sets functions['api/index.py'].includeFiles to "
            "'backend/**', then redeploy."
        ),
        "diagnostics": {
            "backend_dir_present": BACKEND.is_dir(),
            "healing_agent_present": (BACKEND / "healing_agent").is_dir(),
            "root_contents": sorted(p.name for p in ROOT.iterdir())[:25]
            if ROOT.is_dir()
            else [],
            "python_version": sys.version.split()[0],
        },
        "traceback": traceback.format_exc().splitlines()[-6:],
    }

    async def app(scope, receive, send):  # type: ignore[misc]
        """Minimal ASGI app that reports why the real one could not load."""
        if scope["type"] != "http":
            return
        body = json.dumps(_detail, indent=2).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

__all__ = ["app"]
