# ChangeGuard Change Log

## Milestone 4A — Local LLM Provider Foundation

### Added
- `LLMPort` abstraction for prompt-based generation.
- `OllamaLLMAdapter` using Ollama's local `/api/generate` HTTP endpoint.
- Ollama base URL, model, and connection/read timeout configuration, including environment variable overrides.
- Explicit errors for connection failures, timeouts, unavailable models, and invalid responses.
- HTTP-boundary unit tests that do not require Ollama.
- Optional manual Ollama smoke test (`python -m changeguard.llm.smoke`).

### Architecture
LLM caller → LLMPort → OllamaLLMAdapter → Ollama HTTP API

### Not implemented yet
- This increment does not add LLM tool selection, MCP tool calling, an autonomous agent loop, structured tool-call parsing, or final report generation.
- The repository already contains a separate tool-using LLM path; it was not changed by this increment.

### Verification
- Existing tests before changes: 14 passed.
- New tests: 13 passed.
- Full test suite after changes: 27 passed.
- Optional Ollama smoke test: not run; no Ollama process was required.

### Files changed
- `changeguard/llm/config.py` — Ollama endpoint, model, timeout defaults and environment configuration.
- `changeguard/llm/port.py` — minimal LLM provider protocol.
- `changeguard/llm/ollama.py` — local HTTP generation adapter and explicit error types.
- `changeguard/llm/smoke.py` — opt-in manual smoke test.
- `changeguard/llm/__init__.py` — exports the provider API.
- `tests/test_llm_provider.py` — offline config, request, and error handling tests.
- `pyproject.toml` — includes the LLM package in setuptools package configuration.
- `README.md` and `docs/architecture.md` — document the optional provider and usage.
- `CHANGES.md` — records this milestone and its verification.

## Milestone 4B — LLM Tool Selection

### Added
- Structured `ToolDecision`, `ToolSelectionError`, and `ToolSelectionResult` contracts.
- Agent-side tool catalog prompt sourced from the existing MCP-compatible tool definitions.
- A single-cycle LLM tool selection and execution operation using the existing MCP adapter.
- Strict JSON parsing plus tool existence, argument-name, required-argument, type, and range validation.

### Architecture

User task  
→ Agent  
→ LLMPort  
→ ToolDecision  
→ ToolPort/MCP  
→ Tool  
→ ToolResult

### Safety
- Unknown tools are rejected without execution.
- Malformed LLM responses are rejected without execution.
- Invalid and unknown arguments are rejected without execution.
- Repository and commit context is supplied by the agent, not trusted from model output.
- No arbitrary shell execution was added.
- Tool execution continues through `MCPToolPort.call_tool`.

### Not implemented yet
- This increment does not add a multi-step loop, tool retries, final grounded report generation, blast-radius analysis, risk engine, or sandbox execution.
- The repository's pre-existing `ToolUsingAgent` was not replaced, extended, or wired to the new single-cycle `LLMPort` path.

### Verification
- Existing tests before this increment: 27 passed.
- New tests: 10 passed.
- Full test suite: 37 passed.

### Files changed
- `changeguard/llm/decision.py` — structured tool decision, result, and error models.
- `changeguard/llm/agent.py` — single-cycle selection, validation, and MCP execution using the existing catalog.
- `tests/test_llm_tool_selection.py` — offline tests for valid, unknown, malformed, invalid-argument, and MCP-boundary cases.
- `CHANGES.md` — records this increment and its verification.

## Milestone 4C — Bounded Multi-Step Investigation Loop

### Added
- `InvestigationState` and `InvestigationToolCall` models for task status, executed tool history, results, failures, and final answer.
- Strict `type: tool` / `type: final` response parsing.
- A bounded investigation loop with a configurable `max_tool_calls` constructor option (default 5, maximum 10).
- Compact, truncated prior tool results in follow-up prompts.
- Structured budget-exhausted, invalid-response, rejected-decision, and completed states.
- Tool execution failures are recorded and shown to the LLM on the next step without automatic retries.

### Architecture

Task  
→ Bounded agent  
→ LLMPort  
→ ToolDecision or final answer  
→ validation  
→ MCPToolPort  
→ tool result  
→ bounded agent

### Safety
- Tool calls remain restricted to the existing catalog and argument validation.
- Repository and commit context remains agent-supplied.
- Every tool invocation goes through `MCPToolPort.call_tool`.
- Tool output included in subsequent prompts is truncated; call count is capped at 10.
- No shell execution or repository modification capability was added.

### Not implemented yet
- This increment does not add retries, a final structured grounded report, blast-radius analysis, risk scoring, or sandbox execution.
- The pre-existing `ToolUsingAgent` remains unchanged and separate.

### Verification
- Existing tests before this increment: 37 passed.
- New investigation-loop tests: 12 passed.
- Full test suite: 49 passed.

### Files changed
- `changeguard/llm/decision.py` — extends tool decisions for final responses and adds investigation history/state models.
- `changeguard/llm/agent.py` — implements the bounded investigation loop and retains the existing single-cycle API as an alias.
- `tests/test_llm_investigation_loop.py` — covers single/multiple tools, budget limit, immediate final, rejection, tool failures, and bounded history.
- `CHANGES.md` — records this increment and its verification.

## Milestone 4D — Grounded Investigation Report

### Added
- Structured `InvestigationReport` with summary, cited findings, evidence inventory, uncertainties, recommended verification, and tool trace.
- Evidence-linked findings and summary references validated against successful investigation results.
- Stable per-investigation tool-call references and references to actual historical evidence IDs returned by `search_evidence`.
- Structured report generation through the existing `LLMPort`.
- Strict report JSON shape and duplicate-field validation.
- Report-generation failures for malformed or unsupported citations rather than successful-looking fallback reports.

### Architecture

InvestigationState  
→ Report Generator  
→ LLMPort  
→ Structured InvestigationReport

### Grounding
- Findings require one or more references from the actual investigation.
- Summary claims cite available evidence when successful tool results exist.
- Unsupported references are rejected.
- Failed tools and budget exhaustion remain explicit report uncertainties.
- An investigation with no successful tool results returns no findings and a deterministic insufficient-evidence summary.
- The report generator has no tool execution path.

### Not implemented yet
- Blast-radius analysis, Tree-sitter, dependency graph, deterministic risk engine, sandbox verification, automatic retries, and agent evaluation/telemetry.

### Verification
- Existing tests before this increment: 49 passed.
- New grounded-report tests: 9 passed.
- Full test suite: 58 passed.

### Files changed
- `changeguard/llm/decision.py` — adds stable tool-call reference field to investigation history.
- `changeguard/llm/report.py` — defines the serializable grounded report and generation result contracts.
- `changeguard/llm/agent.py` — generates and validates grounded reports downstream of the bounded investigation.
- `tests/test_llm_investigation_loop.py` — verifies generated tool-call references remain in investigation state.
- `tests/test_llm_investigation_report.py` — covers grounding, unsupported references, malformed JSON, failures, budget exhaustion, and empty investigations.
- `CHANGES.md` — records this increment and its verification.

## Milestone — Deterministic Risk Engine

### Added
- `ChangeContext` and typed source signal models for change complexity, blast radius, historical evidence, test results, and runtime data.
- Pure deterministic `RiskEngine` and serializable `RiskAssessment`.
- Per-factor normalized values, weighted contributions, explanations, evidence references, and known/unknown/uncertain state.
- Configurable top-level and dimension weights, normalization caps, and risk thresholds.
- Deterministic calibration, boundary, uncertainty, and monotonicity tests.

### Risk factors
- Change complexity — changed files/lines/symbols/methods/Java types and measured change spread.
- Blast radius — direct/indirect symbols, files, dependency depth, callers, components, and unresolved relationships.
- Historical evidence — supplied evidence IDs, source types, and retrieval similarity scores.
- Test confidence — explicitly supplied affected/passed/failed/unavailable counts.
- Runtime signals — supplied operational incident and error-rate change measurements; unknown by default.

### Scoring model
- Each measurement is normalized to 0–1 using a named cap or native fraction.
- Within a factor, configured dimension weights average available measurements; unavailable dimensions reduce that factor's coverage.
- Complexity averages normalized file/line/symbol/method/Java-type counts plus `1 - changed_file_concentration`. Blast radius averages available direct/indirect symbol, file, dependency-depth, caller, and component counts; unresolved edges reduce completeness and remain uncertainty, but do not artificially inflate measured impact.
- Historical source weights are incident/postmortem 1.0, issue/PR/release 0.5, and runbook/ADR 0.25.
- Historical matches are deduplicated by evidence ID. Test verification risk is `(failed + unavailable) / tests_affected`; runtime risk averages capped operational incidents and error-rate increase.
- Factor contributions use configured weights: complexity 30, blast radius 25, historical 20, tests 15, runtime 10 by default.
- The reported score is the weighted mean of available contributions, scaled to 0–100. Missing signals are omitted, not scored as zero; `score_weight_coverage` exposes scored weight over total configured weight. No scored factor yields score `null` and level `UNKNOWN`.
- Default levels: LOW below 25, MEDIUM from 25 to below 50, HIGH from 50 to below 75, and CRITICAL from 75 to 100.

### Important architectural rule

The LLM does not calculate or override risk. The deterministic engine calculates the assessment. The LLM may explain it.

### Integration and limitations
- No risk MCP tool or report integration was added: there is no trusted upstream graph analyzer, test-result importer, or runtime telemetry source in this repository. Exposing caller-supplied risk numbers as an LLM-callable tool would not provide trustworthy analysis.
- This engine assesses only structured signals explicitly supplied in `ChangeContext`; it does not parse Git/Java or query persistence.
- The score is an explainable engineering signal, not a production-incident prediction.

### Verification
- Existing tests before this milestone: 58 passed.
- New risk engine tests: 34 passed.
- Full test suite: 92 passed.

### Files changed
- `changeguard/risk/__init__.py` — exposes the risk domain and engine API.
- `changeguard/risk/models.py` — defines structured contexts, signal inputs, configuration, and assessment contracts.
- `changeguard/risk/engine.py` — implements pure deterministic scoring, explanations, uncertainty handling, and recommendations.
- `tests/test_risk_engine.py` — covers scoring, calibration, boundaries, thresholds, invalid input, uncertainty, and monotonicity.
- `pyproject.toml` — includes `changeguard.risk` in the installed package list.
- `docs/architecture.md` — documents the deterministic risk path, scoring scope, and current analyzer limitations.
- `CHANGES.md` — records this milestone and its verification.

## Next Milestone — End-to-End Change Impact Analysis

### Added
- `ChangeAnalysisService` coordinates read-only Git inspection, revision-pinned Java source analysis, historical evidence retrieval, deterministic risk scoring, and optional bounded LLM investigation.
- Changed diff hunk ranges map to Java type, method, constructor, and field declarations; edits that cannot be mapped remain explicit `FILE_LEVEL_CHANGE`.
- A bounded Tree-sitter Java analyzer builds symbols and conservative method-call relationships, then calculates reverse dependency impact and retains unresolved edges.
- Historical evidence results now include their persisted source type in addition to evidence ID, source, and matched text, allowing risk weighting to use actual provenance.
- Test and runtime signals are represented as `UNKNOWN` because no trusted test-result importer or runtime telemetry source exists.
- A serializable `ChangeGuardAnalysis` keeps deterministic findings, historical evidence, risk, optional LLM report, uncertainties, recommendations, and stage trace separate.
- Existing SQLite persistence stores/retrieves the structured analysis; oversized raw LLM tool results are compacted in the serialized trace.
- Added the read-only MCP `analyze_change` operation and an `impact` CLI mode; deterministic mode is the default and `--llm` is optional.
- Added offline end-to-end, MCP, CLI, provenance, risk-integrity, uncertainty, and persistence tests.

### Architecture

Git change  
→ Changed-file and line mapping  
→ Commit-pinned Java symbol index and method-call graph  
→ Reverse-dependency blast radius  
→ Provenance-bearing historical evidence  
→ Explicit UNKNOWN test/runtime signals  
→ Deterministic RiskEngine  
→ Optional bounded LLM investigation/report  
→ Structured `ChangeGuardAnalysis`

### Important architectural rules
- `RiskEngine` alone computes the score, level, and factor contributions; LLM output is stored separately.
- Java parsing, symbol mapping, and call graph are deterministic; uncertain and unresolved relationships remain represented.
- Historical matches retain evidence ID, source, source type, and retrieval similarity.
- No test or runtime result is inferred from running this implementation's own test suite.
- Ollama is opt-in and is not required for CLI deterministic mode or tests.
- Git source reads are bounded and pinned to the requested `head` revision; the working tree is not modified.

### Verification
- Existing tests before this increment: 92 passed.
- New tests: 10 passed.
- Full test suite: 102 passed in 25.22 seconds.
- `python -m changeguard.cli impact --help`: succeeded.
- Ollama smoke test: not run; Ollama was not required.

### Limitations
- Source snapshots are bounded to 500 Java files, 512 KiB per file, and 20 MiB total; omitted/generated/vendor sources are reported as uncertainty.
- Call resolution is intentionally conservative and limited to matching in-repository owners, method names, and arity. Dynamic dispatch, inherited behavior, external libraries, imports not resolved to local types, and ambiguous overloads remain unresolved.
- Java package names are reported as code groupings, not deployment components.
- Deletion-only or otherwise unmapped edits remain file-level changes.
- No trusted test-result importer or runtime telemetry source is available; both signals remain `UNKNOWN`.
- Analysis stage timings are captured, but no dedicated repository-scale performance benchmark was run. The bounds constrain memory and parser work.
- This is impact analysis, not production-incident prediction or a merge decision.

### Files changed
- `changeguard/analysis/__init__.py` — exports the orchestration and analysis result API.
- `changeguard/analysis/models.py` — defines the serializable result, blast-radius details, stage trace, and compact tool-result serialization.
- `changeguard/analysis/serialization.py` — reconstructs persisted result objects from their JSON shape.
- `changeguard/analysis/service.py` — composes Git, Java analysis, RAG, risk, and optional LLM investigation.
- `changeguard/code_intelligence/__init__.py` — exports Java analysis contracts.
- `changeguard/code_intelligence/java.py` — parses Java symbols, maps changed line ranges, builds a conservative call graph, and calculates blast radius.
- `changeguard/models/schemas.py` — adds the typed Java source snapshot response.
- `changeguard/tools/git_tools.py` — adds bounded Java source reads from the requested commit and revision-specific recent commits.
- `changeguard/agent/agent.py` — exposes Java source retrieval through the existing local tool boundary.
- `changeguard/mcp/adapter.py` — adds the typed MCP Java-source operation.
- `changeguard/mcp/server.py` — registers bounded source retrieval and high-level read-only `analyze_change`.
- `changeguard/evidence/models.py`, `changeguard/evidence/retrieval.py`, and `changeguard/storage/sqlite.py` — preserve evidence source type through retrieval and support structured change-analysis persistence.
- `changeguard/cli.py` — adds deterministic-by-default `impact` and optional bounded `--llm` mode.
- `tests/test_change_analysis.py` and `tests/test_mcp.py` — cover end-to-end behavior, safety boundaries, and serialization.
- `requirements.txt` and `pyproject.toml` — declare the lightweight Tree-sitter Java parser dependencies and package.
- `README.md` and `docs/architecture.md` — document usage, boundaries, and actual limitations.
- `CHANGES.md` — records this milestone and verified results.

## Safe Change Verification — Bounded Local Sandbox

### Added
- Structured verification plans, check results, and revision-linked evidence contracts.
- Deterministic planning for changed tracked Python files, exact-name-matched pytest files, and `py_compile`.
- An opt-in executor that materializes the analyzed Git revision into a temporary workspace and runs only allowlisted operations through Linux bubblewrap.
- Bounded check count, wall/CPU time, captured output, workspace size, file count, process memory, and temporary storage.
- Verification plan/result persistence, CLI `impact --verify`, and MCP planning, execution, and result retrieval operations.
- Test-risk recalculation through the existing deterministic `RiskEngine`; only observed pytest outcome counts affect the test signal.
- Offline tests for revision pinning, read-only isolation, limits, unsupported plans, risk/report integration, and no unsandboxed fallback.

### Architecture

Change analysis  
→ VerificationPlanner  
→ revision-pinned VerificationPlan  
→ SafeVerificationExecutor  
→ temporary Git snapshot  
→ Linux bubblewrap  
→ allowlisted Python pytest / compile check  
→ VerificationResult and evidence  
→ deterministic test-signal and risk update

### Safety and limitations
- Execution is opt-in through `impact --verify`; planning does not execute code.
- Linux bubblewrap is required. Missing bubblewrap or a non-Linux host is BLOCKED; there is no host-execution fallback.
- The snapshot is pinned to the analyzed commit, read-only, and isolated with network namespaces disabled.
- Pytest auto-discovery of environment-installed plugins is disabled for sandboxed checks.
- Only structured `PYTHON_PYTEST` and `PYTHON_COMPILE` checks are accepted; arbitrary commands and shell execution are not supported.
- Project code is never installed and no network access is provided. Pytest is planned only for discovered, name-matched test files when a supported pytest configuration exists; the entire test suite is not launched automatically.
- This increment supports Python verification only. Other project types remain unsupported rather than receiving guessed commands.
- Missing, unmapped, failed, or incomplete test outcomes remain explicit uncertainty; verification does not change the deterministic risk engine or grant the LLM execution capability.

### Verification
- Existing tests before this increment: 102 passed.
- New verification tests: 12 passed.
- Full test suite: 114 passed.
- Ollama and other external services: not required.

### Files changed
- `changeguard/verification/models.py` and `changeguard/verification/__init__.py` — define and export verification plan, result, status, and evidence models.
- `changeguard/verification/planner.py` — discovers revision-specific tracked Python files and creates bounded, deterministic checks.
- `changeguard/verification/executor.py` — validates plans, creates a bounded commit snapshot, and executes checks only in bubblewrap.
- `changeguard/tools/git_tools.py` — adds bounded revision-specific tracked-file listing and file reading.
- `changeguard/agent/agent.py` and `changeguard/mcp/adapter.py` — carry revision-reading operations through the existing tool boundary.
- `changeguard/mcp/server.py` — exposes verification planning, opt-in execution, and result retrieval.
- `changeguard/analysis/models.py` and `changeguard/analysis/serialization.py` — persist and reconstruct initial risk, verification plan, and result state.
- `changeguard/analysis/service.py` — integrates verification evidence, actual pytest outcomes, and deterministic verified-risk calculation.
- `changeguard/llm/report.py` — includes the verification summary without granting execution capability.
- `changeguard/storage/sqlite.py` — updates the persisted analysis record after verification.
- `changeguard/cli.py` — adds the explicit `impact --verify` opt-in.
- `tests/test_verification.py` and `tests/test_mcp.py` — cover the sandbox and MCP surfaces.
- `pyproject.toml` — registers the verification package.
- `README.md` and `docs/architecture.md` — document opt-in usage, boundaries, and supported scope.
- `CHANGES.md` — records this increment and its verified results.

## Next Milestone — Evaluation, Benchmarking & Observability

### Added
- Versioned, serializable `EvaluationCase`, `EvaluationResult`, `EvaluationRunTrace`, and `AggregateEvaluationReport` contracts.
- An independent authored fixture catalog that materializes disposable local Git repositories for Java impact, unresolved/unmapped changes, historical evidence, and Python verification variants.
- Deterministic change-detection, blast-radius, evidence retrieval, grounding, verification, agent-efficiency, and risk-property metrics.
- Canonical SHA-256 fingerprints that exclude random IDs, timestamps, durations, temporary repository paths, and LLM prose.
- A deterministic runner for single cases and full corpus runs, with optional SafeVerificationExecutor and opt-in Ollama LLM modes.
- SQLite result/aggregate persistence, read-only MCP evaluation tools, and the `changeguard evaluate` CLI.
- Bounded stage counters and a run trace that reuse the analysis stage trace; optional evaluation observability is attached to an LLM report only when one exists.

### Architecture

Authored EvaluationCase and fixture  
→ disposable baseline/target repository  
→ existing ChangeAnalysisService  
→ observed analysis and optional SafeVerificationExecutor  
→ deterministic metrics and fingerprint  
→ EvaluationResult / AggregateEvaluationReport  
→ optional existing SQLite persistence

The evaluator is independent of the LLM in deterministic mode. Evaluation results and report observability do not feed the deterministic risk engine.

### Metrics
- File, changed-symbol, and impacted-symbol precision/recall/F1, with TP/FP/FN and duplicate prediction counts.
- Unresolved edges, explicit uncertainty, unavailable analysis stages, and unmapped change counts, plus authored uncertainty expectations.
- Historical evidence ID precision/recall/hit rate when ground truth is available.
- Grounded finding rate, unsupported claim count, invalid citation count, and duplicate citation count from existing structured references.
- Planned/executed/passed/failed/timed-out/blocked/error verification counts; absent verification is NOT_RUN/unknown.
- Objective tool-call counts, success/rejection/failure, duplicate tool+argument pairs, bounded result size, completion reason, budget exhaustion, and measured investigation latency.
- Risk contribution/score consistency, threshold consistency, factor fingerprint, evidence-reference validity, unknown test-signal preservation, authored expected properties, and initial-to-verified delta.
- Aggregate averages/minima/maxima, summed classification precision/recall/F1, case totals, tool/check totals, failures, and reproducibility failures. No single composite score or subjective risk label accuracy is created.

### Evaluation Fixtures
- `local-method-change`: a changed method with an independently authored direct caller expectation.
- `multi-hop-dependency`: explicitly authored three-class call chain and direct/indirect expected dependents.
- `unresolved-dependency`: expected unresolved edge and explicit uncertainty.
- `historical-evidence`: seeded incident with a fixed provenance ID and expected retrieval ID.
- `verification-pass`, `verification-fail`, and `verification-not-discovered`: bounded Python pytest/compile cases.
- `unmapped-change`: comment-only Java edit expected to remain file-level and uncertain.
- Ground truth is literal fixture data and is never derived from analyzer output.

### Reproducibility
- Canonical deterministic projection excludes analysis/run IDs, commit hashes, timestamps, durations, absolute repository paths, environment values, and LLM prose.
- Deterministic aggregate runs execute each case twice in isolated fixture repositories and compare fingerprints.
- LLM-mode repeatability is explicitly unmeasured; single-case CLI runs are not repeated.

### Observability
- `ChangeStageTrace` now adds bounded counters for Git, source/symbol/call-graph/blast-radius analysis, historical retrieval, risk, LLM/report, verification, and persistence.
- Structured run traces include stage status/duration/counters and compact tool, verification, risk, and final-status summaries.
- No arbitrary environment variables or unbounded raw process output are recorded.
- Evaluation observability appears on an LLM report only when that report exists; it does not change risk or the available tool catalog.

### Security / Integrity
- Fixture execution is confined to temporary repositories; no user-supplied repository path or command is accepted by evaluation.
- Verification continues exclusively through `SafeVerificationExecutor`; it remains opt-in and has no unsandboxed fallback.
- MCP accepts registered fixture identifiers only. It cannot modify ground truth, risk configuration, or repository contents.
- Unknown ground truth remains not evaluated; missing verification is not a passing result.

### Verification
- Existing tests before this increment: 114 passed.
- New evaluation framework tests: 15 passed; MCP coverage added 1 test.
- Targeted evaluation and MCP tests: 20 passed.
- Full test suite: 130 passed in 76.58 seconds.
- Deterministic CLI corpus: 8/8 cases passed; all 8 repeated fingerprints matched.
- Verification-enabled fixture run: 8/8 evaluation cases completed without metric failures; the 5 Java cases were explicitly BLOCKED as unsupported by the Python-only verifier; the three Python variants represented pytest pass, failure, and no confident test mapping.
- `evaluate --case verification-pass --verify`: PASSED, 2 checks passed, test signal known.
- Evaluation CLI help and `compileall` completed successfully.
- Ollama was not required; the LLM report/evaluation integration test used a fake offline LLM.

### Tests
- `tests/test_evaluation_framework.py` — contracts, zero-denominator and duplicate metrics, grounding, retrieval, agent and verification metrics, authored fixture behavior, fingerprints, aggregate/SQLite persistence, opt-in verification, CLI persistence, and report observability.
- `tests/test_mcp.py` — evaluation case catalog, evaluation execution, retrieval, aggregate summaries, and invalid-case rejection through MCP.

### Files changed
- `evaluation/models.py` — versioned case/result/run-trace/aggregate contracts and canonical JSON serialization.
- `evaluation/fixtures.py` — independent local golden fixture corpus and authored expectations.
- `evaluation/metrics.py` — structural, grounding, verification, agent, risk, fingerprint, and aggregate metrics.
- `evaluation/runner.py` and `evaluation/__init__.py` — disposable fixture materialization, evaluation execution, reproducibility, optional LLM/verification modes, and persistence coordination.
- `changeguard/analysis/models.py` and `changeguard/analysis/serialization.py` — backward-compatible bounded stage counters and optional report evaluation field rehydration.
- `changeguard/analysis/service.py` — stage counters and separate LLM/report/verification/persistence observability.
- `changeguard/llm/agent.py` and `changeguard/llm/report.py` — report-generation timing and optional evaluation summary section.
- `changeguard/storage/sqlite.py` — versioned evaluation result and aggregate tables plus CRUD/list methods.
- `changeguard/cli.py` and `pyproject.toml` — `evaluate` command and installed `changeguard` entry point.
- `changeguard/mcp/server.py` — registered-case evaluation and read-only result/summary/list operations.
- `tests/test_evaluation_framework.py` and `tests/test_mcp.py` — offline evaluation and MCP coverage.
- `README.md`, `docs/architecture.md`, and `docs/evaluation.md` — usage, architecture, metrics, fixtures, reproducibility, observability, security, and limitations.
- `CHANGES.md` — records this increment and actual verification results.

### Limitations
- This is a small regression corpus, not a statistically representative benchmark.
- The Java corpus tests only the existing conservative resolver; it does not extend Java analysis semantics.
- Verification remains Python pytest/compile only. Java cases are explicitly unsupported by the verifier, and bubblewrap is required for Python execution.
- LLM-mode measurements require explicit `--llm`, an available local Ollama service/model, and are not repeated for fingerprint reproducibility.
- Runtime telemetry, production-risk accuracy, subjective “smartness,” external repositories, and arbitrary project command execution are not evaluated.

## GitHub PR Integration — Read-only End-to-End Review Workflow

### Added
- Typed `GitHostingPort`, validated pull-request/revision contracts, changed-file metadata, rate-limit metadata, and explicit provider/workflow errors.
- Lightweight standard-library GitHub REST adapter for fixed PR metadata and changed-file endpoints, with bounded response size, timeout, and retries.
- Shared CLI/MCP `PullRequestAnalysisService` that fetches exact base/head SHAs into a disposable repository and delegates to the existing `ChangeAnalysisService`.
- Fork-head snapshot support, immutable object-ID verification, post-analysis head refresh, and explicit `STALE` results.
- Opt-in SQLite persistence and exact repository/PR/head cache lookup; only completed deterministic results are reusable.
- `changeguard pr analyze OWNER/REPO#NUMBER` with human/JSON output and verify, local LLM, persistence, and head-refresh options.
- Read-only MCP `analyze_github_pull_request` operation.
- Offline mocked PR workflow evaluation (`changeguard evaluate --github`) for stable/stale heads, authentication failure, and exact-revision cache reuse.

### Architecture

CLI / MCP  
→ `PullRequestAnalysisService`  
→ `GitHostingPort` / `GitHubAPIAdapter`  
→ disposable, SHA-pinned Git snapshot  
→ existing `ChangeAnalysisService`  
→ optional existing safe verification or local Ollama investigation  
→ structured PR result / optional exact-revision SQLite cache

### Safety and integrity
- GitHub requests use fixed API endpoints and validated owner/repository/PR identifiers; no arbitrary URLs or commands are accepted.
- The user's working tree is never checked out or modified.
- Base and head object IDs are verified after fetch; a fork head is fetched from its validated repository.
- Credentials are never included in URLs, Git command arguments, `.git/config`, persisted PR results, or returned errors. The raw token variable is removed from the Git and optional LLM/MCP child environments; the required Git HTTP authorization value is scoped to the temporary Git child process.
- A post-analysis head change is represented as `STALE`; stale, failed, verification-enabled, and LLM-enabled results are not cached.
- Optional verification continues exclusively through the existing `SafeVerificationExecutor`; no new command-execution path was added.

### Not implemented yet
- PR comments, reviews, labels, check runs, or any GitHub write operation.
- Webhooks, background polling, or multi-PR orchestration.
- Live GitHub API verification as part of the offline suite.
- Expanded verification beyond the existing sandboxed project/check support.

### Verification
- Existing tests before this increment: 130 passed.
- New tests: 14 GitHub integration tests and 1 MCP input-validation test.
- Focused PR/MCP/storage tests: 21 passed.
- Offline mocked GitHub workflow evaluation: 4/4 scenarios passed; network access disabled.
- PR CLI help: succeeded.
- Full test suite: 145 passed in 81.90 seconds.
- No live GitHub API request was made.

### Files changed
- `changeguard/github/__init__.py` — public provider, service, and model exports.
- `changeguard/github/models.py` — typed repository/PR, revision, changed-file, rate-limit, error, and result contracts.
- `changeguard/github/port.py` — read-only Git hosting protocol.
- `changeguard/github/provider.py` — bounded standard-library REST access and structured failures.
- `changeguard/github/service.py` — disposable immutable snapshot materialization, analysis delegation, stale-head detection, and cache coordination.
- `changeguard/storage/sqlite.py` — additive table and exact-revision PR result lookup/persistence.
- `changeguard/cli.py` — PR analysis command and offline GitHub evaluation option.
- `changeguard/mcp/server.py` — shared read-only PR analysis operation.
- `changeguard/mcp/adapter.py` — supports an explicit subprocess environment so PR LLM investigation does not pass GitHub credentials into the MCP server.
- `pyproject.toml` — registers the new `changeguard.github` package.
- `evaluation/github.py` and `evaluation/runner.py` — authored mocked workflow scenarios and offline runner entry.
- `tests/test_github_integration.py` — provider, safety, failure, stale-head, cache, and evaluation coverage.
- `tests/test_mcp.py` — confirms MCP registration and validation before network access.
- `README.md`, `docs/architecture.md`, `docs/evaluation.md`, and `docs/github-integration.md` — usage, architecture, safety, cache, MCP, and offline evaluation guidance.
- `CHANGES.md` — records this increment and observed verification results.

## Cross-Language API Impact

### Added
- Deterministic extraction of Spring endpoint mappings and common static JavaScript/TypeScript HTTP request sites from commit-pinned repository sources.
- Route normalization and method-aware matching, including exact-route preference over compatible path templates.
- Typed `API_CONSUMER` dependency edges with endpoint/consumer evidence references, confidence, ambiguity, and unresolved relationship reporting.
- Changed and impacted endpoint/consumer tracking based on exact added-line ranges for API declarations and request sites.
- API impact integration with the existing blast-radius affected-file/caller signals and structured change-analysis output.
- Read-only MCP retrieval through `get_api_impact` and a compact API summary in human-readable PR analysis.
- Authored API evaluation cases, metrics, deterministic fingerprints, and documentation.

### Architecture

Pinned Java + JavaScript/TypeScript sources  
→ Spring mapping and static HTTP-call extraction  
→ normalized method/route matching  
→ typed API consumer edges and explicit unresolved/ambiguous relationships  
→ additive `ChangeGuardAnalysis.api_impact` result  
→ existing blast-radius signals and read-only CLI/MCP output

### Grounding and bounds
- API edges are emitted only for deterministic unique provider matches; exact routes take precedence over compatible path templates.
- Ambiguous matches create no edge. Dynamic URLs, unknown methods, parse errors, and omitted frontend files remain explicit limitations or uncertainty.
- Frontend source reads are revision-pinned and bounded to 200 files, 256 KiB per file, and 8 MiB total; conventional generated/vendor/build/minified paths are excluded.
- API impact is persisted additively in the existing analysis JSON; no SQLite schema or dependency change was made.
- The existing `affected_callers` risk input is reused. No separate API risk multiplier or subjective score was added.

### Not implemented yet
- Complete JavaScript/TypeScript parsing, type/import/module resolution, arbitrary HTTP wrappers, or framework-specific client semantics.
- Removed-endpoint inventory for deletion-only route mappings.
- Runtime reachability, response-schema compatibility, API test execution, or a full API contract validator.
- API-specific risk scoring, deployment component mapping, and verification beyond existing capabilities.

### Verification
- Existing test suite before this increment: 145 passed.
- New tests: 18 (17 API impact tests and 1 MCP API-impact integration test).
- Focused API/MCP/evaluation tests: 39 passed in 40.81 seconds.
- `compileall` for `changeguard`, `evaluation`, and `tests`: passed.
- Full test suite: 163 passed in 60.45 seconds.
- Read-only local analysis of `self-healing-platform` PR #1 at base `5f1aff421495a54b07af0182758c9edd3ce73c3f` and head `d969dd309fcd5e9c871f058670088d1291a35145` discovered 14 Spring endpoints, 13 frontend request sites, and 10 deterministic API edges. It linked the changed `/self-healing/incidents/summary` endpoint to `ops-console.js` and reported one dynamic request as unresolved; no files in that application were modified.

### Files changed
- `changeguard/code_intelligence/api.py` — endpoint and consumer extraction, normalization, route matching, API edges, and relationship uncertainty.
- `changeguard/code_intelligence/__init__.py` — exports the API impact contracts and mapper.
- `changeguard/analysis/models.py` — adds API impact to the analysis and cross-language detail to blast-radius output.
- `changeguard/analysis/serialization.py` — rehydrates API impact additively and remains compatible with older analysis JSON.
- `changeguard/analysis/service.py` — scans pinned frontend sources, parses exact added-line ranges for API tracking, and integrates API impacts into analysis.
- `changeguard/llm/report.py` — carries deterministic API impact context in the structured report.
- `changeguard/cli.py` — prints a compact API impact summary for human-readable PR analysis.
- `changeguard/mcp/server.py` — adds the read-only `get_api_impact` operation.
- `evaluation/models.py`, `evaluation/metrics.py`, `evaluation/runner.py`, and `evaluation/fixtures.py` — API ground truth, metrics, fingerprinting, and local cases.
- `tests/test_api_impact.py` — offline extraction, matching, limits, uncertainty, persistence, evaluation, and regression tests.
- `tests/test_mcp.py` — API-impact MCP integration coverage.
- `README.md`, `docs/architecture.md`, `docs/evaluation.md`, and `docs/api-impact.md` — describe supported analysis behavior, integration, tests, and limitations.
- `CHANGES.md` — records this increment and observed verification results.
