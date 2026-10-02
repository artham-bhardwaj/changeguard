from __future__ import annotations

import math
from collections.abc import Iterable

from changeguard.risk.models import (
    BlastRadiusSignals,
    ChangeContext,
    EvidenceReference,
    HistoricalEvidenceSignals,
    RiskAssessment,
    RiskConfig,
    RiskFactor,
    RuntimeSignals,
    SignalState,
    TestSignals,
)


class RiskEngine:
    """Deterministic scoring over structured change signals; performs no analysis."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def assess(self, context: ChangeContext) -> RiskAssessment:
        factors = (
            self._complexity(context),
            self._blast_radius(context.blast_radius),
            self._historical(context.historical_evidence),
            self._tests(context.tests),
            self._runtime(context.runtime),
        )
        active = [
            factor
            for factor in factors
            if factor.contribution is not None
            and factor.coverage > 0
            and factor.weight > 0
        ]
        if active:
            active_weight = sum(factor.weight * factor.coverage for factor in active)
            score = round(
                sum(factor.contribution or 0.0 for factor in active)
                / active_weight
                * 100,
                2,
            )
            level = self._risk_level(score)
        else:
            active_weight = 0.0
            score = None
            level = "UNKNOWN"

        total_weight = sum(factor.weight for factor in factors)
        coverage = round(active_weight / total_weight, 4) if total_weight else 0.0
        uncertainties = tuple(
            factor.reason for factor in factors if factor.state != "known"
        )
        recommendations = self._recommendations(factors)
        evidence = self._evidence(context)
        metrics: dict[str, float | int | str | None] = {
            "scoring_version": "1",
            "score_weight_coverage": coverage,
            "scored_weight": round(active_weight, 2),
            "configured_weight": round(total_weight, 2),
            "score_interpretation": "weighted average of available signals",
        }
        return RiskAssessment(
            level,
            score,
            factors,
            evidence,
            uncertainties,
            recommendations,
            metrics,
        )

    def _complexity(self, context: ChangeContext) -> RiskFactor:
        change = context.change
        values: dict[str, int | float | str | None] = {
            "files_changed": change.files_changed,
            "lines_added": change.lines_added,
            "lines_deleted": change.lines_deleted,
            "symbols_changed": change.symbols_changed,
            "methods_changed": change.methods_changed,
            "java_types_changed": change.java_types_changed,
            "changed_file_concentration": change.changed_file_concentration,
        }
        for name, value in (
            ("files_changed", change.files_changed),
            ("lines_added", change.lines_added),
            ("lines_deleted", change.lines_deleted),
            ("symbols_changed", change.symbols_changed),
            ("methods_changed", change.methods_changed),
            ("java_types_changed", change.java_types_changed),
        ):
            if value is not None:
                self._validate_count(value, name)
        component_values: list[tuple[str, float, float]] = []
        dimensions = (
            (
                "files_changed",
                change.files_changed,
                self.config.files_cap,
                self.config.complexity_metric_weights[0],
            ),
            (
                "changed_lines",
                (
                    change.lines_added + change.lines_deleted
                    if change.lines_added is not None
                    and change.lines_deleted is not None
                    else None
                ),
                self.config.changed_lines_cap,
                self.config.complexity_metric_weights[1],
            ),
            (
                "symbols_changed",
                change.symbols_changed,
                self.config.symbols_cap,
                self.config.complexity_metric_weights[2],
            ),
            (
                "methods_changed",
                change.methods_changed,
                self.config.methods_cap,
                self.config.complexity_metric_weights[3],
            ),
            (
                "java_types_changed",
                change.java_types_changed,
                self.config.java_types_cap,
                self.config.complexity_metric_weights[4],
            ),
        )
        for name, value, cap, dimension_weight in dimensions:
            if value is not None:
                if dimension_weight > 0:
                    component_values.append(
                        (name, self._normalize(value, cap), dimension_weight)
                    )
        if change.changed_file_concentration is not None:
            self._validate_fraction(
                change.changed_file_concentration, "changed_file_concentration"
            )
            if self.config.complexity_metric_weights[5] > 0:
                component_values.append(
                    (
                        "change_spread",
                        1.0 - change.changed_file_concentration,
                        self.config.complexity_metric_weights[5],
                    )
                )
        if not component_values:
            return self._unknown(
                "change_complexity",
                values,
                self.config.complexity_weight,
                "Changed-file and line metrics are unavailable.",
                change.evidence_refs,
            )
        normalized = self._weighted_mean(component_values)
        observed_weight = sum(weight for _, _, weight in component_values)
        coverage = observed_weight / sum(self.config.complexity_metric_weights)
        state: SignalState = (
            "known" if coverage == 1 else "uncertain"
        )
        reason = (
            f"Complexity is the mean of {len(component_values)} available normalized "
            "measurements: counts are capped by configured limits; more changed "
            "lines/files/symbols increase the value, and lower file concentration "
            "represents more distributed change."
        )
        if state == "uncertain":
            reason += " Some structural complexity measurements are unavailable."
        return self._factor(
            "change_complexity",
            values,
            normalized,
            self.config.complexity_weight,
            state,
            reason,
            change.evidence_refs,
            coverage,
        )

    def _blast_radius(self, signal: BlastRadiusSignals | None) -> RiskFactor:
        weight = self.config.blast_radius_weight
        if signal is None:
            return self._unknown(
                "blast_radius",
                {},
                weight,
                "Blast-radius analysis is unavailable; no dependency graph analyzer is currently connected.",
            )
        values: dict[str, int | float | str | None] = {
            "directly_affected_symbols": signal.directly_affected_symbols,
            "indirectly_affected_symbols": signal.indirectly_affected_symbols,
            "affected_files": signal.affected_files,
            "dependency_depth": signal.dependency_depth,
            "affected_callers": signal.affected_callers,
            "affected_components": signal.affected_components,
            "unresolved_relationships": signal.unresolved_relationships,
        }
        dimensions = (
            (
                "direct_symbols",
                signal.directly_affected_symbols,
                self.config.blast_count_cap,
                self.config.blast_metric_weights[0],
            ),
            (
                "indirect_symbols",
                signal.indirectly_affected_symbols,
                self.config.blast_count_cap,
                self.config.blast_metric_weights[1],
            ),
            (
                "affected_files",
                signal.affected_files,
                self.config.blast_count_cap,
                self.config.blast_metric_weights[2],
            ),
            (
                "dependency_depth",
                signal.dependency_depth,
                self.config.dependency_depth_cap,
                self.config.blast_metric_weights[3],
            ),
            (
                "affected_callers",
                signal.affected_callers,
                self.config.blast_count_cap,
                self.config.blast_metric_weights[4],
            ),
            (
                "affected_components",
                signal.affected_components,
                self.config.blast_count_cap,
                self.config.blast_metric_weights[5],
            ),
        )
        normalized_dimensions = []
        for name, value, cap, metric_weight in dimensions:
            if value is not None:
                self._validate_count(value, name)
                if metric_weight > 0:
                    normalized_dimensions.append(
                        (name, self._normalize(value, cap), metric_weight)
                    )
        if not normalized_dimensions:
            if signal.unresolved_relationships is not None:
                self._validate_count(
                    signal.unresolved_relationships, "unresolved_relationships"
                )
                return self._unscored(
                    "blast_radius",
                    values,
                    weight,
                    "Graph relationships are unresolved, but no affected-node counts are available.",
                    signal.evidence_refs,
                )
            return self._unknown(
                "blast_radius",
                values,
                weight,
                "Blast-radius counts are unavailable.",
                signal.evidence_refs,
            )

        unresolved = signal.unresolved_relationships
        if unresolved is not None:
            self._validate_count(unresolved, "unresolved_relationships")
        observed_weight = sum(weight for _, _, weight in normalized_dimensions)
        if unresolved is not None:
            observed_weight += self.config.blast_metric_weights[6]
        coverage = observed_weight / sum(self.config.blast_metric_weights)
        state: SignalState = (
            "known"
            if coverage == 1 and unresolved == 0
            else "uncertain"
        )
        reason = (
            f"Blast radius is the mean of {len(normalized_dimensions)} available "
            "normalized counts for direct/indirect symbols, files, callers, "
            "components, and dependency depth."
        )
        if unresolved:
            reason += f" {unresolved} graph relationship(s) remain unresolved."
        elif state == "uncertain":
            reason += " Some graph dimensions or the unresolved-edge count are unavailable."
        return self._factor(
            "blast_radius",
            values,
            self._weighted_mean(normalized_dimensions),
            weight,
            state,
            reason,
            signal.evidence_refs,
            coverage,
        )

    def _historical(
        self, signal: HistoricalEvidenceSignals | None
    ) -> RiskFactor:
        weight = self.config.historical_evidence_weight
        if signal is None or signal.state == "unknown":
            return self._unknown(
                "historical_evidence",
                {"matching_records": len(signal.matches) if signal else None},
                weight,
                "Historical evidence search was unavailable; absence of evidence is not treated as no risk.",
                signal.evidence_refs if signal else (),
            )
        if signal.state not in {"known", "uncertain"}:
            raise ValueError(f"invalid historical evidence state: {signal.state}")
        relevance = 0.0
        incident_count = 0
        unique_matches = {}
        for match in signal.matches:
            if not match.evidence_id:
                raise ValueError("historical evidence ID must not be empty")
            if match.source_type not in {
                "incident",
                "postmortem",
                "issue",
                "pull_request",
                "release_note",
                "runbook",
                "architecture_decision",
            }:
                raise ValueError(f"unsupported historical source type: {match.source_type}")
            self._validate_fraction(match.similarity_score, "similarity_score")
            previous = unique_matches.get(match.evidence_id)
            if previous is not None and previous.source_type != match.source_type:
                raise ValueError(
                    f"historical evidence ID {match.evidence_id} has conflicting source types"
                )
            if previous is None or match.similarity_score > previous.similarity_score:
                unique_matches[match.evidence_id] = match
        for match in sorted(unique_matches.values(), key=lambda item: item.evidence_id):
            source_type = match.source_type.lower()
            source_weight = {
                "incident": 1.0,
                "postmortem": 1.0,
                "issue": 0.5,
                "pull_request": 0.5,
                "release_note": 0.5,
                "runbook": 0.25,
                "architecture_decision": 0.25,
            }[source_type]
            relevance += match.similarity_score * source_weight
            if source_type in {"incident", "postmortem"}:
                incident_count += 1
        normalized = self._normalize(relevance, self.config.historical_match_cap)
        values: dict[str, int | float | str | None] = {
            "matching_records": len(unique_matches),
            "matching_incidents_and_postmortems": incident_count,
            "weighted_similarity": round(relevance, 4),
        }
        reason = (
            "Historical contribution sums structured similarity scores, weighted "
            "by source type (incident/postmortem 1.0, issue/PR/release 0.5, "
            "runbook/ADR 0.25), then caps at the configured match limit."
        )
        if signal.state == "uncertain":
            reason += " The evidence result is marked incomplete or uncertain."
        refs = signal.evidence_refs + tuple(
            evidence_id for evidence_id in sorted(unique_matches)
        )
        if signal.coverage is not None:
            self._validate_fraction(
                signal.coverage, "historical_evidence.coverage"
            )
        coverage = (
            signal.coverage
            if signal.coverage is not None
            else (1.0 if signal.state == "known" else 0.0)
        )
        factor_state: SignalState = (
            "known" if signal.state == "known" and coverage == 1 else "uncertain"
        )
        return self._factor(
            "historical_evidence",
            values,
            normalized,
            weight,
            factor_state,
            reason,
            refs,
            coverage,
        )

    def _tests(self, signal: TestSignals | None) -> RiskFactor:
        weight = self.config.test_confidence_weight
        if signal is None or signal.state == "unknown":
            return self._unknown(
                "test_confidence",
                {
                    "tests_affected": signal.tests_affected if signal else None,
                    "tests_passed": signal.tests_passed if signal else None,
                    "tests_failed": signal.tests_failed if signal else None,
                    "tests_unavailable": signal.tests_unavailable if signal else None,
                },
                weight,
                "Test and verification results are unavailable; no pass status is assumed.",
                signal.evidence_refs if signal else (),
            )
        if signal.state not in {"known", "uncertain"}:
            raise ValueError(f"invalid test signal state: {signal.state}")
        counts = {
            "tests_affected": signal.tests_affected,
            "tests_passed": signal.tests_passed,
            "tests_failed": signal.tests_failed,
            "tests_unavailable": signal.tests_unavailable,
        }
        for name, value in counts.items():
            if value is not None:
                self._validate_count(value, name)
        passed = signal.tests_passed
        failed = signal.tests_failed
        unavailable = signal.tests_unavailable
        if failed is None and unavailable is None:
            if passed is None:
                result = self._unknown(
                    "test_confidence",
                    counts,
                    weight,
                    "Test outcome counts are unavailable.",
                    signal.evidence_refs,
                )
                if signal.state == "uncertain":
                    return self._unscored(
                        "test_confidence",
                        counts,
                        weight,
                        result.reason,
                        signal.evidence_refs,
                    )
                return result
            reason = (
                "Failed and unavailable test counts were not reported; passing "
                "results alone do not establish test confidence."
            )
            if signal.state == "uncertain":
                return self._unscored(
                    "test_confidence", counts, weight, reason, signal.evidence_refs
                )
            return self._unknown(
                "test_confidence",
                counts,
                weight,
                reason,
                signal.evidence_refs,
            )
        else:
            passed_count = passed or 0
            failed_count = failed or 0
            unavailable_count = unavailable or 0
            denominator = signal.tests_affected
            if denominator is None:
                denominator = passed_count + failed_count + unavailable_count
            else:
                self._validate_count(denominator, "tests_affected")
            if denominator == 0:
                return self._unknown(
                    "test_confidence",
                    counts,
                    weight,
                    "No affected test outcomes are available.",
                    signal.evidence_refs,
                )
            if passed_count + failed_count + unavailable_count > denominator:
                raise ValueError("test outcome counts exceed tests_affected")
            complete_counts = (
                failed is not None
                and unavailable is not None
                and passed is not None
            )
            if (
                complete_counts
                and signal.tests_affected is not None
                and passed_count + failed_count + unavailable_count
                != signal.tests_affected
            ):
                raise ValueError("test outcome counts do not account for tests_affected")
            normalized = min(
                1.0, (failed_count + unavailable_count) / denominator
            )
            coverage = self._test_coverage(signal)
            state = (
                "known"
                if complete_counts and signal.state == "known" and coverage == 1
                else "uncertain"
            )
            reason = (
                "Verification risk is (failed + unavailable tests) divided by "
                "tests_affected, or by reported outcomes when that total is absent."
            )
            if state == "uncertain":
                reason += " Some test-result counts are unknown or marked uncertain."
        return self._factor(
            "test_confidence",
            counts,
            normalized,
            weight,
            state,
            reason,
            signal.evidence_refs,
            coverage,
        )

    def _runtime(self, signal: RuntimeSignals | None) -> RiskFactor:
        weight = self.config.runtime_weight
        if signal is None or signal.state == "unknown":
            return self._unknown(
                "runtime_signals",
                {
                    "operational_incidents": (
                        signal.operational_incidents if signal else None
                    ),
                    "error_rate_increase": (
                        signal.error_rate_increase if signal else None
                    ),
                },
                weight,
                "Runtime/operational signals are unavailable in the current repository.",
                signal.evidence_refs if signal else (),
            )
        if signal.state not in {"known", "uncertain"}:
            raise ValueError(f"invalid runtime signal state: {signal.state}")
        values: dict[str, int | float | str | None] = {
            "operational_incidents": signal.operational_incidents,
            "error_rate_increase": signal.error_rate_increase,
        }
        components: list[tuple[str, float, float]] = []
        if signal.operational_incidents is not None:
            self._validate_count(
                signal.operational_incidents, "operational_incidents"
            )
            components.append(
                (
                    "operational_incidents",
                    self._normalize(
                        signal.operational_incidents,
                        self.config.runtime_incident_cap,
                    ),
                    1.0,
                )
            )
        if signal.error_rate_increase is not None:
            self._validate_fraction(signal.error_rate_increase, "error_rate_increase")
            components.append(("error_rate_increase", signal.error_rate_increase, 1.0))
        if not components:
            if signal.state == "uncertain":
                return self._unscored(
                    "runtime_signals",
                    values,
                    weight,
                    "Runtime signal source is uncertain and contains no measurable values.",
                    signal.evidence_refs,
                )
            return self._unknown(
                "runtime_signals",
                values,
                weight,
                "Runtime/operational signals were marked available but contained no measurable values.",
                signal.evidence_refs,
            )
        if signal.coverage is not None:
            self._validate_fraction(signal.coverage, "runtime.coverage")
        coverage = signal.coverage if signal.coverage is not None else len(components) / 2
        state: SignalState = (
            "known"
            if len(components) == 2 and signal.state == "known" and coverage == 1
            else "uncertain"
        )
        reason = (
            "Runtime contribution is the mean of available operational incident "
            "count/cap and error-rate increase measurements."
        )
        if state == "uncertain":
            reason += " Some runtime signals are unavailable or uncertain."
        return self._factor(
            "runtime_signals",
            values,
            self._weighted_mean(components),
            weight,
            state,
            reason,
            signal.evidence_refs,
            coverage,
        )

    def _risk_level(self, score: float) -> str:
        if score < self.config.medium_threshold:
            return "LOW"
        if score < self.config.high_threshold:
            return "MEDIUM"
        if score < self.config.critical_threshold:
            return "HIGH"
        return "CRITICAL"

    def _recommendations(self, factors: tuple[RiskFactor, ...]) -> tuple[str, ...]:
        recommendations = []
        by_name = {factor.factor: factor for factor in factors}
        if by_name["test_confidence"].state != "known":
            recommendations.append("Run and record the relevant automated test results.")
        if by_name["blast_radius"].state != "known":
            recommendations.append(
                "Review dependency and caller impact; graph analysis is not fully available."
            )
        if by_name["historical_evidence"].state != "known":
            recommendations.append(
                "Review relevant incidents and engineering evidence if available."
            )
        if by_name["runtime_signals"].state != "known":
            recommendations.append(
                "Review runtime/operational signals if they are available."
            )
        return tuple(recommendations)

    def _evidence(self, context: ChangeContext) -> tuple[EvidenceReference, ...]:
        source_refs: list[tuple[str, str]] = [
            ("git/change metrics", context.change.evidence_refs),
        ]
        if context.blast_radius is not None:
            source_refs.append(
                ("blast-radius analysis", context.blast_radius.evidence_refs)
            )
        if context.historical_evidence is not None:
            source_refs.append(
                ("historical evidence search", context.historical_evidence.evidence_refs)
            )
            source_refs.append(
                (
                    "historical evidence document",
                    tuple(match.evidence_id for match in context.historical_evidence.matches),
                )
            )
        if context.tests is not None:
            source_refs.append(("test/verification results", context.tests.evidence_refs))
        if context.runtime is not None:
            source_refs.append(("runtime/operational data", context.runtime.evidence_refs))
        references = []
        seen: set[tuple[str, str]] = set()
        for source, refs in source_refs:
            for reference in refs:
                key = (source, reference)
                if reference and key not in seen:
                    seen.add(key)
                    references.append(
                        EvidenceReference(reference, source, f"Input to {source}")
                    )
        return tuple(references)

    def _factor(
        self,
        name: str,
        values: dict[str, int | float | str | None],
        normalized: float,
        weight: float,
        state: SignalState,
        reason: str,
        evidence_refs: Iterable[str] = (),
        coverage: float = 1.0,
    ) -> RiskFactor:
        self._validate_fraction(coverage, f"{name}.coverage")
        return RiskFactor(
            name,
            values,
            round(normalized, 4),
            weight,
            round(normalized * weight * coverage, 2) if coverage > 0 else None,
            coverage,
            state,
            reason,
            tuple(dict.fromkeys(reference for reference in evidence_refs if reference)),
        )

    def _unknown(
        self,
        name: str,
        values: dict[str, int | float | str | None],
        weight: float,
        reason: str,
        evidence_refs: Iterable[str] = (),
    ) -> RiskFactor:
        return RiskFactor(
            name,
            values,
            None,
            weight,
            None,
            0.0,
            "unknown",
            reason,
            tuple(dict.fromkeys(reference for reference in evidence_refs if reference)),
        )

    def _unscored(
        self,
        name: str,
        values: dict[str, int | float | str | None],
        weight: float,
        reason: str,
        evidence_refs: Iterable[str] = (),
    ) -> RiskFactor:
        return RiskFactor(
            name,
            values,
            None,
            weight,
            None,
            0.0,
            "uncertain",
            reason,
            tuple(dict.fromkeys(reference for reference in evidence_refs if reference)),
        )

    @staticmethod
    def _normalize(value: float, cap: float) -> float:
        return min(value / cap, 1.0)

    @staticmethod
    def _weighted_mean(values: list[tuple[str, float, float]]) -> float:
        denominator = sum(weight for _, _, weight in values)
        return sum(value * weight for _, value, weight in values) / denominator

    def _validate_count(self, value: int, name: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    def _test_coverage(self, signal: TestSignals) -> float:
        if signal.coverage is not None:
            self._validate_fraction(signal.coverage, "tests.coverage")
            return signal.coverage
        known_outcome_counts = sum(
            value is not None
            for value in (
                signal.tests_passed,
                signal.tests_failed,
                signal.tests_unavailable,
            )
        )
        return known_outcome_counts / 3

    @staticmethod
    def _validate_fraction(value: float, name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError(f"{name} must be between 0 and 1")
