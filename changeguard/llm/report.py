from __future__ import annotations

from dataclasses import asdict, dataclass

from changeguard.llm.decision import InvestigationState


@dataclass(frozen=True)
class InvestigationFinding:
    statement: str
    evidence_refs: list[str]


@dataclass(frozen=True)
class InvestigationEvidenceReference:
    reference: str
    tool_name: str
    description: str


@dataclass(frozen=True)
class InvestigationReport:
    summary: str
    summary_evidence_refs: list[str]
    findings: list[InvestigationFinding]
    evidence: list[InvestigationEvidenceReference]
    uncertainties: list[str]
    recommended_verification: list[str]
    tool_trace: list[str]
    verification: dict[str, object] | None = None
    evaluation: dict[str, object] | None = None
    api_impact: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ReportGenerationResult:
    report: InvestigationReport | None
    error: str | None = None
    investigation: InvestigationState | None = None
    report_generation_duration_ms: float | None = None
