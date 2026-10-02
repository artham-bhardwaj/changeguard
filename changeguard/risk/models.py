from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Literal

SignalState = Literal["known", "unknown", "uncertain"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN"]


@dataclass(frozen=True)
class EvidenceReference:
    reference: str
    source: str
    description: str


@dataclass(frozen=True)
class ChangeMetrics:
    files_changed: int | None = None
    lines_added: int | None = None
    lines_deleted: int | None = None
    symbols_changed: int | None = None
    methods_changed: int | None = None
    java_types_changed: int | None = None
    changed_file_concentration: float | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class BlastRadiusSignals:
    directly_affected_symbols: int | None = None
    indirectly_affected_symbols: int | None = None
    affected_files: int | None = None
    dependency_depth: int | None = None
    affected_callers: int | None = None
    affected_components: int | None = None
    unresolved_relationships: int | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoricalEvidenceMatch:
    evidence_id: str
    source_type: str
    similarity_score: float


@dataclass(frozen=True)
class HistoricalEvidenceSignals:
    state: SignalState
    matches: tuple[HistoricalEvidenceMatch, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    coverage: float | None = None


@dataclass(frozen=True)
class TestSignals:
    state: SignalState
    tests_affected: int | None = None
    tests_passed: int | None = None
    tests_failed: int | None = None
    tests_unavailable: int | None = None
    evidence_refs: tuple[str, ...] = ()
    coverage: float | None = None


@dataclass(frozen=True)
class RuntimeSignals:
    state: SignalState
    operational_incidents: int | None = None
    error_rate_increase: float | None = None
    evidence_refs: tuple[str, ...] = ()
    coverage: float | None = None


@dataclass(frozen=True)
class ChangeContext:
    change: ChangeMetrics = field(default_factory=ChangeMetrics)
    blast_radius: BlastRadiusSignals | None = None
    historical_evidence: HistoricalEvidenceSignals | None = None
    tests: TestSignals | None = None
    runtime: RuntimeSignals | None = None


@dataclass(frozen=True)
class RiskConfig:
    complexity_weight: float = 30.0
    blast_radius_weight: float = 25.0
    historical_evidence_weight: float = 20.0
    test_confidence_weight: float = 15.0
    runtime_weight: float = 10.0
    files_cap: int = 10
    changed_lines_cap: int = 500
    symbols_cap: int = 50
    methods_cap: int = 50
    java_types_cap: int = 20
    blast_count_cap: int = 25
    dependency_depth_cap: int = 5
    historical_match_cap: int = 3
    runtime_incident_cap: int = 5
    complexity_metric_weights: tuple[float, ...] = (1, 1, 1, 1, 1, 1)
    blast_metric_weights: tuple[float, ...] = (1, 1, 1, 1, 1, 1, 1)
    medium_threshold: float = 25.0
    high_threshold: float = 50.0
    critical_threshold: float = 75.0

    def __post_init__(self) -> None:
        weights = (
            self.complexity_weight,
            self.blast_radius_weight,
            self.historical_evidence_weight,
            self.test_confidence_weight,
            self.runtime_weight,
        )
        if any(not math.isfinite(weight) or weight < 0 for weight in weights) or sum(weights) <= 0:
            raise ValueError("Risk factor weights must be non-negative and sum above zero")
        caps = (
            self.files_cap,
            self.changed_lines_cap,
            self.symbols_cap,
            self.methods_cap,
            self.java_types_cap,
            self.blast_count_cap,
            self.dependency_depth_cap,
            self.historical_match_cap,
            self.runtime_incident_cap,
        )
        if any(cap <= 0 for cap in caps):
            raise ValueError("Risk normalization caps must be greater than zero")
        if not (
            0 <= self.medium_threshold < self.high_threshold < self.critical_threshold <= 100
        ):
            raise ValueError("Risk thresholds must increase within the 0-100 score range")
        if len(self.complexity_metric_weights) != 6 or any(
            not math.isfinite(weight) or weight < 0
            for weight in self.complexity_metric_weights
        ) or sum(self.complexity_metric_weights) <= 0:
            raise ValueError("Complexity metric weights must contain six non-negative values")
        if len(self.blast_metric_weights) != 7 or any(
            not math.isfinite(weight) or weight < 0
            for weight in self.blast_metric_weights
        ) or sum(self.blast_metric_weights) <= 0:
            raise ValueError("Blast-radius metric weights must contain seven non-negative values")


@dataclass(frozen=True)
class RiskFactor:
    factor: str
    value: dict[str, int | float | str | None]
    normalized_value: float | None
    weight: float
    contribution: float | None
    coverage: float
    state: SignalState
    reason: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskAssessment:
    overall_level: RiskLevel
    score: float | None
    factors: tuple[RiskFactor, ...]
    evidence: tuple[EvidenceReference, ...]
    uncertainties: tuple[str, ...]
    recommendations: tuple[str, ...]
    metrics: dict[str, float | int | str | None]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
