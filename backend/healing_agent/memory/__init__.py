"""Persistent incident memory (Hindsight). Optional: a no-op without a key."""

from __future__ import annotations

from .hindsight_service import (
    HindsightService,
    MemoryRecall,
    RecalledMemory,
    format_incident,
    get_memory_service,
    incident_tags,
    reset_memory_service,
)
from .records import build_incident_record, build_recall_query

__all__ = [
    "build_incident_record",
    "build_recall_query",
    "HindsightService",
    "MemoryRecall",
    "RecalledMemory",
    "format_incident",
    "get_memory_service",
    "incident_tags",
    "reset_memory_service",
]
