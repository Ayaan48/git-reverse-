"""The pull request the agent opens for its healing branch.

The body is written for the reviewer who has to decide whether to merge: what
broke, whose fault it was, what changed, and how the change was checked. The
agent opens the pull request but never merges it.
"""

from __future__ import annotations

from typing import Any

from ..models import Diagnosis, Fix, ValidationRun
from .incident import VERDICT_HEADLINE

MAX_LISTED_FIXES = 25


def build_pull_request(
    *,
    diagnosis: Diagnosis | None,
    problems_found: int,
    fixes: list[Fix],
    validations: list[ValidationRun],
    memory: dict[str, Any] | None,
    job_id: str,
    trigger: str | None = None,
) -> tuple[str, str]:
    """Return (title, body) for the healing pull request."""
    count = len(fixes)
    title = (
        f"fix: {count} automated repair{'s' if count != 1 else ''} "
        f"from the CI/CD healing agent"
    )

    lines: list[str] = []
    add = lines.append
    add("## What the healing agent did")
    add("")
    if trigger:
        add(f"**Triggered by:** {trigger}  ")
    if diagnosis:
        add(
            f"**Diagnosis:** {VERDICT_HEADLINE[diagnosis.failure_class]} "
            f"({diagnosis.confidence:.0%} confidence), action "
            f"`{diagnosis.recommended_action.value}`  "
        )
    rule_based = sum(1 for f in fixes if f.tier.value == "deterministic")
    ai = count - rule_based
    verified = ", each verified before it was kept" if ai else ""
    add(
        f"**Repairs:** {problems_found} problem(s) found, {count} fix(es) applied "
        f"({rule_based} rule-based, {ai} AI{verified})  "
    )
    final = validations[-1] if validations else None
    if final:
        gates = ", ".join(
            f"{stage.name} {'skipped' if stage.skipped else ('passed' if stage.passed else 'FAILED')}"
            for stage in final.stages
        )
        add(f"**Validation:** {'passed' if final.passed else 'failed'} ({gates})  ")
    if memory and memory.get("enabled"):
        found = memory.get("incidents_found", 0)
        influenced = len(memory.get("influenced_incidents") or [])
        add(
            f"**Memory:** recalled {found} similar past incident(s); "
            f"{influenced} influenced the diagnosis  "
        )
    add("")

    if fixes:
        add("### Changes")
        add("")
        for fix in fixes[:MAX_LISTED_FIXES]:
            add(f"- `{fix.file}` ({fix.tier.value}): {fix.description}")
        if count > MAX_LISTED_FIXES:
            add(f"- ...and {count - MAX_LISTED_FIXES} more")
        add("")

    if memory and memory.get("risk_notes"):
        add("### Pre-merge risk notes from past incidents")
        add("")
        add(str(memory["risk_notes"]).strip())
        add("")

    add("---")
    add(
        f"Opened automatically by the Autonomous CI/CD Healing Agent (job `{job_id}`). "
        f"Review the diff before merging; the agent never merges its own changes."
    )
    return title, "\n".join(lines)
