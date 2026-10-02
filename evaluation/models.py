from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    repository_fixture: str
    base_revision: str = "baseline"
    target_revision: str = "target"
    changed_files: tuple[str, ...] | None = None
    changed_symbols: tuple[str, ...] | None = None
    expected_impacted_symbols: tuple[str, ...] | None = None
    expected_api_endpoints: tuple[str, ...] | None = None
    expected_api_consumers: tuple[str, ...] | None = None
    expected_api_edges: tuple[str, ...] | None = None
    expected_api_impacted_consumers: tuple[str, ...] | None = None
    expected_api_providers_for_changed_consumers: tuple[str, ...] | None = None
    expected_ambiguous_matches: int | None = None
    expected_unresolved_consumers: int | None = None
    expected_dynamic_url_count: int | None = None
    expected_evidence_ids: tuple[str, ...] | None = None
    expected_unresolved_dependencies: int | None = None
    expected_unmapped_changes: int | None = None
    expected_uncertainty: bool | None = None
    expected_risk_properties: dict[str, Any] | None = None
    expected_verification_properties: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id or not self.case_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("case_id must be a non-empty identifier")
        if not isinstance(self.repository_fixture, str) or not self.repository_fixture:
            raise ValueError("repository_fixture is required")
        if not isinstance(self.base_revision, str) or not isinstance(self.target_revision, str):
            raise ValueError("base_revision and target_revision must be strings")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError(f"Unsupported evaluation case schema version: {self.schema_version}")
        for field_name in (
            "changed_files",
            "changed_symbols",
            "expected_impacted_symbols",
            "expected_api_endpoints",
            "expected_api_consumers",
            "expected_api_edges",
            "expected_api_impacted_consumers",
            "expected_api_providers_for_changed_consumers",
            "expected_evidence_ids",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, tuple) or any(not isinstance(item, str) for item in value)
            ):
                raise ValueError(f"{field_name} must be a tuple of strings or None")
        for field_name in ("expected_risk_properties", "expected_verification_properties", "metadata"):
            if not isinstance(getattr(self, field_name), (dict, type(None))):
                raise ValueError(f"{field_name} must be an object")
        for field_name in (
            "expected_unresolved_dependencies",
            "expected_unmapped_changes",
            "expected_ambiguous_matches",
            "expected_unresolved_consumers",
            "expected_dynamic_url_count",
        ):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{field_name} must be a non-negative integer or None")
        if self.expected_uncertainty is not None and type(self.expected_uncertainty) is not bool:
            raise ValueError("expected_uncertainty must be a boolean or None")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvaluationCase:
        if not isinstance(value, dict):
            raise ValueError("Evaluation case must be a JSON object")
        required = {"case_id", "repository_fixture"}
        missing = required - value.keys()
        if missing:
            raise ValueError(f"Evaluation case is missing fields: {', '.join(sorted(missing))}")
        allowed = {item.name for item in fields(cls)}
        unknown = value.keys() - allowed
        if unknown:
            raise ValueError(f"Unknown evaluation case fields: {', '.join(sorted(unknown))}")
        normalized = dict(value)
        for field_name in (
            "changed_files",
            "changed_symbols",
            "expected_impacted_symbols",
            "expected_api_endpoints",
            "expected_api_consumers",
            "expected_api_edges",
            "expected_api_impacted_consumers",
            "expected_api_providers_for_changed_consumers",
            "expected_evidence_ids",
        ):
            if normalized.get(field_name) is not None:
                raw = normalized[field_name]
                if not isinstance(raw, (list, tuple)):
                    raise ValueError(f"{field_name} must be a JSON array or null")
                normalized[field_name] = tuple(raw)
        return cls(**normalized)


@dataclass(frozen=True)
class EvaluationRunTrace:
    run_id: str
    analysis_id: str
    case_id: str | None
    stages: tuple[dict[str, Any], ...]
    counters: dict[str, int | float | str]
    tool_call_summary: dict[str, int | float | str]
    verification_summary: dict[str, int | float | str]
    risk_summary: dict[str, int | float | str | None]
    final_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvaluationResult:
    evaluation_run_id: str
    case_id: str
    deterministic_metrics: dict[str, Any]
    grounding_metrics: dict[str, Any]
    verification_metrics: dict[str, Any]
    agent_metrics: dict[str, Any]
    risk_metrics: dict[str, Any]
    fingerprint: str
    status: str
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    execution_metadata: dict[str, Any] = field(default_factory=dict)
    run_trace: EvaluationRunTrace | None = None
    investigation_report: dict[str, Any] | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.evaluation_run_id or not self.case_id:
            raise ValueError("evaluation_run_id and case_id are required")
        if self.status not in {"passed", "failed", "skipped"}:
            raise ValueError("evaluation result status must be passed, failed, or skipped")
        if len(self.fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.fingerprint
        ):
            raise ValueError("fingerprint must be a lowercase SHA-256 hex digest")
        for name in (
            "deterministic_metrics",
            "grounding_metrics",
            "verification_metrics",
            "agent_metrics",
            "risk_metrics",
            "execution_metadata",
        ):
            if not isinstance(getattr(self, name), dict):
                raise ValueError(f"{name} must be an object")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported evaluation result schema version")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvaluationResult:
        if value.get("schema_version", 1) != 1:
            raise ValueError("Unsupported evaluation result schema version")
        trace_value = value.get("run_trace")
        trace = EvaluationRunTrace(**{
            **trace_value,
            "stages": tuple(trace_value.get("stages", ())),
        }) if isinstance(trace_value, dict) else None
        return cls(
            evaluation_run_id=str(value["evaluation_run_id"]),
            case_id=str(value["case_id"]),
            deterministic_metrics=dict(value["deterministic_metrics"]),
            grounding_metrics=dict(value["grounding_metrics"]),
            verification_metrics=dict(value["verification_metrics"]),
            agent_metrics=dict(value["agent_metrics"]),
            risk_metrics=dict(value["risk_metrics"]),
            fingerprint=str(value["fingerprint"]),
            status=str(value["status"]),
            failures=tuple(value.get("failures", ())),
            warnings=tuple(value.get("warnings", ())),
            execution_metadata=dict(value.get("execution_metadata", {})),
            run_trace=trace,
            investigation_report=(
                dict(value["investigation_report"])
                if isinstance(value.get("investigation_report"), dict) else None
            ),
            schema_version=1,
        )


@dataclass(frozen=True)
class AggregateEvaluationReport:
    evaluation_run_id: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    skipped_cases: int
    metric_averages: dict[str, float]
    metric_minimums: dict[str, float]
    metric_maximums: dict[str, float]
    aggregate_classification: dict[str, dict[str, float | int]]
    total_tool_calls: int
    total_verification_checks: int
    total_failures: int
    reproducibility_failures: int
    results: tuple[EvaluationResult, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        counts = (
            self.total_cases,
            self.passed_cases,
            self.failed_cases,
            self.skipped_cases,
            self.total_tool_calls,
            self.total_verification_checks,
            self.total_failures,
            self.reproducibility_failures,
        )
        if any(type(item) is not int or item < 0 for item in counts):
            raise ValueError("aggregate counts must be non-negative integers")
        if self.passed_cases + self.failed_cases + self.skipped_cases != self.total_cases:
            raise ValueError("aggregate status counts must sum to total_cases")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported aggregate report schema version")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AggregateEvaluationReport:
        if value.get("schema_version", 1) != 1:
            raise ValueError("Unsupported aggregate report schema version")
        return cls(
            evaluation_run_id=str(value["evaluation_run_id"]),
            total_cases=int(value["total_cases"]),
            passed_cases=int(value["passed_cases"]),
            failed_cases=int(value["failed_cases"]),
            skipped_cases=int(value["skipped_cases"]),
            metric_averages=dict(value["metric_averages"]),
            metric_minimums=dict(value["metric_minimums"]),
            metric_maximums=dict(value["metric_maximums"]),
            aggregate_classification=dict(value["aggregate_classification"]),
            total_tool_calls=int(value["total_tool_calls"]),
            total_verification_checks=int(value["total_verification_checks"]),
            total_failures=int(value["total_failures"]),
            reproducibility_failures=int(value["reproducibility_failures"]),
            results=tuple(EvaluationResult.from_dict(item) for item in value.get("results", ())),
        )


def dumps_canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
