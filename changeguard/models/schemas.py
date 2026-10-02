"""Typed data exchanged between ChangeGuard's tools, agent, and storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RepoStatus:
    branch: str
    head_commit: str
    modified_files: list[str]


@dataclass(frozen=True)
class DiffResult:
    changed_files: list[str]
    additions: int
    deletions: int
    diff_text: str


@dataclass(frozen=True)
class JavaSourceSnapshot:
    revision: str
    sources: dict[str, str]
    omitted_files: int = 0


@dataclass(frozen=True)
class CommitMetadata:
    commit: str
    author: str
    timestamp: str
    subject: str


@dataclass(frozen=True)
class Evidence:
    kind: str
    summary: str
    details: dict[str, Any]


@dataclass(frozen=True)
class AgentTrace:
    step: int
    tool_name: str
    input: dict[str, Any]
    output_summary: str
    transport: str = "local"
    timestamp: str = field(default_factory=utc_now)


@dataclass
class AnalysisState:
    repository: str
    base_commit: str
    head_commit: str
    changed_files: list[str]
    diff_summary: dict[str, int]
    recent_commits: list[CommitMetadata]
    evidence: list[Evidence]
    agent_trace: list[AgentTrace]
    status: str
    analysis_id: str = field(default_factory=lambda: str(uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
