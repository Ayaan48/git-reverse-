"""Persistent incident memory backed by Hindsight (Vectorize).

The agent retains every run's post-incident data and recalls similar past
incidents before diagnosing a new one, so a failure it has already seen is
recognised rather than re-derived from scratch.

Memory is additive, never load-bearing. With no API key, no client library, or
an unreachable service, every method degrades to an empty result and the agent
behaves exactly as it did without memory. Nothing in here raises into the
pipeline.

The Hindsight client is async underneath (its sync methods drive an event loop
of their own), and the pipeline runs on a worker thread. Each operation
therefore opens a short-lived client inside its own event loop, which is safe
from any thread and never collides with the FastAPI loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..redaction import scrub

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.hindsight.vectorize.io"
DEFAULT_BANK_ID = "cicd-healing-agent"
BANK_TEMPLATE_PATH = Path(__file__).with_name("bank-template.json")

# Health checks poll often; reuse a connectivity result for this long.
PING_TTL_SECONDS = 60.0

# A recall query longer than this adds noise rather than precision.
MAX_QUERY_CHARS = 3000

# Recall returns a long, rapidly-decaying tail. The reranker score separates
# genuinely similar incidents (~0.9+) from loosely related ones (<0.1) sharply,
# so anything below this is dropped rather than shown or voted on.
DEFAULT_MIN_RELEVANCE = 0.1

# Observations Hindsight consolidates across facts carry no document id, but
# they name the incident in their text.
_INCIDENT_TEXT_RE = re.compile(r"\b(seed-\d{3}|incident-[0-9a-f]{6,})\b")

_CLASS_TEXT_RE = re.compile(
    r"\b(platform|code|mixed)[- ](?:level|failure|side|classification)", re.IGNORECASE
)


@dataclass
class RecalledMemory:
    """One fact Hindsight returned, annotated with the incident it came from."""

    id: str
    text: str
    type: str = ""
    incident_id: str = ""
    repo: str = ""
    failure_class: str = ""
    outcome: str = ""
    occurred_at: str = ""
    tags: list[str] = field(default_factory=list)
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": None if self.score is None else round(self.score, 3),
            "id": self.id,
            "text": self.text,
            "type": self.type,
            "incident_id": self.incident_id,
            "repo": self.repo,
            "failure_class": self.failure_class,
            "outcome": self.outcome,
            "occurred_at": self.occurred_at,
            "tags": self.tags,
        }


@dataclass
class MemoryRecall:
    """Result of a recall. `error` is set when memory was enabled but failed."""

    enabled: bool
    query: str = ""
    memories: list[RecalledMemory] = field(default_factory=list)
    error: str | None = None

    @property
    def incident_ids(self) -> list[str]:
        """Distinct past incidents, in order of first (most relevant) mention."""
        seen: dict[str, None] = {}
        for memory in self.memories:
            seen.setdefault(memory.incident_id or memory.id, None)
        return list(seen)

    def as_prompt_context(self, limit: int = 8) -> str:
        """Compact text block for a model prompt. Empty when nothing recalled."""
        if not self.memories:
            return ""
        lines = [
            f"- [{m.failure_class or 'unclassified'}"
            + (f", {m.repo}" if m.repo else "")
            + f"] {m.text}"
            for m in self.memories[:limit]
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "incidents_found": len(self.incident_ids),
            "memories_found": len(self.memories),
            "memories": [m.to_dict() for m in self.memories],
            "error": self.error,
        }


def _describe(exc: BaseException) -> str:
    """Scrubbed error text. Timeouts often stringify to '' -- name them instead."""
    return scrub(str(exc)) or type(exc).__name__


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


class HindsightService:
    """Thin, failure-tolerant wrapper over the Hindsight Python client."""

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        bank_id: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.api_url = (api_url or _env("HINDSIGHT_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.api_key = api_key or _env("HINDSIGHT_API_KEY")
        self.bank_id = bank_id or _env("HINDSIGHT_BANK_ID") or DEFAULT_BANK_ID
        self.timeout = timeout
        self.disabled_reason: str | None = None
        self._ping_cache: tuple[float, dict[str, Any]] | None = None

        if not self.api_key:
            self.disabled_reason = "HINDSIGHT_API_KEY is not set"
        else:
            try:
                import hindsight_client  # noqa: F401
            except ImportError:
                self.disabled_reason = "hindsight-client is not installed"

        if self.disabled_reason:
            log.warning("Hindsight memory disabled: %s", self.disabled_reason)

    # ------------------------------------------------------------ plumbing --
    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    def describe(self) -> dict[str, Any]:
        """Non-secret configuration summary."""
        return {
            "enabled": self.enabled,
            "api_url": self.api_url,
            "bank_id": self.bank_id,
            "disabled_reason": self.disabled_reason,
        }

    def _run(self, operation: Callable[[Any], Awaitable[Any]]) -> Any:
        """Run `operation(client)` to completion on a private event loop."""
        from hindsight_client import Hindsight

        async def runner() -> Any:
            client = Hindsight(
                base_url=self.api_url,
                api_key=self.api_key,
                timeout=self.timeout,
                max_attempts=2,
            )
            try:
                return await operation(client)
            finally:
                await client.aclose()

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(runner())
        # Called from inside a running loop: hand off to a fresh thread rather
        # than nesting loops.
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, runner()).result()

    # --------------------------------------------------------------- health --
    def ping(self) -> dict[str, Any]:
        """Connectivity check for /api/health. Never raises; cached briefly."""
        status = self.describe()
        if not self.enabled:
            status["connected"] = False
            return status
        cached = self._ping_cache
        if cached and time.monotonic() - cached[0] < PING_TTL_SECONDS:
            return dict(cached[1])
        try:
            version = self._run(lambda c: c.aget_version())
            status["connected"] = True
            status["api_version"] = getattr(version, "api_version", None)
        except Exception as exc:  # noqa: BLE001
            status["connected"] = False
            status["error"] = _describe(exc)[:200]
        self._ping_cache = (time.monotonic(), dict(status))
        return status

    # ------------------------------------------------------------- retain --
    def retain(self, report_data: dict[str, Any], wait: bool = False) -> bool:
        """Store one completed run. Returns True when Hindsight accepted it.

        By default fact extraction runs server-side after the call returns,
        so retaining never adds model latency to the end of a run. Pass
        `wait=True` to block until the memory is searchable.
        """
        if not self.enabled:
            return False
        try:
            content = format_incident(report_data)
            tags = incident_tags(report_data)
            timestamp = _parse_timestamp(report_data.get("timestamp"))
            incident_id = str(report_data.get("incident_id") or "") or None
            metadata = {
                key: str(report_data[key])
                for key in ("repo", "failure_class", "outcome", "incident_id")
                if report_data.get(key)
            }

            self._run(
                lambda c: c.aretain(
                    bank_id=self.bank_id,
                    content=content,
                    timestamp=timestamp,
                    context="CI/CD post-incident report from the healing agent",
                    document_id=incident_id,
                    metadata=metadata,
                    tags=tags,
                    retain_async=not wait,
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Hindsight retain failed: %s", _describe(exc)[:300])
            return False

    # ------------------------------------------------------------- recall --
    def recall(
        self,
        error_context: str,
        max_tokens: int = 2000,
        min_relevance: float = DEFAULT_MIN_RELEVANCE,
    ) -> MemoryRecall:
        """Find past incidents similar to `error_context`.

        Results below `min_relevance` (reranker score) are dropped.
        """
        query = scrub(error_context or "").strip()[:MAX_QUERY_CHARS]
        result = MemoryRecall(enabled=self.enabled, query=query)
        if not self.enabled or not query:
            return result
        try:
            response = self._run(
                lambda c: c.arecall(
                    bank_id=self.bank_id,
                    query=query,
                    max_tokens=max_tokens,
                    budget="mid",
                )
            )
            memories = [
                _to_memory(item) for item in (getattr(response, "results", None) or [])
            ]
            result.memories = [
                m for m in memories if m.score is None or m.score >= min_relevance
            ]
        except Exception as exc:  # noqa: BLE001
            result.error = _describe(exc)[:300]
            log.warning("Hindsight recall failed: %s", result.error)
        return result

    # ------------------------------------------------------------ reflect --
    def reflect(self, query: str) -> str:
        """Reason over accumulated memory, e.g. for pre-merge risk notes."""
        if not self.enabled or not query.strip():
            return ""
        try:
            response = self._run(
                lambda c: c.areflect(bank_id=self.bank_id, query=scrub(query))
            )
            return getattr(response, "text", "") or ""
        except Exception as exc:  # noqa: BLE001
            log.warning("Hindsight reflect failed: %s", _describe(exc)[:300])
            return ""

    # ------------------------------------------------------- bank set-up --
    def ensure_bank(self, template_path: Path = BANK_TEMPLATE_PATH) -> dict[str, Any]:
        """Create (or update) the bank from bank-template.json.

        Idempotent: re-running updates mission and disposition in place and
        only adds directives whose name is not already present.
        """
        if not self.enabled:
            raise RuntimeError(f"Hindsight memory disabled: {self.disabled_reason}")
        template = json.loads(template_path.read_text(encoding="utf-8"))
        disposition = template.get("disposition", {})

        async def setup(client: Any) -> dict[str, Any]:
            await client.acreate_bank(
                bank_id=self.bank_id,
                name=template.get("name"),
                mission=template.get("mission"),
                retain_mission=template.get("retain_mission"),
                disposition_skepticism=disposition.get("skepticism"),
                disposition_literalism=disposition.get("literalism"),
                disposition_empathy=disposition.get("empathy"),
                enable_temporal_retrieval=True,
            )
            existing = await client.alist_directives(bank_id=self.bank_id)
            existing_names = {
                getattr(d, "name", None) for d in _items(existing)
            }
            added = []
            for directive in template.get("directives", []):
                if directive["name"] in existing_names:
                    continue
                await client.acreate_directive(
                    bank_id=self.bank_id,
                    name=directive["name"],
                    content=directive["content"],
                    priority=directive.get("priority", 0),
                )
                added.append(directive["name"])
            return {"bank_id": self.bank_id, "directives_added": added}

        return self._run(setup)


# ---------------------------------------------------------------- helpers --


def _items(response: Any) -> list[Any]:
    """Pull the list out of a paginated/list response, whatever its shape."""
    if isinstance(response, list):
        return response
    for attr in ("items", "directives", "results", "data"):
        value = getattr(response, attr, None)
        if isinstance(value, list):
            return value
    if isinstance(response, dict):
        for key in ("items", "directives", "results", "data"):
            if isinstance(response.get(key), list):
                return response[key]
    return []


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _tag_value(tags: list[str], prefix: str) -> str:
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return ""


def _to_memory(item: Any) -> RecalledMemory:
    tags = list(getattr(item, "tags", None) or [])
    metadata = getattr(item, "metadata", None) or {}
    text = str(getattr(item, "text", "") or "")

    failure_class = metadata.get("failure_class") or _tag_value(tags, "class:")
    if not failure_class:
        match = _CLASS_TEXT_RE.search(text)
        failure_class = match.group(1).lower() if match else ""

    occurred = (
        getattr(item, "occurred_start", None) or getattr(item, "mentioned_at", None)
    )
    scores = getattr(item, "scores", None)
    if isinstance(scores, dict):
        score = scores.get("reranker", scores.get("final"))
    else:
        score = getattr(scores, "reranker", None)
        if score is None:
            score = getattr(scores, "final", None)
    incident_id = (
        _tag_value(tags, "incident:")
        or metadata.get("incident_id")
        or getattr(item, "document_id", None)
        or ""
    )
    if not incident_id:
        match = _INCIDENT_TEXT_RE.search(text)
        incident_id = match.group(1) if match else ""
    return RecalledMemory(
        id=str(getattr(item, "id", "")),
        text=text,
        type=str(getattr(item, "type", "") or ""),
        incident_id=str(incident_id),
        repo=metadata.get("repo") or _tag_value(tags, "repo:"),
        failure_class=failure_class,
        outcome=metadata.get("outcome") or _tag_value(tags, "outcome:"),
        occurred_at=str(occurred) if occurred else "",
        tags=tags,
        score=float(score) if isinstance(score, int | float) else None,
    )


def incident_tags(report: dict[str, Any]) -> list[str]:
    """Tags used to scope and group memories: repo, class, failure types."""
    tags: list[str] = []
    if report.get("incident_id"):
        tags.append(f"incident:{report['incident_id']}")
    if report.get("repo"):
        tags.append(f"repo:{report['repo']}")
    if report.get("failure_class"):
        tags.append(f"class:{report['failure_class']}")
    for failure_type in report.get("failure_types") or []:
        tags.append(f"failure:{failure_type}")
    if report.get("outcome"):
        tags.append(f"outcome:{report['outcome']}")
    return sorted(set(tags))


def format_incident(report: dict[str, Any]) -> str:
    """Render the structured report as prose Hindsight can extract facts from.

    Written as full sentences rather than a table on purpose: fact extraction
    works on statements, and "repair X passed validation" survives extraction
    where a bare "| X | pass |" row does not.
    """
    repo = report.get("repo") or "an unknown repository"
    when = report.get("timestamp") or datetime.now(UTC).isoformat()
    failure_class = report.get("failure_class") or "unknown"
    incident = f" {report['incident_id']}" if report.get("incident_id") else ""
    lines = [
        f"CI/CD incident{incident} in {repo} at {when}.",
        f"The failure was classified as a {failure_class}-level failure"
        + (
            f" with {float(report['confidence']):.0%} confidence"
            if report.get("confidence") is not None
            else ""
        )
        + (
            f"; the recommended action was {report['recommended_action']}."
            if report.get("recommended_action")
            else "."
        ),
    ]
    if report.get("failure_types"):
        lines.append("Failure types: " + ", ".join(report["failure_types"]) + ".")
    if report.get("summary"):
        lines.append(f"Root cause: {report['summary']}")
    if report.get("error_signatures"):
        lines.append("Error signatures observed:")
        lines.extend(f"- {sig}" for sig in report["error_signatures"][:15])
    if report.get("packages"):
        lines.append("Packages/modules involved: " + ", ".join(report["packages"]) + ".")
    if report.get("evidence"):
        lines.append("Evidence that decided the classification:")
        lines.extend(f"- {item}" for item in report["evidence"][:10])

    repairs = report.get("repairs") or []
    if repairs:
        lines.append("Repairs attempted:")
        for repair in repairs[:20]:
            verdict = (
                "passed validation"
                if repair.get("passed_validation")
                else "did NOT pass validation (rejected)"
            )
            where = f" in {repair['file']}" if repair.get("file") else ""
            tier = f" ({repair['tier']} tier)" if repair.get("tier") else ""
            lines.append(f"- {repair.get('description', 'repair')}{where}{tier}: {verdict}.")
    else:
        lines.append("No code repairs were attempted.")

    for action in report.get("remediations") or []:
        lines.append(f"Corrective action: {action}.")

    for run in report.get("validation") or []:
        failed = run.get("failed_stages") or []
        lines.append(
            f"Validation round {run.get('round')}: "
            + ("passed." if run.get("passed") else f"failed at {', '.join(failed) or 'unknown stage'}.")
        )

    lines.append(f"Final outcome: {report.get('outcome', 'unknown')}.")
    if report.get("notes"):
        lines.append(str(report["notes"]))
    return scrub("\n".join(lines))


# ------------------------------------------------------------- singleton --

_service: HindsightService | None = None
_service_lock = threading.Lock()


def get_memory_service() -> HindsightService:
    """Process-wide service, built from the environment on first use."""
    global _service
    with _service_lock:
        if _service is None:
            _service = HindsightService()
        return _service


def reset_memory_service() -> None:
    """Drop the cached service. Used by tests that patch the environment."""
    global _service
    with _service_lock:
        _service = None
