from __future__ import annotations
import json, logging, time
from dataclasses import asdict, dataclass
from typing import Any
from changeguard.agent.agent import DeterministicAgent
from changeguard.llm.client import LLMClient, LLMResponse, LLMUnavailableError
from changeguard.llm.decision import (
    InvestigationState,
    InvestigationToolCall,
    ToolDecision,
    ToolSelectionError,
    ToolSelectionResult,
)
from changeguard.llm.grounding import validate_grounding
from changeguard.llm.port import LLMPort
from changeguard.llm.report import (
    InvestigationEvidenceReference,
    InvestigationFinding,
    InvestigationReport,
    ReportGenerationResult,
)
from changeguard.llm.tools import TOOL_NAMES, TOOLS
from changeguard.llm.ollama import OllamaError
from changeguard.models.schemas import AgentTrace, AnalysisState
from changeguard.mcp.adapter import MCPToolError, MCPToolPort
from changeguard.storage.sqlite import SQLiteAnalysisStore
LOG=logging.getLogger(__name__)
MAX_AGENT_STEPS=8
SYSTEM="""You are a software engineering investigation agent. Use read-only tools when concrete evidence is needed. Never invent files, dependencies, incidents, URLs, tests, or call relationships. Use historical evidence only when returned by search_evidence. Finish with JSON containing summary, changed_areas, potential_impacts, historical_evidence, unknowns, recommended_checks, confidence. Important claims must use evidence_ids from collected tool results. If evidence is insufficient, say so."""
DEFAULT_MAX_TOOL_CALLS = 5
MAX_ALLOWED_TOOL_CALLS = 10
MAX_TASK_CHARS = 2000
MAX_TOOL_RESULT_CHARS = 1200


def _strict_json_loads(payload: str) -> Any:
    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON field: {key}")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(
        payload,
        object_pairs_hook=object_from_pairs,
        parse_constant=reject_constant,
    )


class BoundedInvestigationAgent:
    """Select and execute read-only MCP tools within a fixed call budget."""

    def __init__(
        self,
        llm: LLMPort,
        mcp: MCPToolPort,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    ) -> None:
        if not 1 <= max_tool_calls <= MAX_ALLOWED_TOOL_CALLS:
            raise ValueError(
                f"max_tool_calls must be between 1 and {MAX_ALLOWED_TOOL_CALLS}"
            )
        self.llm, self.mcp = llm, mcp
        self.max_tool_calls = max_tool_calls

    def select_and_execute(
        self, task: str, *, repo_path: str, base: str, head: str
    ) -> ToolSelectionResult:
        prompt = self._build_prompt(task, repo_path, base, head)
        try:
            decision = self._parse_decision(
                self.llm.generate(prompt), require_type=False
            )
        except (ValueError, TypeError) as error:
            return ToolSelectionResult(
                None, error=ToolSelectionError("invalid_decision", str(error))
            )

        if decision.decision_type != "tool":
            return ToolSelectionResult(
                decision,
                error=ToolSelectionError(
                    "invalid_decision", "single-cycle selection requires a tool decision"
                ),
            )
        if decision.tool_name is None or decision.tool_name not in TOOL_NAMES:
            return ToolSelectionResult(
                decision,
                error=ToolSelectionError(
                    "unknown_tool", f"Unknown tool: {decision.tool_name}"
                ),
            )

        try:
            arguments = self._validated_arguments(
                decision.tool_name, decision.arguments, repo_path, base, head
            )
        except ValueError as error:
            return ToolSelectionResult(
                decision,
                error=ToolSelectionError("invalid_arguments", str(error)),
            )

        result = self.mcp.call_tool(decision.tool_name, arguments)
        return ToolSelectionResult(decision, tool_result=result)

    def _build_prompt(self, task: str, repo_path: str, base: str, head: str) -> str:
        return self._prompt(task, repo_path, base, head, [])

    def investigate(
        self, task: str, *, repo_path: str, base: str, head: str
    ) -> InvestigationState:
        state = InvestigationState(task=task)
        while state.tool_call_count < self.max_tool_calls:
            prompt = self._prompt(task, repo_path, base, head, state.tool_calls)
            try:
                decision = self._parse_decision(
                    self.llm.generate(prompt), require_type=True
                )
            except (ValueError, TypeError) as error:
                state.status = "invalid_response"
                state.error = ToolSelectionError("invalid_decision", str(error))
                return state

            if decision.decision_type == "final":
                state.status = "completed"
                state.final_answer = decision.answer
                return state

            if decision.tool_name is None or decision.tool_name not in TOOL_NAMES:
                state.status = "rejected"
                state.error = ToolSelectionError(
                    "unknown_tool", f"Unknown tool: {decision.tool_name}"
                )
                return state

            try:
                arguments = self._validated_arguments(
                    decision.tool_name, decision.arguments, repo_path, base, head
                )
            except ValueError as error:
                state.status = "rejected"
                state.error = ToolSelectionError("invalid_arguments", str(error))
                return state

            try:
                result = self.mcp.call_tool(decision.tool_name, arguments)
            except MCPToolError as error:
                state.tool_calls.append(
                    InvestigationToolCall(
                        decision.tool_name,
                        arguments,
                        error=str(error),
                        call_id=f"tool-{state.tool_call_count + 1:02d}",
                    )
                )
                continue
            state.tool_calls.append(
                InvestigationToolCall(
                    decision.tool_name,
                    arguments,
                    result=result,
                    call_id=f"tool-{state.tool_call_count + 1:02d}",
                )
            )

        state.status = "max_tool_calls_reached"
        state.error = ToolSelectionError(
            "MAX_TOOL_CALLS_REACHED",
            f"Investigation stopped after {self.max_tool_calls} tool calls",
        )
        return state

    def investigate_and_report(
        self, task: str, *, repo_path: str, base: str, head: str
    ) -> ReportGenerationResult:
        state = self.investigate(task, repo_path=repo_path, base=base, head=head)
        return self.generate_report(state)

    def generate_report(self, state: InvestigationState) -> ReportGenerationResult:
        report_started = time.perf_counter()
        evidence = self._evidence_references(state)
        prompt = self._report_prompt(state, evidence)
        try:
            response = self.llm.generate(prompt)
            report = self._parse_report(response, state, evidence)
        except (ValueError, TypeError, OllamaError) as error:
            return ReportGenerationResult(
                None,
                f"report_generation_failed: {error}",
                state,
                round((time.perf_counter() - report_started) * 1000, 3),
            )
        return ReportGenerationResult(
            report,
            investigation=state,
            report_generation_duration_ms=round(
                (time.perf_counter() - report_started) * 1000, 3
            ),
        )

    def _evidence_references(
        self, state: InvestigationState
    ) -> list[InvestigationEvidenceReference]:
        references: list[InvestigationEvidenceReference] = []
        for index, call in enumerate(state.tool_calls, start=1):
            call_id = call.call_id or f"tool-{index:02d}"
            if call.result is None:
                continue
            references.append(
                InvestigationEvidenceReference(
                    call_id,
                    call.tool_name,
                    f"Successful result returned by {call.tool_name}",
                )
            )
            if call.tool_name != "search_evidence":
                continue
            results = call.result.get("results")
            if not isinstance(results, list):
                continue
            for item in results:
                if not isinstance(item, dict):
                    continue
                evidence_id = item.get("evidence_id")
                if not isinstance(evidence_id, str) or not evidence_id:
                    continue
                title = item.get("title")
                description = (
                    f"Historical evidence returned by search_evidence: {title}"
                    if isinstance(title, str) and title
                    else "Historical evidence returned by search_evidence"
                )
                references.append(
                    InvestigationEvidenceReference(
                        evidence_id, "search_evidence", description
                    )
                )
        return references

    def _report_prompt(
        self,
        state: InvestigationState,
        evidence: list[InvestigationEvidenceReference],
    ) -> str:
        history = [
            {
                "reference": call.call_id or f"tool-{index:02d}",
                "tool": call.tool_name,
                "arguments": call.arguments,
                "result": self._compact_result(call.result),
                "error": call.error,
            }
            for index, call in enumerate(state.tool_calls, start=1)
        ]
        return (
            "Produce a conservative engineering investigation report using only "
            "successful recorded tool results below. Do not infer unsupported "
            "files, commits, incidents, tests, dependencies, or production behavior. "
            "Every summary claim must cite summary_evidence_refs. Every finding "
            "must cite one or more evidence_refs. Use only reference strings in "
            "the available evidence list. Treat failures and the investigation "
            "status as limitations, not successful evidence. Recommendations are "
            "proposed future checks, not completed actions. Return only strict JSON "
            "with keys summary, summary_evidence_refs, findings, uncertainties, "
            "recommended_verification. Each finding has statement and evidence_refs.\n"
            f"Task: {state.task[:MAX_TASK_CHARS]}\n"
            f"Investigation status: {state.status}\n"
            f"Investigation error: {state.error.message if state.error else 'none'}\n"
            f"Unverified final draft: {(state.final_answer or '')[:500]}\n"
            f"Available evidence: {json.dumps([asdict(item) for item in evidence], separators=(',', ':'))}\n"
            f"Tool history: {json.dumps(history, separators=(',', ':'))}"
        )

    def _parse_report(
        self,
        response: str,
        state: InvestigationState,
        evidence: list[InvestigationEvidenceReference],
    ) -> InvestigationReport:
        value = _strict_json_loads(response)
        if not isinstance(value, dict):
            raise ValueError("report must be a JSON object")
        expected = {
            "summary",
            "summary_evidence_refs",
            "findings",
            "uncertainties",
            "recommended_verification",
        }
        if set(value) != expected:
            raise ValueError("report must contain exactly the required fields")

        summary = value["summary"]
        summary_refs = value["summary_evidence_refs"]
        raw_findings = value["findings"]
        uncertainties = value["uncertainties"]
        recommendations = value["recommended_verification"]
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("report summary must be a non-empty string")
        if not isinstance(summary_refs, list) or not all(
            isinstance(item, str) for item in summary_refs
        ):
            raise ValueError("summary_evidence_refs must be a list of strings")
        if not isinstance(raw_findings, list):
            raise ValueError("findings must be a list")
        if not isinstance(uncertainties, list) or not all(
            isinstance(item, str) and item.strip() for item in uncertainties
        ):
            raise ValueError("uncertainties must be a list of non-empty strings")
        if not isinstance(recommendations, list) or not all(
            isinstance(item, str) and item.strip() for item in recommendations
        ):
            raise ValueError(
                "recommended_verification must be a list of non-empty strings"
            )

        available_refs = {item.reference for item in evidence}
        self._validate_evidence_refs(summary_refs, available_refs, "summary")
        findings: list[InvestigationFinding] = []
        for index, finding in enumerate(raw_findings):
            if not isinstance(finding, dict) or set(finding) != {
                "statement",
                "evidence_refs",
            }:
                raise ValueError(f"finding {index} has an invalid shape")
            statement = finding["statement"]
            refs = finding["evidence_refs"]
            if not isinstance(statement, str) or not statement.strip():
                raise ValueError(f"finding {index} statement must be non-empty")
            if not isinstance(refs, list) or not refs or not all(
                isinstance(item, str) for item in refs
            ):
                raise ValueError(
                    f"finding {index} must cite one or more evidence references"
                )
            self._validate_evidence_refs(refs, available_refs, f"finding {index}")
            findings.append(InvestigationFinding(statement, refs))

        successful_calls = any(call.result is not None for call in state.tool_calls)
        if findings and not successful_calls:
            raise ValueError("findings cannot be reported without successful tool results")
        if successful_calls and not summary_refs:
            raise ValueError("summary must cite evidence when successful results exist")

        report_uncertainties = list(uncertainties)
        failed_tools = [call.tool_name for call in state.tool_calls if call.error]
        if failed_tools:
            report_uncertainties.append(
                "Tool execution failed for: " + ", ".join(failed_tools)
            )
        if state.error and state.status != "max_tool_calls_reached":
            report_uncertainties.append(
                f"Investigation ended with {state.error.code}: {state.error.message}"
            )
        if state.status == "max_tool_calls_reached":
            report_uncertainties.append(
                "Investigation stopped because the tool-call budget was exhausted."
            )
        if not successful_calls:
            report_uncertainties.append(
                "No successful tool results were available; findings cannot be established."
            )

        if not successful_calls:
            summary = "Insufficient evidence was collected to establish findings."
            summary_refs = []

        return InvestigationReport(
            summary=summary,
            summary_evidence_refs=summary_refs,
            findings=findings,
            evidence=evidence,
            uncertainties=list(dict.fromkeys(report_uncertainties)),
            recommended_verification=recommendations,
            tool_trace=[
                call.call_id or f"tool-{index:02d}"
                for index, call in enumerate(state.tool_calls, start=1)
            ],
        )

    def _validate_evidence_refs(
        self, references: list[str], available: set[str], location: str
    ) -> None:
        unknown = set(references) - available
        if unknown:
            raise ValueError(
                f"{location} contains unsupported evidence reference(s): "
                + ", ".join(sorted(unknown))
            )

    def _prompt(
        self,
        task: str,
        repo_path: str,
        base: str,
        head: str,
        history: list[InvestigationToolCall],
    ) -> str:
        catalog = [
            {
                "name": tool["function"]["name"],
                "description": tool["function"]["description"],
                "parameters": tool["function"]["parameters"],
            }
            for tool in TOOLS
        ]
        compact_history = [
            {
                "tool": call.tool_name,
                "arguments": call.arguments,
                "result": self._compact_result(call.result),
                "error": call.error,
            }
            for call in history
        ]
        return (
            "Investigate the task using only the recorded tool results. "
            "Choose one action: return a tool decision as "
            '{"type":"tool","tool":"name","arguments":{...}} or a final answer '
            'as {"type":"final","answer":"..."}. Return only JSON, no markdown. '
            "Never claim a tool ran unless it appears in the previous investigation. "
            "If evidence is missing, say so. Use only tools in this catalog. "
            "Repository and commit arguments are supplied by the agent.\n"
            f"Available tools:\n{json.dumps(catalog)}\n"
            f"Original task: {task[:MAX_TASK_CHARS]}\n"
            f"Repository context: {json.dumps({'repo_path': repo_path, 'base': base, 'head': head})}\n"
            f"Previous investigation: {json.dumps(compact_history, separators=(',', ':'))}"
        )

    def _compact_result(self, result: dict[str, Any] | None) -> str | None:
        if result is None:
            return None
        serialized = json.dumps(result, default=str, separators=(",", ":"))
        if len(serialized) > MAX_TOOL_RESULT_CHARS:
            return serialized[:MAX_TOOL_RESULT_CHARS] + "...[truncated]"
        return serialized

    def _parse_decision(
        self, response: str, *, require_type: bool = True
    ) -> ToolDecision:
        value = json.loads(response)
        if not isinstance(value, dict):
            raise ValueError("LLM decision must be a JSON object")
        decision_type = value.get("type", "tool" if not require_type else None)
        if decision_type == "final":
            if set(value) != {"type", "answer"}:
                raise ValueError("final decision must contain only 'type' and 'answer'")
            answer = value.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("final decision must contain a non-empty 'answer'")
            return ToolDecision(None, decision_type="final", answer=answer)
        if decision_type != "tool":
            raise ValueError("decision 'type' must be 'tool' or 'final'")
        allowed_fields = {"type", "tool", "arguments", "intent"}
        if set(value) - allowed_fields:
            raise ValueError("tool decision contains unknown fields")
        tool_name = value.get("tool")
        arguments = value.get("arguments")
        intent = value.get("intent")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("tool decision must contain a non-empty 'tool' string")
        if not isinstance(arguments, dict):
            raise ValueError("tool decision must contain an 'arguments' object")
        if intent is not None and not isinstance(intent, str):
            raise ValueError("'intent' must be a string when provided")
        return ToolDecision(
            tool_name, arguments, intent, decision_type="tool"
        )

    def _validated_arguments(
        self,
        tool_name: str,
        supplied: dict[str, Any],
        repo_path: str,
        base: str,
        head: str,
    ) -> dict[str, Any]:
        definition = next(
            tool["function"] for tool in TOOLS if tool["function"]["name"] == tool_name
        )
        schema = definition["parameters"]
        properties = schema["properties"]
        unknown = set(supplied) - set(properties)
        if unknown:
            raise ValueError(f"unknown argument(s): {', '.join(sorted(unknown))}")

        arguments = dict(supplied)
        context = {
            "repo_path": repo_path,
            "base": base,
            "head": head,
            "repository": repo_path,
        }
        for key, value in context.items():
            if key in properties:
                arguments[key] = value

        missing = set(schema.get("required", [])) - set(arguments)
        if missing:
            raise ValueError(f"missing required argument(s): {', '.join(sorted(missing))}")

        for name, value in arguments.items():
            property_schema = properties[name]
            expected_type = property_schema["type"]
            if expected_type == "string" and not isinstance(value, str):
                raise ValueError(f"argument '{name}' must be a string")
            if expected_type == "integer" and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                raise ValueError(f"argument '{name}' must be an integer")
            if expected_type == "integer":
                minimum = property_schema.get("minimum")
                maximum = property_schema.get("maximum")
                if minimum is not None and value < minimum:
                    raise ValueError(f"argument '{name}' must be at least {minimum}")
                if maximum is not None and value > maximum:
                    raise ValueError(f"argument '{name}' must be at most {maximum}")
        return arguments


SingleCycleToolAgent = BoundedInvestigationAgent


@dataclass
class AgentRunResult:
    mode: str
    state: AnalysisState
    report: dict[str,Any]
    grounding: dict[str,Any]
    llm_model: str|None
    total_steps: int
class ToolUsingAgent:
    def __init__(self,store: SQLiteAnalysisStore,llm: LLMClient,mcp: MCPToolPort,max_steps: int=MAX_AGENT_STEPS)->None: self.store,self.llm,self.mcp,self.max_steps=store,llm,mcp,max_steps
    def investigate(self,repo_path: str,base: str,head: str)->AgentRunResult:
        messages=[{"role":"system","content":SYSTEM},{"role":"user","content":json.dumps({"repo_path":repo_path,"base_commit":base,"head_commit":head,"task":"Investigate what this change could affect."})}]
        traces=[]; collected_ids=set(); files=set(); changed=[]; model=None
        try:
            for step in range(1,self.max_steps+1):
                started=time.monotonic(); response=self.llm.complete(messages,TOOLS); model=response.model or model
                if not isinstance(response,LLMResponse): raise LLMUnavailableError("LLM returned malformed response")
                if not response.tool_calls:
                    report=self._parse_report(response.content)
                    grounding=validate_grounding(report,collected_ids,files)
                    state=AnalysisState(repo_path,base,head,changed,{"additions":0,"deletions":0},[],[],traces,"completed" if grounding["grounding_status"]=="passed" else "grounding_failed")
                    self.store.save_analysis(state)
                    return AgentRunResult("llm",state,report,grounding,model,step)
                for call in response.tool_calls:
                    if call.name not in TOOL_NAMES: raise LLMUnavailableError("LLM requested unknown tool")
                    args=self._bounded_arguments(call.name,call.arguments,repo_path,base,head)
                    result=self.mcp.call_tool(call.name,args)
                    if call.name=="get_file": files.add(str(args["path"]))
                    if call.name=="get_git_diff": changed.extend(result.get("changed_files",[]))
                    if call.name=="search_evidence": collected_ids.update(str(x.get("evidence_id")) for x in result.get("results",[]))
                    summary=json.dumps(result,default=str)[:1000]
                    traces.append(AgentTrace(step,call.name,args,summary,"mcp"))
                    LOG.info("agent_tool_call",extra={"analysis_id":"pending","step":step,"tool":call.name,"latency_ms":int((time.monotonic()-started)*1000),"success":True,"model":model})
                    messages.append({"role":"tool","tool_name":call.name,"content":summary})
            report={"summary":"Agent step limit reached before a final response.","changed_areas":[],"potential_impacts":[],"historical_evidence":[],"unknowns":["agent_limit_reached"],"recommended_checks":[],"confidence":"low"}
            grounding=validate_grounding(report,collected_ids,files)
            state=AnalysisState(repo_path,base,head,changed,{"additions":0,"deletions":0},[],[],traces,"agent_limit_reached"); self.store.save_analysis(state)
            return AgentRunResult("llm",state,report,grounding,model,self.max_steps)
        except (LLMUnavailableError,ValueError,KeyError,TypeError,json.JSONDecodeError) as error:
            LOG.warning("llm_fallback",extra={"reason":str(error)})
            state=DeterministicAgent(self.store,self.mcp).investigate(repo_path=repo_path,base=base,head=head)
            report={"summary":"Local LLM unavailable; deterministic evidence collection completed.","changed_areas":state.changed_files,"potential_impacts":[],"historical_evidence":[],"unknowns":[str(error)],"recommended_checks":[],"confidence":"low"}
            return AgentRunResult("deterministic_fallback",state,report,{"grounding_status":"passed","errors":[]},model,len(state.agent_trace))
    def _bounded_arguments(self,name: str,args: dict[str,Any],repo: str,base: str,head: str)->dict[str,Any]:
        if not isinstance(args,dict): raise ValueError("tool arguments must be object")
        values=dict(args)
        if name in {"get_repo_status","get_git_diff","get_file","get_recent_commits"}: values["repo_path"]=repo
        if name=="get_git_diff": values["base"],values["head"]=base,head
        if name=="search_evidence": values.setdefault("repository",repo); values["top_k"]=min(int(values.get("top_k",5)),10)
        if name=="get_recent_commits": values["limit"]=min(int(values.get("limit",10)),100)
        return values
    def _parse_report(self,content: str)->dict[str,Any]:
        value=json.loads(content)
        required={"summary","changed_areas","potential_impacts","historical_evidence","unknowns","recommended_checks","confidence"}
        if not isinstance(value,dict) or not required.issubset(value) or value["confidence"] not in {"low","medium","high"}: raise ValueError("malformed structured LLM report")
        return value
