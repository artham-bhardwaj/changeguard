from __future__ import annotations

import json
import subprocess
import sys

from changeguard.analysis import ChangeAnalysisService, ChangeGuardAnalysis
from changeguard.code_intelligence import ChangedLineRange, JavaSymbolMapper
from changeguard.cli import main as cli_main
from changeguard.evidence.ingest import EvidenceIngestor
from changeguard.evidence.models import EvidenceDocument
from changeguard.llm.decision import InvestigationState, InvestigationToolCall
from changeguard.llm.ollama import OllamaConnectionError
from changeguard.llm.report import InvestigationReport, ReportGenerationResult
from changeguard.mcp.adapter import MCPToolPort
from changeguard.storage.sqlite import SQLiteAnalysisStore


def git(repo, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def java_repo(tmp_path, *, unknown_call: bool = False):
    repo = tmp_path / "java-repository"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    source_root = repo / "src" / "main" / "java" / "demo"
    source_root.mkdir(parents=True)
    repository_method = (
        "    String find() { Client client; client.unknown(); return \"before\"; }\n"
        if unknown_call
        else "    String find() { return \"before\"; }\n"
    )
    (source_root / "Repository.java").write_text(
        "package demo;\n"
        "class Repository {\n"
        + repository_method
        + "}\n",
        encoding="utf-8",
    )
    (source_root / "Service.java").write_text(
        "package demo;\n"
        "class Service {\n"
        "    private Repository repository;\n"
        "    String load() { return repository.find(); }\n"
        "}\n",
        encoding="utf-8",
    )
    controller_call = "service.load();"
    (source_root / "Controller.java").write_text(
        "package demo;\n"
        "class Controller {\n"
        "    private Service service;\n"
        f"    void get() {{ {controller_call} }}\n"
        "}\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial Java call chain")
    base = git(repo, "rev-parse", "HEAD")
    repository_file = source_root / "Repository.java"
    repository_file.write_text(
        repository_file.read_text(encoding="utf-8").replace('"before"', '"after"'),
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change repository result")
    head = git(repo, "rev-parse", "HEAD")
    return repo, base, head


def test_java_field_changes_map_when_line_data_exists_and_fall_back_when_deleted():
    path = "src/main/java/demo/Sample.java"
    source = "package demo;\nclass Sample {\n    int count = 1;\n    void run() {}\n}\n"
    mapper = JavaSymbolMapper()

    mapped = mapper.analyze(
        {path: source},
        [ChangedLineRange(path, 3, 1, 3, 1)],
        [path],
    )
    deleted = mapper.analyze(
        {path: source},
        [ChangedLineRange(path, 3, 1, 3, 0)],
        [path],
    )

    mapped_names = {
        symbol.qualified_name
        for symbol in mapped.symbols
        if symbol.symbol_id in mapped.changed_symbol_ids
    }
    assert "demo.Sample.count" in mapped_names
    assert path in deleted.file_level_changes
    assert deleted.changed_symbol_ids == ()


def test_end_to_end_java_change_builds_call_graph_and_blast_radius(tmp_path):
    repo, base, head = java_repo(tmp_path)
    store = SQLiteAnalysisStore(str(tmp_path / "analysis.sqlite3"))

    result = ChangeAnalysisService(store).analyze(
        repo_path=str(repo), base=base, head=head
    )

    changed_names = {item.qualified_name for item in result.changed_symbols}
    assert any(name.endswith(".find()") for name in changed_names)
    assert result.code_analysis is not None
    assert len(result.code_analysis.call_edges) == 2
    assert result.blast_radius is not None
    assert len(result.blast_radius.direct_dependents) == 1
    assert len(result.blast_radius.indirect_dependents) == 1
    assert result.blast_radius.signals.directly_affected_symbols == 1
    assert result.risk_assessment.overall_level in {"LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN"}
    assert result.test_signals.state == "unknown"
    assert result.runtime_signals.state == "unknown"
    assert result.historical_evidence is not None
    assert result.historical_evidence.status == "no_relevant_evidence"
    assert "NO_RELEVANT_EVIDENCE" in result.uncertainties
    assert [item.stage for item in result.trace][:4] == [
        "git_change",
        "java_source_snapshot",
        "java_code_intelligence",
        "symbol_mapping",
    ]
    restored = ChangeGuardAnalysis.from_dict(
        store.get_change_analysis(result.analysis_id) or {}
    )
    persisted = store.get_change_analysis(result.analysis_id)
    assert persisted == json.loads(json.dumps(result.to_dict()))
    assert restored.analysis_id == result.analysis_id
    assert restored.risk_assessment.score == result.risk_assessment.score


def test_non_java_change_skips_java_analysis_and_keeps_unknown_signals(
    git_repo, tmp_path
):
    repo, base, head = git_repo
    result = ChangeAnalysisService(
        SQLiteAnalysisStore(str(tmp_path / "analysis.sqlite3"))
    ).analyze(repo_path=str(repo), base=base, head=head)

    assert result.code_analysis is None
    assert result.blast_radius is None
    assert all(
        next(item for item in result.trace if item.stage == stage).status == "skipped"
        for stage in ("java_code_intelligence", "symbol_mapping", "call_graph", "blast_radius")
    )
    assert result.test_signals.state == "unknown"
    assert result.runtime_signals.state == "unknown"


def test_unresolved_java_calls_are_preserved_and_lower_graph_coverage(tmp_path):
    repo, base, head = java_repo(tmp_path, unknown_call=True)
    result = ChangeAnalysisService(
        SQLiteAnalysisStore(str(tmp_path / "analysis.sqlite3"))
    ).analyze(repo_path=str(repo), base=base, head=head)

    assert result.code_analysis is not None
    assert any(edge.method_name == "unknown" for edge in result.code_analysis.unresolved_calls)
    assert result.blast_radius is not None
    assert result.blast_radius.unresolved_relationships is not None
    assert result.blast_radius.unresolved_relationships >= 0
    assert any("unresolved call" in item for item in result.uncertainties)


def test_historical_evidence_is_attached_with_source_provenance(tmp_path):
    repo, base, head = java_repo(tmp_path)
    store = SQLiteAnalysisStore(str(tmp_path / "analysis.sqlite3"))
    document = EvidenceDocument(
        title="Repository incident",
        source_type="incident",
        source="incident-42",
        repository=str(repo),
        content="Repository.java changed the demo Repository find method after a similar service update.",
    )
    EvidenceIngestor(store).ingest(document)

    result = ChangeAnalysisService(store).analyze(
        repo_path=str(repo), base=base, head=head
    )

    assert result.historical_evidence is not None
    assert result.historical_evidence.status == "ok"
    assert any(
        item.evidence_id == document.evidence_id
        and item.source_type == "incident"
        for item in result.historical_evidence.results
    )
    assert document.evidence_id in result.risk_assessment.factors[2].evidence_refs


def test_java_analysis_uses_existing_mcp_tool_port(tmp_path):
    repo, base, head = java_repo(tmp_path)
    database = str(tmp_path / "analysis.sqlite3")
    mcp = MCPToolPort(
        [sys.executable, "-m", "changeguard.mcp.server", "--database", database]
    )
    result = ChangeAnalysisService(
        SQLiteAnalysisStore(database), mcp
    ).analyze(repo_path=str(repo), base=base, head=head)

    assert result.code_analysis is not None
    assert len(result.blast_radius.direct_dependents) == 1
    assert all(stage.status in {"completed", "skipped", "unavailable"} for stage in result.trace)


def test_impact_cli_runs_deterministic_mode_without_ollama(git_repo, tmp_path, monkeypatch, capsys):
    repo, base, head = git_repo
    database = str(tmp_path / "cli-analysis.sqlite3")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "changeguard",
            "impact",
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--database",
            database,
        ],
    )

    cli_main()

    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "deterministic"
    assert output["analysis"]["changed_files"] == ["app.txt", "new.txt"]
    assert output["analysis"]["test_signals"]["state"] == "unknown"


def test_llm_failure_does_not_prevent_deterministic_analysis(git_repo, tmp_path):
    class FailingInvestigator:
        def investigate_and_report(self, *args, **kwargs):
            raise OllamaConnectionError("local Ollama is unavailable")

    repo, base, head = git_repo
    result = ChangeAnalysisService(
        SQLiteAnalysisStore(str(tmp_path / "analysis.sqlite3")),
        investigator=FailingInvestigator(),  # type: ignore[arg-type]
    ).analyze(repo_path=str(repo), base=base, head=head)

    assert result.risk_assessment is not None
    assert result.investigation_report is None
    assert "Ollama is unavailable" in (result.investigation_error or "")
    assert any(stage.stage == "llm_investigation" and stage.status == "unavailable" for stage in result.trace)


def test_llm_report_is_attached_without_changing_deterministic_risk(git_repo, tmp_path):
    class MockInvestigator:
        def investigate_and_report(self, *args, **kwargs):
            state = InvestigationState(
                task=args[0],
                status="completed",
                tool_calls=[
                    InvestigationToolCall(
                        "get_file",
                        {"path": "large.txt"},
                        result={"content": "x" * 3000},
                        call_id="tool-01",
                    )
                ],
            )
            report = InvestigationReport(
                summary="Insufficient evidence was collected to establish findings.",
                summary_evidence_refs=[],
                findings=[],
                evidence=[],
                uncertainties=["No successful tool results were available."],
                recommended_verification=["Review the resulting diff."],
                tool_trace=[],
            )
            return ReportGenerationResult(report, investigation=state)

    repo, base, head = git_repo
    store = SQLiteAnalysisStore(str(tmp_path / "analysis.sqlite3"))
    deterministic = ChangeAnalysisService(store).analyze(
        repo_path=str(repo), base=base, head=head
    )
    enhanced = ChangeAnalysisService(
        store, investigator=MockInvestigator()  # type: ignore[arg-type]
    ).analyze(repo_path=str(repo), base=base, head=head)

    assert enhanced.investigation_report is not None
    assert enhanced.risk_assessment.score == deterministic.risk_assessment.score
    assert enhanced.risk_assessment.overall_level == deterministic.risk_assessment.overall_level
    assert [
        (item.factor, item.normalized_value, item.contribution, item.state)
        for item in enhanced.risk_assessment.factors
    ] == [
        (item.factor, item.normalized_value, item.contribution, item.state)
        for item in deterministic.risk_assessment.factors
    ]
    assert "Review the resulting diff." in enhanced.recommended_verification
    serialized_calls = enhanced.to_dict()["investigation_state"]["tool_calls"]
    assert serialized_calls[0]["result"]["truncated"] is True
