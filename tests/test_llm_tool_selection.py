from __future__ import annotations

from typing import Any

import pytest

from changeguard.llm.agent import SingleCycleToolAgent
from changeguard.llm.decision import ToolDecision
from changeguard.mcp.adapter import MCPToolPort


class FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompt = ""

    def generate(self, prompt: str) -> str:
        self.prompt = prompt
        return self.response


def agent_for(response: str):
    llm = FakeLLM(response)
    mcp = MCPToolPort(server_command=["not-started"])
    calls: list[tuple[str, dict[str, Any]]] = []
    mcp.call_tool = lambda name, arguments: calls.append((name, arguments)) or {
        "status": "ok"
    }
    return SingleCycleToolAgent(llm, mcp), llm, calls


def test_valid_decision_selects_catalog_tool_and_executes_through_mcp():
    agent, llm, calls = agent_for(
        '{"tool":"get_git_diff","arguments":{},"intent":"Inspect the change"}'
    )

    result = agent.select_and_execute(
        "What changed?", repo_path="/repo", base="base-sha", head="head-sha"
    )

    assert result.error is None
    assert result.decision == ToolDecision(
        "get_git_diff", {}, "Inspect the change"
    )
    assert result.tool_result == {"status": "ok"}
    assert calls == [
        (
            "get_git_diff",
            {
                "repo_path": "/repo",
                "base": "base-sha",
                "head": "head-sha",
            },
        )
    ]
    assert "Available tools" in llm.prompt
    assert "search_evidence" in llm.prompt


def test_unknown_tool_returns_error_without_execution():
    agent, _, calls = agent_for(
        '{"tool":"delete_repository","arguments":{}}'
    )

    result = agent.select_and_execute(
        "Delete the repository", repo_path="/repo", base="base", head="head"
    )

    assert result.error is not None
    assert result.error.code == "unknown_tool"
    assert calls == []


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        ("not JSON", "invalid_decision"),
        ('{"tool":"get_git_diff","arguments":{}', "invalid_decision"),
        ('{"tool":"get_git_diff","arguments":{},"extra":true}', "invalid_decision"),
    ],
)
def test_invalid_decision_is_reported_without_execution(response, expected_code):
    agent, _, calls = agent_for(response)

    result = agent.select_and_execute(
        "Inspect the change", repo_path="/repo", base="base", head="head"
    )

    assert result.error is not None
    assert result.error.code == expected_code
    assert calls == []


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ('{"tool":"get_file","arguments":{}}', "missing required argument"),
        (
            '{"tool":"get_file","arguments":{"path":7}}',
            "argument 'path' must be a string",
        ),
        (
            '{"tool":"get_recent_commits","arguments":{"limit":101}}',
            "argument 'limit' must be at most 100",
        ),
        (
            '{"tool":"search_evidence","arguments":{"query":"x","unexpected":1}}',
            "unknown argument",
        ),
    ],
)
def test_invalid_arguments_are_rejected_without_execution(response, message):
    agent, _, calls = agent_for(response)

    result = agent.select_and_execute(
        "Inspect the change", repo_path="/repo", base="base", head="head"
    )

    assert result.error is not None
    assert result.error.code == "invalid_arguments"
    assert message in result.error.message
    assert calls == []


def test_execution_preserves_mcp_adapter_boundary():
    llm = FakeLLM('{"tool":"get_file","arguments":{"path":"app.py"}}')
    mcp = MCPToolPort(server_command=["not-started"])
    called = []
    mcp.call_tool = lambda name, arguments: called.append((name, arguments)) or {
        "content": "source"
    }
    agent = SingleCycleToolAgent(llm, mcp)

    result = agent.select_and_execute(
        "Read app.py", repo_path="/repo", base="base", head="head"
    )

    assert result.tool_result == {"content": "source"}
    assert called == [
        ("get_file", {"path": "app.py", "repo_path": "/repo"})
    ]
