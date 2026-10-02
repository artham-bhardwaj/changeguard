# Architecture

## Current components

Agent -> ToolPort -> Local tools or MCP adapter -> MCP server -> Git and evidence tools

ToolPort keeps the deterministic agent independent of the transport. MCP handlers are thin protocol adapters and always delegate to the reusable local implementation.

## Evidence pipeline

EvidenceDocument -> normalization -> SHA-256 hash -> duplicate detection -> deterministic chunks -> SQLite

SQLite contains evidence_documents for provenance and evidence_chunks for searchable content. Storage-facing protocols in the ingestion and retrieval modules allow a future PostgreSQL implementation without changing the pipeline or retriever interface.

## Retrieval

Query -> TF-IDF lexical retriever -> cosine ranking -> EvidenceBundle

The lexical retriever provides the V1 implementation behind a lexical retrieval interface. VectorRetriever and HybridRetriever are deliberately only future-facing protocols; no embeddings are generated in this milestone. TF-IDF is appropriate now because it is deterministic, local, CPU-small, and has no download or API cost.

The future route is:
TF-IDF -> PostgreSQL FTS plus pgvector -> hybrid retrieval -> optional reranker.

## Optional local LLM provider

LLM callers depend on `LLMPort`; `OllamaLLMAdapter` implements text generation through Ollama's local HTTP API. It is an optional provider and is independent of the deterministic evidence pipeline:

LLM caller -> LLMPort -> OllamaLLMAdapter -> Ollama HTTP API

This provider addition does not change MCP, tool selection, agent orchestration, or persistence.

## MCP evidence tool

search_evidence accepts query, top_k, optional source_type, and optional repository. It returns structured provenance-bearing evidence. An empty retrieval is explicitly represented as no_relevant_evidence with no results.

## Evaluation

evaluation/dataset.json is small, versioned ground truth. evaluation.run reports Recall@1, Recall@3, Recall@5, and MRR. It is a smoke-test evaluation only, not a production accuracy claim.

## Deterministic risk analysis

Structured change metrics, optional blast-radius results, historical evidence matches, test results, and runtime signals form a `ChangeContext` consumed by the pure `RiskEngine`. The engine performs no Git/Java parsing, database access, MCP calls, or LLM calls.

Change Intelligence  
↓  
Code Analysis  
↓  
Blast Radius  
↓  
Historical Evidence  
↓  
Test Signals  
↓  
Deterministic Risk Engine  
↓  
RiskAssessment  
↓  
LLM Explanation (optional, downstream only)

Risk is calculated deterministically. The LLM does not determine the risk score. The engine reports separately normalized factor values, weighted contributions, source references, uncertainties, and recommendations. Configured factor weights, metric caps, and LOW/MEDIUM/HIGH/CRITICAL thresholds are explicit in `RiskConfig`.

The default factor weights are complexity 30, blast radius 25, historical evidence 20, tests 15, and runtime 10. Counts are divided by their configured caps and clamped to 1. Complexity averages available capped file, changed-line, symbol, method, and Java-type counts plus `1 - changed_file_concentration`. Blast radius averages available direct/indirect symbol, affected-file, dependency-depth, caller, and component counts; unresolved relationships lower signal coverage and remain explicit uncertainty, but do not artificially increase the measured impact. Historical matches are deduplicated by evidence ID and weighted by source type (incident/postmortem 1.0, issue/PR/release 0.5, runbook/ADR 0.25) times retrieval similarity. Test verification risk is `(failed + unavailable) / tests_affected`; runtime risk averages capped operational incidents and error-rate increase.

Factor contributions are `normalized_value × configured_factor_weight × factor_coverage`. The aggregate score is the weighted mean of available factor contributions scaled to 0–100. Default thresholds are LOW `<25`, MEDIUM `25–<50`, HIGH `50–<75`, and CRITICAL `≥75`. Unavailable factors are not scored as zero; `score_weight_coverage` reports what portion of configured factor weight is backed by measurements, and missing/partial signals remain in factor states and uncertainties. If no factor can be scored, the score is `null` and level is `UNKNOWN`.

Before the end-to-end increment, this repository did not include Java symbol/call-graph blast-radius analysis, test execution result ingestion, or runtime telemetry. Those inputs remained unknown unless supplied as structured, referenced signals; the risk engine does not fabricate them. The deterministic score is an explainable engineering signal, not a production incident prediction. No standalone caller-supplied risk MCP operation is exposed.

## End-to-end change impact analysis

`ChangeAnalysisService` composes the existing read-only Git and evidence interfaces with Java code intelligence and `RiskEngine` into one serializable `ChangeGuardAnalysis`:

Git status/diff/commit range  
→ changed-file and hunk mapping  
→ commit-pinned Java source snapshot  
→ Tree-sitter Java symbols and conservative method-call graph  
→ reverse-dependency blast radius  
→ provenance-preserving TF-IDF historical evidence  
→ explicit UNKNOWN test/runtime signals  
→ deterministic `RiskAssessment`  
→ optional bounded LLM investigation/report

The Java snapshot is read from the requested Git revision, not from the potentially different working tree. It is limited to 500 files, 512 KiB per file, and 20 MiB total; conventional generated/vendor directories are excluded. Exclusions and limits are reported as uncertainty. The analyzer resolves calls only when a receiver and method signature match a unique in-repository target (or a unique same-class unqualified target). Dynamic dispatch, inheritance, external libraries, ambiguous overloads, and other unresolved calls remain explicit rather than being guessed. A changed declaration is identified only when its current source range intersects a changed diff hunk; deletion-only or unmapped edits remain `FILE_LEVEL_CHANGE`.

Blast-radius counts are computed from the reverse call graph. Packages are exposed as code groupings, not asserted to be deployment components. Incomplete snapshots, parse errors, unresolved calls, and unmapped changes lower signal coverage or appear in the analysis uncertainties. The risk engine is the only score/level authority. Test results and runtime signals remain `UNKNOWN` because this repository has no trusted importer for either.

The optional LLM path uses the existing bounded read-only MCP investigation and grounded report generation after deterministic risk calculation. Its report is stored separately from the immutable deterministic risk assessment; LLM failure does not fail deterministic analysis. Ollama is never started or required by default. The `impact` CLI command supports deterministic mode by default and `--llm` for opt-in local investigation. MCP exposes a read-only `analyze_change` operation; it invokes the same internal service without recursively invoking the MCP client.

The final analysis and compact metadata are saved to the existing SQLite database in `change_analyses`. It includes evidence provenance, graph relationships, risk factors, uncertainty, optional investigation/report, and stage/status/duration/failure trace data; no source snapshot or large raw diff is persisted.

This increment does not provide project-component/deployment mapping, Java inheritance or dynamic-dispatch resolution, trusted test-result ingestion, runtime telemetry ingestion, production risk prediction, PR mutation, or automated verification.

## Cross-language API impact

When a changed file is Java or JavaScript/TypeScript, `ChangeAnalysisService` runs `ApiContractMapper` against source files read from the requested revision:

Pinned Java sources + pinned JS/TS sources  
→ Spring route declarations + static HTTP request extraction  
→ normalized method/route matching  
→ typed `API_CONSUMER` edges, ambiguity, and unresolved relationships  
→ separate API impact result and existing blast-radius caller signal

The Java extractor handles Spring mapping annotations and class/method route composition. The lightweight frontend tokenizer recognizes common `fetch`, Axios, and named-client call forms, simple literal/constant URL construction, and a constrained local URL-forwarding wrapper. It is deliberately not a JavaScript/TypeScript parser. Matching preserves HTTP method and route case; a single `{parameter}` route segment may match one concrete segment. Multiple matches are ambiguous and do not produce an edge. Dynamic URLs and unknown methods are unresolved, not guessed. API relationships remain separate from Java call-graph edges.

Frontend reads are pinned to the analysis revision, excluding conventional generated/vendor/build/minified paths and bounded to 200 files, 256 KiB per file, and 8 MiB total. Omitted files, parse errors, unresolved requests, and ambiguous matches are visible as uncertainties or counters. API results are serialized inside the existing `change_analyses.analysis_json` payload, so old analysis records without API fields remain readable; no new persistence table is introduced. JSON impact output contains the structured API result, the human-readable PR command prints a compact API summary, and read-only MCP operation `get_api_impact` retrieves it by analysis ID.

Cross-language consumers contribute only where a deterministic API edge exists. Their known count enters the existing blast-radius `affected_callers` signal; there is no API-specific risk multiplier or score. The API result adds separate consumer, provider, and edge references to the blast-radius response. Evaluation fingerprints and fixture metrics include endpoints, consumers, API edges, ambiguous/unresolved relations, and changed/impacted API nodes. See [api-impact.md](api-impact.md) for supported forms and limits.

## Opt-in bounded change verification

The `impact --verify` flag invokes `VerificationPlanner` and `SafeVerificationExecutor` only after analysis. Plans contain structured operations and relative targets, not model- or user-supplied command strings. The planner currently supports changed tracked Python sources: it selects pytest files only when test filenames match changed/affected module names and the pinned revision declares pytest configuration; changed Python files are also compiled with `py_compile`. It does not automatically run an unmatched or full test suite. Other project types are unsupported.

The executor reconstructs the exact analyzed commit from `git archive` in a temporary directory, validates archive paths and regular-file entries, enforces file and byte limits, mounts the workspace read-only, disables network access through bubblewrap namespaces, and bounds process time, memory, output, and temporary storage. It requires Linux bubblewrap; if unavailable, execution is blocked rather than falling back to host execution. No dependencies are installed.

`VerificationPlan`, `VerificationResult`, and per-check evidence are stored with the existing analysis record. Only pytest output containing actual outcome counts can provide a test signal to `RiskEngine`; compile-only, missing, or incomplete results remain unknown. The report can display verification status, but report generation and the LLM cannot plan or execute these checks. MCP exposes planning, execution, and result retrieval as explicit operations; execution is not triggered by ordinary analysis.

This verification slice is deliberately Python-only and does not provide dependency provisioning, arbitrary build commands, full-suite discovery, non-Linux sandboxing, or sandbox fallback.

## Evaluation and observability

The independent evaluation package hosts a versioned fixture catalog and authored expected values. It creates disposable baseline/target repositories, calls the same `ChangeAnalysisService`, optionally enables the existing verifier or LLM path, computes structural metrics, and returns `EvaluationResult` plus `AggregateEvaluationReport`. Ground truth is never derived from `JavaSymbolMapper`, `RiskEngine`, or the observed output. Evaluation metadata is downstream-only and does not enter the risk calculation.

Results and aggregate summaries use additive tables in the existing SQLite store. Deterministic fingerprints use canonical serialized analysis output while excluding random identifiers, timestamps, durations, temporary repository paths, and model prose. The default corpus run repeats deterministic cases and compares fingerprints; explicitly requested LLM evaluation reports reproducibility as unmeasured.

`ChangeStageTrace` retains its existing status/duration/failure contract and now also carries bounded counters. The evaluation run trace summarizes those stages, tool-call and verification counts, deterministic risk summary, and final state without recording environment variables or unbounded raw output. CLI evaluation is offline by default; MCP evaluation accepts registered case identifiers and cannot specify arbitrary repositories or commands. See [evaluation.md](evaluation.md) for schemas, metric conventions, cases, modes, and limitations.

## Read-only GitHub pull request analysis

The `GitHostingPort` isolates GitHub metadata and changed-file access from the PR workflow. `GitHubAPIAdapter` uses the standard library HTTP client against fixed pull-request endpoints and validates the immutable base/head SHAs. `PullRequestAnalysisService`, shared by CLI and MCP, fetches those exact commits into a disposable repository and delegates to the existing `ChangeAnalysisService`; it does not introduce a second analyzer or access the user's checkout.

Optional `GITHUB_TOKEN` authorization is kept out of URLs, arguments, persisted results, and `.git/config`; Git's HTTP authorization is injected only into the snapshot subprocess environment. A post-analysis metadata read marks a moved PR head `STALE`. Opt-in caching uses the existing SQLite store and the exact repository/PR/head key, also requiring the same base SHA. Only completed deterministic results can be reused. Verification and LLM behavior remain the existing opt-in safe sandbox and local Ollama paths.

The mocked GitHub evaluation path (`evaluate --github`) covers stable/stale heads, authentication failure, and exact-revision cache reuse without network access. The regular test suite uses a fake HTTP boundary and never requires a GitHub account.
