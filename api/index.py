"""Vercel serverless entrypoint.

Vercel's Python runtime serves an exported ASGI ``app`` directly, so this
module only has to make the backend package importable and re-export it.

Note on runtime differences: Vercel functions ship no ``git`` binary and only
``/tmp`` is writable. Both are handled -- the repository layer falls back to
the pure-HTTP GitHub backend when git is absent, and the workspace resolver
falls back to ``/tmp`` when the configured directory is read-only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Serverless filesystems are read-only apart from /tmp.
os.environ.setdefault("HEALING_AGENT_WORKSPACE", "/tmp/healing-agent-workspaces")

from healing_agent.app import app  # noqa: E402

__all__ = ["app"]
