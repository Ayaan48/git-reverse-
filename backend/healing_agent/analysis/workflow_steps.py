"""Defects inside the shell commands of GitHub Actions job steps.

The workflow checks elsewhere validate a file's *shape* -- that it has a
trigger, jobs, and a runner. Nothing looked at what the steps actually run, so
a job could be structurally perfect and still fail on its first command.

This module reads the `run:` blocks of every job step and inspects the pip
lines, where a large share of real CI breakage lives: installing from a
requirements file that is not in the repository, or exact pins that have no
wheel for whatever Python the runner resolved.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from ..models import Problem, ProblemKind, Severity
from .scanning import RepoInventory

try:
    import yaml

    _YAML = True
except ImportError:  # pragma: no cover - optional dependency
    _YAML = False

# A pip invocation, however it is spelled.
_PIP_LINE = re.compile(
    r"(?P<sudo>sudo\s+)?"
    r"(?P<runner>(?:python[0-9.]*\s+-m\s+pip)|(?:pip[0-9.]*))"
    r"\s+(?P<rest>install\b.*)",
)
# Floating interpreter versions: the runner may resolve these to a release
# newer than the pins were ever tested against.
_FLOATING_PYTHON = {"3", "3.x", "3.X", "*", "", "x"}


def _requirement_files(rest: str) -> list[str]:
    """Requirement files named by -r/--requirement in a pip command."""
    try:
        tokens = shlex.split(rest, comments=True)
    except ValueError:
        tokens = rest.split()
    found: list[str] = []
    for index, token in enumerate(tokens):
        if token in {"-r", "--requirement"} and index + 1 < len(tokens):
            found.append(tokens[index + 1])
        elif token.startswith("--requirement="):
            found.append(token.split("=", 1)[1])
        elif token.startswith("-r") and len(token) > 2:
            found.append(token[2:])
    return found


def _line_of(text: str, needle: str, used: set[int]) -> int:
    """1-based line of `needle`, skipping lines already reported.

    A `run:` block is re-found in the raw file because PyYAML discards source
    positions, and the same command often appears in several jobs.
    """
    for number, line in enumerate(text.splitlines(), start=1):
        if number in used:
            continue
        if needle in line:
            used.add(number)
            return number
    return 1


def _iter_steps(document: dict):
    """Yield (job_name, step) for every step in a parsed workflow."""
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict):
                yield str(job_name), step


def _python_versions(document: dict) -> dict[str, str]:
    """Interpreter version each job pins via actions/setup-python."""
    versions: dict[str, str] = {}
    for job_name, step in _iter_steps(document):
        uses = str(step.get("uses") or "")
        if not uses.startswith("actions/setup-python"):
            continue
        with_block = step.get("with")
        requested = ""
        if isinstance(with_block, dict):
            requested = str(with_block.get("python-version", "")).strip()
        versions[job_name] = requested
    return versions


def _repo_has(root: Path, candidate: str) -> bool:
    """Whether a requirements path named in a workflow exists in the repo.

    Paths are workflow-relative, which is the repository root -- a step's
    `working-directory` can move it, so an unresolved path is only reported
    when no file of that name exists anywhere in the tree.
    """
    cleaned = candidate.strip().strip("'\"")
    if not cleaned or cleaned.startswith(("$", "{", "-")):
        return True  # expression or flag; not a path we can resolve
    if (root / cleaned).is_file():
        return True
    name = Path(cleaned).name
    return any(root.rglob(name))


def detect_pip_problems(inventory: RepoInventory) -> list[Problem]:
    """Inspect pip commands in workflow job steps."""
    if not _YAML:
        return []

    problems: list[Problem] = []
    root = inventory.root

    for path in inventory.by_suffix(".yml", ".yaml"):
        rel = inventory.relative(path)
        if ".github/workflows/" not in rel.replace("\\", "/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            document = yaml.safe_load(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue  # the YAML detector already reports unparseable files
        if not isinstance(document, dict):
            continue

        versions = _python_versions(document)
        used_lines: set[int] = set()

        for job_name, step in _iter_steps(document):
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for raw in run.splitlines():
                command = raw.strip()
                match = _PIP_LINE.search(command)
                if not match:
                    continue
                line = _line_of(text, command[:60], used_lines)
                problems.extend(
                    _inspect(rel, line, job_name, command, match, root, versions)
                )
    return problems


def _inspect(
    rel: str,
    line: int,
    job_name: str,
    command: str,
    match: re.Match[str],
    root: Path,
    versions: dict[str, str],
) -> list[Problem]:
    """Check one pip command."""
    found: list[Problem] = []
    rest = match.group("rest")

    def add(code, severity, message, auto_fixable=False):
        found.append(
            Problem(
                file=rel, line=line, column=1, kind=ProblemKind.CONFIG,
                severity=severity, code=code, message=message,
                detector="workflow-pip", snippet=command[:200],
                auto_fixable=auto_fixable,
            )
        )

    # A requirements file that is not in the repository fails the job on its
    # first command -- the highest-confidence finding available here.
    for requirement in _requirement_files(rest):
        if not _repo_has(root, requirement):
            add(
                "PIP-MISSING-REQUIREMENTS",
                Severity.CRITICAL,
                f"Job '{job_name}' installs from '{requirement}', which does "
                f"not exist in this repository. The step will fail with "
                f"'Could not open requirements file'.",
            )

    if match.group("sudo"):
        add(
            "PIP-SUDO",
            Severity.MEDIUM,
            f"Job '{job_name}' runs pip under sudo, which installs into the "
            f"system interpreter rather than the one actions/setup-python "
            f"selected, so later steps may not see the packages.",
        )

    # `pip` resolves through PATH and can belong to a different interpreter
    # than the job selected; `python -m pip` cannot.
    if match.group("runner").startswith("pip"):
        add(
            "PIP-BARE-EXECUTABLE",
            Severity.LOW,
            f"Job '{job_name}' calls '{match.group('runner')}' directly. "
            f"'python -m pip' installs into the interpreter the job actually "
            f"uses, which is not guaranteed to be the one on PATH.",
            auto_fixable=True,
        )

    # Exact pins against a floating interpreter is the wheel-availability
    # trap: pip falls back to building from source on a Python the pinned
    # release has no wheel for, and the job dies in a compiler it has no
    # reason to need.
    pins = re.findall(r"[A-Za-z0-9._-]+==[0-9][^\s'\";]*", rest)
    requested = versions.get(job_name)
    if pins and requested is not None and requested.strip() in _FLOATING_PYTHON:
        shown = ", ".join(pins[:3]) + ("…" if len(pins) > 3 else "")
        add(
            "PIP-PINNED-FLOATING-PYTHON",
            Severity.MEDIUM,
            f"Job '{job_name}' pins exact versions ({shown}) but requests a "
            f"floating Python ('{requested or 'unset'}'). When the runner "
            f"moves to a newer release the pinned builds may have no wheel "
            f"for it, and pip falls back to compiling from source.",
        )

    return found
