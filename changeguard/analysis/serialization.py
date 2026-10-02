from __future__ import annotations

from typing import Any

from changeguard.analysis.models import BlastRadiusResult, ChangeGuardAnalysis, ChangeStageTrace
from changeguard.code_intelligence.api import (
    ApiAnalysisMetrics,
    ApiConsumer,
    ApiDependencyEdge,
    ApiEndpoint,
    ApiImpact,
    ApiRelationshipIssue,
)
from changeguard.code_intelligence.java import ChangedLineRange, JavaAnalysis, JavaCallEdge, JavaSymbol
from changeguard.evidence.models import EvidenceBundle, RetrievedEvidence
from changeguard.llm.decision import InvestigationState, InvestigationToolCall, ToolSelectionError
from changeguard.llm.report import (
    InvestigationEvidenceReference,
    InvestigationFinding,
    InvestigationReport,
)
from changeguard.models.schemas import CommitMetadata, RepoStatus
from changeguard.risk.models import (
    BlastRadiusSignals,
    EvidenceReference,
    RiskAssessment,
    RiskFactor,
    RuntimeSignals,
    TestSignals,
)
from changeguard.verification.models import (
    verification_plan_from_dict,
    verification_result_from_dict,
)


def _risk_from_dict(risk_value: dict[str, Any]) -> RiskAssessment:
    return RiskAssessment(
        overall_level=risk_value["overall_level"],
        score=risk_value["score"],
        factors=tuple(
            RiskFactor(
                **{
                    **item,
                    "evidence_refs": tuple(item.get("evidence_refs", ())),
                }
            )
            for item in risk_value["factors"]
        ),
        evidence=tuple(EvidenceReference(**item) for item in risk_value["evidence"]),
        uncertainties=tuple(risk_value["uncertainties"]),
        recommendations=tuple(risk_value["recommendations"]),
        metrics=risk_value["metrics"],
    )


def _api_impact_from_dict(value: dict[str, Any] | None) -> ApiImpact | None:
    if value is None:
        return None
    return ApiImpact(
        source_revision=value["source_revision"],
        endpoints=tuple(
            ApiEndpoint(
                **{
                    **item,
                    "mapping_source_lines": tuple(item.get("mapping_source_lines", ())),
                }
            )
            for item in value.get("endpoints", ())
        ),
        consumers=tuple(ApiConsumer(**item) for item in value.get("consumers", ())),
        edges=tuple(
            ApiDependencyEdge(
                **{**item, "evidence_refs": tuple(item.get("evidence_refs", ()))}
            )
            for item in value.get("edges", ())
        ),
        ambiguous_relationships=tuple(
            ApiRelationshipIssue(
                **{
                    **item,
                    "candidate_endpoint_ids": tuple(item.get("candidate_endpoint_ids", ())),
                    "evidence_refs": tuple(item.get("evidence_refs", ())),
                }
            )
            for item in value.get("ambiguous_relationships", ())
        ),
        unresolved_relationships=tuple(
            ApiRelationshipIssue(
                **{
                    **item,
                    "candidate_endpoint_ids": tuple(item.get("candidate_endpoint_ids", ())),
                    "evidence_refs": tuple(item.get("evidence_refs", ())),
                }
            )
            for item in value.get("unresolved_relationships", ())
        ),
        changed_endpoint_ids=tuple(value.get("changed_endpoint_ids", ())),
        changed_consumer_ids=tuple(value.get("changed_consumer_ids", ())),
        impacted_consumer_ids=tuple(value.get("impacted_consumer_ids", ())),
        providers_for_changed_consumers=tuple(
            value.get("providers_for_changed_consumers", ())
        ),
        metrics=ApiAnalysisMetrics(**value.get("metrics", {})),
        uncertainties=tuple(value.get("uncertainties", ())),
    )


def analysis_from_dict(value: dict[str, Any]) -> ChangeGuardAnalysis:
    code_value = value.get("code_analysis")
    code_analysis = None
    if isinstance(code_value, dict):
        code_analysis = JavaAnalysis(
            symbols=tuple(JavaSymbol(**item) for item in code_value["symbols"]),
            call_edges=tuple(JavaCallEdge(**item) for item in code_value["call_edges"]),
            changed_symbol_ids=tuple(code_value["changed_symbol_ids"]),
            file_level_changes=tuple(code_value["file_level_changes"]),
            direct_dependents=tuple(code_value["direct_dependents"]),
            indirect_dependents=tuple(code_value["indirect_dependents"]),
            affected_files=tuple(code_value["affected_files"]),
            affected_packages=tuple(code_value["affected_packages"]),
            maximum_dependency_depth=code_value["maximum_dependency_depth"],
            unresolved_relationships=code_value["unresolved_relationships"],
            unresolved_calls=tuple(JavaCallEdge(**item) for item in code_value["unresolved_calls"]),
            parse_error_files=tuple(code_value["parse_error_files"]),
            omitted_sources=code_value["omitted_sources"],
            uncertainties=tuple(code_value["uncertainties"]),
            phase_durations_ms=code_value["phase_durations_ms"],
        )

    blast_value = value.get("blast_radius")
    blast_radius = None
    if isinstance(blast_value, dict):
        blast_radius = BlastRadiusResult(
            signals=BlastRadiusSignals(
                **{
                    **blast_value["signals"],
                    "evidence_refs": tuple(
                        blast_value["signals"].get("evidence_refs", ())
                    ),
                }
            ),
            direct_dependents=tuple(blast_value["direct_dependents"]),
            indirect_dependents=tuple(blast_value["indirect_dependents"]),
            affected_files=tuple(blast_value["affected_files"]),
            affected_packages=tuple(blast_value["affected_packages"]),
            maximum_dependency_depth=blast_value["maximum_dependency_depth"],
            unresolved_relationships=blast_value["unresolved_relationships"],
            uncertainties=tuple(blast_value["uncertainties"]),
            cross_language_api_consumers=tuple(
                blast_value.get("cross_language_api_consumers", ())
            ),
            api_edges=tuple(
                ApiDependencyEdge(
                    **{**item, "evidence_refs": tuple(item.get("evidence_refs", ()))}
                )
                for item in blast_value.get("api_edges", ())
            ),
            api_unresolved_relationships=blast_value.get(
                "api_unresolved_relationships", 0
            ),
            api_ambiguous_relationships=blast_value.get(
                "api_ambiguous_relationships", 0
            ),
            api_provider_symbols_for_changed_consumers=tuple(
                blast_value.get("api_provider_symbols_for_changed_consumers", ())
            ),
        )

    history_value = value.get("historical_evidence")
    history = None
    if isinstance(history_value, dict):
        history = EvidenceBundle(
            query=history_value["query"],
            results=[RetrievedEvidence(**item) for item in history_value["results"]],
            retrieval_timestamp=history_value["retrieval_timestamp"],
            retrieval_method=history_value["retrieval_method"],
            top_k=history_value["top_k"],
            filters=history_value["filters"],
            status=history_value["status"],
        )

    report_value = value.get("investigation_report")
    report = None
    if isinstance(report_value, dict):
        report = InvestigationReport(
            summary=report_value["summary"],
            summary_evidence_refs=list(report_value["summary_evidence_refs"]),
            findings=[InvestigationFinding(**item) for item in report_value["findings"]],
            evidence=[
                InvestigationEvidenceReference(**item)
                for item in report_value["evidence"]
            ],
            uncertainties=list(report_value["uncertainties"]),
            recommended_verification=list(report_value["recommended_verification"]),
            tool_trace=list(report_value["tool_trace"]),
            verification=report_value.get("verification"),
            evaluation=report_value.get("evaluation"),
            api_impact=report_value.get("api_impact"),
        )

    api_impact = _api_impact_from_dict(value.get("api_impact"))

    state_value = value.get("investigation_state")
    investigation_state = None
    if isinstance(state_value, dict):
        error_value = state_value.get("error")
        investigation_state = InvestigationState(
            task=state_value["task"],
            tool_calls=[
                InvestigationToolCall(**item) for item in state_value["tool_calls"]
            ],
            status=state_value["status"],
            final_answer=state_value.get("final_answer"),
            error=ToolSelectionError(**error_value) if error_value else None,
        )

    risk_assessment = _risk_from_dict(value["risk_assessment"])
    initial_risk_value = value.get("initial_risk_assessment")
    initial_risk = (
        _risk_from_dict(initial_risk_value)
        if isinstance(initial_risk_value, dict)
        else None
    )
    plan_value = value.get("verification_plan")
    verification_plan = (
        verification_plan_from_dict(plan_value)
        if isinstance(plan_value, dict)
        else None
    )
    result_value = value.get("verification_result")
    verification_result = (
        verification_result_from_dict(result_value)
        if isinstance(result_value, dict)
        else None
    )

    return ChangeGuardAnalysis(
        analysis_id=value["analysis_id"],
        repository=value["repository"],
        change_reference=value["change_reference"],
        change_summary=value["change_summary"],
        repository_status=RepoStatus(**value["repository_status"]),
        changed_files=tuple(value["changed_files"]),
        changed_lines=tuple(ChangedLineRange(**item) for item in value["changed_lines"]),
        changed_symbols=tuple(JavaSymbol(**item) for item in value["changed_symbols"]),
        file_level_changes=tuple(value["file_level_changes"]),
        recent_commits=tuple(CommitMetadata(**item) for item in value["recent_commits"]),
        code_analysis=code_analysis,
        blast_radius=blast_radius,
        historical_evidence=history,
        test_signals=TestSignals(**value["test_signals"]),
        runtime_signals=RuntimeSignals(**value["runtime_signals"]),
        risk_assessment=risk_assessment,
        investigation_report=report,
        api_impact=api_impact,
        investigation_state=investigation_state,
        investigation_error=value.get("investigation_error"),
        uncertainties=tuple(value["uncertainties"]),
        recommended_verification=tuple(value["recommended_verification"]),
        trace=tuple(
            ChangeStageTrace(
                stage=item["stage"],
                status=item["status"],
                duration_ms=item["duration_ms"],
                failure=item.get("failure"),
                counters=item.get("counters", {}),
            )
            for item in value["trace"]
        ),
        initial_risk_assessment=initial_risk,
        verification_plan=verification_plan,
        verification_result=verification_result,
    )
