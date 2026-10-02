from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolDecision:
    tool_name: str | None
    arguments: dict[str, Any] = field(default_factory=dict)
    intent: str | None = None
    decision_type: str = "tool"
    answer: str | None = None


@dataclass(frozen=True)
class ToolSelectionError:
    code: str
    message: str


@dataclass(frozen=True)
class ToolSelectionResult:
    decision: ToolDecision | None
    tool_result: dict[str, Any] | None = None
    error: ToolSelectionError | None = None


@dataclass(frozen=True)
class InvestigationToolCall:
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    call_id: str = ""


@dataclass
class InvestigationState:
    task: str
    tool_calls: list[InvestigationToolCall] = field(default_factory=list)
    status: str = "in_progress"
    final_answer: str | None = None
    error: ToolSelectionError | None = None

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)
