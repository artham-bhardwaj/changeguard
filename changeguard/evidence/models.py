from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4
SourceType = Literal["incident", "postmortem", "runbook", "architecture_decision", "issue", "pull_request", "release_note"]
def now() -> str: return datetime.now(timezone.utc).isoformat()
@dataclass(frozen=True)
class EvidenceDocument:
    title: str
    source_type: SourceType
    source: str
    repository: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now)
    evidence_id: str = field(default_factory=lambda: str(uuid4()))
    content_hash: str = ""
@dataclass(frozen=True)
class EvidenceChunk:
    chunk_id: str
    evidence_id: str
    chunk_index: int
    content: str
    character_count: int
@dataclass(frozen=True)
class RetrievedEvidence:
    evidence_id: str
    chunk_id: str
    title: str
    source: str
    similarity_score: float
    matched_text: str
    metadata: dict[str, Any]
    source_type: str = "unknown"
@dataclass(frozen=True)
class EvidenceBundle:
    query: str
    results: list[RetrievedEvidence]
    retrieval_timestamp: str
    retrieval_method: str
    top_k: int
    filters: dict[str, str | None]
    status: str
