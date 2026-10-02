from __future__ import annotations

import pytest

from changeguard.risk.engine import RiskEngine
from changeguard.risk.models import (
    BlastRadiusSignals,
    ChangeContext,
    ChangeMetrics,
    HistoricalEvidenceMatch,
    HistoricalEvidenceSignals,
    RiskConfig,
    RuntimeSignals,
    TestSignals as RiskTestSignals,
)


def fully_known_context(
    *,
    change: ChangeMetrics | None = None,
    blast_radius: BlastRadiusSignals | None = None,
    history: HistoricalEvidenceSignals | None = None,
    tests: RiskTestSignals | None = None,
    runtime: RuntimeSignals | None = None,
) -> ChangeContext:
    return ChangeContext(
        change=change
        or ChangeMetrics(
            files_changed=1,
            lines_added=2,
            lines_deleted=0,
            symbols_changed=1,
            methods_changed=1,
            java_types_changed=1,
            changed_file_concentration=1.0,
            evidence_refs=("diff-1",),
        ),
        blast_radius=blast_radius
        or BlastRadiusSignals(
            directly_affected_symbols=1,
            indirectly_affected_symbols=0,
            affected_files=1,
            dependency_depth=0,
            affected_callers=0,
            affected_components=1,
            unresolved_relationships=0,
            evidence_refs=("graph-1",),
        ),
        historical_evidence=history
        or HistoricalEvidenceSignals("known", evidence_refs=("search-1",)),
        tests=tests
        or RiskTestSignals(
            "known",
            tests_affected=4,
            tests_passed=4,
            tests_failed=0,
            tests_unavailable=0,
            evidence_refs=("tests-1",),
        ),
        runtime=runtime
        or RuntimeSignals(
            "known",
            operational_incidents=0,
            error_rate_increase=0.0,
            evidence_refs=("runtime-1",),
        ),
    )


def factor(assessment, name: str):
    return next(item for item in assessment.factors if item.factor == name)


def test_small_isolated_change_scores_low_with_explainable_factors():
    assessment = RiskEngine().assess(fully_known_context())

    assert assessment.overall_level == "LOW"
    assert assessment.score is not None and assessment.score < 25
    assert factor(assessment, "change_complexity").normalized_value < 0.1
    assert factor(assessment, "blast_radius").normalized_value < 0.1
    assert all(item.reason and item.factor for item in assessment.factors)


def test_assessment_is_reproducible_and_json_serializable():
    context = fully_known_context()
    first = RiskEngine().assess(context)
    second = RiskEngine().assess(context)

    assert first == second
    assert first.to_dict()["overall_level"] == first.overall_level


def test_larger_change_does_not_reduce_complexity():
    engine = RiskEngine()
    small = engine.assess(
        ChangeContext(change=ChangeMetrics(files_changed=1, lines_added=10, lines_deleted=0))
    )
    large = engine.assess(
        ChangeContext(change=ChangeMetrics(files_changed=5, lines_added=200, lines_deleted=50))
    )

    assert factor(large, "change_complexity").normalized_value > factor(
        small, "change_complexity"
    ).normalized_value
    assert factor(large, "change_complexity").contribution > factor(
        small, "change_complexity"
    ).contribution


def test_more_affected_nodes_do_not_reduce_blast_radius():
    engine = RiskEngine()
    small = engine.assess(
        ChangeContext(
            blast_radius=BlastRadiusSignals(
                directly_affected_symbols=1,
                indirectly_affected_symbols=1,
                affected_files=1,
                dependency_depth=1,
                affected_callers=1,
                affected_components=1,
                unresolved_relationships=0,
            )
        )
    )
    large = engine.assess(
        ChangeContext(
            blast_radius=BlastRadiusSignals(
                directly_affected_symbols=10,
                indirectly_affected_symbols=12,
                affected_files=9,
                dependency_depth=4,
                affected_callers=12,
                affected_components=5,
                unresolved_relationships=0,
            )
        )
    )

    assert factor(large, "blast_radius").normalized_value > factor(
        small, "blast_radius"
    ).normalized_value


def test_more_failed_tests_do_not_improve_test_confidence():
    engine = RiskEngine()
    passed = engine.assess(
        ChangeContext(tests=RiskTestSignals("known", 10, 10, 0, 0))
    )
    failed = engine.assess(
        ChangeContext(tests=RiskTestSignals("known", 10, 7, 3, 0))
    )

    assert factor(passed, "test_confidence").normalized_value == 0
    assert factor(failed, "test_confidence").normalized_value == pytest.approx(0.3)


def test_no_test_information_is_unknown_not_a_pass():
    assessment = RiskEngine().assess(ChangeContext(tests=RiskTestSignals("unknown")))

    test_factor = factor(assessment, "test_confidence")
    assert test_factor.state == "unknown"
    assert test_factor.normalized_value is None
    assert test_factor.contribution is None


def test_passing_only_information_does_not_assume_zero_failures():
    assessment = RiskEngine().assess(
        ChangeContext(tests=RiskTestSignals("uncertain", tests_passed=5))
    )
    test_factor = factor(assessment, "test_confidence")

    assert test_factor.state == "uncertain"
    assert test_factor.contribution is None
    assert "do not establish test confidence" in test_factor.reason


def test_historical_incident_contribution_is_explained_and_referenced():
    assessment = RiskEngine().assess(
        ChangeContext(
            historical_evidence=HistoricalEvidenceSignals(
                "known",
                matches=(HistoricalEvidenceMatch("INC-42", "incident", 0.9),),
                evidence_refs=("query-1",),
            )
        )
    )

    history_factor = factor(assessment, "historical_evidence")
    assert history_factor.normalized_value == pytest.approx(0.3)
    assert history_factor.contribution == pytest.approx(6.0)
    assert "similarity scores" in history_factor.reason
    assert "INC-42" in history_factor.evidence_refs


def test_historical_chunks_for_one_document_are_counted_once():
    assessment = RiskEngine().assess(
        ChangeContext(
            historical_evidence=HistoricalEvidenceSignals(
                "known",
                matches=(
                    HistoricalEvidenceMatch("INC-42", "incident", 0.6),
                    HistoricalEvidenceMatch("INC-42", "incident", 0.9),
                ),
            )
        )
    )

    history = factor(assessment, "historical_evidence")
    assert history.value["matching_records"] == 1
    assert history.value["weighted_similarity"] == pytest.approx(0.9)
    assert history.evidence_refs == ("INC-42",)


def test_unresolved_graph_edges_remain_uncertain():
    assessment = RiskEngine().assess(
        ChangeContext(
            blast_radius=BlastRadiusSignals(
                directly_affected_symbols=2,
                indirectly_affected_symbols=3,
                affected_files=2,
                dependency_depth=1,
                affected_callers=3,
                affected_components=1,
                unresolved_relationships=2,
            )
        )
    )

    blast = factor(assessment, "blast_radius")
    assert blast.state == "uncertain"
    assert blast.value["unresolved_relationships"] == 2
    assert any("unresolved" in item for item in assessment.uncertainties)


def test_no_evidence_returns_unknown_score_without_fabricated_data():
    assessment = RiskEngine().assess(ChangeContext())

    assert assessment.score is None
    assert assessment.overall_level == "UNKNOWN"
    assert assessment.evidence == ()
    assert len(assessment.uncertainties) == 5
    assert all(item.contribution is None for item in assessment.factors)


def test_zero_change_and_zero_blast_have_zero_normalized_values():
    assessment = RiskEngine().assess(
        fully_known_context(
            change=ChangeMetrics(
                files_changed=0,
                lines_added=0,
                lines_deleted=0,
                symbols_changed=0,
                methods_changed=0,
                java_types_changed=0,
                changed_file_concentration=1.0,
            ),
            blast_radius=BlastRadiusSignals(
                directly_affected_symbols=0,
                indirectly_affected_symbols=0,
                affected_files=0,
                dependency_depth=0,
                affected_callers=0,
                affected_components=0,
                unresolved_relationships=0,
            ),
        )
    )

    assert factor(assessment, "change_complexity").normalized_value == 0
    assert factor(assessment, "blast_radius").normalized_value == 0
    assert assessment.score == 0


def test_caps_and_score_are_bounded_at_maximum_supported_measurements():
    assessment = RiskEngine().assess(
        fully_known_context(
            change=ChangeMetrics(
                files_changed=1000,
                lines_added=10000,
                lines_deleted=10000,
                symbols_changed=1000,
                methods_changed=1000,
                java_types_changed=1000,
                changed_file_concentration=0.0,
            ),
            blast_radius=BlastRadiusSignals(
                directly_affected_symbols=1000,
                indirectly_affected_symbols=1000,
                affected_files=1000,
                dependency_depth=1000,
                affected_callers=1000,
                affected_components=1000,
                unresolved_relationships=0,
            ),
            history=HistoricalEvidenceSignals(
                "known",
                matches=tuple(
                    HistoricalEvidenceMatch(f"INC-{index}", "incident", 1.0)
                    for index in range(10)
                ),
            ),
            tests=RiskTestSignals("known", 10, 0, 10, 0),
            runtime=RuntimeSignals("known", 1000, 1.0),
        )
    )

    assert assessment.score == 100
    assert assessment.overall_level == "CRITICAL"
    assert all(
        item.normalized_value is None or 0 <= item.normalized_value <= 1
        for item in assessment.factors
    )


def test_missing_runtime_is_explicit_and_excluded_from_score_weight():
    assessment = RiskEngine().assess(
        fully_known_context(runtime=RuntimeSignals("unknown"))
    )

    runtime = factor(assessment, "runtime_signals")
    assert runtime.state == "unknown"
    assert runtime.contribution is None
    assert assessment.metrics["score_weight_coverage"] == pytest.approx(0.9)
    assert any("Runtime/operational signals are unavailable" in item for item in assessment.uncertainties)


def test_partial_measurements_keep_uncertainty_and_report_weight_coverage():
    assessment = RiskEngine().assess(
        ChangeContext(change=ChangeMetrics(files_changed=3))
    )

    complexity = factor(assessment, "change_complexity")
    assert complexity.state == "uncertain"
    assert complexity.coverage == pytest.approx(1 / 6)
    assert complexity.contribution is not None
    assert assessment.metrics["score_weight_coverage"] == pytest.approx(0.05)


def test_configurable_weights_caps_and_thresholds():
    config = RiskConfig(
        complexity_weight=100,
        blast_radius_weight=0,
        historical_evidence_weight=0,
        test_confidence_weight=0,
        runtime_weight=0,
        files_cap=4,
        changed_lines_cap=20,
        complexity_metric_weights=(0, 1, 0, 0, 0, 0),
        blast_metric_weights=(1, 1, 1, 1, 1, 1, 1),
        medium_threshold=20,
        high_threshold=40,
        critical_threshold=60,
    )
    assessment = RiskEngine(config).assess(
        ChangeContext(change=ChangeMetrics(files_changed=1, lines_added=100, lines_deleted=0))
    )

    assert assessment.score == 100
    assert assessment.overall_level == "CRITICAL"
    assert factor(assessment, "change_complexity").weight == 100


@pytest.mark.parametrize(
    ("line_count", "expected_level"),
    [
        (0, "LOW"),
        (24, "LOW"),
        (25, "MEDIUM"),
        (49, "MEDIUM"),
        (50, "HIGH"),
        (74, "HIGH"),
        (75, "CRITICAL"),
        (100, "CRITICAL"),
    ],
)
def test_risk_level_threshold_boundaries(line_count, expected_level):
    engine = RiskEngine(
        RiskConfig(
            complexity_weight=100,
            blast_radius_weight=0,
            historical_evidence_weight=0,
            test_confidence_weight=0,
            runtime_weight=0,
            changed_lines_cap=100,
            complexity_metric_weights=(0, 1, 0, 0, 0, 0),
        )
    )

    assessment = engine.assess(
        ChangeContext(
            change=ChangeMetrics(lines_added=line_count, lines_deleted=0)
        )
    )

    assert assessment.score == line_count
    assert assessment.overall_level == expected_level


def test_missing_added_or_deleted_line_count_is_not_assumed_zero():
    assessment = RiskEngine().assess(
        ChangeContext(change=ChangeMetrics(lines_added=25))
    )

    complexity = factor(assessment, "change_complexity")
    assert complexity.state == "unknown"
    assert complexity.contribution is None
    assert assessment.score is None


def test_uncertain_historical_matches_without_coverage_are_not_scored():
    assessment = RiskEngine().assess(
        ChangeContext(
            historical_evidence=HistoricalEvidenceSignals(
                "uncertain",
                matches=(HistoricalEvidenceMatch("INC-1", "incident", 1.0),),
            )
        )
    )

    history = factor(assessment, "historical_evidence")
    assert history.state == "uncertain"
    assert history.normalized_value == pytest.approx(0.3333)
    assert history.contribution is None


def test_runtime_partial_coverage_is_explicit():
    assessment = RiskEngine().assess(
        ChangeContext(runtime=RuntimeSignals("uncertain", operational_incidents=2))
    )

    runtime = factor(assessment, "runtime_signals")
    assert runtime.state == "uncertain"
    assert runtime.coverage == pytest.approx(0.5)
    assert runtime.contribution is not None


@pytest.mark.parametrize(
    "context",
    [
        ChangeContext(change=ChangeMetrics(files_changed=-1)),
        ChangeContext(change=ChangeMetrics(lines_added=-1, lines_deleted=3)),
        ChangeContext(change=ChangeMetrics(changed_file_concentration=1.1)),
        ChangeContext(
            historical_evidence=HistoricalEvidenceSignals(
                "known",
                matches=(HistoricalEvidenceMatch("INC", "incident", float("nan")),),
            )
        ),
        ChangeContext(tests=RiskTestSignals("known", 2, 2, 1, 0)),
        ChangeContext(runtime=RuntimeSignals("known", -1, 0.0)),
    ],
)
def test_invalid_signal_values_are_rejected(context):
    with pytest.raises(ValueError):
        RiskEngine().assess(context)


def test_invalid_risk_config_is_rejected():
    with pytest.raises(ValueError, match="thresholds"):
        RiskConfig(medium_threshold=50, high_threshold=50)
    with pytest.raises(ValueError, match="caps"):
        RiskConfig(files_cap=0)
