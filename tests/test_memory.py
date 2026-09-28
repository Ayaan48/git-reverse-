"""Persistent incident memory (Hindsight): additive, never load-bearing."""

from healing_agent import pipeline as pipeline_module
from healing_agent.cicd import DiagnosisInput, diagnose
from healing_agent.cicd.statuspage import PlatformStatus
from healing_agent.cicd.telemetry import PipelineTelemetry
from healing_agent.memory import (
    HindsightService,
    MemoryRecall,
    RecalledMemory,
    format_incident,
    incident_tags,
)
from healing_agent.models import FailureClass
from test_pipeline_integration import _run


class FakeMemory(HindsightService):
    """An enabled memory service that never touches the network."""

    def __init__(self, memories=None):
        super().__init__(api_key="test-key", bank_id="test-bank")
        self.disabled_reason = None
        self.memories = memories or []
        self.queries: list[str] = []
        self.retained: list[dict] = []

    def recall(self, error_context, max_tokens=2000):
        self.queries.append(error_context)
        return MemoryRecall(enabled=True, query=error_context, memories=self.memories)

    def retain(self, report_data, wait=False):
        self.retained.append(report_data)
        return True

    def reflect(self, query):
        return "- Check the rewritten import before merging."


def _platform_memory(n: int) -> list[RecalledMemory]:
    return [
        RecalledMemory(
            id=f"m{i}", text=f"Rate limit hit on run {i}; retry with backoff fixed it.",
            incident_id=f"incident-{i}", failure_class="platform",
        )
        for i in range(n)
    ]


# ------------------------------------------------------------------ service --


def test_service_without_key_is_a_silent_no_op(monkeypatch):
    monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
    service = HindsightService()
    assert not service.enabled
    assert "HINDSIGHT_API_KEY" in service.disabled_reason
    assert service.retain({"repo": "o/r"}) is False
    recall = service.recall("ModuleNotFoundError: No module named 'yaml'")
    assert recall.memories == [] and recall.error is None
    assert service.reflect("anything") == ""
    assert service.ping()["connected"] is False


def test_unreachable_service_degrades_instead_of_raising():
    service = HindsightService(api_url="http://127.0.0.1:9", api_key="k", timeout=1)
    assert service.enabled
    recall = service.recall("SyntaxError: invalid syntax")
    assert recall.memories == [] and recall.error
    assert service.retain({"repo": "o/r", "outcome": "failed"}) is False
    assert service.reflect("risk?") == ""


def test_incident_text_records_repairs_and_their_validation_outcome():
    report = {
        "incident_id": "incident-1", "repo": "acme/api", "failure_class": "code",
        "confidence": 0.9, "failure_types": ["import"], "outcome": "succeeded",
        "error_signatures": ["ModuleNotFoundError: No module named 'yaml'"],
        "repairs": [
            {"description": "Declared PyYAML", "file": "requirements.txt",
             "tier": "deterministic", "passed_validation": True},
            {"description": "Removed the import", "passed_validation": False},
        ],
    }
    text = format_incident(report)
    assert "code-level failure" in text
    assert "Declared PyYAML in requirements.txt (deterministic tier): passed validation" in text
    assert "Removed the import: did NOT pass validation" in text
    assert incident_tags(report) == sorted(
        ["incident:incident-1", "repo:acme/api", "class:code", "failure:import",
         "outcome:succeeded"]
    )


# ---------------------------------------------------------------- diagnosis --


def _live_rate_limit_input(memories):
    return DiagnosisInput(
        telemetry=PipelineTelemetry(),
        platform_status=PlatformStatus(),
        log_text="Error: API rate limit exceeded for installation",
        recalled_memories=[m.to_dict() for m in memories],
    )


def test_matching_precedents_add_capped_weight_and_are_attributed():
    without = diagnose(_live_rate_limit_input([]))
    with_memory = diagnose(_live_rate_limit_input(_platform_memory(10)))

    assert with_memory.failure_class is FailureClass.PLATFORM
    gained = with_memory.signals["platform_score"] - without.signals["platform_score"]
    assert 0 < gained <= 1.5, "memory is a prior, never a trump card"
    assert len(with_memory.signals["memory_influenced"]) == 10
    assert any(line.startswith("[memory +") for line in with_memory.evidence)


def test_memory_alone_cannot_manufacture_a_verdict():
    quiet = DiagnosisInput(
        telemetry=PipelineTelemetry(), platform_status=PlatformStatus(),
        recalled_memories=[m.to_dict() for m in _platform_memory(10)],
    )
    diagnosis = diagnose(quiet)
    assert diagnosis.failure_class is FailureClass.UNKNOWN
    assert diagnosis.signals["memory_influenced"] == []


def test_diagnosis_without_memory_is_unchanged():
    diagnosis = diagnose(_live_rate_limit_input([]))
    assert "memory_incidents" not in diagnosis.signals
    assert not any(line.startswith("[memory") for line in diagnosis.evidence)


# ----------------------------------------------------------------- pipeline --


def test_pipeline_recalls_before_diagnosis_and_retains_after(
    broken_repo, tmp_path, monkeypatch
):
    fake = FakeMemory(memories=_platform_memory(2))
    monkeypatch.setattr(pipeline_module, "get_memory_service", lambda: fake)

    job = _run(broken_repo, tmp_path, monkeypatch)
    snapshot = job.snapshot()

    assert fake.queries and "syntax" in fake.queries[0].lower()
    assert len(fake.retained) == 1
    record = fake.retained[0]
    assert record["repo"] == "demo/repo"
    assert record["outcome"] == snapshot["status"]
    assert record["repairs"], "repairs tried must be retained"

    memory = snapshot["memory"]
    assert memory["enabled"] and memory["retained"] is True
    assert memory["incidents_found"] == 2
    assert memory["risk_notes"]
    assert "Incident memory" in snapshot["incident_report"]


def test_pipeline_without_memory_reports_it_off(broken_repo, tmp_path, monkeypatch):
    job = _run(broken_repo, tmp_path, monkeypatch)
    snapshot = job.snapshot()
    assert snapshot["memory"]["enabled"] is False
    assert snapshot["memory"]["retained"] is None
    assert any("Memory: off" in entry["message"] for entry in snapshot["logs"])
