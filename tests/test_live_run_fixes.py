"""Regressions found by the first live autonomous run (Broken-demo, 3 Oct).

1. The model monitor spent Gemini's small per-minute quota on test
   generations, so the real repair that followed got HTTP 429.
2. A re-run CI attempt looked like 23 minutes of runner queueing, which read
   as "critical queue pressure" and pulled the diagnosis toward the platform.
3. Adding the healing trigger (a workflow) read as a suspicious CI config
   change, so a plain code failure was answered with rollback_config.
"""

import httpx
import pytest
from healing_agent.cicd.telemetry import collect_telemetry
from healing_agent.config import Settings
from healing_agent.healing.providers import GeminiProvider, ProviderError
from healing_agent.pipeline import _failing_workflow_changes

KEY = "AIzaSyFAKE-KEY-FOR-TESTS-0123456789abcd"
OK_ANSWER = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
    {"text": '{"fixed_content": "x = 1\\n", "changes": [], "unable_to_fix": false, "reason": ""}'}
]}}]}


def _gemini(tmp_path, handler, sleeps):
    settings = Settings(workspace_root=tmp_path, anthropic_api_key=None, gemini_api_key=KEY)
    return GeminiProvider(settings, transport=httpx.MockTransport(handler), sleep=sleeps.append)


def _rate_limited(retry_delay=None, quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier"):
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": quota_id}]}]
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": retry_delay})
    return httpx.Response(429, json={"error": {"code": 429, "message": "quota", "details": details}})


# ------------------------------------------------------- 1. Gemini quota --


def test_health_check_reads_model_metadata_instead_of_generating(tmp_path):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"name": "models/gemini-2.5-flash"})

    status = _gemini(tmp_path, handler, []).check()
    assert status.ok is True
    assert seen == [("GET", "/v1beta/models/gemini-2.5-flash")]
    assert not any("generateContent" in path for _, path in seen)


def test_per_minute_limit_waits_as_long_as_google_asks_then_succeeds(tmp_path):
    responses = [_rate_limited("7s"), httpx.Response(200, json=OK_ANSWER)]
    sleeps = []
    provider = _gemini(tmp_path, lambda r: responses.pop(0), sleeps)
    assert provider.request_repair("a.py", "x = 1\n", [])["fixed_content"] == "x = 1\n"
    assert sleeps == [7.0]


def test_daily_quota_fails_at_once_without_waiting(tmp_path):
    calls, sleeps = [], []

    def handler(request):
        calls.append(1)
        return _rate_limited("30s", quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier")

    with pytest.raises(ProviderError) as info:
        _gemini(tmp_path, handler, sleeps).request_repair("a.py", "x = 1\n", [])
    assert info.value.fatal and len(calls) == 1 and sleeps == []


def test_an_unreasonably_long_wait_is_not_honoured(tmp_path):
    sleeps = []
    provider = _gemini(tmp_path, lambda r: _rate_limited("900s"), sleeps)
    with pytest.raises(ProviderError) as info:
        provider.request_repair("a.py", "x = 1\n", [])
    assert info.value.fatal and sleeps == []


# ------------------------------------------------ 2. re-runs and queueing --


class RunsOnly:
    def __init__(self, runs):
        self.runs = runs

    def list_workflow_runs(self, owner, repo, per_page=30, branch=None, head_sha=None):
        return {"workflow_runs": self.runs}


def _run(attempt, created, started):
    return {"id": attempt, "name": "CI", "status": "completed", "conclusion": "success",
            "run_attempt": attempt, "created_at": created, "run_started_at": started,
            "head_branch": "main", "event": "push"}


def test_a_rerun_does_not_count_as_runner_queue_time():
    rerun = _run(2, "2026-10-03T16:53:00Z", "2026-10-03T17:16:00Z")  # re-run 23 min later
    telemetry = collect_telemetry(RunsOnly([rerun]), "o", "r")
    assert telemetry.max_queue_seconds == 0
    assert telemetry.queue_pressure == "normal"


def test_real_queueing_on_a_first_attempt_is_still_detected():
    slow = _run(1, "2026-10-03T16:53:00Z", "2026-10-03T17:08:00Z")  # waited 15 min
    telemetry = collect_telemetry(RunsOnly([slow]), "o", "r")
    assert telemetry.queue_pressure == "critical"


# --------------------------------------- 3. which workflow changes matter --


def _workflows(tmp_path):
    folder = tmp_path / ".github" / "workflows"
    folder.mkdir(parents=True)
    (folder / "ci.yml").write_text("name: CI\non: push\njobs: {}\n")
    (folder / "heal.yml").write_text("name: Heal failed CI\non: workflow_run\njobs: {}\n")
    (folder / "nameless.yml").write_text("on: push\njobs: {}\n")
    return [".github/workflows/ci.yml", ".github/workflows/heal.yml",
            ".github/workflows/nameless.yml"]


def test_only_changes_to_failing_workflows_are_suspects(tmp_path):
    changed = _workflows(tmp_path)
    assert _failing_workflow_changes(tmp_path, changed, ["CI"]) == [".github/workflows/ci.yml"]


def test_adding_the_healing_trigger_alone_is_not_a_suspect(tmp_path):
    changed = _workflows(tmp_path)[1:2]  # only heal.yml changed
    assert _failing_workflow_changes(tmp_path, changed, ["CI"]) == []


def test_a_workflow_without_a_name_is_matched_by_its_path(tmp_path):
    changed = _workflows(tmp_path)
    failing = [".github/workflows/nameless.yml"]
    assert _failing_workflow_changes(tmp_path, changed, failing) == failing


def test_without_failure_telemetry_every_change_is_kept(tmp_path):
    changed = _workflows(tmp_path)
    assert _failing_workflow_changes(tmp_path, changed, []) == changed
