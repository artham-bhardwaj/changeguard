# ChangeGuard local demo

The demo is a lightweight local browser interface around the existing ChangeGuard PR analysis service. It is a presentation layer only: PR retrieval, code intelligence, API impact, evidence retrieval, risk, verification, and LLM behavior remain owned by their existing components.

## Run locally

From the repository root, using Python 3.12 or later:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m changeguard.demo_server
```

Open <http://127.0.0.1:8000>. Stop the server with Ctrl+C.

The presentation server uses Python's standard library; it adds no dependency and stores no analyses in a database. By default it listens only on `127.0.0.1:8000`. `CHANGEGUARD_DEMO_PORT` may select a different local port. The server rejects non-loopback host configuration.

### Live pull-request analysis

Enter a GitHub repository (`OWNER/REPO`) and PR number, choose any optional verification or local LLM setting, then select **Analyze Pull Request**. The server calls the existing `PullRequestAnalysisService`; JSON results are passed directly to the browser for display. Public repositories can be analyzed without credentials. For private repositories, configure a read-only `GITHUB_TOKEN` in the server process environment before launch. Do not place tokens in the browser or source files.

Verification and local LLM investigation are opt-in. Verification may be blocked when the existing verifier does not support the repository's language/build system. LLM investigation requires an available local Ollama service and configured model; the demo never downloads a model.

The interface and local API are unauthenticated and intended only for local use. Keep the server bound to loopback; do not expose it to a network.

## Demo workflow

1. Open the landing page and review the local-first analysis options.
2. Select **Load Demo Analysis** to view the saved self-healing-platform PR example without GitHub authentication or Ollama.
3. Review changed files/symbols and the resolved frontend consumer in the API impact graph.
4. Inspect risk factors, score coverage, uncertainties, and recommended verification.
5. Review the actual saved verification state. For this Java/Maven PR it is `BLOCKED`; no checks were executed by ChangeGuard's verifier.
6. Open the report section. The example findings include evidence references, and are marked as a **sample report, not live LLM output** because an Ollama run was unavailable for the recorded PR analysis.
7. To analyze another PR, return to the landing page and submit its repository and number.

## Local API

- `GET /` — demo UI
- `GET /api/demo` — saved sample in the serialized PR-analysis shape
- `POST /api/analyze` — delegates a validated repository/PR request to the existing analysis service

The analysis endpoint accepts JSON with `repository`, `pull_number`, `verify`, and `llm`. It places a small request-size limit, validates identifiers and option types, and uses the existing GitHub and analysis adapters. The demo server does not provide a second analysis implementation.

## Architecture

```text
Browser UI (plain HTML/CSS/JavaScript)
    │ JSON over localhost
    ▼
ChangeGuard local demo HTTP server (Python standard library)
    │
    ▼
Existing PullRequestAnalysisService
    │
    ├── GitHub integration and immutable temporary checkout
    └── Existing ChangeAnalysisService and opt-in capabilities
```

The sample JSON is a development/demo fallback based on the recorded PR #1 analysis. Its report is demonstrative content with cited source and API references; it is clearly labeled and must not be mistaken for a live LLM result. Live responses are consumed from `PRAnalysisResult.to_dict()` and its nested `ChangeGuardAnalysis.to_dict()` without reimplementing business logic.

## Screenshots

Screenshots are generated from the running local app and kept in `docs/images/`:

### Landing page

![ChangeGuard demo landing page](images/demo-landing.png)

### PR analysis dashboard

![ChangeGuard PR analysis dashboard](images/demo-dashboard.png)

### Risk analysis

![ChangeGuard risk analysis](images/demo-risk.png)

### API impact graph

![ChangeGuard API impact graph](images/demo-api-impact.png)

### Grounded investigation report

![ChangeGuard grounded investigation report](images/demo-investigation-report.png)

The report screenshot shows the saved demo fixture, not a live local-model run.
