# GitHub pull request integration

ChangeGuard can analyze a pull request without modifying the user's working tree. The integration is read-only with respect to GitHub and delegates the analysis itself to the existing `ChangeAnalysisService`.

## Usage

```sh
python -m changeguard.cli pr analyze OWNER/REPO#NUMBER --json
python -m changeguard.cli pr analyze OWNER/REPO#NUMBER --persist
python -m changeguard.cli pr analyze OWNER/REPO#NUMBER --verify
python -m changeguard.cli pr analyze OWNER/REPO#NUMBER --llm
```

The command emits JSON with `--json`, or a concise status and summary otherwise. A failed operation produces a structured error and a nonzero CLI exit. `--refresh-head` is enabled by default; `--no-refresh-head` disables the post-analysis check, in which case `current_head_sha` remains null rather than implying a fresh status. `--verify` opts in to the existing safe verification executor, and `--llm` opts in to the existing local Ollama path. Neither is needed for deterministic analysis.

Public repositories need no credentials. To access private repositories or raise API rate limits, set `GITHUB_TOKEN` in the CLI/MCP server environment. Use a read-only token with only the repository contents and pull-request metadata permissions needed for the target repository. Do not put tokens in command arguments.

## Revision and workspace safety

The provider calls only the fixed GitHub pull-request metadata and changed-files endpoints. Repository and pull number input is validated; arbitrary API URLs, GitHub operations, and commands are not accepted.

The service captures the PR's base and head commit SHAs, creates a disposable temporary Git repository, fetches those exact revisions (including head commits from a validated fork repository), verifies the fetched object IDs, and invokes `ChangeAnalysisService` on that snapshot. It never checks out or writes to a caller's repository. The temporary clone and its temporary analysis database are removed when the operation finishes.

Git authentication is passed only to the Git child process through its environment-scoped HTTP header configuration. The raw `GITHUB_TOKEN` variable is removed from that child environment and from the optional LLM path's MCP subprocess environment. No token is included in the fetch URL, command arguments, `.git/config`, persisted PR results, or structured errors. The API token is kept in process memory only for the request sequence.

When head refresh is enabled, ChangeGuard fetches PR metadata again after analysis. A changed head produces status `STALE`, preserving both the analyzed `head_sha` and the observed `current_head_sha`; it is not represented as a current completed review. API, authentication, timeout, rate-limit, malformed-response, and analysis failures have explicit error codes.

## Persistence and cache

`--persist` stores only completed deterministic results in the existing SQLite database. The primary lookup key is canonical `owner/repository`, PR number, and exact head SHA; the base SHA is checked before reuse. A PR metadata read still occurs on each invocation, and a changed head cannot hit an old cache entry. Verification-enabled, LLM-enabled, stale, and failed results are not cached. PR data is not sent to another persistence system.

## MCP

The MCP server exposes `analyze_github_pull_request` with `owner`, `repository`, `pull_number`, and optional `verify`, `llm`, `persist`, and `refresh_head` fields. It delegates to the same `PullRequestAnalysisService` used by the CLI. GitHub access is read-only; persistence is disabled unless explicitly requested.

## Offline evaluation and limits

Run the authored mocked workflow scenarios without network access:

```sh
python -m changeguard.cli evaluate --github
```

These cases cover a stable head, a head that changes during analysis, authentication failure, and exact-revision cache reuse. Unit tests also cover provider parsing, bounded retries, rate-limit metadata, malformed JSON, immutable SHA fetch behavior, credential handling, and persistence.

The normal suite does not call GitHub. A live API run was not part of offline verification. GitHub may reject fetching a commit SHA for repositories whose object is no longer reachable; such a fetch fails explicitly. Changed-file metadata is capped at 1,000 API entries and an incomplete list fails closed. Verification remains subject to the existing supported-project and bubblewrap restrictions.
