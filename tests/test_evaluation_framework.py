from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace

import pytest

from changeguard.analysis.service import ChangeAnalysisService
from changeguard.evidence.models import EvidenceBundle, RetrievedEvidence
from changeguard.llm.decision import InvestigationState, InvestigationToolCall, ToolSelectionError
from changeguard.llm.report import InvestigationEvidenceReference, InvestigationFinding, InvestigationReport
from changeguard.storage.sqlite import SQLiteAnalysisStore
from changeguard.verification.models import (
    VerificationCheck,
    VerificationCheckResult,
    VerificationEvidence,
    VerificationResult,
)
from evaluation.fixtures import get_fixture, list_cases
from evaluation.metrics import (
    agent_metrics,
    deterministic_fingerprint,
    grounding_metrics,
    retrieval_metrics,
    verification_metrics,
)
from evaluation.models import AggregateEvaluationReport, EvaluationCase, EvaluationResult
from evaluation.runner import EvaluationRunner


def test_case_contract_round_trips_and_allows_missing_ground_truth():
    case = EvaluationCase.from_dict(
        {
            "case_id": "partial-case",
            "repository_fixture": "partial-fixture",
            "changed_files": ["src/A.java"],
        }
    )

    assert case.changed_files == ("src/A.java",)
    assert case.changed_symbols is None
    assert case.to_dict()["schema_version"] == 1
    assert EvaluationCase.from_dict(case.to_dict()) == case
    with pytest.raises(ValueError, match="missing fields"):
        EvaluationCase.from_dict({"case_id": "invalid"})
    with pytest.raises(ValueError, match="Unknown evaluation case fields"):
        EvaluationCase.from_dict(
            {"case_id": "bad", "repository_fixture": "fixture", "tool_command": "rm"}
        )
    with pytest.raises(ValueError, match="Unsupported evaluation case schema version"):
        EvaluationCase.from_dict(
            {
                "case_id": "future",
                "repository_fixture": "fixture",
                "schema_version": 2,
            }
        )


def test_classification_metrics_cover_perfect_partial_empty_and_duplicates():
    from evaluation.metrics import _classification

    perfect = _classification(["a", "b"], ["a", "b"])
    assert (perfect["precision"], perfect["recall"], perfect["f1"]) == (1, 1, 1)
    partial = _classification(["a", "a", "extra"], ["a", "missing"])
    assert partial["true_positive"] == 1
    assert partial["false_positive"] == 1
    assert partial["false_negative"] == 1
    assert partial["duplicate_predictions"] == 1
    assert partial["f1"] == pytest.approx(0.5)
    assert _classification([], ["missing"])["precision"] == 0
    assert _classification([], ["missing"])["recall"] == 0
    assert _classification([], [])["precision"] == 1
    assert _classification([], [])["recall"] == 1
    assert _classification(["extra"], [])["recall"] == 0


def test_structural_grounding_metrics_require_real_citations():
    history = EvidenceBundle(
        "query",
        [RetrievedEvidence("E-1", "chunk", "title", "src", 1.0, "text", {})],
        "timestamp",
        "tfidf",
        1,
        {},
        "ok",
    )
    report = InvestigationReport(
        "summary",
        [],
        [
            InvestigationFinding("supported", ["E-1"]),
            InvestigationFinding("missing", []),
            InvestigationFinding("invented", ["E-404"]),
            InvestigationFinding("duplicate", ["E-1", "E-1"]),
        ],
        [InvestigationEvidenceReference("E-1", "search_evidence", "real result")],
        [],
        [],
        [],
    )

    measured = grounding_metrics(report, None, history)

    assert measured["grounded_findings"] == 2
    assert measured["unsupported_claim_count"] == 2
    assert measured["invalid_citation_count"] == 1
    assert measured["duplicate_citation_count"] == 1
    assert measured["grounded_finding_rate"] == 0.5


def test_retrieval_metrics_distinguish_missing_ground_truth():
    assert retrieval_metrics(None, None)["state"] == "not_evaluated"
    assert retrieval_metrics(None, ("known",))["recall"] == 0


def test_agent_metrics_identify_duplicate_rejected_and_failed_calls():
    state = InvestigationState(
        "inspect",
        [
            InvestigationToolCall("get_file", {"path": "a.py"}, {"content": "x"}),
            InvestigationToolCall("get_file", {"path": "a.py"}, {"content": "x"}),
            InvestigationToolCall("get_git_diff", {}, error="tool unavailable"),
        ],
        status="rejected_decision",
        error=ToolSelectionError("unknown_tool", "unknown"),
    )

    measured = agent_metrics(state)

    assert measured["total_tool_calls"] == 3
    assert measured["successful_tool_calls"] == 2
    assert measured["failed_tool_calls"] == 1
    assert measured["duplicate_tool_calls"] == 1
    assert measured["rejected_tool_calls"] == 1
    exhausted = agent_metrics(
        InvestigationState(
            "bounded",
            [],
            status="max_tool_calls_reached",
            error=ToolSelectionError("MAX_TOOL_CALLS_REACHED", "budget"),
        )
    )
    assert exhausted["budget_exhausted"] is True


def test_verification_metrics_preserve_missing_and_blocked_states():
    assert verification_metrics(None)["missing_is_unknown"] is True
    check = VerificationCheck("check", "PYTHON_PYTEST", ("tests/test_a.py",), "test")
    evidence = VerificationEvidence(
        "check", "PYTHON_PYTEST", "/repo", "a" * 40, "BLOCKED",
        "2025-01-01T00:00:00Z", 0.0, "", "sandbox unavailable",
    )
    result = VerificationResult(
        "BLOCKED",
        (VerificationCheckResult(check, "BLOCKED", 0.0, "", "sandbox unavailable", evidence),),
        0.0, "", "", ("sandbox unavailable",), (evidence,), (),
    )
    assert verification_metrics(result)["blocked"] == 1
    assert verification_metrics(result)["failed"] == 0


def test_authored_java_fixtures_cover_call_graph_uncertainty_and_unmapped_changes():
    runner = EvaluationRunner()
    for case_id in (
        "local-method-change",
        "multi-hop-dependency",
        "unresolved-dependency",
        "historical-evidence",
        "unmapped-change",
    ):
        result = runner.run_case(case_id)
        assert result.status == "passed", result.failures
    unresolved = runner.run_case("unresolved-dependency")
    assert unresolved.deterministic_metrics["unresolved_dependency_count"] == 1
    unmapped = runner.run_case("unmapped-change")
    assert unmapped.deterministic_metrics["unmapped_change_count"] == 1
    historical = runner.run_case("historical-evidence")
    assert historical.deterministic_metrics["historical_evidence"]["recall"] == 1


def test_deterministic_fingerprint_ignores_random_ids_and_timing(tmp_path):
    fixture = get_fixture("historical-evidence")
    runner = EvaluationRunner()
    snapshots = []
    for _ in range(2):
        from pathlib import Path

        root = Path(tmp_path) / str(len(snapshots))
        root.mkdir()
        repo = root / "repo"
        repo.mkdir()
        base, head = runner._materialize(fixture.baseline_files, fixture.target_files, repo)
        analysis = ChangeAnalysisService(SQLiteAnalysisStore(str(root / "analysis.sqlite"))).analyze(
            repo_path=str(repo), base=base, head=head
        )
        snapshots.append(analysis)
    altered = replace(
        snapshots[0],
        analysis_id="different-random-analysis-id",
        trace=tuple(replace(item, duration_ms=item.duration_ms + 1000) for item in snapshots[0].trace),
        historical_evidence=replace(
            snapshots[0].historical_evidence,
            retrieval_timestamp="2030-12-31T23:59:59+00:00",
        ),
    )
    assert deterministic_fingerprint(snapshots[0]) == deterministic_fingerprint(altered)
    assert deterministic_fingerprint(snapshots[0]) == deterministic_fingerprint(snapshots[1])


def test_runner_reproducibility_aggregate_and_sqlite_round_trip(tmp_path):
    store = SQLiteAnalysisStore(str(tmp_path / "evaluations.sqlite"))
    report = EvaluationRunner(store).run_all(case_ids=("local-method-change",))

    assert report.total_cases == 1
    assert report.passed_cases == 1
    assert report.reproducibility_failures == 0
    loaded = store.get_evaluation_report(report.evaluation_run_id)
    assert loaded is not None
    restored = AggregateEvaluationReport.from_dict(loaded)
    assert restored.results[0].execution_metadata["repeated_fingerprint_match"] is True
    result_id = restored.results[0].evaluation_run_id
    assert EvaluationResult.from_dict(store.get_evaluation_result(result_id) or {}) == restored.results[0]
    assert store.list_evaluation_runs()
    import sqlite3

    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is required for verification fixture execution")
@pytest.mark.parametrize(
    ("case_id", "expected_status", "expected_signal"),
    [
        ("verification-pass", "PASSED", "known"),
        ("verification-fail", "FAILED", "known"),
        ("verification-not-discovered", "PASSED", "unknown"),
    ],
)
def test_verification_fixture_variants_execute_through_bounded_executor(
    case_id, expected_status, expected_signal
):
    result = EvaluationRunner().run_case(case_id, verify=True)

    assert result.status == "passed", result.failures
    assert result.verification_metrics["status"] == expected_status
    assert result.risk_metrics["test_signal_state"] == expected_signal
    if expected_status == "FAILED":
        assert result.verification_metrics["failed"] >= 1
        assert result.verification_metrics["passed"] == 0


def test_cli_evaluate_case_persists_machine_readable_result(tmp_path):
    database = tmp_path / "cli-eval.sqlite"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "changeguard.cli",
            "evaluate",
            "--case",
            "local-method-change",
            "--database",
            str(database),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["case_id"] == "local-method-change"
    assert result["status"] == "passed"
    assert SQLiteAnalysisStore(str(database)).get_evaluation_result(
        result["evaluation_run_id"]
    ) is not None


def test_case_registry_has_authored_expected_properties():
    cases = {case.case_id: case for case in list_cases()}

    assert cases["unresolved-dependency"].expected_unresolved_dependencies == 1
    assert cases["unmapped-change"].expected_unmapped_changes == 1
    assert cases["historical-evidence"].expected_evidence_ids == ("HISTORY-PAYMENTS-001",)


def test_llm_report_receives_evaluation_observability_without_affecting_fingerprint():
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def generate(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return '{"type":"final","answer":"No tool evidence was collected."}'
            return json.dumps(
                {
                    "summary": "Insufficient evidence.",
                    "summary_evidence_refs": [],
                    "findings": [],
                    "uncertainties": ["No investigation evidence was collected."],
                    "recommended_verification": [],
                }
            )

    llm_result = EvaluationRunner(llm_port=FakeLLM()).run_case(
        "local-method-change", llm=True
    )
    deterministic = EvaluationRunner().run_case("local-method-change")

    assert llm_result.status == "passed"
    assert llm_result.investigation_report is not None
    assert llm_result.investigation_report["evaluation"]["status"] == "passed"
    assert llm_result.fingerprint == deterministic.fingerprint
