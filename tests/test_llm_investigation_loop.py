from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from changeguard.llm.agent import BoundedInvestigationAgent
from changeguard.llm.decision import InvestigationToolCall
from changeguard.mcp.adapter import MCPToolError, MCPToolPort


class SequenceLLM:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


def create_agent(
    responses: list[str],
    tool: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    *,
    max_tool_calls: int = 5,
):
    llm = SequenceLLM(*responses)
    mcp = MCPToolPort(server_command=["not-started"])
    calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        if tool is not None:
            return tool(name, arguments)
        return {"tool": name, "value": len(calls)}

    mcp.call_tool = call_tool
    return BoundedInvestigationAgent(llm, mcp, max_tool_calls), llm, calls


def investigate(agent: BoundedInvestigationAgent):
    return agent.investigate(
        "Investigate this change",
        repo_path="/repo",
        base="base-sha",
        head="head-sha",
    )


def test_single_tool_then_final_answer():
    agent, llm, calls = create_agent(
        [
            '{"type":"tool","tool":"get_git_diff","arguments":{}}',
            '{"type":"final","answer":"The diff adds one file."}',
        ]
    )

    state = investigate(agent)

    assert state.status == "completed"
    assert state.final_answer == "The diff adds one file."
    assert state.tool_call_count == 1
    assert state.tool_calls == [
        InvestigationToolCall(
            "get_git_diff",
            {"repo_path": "/repo", "base": "base-sha", "head": "head-sha"},
            result={"tool": "get_git_diff", "value": 1},
            call_id="tool-01",
        )
    ]
    assert calls[0][0] == "get_git_diff"
    assert "get_git_diff" in llm.prompts[1]
    assert "The diff adds one file" not in llm.prompts[0]


def test_multiple_tools_receive_compact_prior_results():
    agent, llm, calls = create_agent(
        [
            '{"type":"tool","tool":"get_repo_status","arguments":{}}',
            '{"type":"tool","tool":"get_recent_commits","arguments":{}}',
            '{"type":"final","answer":"The repository is clean and has recent commits."}',
        ]
    )

    state = investigate(agent)

    assert state.status == "completed"
    assert state.tool_call_count == 2
    assert [call[0] for call in calls] == [
        "get_repo_status",
        "get_recent_commits",
    ]
    assert '"tool":"get_repo_status"' in llm.prompts[1]
    assert '\\"value\\":1' in llm.prompts[1]
    assert '"tool":"get_recent_commits"' in llm.prompts[2]


def test_call_budget_stops_after_configured_limit():
    agent, llm, calls = create_agent(
        [
            '{"type":"tool","tool":"get_repo_status","arguments":{}}',
            '{"type":"tool","tool":"get_repo_status","arguments":{}}',
            '{"type":"final","answer":"Should not be requested."}',
        ],
        max_tool_calls=2,
    )

    state = investigate(agent)

    assert state.status == "max_tool_calls_reached"
    assert state.error is not None
    assert state.error.code == "MAX_TOOL_CALLS_REACHED"
    assert state.tool_call_count == 2
    assert len(calls) == 2
    assert len(llm.prompts) == 2


def test_final_answer_can_be_returned_without_tool_calls():
    agent, _, calls = create_agent(
        ['{"type":"final","answer":"No tool call was needed."}']
    )

    state = investigate(agent)

    assert state.status == "completed"
    assert state.final_answer == "No tool call was needed."
    assert state.tool_call_count == 0
    assert calls == []


def test_unknown_tool_during_loop_is_rejected():
    agent, _, calls = create_agent(
        ['{"type":"tool","tool":"delete_repository","arguments":{}}']
    )

    state = investigate(agent)

    assert state.status == "rejected"
    assert state.error is not None
    assert state.error.code == "unknown_tool"
    assert calls == []


def test_invalid_arguments_during_loop_are_rejected():
    agent, _, calls = create_agent(
        ['{"type":"tool","tool":"get_file","arguments":{"path":9}}']
    )

    state = investigate(agent)

    assert state.status == "rejected"
    assert state.error is not None
    assert state.error.code == "invalid_arguments"
    assert state.tool_call_count == 0
    assert calls == []


def test_malformed_llm_response_stops_with_structured_failure():
    agent, _, calls = create_agent(["not JSON"])

    state = investigate(agent)

    assert state.status == "invalid_response"
    assert state.error is not None
    assert state.error.code == "invalid_decision"
    assert calls == []


def test_tool_failure_is_recorded_and_included_in_next_prompt():
    def fail_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise MCPToolError(f"{name} failed")

    agent, llm, calls = create_agent(
        [
            '{"type":"tool","tool":"get_git_diff","arguments":{}}',
            '{"type":"final","answer":"The diff tool failed; no diff evidence is available."}',
        ],
        fail_tool,
    )

    state = investigate(agent)

    assert state.status == "completed"
    assert state.tool_call_count == 1
    assert state.tool_calls[0].error == "get_git_diff failed"
    assert state.tool_calls[0].result is None
    assert "get_git_diff failed" in llm.prompts[1]
    assert calls == [
        (
            "get_git_diff",
            {"repo_path": "/repo", "base": "base-sha", "head": "head-sha"},
        )
    ]


def test_tool_result_in_prompt_is_truncated():
    agent, llm, _ = create_agent(
        [
            '{"type":"tool","tool":"get_repo_status","arguments":{}}',
            '{"type":"final","answer":"Done."}',
        ],
        lambda name, arguments: {"data": "x" * 5000},
    )

    state = investigate(agent)

    assert state.status == "completed"
    history = json.loads(
        llm.prompts[1].split("Previous investigation: ", 1)[1]
    )
    assert len(history[0]["result"]) <= 1200 + len("...[truncated]")


@pytest.mark.parametrize("max_tool_calls", [0, -1, 11])
def test_tool_call_budget_must_be_conservative(max_tool_calls):
    llm = SequenceLLM()
    mcp = MCPToolPort(server_command=["not-started"])
    with pytest.raises(ValueError, match="max_tool_calls"):
        BoundedInvestigationAgent(llm, mcp, max_tool_calls)
