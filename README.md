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

- Local web app with dashboard, saved reports, projects, evidence, settings and a built-in guide
- GitHub PR analysis, plus automatic PR comments through a GitHub Action
- Review gate (`--fail-on`) that holds risky changes, such as AI-agent pushes, until a senior developer approves
- Code intelligence for Python, TypeScript/JavaScript and Java
- Cross-language API impact (FastAPI, Flask, Express, Fastify, Hono, Next.js and Spring endpoints matched to `fetch`/`axios` callers)
- One-command project setup (`changeguard init`)
- RAG evidence retrieval
- MCP tool-based AI agent
- deterministic risk engine
- sandbox verification (pytest for Python; type-check and `npm test` for JS/TS)
- evaluation framework

## Web app

```sh
changeguard ui     # opens on http://127.0.0.1:8000
```

The web app runs locally and has:

- **Dashboard**: recent reports and a setup check.
- **New analysis**: a GitHub PR link, or commits in a local repository (latest commit, branch vs main, or any range).
- **Reports**: every analysis is saved, with risk, affected code, API callers, history, tests and AI findings. Copy or download a report as Markdown.
- **Projects**: add a repository; ChangeGuard detects its languages and frameworks.
- **Evidence**: add and search incident notes, decisions and runbooks.
- **Settings**: AI provider, Gemini key and GitHub token.
- **Guide**: step-by-step instructions.

Open **Reports → Sample report** to see a saved analysis of a real PR without setting anything up.

## Run with Docker

The easiest way to run ChangeGuard, and to share it: anyone with Docker can start it with one command.

```sh
docker run -d --name changeguard -p 127.0.0.1:8000:8000 \
  -v changeguard-data:/data \
  -v "$HOME/projects:$HOME/projects" \
  ghcr.io/artham-bhardwaj/changeguard
```

Open <http://127.0.0.1:8000>. Replace `$HOME/projects` with the folder that holds your git repositories;
it is mounted at the same path, so paths you type in the app match your computer. Reports, projects and
settings live in the `changeguard-data` volume.

Or, from a clone of this repository:

```sh
docker compose up -d        # builds or pulls the image, then serves http://127.0.0.1:8000
docker compose down         # stop (data is kept)
```

`docker-compose.yml` reads an optional `.env` next to it: `PROJECTS_DIR`, `CHANGEGUARD_PORT`,
`GEMINI_API_KEY`, `GITHUB_TOKEN` (all optional; keys can also be set on the Settings page).

CLI commands work too:

```sh
docker run --rm -v "$PWD:/repo" -w /repo ghcr.io/artham-bhardwaj/changeguard impact
docker run --rm -e GITHUB_TOKEN ghcr.io/artham-bhardwaj/changeguard pr analyze OWNER/REPO#12
```

Notes for Docker:

- The app has no login. Keep the port bound to `127.0.0.1` as above, and don't expose it on a network.
- `gh auth login` isn't available inside the container: use a token for private repositories.
- An Ollama server on the host is reached at `host.docker.internal:11434` (set up by `docker-compose.yml`).
- Sandboxed tests (`--verify`) don't run inside Docker, because Docker's security profile blocks the sandbox. Use a pip install for that.
- If your user ID isn't 1000, set `CHANGEGUARD_UID` / `CHANGEGUARD_GID` so the container can write to your projects (`changeguard init`).

### Publishing the image

`.github/workflows/docker-publish.yml` builds the image for amd64 and arm64 and publishes it to
GitHub Container Registry, free for public repositories: `latest` on every push to `main`, and
version tags (`1.2.3`) when you push a `v1.2.3` tag. After the first run, open the package on GitHub
(your profile → Packages → changeguard → Package settings) and make sure its visibility is **Public**
so others can pull it without logging in.

## Quick start

Python 3.12 or later is required.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"   # or: python -m pip install -r requirements.txt
```

To use the `changeguard` command from any directory, put settings in the user-wide file
`~/.config/changeguard/.env` (see [`.env.example`](.env.example)):

```sh
mkdir -p ~/.config/changeguard
cp .env.example ~/.config/changeguard/.env   # then fill in GEMINI_API_KEY etc.
```

ChangeGuard loads settings from, in order of priority: real environment variables,
`CHANGEGUARD_ENV_FILE` or the nearest `.env` in the current directory or its parents
(up to the git root), then `~/.config/changeguard/.env`.

Then set up each project once:

```sh
cd path/to/your/project
changeguard init                  # detect languages, create .changeguard/evidence/, import docs
changeguard init --github-action  # also report on every pull request (see below)
changeguard doctor                # check what is installed and configured
```

GitHub access uses `GITHUB_TOKEN`/`GH_TOKEN` if set, otherwise the token of a logged-in
[GitHub CLI](https://cli.github.com) (`gh auth login`). Public repositories work without a token.
Set `CHANGEGUARD_USE_GH_TOKEN=0` to disable the `gh` fallback.

## Usage

### Review a pull request

```sh
changeguard pr analyze OWNER/REPO#123
changeguard pr analyze https://github.com/OWNER/REPO/pull/123 --llm   # add an AI investigation
changeguard pr analyze OWNER/REPO#123 -o review.md                   # also save the report
changeguard pr analyze OWNER/REPO#123 --json                         # full structured result
```

The report covers the risk score and its factors, changed files and methods, changed API
endpoints with the frontend code that calls them, related past incidents, the AI findings,
recommended checks, and the analysis limitations.

### Analyze local commits

```sh
cd path/to/repo
changeguard impact                              # HEAD~1..HEAD
changeguard impact --base main --head my-branch
changeguard impact --repo ../other --base v1.2 --head v1.3 --llm
```

### Review gate: hold risky changes for a senior developer

ChangeGuard can stop a change, for example one pushed by an AI coding agent, until a senior
developer has reviewed it. Add `--fail-on LEVEL` (`low`, `medium`, `high`, `critical`): the command
exits with code **4** when the risk is at or above that level, or when the analysis is incomplete
or the risk is unknown. Code 0 means the change passed.

```sh
changeguard impact --base origin/main --head HEAD --fail-on high   # e.g. in a pre-push hook
changeguard pr analyze OWNER/REPO#123 --fail-on high               # e.g. as a CI check
```

The risk level is raised to at least HIGH by **review triggers**, whatever the size of the change:

- a removed check or error path (`if`, `assert`, `raise`, an error redirect), including removed test assertions;
- a removed access or permission check (`login_required`, `Depends(auth…)`, `@PreAuthorize`, …);
- new shell commands or `eval`, new data or file deletion, or what looks like a hard-coded secret
  (the value is never shown; test files are skipped for these three);
- changes to CI workflows or `CODEOWNERS`;
- changed functions whose names mention sending, deleting, auth, secrets or payments;
- changed code called from 25 or more places.

Dependency or build-file changes raise the level to at least MEDIUM. The score itself is unchanged,
and the report lists each trigger and the level it raised.

For pull requests, an approving review of the **latest commit** clears the gate. By default it must
come from a repository owner, member or collaborator who is not the PR author and not a bot. Use
`--approvers alice,bob` or `CHANGEGUARD_APPROVERS` to name the seniors who may approve. A new push, a
"changes requested" review or a dismissed review closes the gate again.

### Report on every pull request

`changeguard init --github-action` writes `.github/workflows/changeguard.yml`. Commit and push it.
On each PR, and again whenever a review is submitted or dismissed, the workflow:

1. installs ChangeGuard from this repository;
2. imports the repository's evidence (`changeguard evidence sync`);
3. runs `changeguard pr analyze ... --comment --fail-on high`, which posts the report as a single PR
   comment (updated on every push) and **fails the "ChangeGuard review gate" check** until a senior approves.

To make the check block merging, open the repository's **Settings → Branches**, add a rule for the main
branch, enable **Require status checks to pass**, and select **ChangeGuard review gate**. Also add a
`CODEOWNERS` entry such as `/.github/ @your-senior-team` with **Require review from Code Owners**, so a
pull request cannot weaken the workflow itself without a senior's approval.

Optional settings: the repository variable `CHANGEGUARD_APPROVERS` (comma-separated logins), the secret
`GEMINI_API_KEY` for an AI investigation, and `changeguard init --github-action --fail-on critical`
for a different threshold. To post a comment from your own machine, use
`changeguard pr analyze OWNER/REPO#123 --comment`.

### Add historical evidence

Past incidents, postmortems, runbooks and architecture decisions make the risk score and
AI investigation more useful. Evidence is matched to the repository it belongs to
(`OWNER/REPO` for GitHub clones).

```sh
changeguard evidence add incidents/*.md --type incident --repo OWNER/REPO
changeguard evidence add docs/adr/*.md --type architecture_decision   # repo inferred from cwd
changeguard evidence list
changeguard evidence search "payment retry timeout"
```

The easiest way is to commit Markdown files to `.changeguard/evidence/incidents/`, `decisions/`
or `runbooks/` (or `docs/adr/`, `docs/incidents/`, `docs/runbooks/`), then run:

```sh
changeguard evidence sync     # also runs automatically in the GitHub Action
```

Types: `incident`, `postmortem`, `runbook`, `architecture_decision`, `issue`, `pull_request`, `release_note`.

Analyses and evidence are stored in `~/.local/share/changeguard/changeguard.sqlite3`
(override with `CHANGEGUARD_DATABASE` or `--database`).

### AI investigation (optional)

Deterministic analysis is the default and makes no LLM calls. Add `--llm` (or tick **AI investigation**
in the web app) for a bounded AI investigation. Choose the provider with
`--llm-provider ollama|gemini` or `CHANGEGUARD_LLM_PROVIDER`; the web app uses the provider chosen in Settings.

- **Gemini:** set `GEMINI_API_KEY` and `CHANGEGUARD_LLM_PROVIDER=gemini`.
- **Ollama:** runs locally. Install [Ollama](https://ollama.com) and pull the model, e.g. `ollama pull qwen2.5:1.5b`.

The Gemini provider only receives bounded investigation prompts and read-only MCP results. It is
limited to one MCP tool call, 12,000 prompt characters, 800 output tokens (thinking set to `low`
so it does not consume the budget), and a three-request budget per analysis; temporary API errors
(429/5xx) are retried twice without using that budget. If Gemini fails, ChangeGuard keeps the
deterministic analysis and notes the failure in the report. The key is never persisted, logged,
or committed.

### Sandboxed verification (optional)

`--verify` (or **Run tests in a sandbox** in the web app) runs checks for the changed code inside a
Linux [bubblewrap](https://github.com/containers/bubblewrap) sandbox with no network, on a
throw-away copy of the code:

- **Python**: the matching pytest files and `py_compile`.
  Tests are matched by file name, by importing a changed module, or by requesting a changed endpoint's
  URL (e.g. `client.post("/orders/7/pay")`). Python checks use the project's `.venv` (or `venv`) when it
  has one, so install the project's dependencies and pytest there.
- **JavaScript/TypeScript**: `npm run typecheck` (or `tsc --noEmit`) and `npm test` in the changed
  package. Run `npm install` first: the sandbox reuses your local `node_modules` read-only and cannot download.

Checks are reported as `BLOCKED` if `bwrap` is missing (`sudo apt install bubblewrap` on Debian/Ubuntu).
Java/Maven projects are not verified.

### Development

```sh
python -m pytest
changeguard evaluate --all
```

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

- Supported languages: Python, TypeScript/JavaScript and Java. Other languages are tracked per file and line only.
- Call and API resolution is conservative. Dynamic imports, dependency injection and URLs built at runtime are reported as unresolved rather than guessed.
- Django URL routes are not detected yet.
- Verification is opt-in and limited to the checks listed above.
- AI investigation quality depends on the provider and on the evidence you have added.
- The web app has no login and only listens on localhost. Do not expose it to a network.
