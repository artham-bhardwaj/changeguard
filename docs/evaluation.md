# Evaluation, Benchmarking, and Observability

ChangeGuard evaluation compares analysis output with versioned, independently authored fixture expectations. It is not part of the analysis or risk decision path and never turns unknown input into a passing signal.

## Architecture

Authored `EvaluationCase` and fixture corpus  
→ disposable baseline/target Git repository  
→ existing `ChangeAnalysisService`  
→ observed analysis and optional safe verification  
→ deterministic metrics and fingerprint  
→ structured `EvaluationResult`  
→ aggregate report and optional SQLite persistence

The fixture runner creates repositories only beneath a temporary directory. It uses the existing Git tools, evidence retriever, risk engine, and—only when requested—the existing `SafeVerificationExecutor`. Evaluation does not take an arbitrary repository path or arbitrary command.

## Cases and ground truth

`evaluation/fixtures.py` contains the registered case catalog, literal baseline/target source contents, and ground-truth expectations. Cases are versioned/serializable through `EvaluationCase` (`schema_version: 1`). Each expected field is optional; `None` means “not measured,” not “expected empty.” Expected symbols, impacted IDs, evidence IDs, unresolved edges, unmapped changes, and uncertainty are authored separately from code that analyzes them.

The local corpus includes:

- `local-method-change` — a changed Java method and a separately-filed direct caller.
- `multi-hop-dependency` — a three-class call chain with direct and indirect dependents.
- `unresolved-dependency` — an explicit unresolved call from a changed method.
- `historical-evidence` — a deterministic, provenance-bearing incident document.
- `api-java-to-javascript`, `api-java-to-typescript`, and `api-multiple-consumers` — static Spring route matching to JavaScript/TypeScript consumers.
- `api-dynamic-url`, `api-ambiguous-endpoint`, `api-path-variable`, and `api-unrelated-endpoint` — dynamic and ambiguous requests, a single path-template match, and a no-match case.
- `api-changed-backend-impact`, `api-changed-frontend-consumer`, and `self-healing-summary-api-regression` — changed API providers/consumers and a summary endpoint consumed by the operations-console-style frontend.
- `verification-pass`, `verification-fail`, and `verification-not-discovered` — changed Python source and mapped, failing, or unmapped pytest coverage.
- `unmapped-change` — a Java file-level edit outside a declaration.

Fixtures are materialized with fixed local Git author/committer dates and are discarded after evaluation. Evidence IDs and expected values are fixture literals, not generated from analyzer output.

## Metrics

- Change files, changed symbols, and impacted symbol IDs use precision, recall, and F1, with duplicate predictions separately counted.
- Zero denominator convention: two empty sets score precision and recall as 1; empty predictions against non-empty ground truth score 0; non-empty predictions against empty ground truth score 0 for recall. Metrics are accompanied by TP/FP/FN counts.
- Historical evidence metrics compare evidence IDs and report precision, recall, and hit rate only when expected IDs were authored.
- API metrics independently compare authored endpoints, frontend consumers, deterministic `API_CONSUMER` edges, impacted consumers, and providers of changed consumers. They also report observed ambiguity, unresolved/dynamic requests, and false API edges where ground truth is available.
- Uncertainty output includes unresolved relationship count, unavailable stage count, unmapped change count, and explicit uncertainty presence. Uncertainty alone is not a failure.
- Verification reports planned/executed/passed/failed/timed-out/blocked/error counts. Missing verification is `NOT_RUN`/unknown, never a pass.
- Risk properties check deterministic consistency (score from factor contributions, threshold/level consistency, factor fingerprint, and evidence-reference validity), missing test state, expected explicit properties, and initial-to-verified score delta. No subjective expected risk score is authored.
- Grounding uses existing report references, actual investigation call outputs, and retrieved evidence IDs. Every finding without a citation or with an unknown citation is unsupported; citation checks do not use an LLM.
- Agent metrics count calls, successes, rejections, execution failures, duplicate tool+argument pairs, bounded result size, completion reason, budget exhaustion, and observed latency. “Unnecessary” is limited to objectively identifiable duplicate/rejected calls.

No single aggregate ChangeGuard score is calculated. The aggregate gives per-metric averages, minima, maxima, summed classification counts and aggregate precision/recall/F1, plus case/failure/call/check counts.

## Reproducibility

The SHA-256 fingerprint uses canonical sorted JSON over deterministic analysis fields: changed paths/symbol names, blast-radius IDs, unresolved-edge count, retrieved evidence IDs, deterministic risk factors, and verification check statuses when enabled. When API analysis is present, it also includes normalized endpoint identities, consumer locations/routes, API edges, ambiguous and unresolved relationships, and changed/impacted API nodes. It excludes analysis/run UUIDs, commit hashes, timestamps, wall-clock durations, repository paths, raw LLM output, and environment details.

Default deterministic `evaluate --all` repeats every case in a fresh temporary repository and compares fingerprints. LLM mode does not repeat runs; its report records repeatability as unmeasured. `evaluate --case` runs once and reports repeatability as not repeated.

## Modes and commands

All commands are offline by default and require no Ollama:

```sh
changeguard evaluate
changeguard evaluate --all
changeguard evaluate --case local-method-change
changeguard evaluate --case verification-pass --verify
changeguard evaluate --case local-method-change --llm
```

The equivalent source-tree form is `python -m changeguard.cli evaluate ...`. `--verify` opts into the existing Python pytest/compile sandbox and requires Linux bubblewrap; there is no host-execution fallback. `--llm` explicitly enables the optional Ollama-backed investigation and report path; it may require a running Ollama instance and an already-installed model. Modes are recorded in each result. Verification expectations are compared only when verification is enabled.

The CLI persists case results and aggregate reports in the selected existing SQLite database (`--database`). The schema uses additive evaluation tables and `PRAGMA user_version = 1`. MCP provides `list_evaluation_cases`, `evaluate_case`, `evaluate_all`, `get_evaluation_result`, `get_evaluation_run`, `get_evaluation_summary`, and `list_evaluation_runs`; evaluation requests select only registered case identifiers and cannot supply repository paths or commands.

## Observability

Existing analysis stage traces now expose bounded counters alongside duration and status for Git inspection, source analysis/symbol mapping/call graph/blast radius, historical retrieval, risk, LLM investigation/report generation, verification, and persistence. Evaluation `run_trace` summarizes those stages, counters, tool calls, verification, risk, and final status. It records no environment variables or raw process environment. Existing bounded tool-result storage remains in force.

Evaluation metadata may be included in an optional LLM investigation report when a report exists. It is downstream display metadata only and cannot affect `RiskEngine`, ground truth, or the agent's available tools.

The separate `changeguard evaluate --github` mode runs authored mocked PR workflow cases without making network requests. It exercises stable and stale head detection, authentication failure, and exact-revision cache reuse through the production PR analysis service; it does not alter the core analysis corpus or require Ollama.

## Limitations

- Java impact fixtures exercise the existing conservative Java call resolver; they are not a representative industrial corpus.
- Cross-language API evaluation uses a small authored corpus and the conservative supported syntax described in [api-impact.md](api-impact.md); it is not a full JavaScript/TypeScript parser or a benchmark of arbitrary framework conventions.
- Verification is currently Python pytest/compile only, mapped by exact module names, with no dependency installation or full-suite discovery. Bubblewrap availability controls whether verification cases run or are explicitly skipped.
- This is a small regression corpus, not a statistically representative benchmark. Metrics describe these cases only.
- LLM tool-quality and grounding metrics are available only in explicitly requested LLM mode; normal tests do not contact Ollama.
- Risk accuracy against subjective labels, production incident prediction, and external telemetry are not evaluated.
