"""Working on its own: pull requests, real-CI verification, live remediation,
and the GitHub Actions trigger that starts a run when CI fails."""

from pathlib import Path

import pytest
import yaml
from healing_agent import pipeline as pipeline_module
from healing_agent.cicd import build_pull_request, wait_for_ci
from healing_agent.github import GitHubError
from healing_agent.memory import HindsightService, MemoryRecall
from healing_agent.models import (
    AnalyzeRequest,
    Diagnosis,
    FailureClass,
    Fix,
    FixTier,
    HealingAction,
    JobStatus,
    StageResult,
    ValidationRun,
)
from test_pipeline_integration import _run

ROOT = Path(__file__).resolve().parent.parent
SHA = "b" * 40  # the commit the stub backend reports for every push


# ------------------------------------------------------------------ helpers --


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(status="completed", conclusion="success", name="CI", sha=SHA):
    return {"name": name, "status": status, "conclusion": conclusion,
            "head_sha": sha, "event": "pull_request",
            "html_url": f"https://github.com/demo/repo/actions/runs/{name}"}


class RunsClient:
    """Answers list_workflow_runs from a script of responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.shas = []

    def list_workflow_runs(self, owner, repo, per_page=30, branch=None, head_sha=None):
        self.shas.append(head_sha)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return {"workflow_runs": item}


def _wait(client, **kw):
    clock = Clock()
    kw.setdefault("timeout", 300)
    return wait_for_ci(client, "demo", "repo", SHA, sleep=clock.sleep,
                       clock=clock.time, **kw)


# ------------------------------------------------------------- the request --


def test_new_options_are_off_by_default():
    request = AnalyzeRequest(repo_url="https://github.com/o/r", author_name="A",
                             branch_name="heal/x")
    assert request.open_pull_request is False
    assert request.verify_in_ci is False
    assert request.execute_remediation is False
    assert request.trigger is None


# --------------------------------------------------------------- real CI ---


def test_waits_for_runs_to_finish_and_reports_pass():
    client = RunsClient([], [run(status="in_progress", conclusion=None)], [run()])
    updates = []
    result = _wait(client, on_update=updates.append)
    assert result.status == "passed" and result.passed is True
    assert client.shas == [SHA, SHA, SHA], "runs are looked up by the healing commit"
    assert updates and updates[0][0]["status"] == "in_progress"


def test_any_failed_run_fails_verification():
    client = RunsClient([run(name="CI"), run(name="Lint", conclusion="failure")])
    result = _wait(client)
    assert result.status == "failed" and result.passed is False
    assert "Lint (failure)" in result.detail


def test_skipped_and_neutral_runs_do_not_count_as_failures():
    client = RunsClient([run(conclusion="skipped"), run(name="Docs", conclusion="neutral")])
    assert _wait(client).status == "passed"


def test_no_run_starting_ends_early_instead_of_waiting_for_the_deadline():
    result = _wait(RunsClient([]), timeout=300)
    assert result.status == "no_ci" and result.passed is None
    assert result.waited_seconds < 300


def test_runs_still_going_at_the_deadline_time_out():
    result = _wait(RunsClient([run(status="in_progress", conclusion=None)]), timeout=60)
    assert result.status == "timed_out"


def test_unreadable_actions_is_reported_not_raised():
    result = _wait(RunsClient(GitHubError("forbidden", status=403)))
    assert result.status == "error" and "forbidden" in result.detail


# ---------------------------------------------------------- pull request ---


def _diagnosis():
    return Diagnosis(failure_class=FailureClass.CODE, confidence=0.91,
                     summary="s", recommended_action=HealingAction.FIX_CODE)


def _validation(passed=True):
    return ValidationRun(round_index=1, passed=passed, stages=[
        StageResult("syntax", True, 5, "ok"),
        StageResult("tests", True, 0, "skipped", skipped=True),
    ])


def test_pull_request_explains_the_change_to_a_reviewer():
    fixes = [Fix("a.py", FixTier.DETERMINISTIC, "Removed unused import"),
             Fix("b.py", FixTier.AI, "Fixed comparison to None")]
    memory = {"enabled": True, "incidents_found": 3, "influenced_incidents": ["x"],
              "risk_notes": "- Check b.py"}
    title, body = build_pull_request(
        diagnosis=_diagnosis(), problems_found=5, fixes=fixes,
        validations=[_validation()], memory=memory, job_id="job1",
        trigger="GitHub Actions: CI run #4 failed on main",
    )
    assert title == "fix: 2 automated repairs from the CI/CD healing agent"
    assert "Code-level failure" in body and "91% confidence" in body
    assert "5 problem(s) found, 2 fix(es) applied (1 rule-based, 1 AI" in body
    assert "syntax passed" in body and "tests skipped" in body
    assert "recalled 3 similar past incident(s); 1 influenced" in body
    assert "- Check b.py" in body
    assert "GitHub Actions: CI run #4 failed on main" in body
    assert "never merges" in body


def test_pull_request_title_is_singular_for_one_fix():
    title, _ = build_pull_request(
        diagnosis=None, problems_found=1,
        fixes=[Fix("a.py", FixTier.DETERMINISTIC, "x")],
        validations=[], memory=None, job_id="j",
    )
    assert title == "fix: 1 automated repair from the CI/CD healing agent"


# ------------------------------------------------------ the whole pipeline --


class FakeGitHub:
    """A GitHub API double: anything not scripted fails like a 404 would."""

    pull_error = None
    ci_runs = None
    created = []

    def __init__(self, token=None, **kwargs):
        pass

    def create_pull_request(self, owner, repo, title, head, base, body):
        if FakeGitHub.pull_error:
            raise FakeGitHub.pull_error
        FakeGitHub.created.append({"head": head, "base": base, "title": title, "body": body})
        return {"html_url": f"https://github.com/{owner}/{repo}/pull/7"}

    def list_workflow_runs(self, owner, repo, per_page=30, branch=None, head_sha=None):
        if head_sha:
            return {"workflow_runs": FakeGitHub.ci_runs or []}
        return {"workflow_runs": []}

    def list_commits(self, *args, **kwargs):
        return []

    def close(self):
        pass

    def __getattr__(self, name):
        def missing(*args, **kwargs):
            raise GitHubError(f"{name}: not available in tests", status=404)
        return missing


@pytest.fixture
def fake_github(monkeypatch):
    FakeGitHub.pull_error = None
    FakeGitHub.ci_runs = [run()]
    FakeGitHub.created = []
    monkeypatch.setattr(pipeline_module, "GitHubClient", FakeGitHub)
    return FakeGitHub


class RecordingMemory(HindsightService):
    def __init__(self):
        super().__init__(api_key="test", bank_id="b")
        self.disabled_reason = None
        self.retained = []

    def recall(self, error_context, max_tokens=2000):
        return MemoryRecall(enabled=True, query=error_context)

    def retain(self, report_data, wait=False):
        self.retained.append(report_data)
        return True

    def reflect(self, query):
        return ""


def test_pipeline_opens_a_pull_request_and_verifies_it_in_real_ci(
    broken_repo, tmp_path, monkeypatch, fake_github
):
    memory = RecordingMemory()
    monkeypatch.setattr(pipeline_module, "get_memory_service", lambda: memory)
    job = _run(broken_repo, tmp_path, monkeypatch, open_pull_request=True,
               verify_in_ci=True, trigger="GitHub Actions: CI run #4 failed on main")
    snapshot = job.snapshot()

    assert fake_github.created[0]["head"] == "heal/fixes"
    assert fake_github.created[0]["base"] == "main"
    assert snapshot["pull_request_url"] == "https://github.com/demo/repo/pull/7"
    assert snapshot["ci_verification"]["status"] == "passed"
    assert snapshot["trigger"] == "GitHub Actions: CI run #4 failed on main"
    messages = [entry["message"] for entry in snapshot["logs"]]
    assert any("Triggered by: GitHub Actions" in m for m in messages)
    assert any(m.startswith("Opened pull request") for m in messages)
    assert any("Waiting for the repository's own CI" in m for m in messages)
    report = snapshot["incident_report"]
    assert "**Pull request:** https://github.com/demo/repo/pull/7" in report
    assert "Real CI on the healing commit: PASSED" in report
    record = memory.retained[0]
    assert record["ci_verification"]["status"] == "passed"
    assert record["pull_request_url"].endswith("/pull/7")


def test_failing_real_ci_downgrades_the_run(broken_repo, tmp_path, monkeypatch, fake_github):
    fake_github.ci_runs = [run(conclusion="failure")]
    job = _run(broken_repo, tmp_path, monkeypatch, verify_in_ci=True)
    snapshot = job.snapshot()
    assert snapshot["ci_verification"]["status"] == "failed"
    assert job.status is not JobStatus.SUCCEEDED
    assert "Real CI on the healing commit: FAILED" in snapshot["incident_report"]


def test_a_pull_request_error_does_not_stop_the_run(
    broken_repo, tmp_path, monkeypatch, fake_github
):
    fake_github.pull_error = GitHubError("A pull request already exists", status=422)
    job = _run(broken_repo, tmp_path, monkeypatch, open_pull_request=True)
    snapshot = job.snapshot()
    assert snapshot["pull_request_url"] is None
    assert snapshot["branch_url"], "the branch is still pushed"
    assert any("Could not open a pull request" in e["message"] for e in snapshot["logs"])


def test_manual_runs_are_unchanged_by_default(broken_repo, tmp_path, monkeypatch, fake_github):
    job = _run(broken_repo, tmp_path, monkeypatch)
    snapshot = job.snapshot()
    assert fake_github.created == []
    assert snapshot["pull_request_url"] is None
    assert snapshot["ci_verification"] == {}


def test_dry_runs_never_open_pull_requests_or_wait_for_ci(
    broken_repo, tmp_path, monkeypatch, fake_github
):
    job = _run(broken_repo, tmp_path, monkeypatch, push=False,
               open_pull_request=True, verify_in_ci=True)
    assert fake_github.created == []
    assert job.snapshot()["ci_verification"] == {}


@pytest.mark.parametrize("allowed", [True, False])
def test_live_remediation_needs_explicit_permission(
    broken_repo, tmp_path, monkeypatch, fake_github, allowed
):
    seen = {}
    real = pipeline_module.remediate

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "remediate", spy)
    _run(broken_repo, tmp_path, monkeypatch, execute_remediation=allowed)
    assert seen["execute_pipeline_actions"] is allowed


# --------------------------------------------------------------- trigger ---


def test_the_ci_trigger_workflow_cannot_heal_its_own_branches():
    workflow = yaml.safe_load((ROOT / "examples" / "heal-on-ci-failure.yml").read_text())
    trigger = workflow[True]["workflow_run"]  # YAML 1.1 reads the key `on` as True
    assert trigger["types"] == ["completed"]

    job = workflow["jobs"]["heal"]
    guard = job["if"]
    assert "conclusion == 'failure'" in guard
    assert "event == 'push'" in guard
    assert "!startsWith(github.event.workflow_run.head_branch, 'heal/')" in guard

    start = next(step for step in job["steps"] if step.get("id") == "start")
    assert start["env"]["HEAL_BRANCH"].startswith("heal/"), (
        "healing branches must match the guard's prefix, or a failing fix "
        "would trigger another healing run"
    )
    script = start["run"]
    for option in ("open_pull_request: true", "verify_in_ci: true",
                   "execute_remediation: $rerun"):
        assert option in script
    assert "run_attempt == 1" in start["env"]["FIRST_ATTEMPT"], (
        "re-running jobs is limited to a run's first failure"
    )
    assert "${{" not in script, "workflow values reach the shell only via env"
