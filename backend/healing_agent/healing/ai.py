"""Tier 2 repairs: model-generated fixes, gated by verification.

The model is never trusted to be right. Every candidate rewrite must parse, and
must strictly reduce that file's problem count without introducing a new
critical defect, or it is discarded and the original file is restored. A repair
agent that can make things worse is worse than no repair agent.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import Fix, FixTier, Problem, Severity
from ..redaction import scrub
from .prompts import RESPONSE_SCHEMA, SYSTEM_PROMPT  # noqa: F401 - re-exported
from .providers import ProviderError, RepairProvider, build_providers

MAX_FILE_CHARS = 60_000

@dataclass
class AiRepairOutcome:
    fixes: list[Fix]
    attempted: int = 0
    accepted: int = 0
    rejected: int = 0
    skipped_reason: str | None = None
    notes: list[str] = None  # type: ignore[assignment]
    models_used: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []
        if self.models_used is None:
            self.models_used = {}


# ------------------------------------------------------------ verification --


def file_parses(path: Path) -> tuple[bool, str]:
    """Check that a file is syntactically valid for its type."""
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"unreadable: {exc}"

    if suffix in {".py", ".pyi"}:
        try:
            ast.parse(text)
        except SyntaxError as exc:
            return False, f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})"
        return True, "parses"

    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return False, f"invalid JSON: {exc.msg} (line {exc.lineno})"
        return True, "parses"

    if suffix in {".yml", ".yaml"}:
        try:
            import yaml

            yaml.safe_load(text)
        except ImportError:
            return True, "yaml module unavailable; skipped"
        except Exception as exc:
            return False, f"invalid YAML: {exc}"
        return True, "parses"

    if suffix in {".js", ".mjs", ".cjs"} and shutil.which("node"):
        try:
            result = subprocess.run(
                ["node", "--check", str(path)],
                capture_output=True, text=True, timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError):
            return True, "node unavailable; skipped"
        if result.returncode != 0:
            return False, f"JS syntax error: {result.stderr.strip()[:200]}"
        return True, "parses"

    return True, "no parser for this file type"


def count_file_problems(root: Path, rel: str) -> tuple[int, int]:
    """Re-scan one file. Returns (total problems, critical problems)."""
    from ..analysis import scan_repository
    from ..analysis.scanning import RepoInventory

    path = root / rel
    inventory = RepoInventory(root=root, files=[path], languages={})
    result = scan_repository(root, inventory=inventory)
    relevant = [p for p in result.problems if p.file == rel]
    critical = sum(1 for p in relevant if p.severity is Severity.CRITICAL)
    return len(relevant), critical


def public_symbols(source: str) -> set[str]:
    """Names of the functions and classes a Python source file defines.

    Used to prove a rewrite did not "fix" a defect by deleting the code that
    contained it.
    """
    symbols: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return symbols
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
    return symbols


def docstring_owners(source: str) -> set[str]:
    """Which scopes of a Python file carry a docstring: '<module>', 'Class',
    'Class.method', 'function'.

    A rewrite that drops one has deleted documentation it was never asked to
    touch. Models do this quietly (the module docstring on line 1 is the usual
    casualty), and the problem count cannot see it.
    """
    owners: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return owners
    if ast.get_docstring(tree):
        owners.add("<module>")

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if ast.get_docstring(child):
                    owners.add(name)
                walk(child, f"{name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return owners


def rewrite_destroys_code(source: str, candidate: str, suffix: str) -> str | None:
    """Return a rejection reason if a candidate rewrite removes real code.

    The problem count alone is a corruptible objective: deleting the offending
    function drives it to zero. These checks make deletion an automatic
    rejection instead of a winning strategy.
    """
    source_lines = [line for line in source.splitlines() if line.strip()]
    candidate_lines = [line for line in candidate.splitlines() if line.strip()]

    if len(source_lines) > 5 and len(candidate_lines) < len(source_lines) * 0.6:
        return (
            f"rewrite dropped {len(source_lines) - len(candidate_lines)} of "
            f"{len(source_lines)} non-blank lines"
        )

    if suffix in {".py", ".pyi"}:
        lost = public_symbols(source) - public_symbols(candidate)
        if lost:
            return (
                "rewrite removed definitions that existed before: "
                + ", ".join(sorted(lost)[:5])
            )
        lost_docs = docstring_owners(source) - docstring_owners(candidate)
        if lost_docs:
            return "rewrite deleted docstring(s) it was not asked to touch: " + ", ".join(
                sorted(lost_docs)[:5]
            )

    return None


# ----------------------------------------------------------------- client ---


# -------------------------------------------------------------- entrypoint --


def _request_with_fallback(
    providers: list[RepairProvider],
    dead: dict[str, str],
    outcome: AiRepairOutcome,
    rel: str,
    source: str,
    problems: list[Problem],
    memory_context: str,
) -> tuple[dict[str, Any] | None, RepairProvider | None, list[str]]:
    """Ask each live provider in turn until one answers.

    A provider that fails fatally (no credit, bad key, quota) is retired for the
    rest of the run, so one dead account costs one failed call, not one per file.
    Returns (payload, provider that answered, error messages).
    """
    errors: list[str] = []
    for provider in providers:
        if provider.name in dead:
            continue
        try:
            payload = provider.request_repair(rel, source, problems, memory_context)
            return payload, provider, errors
        except ProviderError as exc:
            errors.append(f"{provider.name}: {exc}")
            if exc.fatal:
                dead[provider.name] = str(exc)
                outcome.notes.append(
                    f"{provider.name} ({provider.model}) unavailable, not used for "
                    f"the rest of this run: {scrub(str(exc))[:160]}"
                )
        except Exception as exc:  # noqa: BLE001 - never let one provider end the run
            errors.append(f"{provider.name}: {scrub(str(exc))[:160]}")
    return None, None, errors


def apply_ai_fixes(
    root: Path,
    problems: list[Problem],
    settings: Settings,
    round_index: int = 1,
    max_files: int | None = None,
    on_progress=None,
    memory_context: str = "",
) -> AiRepairOutcome:
    """Repair remaining defects with the model, verifying every candidate."""
    outcome = AiRepairOutcome(fixes=[])

    if not problems:
        outcome.skipped_reason = "No problems remained for the AI tier."
        return outcome

    providers = build_providers(settings)
    if not providers:
        outcome.skipped_reason = (
            "GEMINI_API_KEY is not set - AI repair tier disabled. "
            "Deterministic fixes were still applied."
        )
        return outcome
    dead: dict[str, str] = {}

    by_file: dict[str, list[Problem]] = {}
    for problem in problems:
        by_file.setdefault(problem.file, []).append(problem)

    # Repair the most damaged files first: a file with a syntax error blocks
    # every other check on it, so it buys the most per call.
    ordered = sorted(
        by_file.items(),
        key=lambda kv: -sum(p.severity.weight for p in kv[1]),
    )
    limit = max_files if max_files is not None else settings.max_ai_files

    for rel, file_problems in ordered[:limit]:
        path = root / rel
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(source) > MAX_FILE_CHARS:
            outcome.notes.append(f"{rel}: skipped, file exceeds size limit")
            continue

        outcome.attempted += 1
        if on_progress:
            on_progress(rel, len(file_problems))

        before_total, before_critical = count_file_problems(root, rel)
        backup = source

        payload, used, errors = _request_with_fallback(
            providers, dead, outcome, rel, source, file_problems, memory_context
        )
        if used is None:
            outcome.rejected += 1
            outcome.notes.append(
                f"{rel}: model call failed - " + ("; ".join(errors) or "no provider")
            )
            if len(dead) == len(providers):
                outcome.notes.append(
                    "Every AI provider is unavailable; remaining files were left "
                    "for the next run."
                )
                break
            continue

        if not payload or payload.get("unable_to_fix"):
            outcome.rejected += 1
            reason = (payload or {}).get("reason", "no reason given")
            outcome.notes.append(f"{rel}: model declined - {scrub(reason)[:200]}")
            continue

        candidate = payload.get("fixed_content") or ""
        if not candidate.strip() or candidate == source:
            outcome.rejected += 1
            outcome.notes.append(f"{rel}: model returned no usable change")
            continue

        # Guard against a rewrite that deletes code rather than repairing it.
        destruction = rewrite_destroys_code(source, candidate, path.suffix.lower())
        if destruction:
            outcome.rejected += 1
            outcome.notes.append(f"{rel}: rejected, {destruction}")
            continue

        path.write_text(candidate, encoding="utf-8")

        parses, parse_detail = file_parses(path)
        if not parses:
            path.write_text(backup, encoding="utf-8")
            outcome.rejected += 1
            outcome.notes.append(f"{rel}: rejected, {parse_detail}")
            continue

        after_total, after_critical = count_file_problems(root, rel)
        if after_total >= before_total or after_critical > before_critical:
            path.write_text(backup, encoding="utf-8")
            outcome.rejected += 1
            outcome.notes.append(
                f"{rel}: rejected, problems did not improve "
                f"({before_total} -> {after_total}, "
                f"critical {before_critical} -> {after_critical})"
            )
            continue

        outcome.accepted += 1
        outcome.models_used[used.model] = outcome.models_used.get(used.model, 0) + 1
        changes = payload.get("changes") or ["Repaired reported defects."]
        outcome.fixes.append(
            Fix(
                file=rel,
                tier=FixTier.AI,
                description=(
                    "; ".join(str(c) for c in changes)[:500]
                    + f" [verified: {before_total} -> {after_total} problems, "
                    f"via {used.model}]"
                ),
                problems_addressed=[p.key for p in file_problems],
                lines_changed=abs(
                    len(candidate.splitlines()) - len(source.splitlines())
                )
                or len(file_problems),
                round_index=round_index,
            )
        )

    return outcome
