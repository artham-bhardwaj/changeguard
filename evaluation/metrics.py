from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

from changeguard.analysis.models import ChangeGuardAnalysis
from changeguard.evidence.models import EvidenceBundle
from changeguard.llm.decision import InvestigationState
from changeguard.llm.report import InvestigationReport
from changeguard.verification.models import VerificationResult
from evaluation.models import EvaluationCase, dumps_canonical


def recall_at_k(bundle: EvidenceBundle, relevant: list[str], k: int) -> float:
    return float(bool(set(relevant) & {item.evidence_id for item in bundle.results[:k]}))


def mrr(bundle: EvidenceBundle, relevant: list[str]) -> float:
    wanted = set(relevant)
    for index, item in enumerate(bundle.results, 1):
        if item.evidence_id in wanted:
            return 1 / index
    return 0.0


def _classification(predicted: Iterable[str], expected: Iterable[str]) -> dict[str, float | int]:
    raw_predictions = list(predicted)
    expected_set, predicted_set = set(expected), set(raw_predictions)
    tp = len(predicted_set & expected_set)
    fp, fn = len(predicted_set - expected_set), len(expected_set - predicted_set)
    precision = tp / len(predicted_set) if predicted_set else float(not expected_set)
    recall = tp / len(expected_set) if expected_set else float(not predicted_set)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision, "recall": recall, "f1": f1,
        "true_positive": tp, "false_positive": fp, "false_negative": fn,
        "duplicate_predictions": len(raw_predictions) - len(predicted_set),
        "expected_count": len(expected_set), "predicted_count": len(predicted_set),
    }
def retrieval_metrics(
    bundle: EvidenceBundle | None, expected: tuple[str, ...] | None
) -> dict[str, Any]:
    if expected is None:
        return {"state": "not_evaluated", "precision": None, "recall": None, "hit_rate": None}
    measured = _classification(
        [] if bundle is None else [item.evidence_id for item in bundle.results],
        expected,
    )
    measured.update(state="evaluated", hit_rate=float(measured["true_positive"] > 0))
    return measured


def verification_metrics(result: VerificationResult | None, plan=None) -> dict[str, Any]:
    checks = [] if result is None else list(result.checks)
    return {
        "planned": len(checks) if plan is None else len(plan.checks),
        "executed": len(checks),
        "passed": sum(item.status == "PASSED" for item in checks),
        "failed": sum(item.status == "FAILED" for item in checks),
        "timed_out": sum(item.status == "TIMED_OUT" for item in checks),
        "blocked": sum(item.status == "BLOCKED" for item in checks),
        "error": sum(item.status == "ERROR" for item in checks),
        "status": "NOT_RUN" if result is None else result.status,
        "missing_is_unknown": result is None,
    }


def _produced_references(
    report: InvestigationReport | None,
    state: InvestigationState | None,
    history: EvidenceBundle | None,
) -> set[str]:
    refs: set[str] = set()
    if history:
        refs.update(item.evidence_id for item in history.results)
    if state:
        for call in state.tool_calls:
            if call.call_id:
                refs.add(call.call_id)
            stack: list[Any] = [call.result] if call.result is not None else []
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key in {"evidence_id", "reference"} and isinstance(item, str):
                            refs.add(item)
                        elif key in {"evidence_ids", "references"} and isinstance(item, list):
                            refs.update(entry for entry in item if isinstance(entry, str))
                        else:
                            stack.append(item)
                elif isinstance(value, list):
                    stack.extend(value)
    return refs


def grounding_metrics(
    report: InvestigationReport | None,
    state: InvestigationState | None,
    history: EvidenceBundle | None,
) -> dict[str, Any]:
    findings = [] if report is None else report.findings
    known = _produced_references(report, state, history)
    grounded = unsupported = invalid = duplicates = citations_total = 0
    for finding in findings:
        citations = finding.evidence_refs
        citations_total += len(citations)
        duplicates += len(citations) - len(set(citations))
        invalid += sum(reference not in known for reference in citations)
        if citations and all(reference in known for reference in citations):
            grounded += 1
        else:
            unsupported += 1
    return {
        "total_findings": len(findings),
        "grounded_findings": grounded,
        "grounded_finding_rate": grounded / len(findings) if findings else None,
        "unsupported_claim_count": unsupported,
        "invalid_citation_count": invalid,
        "duplicate_citation_count": duplicates,
        "citation_count": citations_total,
        "available_reference_count": len(known),
    }


def agent_metrics(
    state: InvestigationState | None, analysis: ChangeGuardAnalysis | None = None
) -> dict[str, Any]:
    calls = [] if state is None else state.tool_calls
    keys = [(call.tool_name, dumps_canonical(call.arguments)) for call in calls]
    failed = sum(call.error is not None for call in calls)
    rejected = int(bool(
        state and state.error
        and state.error.code in {"unknown_tool", "invalid_arguments", "invalid_decision"}
    ))
    result_sizes = [
        len(dumps_canonical(call.result).encode("utf-8"))
        for call in calls if call.result is not None
    ]
    return {
        "total_tool_calls": len(calls),
        "successful_tool_calls": sum(call.error is None and call.result is not None for call in calls),
        "rejected_tool_calls": rejected,
        "failed_tool_calls": failed,
        "duplicate_tool_calls": len(keys) - len(set(keys)),
        "unnecessary_tool_calls": (len(keys) - len(set(keys))) + rejected,
        "completion_reason": "not_run" if state is None else state.status,
        "budget_exhausted": bool(
            state and state.status in {"budget_exhausted", "max_tool_calls_reached"}
        ),
        "average_tool_result_bytes": sum(result_sizes) / len(result_sizes) if result_sizes else 0.0,
        "total_investigation_latency_ms": (
            sum(
                stage.duration_ms for stage in analysis.trace
                if stage.stage == "llm_investigation"
            )
            if analysis is not None else None
        ),
    }


def _get_path(value: Any, path: str) -> Any:
    for part in path.split("."):
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get(part)
        elif part.isdecimal() and isinstance(value, (tuple, list)):
            index = int(part)
            value = value[index] if index < len(value) else None
        else:
            value = getattr(value, part, None)
    return value


def risk_metrics(analysis: ChangeGuardAnalysis, expected: dict[str, Any] | None) -> dict[str, Any]:
    risk = analysis.risk_assessment
    active = [
        factor for factor in risk.factors
        if factor.contribution is not None and factor.coverage > 0 and factor.weight > 0
    ]
    active_weight = sum(factor.weight * factor.coverage for factor in active)
    implied_score = (
        round(sum(factor.contribution or 0.0 for factor in active) / active_weight * 100, 2)
        if active_weight else None
    )
    expected_checks = {
        path: _get_path(analysis, path) == wanted
        for path, wanted in (expected or {}).items()
    }
    risk_projection = {
        "score": risk.score,
        "level": risk.overall_level,
        "factors": [
            {
                "factor": factor.factor, "value": factor.value,
                "normalized_value": factor.normalized_value, "weight": factor.weight,
                "contribution": factor.contribution, "coverage": factor.coverage,
                "state": factor.state,
            }
            for factor in risk.factors
        ],
    }
    thresholds = (25.0, 50.0, 75.0)
    threshold_level = (
        "UNKNOWN" if risk.score is None
        else "CRITICAL" if risk.score >= thresholds[2]
        else "HIGH" if risk.score >= thresholds[1]
        else "MEDIUM" if risk.score >= thresholds[0]
        else "LOW"
    )
    factor_references = {
        reference for factor in risk.factors for reference in factor.evidence_refs
    }
    pytest_checks = (
        [
            item for item in analysis.verification_result.checks
            if item.check.operation == "PYTHON_PYTEST"
        ]
        if analysis.verification_result else []
    )
    observed_test_counts = any(
        re.search(rf"\b\d+\s+{word}\b", item.output_summary)
        for item in pytest_checks
        for word in ("passed", "failed", "error", "skipped")
    )
    unknown_test_signal_preserved = (
        (analysis.test_signals.state == "unknown") == (not observed_test_counts)
    )
    return {
        "score": risk.score,
        "level": risk.overall_level,
        "score_consistent_with_contributions": (
            risk.score == implied_score if risk.score is not None else implied_score is None
        ),
        "threshold_consistent": risk.overall_level == threshold_level,
        "factor_reproducibility_fingerprint": hashlib.sha256(
            dumps_canonical(risk_projection["factors"]).encode()
        ).hexdigest(),
        "evidence_reference_validity": all(
            item.reference in factor_references for item in risk.evidence
        ),
        "contribution_sum": round(sum(factor.contribution or 0.0 for factor in active), 6),
        "known_factor_count": sum(factor.state == "known" for factor in risk.factors),
        "unknown_factor_count": sum(factor.state == "unknown" for factor in risk.factors),
        "test_signal_state": analysis.test_signals.state,
        "unknown_test_signal_preserved": unknown_test_signal_preserved,
        "unknown_runtime_signal_preserved": (
            analysis.runtime_signals.state == "unknown"
            and any(
                factor.factor == "runtime_signals" and factor.state == "unknown"
                for factor in risk.factors
            )
        ),
        "expected_properties": expected_checks,
        "expected_properties_passed": (
            all(expected_checks.values()) if expected_checks else None
        ),
        "initial_to_verified_delta": (
            round(risk.score - analysis.initial_risk_assessment.score, 2)
            if analysis.initial_risk_assessment
            and risk.score is not None
            and analysis.initial_risk_assessment.score is not None
            else None
        ),
        "risk_fingerprint": hashlib.sha256(dumps_canonical(risk_projection).encode()).hexdigest(),
    }


def evaluate_metrics(
    case: EvaluationCase, analysis: ChangeGuardAnalysis, *, verification_requested: bool = False
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    def optional_classification(actual: Iterable[str], expected: tuple[str, ...] | None):
        return None if expected is None else _classification(actual, expected)

    code = analysis.code_analysis
    api = analysis.api_impact
    api_consumers = {} if api is None else {
        item.consumer_id: item for item in api.consumers
    }
    endpoint_actual = (
        []
        if api is None
        else [
            f"{item.http_method} {item.route}|{item.declaring_method}"
            for item in api.endpoints
        ]
    )
    consumer_actual = (
        []
        if api is None
        else [
            f"{item.source_file}|{item.http_method} "
            f"{item.route if item.route is not None else 'DYNAMIC'}"
            for item in api.consumers
        ]
    )
    edge_actual = (
        []
        if api is None
        else [
            f"{item.endpoint_identity}|"
            f"{api_consumers[item.target_node].source_file}|"
            f"{api_consumers[item.target_node].route or 'DYNAMIC'}"
            for item in api.edges
            if item.target_node in api_consumers
        ]
    )
    impacted_consumer_actual = (
        []
        if api is None
        else [
            f"{item.source_file}|{item.http_method} "
            f"{item.route if item.route is not None else 'DYNAMIC'}"
            for item in api.consumers
            if item.consumer_id in api.impacted_consumer_ids
        ]
    )
    provider_actual = (
        []
        if api is None
        else list(api.providers_for_changed_consumers)
    )
    api_metrics = {
        "endpoints": optional_classification(
            endpoint_actual, case.expected_api_endpoints
        ),
        "consumers": optional_classification(
            consumer_actual, case.expected_api_consumers
        ),
        "api_consumer_edges": optional_classification(
            edge_actual, case.expected_api_edges
        ),
        "impacted_consumers": optional_classification(
            impacted_consumer_actual, case.expected_api_impacted_consumers
        ),
        "providers_for_changed_consumers": optional_classification(
            provider_actual, case.expected_api_providers_for_changed_consumers
        ),
        "ambiguous_match_count": 0 if api is None else len(api.ambiguous_relationships),
        "unresolved_consumer_count": 0 if api is None else len(api.unresolved_relationships),
        "dynamic_url_count": (
            0 if api is None else sum(item.route is None for item in api.consumers)
        ),
        "false_api_consumer_count": (
            None
            if case.expected_api_edges is None
            else _classification(edge_actual, case.expected_api_edges)["false_positive"]
        ),
        "expected_ambiguous_matches_match": (
            None
            if case.expected_ambiguous_matches is None
            else (
                0 if api is None else len(api.ambiguous_relationships)
            ) == case.expected_ambiguous_matches
        ),
        "expected_unresolved_consumers_match": (
            None
            if case.expected_unresolved_consumers is None
            else (
                0 if api is None else len(api.unresolved_relationships)
            ) == case.expected_unresolved_consumers
        ),
        "expected_dynamic_url_count_match": (
            None
            if case.expected_dynamic_url_count is None
            else (
                0 if api is None else sum(item.route is None for item in api.consumers)
            ) == case.expected_dynamic_url_count
        ),
    }
    deterministic = {
        "changed_files": optional_classification(analysis.changed_files, case.changed_files),
        "changed_symbols": optional_classification(
            (item.qualified_name for item in analysis.changed_symbols), case.changed_symbols
        ),
        "impacted_symbols": optional_classification(
            (
                analysis.blast_radius.direct_dependents + analysis.blast_radius.indirect_dependents
                if analysis.blast_radius else ()
            ),
            case.expected_impacted_symbols,
        ),
        "api_impact": api_metrics,
        "historical_evidence": retrieval_metrics(analysis.historical_evidence, case.expected_evidence_ids),
        "unresolved_dependency_count": 0 if code is None else code.unresolved_relationships,
        "reported_uncertainty_count": len(analysis.uncertainties),
        "unsupported_analysis_count": sum(item.status == "unavailable" for item in analysis.trace),
        "unmapped_change_count": len(analysis.file_level_changes),
        "uncertainty_state": "reported" if analysis.uncertainties else "none",
        "expected_unresolved_dependencies_match": (
            None if case.expected_unresolved_dependencies is None
            else (0 if code is None else code.unresolved_relationships)
            == case.expected_unresolved_dependencies
        ),
        "expected_unmapped_changes_match": (
            None if case.expected_unmapped_changes is None
            else len(analysis.file_level_changes) == case.expected_unmapped_changes
        ),
        "expected_uncertainty_match": (
            None if case.expected_uncertainty is None
            else bool(analysis.uncertainties) == case.expected_uncertainty
        ),
        "uncertainty_correctness": (
            "not_evaluated" if case.expected_uncertainty is None
            else "correctly_uncertain"
            if case.expected_uncertainty and bool(analysis.uncertainties)
            else "uncertainty_expected_but_missing"
            if case.expected_uncertainty
            else "unexpected_uncertainty"
            if analysis.uncertainties
            else "no_expected_uncertainty"
        ),
    }
    evidence = retrieval_metrics(analysis.historical_evidence, case.expected_evidence_ids)
    verification = verification_metrics(analysis.verification_result, analysis.verification_plan)
    agent = agent_metrics(analysis.investigation_state, analysis)
    grounding = grounding_metrics(
        analysis.investigation_report, analysis.investigation_state, analysis.historical_evidence
    )
    risk = risk_metrics(
        analysis,
        case.expected_risk_properties
        if verification_requested or not case.expected_verification_properties
        else None,
    )
    failures: list[str] = []
    for label, values in (
        ("changed_files", deterministic["changed_files"]),
        ("changed_symbols", deterministic["changed_symbols"]),
        ("impacted_symbols", deterministic["impacted_symbols"]),
    ):
        if values is not None and (values["false_positive"] or values["false_negative"]):
            failures.append(f"{label} differ from authored ground truth")
    for label in (
        "endpoints",
        "consumers",
        "api_consumer_edges",
        "impacted_consumers",
        "providers_for_changed_consumers",
    ):
        values = api_metrics[label]
        if values is not None and (values["false_positive"] or values["false_negative"]):
            failures.append(f"API {label} differ from authored ground truth")
    for key in (
        "expected_ambiguous_matches_match",
        "expected_unresolved_consumers_match",
        "expected_dynamic_url_count_match",
    ):
        if api_metrics[key] is False:
            failures.append(f"API impact {key.removeprefix('expected_').removesuffix('_match')} differs from authored ground truth")
    if case.expected_evidence_ids is not None and (
        evidence["false_positive"] or evidence["false_negative"]
    ):
        failures.append("historical evidence IDs differ from authored ground truth")
    if deterministic["expected_unresolved_dependencies_match"] is False:
        failures.append("unresolved relationship count differs from authored ground truth")
    if deterministic["expected_unmapped_changes_match"] is False:
        failures.append("unmapped change count differs from authored ground truth")
    if deterministic["expected_uncertainty_match"] is False:
        failures.append("uncertainty presence differs from authored ground truth")
    if (
        case.expected_risk_properties is not None
        and (verification_requested or not case.expected_verification_properties)
        and risk["expected_properties_passed"] is False
    ):
        failures.append("expected risk properties were not satisfied")
    for metric in (
        "score_consistent_with_contributions",
        "threshold_consistent",
        "evidence_reference_validity",
        "unknown_test_signal_preserved",
        "unknown_runtime_signal_preserved",
    ):
        if risk[metric] is False:
            failures.append(f"deterministic risk invariant failed: {metric}")
    if verification_requested:
        for path, wanted in (case.expected_verification_properties or {}).items():
            if _get_path(analysis, path) != wanted:
                failures.append(f"expected verification property {path} was not satisfied")
    return deterministic, grounding, verification, agent, risk, failures


def deterministic_fingerprint(analysis: ChangeGuardAnalysis) -> str:
    code = analysis.code_analysis
    projection = {
        "change_summary": analysis.change_summary,
        "changed_files": sorted(analysis.changed_files),
        "changed_symbols": sorted(item.qualified_name for item in analysis.changed_symbols),
        "file_level_changes": sorted(analysis.file_level_changes),
        "direct_dependents": sorted(analysis.blast_radius.direct_dependents) if analysis.blast_radius else [],
        "indirect_dependents": sorted(analysis.blast_radius.indirect_dependents) if analysis.blast_radius else [],
        "unresolved_relationships": 0 if code is None else code.unresolved_relationships,
        "api_impact": (
            {
                "endpoints": sorted(
                    f"{item.http_method} {item.route}|{item.declaring_method}"
                    for item in analysis.api_impact.endpoints
                ),
                "consumers": sorted(
                    f"{item.source_file}|{item.http_method}|"
                    f"{item.route or 'DYNAMIC'}|{item.source_line}"
                    for item in analysis.api_impact.consumers
                ),
                "edges": sorted(
                    f"{item.relationship_type}|{item.endpoint_identity}|"
                    f"{item.source_node}|{item.target_node}"
                    for item in analysis.api_impact.edges
                ),
                "ambiguous": sorted(
                    (item.consumer_id, item.candidate_endpoint_ids)
                    for item in analysis.api_impact.ambiguous_relationships
                ),
                "unresolved": sorted(
                    (item.consumer_id, item.reason, item.candidate_endpoint_ids)
                    for item in analysis.api_impact.unresolved_relationships
                ),
                "changed_endpoints": sorted(analysis.api_impact.changed_endpoint_ids),
                "changed_consumers": sorted(analysis.api_impact.changed_consumer_ids),
                "impacted_consumers": sorted(analysis.api_impact.impacted_consumer_ids),
                "providers_for_changed_consumers": sorted(
                    analysis.api_impact.providers_for_changed_consumers
                ),
            }
            if analysis.api_impact else None
        ),
        "historical_evidence_ids": (
            sorted(item.evidence_id for item in analysis.historical_evidence.results)
            if analysis.historical_evidence else []
        ),
        "risk": {
            "score": analysis.risk_assessment.score,
            "level": analysis.risk_assessment.overall_level,
            "factors": [
                {
                    "factor": factor.factor, "value": factor.value,
                    "normalized_value": factor.normalized_value, "weight": factor.weight,
                    "contribution": factor.contribution, "coverage": factor.coverage,
                    "state": factor.state,
                }
                for factor in analysis.risk_assessment.factors
            ],
        },
        "verification": (
            {
                "status": analysis.verification_result.status,
                "checks": [
                    {"operation": item.check.operation, "status": item.status}
                    for item in analysis.verification_result.checks
                ],
            }
            if analysis.verification_result else None
        ),
    }
    return hashlib.sha256(dumps_canonical(projection).encode("utf-8")).hexdigest()


def aggregate_results(
    results: list[Any], evaluation_run_id: str, reproducibility_failures: int = 0
) -> dict[str, Any]:
    numeric: dict[str, list[float]] = {}
    classifications: dict[str, list[dict[str, Any]]] = {}

    def collect_numeric(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for child, item in value.items():
                collect_numeric(f"{prefix}.{child}", item)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric.setdefault(prefix, []).append(float(value))

    for result in results:
        for section, metrics in (
            ("deterministic", result.deterministic_metrics),
            ("grounding", result.grounding_metrics),
            ("verification", result.verification_metrics),
            ("agent", result.agent_metrics),
            ("risk", result.risk_metrics),
        ):
            collect_numeric(section, metrics)
        def collect_classification(prefix: str, value: Any) -> None:
            if not isinstance(value, dict):
                return
            if {"true_positive", "false_positive", "false_negative"} <= value.keys():
                classifications.setdefault(prefix, []).append(value)
                return
            for child, item in value.items():
                collect_classification(f"{prefix}.{child}", item)

        for key, value in result.deterministic_metrics.items():
            collect_classification(key, value)
    aggregate_classification = {}
    for key, items in classifications.items():
        tp = sum(item["true_positive"] for item in items)
        fp = sum(item["false_positive"] for item in items)
        fn = sum(item["false_negative"] for item in items)
        precision = tp / (tp + fp) if tp + fp else float(fn == 0)
        recall = tp / (tp + fn) if tp + fn else float(fp == 0)
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        aggregate_classification[key] = {
            "precision": precision, "recall": recall, "f1": f1,
            "true_positive": tp, "false_positive": fp, "false_negative": fn,
        }
    return {
        "evaluation_run_id": evaluation_run_id,
        "total_cases": len(results),
        "passed_cases": sum(item.status == "passed" for item in results),
        "failed_cases": sum(item.status == "failed" for item in results),
        "skipped_cases": sum(item.status == "skipped" for item in results),
        "metric_averages": {key: sum(values) / len(values) for key, values in numeric.items() if values},
        "metric_minimums": {key: min(values) for key, values in numeric.items() if values},
        "metric_maximums": {key: max(values) for key, values in numeric.items() if values},
        "aggregate_classification": aggregate_classification,
        "total_tool_calls": sum(item.agent_metrics["total_tool_calls"] for item in results),
        "total_verification_checks": sum(item.verification_metrics["executed"] for item in results),
        "total_failures": sum(len(item.failures) for item in results),
        "reproducibility_failures": reproducibility_failures,
        "results": [item.to_dict() for item in results],
        "schema_version": 1,
    }
