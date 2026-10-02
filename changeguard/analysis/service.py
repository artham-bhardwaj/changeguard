from __future__ import annotations

import re
import sqlite3
import time
from collections import defaultdict
import re
from dataclasses import replace
from typing import Protocol
from uuid import uuid4

from changeguard.agent.agent import (
    JavaSourcePort,
    LocalTools,
    RevisionReadPort,
    ToolPort,
)
from changeguard.analysis.models import (
    BlastRadiusResult,
    ChangeGuardAnalysis,
    ChangeStageTrace,
    StageStatus,
)
from changeguard.code_intelligence.api import ApiContractMapper, ApiImpact
from changeguard.code_intelligence.java import ChangedLineRange, JavaAnalysis, JavaSymbolMapper
from changeguard.evidence.models import EvidenceBundle
from changeguard.llm.agent import BoundedInvestigationAgent
from changeguard.llm.ollama import OllamaError
from changeguard.llm.report import ReportGenerationResult
from changeguard.mcp.adapter import MCPToolError
from changeguard.models.schemas import AnalysisState, DiffResult
from changeguard.risk.engine import RiskEngine
from changeguard.risk.models import (
    BlastRadiusSignals,
    ChangeContext,
    ChangeMetrics,
    HistoricalEvidenceMatch,
    HistoricalEvidenceSignals,
    RiskAssessment,
    RuntimeSignals,
    TestSignals,
)
from changeguard.storage.sqlite import SQLiteAnalysisStore
from changeguard.verification.executor import (
    SafeVerificationExecutor,
    VerificationExecutorPort,
)
from changeguard.verification.models import (
    VerificationPlan,
    VerificationResult,
)
from changeguard.verification.planner import VerificationPlanner


class AnalysisToolPort(ToolPort, JavaSourcePort, RevisionReadPort, Protocol):
    pass


class ChangeAnalysisError(RuntimeError):
    def __init__(self, message: str, trace: tuple[ChangeStageTrace, ...]) -> None:
        super().__init__(message)
        self.trace = trace


class ChangeAnalysisService:
    """Coordinate read-only change analysis while keeping scoring deterministic."""

    def __init__(
        self,
        store: SQLiteAnalysisStore,
        tools: AnalysisToolPort | None = None,
        *,
        java_mapper: JavaSymbolMapper | None = None,
        api_mapper: ApiContractMapper | None = None,
        risk_engine: RiskEngine | None = None,
        investigator: BoundedInvestigationAgent | None = None,
        verification_planner: VerificationPlanner | None = None,
        verification_executor: VerificationExecutorPort | None = None,
    ) -> None:
        self.store = store
        self.tools = tools or LocalTools(store)
        self.java_mapper = java_mapper or JavaSymbolMapper()
        self.api_mapper = api_mapper or ApiContractMapper()
        self.risk_engine = risk_engine or RiskEngine()
        self.investigator = investigator
        self.verification_planner = verification_planner or VerificationPlanner()
        self.verification_executor = verification_executor or SafeVerificationExecutor()

    def analyze(
        self, *, repo_path: str, base: str, head: str
    ) -> ChangeGuardAnalysis:
        trace: list[ChangeStageTrace] = []
        uncertainties: list[str] = []
        analysis_id = str(uuid4())

        git_started = time.perf_counter()
        try:
            repository_status = self.tools.repo_status(repo_path)
            diff = self.tools.git_diff(repo_path, base, head)
            recent_commits = self.tools.recent_commits(repo_path, 10, head)
        except (MCPToolError, RuntimeError, ValueError) as error:
            trace.append(self._trace("git_change", "failed", git_started, str(error)))
            raise ChangeAnalysisError(str(error), tuple(trace)) from error
        trace.append(
            self._trace(
                "git_change",
                "completed",
                git_started,
                counters={
                    "changed_files": len(diff.changed_files),
                    "recent_commits": len(recent_commits),
                    "working_tree_modified_files": len(repository_status.modified_files),
                },
            )
        )
        if repository_status.head_commit != head:
            uncertainties.append(
                "The checked-out HEAD differs from the requested analysis head; "
                "repository status describes the checkout while the diff and Java snapshot use the requested commits."
            )
        if repository_status.modified_files:
            uncertainties.append(
                f"The working tree has {len(repository_status.modified_files)} uncommitted file(s); "
                "they are not included in the requested commit-to-commit analysis."
            )
        diff_is_truncated = "[diff truncated]" in diff.diff_text
        if diff_is_truncated:
            uncertainties.append(
                "Git diff text was truncated; changed-line mapping and diff-dependent analysis may be incomplete."
            )

        changed_lines = self._parse_changed_lines(diff.diff_text, diff.changed_files)
        api_changed_lines = self._parse_added_lines(diff.diff_text)
        java_changed_files = tuple(
            path for path in diff.changed_files if path.endswith(".java")
        )
        frontend_changed_files = tuple(
            path for path in diff.changed_files if self._is_frontend_source(path)
        )
        api_relevant_change = bool(java_changed_files or frontend_changed_files)
        code_analysis: JavaAnalysis | None = None
        blast_radius: BlastRadiusResult | None = None
        api_impact: ApiImpact | None = None
        changed_symbols = ()
        file_level_changes: tuple[str, ...] = ()

        if api_relevant_change:
            snapshot_started = time.perf_counter()
            try:
                snapshot = self.tools.java_sources(repo_path, head)
            except (MCPToolError, RuntimeError, ValueError) as error:
                trace.append(
                    self._trace("java_source_snapshot", "unavailable", snapshot_started, str(error))
                )
                uncertainties.append(f"Java source snapshot unavailable: {error}")
                trace.append(
                    ChangeStageTrace(
                        "api_analysis", "unavailable", 0.0,
                        "Java endpoint providers could not be inspected at the pinned revision.",
                    )
                )
            else:
                trace.append(
                    self._trace(
                        "java_source_snapshot",
                        "completed" if snapshot.omitted_files == 0 else "unavailable",
                        snapshot_started,
                        (
                            f"{snapshot.omitted_files} source file(s) omitted by snapshot limits"
                            if snapshot.omitted_files
                            else None
                        ),
                        counters={
                            "code_files_analyzed": len(snapshot.sources),
                            "code_files_omitted": snapshot.omitted_files,
                        },
                    )
                )
                intelligence_started = time.perf_counter()
                code_analysis = self.java_mapper.analyze(
                    snapshot.sources,
                    changed_lines,
                    diff.changed_files,
                    omitted_sources=snapshot.omitted_files,
                )
                if diff_is_truncated:
                    file_level_changes = tuple(sorted(java_changed_files))
                trace.append(
                    self._trace(
                        "java_code_intelligence",
                        "completed"
                        if not code_analysis.parse_error_files
                        and not code_analysis.omitted_sources
                        and not code_analysis.file_level_changes
                        and not diff_is_truncated
                        else "unavailable",
                        intelligence_started,
                        (
                            "; ".join(
                                (*code_analysis.uncertainties, "Git diff text was truncated")
                                if diff_is_truncated
                                else code_analysis.uncertainties
                            )
                            if code_analysis.uncertainties or diff_is_truncated
                            else None
                        ),
                        counters={
                            "code_files_analyzed": len(snapshot.sources),
                            "symbols_detected": len(code_analysis.symbols),
                            "edges_detected": len(code_analysis.call_edges),
                            "unresolved_edges": code_analysis.unresolved_relationships,
                        },
                    )
                )
                for stage in ("symbol_mapping", "call_graph", "blast_radius"):
                    duration = code_analysis.phase_durations_ms.get(stage, 0.0)
                    stage_incomplete = bool(
                        code_analysis.parse_error_files
                        or code_analysis.omitted_sources
                        or diff_is_truncated
                        or (
                            stage == "symbol_mapping"
                            and code_analysis.file_level_changes
                        )
                        or (
                            stage in {"call_graph", "blast_radius"}
                            and code_analysis.unresolved_relationships
                        )
                        or (stage == "blast_radius" and not code_analysis.changed_symbol_ids)
                    )
                    stage_status = "unavailable" if stage_incomplete else "completed"
                    stage_failure = "; ".join(code_analysis.uncertainties) or None
                    stage_counters = {
                        "symbols_detected": len(code_analysis.symbols),
                        "edges_detected": len(code_analysis.call_edges),
                        "unresolved_edges": code_analysis.unresolved_relationships,
                    }
                    if stage == "symbol_mapping":
                        stage_counters["changed_symbols"] = len(code_analysis.changed_symbol_ids)
                        stage_counters["unmapped_changes"] = len(code_analysis.file_level_changes)
                    elif stage == "blast_radius":
                        stage_counters["affected_symbols"] = len(
                            code_analysis.direct_dependents + code_analysis.indirect_dependents
                        )
                    trace.append(
                        ChangeStageTrace(stage, stage_status, duration, stage_failure, stage_counters)
                    )
                by_id = {item.symbol_id: item for item in code_analysis.symbols}
                changed_symbols = tuple(
                    by_id[item]
                    for item in code_analysis.changed_symbol_ids
                    if item in by_id
                )
                if not diff_is_truncated:
                    file_level_changes = code_analysis.file_level_changes
                uncertainties.extend(code_analysis.uncertainties)
                if snapshot.omitted_files:
                    uncertainties.append(
                        "Java code analysis is incomplete because the source snapshot was bounded."
                    )

                api_started = time.perf_counter()
                try:
                    frontend_sources, omitted_frontend_files = self._frontend_sources(
                        repo_path, snapshot.revision
                    )
                except (MCPToolError, RuntimeError, ValueError) as error:
                    uncertainties.append(f"API source snapshot unavailable: {error}")
                    trace.append(
                        self._trace("api_analysis", "unavailable", api_started, str(error))
                    )
                else:
                    api_impact = self.api_mapper.analyze(
                        java_sources=snapshot.sources,
                        frontend_sources=frontend_sources,
                        symbols=code_analysis.symbols,
                        revision=snapshot.revision,
                        changed_files=tuple(diff.changed_files),
                        changed_lines=api_changed_lines,
                        file_level_changes=file_level_changes,
                        omitted_frontend_files=omitted_frontend_files,
                    )
                    api_incomplete = bool(
                        snapshot.omitted_files
                        or omitted_frontend_files
                        or code_analysis.parse_error_files
                    )
                    if code_analysis.parse_error_files:
                        api_impact = replace(
                            api_impact,
                            uncertainties=tuple(
                                dict.fromkeys(
                                    (
                                        *api_impact.uncertainties,
                                        "Spring endpoint extraction may be incomplete because some Java files did not parse.",
                                    )
                                )
                            ),
                        )
                    trace.append(
                        self._trace(
                            "api_analysis",
                            "unavailable" if api_incomplete else "completed",
                            api_started,
                            "; ".join(api_impact.uncertainties) or None,
                            counters={
                                "endpoints_discovered": api_impact.metrics.endpoints_discovered,
                                "frontend_files_analyzed": api_impact.metrics.frontend_files_analyzed,
                                "consumers_discovered": api_impact.metrics.consumers_discovered,
                                "deterministic_matches": api_impact.metrics.deterministic_matches,
                                "ambiguous_matches": api_impact.metrics.ambiguous_matches,
                                "unresolved_consumers": api_impact.metrics.unresolved_consumers,
                                "api_consumer_edges": api_impact.metrics.api_consumer_edges,
                            },
                        )
                    )
                    uncertainties.extend(api_impact.uncertainties)
                blast_radius = self._blast_radius(
                    code_analysis,
                    changed_symbols,
                    analysis_id,
                    api_impact=api_impact,
                    incomplete=diff_is_truncated,
                )
        else:
            trace.append(ChangeStageTrace("java_source_snapshot", "skipped", 0.0))
            trace.append(ChangeStageTrace("java_code_intelligence", "skipped", 0.0))
            trace.append(ChangeStageTrace("symbol_mapping", "skipped", 0.0))
            trace.append(ChangeStageTrace("call_graph", "skipped", 0.0))
            trace.append(ChangeStageTrace("blast_radius", "skipped", 0.0))
            trace.append(ChangeStageTrace("api_analysis", "skipped", 0.0))

        history_started = time.perf_counter()
        history: EvidenceBundle | None
        try:
            query_state = AnalysisState(
                repo_path,
                base,
                head,
                diff.changed_files,
                {"additions": diff.additions, "deletions": diff.deletions},
                list(recent_commits),
                [],
                [],
                "in_progress",
            )
            from changeguard.evidence.query import build_change_query

            query = build_change_query(query_state, diff.diff_text)
            history = self.tools.search_evidence(query, 5, repository=repo_path)
        except (MCPToolError, RuntimeError, sqlite3.Error, ValueError) as error:
            history = None
            historical_signals = HistoricalEvidenceSignals("unknown")
            uncertainties.append(f"Historical evidence retrieval failed: {error}")
            trace.append(
                self._trace(
                    "historical_evidence", "failed", history_started, str(error),
                    counters={"historical_results": 0},
                )
            )
        else:
            has_results = bool(history.results)
            historical_signals = HistoricalEvidenceSignals(
                state="known" if has_results else "uncertain",
                matches=tuple(
                    HistoricalEvidenceMatch(
                        item.evidence_id, item.source_type, item.similarity_score
                    )
                    for item in history.results
                    if item.source_type != "unknown"
                ),
                evidence_refs=tuple(
                    dict.fromkeys(item.evidence_id for item in history.results)
                ),
                coverage=1.0 if has_results else 0.0,
            )
            trace.append(
                self._trace(
                    "historical_evidence",
                    "completed",
                    history_started,
                    None if has_results else "NO_RELEVANT_EVIDENCE",
                    counters={"historical_results": len(history.results)},
                )
            )
            if not has_results:
                uncertainties.append("NO_RELEVANT_EVIDENCE")
            if any(item.source_type == "unknown" for item in history.results):
                historical_signals = HistoricalEvidenceSignals(
                    "uncertain",
                    historical_signals.matches,
                    historical_signals.evidence_refs,
                    0.0,
                )
                uncertainties.append(
                    "Some retrieved evidence did not include a trustworthy source type."
                )

        test_signals = TestSignals("unknown")
        runtime_signals = RuntimeSignals("unknown")
        uncertainties.extend(
            (
                "Test signal is UNKNOWN: no trusted test-result importer is configured.",
                "Runtime signal is UNKNOWN: no trusted runtime telemetry source is configured.",
            )
        )

        change_metrics = self._change_metrics(
            diff,
            changed_lines,
            code_analysis,
            file_level_changes,
            analysis_id,
            diff_is_truncated,
        )
        risk_started = time.perf_counter()
        risk = self.risk_engine.assess(
            ChangeContext(
                change=change_metrics,
                blast_radius=blast_radius.signals if blast_radius else None,
                historical_evidence=historical_signals,
                tests=test_signals,
                runtime=runtime_signals,
            )
        )
        trace.append(
            ChangeStageTrace(
                "verification_planning",
                "skipped",
                0.0,
                "Verification is opt-in; no checks are executed during impact analysis.",
            )
        )
        trace.append(
            self._trace(
                "risk_engine",
                "completed",
                risk_started,
                counters={
                    "risk_factors": len(risk.factors),
                    "known_risk_factors": sum(item.state == "known" for item in risk.factors),
                },
            )
        )
        uncertainties.extend(risk.uncertainties)

        report = None
        investigation_state = None
        investigation_error = None
        report_generation_duration_ms = 0.0
        if self.investigator is None:
            trace.append(ChangeStageTrace("llm_investigation", "skipped", 0.0))
            trace.append(ChangeStageTrace("report_generation", "skipped", 0.0))
        else:
            llm_started = time.perf_counter()
            task = self._investigation_task(
                repo_path, base, head, diff, changed_symbols, blast_radius, risk,
                api_impact,
            )
            try:
                generated: ReportGenerationResult = self.investigator.investigate_and_report(
                    task, repo_path=repo_path, base=base, head=head
                )
                report = generated.report
                if report is not None and api_impact is not None:
                    report = replace(report, api_impact=api_impact.to_dict())
                investigation_state = generated.investigation
                investigation_error = generated.error
                report_generation_duration_ms = (
                    generated.report_generation_duration_ms or 0.0
                )
                if investigation_error:
                    uncertainties.append(
                        f"LLM investigation/report unavailable: {investigation_error}"
                    )
            except (OllamaError, MCPToolError, ValueError, TypeError) as error:
                investigation_error = str(error)
                uncertainties.append(f"LLM investigation/report unavailable: {error}")
            total_llm_duration = (time.perf_counter() - llm_started) * 1000
            trace.append(
                ChangeStageTrace(
                    "llm_investigation",
                    "completed" if investigation_state is not None else "unavailable",
                    round(max(0.0, total_llm_duration - report_generation_duration_ms), 3),
                    investigation_error,
                    {
                        "tool_calls": (
                            len(investigation_state.tool_calls)
                            if investigation_state is not None else 0
                        ),
                        "report_findings": len(report.findings) if report else 0,
                    },
                )
            )
            trace.append(
                ChangeStageTrace(
                    "report_generation",
                    "completed" if report is not None else "unavailable",
                    report_generation_duration_ms,
                    investigation_error,
                    {"report_findings": len(report.findings) if report else 0},
                )
            )

        summary = self._change_summary(diff, java_changed_files, changed_symbols)
        recommendations = list(risk.recommendations)
        if report:
            recommendations.extend(report.recommended_verification)
        if test_signals.state == "unknown":
            recommendations.append("Run the relevant automated test suites and record their results.")
        if runtime_signals.state == "unknown":
            recommendations.append(
                "Review service telemetry or operational dashboards if runtime impact is in scope."
            )

        result = ChangeGuardAnalysis(
            analysis_id=analysis_id,
            repository=repo_path,
            change_reference=f"{base}..{head}",
            change_summary=summary,
            repository_status=repository_status,
            changed_files=tuple(diff.changed_files),
            changed_lines=changed_lines,
            changed_symbols=changed_symbols,
            file_level_changes=file_level_changes,
            recent_commits=tuple(recent_commits),
            code_analysis=code_analysis,
            blast_radius=blast_radius,
            historical_evidence=history,
            test_signals=test_signals,
            runtime_signals=runtime_signals,
            risk_assessment=risk,
            investigation_report=report,
            api_impact=api_impact,
            investigation_state=investigation_state,
            investigation_error=investigation_error,
            uncertainties=tuple(dict.fromkeys(uncertainties)),
            recommended_verification=tuple(dict.fromkeys(recommendations)),
            trace=tuple(trace),
            initial_risk_assessment=risk,
        )
        persistence_started = time.perf_counter()
        self.store.save_change_analysis(result.analysis_id, repo_path, result.to_dict())
        result = replace(
            result,
            trace=(
                *result.trace,
                ChangeStageTrace(
                    "persistence",
                    "completed",
                    round((time.perf_counter() - persistence_started) * 1000, 3),
                    counters={"analysis_records": 1, "persistence_upserts": 2},
                ),
            ),
        )
        self.store.save_change_analysis(result.analysis_id, repo_path, result.to_dict())
        return result

    def plan_verification(
        self, analysis: ChangeGuardAnalysis
    ) -> VerificationPlan:
        plan_started = time.perf_counter()
        plan = self.verification_planner.plan(analysis)
        updated = self._with_verification_plan(
            analysis, plan, (time.perf_counter() - plan_started) * 1000
        )
        persistence_started = time.perf_counter()
        self.store.save_change_analysis(updated.analysis_id, updated.repository, updated.to_dict())
        updated = replace(
            updated,
            trace=(
                *updated.trace,
                ChangeStageTrace(
                    "persistence",
                    "completed",
                    round((time.perf_counter() - persistence_started) * 1000, 3),
                    counters={"analysis_records": 1, "persistence_upserts": 2},
                ),
            ),
        )
        self.store.save_change_analysis(updated.analysis_id, updated.repository, updated.to_dict())
        return plan

    def verify(self, analysis: ChangeGuardAnalysis) -> ChangeGuardAnalysis:
        if analysis.verification_plan is None:
            plan_started = time.perf_counter()
            plan = self.verification_planner.plan(analysis)
            analysis = self._with_verification_plan(
                analysis, plan, (time.perf_counter() - plan_started) * 1000
            )
        else:
            plan = analysis.verification_plan
        verification = self.verification_executor.execute(plan)
        test_signals = self._test_signals(verification)
        verified_risk = self.risk_engine.assess(
            self._risk_context(analysis, test_signals)
        )
        uncertainties = list(analysis.uncertainties)
        uncertainties.extend(verification.limitations)
        if verification.status != "PASSED":
            uncertainties.append(
                f"Verification status is {verification.status}; checks do not establish a clean pass."
            )
        if test_signals.state == "unknown":
            uncertainties.append(
                "No complete pytest outcome counts were available; test risk remains UNKNOWN."
            )
        recommendations = list(analysis.recommended_verification)
        if verification.failures:
            recommendations.extend(
                f"Investigate verification failure: {failure}"
                for failure in verification.failures
            )
        report = analysis.investigation_report
        if report is not None:
            report = replace(
                report,
                uncertainties=list(
                    dict.fromkeys(
                        (*report.uncertainties, *verification.limitations)
                    )
                ),
                verification={
                    "status": verification.status,
                    "checks": [
                        {
                            "check_id": item.check.check_id,
                            "operation": item.check.operation,
                            "status": item.status,
                            "revision": item.evidence.revision,
                            "failure_summary": item.failure_summary,
                        }
                        for item in verification.checks
                    ],
                    "limitations": list(verification.limitations),
                    "initial_risk": {
                        "score": (
                            analysis.initial_risk_assessment.score
                            if analysis.initial_risk_assessment
                            else analysis.risk_assessment.score
                        ),
                        "level": (
                            analysis.initial_risk_assessment.overall_level
                            if analysis.initial_risk_assessment
                            else analysis.risk_assessment.overall_level
                        ),
                    },
                    "verified_risk": {
                        "score": verified_risk.score,
                        "level": verified_risk.overall_level,
                    },
                },
            )

        result = replace(
            analysis,
            test_signals=test_signals,
            risk_assessment=verified_risk,
            initial_risk_assessment=(
                analysis.initial_risk_assessment or analysis.risk_assessment
            ),
            verification_plan=plan,
            verification_result=verification,
            investigation_report=report,
            uncertainties=tuple(dict.fromkeys(uncertainties)),
            recommended_verification=tuple(dict.fromkeys(recommendations)),
            trace=(
                *analysis.trace,
                ChangeStageTrace(
                    "verification",
                    "completed" if verification.status == "PASSED" else "unavailable",
                    verification.duration_seconds * 1000,
                    "; ".join(verification.failures) or None,
                    {
                        "verification_checks": len(verification.checks),
                        "verification_passed": sum(item.status == "PASSED" for item in verification.checks),
                        "verification_failed": sum(item.status == "FAILED" for item in verification.checks),
                        "verification_blocked": sum(item.status == "BLOCKED" for item in verification.checks),
                    },
                ),
                ChangeStageTrace("verified_risk_engine", "completed", 0.0),
            ),
        )
        persistence_started = time.perf_counter()
        self.store.save_change_analysis(result.analysis_id, result.repository, result.to_dict())
        result = replace(
            result,
            trace=(
                *result.trace,
                ChangeStageTrace(
                    "persistence",
                    "completed",
                    round((time.perf_counter() - persistence_started) * 1000, 3),
                    counters={"analysis_records": 1, "persistence_upserts": 2},
                ),
            ),
        )
        self.store.save_change_analysis(result.analysis_id, result.repository, result.to_dict())
        return result

    def _with_verification_plan(
        self,
        analysis: ChangeGuardAnalysis,
        plan: VerificationPlan,
        duration_ms: float,
    ) -> ChangeGuardAnalysis:
        trace = tuple(
            item for item in analysis.trace if item.stage != "verification_planning"
        )
        return replace(
            analysis,
            verification_plan=plan,
            trace=(
                *trace,
                ChangeStageTrace(
                    "verification_planning",
                    "completed" if plan.status == "READY" else "unavailable",
                    round(duration_ms, 3),
                    "; ".join(plan.limitations) or None,
                ),
            ),
        )

    def _test_signals(self, result: VerificationResult) -> TestSignals:
        pytest_results = [
            check
            for check in result.checks
            if check.check.operation == "PYTHON_PYTEST"
        ]
        if not pytest_results:
            return TestSignals("unknown")
        output = "\n".join(check.output_summary for check in pytest_results)
        counts = {
            name: sum(int(value) for value in re.findall(rf"\b(\d+)\s+{name}\b", output))
            for name in ("passed", "failed", "error", "skipped")
        }
        if not any(counts.values()):
            return TestSignals(
                "unknown",
                evidence_refs=tuple(check.evidence.check_id for check in pytest_results),
            )
        failed = counts["failed"] + counts["error"]
        total = counts["passed"] + failed + counts["skipped"]
        if total <= 0:
            return TestSignals("unknown")
        return TestSignals(
            state="known" if result.status in {"PASSED", "FAILED"} else "uncertain",
            tests_affected=total,
            tests_passed=counts["passed"],
            tests_failed=failed,
            tests_unavailable=counts["skipped"],
            evidence_refs=tuple(check.evidence.check_id for check in pytest_results),
            coverage=1.0 if result.status in {"PASSED", "FAILED"} else 0.5,
        )

    def _risk_context(
        self, analysis: ChangeGuardAnalysis, tests: TestSignals
    ) -> ChangeContext:
        initial = analysis.initial_risk_assessment or analysis.risk_assessment
        complexity = next(
            factor for factor in initial.factors if factor.factor == "change_complexity"
        )
        values = complexity.value
        change = ChangeMetrics(
            files_changed=values.get("files_changed"),
            lines_added=values.get("lines_added"),
            lines_deleted=values.get("lines_deleted"),
            symbols_changed=values.get("symbols_changed"),
            methods_changed=values.get("methods_changed"),
            java_types_changed=values.get("java_types_changed"),
            changed_file_concentration=values.get("changed_file_concentration"),
            evidence_refs=complexity.evidence_refs,
        )
        history = analysis.historical_evidence
        if history is None:
            historical = HistoricalEvidenceSignals("unknown")
        else:
            matches = tuple(
                HistoricalEvidenceMatch(
                    item.evidence_id, item.source_type, item.similarity_score
                )
                for item in history.results
                if item.source_type != "unknown"
            )
            historical = HistoricalEvidenceSignals(
                state="known" if history.results else "uncertain",
                matches=matches,
                evidence_refs=tuple(dict.fromkeys(item.evidence_id for item in history.results)),
                coverage=1.0 if history.results else 0.0,
            )
        return ChangeContext(
            change=change,
            blast_radius=(
                analysis.blast_radius.signals if analysis.blast_radius else None
            ),
            historical_evidence=historical,
            tests=tests,
            runtime=analysis.runtime_signals,
        )

    def _blast_radius(
        self,
        analysis: JavaAnalysis | None,
        changed_symbols: tuple,
        analysis_id: str,
        *,
        api_impact: ApiImpact | None = None,
        incomplete: bool = False,
    ) -> BlastRadiusResult:
        java_direct = analysis.direct_dependents if analysis else ()
        java_indirect = analysis.indirect_dependents if analysis else ()
        has_seeds = bool(
            changed_symbols
            or (
                api_impact
                and (api_impact.changed_endpoint_ids or api_impact.changed_consumer_ids)
            )
        )
        incomplete = bool(
            incomplete
            or (
                analysis
                and (
                    analysis.parse_error_files
                    or analysis.omitted_sources
                    or analysis.file_level_changes
                )
            )
        )
        api_edges = api_impact.edges if api_impact else ()
        api_consumers = (
            set(api_impact.impacted_consumer_ids)
            | set(api_impact.changed_consumer_ids)
            if api_impact
            else set()
        )
        api_providers = (
            set(api_impact.providers_for_changed_consumers) if api_impact else set()
        )
        changed_endpoint_ids = set(api_impact.changed_endpoint_ids) if api_impact else set()
        changed_consumer_ids = set(api_impact.changed_consumer_ids) if api_impact else set()
        api_issue_is_relevant = lambda item: (
            item.consumer_id in changed_consumer_ids
            or bool(changed_endpoint_ids.intersection(item.candidate_endpoint_ids))
        )
        relevant_ambiguous = (
            [item for item in api_impact.ambiguous_relationships if api_issue_is_relevant(item)]
            if api_impact else []
        )
        relevant_unresolved = (
            [item for item in api_impact.unresolved_relationships if api_issue_is_relevant(item)]
            if api_impact else []
        )
        relevant_api_issues = [*relevant_ambiguous, *relevant_unresolved]
        api_issue_count = len(relevant_api_issues)
        java_unresolved = analysis.unresolved_relationships if analysis else 0
        unresolved = None if incomplete or not has_seeds else java_unresolved + api_issue_count
        endpoint_by_id = (
            {item.endpoint_id: item for item in api_impact.endpoints}
            if api_impact
            else {}
        )
        consumer_by_id = (
            {item.consumer_id: item for item in api_impact.consumers}
            if api_impact
            else {}
        )
        api_files = {
            consumer_by_id[item].source_file
            for item in api_consumers
            if item in consumer_by_id
        }
        provider_ids = api_providers
        api_files.update(
            endpoint_by_id[edge.endpoint_id].source_file
            for edge in api_edges
            if edge.source_node in provider_ids and edge.endpoint_id in endpoint_by_id
        )
        java_affected_files = set(analysis.affected_files) if analysis else set()
        affected_files = tuple(sorted(java_affected_files | api_files))
        affected_packages = analysis.affected_packages if analysis else ()
        java_depth = analysis.maximum_dependency_depth if analysis else None
        java_unresolved_count = analysis.unresolved_relationships if analysis else 0
        uncertainties = list(analysis.uncertainties if analysis else ())
        if api_impact:
            uncertainties.extend(api_impact.uncertainties)
        signal = BlastRadiusSignals(
            directly_affected_symbols=(
                len(java_direct) if has_seeds and not incomplete else None
            ),
            indirectly_affected_symbols=(
                len(java_indirect) if has_seeds and not incomplete else None
            ),
            affected_files=len(affected_files) if has_seeds and not incomplete else None,
            dependency_depth=(
                java_depth if has_seeds and not incomplete else None
            ),
            affected_callers=(
                len(set(java_direct + java_indirect)) + len(api_consumers) + len(api_providers)
                if has_seeds and not incomplete
                else None
            ),
            affected_components=None,
            unresolved_relationships=unresolved,
            evidence_refs=(
                f"analysis:{analysis_id}:java-call-graph",
                *((f"analysis:{analysis_id}:api-impact",) if api_impact else ()),
            ),
        )
        if not has_seeds:
            uncertainties.append(
                "No changed Java symbol or API relationship was mapped; numeric blast-radius counts are unknown."
            )
        if affected_packages:
            uncertainties.append(
                "Package names are shown as code groupings; repository component boundaries are not modeled."
            )
        return BlastRadiusResult(
            signals=signal,
            direct_dependents=java_direct,
            indirect_dependents=java_indirect,
            affected_files=affected_files,
            affected_packages=affected_packages,
            maximum_dependency_depth=java_depth,
            unresolved_relationships=java_unresolved_count if analysis else None,
            uncertainties=tuple(dict.fromkeys(uncertainties)),
            cross_language_api_consumers=tuple(sorted(api_consumers)),
            api_edges=api_edges,
            api_unresolved_relationships=(
                len(relevant_api_issues) if api_impact is not None else None
            ),
            api_ambiguous_relationships=(
                len(relevant_ambiguous) if api_impact else 0
            ),
            api_provider_symbols_for_changed_consumers=tuple(sorted(api_providers)),
        )

    def _change_metrics(
        self,
        diff: DiffResult,
        changed_lines: tuple[ChangedLineRange, ...],
        analysis: JavaAnalysis | None,
        file_level_changes: tuple[str, ...],
        analysis_id: str,
        diff_is_truncated: bool,
    ) -> ChangeMetrics:
        java_files = [path for path in diff.changed_files if path.endswith(".java")]
        symbol_count: int | None
        method_count: int | None
        type_count: int | None
        if analysis is None:
            symbol_count = method_count = type_count = 0 if not java_files else None
        elif file_level_changes or diff_is_truncated:
            symbol_count = method_count = type_count = None
        else:
            changed_ids = set(analysis.changed_symbol_ids)
            changed = [item for item in analysis.symbols if item.symbol_id in changed_ids]
            symbol_count = len(changed)
            method_count = sum(item.kind in {"method", "constructor"} for item in changed)
            type_count = sum(item.kind == "type" for item in changed)

        distribution = self._changed_line_counts(diff.diff_text)
        total = sum(distribution.values())
        concentration = max(distribution.values()) / total if total else None
        return ChangeMetrics(
            files_changed=len(diff.changed_files),
            lines_added=diff.additions,
            lines_deleted=diff.deletions,
            symbols_changed=symbol_count,
            methods_changed=method_count,
            java_types_changed=type_count,
            changed_file_concentration=concentration,
            evidence_refs=(f"git:{diff.changed_files!r}:{analysis_id}",),
        )

    def _parse_changed_lines(
        self, diff_text: str, changed_files: list[str]
    ) -> tuple[ChangedLineRange, ...]:
        ranges: list[ChangedLineRange] = []
        old_path: str | None = None
        current_path: str | None = None
        hunk_pattern = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
        for line in diff_text.splitlines():
            if line.startswith("--- "):
                candidate = line[4:]
                old_path = candidate[2:] if candidate.startswith("a/") else None
            elif line.startswith("+++ "):
                candidate = line[4:]
                current_path = candidate[2:] if candidate.startswith("b/") else old_path
            elif line.startswith("@@ ") and current_path:
                match = hunk_pattern.match(line)
                if match:
                    old_start, old_count, new_start, new_count = match.groups()
                    ranges.append(
                        ChangedLineRange(
                            current_path,
                            int(old_start),
                            int(old_count or 1),
                            int(new_start),
                            int(new_count or 1),
                        )
                    )
        if not ranges and changed_files and diff_text:
            return ()
        return tuple(ranges)

    def _parse_added_lines(self, diff_text: str) -> tuple[ChangedLineRange, ...]:
        ranges: list[ChangedLineRange] = []
        old_path: str | None = None
        current_path: str | None = None
        new_line: int | None = None
        old_remaining: int | None = None
        new_remaining: int | None = None
        hunk_pattern = re.compile(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
        for line in diff_text.splitlines():
            if old_remaining is None and line.startswith("--- "):
                candidate = line[4:]
                old_path = candidate[2:] if candidate.startswith("a/") else None
                current_path = None
                new_line = None
            elif old_remaining is None and line.startswith("+++ "):
                candidate = line[4:]
                current_path = candidate[2:] if candidate.startswith("b/") else old_path
            elif old_remaining is None and line.startswith("@@ ") and current_path:
                match = hunk_pattern.match(line)
                if match:
                    old_remaining = int(match.group(1) or 1)
                    new_line = int(match.group(2))
                    new_remaining = int(match.group(3) or 1)
                    if old_remaining == 0 and new_remaining == 0:
                        old_remaining = None
                        new_remaining = None
                        new_line = None
            elif old_remaining is not None and new_remaining is not None:
                if line.startswith("\\"):
                    continue
                if line.startswith("+"):
                    if current_path is not None and new_line is not None:
                        ranges.append(
                            ChangedLineRange(
                                current_path, new_line, 1, new_line, 1
                            )
                        )
                    if new_line is not None:
                        new_line += 1
                    new_remaining -= 1
                elif line.startswith("-"):
                    old_remaining -= 1
                elif line.startswith(" "):
                    if new_line is not None:
                        new_line += 1
                    old_remaining -= 1
                    new_remaining -= 1
                if old_remaining == 0 and new_remaining == 0:
                    old_remaining = None
                    new_remaining = None
                    new_line = None
        return tuple(ranges)

    def _changed_line_counts(self, diff_text: str) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        current_path: str | None = None
        old_path: str | None = None
        for line in diff_text.splitlines():
            if line.startswith("--- "):
                candidate = line[4:]
                old_path = candidate[2:] if candidate.startswith("a/") else None
            elif line.startswith("+++ "):
                candidate = line[4:]
                current_path = candidate[2:] if candidate.startswith("b/") else old_path
            elif current_path and line.startswith(("+", "-")):
                if not line.startswith(("+++", "---")):
                    counts[current_path] += 1
        return counts

    def _investigation_task(
        self,
        repo_path: str,
        base: str,
        head: str,
        diff: DiffResult,
        changed_symbols: tuple,
        blast_radius: BlastRadiusResult | None,
        risk,
        api_impact: ApiImpact | None = None,
    ) -> str:
        compact_context = {
            "changed_files": diff.changed_files[:50],
            "changed_symbols": [
                item.qualified_name for item in changed_symbols[:50]
            ],
            "blast_radius": (
                {
                    "direct": blast_radius.signals.directly_affected_symbols,
                    "indirect": blast_radius.signals.indirectly_affected_symbols,
                    "cross_language_api_consumers": len(
                        blast_radius.cross_language_api_consumers
                    ),
                    "unresolved": blast_radius.unresolved_relationships,
                }
                if blast_radius
                else None
            ),
            "api_impact": (
                {
                    "endpoints": [
                        {
                            "method": item.http_method,
                            "route": item.route,
                            "provider": item.declaring_method,
                        }
                        for item in api_impact.endpoints[:50]
                    ],
                    "deterministic_edges": [
                        {
                            "relationship": item.relationship_type,
                            "endpoint": item.endpoint_identity,
                            "consumer": item.target_node,
                            "source_file": next(
                                (
                                    consumer.source_file
                                    for consumer in api_impact.consumers
                                    if consumer.consumer_id == item.target_node
                                ),
                                None,
                            ),
                        }
                        for item in api_impact.edges[:50]
                    ],
                    "ambiguous_matches": len(api_impact.ambiguous_relationships),
                    "unresolved_consumers": len(api_impact.unresolved_relationships),
                }
                if api_impact
                else None
            ),
            "deterministic_risk": {
                "level": risk.overall_level,
                "score": risk.score,
                "uncertainties": list(risk.uncertainties),
            },
        }
        return (
            "Investigate this change using available read-only tools. Explain only "
            "what the returned tool results support. The deterministic risk assessment "
            "below is authoritative and must not be changed or recomputed. Treat the "
            "supplied code-analysis context as deterministic input, not as tool output. "
            "API_CONSUMER edges are deterministic and authoritative: describe only "
            "listed edges and do not infer additional API relationships. "
            "State unknowns and do not claim tests or runtime checks were performed.\n"
            f"Repository: {repo_path}\nChange: {base}..{head}\n"
            f"Deterministic context: {compact_context}"
        )

    def _frontend_sources(
        self, repo_path: str, revision: str
    ) -> tuple[dict[str, str], int]:
        paths = self.tools.revision_files(repo_path, revision, max_files=10_000)
        candidates = [
            path for path in paths
            if self._is_frontend_source(path)
            and not self._excluded_frontend_source(path)
        ]
        selected: dict[str, str] = {}
        omitted = 0
        total_bytes = 0
        for index, path in enumerate(candidates):
            if len(selected) >= self.api_mapper.max_frontend_files:
                omitted += len(candidates) - index
                break
            try:
                source = self.tools.revision_file(
                    repo_path, revision, path,
                    max_bytes=self.api_mapper.max_file_bytes,
                )
            except ValueError as error:
                if "exceeds" not in str(error):
                    raise
                omitted += 1
                continue
            size = len(source.encode("utf-8"))
            if total_bytes + size > self.api_mapper.max_total_bytes:
                omitted += len(candidates) - index
                break
            selected[path] = source
            total_bytes += size
        return selected, omitted

    @staticmethod
    def _is_frontend_source(path: str) -> bool:
        return path.casefold().endswith(
            (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
        )

    @staticmethod
    def _excluded_frontend_source(path: str) -> bool:
        parts = {part.casefold() for part in path.split("/")}
        filename = path.rsplit("/", 1)[-1].casefold()
        return bool(
            parts
            & {
                ".git", ".next", ".nuxt", "build", "coverage", "dist",
                "generated", "node_modules", "out", "target", "vendor",
            }
        ) or any(marker in filename for marker in (".min.", ".bundle.", ".chunk."))

    def _change_summary(
        self, diff: DiffResult, java_files: tuple[str, ...], changed_symbols: tuple
    ) -> str:
        summary = (
            f"{len(diff.changed_files)} file(s) changed "
            f"(+{diff.additions}/-{diff.deletions} lines)"
        )
        if java_files:
            summary += f"; {len(java_files)} Java file(s)"
        if changed_symbols:
            summary += f"; {len(changed_symbols)} Java symbol declaration(s) intersect changed lines"
        return summary + "."

    def _trace(
        self,
        stage: str,
        status: StageStatus,
        started: float,
        failure: str | None = None,
        counters: dict[str, int | float | str] | None = None,
    ) -> ChangeStageTrace:
        return ChangeStageTrace(
            stage,
            status,
            round((time.perf_counter() - started) * 1000, 3),
            failure,
            counters or {},
        )
