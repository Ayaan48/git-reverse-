"""Close the loop: did the healing commit pass the repository's real CI?

The agent's own validation gates are a strong signal, but they are the agent
grading its own work. The repository's CI is the judge that actually matters,
so after pushing, the agent can wait for the GitHub Actions runs on its commit
and record their verdict -- in the report, and in memory, so later incidents
recall fixes that were proven in real CI rather than only in the sandbox.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..github import GitHubClient, GitHubError

# Conclusions that do not mean the change is broken.
GOOD_CONCLUSIONS = {"success", "skipped", "neutral"}

# How long to wait for GitHub to queue any run before concluding that the
# repository's workflows simply do not run for this branch.
START_GRACE_SECONDS = 90
POLL_SECONDS = 10


@dataclass
class CiVerification:
    """Outcome of watching real CI on one commit.

    `status` is one of: passed, failed, no_ci (no run started), timed_out
    (runs still going at the deadline), error (could not read Actions).
    """

    status: str
    detail: str
    runs: list[dict[str, Any]] = field(default_factory=list)
    waited_seconds: float = 0.0

    @property
    def passed(self) -> bool | None:
        if self.status == "passed":
            return True
        if self.status == "failed":
            return False
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "runs": self.runs,
            "waited_seconds": round(self.waited_seconds, 1),
        }


def _summarise(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": run.get("name") or "workflow",
            "event": run.get("event"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "url": run.get("html_url"),
        }
        for run in runs
    ]


def wait_for_ci(
    client: GitHubClient,
    owner: str,
    repo: str,
    sha: str,
    timeout: float = 300,
    start_grace: float = START_GRACE_SECONDS,
    poll: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_update: Callable[[list[dict[str, Any]]], None] | None = None,
) -> CiVerification:
    """Poll the Actions runs for `sha` until they finish or time runs out."""
    started = clock()
    summary: list[dict[str, Any]] = []

    while True:
        elapsed = clock() - started
        try:
            data = client.list_workflow_runs(owner, repo, per_page=50, head_sha=sha)
        except GitHubError as exc:
            return CiVerification(
                "error", f"Could not read GitHub Actions runs: {exc}", summary, elapsed
            )

        runs = [
            run for run in (data.get("workflow_runs") or [])
            if run.get("head_sha") in (None, sha)
        ]
        if runs:
            summary = _summarise(runs)
            if all(run.get("status") == "completed" for run in runs):
                failed = [
                    s for s in summary if (s["conclusion"] or "") not in GOOD_CONCLUSIONS
                ]
                if failed:
                    names = ", ".join(f"{s['name']} ({s['conclusion']})" for s in failed)
                    return CiVerification(
                        "failed", f"Real CI failed: {names}.", summary, elapsed
                    )
                return CiVerification(
                    "passed",
                    f"Real CI passed: {len(summary)} run(s) green on the healing commit.",
                    summary,
                    elapsed,
                )
            if on_update:
                on_update(summary)
        elif elapsed >= start_grace:
            return CiVerification(
                "no_ci",
                f"No GitHub Actions run started for the healing commit within "
                f"{start_grace:.0f}s. The repository's workflows may not run on "
                f"this branch (many only run on main or on pull requests).",
                summary,
                elapsed,
            )

        if elapsed >= timeout:
            return CiVerification(
                "timed_out",
                f"CI was still running after {timeout:.0f}s; check the run links.",
                summary,
                elapsed,
            )
        sleep(poll)
