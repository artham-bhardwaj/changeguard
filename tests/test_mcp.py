from __future__ import annotations
import asyncio
import subprocess
import sys
import pytest
from changeguard.agent import DeterministicAgent, LocalTools
from changeguard.mcp import MCPToolPort
from changeguard.mcp.adapter import MCPToolError
from changeguard.mcp.server import create_server
from changeguard.storage.sqlite import SQLiteAnalysisStore

def mcp_port():
    return MCPToolPort([sys.executable, "-m", "changeguard.mcp.server"])

def test_server_starts_and_registers_tools():
    names = {tool.name for tool in asyncio.run(create_server().list_tools())}
    assert names == {
        "get_repo_status",
        "get_git_diff",
        "get_file",
        "get_recent_commits",
        "get_java_sources",
        "get_revision_files",
        "get_revision_file",
        "search_evidence",
        "analyze_change",
        "plan_verification",
        "run_verification",
        "get_verification_result",
        "get_api_impact",
        "evaluate_case",
        "evaluate_all",
        "list_evaluation_cases",
        "get_evaluation_result",
        "get_evaluation_run",
        "get_evaluation_summary",
        "list_evaluation_runs",
        "analyze_github_pull_request",
    }

def test_github_pr_mcp_operation_validates_input_before_network_access():
    port = mcp_port()
    with pytest.raises(MCPToolError):
        port.call_tool(
            "analyze_github_pull_request",
            {"owner": "../invalid", "repository": "widget", "pull_number": 1},
        )

def test_mcp_tools_use_structured_existing_implementations(git_repo):
    repo, base, head = git_repo
    port = mcp_port()
    assert port.repo_status(str(repo)).head_commit == head
    assert port.git_diff(str(repo), base, head).changed_files == ["app.txt", "new.txt"]
    assert port.get_file(str(repo), "app.txt") == "first line\nsecond line\n"
    assert len(port.recent_commits(str(repo), 2)) == 2
    assert port.java_sources(str(repo), head).sources == {}


def test_analyze_change_mcp_operation_returns_read_only_structured_analysis(
    git_repo, tmp_path
):
    repo, base, head = git_repo
    database = str(tmp_path / "mcp-analysis.sqlite3")
    port = MCPToolPort(
        [sys.executable, "-m", "changeguard.mcp.server", "--database", database]
    )

    result = port.call_tool(
        "analyze_change",
        {"repo_path": str(repo), "base": base, "head": head},
    )

    assert result["change_reference"] == f"{base}..{head}"
    assert result["changed_files"] == ["app.txt", "new.txt"]
    assert result["code_analysis"] is None
    assert result["risk_assessment"]["overall_level"] in {
        "LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN"
    }
    assert result["repository_status"]["modified_files"] == []
    assert SQLiteAnalysisStore(database).get_change_analysis(result["analysis_id"]) == result
    api_result = port.call_tool(
        "get_api_impact", {"analysis_id": result["analysis_id"]}
    )
    assert api_result["status"] == "UNAVAILABLE"


def test_mcp_exposes_api_impact_for_java_and_frontend_analysis(tmp_path):
    repo = tmp_path / "api-mcp-repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "mcp-api@example.com")
    git("config", "user.name", "MCP API Test")
    java_file = repo / "src/main/java/demo/Controller.java"
    java_file.parent.mkdir(parents=True)
    java_file.write_text(
        'package demo; class Controller { @GetMapping("/summary") '
        'String summary(){return "before";} }\n',
        encoding="utf-8",
    )
    client_file = repo / "src/main/js/client.js"
    client_file.parent.mkdir(parents=True)
    client_file.write_text('fetch("/summary");\n', encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "baseline")
    base = git("rev-parse", "HEAD")
    java_file.write_text(
        'package demo; class Controller { @GetMapping("/summary") '
        'String summary(){return "after";} }\n',
        encoding="utf-8",
    )
    git("add", ".")
    git("commit", "-m", "change endpoint implementation")
    head = git("rev-parse", "HEAD")

    database = str(tmp_path / "mcp-api-analysis.sqlite3")
    port = MCPToolPort(
        [sys.executable, "-m", "changeguard.mcp.server", "--database", database]
    )
    analysis = port.call_tool(
        "analyze_change",
        {"repo_path": str(repo), "base": base, "head": head},
    )

    api_result = port.call_tool(
        "get_api_impact", {"analysis_id": analysis["analysis_id"]}
    )
    assert api_result["status"] == "AVAILABLE"
    assert len(api_result["api_impact"]["edges"]) == 1


def test_mcp_evaluation_is_case_catalog_only_and_persists_retrievable_results(tmp_path):
    database = str(tmp_path / "evaluation-mcp.sqlite")
    port = MCPToolPort(
        [sys.executable, "-m", "changeguard.mcp.server", "--database", database],
        evidence_database=database,
    )

    cases = port.call_tool("list_evaluation_cases", {})["cases"]
    assert any(case["case_id"] == "local-method-change" for case in cases)
    result = port.call_tool(
        "evaluate_case", {"case_id": "local-method-change"}
    )
    assert result["status"] == "passed"
    assert port.call_tool(
        "get_evaluation_result",
        {"evaluation_run_id": result["evaluation_run_id"]},
    )["fingerprint"] == result["fingerprint"]
    aggregate = port.call_tool(
        "evaluate_all", {"case_ids": ["local-method-change"]}
    )
    assert aggregate["total_cases"] == 1
    assert port.call_tool(
        "get_evaluation_summary",
        {"evaluation_run_id": aggregate["evaluation_run_id"]},
    )["passed_cases"] == 1
    assert len(port.call_tool("list_evaluation_runs", {})["runs"]) == 3
    with pytest.raises(MCPToolError):
        port.call_tool("evaluate_case", {"case_id": "unregistered-case"})
    assert len(port.call_tool("list_evaluation_runs", {})["runs"]) == 3


def test_agent_supports_local_and_mcp_tool_ports(git_repo, tmp_path):
    repo, base, head = git_repo
    local = DeterministicAgent(SQLiteAnalysisStore(str(tmp_path / "local.sqlite3")), LocalTools()).investigate(repo_path=str(repo), base=base, head=head)
    remote = DeterministicAgent(SQLiteAnalysisStore(str(tmp_path / "mcp.sqlite3")), mcp_port()).investigate(repo_path=str(repo), base=base, head=head)
    assert {trace.transport for trace in local.agent_trace} == {"local"}
    assert {trace.transport for trace in remote.agent_trace} == {"mcp"}
    assert remote.changed_files == local.changed_files
