from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from uuid import uuid4

from changeguard.code_intelligence.api import ApiDependencyEdge, ApiImpact
from changeguard.code_intelligence.java import ChangedLineRange, JavaAnalysis, JavaSymbol
from changeguard.evidence.models import EvidenceBundle
from changeguard.llm.decision import InvestigationState
from changeguard.llm.report import InvestigationReport
from changeguard.models.schemas import CommitMetadata, RepoStatus
from changeguard.risk.models import BlastRadiusSignals, RiskAssessment, RuntimeSignals, TestSignals
from changeguard.verification.models import VerificationPlan, VerificationResult

StageStatus = Literal["completed", "unavailable", "skipped", "failed"]


@dataclass(frozen=True)
class ChangeStageTrace:
    stage: str
    status: StageStatus
    duration_ms: float
    failure: str | None = None
    counters: dict[str, int | float | str] = field(default_factory=dict)


@dataclass(frozen=True)
class BlastRadiusResult:
    signals: BlastRadiusSignals
    direct_dependents: tuple[str, ...]
    indirect_dependents: tuple[str, ...]
    affected_files: tuple[str, ...]
    affected_packages: tuple[str, ...]
    maximum_dependency_depth: int | None
    unresolved_relationships: int | None
    uncertainties: tuple[str, ...]
    cross_language_api_consumers: tuple[str, ...] = ()
    api_edges: tuple[ApiDependencyEdge, ...] = ()
    api_unresolved_relationships: int | None = 0
    api_ambiguous_relationships: int = 0
    api_provider_symbols_for_changed_consumers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChangeGuardAnalysis:
    analysis_id: str
    repository: str
    change_reference: str
    change_summary: str
    repository_status: RepoStatus
    changed_files: tuple[str, ...]
    changed_lines: tuple[ChangedLineRange, ...]
    changed_symbols: tuple[JavaSymbol, ...]
    file_level_changes: tuple[str, ...]
    recent_commits: tuple[CommitMetadata, ...]
    code_analysis: JavaAnalysis | None
    blast_radius: BlastRadiusResult | None
    historical_evidence: EvidenceBundle | None
    test_signals: TestSignals
    runtime_signals: RuntimeSignals
    risk_assessment: RiskAssessment
    investigation_report: InvestigationReport | None = None
    api_impact: ApiImpact | None = None
    investigation_state: InvestigationState | None = None
    investigation_error: str | None = None
    uncertainties: tuple[str, ...] = ()
    recommended_verification: tuple[str, ...] = ()
    trace: tuple[ChangeStageTrace, ...] = ()
    initial_risk_assessment: RiskAssessment | None = None
    verification_plan: VerificationPlan | None = None
    verification_result: VerificationResult | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        state = value.get("investigation_state")
        if isinstance(state, dict):
            for call in state.get("tool_calls", []):
                if not isinstance(call, dict) or call.get("result") is None:
                    continue
                serialized = json.dumps(
                    call["result"], default=str, separators=(",", ":")
                )
                if len(serialized) > 2048:
                    call["result"] = {
                        "truncated": True,
                        "preview": serialized[:2048],
                    }
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ChangeGuardAnalysis:
        """Rehydrate a persisted analysis using the same JSON shape as ``to_dict``."""
        from changeguard.analysis.serialization import analysis_from_dict

        return analysis_from_dict(value)
