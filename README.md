# ChangeGuard

ChangeGuard is an AI software engineering agent that analyzes GitHub pull requests using deterministic code intelligence, MCP tool calling, RAG-based engineering evidence retrieval, sandbox verification, and grounded LLM investigation.

## Problem

Large codebases make it difficult to understand the real impact of a pull request. A diff shows what changed, but not which methods, APIs, frontend consumers, historical signals, or verification gaps matter before merge.

## Solution

ChangeGuard builds an evidence-backed understanding of software changes. It uses immutable revision analysis, code intelligence, cross-language API mapping, local evidence retrieval, deterministic risk scoring, and a bounded AI investigation to explain what changed and what may be affected.

## Architecture

```text
Developer PR
    ↓
GitHub Integration
    ↓
Code Intelligence
    ↓
API Impact Graph
    ↓
RAG Evidence
    ↓
Risk Engine
    ↓
Verification Sandbox
    ↓
AI Agent
    ↓
Grounded Report
```

## Features

- GitHub PR analysis
- Java code intelligence
- Cross-language API impact
- RAG evidence retrieval
- MCP tool-based AI agent
- deterministic risk engine
- sandbox verification
- evaluation framework

## Demo

The local demo is a lightweight, browser-based presentation layer around the existing ChangeGuard analysis flow. It can load a real PR result or a realistic saved sample without requiring GitHub credentials.

<p align="center">
  <img src="docs/images/demo-landing.png" alt="ChangeGuard landing page" width="920" />
</p>

<p align="center">
  <img src="docs/images/demo-dashboard.png" alt="ChangeGuard PR analysis dashboard" width="920" />
</p>

<p align="center">
  <img src="docs/images/demo-risk.png" alt="ChangeGuard risk analysis" width="920" />
</p>

<p align="center">
  <img src="docs/images/demo-api-impact.png" alt="ChangeGuard API impact graph" width="920" />
</p>

<p align="center">
  <img src="docs/images/demo-investigation-report.png" alt="ChangeGuard AI investigation report" width="920" />
</p>

## Running locally

Python 3.12 or later is required.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"   # or: python -m pip install -r requirements.txt
cp .env.example .env                # optional: add GITHUB_TOKEN / GEMINI_API_KEY
changeguard demo
```

Then open <http://127.0.0.1:8000> in a browser.

For a private repository, set `GITHUB_TOKEN` in `.env` or the environment before starting the demo server. The browser never receives GitHub credentials.

ChangeGuard reads `.env` from the current directory or its parents (up to the git root), or from the path in `CHANGEGUARD_ENV_FILE`. Variables already set in the environment take precedence. See [`.env.example`](.env.example) for every setting.

### Command-line usage

```sh
changeguard pr analyze OWNER/REPO#123              # analyze a GitHub pull request
changeguard impact --repo . --base HEAD~1 --head HEAD   # analyze a local commit range
changeguard evaluate --all                          # run the local evaluation fixtures
python -m pytest                                    # run the test suite
```

### Optional LLM investigation

Deterministic analysis is the default and makes no LLM calls. Add `--llm` to `pr analyze` or `impact` (or tick the LLM option in the demo) to add a bounded AI investigation. Choose the provider with `--llm-provider ollama|gemini` or set `CHANGEGUARD_LLM_PROVIDER` in `.env`; the demo always uses `CHANGEGUARD_LLM_PROVIDER`.

- **Ollama** (default): runs locally. Install [Ollama](https://ollama.com) and pull the model, e.g. `ollama pull qwen2.5:1.5b`.
- **Gemini**: set `GEMINI_API_KEY` in `.env`, then for example:

  ```sh
  changeguard pr analyze OWNER/REPO#123 --llm --llm-provider gemini
  ```

The Gemini provider only receives bounded investigation prompts and read-only MCP results. It is limited to one MCP tool call, 12,000 prompt characters, 800 output tokens, and a three-request budget per analysis. If its API request, quota, or response fails, ChangeGuard retains the deterministic analysis rather than failing the run. The key is never persisted, logged, or committed.

### Optional sandboxed verification

`--verify` runs planned checks inside a Linux [bubblewrap](https://github.com/containers/bubblewrap) sandbox, and reports `BLOCKED` if `bwrap` is missing. On Debian/Ubuntu: `sudo apt install bubblewrap`.

## Example Output

```text
PR: artham-bhardwaj/self-healing-platform#1
Changed files: 2
Changed symbols: 2 methods
API impact: 14 endpoints · 13 consumers · 10 deterministic edges
Risk: LOW · 7.96 / 100 (47.86% weight coverage)
Verification: BLOCKED · Java/Maven unsupported by the configured verifier
LLM: sample report only; no live model output was recorded for the saved PR
```

## Limitations

- Java and API resolution are intentionally conservative and may leave some relationships unresolved.
- Verification is opt-in and limited to the existing supported checks.
- Grounded AI investigation depends on the local environment and the available evidence.
- The demo is intended for loopback-only local use and does not add authentication, cloud hosting, or persistent storage.
