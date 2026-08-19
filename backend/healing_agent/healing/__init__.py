"""Two-tier repair: deterministic rules first, then the model."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..models import Fix, Problem
from .ai import AiRepairOutcome, apply_ai_fixes, file_parses, public_symbols
from .deterministic import apply_deterministic_fixes, run_ruff_autofix

__all__ = [
    "AiRepairOutcome",
    "HealOutcome",
    "apply_ai_fixes",
    "apply_deterministic_fixes",
    "file_parses",
    "heal",
    "public_symbols",
    "run_ruff_autofix",
]


@dataclass
class HealOutcome:
    fixes: list[Fix] = field(default_factory=list)
    deterministic_count: int = 0
    ai_attempted: int = 0
    ai_accepted: int = 0
    ai_rejected: int = 0
    notes: list[str] = field(default_factory=list)
    ai_skipped_reason: str | None = None


def heal(
    root: Path,
    problems: list[Problem],
    settings: Settings,
    round_index: int = 1,
    use_ai: bool = True,
    on_progress=None,
) -> HealOutcome:
    """Repair `problems` in the checkout at `root`.

    Tier 1 runs first and unconditionally: it is fast, free, and its results
    shrink the surface the model has to reason about. Tier 2 then handles what
    is left -- the defects that actually need understanding.
    """
    outcome = HealOutcome()

    deterministic = apply_deterministic_fixes(root, problems, round_index)
    outcome.fixes.extend(deterministic)
    outcome.deterministic_count = len(deterministic)

    if not use_ai:
        outcome.ai_skipped_reason = "AI tier disabled for this run."
        return outcome

    # Re-scan so the model only sees what tier 1 could not resolve.
    from ..analysis import scan_repository

    remaining = scan_repository(root, max_files=settings.max_repo_files).problems
    if not remaining:
        outcome.ai_skipped_reason = "Deterministic fixes resolved every problem."
        return outcome

    ai_outcome = apply_ai_fixes(
        root,
        remaining,
        settings,
        round_index=round_index,
        on_progress=on_progress,
    )
    outcome.fixes.extend(ai_outcome.fixes)
    outcome.ai_attempted = ai_outcome.attempted
    outcome.ai_accepted = ai_outcome.accepted
    outcome.ai_rejected = ai_outcome.rejected
    outcome.notes.extend(ai_outcome.notes)
    outcome.ai_skipped_reason = ai_outcome.skipped_reason
    return outcome
