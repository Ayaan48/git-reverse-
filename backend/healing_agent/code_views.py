"""Before-and-after code views for the dashboard's Problems panel.

The panel shows a repository the way a GitHub pull request does:

* excerpts: the original lines around every problem, captured before the
  agent changes anything, so a problem line can be shown in red in context;
* diffs: for every file the agent changed, hunks of removed (red) and added
  (green) lines with surrounding context;
* problem status: whether each original problem is gone after healing. This
  comes from re-scanning the healed code and following each problem's line
  through the diff, not from what a fix claims it addressed.

Everything is bounded: the job snapshot is streamed to the browser, so a large
repository must not turn it into megabytes.
"""

from __future__ import annotations

import difflib
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import Problem

EXCERPT_RADIUS = 2
MAX_EXCERPT_FILES = 40
MAX_EXCERPT_LINES_PER_FILE = 120
MAX_LINE_CHARS = 300
DIFF_CONTEXT = 3
MAX_DIFF_FILES = 40
MAX_DIFF_LINES_PER_FILE = 500
MAX_DIFF_LINES_TOTAL = 4000
MAX_FILE_BYTES = 2_000_000


# ------------------------------------------------------------------ reading --


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return None


def _split_keep(text: str | None) -> list[str]:
    """Lines with their newline kept, so a missing final newline is a change.

    Splits on newline only: str.splitlines() would also split on form feeds
    and other separators, shifting line numbers away from what linters report.
    """
    if not text:
        return []
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _clip(line: str) -> str:
    line = line.rstrip("\n")
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"


def workflow_paths(root: Path) -> list[str]:
    """Repo-relative workflow files: remediation may rewrite these."""
    folder = root / ".github" / "workflows"
    if not folder.is_dir():
        return []
    return sorted(
        str(path.relative_to(root)).replace("\\", "/") for path in folder.glob("*.y*ml")
    )


def snapshot_originals(root: Path, paths: Iterable[str]) -> dict[str, str]:
    """Copy the files the agent may change, before it changes any of them."""
    originals: dict[str, str] = {}
    for rel in sorted(set(paths)):
        text = _read_text(root / rel)
        if text is not None:
            originals[rel] = text
    return originals


def _original_from_git(root: Path, rel: str) -> str | None:
    """Fallback for a changed file nobody snapshotted: ask git for HEAD's copy."""
    if not (root / ".git").exists() or not shutil.which("git"):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{rel}"],
            capture_output=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or len(result.stdout) > MAX_FILE_BYTES:
        return None
    return result.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")


# ----------------------------------------------------------------- excerpts --


def capture_excerpts(
    root: Path, problems: list[Problem], radius: int = EXCERPT_RADIUS
) -> list[dict[str, Any]]:
    """The original lines around each problem, merged into ranges per file.

    Files are ordered worst first (by the severity weight of their problems).
    """
    lines_by_file: dict[str, set[int]] = {}
    weight: dict[str, int] = {}
    for problem in problems:
        if problem.line and problem.line > 0:
            lines_by_file.setdefault(problem.file, set()).add(problem.line)
            weight[problem.file] = weight.get(problem.file, 0) + problem.severity.weight

    files = sorted(lines_by_file, key=lambda rel: (-weight[rel], rel))[:MAX_EXCERPT_FILES]
    excerpts: list[dict[str, Any]] = []
    for rel in files:
        lines = [_clip(line) for line in _split_keep(_read_text(root / rel))]
        if not lines:
            continue
        windows = sorted(
            (max(1, min(n, len(lines)) - radius), min(len(lines), n + radius))
            for n in lines_by_file[rel]
        )
        merged: list[list[int]] = []
        for start, end in windows:
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])

        ranges: list[dict[str, Any]] = []
        budget = MAX_EXCERPT_LINES_PER_FILE
        for start, end in merged:
            if budget <= 0:
                break
            end = min(end, start + budget - 1)
            ranges.append({"start": start, "lines": lines[start - 1:end]})
            budget -= end - start + 1
        excerpts.append({"file": rel, "ranges": ranges})
    return excerpts


# -------------------------------------------------------------------- diffs --


def build_file_diff(
    rel: str,
    old: str | None,
    new: str,
    context: int = DIFF_CONTEXT,
    max_rows: int = MAX_DIFF_LINES_PER_FILE,
) -> dict[str, Any]:
    """GitHub-style hunks: context, removed ("del") and added ("add") rows."""
    a, b = _split_keep(old), _split_keep(new)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)

    additions = deletions = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            deletions += i2 - i1
        if tag in ("replace", "insert"):
            additions += j2 - j1

    groups = list(matcher.get_grouped_opcodes(context))
    hunks: list[dict[str, Any]] = []
    emitted = 0
    truncated = False
    for index, group in enumerate(groups):
        rows: list[dict[str, Any]] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows.extend(
                    {"type": "context", "old": i1 + k + 1, "new": j1 + k + 1,
                     "text": _clip(a[i1 + k])}
                    for k in range(i2 - i1)
                )
                continue
            for k in range(i1, i2):
                rows.append({"type": "del", "old": k + 1, "new": None, "text": _clip(a[k])})
                if not a[k].endswith("\n"):
                    rows.append({"type": "note", "text": "No newline at end of file"})
            for k in range(j1, j2):
                rows.append({"type": "add", "old": None, "new": k + 1, "text": _clip(b[k])})
                if not b[k].endswith("\n"):
                    rows.append({"type": "note", "text": "No newline at end of file"})

        if emitted + len(rows) > max_rows:
            rows = rows[: max(0, max_rows - emitted)]
            truncated = True
        first, last = group[0], group[-1]
        hunks.append({
            "old_start": first[1] + 1, "old_count": last[2] - first[1],
            "new_start": first[3] + 1, "new_count": last[4] - first[3],
            "rows": rows,
        })
        emitted += len(rows)
        if truncated or emitted >= max_rows:
            truncated = truncated or index < len(groups) - 1
            break

    return {
        "file": rel,
        "additions": additions,
        "deletions": deletions,
        "hunks": hunks,
        "truncated": truncated,
    }


# ----------------------------------------------------------- problem status --


def _old_to_new(old: str | None, new: str | None) -> dict[int, tuple[int, int] | None]:
    """Map each old line number to the new lines it became (None = deleted)."""
    a, b = _split_keep(old), _split_keep(new)
    mapping: dict[int, tuple[int, int] | None] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        for i in range(i1, i2):
            if tag == "equal":
                line = j1 + (i - i1) + 1
                mapping[i + 1] = (line, line)
            elif tag == "replace":
                mapping[i + 1] = (j1 + 1, j2)
            elif tag == "delete":
                mapping[i + 1] = None
    return mapping


def problem_status(
    before: list[Problem],
    after: list[Problem],
    old_texts: dict[str, str],
    new_texts: dict[str, str],
    changed: set[str],
) -> dict[str, str]:
    """For each original problem: "resolved" or "remaining".

    A problem remains when the re-scan finds the same problem code in the
    same file on the line the original line became.
    """
    remaining_lines: dict[tuple[str, str], list[int]] = {}
    for problem in after:
        remaining_lines.setdefault((problem.file, problem.code), []).append(problem.line)

    maps: dict[str, dict[int, tuple[int, int] | None] | None] = {}
    status: dict[str, str] = {}
    for problem in before:
        candidates = remaining_lines.get((problem.file, problem.code), [])
        if not candidates:
            status[problem.key] = "resolved"
            continue
        if problem.file not in changed:
            target: tuple[int, int] | None = (problem.line, problem.line)
        else:
            if problem.file not in maps:
                old, new = old_texts.get(problem.file), new_texts.get(problem.file)
                maps[problem.file] = (
                    _old_to_new(old, new) if old is not None and new is not None else None
                )
            mapping = maps[problem.file]
            if mapping is None or problem.line not in mapping:
                # Original unknown, or a line past the end: judge by the file.
                status[problem.key] = "remaining"
                continue
            target = mapping[problem.line]
        if target is None:
            status[problem.key] = "resolved"  # the line itself was removed
            continue
        start, end = target
        status[problem.key] = (
            "remaining" if any(start <= line <= end for line in candidates) else "resolved"
        )
    return status


# -------------------------------------------------------------------- entry --


def build_code_views(
    root: Path,
    changed: list[str],
    originals: dict[str, str],
    before: list[Problem],
    after: list[Problem],
) -> dict[str, Any]:
    """Diffs for the changed files, and a status for every original problem."""
    old_texts: dict[str, str] = {}
    new_texts: dict[str, str] = {}
    diffs: list[dict[str, Any]] = []
    total = 0
    for rel in changed:
        new = _read_text(root / rel)
        if new is None:
            continue
        old = originals.get(rel)
        if old is None:
            old = _original_from_git(root, rel)
        new_texts[rel] = new
        if old is not None:
            old_texts[rel] = old
        if len(diffs) >= MAX_DIFF_FILES or total >= MAX_DIFF_LINES_TOTAL:
            continue
        diff = build_file_diff(
            rel, old, new,
            max_rows=min(MAX_DIFF_LINES_PER_FILE, MAX_DIFF_LINES_TOTAL - total),
        )
        diff["original_known"] = old is not None
        if diff["hunks"]:
            total += sum(len(hunk["rows"]) for hunk in diff["hunks"])
            diffs.append(diff)

    return {
        "diffs": diffs,
        "problem_status": problem_status(before, after, old_texts, new_texts, set(changed)),
    }
