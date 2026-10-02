from __future__ import annotations

import json
from typing import Any

from changeguard.llm.agent import BoundedInvestigationAgent
from changeguard.mcp.adapter import MCPToolError, MCPToolPort


class SequenceLLM:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


def report_json(
    *,
    summary: str = "The diff changes a file.",
    summary_refs: list[str] | None = None,
    findings: list[dict[str, Any]] | None = None,
    uncertainties: list[str] | None = None,
    recommendations: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "summary": summary,
            "summary_evidence_refs": summary_refs or [],
            "findings": findings or [],
            "uncertainties": uncertainties or [],
            "recommended_verification": recommendations or [],
        }
    )


def create_agent(
    responses: list[str],
    tool_result: dict[str, Any] | None = None,
    *,
    fail_tool: bool = False,
    max_tool_calls: int = 5,
):
    llm = SequenceLLM(*responses)
    mcp = MCPToolPort(server_command=["not-started"])
    calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        if fail_tool:
            raise MCPToolError(f"{name} unavailable")
        return tool_result or {"changed_files": ["src/payment.py"]}

    mcp.call_tool = call_tool
    return BoundedInvestigationAgent(llm, mcp, max_tool_calls), llm, calls


def test_investigation_generates_serializable_grounded_report():
    agent, llm, _ = create_agent(
        [
            '{"type":"tool","tool":"get_git_diff","arguments":{}}',
            '{"type":"final","answer":"The diff changes src/payment.py."}',
            report_json(
                summary="The change modifies src/payment.py.",
                summary_refs=["tool-01"],
                findings=[
                    {
                        "statement": "src/payment.py is listed as changed.",
                        "evidence_refs": ["tool-01"],
                    }
                ],
                recommendations=["Run the relevant payment tests."],
            ),
        ]
    )

    result = agent.investigate_and_report(
        "Investigate payment changes",
        repo_path="/repo",
        base="base",
        head="head",
    )

    assert result.error is None
    assert result.investigation is not None
    assert result.report is not None
    assert result.report.findings[0].evidence_refs == ["tool-01"]
    assert result.report.evidence[0].reference == "tool-01"
    assert result.report.tool_trace == ["tool-01"]
    assert json.loads(json.dumps(result.report.to_dict()))["summary"] == (
        "The change modifies src/payment.py."
    )
    assert "Available evidence" in llm.prompts[-1]
    assert "changed_files" in llm.prompts[-1]
    assert calls_tool_was_only_mcp(result.investigation.tool_calls)


def calls_tool_was_only_mcp(tool_calls) -> bool:
    return len(tool_calls) == 1 and tool_calls[0].call_id == "tool-01"


def test_report_may_reference_actual_historical_evidence_id():
    agent, _, _ = create_agent(
        [
            '{"type":"tool","tool":"search_evidence","arguments":{"query":"payment retry"}}',
            '{"type":"final","answer":"One historical incident matched."}',
            report_json(
                summary="A historical incident matched the query.",
                summary_refs=["INC-42"],
                findings=[
                    {
                        "statement": "INC-42 was returned by historical evidence search.",
                        "evidence_refs": ["INC-42"],
                    }
                ],
            ),
        ],
        {
            "results": [
                {
                    "evidence_id": "INC-42",
                    "title": "Payment retry incident",
                    "source": "incident://42",
                }
            ]
        },
    )

    result = agent.investigate_and_report(
        "Find related payment incidents",
        repo_path="/repo",
        base="base",
        head="head",
    )

    assert result.error is None
    assert result.report is not None
    assert {item.reference for item in result.report.evidence} == {
        "tool-01",
        "INC-42",
    }


def test_unsupported_evidence_reference_returns_report_failure():
    agent, _, _ = create_agent(
        [
            '{"type":"tool","tool":"get_git_diff","arguments":{}}',
            '{"type":"final","answer":"Done."}',
            report_json(
                summary_refs=["made-up-reference"],
                findings=[
                    {
                        "statement": "Unsupported claim.",
                        "evidence_refs": ["tool-01"],
                    }
                ],
            ),
        ]
    )

    result = agent.investigate_and_report(
        "Investigate", repo_path="/repo", base="base", head="head"
    )

    assert result.report is None
    assert result.error is not None
    assert "unsupported evidence reference" in result.error


def test_malformed_report_json_returns_structured_failure():
    agent, _, _ = create_agent(
        [
            '{"type":"final","answer":"No evidence gathered."}',
            "not JSON",
        ]
    )

    result = agent.investigate_and_report(
        "Investigate", repo_path="/repo", base="base", head="head"
    )

    assert result.report is None
    assert result.error is not None
    assert result.error.startswith("report_generation_failed:")


def test_report_with_duplicate_json_fields_is_rejected():
    agent, _, _ = create_agent(
        [
            '{"type":"final","answer":"No evidence gathered."}',
            '{"summary":"first","summary":"second","summary_evidence_refs":[],"findings":[],"uncertainties":[],"recommended_verification":[]}',
        ]
    )

    result = agent.investigate_and_report(
        "Investigate", repo_path="/repo", base="base", head="head"
    )

    assert result.report is None
    assert result.error is not None
    assert "duplicate JSON field" in result.error


def test_tool_failure_remains_visible_as_report_uncertainty():
    agent, _, _ = create_agent(
        [
            '{"type":"tool","tool":"get_git_diff","arguments":{}}',
            '{"type":"final","answer":"No diff was available."}',
            report_json(summary="The diff could not be inspected."),
        ],
        fail_tool=True,
    )

    result = agent.investigate_and_report(
        "Investigate", repo_path="/repo", base="base", head="head"
    )

    assert result.error is None
    assert result.investigation is not None
    assert result.report is not None
    assert result.investigation.tool_calls[0].error == "get_git_diff unavailable"
    assert result.report.findings == []
    assert result.report.evidence == []
    assert any("get_git_diff" in item for item in result.report.uncertainties)


def test_budget_exhaustion_is_reported_as_uncertainty():
    agent, _, _ = create_agent(
        [
            '{"type":"tool","tool":"get_git_diff","arguments":{}}',
            report_json(
                summary="One diff result was collected.",
                summary_refs=["tool-01"],
                findings=[
                    {
                        "statement": "A diff result was collected.",
                        "evidence_refs": ["tool-01"],
                    }
                ],
            ),
        ],
        max_tool_calls=1,
    )

    result = agent.investigate_and_report(
        "Investigate", repo_path="/repo", base="base", head="head"
    )

    assert result.investigation is not None
    assert result.investigation.status == "max_tool_calls_reached"
    assert result.report is not None
    assert any("budget was exhausted" in item for item in result.report.uncertainties)


def test_empty_investigation_cannot_emit_unsupported_findings():
    agent, _, calls = create_agent(
        [
            '{"type":"final","answer":"The code has no bugs."}',
            report_json(
                summary="There are no bugs.",
                findings=[],
                recommendations=["Run tests."],
            ),
        ]
    )

    result = agent.investigate_and_report(
        "Investigate", repo_path="/repo", base="base", head="head"
    )

    assert result.error is None
    assert result.report is not None
    assert result.report.summary == (
        "Insufficient evidence was collected to establish findings."
    )
    assert result.report.summary_evidence_refs == []
    assert result.report.findings == []
    assert result.report.evidence == []
    assert result.report.recommended_verification == ["Run tests."]
    assert any("No successful tool results" in item for item in result.report.uncertainties)
    assert calls == []


def test_findings_without_evidence_references_are_rejected():
    agent, _, _ = create_agent(
        [
            '{"type":"tool","tool":"get_git_diff","arguments":{}}',
            '{"type":"final","answer":"Diff inspected."}',
            report_json(
                summary_refs=["tool-01"],
                findings=[{"statement": "Unsupported finding.", "evidence_refs": []}],
            ),
        ]
    )

    result = agent.investigate_and_report(
        "Investigate", repo_path="/repo", base="base", head="head"
    )

    assert result.report is None
    assert result.error is not None
    assert "one or more evidence references" in result.error
