#!/usr/bin/env python3
"""Create (or update) the Hindsight memory bank from bank-template.json.

    python scripts/setup_memory_bank.py

Reads HINDSIGHT_API_URL / HINDSIGHT_API_KEY / HINDSIGHT_BANK_ID from the
environment or the repo's .env. Safe to re-run: mission and disposition are
updated in place and directives are only added when missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from healing_agent.config import load_env_file  # noqa: E402
from healing_agent.memory import HindsightService  # noqa: E402


def main() -> int:
    load_env_file(ROOT / ".env")
    service = HindsightService()
    if not service.enabled:
        print(f"Memory disabled: {service.disabled_reason}. Set it in .env first.")
        return 1
    result = service.ensure_bank()
    added = ", ".join(result["directives_added"]) or "none (already present)"
    print(f"Bank '{result['bank_id']}' ready at {service.api_url}")
    print(f"Directives added: {added}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
