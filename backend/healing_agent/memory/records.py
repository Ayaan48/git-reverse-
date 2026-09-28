"""Translate a run into what memory needs: a recall query and a retain record."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from ..cicd.telemetry import PipelineTelemetry
from ..models import (
    Diagnosis,
    Fix,
    Problem,
    ProblemKind,
    RemediationStep,
    ValidationRun,
)

_MODULE_RE = re.compile(r"['\"`]([A-Za-z_][\w.\-]*)['\"`]")
_LOG_MODULE_RE = re.compile(
    r"(?:No module named|Cannot find module|could not find a version that "
    r"satisfies the requirement|ResolutionImpossible.*?)\s*['\"]?([A-Za-z_][\w.\-]*)",
    re.IGNORECASE,
)

MAX_SIGNATURES = 12


def _ranked(problems: list[Problem]) -> list[Problem]:
    return sorted(problems, key=lambda p: -p.severity.weight)


def involved_packages(problems: list[Problem], log_text: str = "") -> list[str]:
    """Module/package names named by import failures, in the repo or the logs."""
    names: dict[str, None] = {}
    for problem in problems:
        if problem.kind is ProblemKind.IMPORT:
            match = _MODULE_RE.search(problem.message)
            if match:
                names.setdefault(match.group(1), None)
    for match in _LOG_MODULE_RE.finditer(log_text or ""):
        names.setdefault(match.group(1), None)
    return list(names)[:10]


def error_signatures(
    problems: list[Problem], telemetry: PipelineTelemetry | None
) -> list[str]:
    """The lines that identify this failure: CI errors first, then defects."""
    signatures: list[str] = []
    if telemetry is not None:
        for failure in telemetry.job_failures[:4]:
            step = f" at step '{failure.step_name}'" if failure.step_name else ""
            signatures.append(
                f"{failure.workflow_name}/{failure.job_name}{step}: {failure.conclusion}"
            )
            excerpt = [
                line.strip()
                for line in (failure.log_excerpt or "").splitlines()
                if re.search(r"error|fail|exception|denied|limit|timeout", line, re.I)
            ]
            signatures.extend(excerpt[:3])
    for problem in _ranked(problems):
        if len(signatures) >= MAX_SIGNATURES:
            break
        signatures.append(
            f"{problem.code} ({problem.kind.value}, {problem.severity.value}): "
            f"{problem.message} [{problem.file}:{problem.line}]"
        )
    return [s[:300] for s in signatures[:MAX_SIGNATURES]]


def build_recall_query(
    problems: list[Problem], telemetry: PipelineTelemetry, log_text: str
) -> str:
    """What to ask memory before diagnosis: error text, stack traces, packages."""
    parts: list[str] = []
    signatures = error_signatures(problems, telemetry)
    if signatures:
        parts.append("CI failure signatures:\n" + "\n".join(signatures))
    if log_text:
        parts.append("Log excerpt:\n" + log_text[:1200])
    packages = involved_packages(problems, log_text)
    if packages:
        parts.append("Packages involved: " + ", ".join(packages))
    kinds = sorted({p.kind.value for p in problems})
    if kinds:
        parts.append("Defect categories: " + ", ".join(kinds))
    return "\n\n".join(parts)


def build_incident_record(
    *,
    incident_id: str,
    repo: str,
    outcome: str,
    diagnosis: Diagnosis | None,
    problems: list[Problem],
    fixes: list[Fix],
    validations: list[ValidationRun],
    remediations: list[RemediationStep],
    telemetry: PipelineTelemetry | None,
    log_text: str = "",
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Structured post-incident data handed to HindsightService.retain()."""
    passed_rounds = {run.round_index for run in validations if run.passed}
    categories = list(diagnosis.signals.get("categories", [])) if diagnosis else []
    return {
        "incident_id": incident_id,
        "repo": repo,
        "timestamp": (timestamp or datetime.now(UTC)).isoformat(),
        "failure_class": diagnosis.failure_class.value if diagnosis else "unknown",
        "confidence": diagnosis.confidence if diagnosis else None,
        "recommended_action": diagnosis.recommended_action.value if diagnosis else None,
        "summary": diagnosis.summary if diagnosis else "",
        "evidence": list(diagnosis.evidence) if diagnosis else [],
        "failure_types": sorted({p.kind.value for p in problems} | set(categories)),
        "error_signatures": error_signatures(problems, telemetry),
        "packages": involved_packages(problems, log_text),
        "repairs": [
            {
                "file": fix.file,
                "tier": fix.tier.value,
                "description": fix.description,
                "round": fix.round_index,
                "passed_validation": fix.round_index in passed_rounds,
            }
            for fix in fixes
        ],
        "validation": [
            {
                "round": run.round_index,
                "passed": run.passed,
                "failed_stages": [
                    s.name for s in run.stages if not s.passed and not s.skipped
                ],
            }
            for run in validations
        ],
        "remediations": [
            f"{step.action.value} - {step.description}" for step in remediations
        ],
        "outcome": outcome,
    }
