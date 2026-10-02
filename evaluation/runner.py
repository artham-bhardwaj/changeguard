from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from changeguard.analysis.service import ChangeAnalysisService
from changeguard.evidence.ingest import EvidenceIngestor
from changeguard.evidence.models import EvidenceDocument
from changeguard.llm.ollama import OllamaLLMAdapter
from changeguard.llm.agent import BoundedInvestigationAgent
from changeguard.llm.port import LLMPort
from changeguard.mcp.adapter import MCPToolPort
from changeguard.storage.sqlite import SQLiteAnalysisStore
from changeguard.verification.executor import SafeVerificationExecutor
from evaluation.fixtures import get_fixture, list_cases
from evaluation.metrics import aggregate_results, deterministic_fingerprint, evaluate_metrics
from evaluation.models import (
    AggregateEvaluationReport,
    EvaluationCase,
    EvaluationResult,
    EvaluationRunTrace,
)


class EvaluationRunner:
    """Run authored fixtures in disposable Git repositories, never in user repos."""

    def __init__(
        self,
        persistence: SQLiteAnalysisStore | None = None,
        *,
        llm_port: LLMPort | None = None,
    ) -> None:
        self.persistence = persistence
        self.llm_port = llm_port

    def cases(self) -> tuple[EvaluationCase, ...]:
        return list_cases()

    def run_github_workflows(self) -> dict[str, Any]:
        from evaluation.github import run_github_workflows

        return run_github_workflows()

    def run_case(
        self,
        case_id: str,
        *,
        verify: bool = False,
        llm: bool = False,
        persist: bool = True,
    ) -> EvaluationResult:
        fixture = get_fixture(case_id)
        run_id = str(uuid4())
        failures: list[str] = []
        warnings: list[str] = []
        with tempfile.TemporaryDirectory(prefix="changeguard-eval-") as temp_dir:
            root = Path(temp_dir)
            repository = root / "fixture-repo"
            repository.mkdir()
            base, head = self._materialize(fixture.baseline_files, fixture.target_files, repository)
            analysis_database = str(root / "analysis.sqlite3")
            store = SQLiteAnalysisStore(analysis_database)
            ingestor = EvidenceIngestor(store)
            for document in fixture.evidence:
                ingestor.ingest(replace(document, repository=str(repository)))

            investigator = None
            if llm:
                investigator = BoundedInvestigationAgent(
                    self.llm_port or OllamaLLMAdapter(),
                    MCPToolPort(evidence_database=analysis_database),
                )
            service = ChangeAnalysisService(
                store,
                investigator=investigator,
                verification_executor=SafeVerificationExecutor(),
            )
            analysis = service.analyze(
                repo_path=str(repository),
                base=base,
                head=head,
            )
            if verify:
                analysis = service.verify(analysis)
            (
                deterministic,
                grounding,
                verification_metrics,
                agent_metrics,
                risk,
                metric_failures,
            ) = evaluate_metrics(fixture.case, analysis, verification_requested=verify)
            verification_blocked = bool(
                verify
                and analysis.verification_result
                and analysis.verification_result.status == "BLOCKED"
                and any("bubblewrap" in item.lower() for item in analysis.verification_result.limitations)
            )
            if not verification_blocked:
                failures.extend(metric_failures)
            else:
                warnings.append("Verification was skipped because the required bubblewrap sandbox is unavailable.")
            if llm and analysis.investigation_error:
                warnings.append(f"LLM investigation unavailable: {analysis.investigation_error}")
            if not verify and fixture.case.expected_verification_properties:
                warnings.append("Verification expectations were not evaluated; pass verify=true to execute.")
            if analysis.verification_result and analysis.verification_result.status in {"BLOCKED", "ERROR", "TIMED_OUT"}:
                warnings.append(f"Verification ended with status {analysis.verification_result.status}.")

            stage_values = tuple(
                {
                    "stage": item.stage,
                    "status": item.status,
                    "duration_ms": item.duration_ms,
                    "counters": dict(item.counters),
                }
                for item in analysis.trace
            )
            counters: dict[str, int | float | str] = {}
            for stage in stage_values:
                for key, value in stage["counters"].items():
                    if isinstance(value, (int, float)):
                        counters[f"{stage['stage']}.{key}"] = value
            investigation_state = analysis.investigation_state
            tool_summary = {
                "total": agent_metrics["total_tool_calls"],
                "successful": agent_metrics["successful_tool_calls"],
                "rejected": agent_metrics["rejected_tool_calls"],
                "failed": agent_metrics["failed_tool_calls"],
            }
            verification_summary = {
                "planned": verification_metrics["planned"],
                "executed": verification_metrics["executed"],
                "passed": verification_metrics["passed"],
                "failed": verification_metrics["failed"],
                "status": verification_metrics["status"],
            }
            trace = EvaluationRunTrace(
                run_id=run_id,
                analysis_id=analysis.analysis_id,
                case_id=case_id,
                stages=stage_values,
                counters=counters,
                tool_call_summary=tool_summary,
                verification_summary=verification_summary,
                risk_summary={
                    "score": analysis.risk_assessment.score,
                    "level": analysis.risk_assessment.overall_level,
                },
                final_status="skipped" if verification_blocked else ("failed" if failures else "completed"),
            )
            expected_fields = (
                fixture.case.changed_files,
                fixture.case.changed_symbols,
                fixture.case.expected_impacted_symbols,
                fixture.case.expected_api_endpoints,
                fixture.case.expected_api_consumers,
                fixture.case.expected_api_edges,
                fixture.case.expected_api_impacted_consumers,
                fixture.case.expected_api_providers_for_changed_consumers,
                fixture.case.expected_ambiguous_matches,
                fixture.case.expected_unresolved_consumers,
                fixture.case.expected_dynamic_url_count,
                fixture.case.expected_evidence_ids,
                fixture.case.expected_unresolved_dependencies,
                fixture.case.expected_unmapped_changes,
                fixture.case.expected_uncertainty,
                fixture.case.expected_risk_properties,
                fixture.case.expected_verification_properties,
            )
            if not any(value is not None for value in expected_fields):
                warnings.append("No ground-truth metrics were authored for this case.")
            result = EvaluationResult(
                evaluation_run_id=run_id,
                case_id=case_id,
                deterministic_metrics=deterministic,
                grounding_metrics=grounding,
                verification_metrics=verification_metrics,
                agent_metrics=agent_metrics,
                risk_metrics=risk,
                fingerprint=deterministic_fingerprint(analysis),
                status="skipped" if verification_blocked else ("failed" if failures else "passed"),
                failures=tuple(failures),
                warnings=tuple(warnings),
                execution_metadata={
                    "mode": "llm" if llm else "deterministic",
                    "verification_enabled": verify,
                    "case_schema_version": fixture.case.schema_version,
                },
                run_trace=trace,
            )
            if analysis.investigation_report is not None:
                report = replace(
                    analysis.investigation_report,
                    evaluation={
                        "evaluation_run_id": run_id,
                        "status": result.status,
                        "fingerprint": result.fingerprint,
                        "grounded_finding_rate": grounding["grounded_finding_rate"],
                        "unsupported_claim_count": grounding["unsupported_claim_count"],
                        "stage_count": len(stage_values),
                    },
                )
                analysis = replace(analysis, investigation_report=report)
                store.save_change_analysis(
                    analysis.analysis_id, analysis.repository, analysis.to_dict()
                )
                result = replace(result, investigation_report=report.to_dict())
        if persist and self.persistence is not None:
            self.persistence.save_evaluation_result(result.to_dict())
        return result

    def run_all(
        self,
        *,
        verify: bool = False,
        llm: bool = False,
        case_ids: tuple[str, ...] | None = None,
        check_reproducibility: bool = True,
    ) -> AggregateEvaluationReport:
        run_id = str(uuid4())
        selected = case_ids or tuple(case.case_id for case in self.cases())
        results = [
            self.run_case(case_id, verify=verify, llm=llm, persist=False)
            for case_id in selected
        ]
        reproducibility_failures = 0
        if check_reproducibility and not llm:
            for index, case_id in enumerate(selected):
                repeated = self.run_case(case_id, verify=verify, persist=False, llm=False)
                matched = repeated.fingerprint == results[index].fingerprint
                metadata = dict(results[index].execution_metadata)
                metadata["repeated_fingerprint_match"] = matched
                metadata["repeated_fingerprint"] = repeated.fingerprint
                failures = results[index].failures
                status = results[index].status
                if not matched:
                    reproducibility_failures += 1
                    failures = (*failures, "Repeated deterministic evaluation produced a different fingerprint.")
                    status = "failed"
                results[index] = replace(
                    results[index],
                    execution_metadata=metadata,
                    failures=failures,
                    status=status,
                )
        elif llm:
            results = [
                replace(
                    result,
                    warnings=(*result.warnings, "Fingerprint repeatability is not measured in LLM mode."),
                    execution_metadata={**result.execution_metadata, "repeated_fingerprint_match": None},
                )
                for result in results
            ]
        aggregate = AggregateEvaluationReport.from_dict(
            aggregate_results(results, run_id, reproducibility_failures)
        )
        if self.persistence is not None:
            for result in results:
                self.persistence.save_evaluation_result(result.to_dict())
        if self.persistence is not None:
            self.persistence.save_evaluation_report(aggregate.to_dict())
        return aggregate

    def _materialize(
        self,
        baseline_files: dict[str, str],
        target_files: dict[str, str],
        repository: Path,
    ) -> tuple[str, str]:
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("Git is required to materialize evaluation fixtures.")
        env = {
            "PATH": os.path.dirname(git),
            "HOME": str(repository),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "ChangeGuard Evaluation",
            "GIT_AUTHOR_EMAIL": "evaluation@changeguard.local",
            "GIT_COMMITTER_NAME": "ChangeGuard Evaluation",
            "GIT_COMMITTER_EMAIL": "evaluation@changeguard.local",
            "GIT_AUTHOR_DATE": "2025-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2025-01-01T00:00:00+00:00",
        }

        def run(*args: str) -> str:
            completed = subprocess.run(
                [git, *args],
                cwd=repository,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            if completed.returncode:
                raise RuntimeError(completed.stderr.strip() or "Git fixture operation failed")
            return completed.stdout.strip()

        run("init", "--quiet", "-b", "main")
        run("config", "core.hooksPath", os.devnull)
        run("config", "commit.gpgSign", "false")
        self._write_tree(repository, baseline_files)
        run("add", "-A")
        run("commit", "--quiet", "-m", "evaluation baseline")
        base = run("rev-parse", "HEAD")
        self._write_tree(repository, target_files)
        run("add", "-A")
        run("commit", "--quiet", "-m", "evaluation target")
        return base, run("rev-parse", "HEAD")

    def _write_tree(self, root: Path, files: dict[str, str]) -> None:
        for existing in root.iterdir():
            if existing.name == ".git":
                continue
            if existing.is_dir() and not existing.is_symlink():
                shutil.rmtree(existing)
            else:
                existing.unlink()
        for relative, content in files.items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
                raise ValueError(f"Invalid evaluation fixture path: {relative}")
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
