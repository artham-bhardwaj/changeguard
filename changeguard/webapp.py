"""ChangeGuard local web app: a JSON API plus static files, bound to localhost only."""
from __future__ import annotations

import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from changeguard.github import GitHostingError, PullRequestRef
from changeguard.history import HistoryStore
from changeguard.llm import LLM_PROVIDERS, OllamaError, default_llm_provider, load_local_env
from changeguard.settings import (
    default_database_path,
    github_token,
    repository_key,
    update_user_settings,
)
from changeguard.storage import SQLiteAnalysisStore

_STATIC = Path(__file__).resolve().parent / "web"
_SAMPLE = _STATIC / "demo-analysis.json"
_DEMO_GUIDE = Path(__file__).resolve().parent.parent / "docs" / "demo.md"
_MAX_REQUEST_BYTES = 256 * 1024
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PR_URL = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)")
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
_CSP = (
    "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; "
    "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
EVIDENCE_TYPES = ("incident", "postmortem", "runbook", "architecture_decision", "issue", "pull_request", "release_note")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class WebAppHandler(BaseHTTPRequestHandler):
    server_version = "ChangeGuard/2.0"
    database_path: str | None = None  # overridable (tests)

    # ----- plumbing -------------------------------------------------------------------------

    @property
    def database(self) -> str:
        return self.database_path or default_database_path()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        try:
            self._check_host()
            if method == "GET" and url.path in _FILES:
                return self._send_file(*_FILES[url.path])
            if method == "GET" and url.path == "/docs/demo.md":
                return self._send_file(str(_DEMO_GUIDE), "text/markdown; charset=utf-8")
            if not url.path.startswith("/api/"):
                raise ApiError(404, "Not found")
            if method == "POST":
                self._check_same_origin()
            query = {key: values[-1] for key, values in parse_qs(url.query).items()}
            body = self._read_json() if method == "POST" else {}
            status, payload = self._route(method, url.path, query, body)
            self._send_json(status, payload)
        except ApiError as error:
            self._send_json(error.status, {"error": str(error)})
        except (ValueError, GitHostingError, OllamaError) as error:
            self._send_json(400, {"error": str(error)})
        except (OSError, RuntimeError) as error:
            self._send_json(500, {"error": f"{type(error).__name__}: {error}"})

    def _check_host(self) -> None:
        """Reject other Host headers so DNS-rebinding pages cannot reach the API."""
        header = self.headers.get("Host") or ""
        host = header.split("]")[0] + "]" if header.startswith("[") else header.rsplit(":", 1)[0]
        if host.lower() not in _allowed_hosts():
            raise ApiError(403, "Unexpected Host header")

    def _check_same_origin(self) -> None:
        """POSTs must carry X-ChangeGuard (forces a CORS preflight we never approve) and, if sent,
        a localhost Origin. This stops other websites from triggering actions."""
        if self.headers.get("X-ChangeGuard") != "1":
            raise ApiError(403, "Missing X-ChangeGuard header")
        origin = self.headers.get("Origin")
        if origin and (urlsplit(origin).hostname or "").lower() not in _allowed_hosts() | {"::1"}:
            raise ApiError(403, "Cross-origin request rejected")

    def _read_json(self) -> dict[str, Any]:
        length = self.headers.get("Content-Length", "0")
        if not length.isdecimal():
            raise ApiError(400, "A valid Content-Length header is required.")
        if int(length) > _MAX_REQUEST_BYTES:
            raise ApiError(413, "Request body is too large.")
        raw = self.rfile.read(int(length)) if int(length) else b"{}"
        try:
            value = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ApiError(400, "Request body must be valid JSON.") from error
        if not isinstance(value, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        return value

    def _send_json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, name: str, content_type: str) -> None:
        path = Path(name) if os.path.isabs(name) else _STATIC / name
        if not path.is_file():
            raise ApiError(404, "Not found")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", _CSP)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[changeguard] {format % args}", flush=True)

    # ----- routes ---------------------------------------------------------------------------

    def _route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        routes = {
            ("GET", "/api/status"): self._status,
            ("GET", "/api/demo"): lambda: json.loads(_SAMPLE.read_text(encoding="utf-8")),
            ("GET", "/api/reports"): lambda: HistoryStore(self.database).list_reports(
                limit=min(int(query.get("limit", "50")), 200), repository=query.get("repository")
            ),
            ("GET", "/api/projects"): lambda: HistoryStore(self.database).list_projects(),
            ("POST", "/api/projects"): lambda: self._add_project(body),
            ("POST", "/api/projects/remove"): lambda: {"removed": HistoryStore(self.database).remove_project(_text(body, "path"))},
            ("GET", "/api/revisions"): lambda: self._revisions(query.get("path", "")),
            ("POST", "/api/analyze/pr"): lambda: self._analyze_pr(body),
            ("POST", "/api/analyze/local"): lambda: self._analyze_local(body),
            ("POST", "/api/analyze"): lambda: self._legacy_analyze(body),
            ("GET", "/api/evidence"): lambda: self._evidence_list(query.get("repository")),
            ("POST", "/api/evidence"): lambda: self._evidence_add(body),
            ("GET", "/api/evidence/search"): lambda: self._evidence_search(query),
            ("POST", "/api/evidence/sync"): lambda: self._evidence_sync(body),
            ("GET", "/api/settings"): self._settings,
            ("POST", "/api/settings"): lambda: self._save_settings(body),
        }
        handler = routes.get((method, path))
        if handler is not None:
            return 200, handler()
        match = re.fullmatch(r"/api/reports/([0-9a-f]{12})(/delete)?", path)
        if match:
            store = HistoryStore(self.database)
            if match[2] and method == "POST":
                return 200, {"deleted": store.delete_report(match[1])}
            if not match[2] and method == "GET":
                report = store.get_report(match[1])
                if report is None:
                    raise ApiError(404, "Report not found")
                from changeguard.runner import report_markdown

                report["markdown"] = report_markdown(report["payload"])
                return 200, report
        raise ApiError(404, "Not found")

    def _status(self) -> dict[str, Any]:
        from changeguard.project import doctor

        history = HistoryStore(self.database)
        return {
            "checks": [check.__dict__ for check in doctor()],
            "provider": _safe_provider(),
            "projects": len(history.list_projects()),
            "reports": len(history.list_reports(limit=200)),
            "database": self.database,
        }

    def _add_project(self, body: dict[str, Any]) -> dict[str, Any]:
        from changeguard.project import create_evidence_folders, detect_project, sync_evidence, write_github_action

        path = _local_path(body)
        profile = detect_project(path)
        root = Path(profile.path)
        repository = repository_key(str(root))
        created = create_evidence_folders(root) if body.get("evidence_folders") else []
        added, existing, skipped = sync_evidence(SQLiteAnalysisStore(self.database), root, repository)
        workflow = None
        if body.get("github_action"):
            target, written = write_github_action(root)
            workflow = {"path": str(target.relative_to(root)), "created": written}
        HistoryStore(self.database).save_project(str(root), repository, profile.name, profile.languages, profile.frameworks)
        return {
            "path": str(root), "repository": repository, "name": profile.name,
            "languages": profile.languages, "frameworks": profile.frameworks, "notes": profile.notes,
            "created": created, "evidence": {"added": added, "existing": existing, "skipped": skipped},
            "workflow": workflow,
        }

    def _revisions(self, path: str) -> dict[str, Any]:
        repo = _local_path({"path": path})
        log = subprocess.run(
            ["git", "-C", repo, "log", "--max-count=25", "--format=%H%x1f%h%x1f%s%x1f%an%x1f%ar"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        branches = subprocess.run(
            ["git", "-C", repo, "for-each-ref", "--sort=-committerdate", "--count=30", "--format=%(refname:short)", "refs/heads"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        commits = [dict(zip(("sha", "short", "subject", "author", "when"), line.split("\x1f"))) for line in log.stdout.splitlines()]
        return {"commits": commits, "branches": [line for line in branches.stdout.splitlines() if line]}

    def _analyze_pr(self, body: dict[str, Any]) -> dict[str, Any]:
        from changeguard.runner import analyze_pull_request

        reference = _pull_request(body)
        run = analyze_pull_request(
            reference, database=self.database, llm=_flag(body, "llm"), verify=_flag(body, "verify"),
            llm_provider=_safe_provider(),
        )
        payload = run.payload
        if payload.get("status") == "FAILED" or not run.report_id:
            error = payload.get("error") or {}
            message = error.get("message") or "The pull request could not be analyzed."
            if not run.token_available and error.get("code") in {"NOT_FOUND", "ACCESS_DENIED", "AUTHENTICATION_FAILED"}:
                message += " For private repositories, sign in with `gh auth login` or add a GitHub token in Settings."
            raise ApiError(400, message)
        return {"report_id": run.report_id, "status": payload.get("status")}

    def _analyze_local(self, body: dict[str, Any]) -> dict[str, Any]:
        from changeguard.runner import analyze_local

        run = analyze_local(
            _local_path(body), _text(body, "base") or "HEAD~1", _text(body, "head") or "HEAD",
            database=self.database, llm=_flag(body, "llm"), verify=_flag(body, "verify"),
            llm_provider=_safe_provider(),
        )
        return {"report_id": run.report_id, "status": "COMPLETED"}

    def _legacy_analyze(self, body: dict[str, Any]) -> dict[str, Any]:
        """The original demo endpoint: returns the full PR result."""
        from changeguard.runner import analyze_pull_request

        reference = _pull_request(body)
        return analyze_pull_request(
            reference, database=self.database, llm=_flag(body, "llm"), verify=_flag(body, "verify"),
            llm_provider=_safe_provider(),
        ).payload

    def _evidence_list(self, repository: str | None) -> list[dict[str, Any]]:
        documents: dict[str, dict[str, Any]] = {}
        for row in SQLiteAnalysisStore(self.database).list_evidence_chunks(None, repository.lower() if repository else None):
            documents.setdefault(str(row["evidence_id"]), {
                "evidence_id": row["evidence_id"], "title": row["title"], "source": row["source"],
                "source_type": row["source_type"], "preview": str(row["content"])[:240],
            })
        return list(documents.values())

    def _evidence_add(self, body: dict[str, Any]) -> dict[str, Any]:
        from changeguard.evidence.ingest import EvidenceIngestor
        from changeguard.evidence.models import EvidenceDocument

        kind = _text(body, "source_type")
        if kind not in EVIDENCE_TYPES:
            raise ApiError(400, f"Type must be one of: {', '.join(EVIDENCE_TYPES)}")
        repository = _text(body, "repository").lower()
        if not _REPOSITORY_NAME.fullmatch(repository) and not repository.startswith("/"):
            raise ApiError(400, "Repository must be OWNER/REPO (or a registered project).")
        title, content = _text(body, "title"), _text(body, "content")
        if not title or not content.strip():
            raise ApiError(400, "Title and content are required.")
        document, duplicate = EvidenceIngestor(SQLiteAnalysisStore(self.database)).ingest(
            EvidenceDocument(title[:200], kind, "web app", repository, content[:200_000])  # type: ignore[arg-type]
        )
        return {"evidence_id": document.evidence_id, "duplicate": duplicate}

    def _evidence_search(self, query: dict[str, str]) -> list[dict[str, Any]]:
        from changeguard.evidence.retrieval import TfidfLexicalRetriever
        from changeguard.evidence.service import EvidenceService

        text = query.get("q", "").strip()
        if not text:
            return []
        repository = query.get("repository") or None
        bundle = EvidenceService(TfidfLexicalRetriever(SQLiteAnalysisStore(self.database))).search(
            text, 10, None, repository.lower() if repository else None
        )
        return [item.__dict__ for item in bundle.results]

    def _evidence_sync(self, body: dict[str, Any]) -> dict[str, Any]:
        from changeguard.project import git_root, sync_evidence

        root = git_root(_local_path(body))
        repository = repository_key(str(root))
        added, existing, skipped = sync_evidence(SQLiteAnalysisStore(self.database), root, repository)
        return {"repository": repository, "added": added, "existing": existing, "skipped": skipped}

    def _settings(self) -> dict[str, Any]:
        from changeguard.llm.config import GeminiConfig, user_env_file
        from changeguard.project import sandbox_status

        try:
            gemini_model = GeminiConfig.from_env().model
        except ValueError:
            gemini_model = os.getenv("CHANGEGUARD_GEMINI_MODEL", "")
        return {
            "provider": _safe_provider(),
            "providers": list(LLM_PROVIDERS),
            "gemini_key_set": bool(os.getenv("GEMINI_API_KEY")),
            "gemini_model": gemini_model,
            "ollama_model": os.getenv("CHANGEGUARD_MODEL", "qwen2.5:1.5b"),
            "github_token": "environment" if os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
            else ("gh CLI" if github_token() else None),
            "settings_file": str(user_env_file()),
            "sandbox": dict(zip(("ok", "detail"), sandbox_status())),
            "database": self.database,
        }

    def _save_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, str | None] = {}
        provider = body.get("provider")
        if provider is not None:
            if provider not in LLM_PROVIDERS:
                raise ApiError(400, "Unknown provider")
            updates["CHANGEGUARD_LLM_PROVIDER"] = provider
        for field, name in (("gemini_api_key", "GEMINI_API_KEY"), ("gemini_model", "CHANGEGUARD_GEMINI_MODEL"),
                            ("ollama_model", "CHANGEGUARD_MODEL"), ("github_token", "GITHUB_TOKEN")):
            if field in body:
                value = body[field]
                if value is not None and not isinstance(value, str):
                    raise ApiError(400, f"{field} must be text")
                updates[name] = value.strip() if value else None
        update_user_settings(updates)
        return self._settings()


# ----- request helpers ------------------------------------------------------------------------

def _text(body: dict[str, Any], name: str) -> str:
    value = body.get(name, "")
    if not isinstance(value, str):
        raise ApiError(400, f"{name} must be text")
    return value.strip()


def _flag(body: dict[str, Any], name: str) -> bool:
    value = body.get(name, False)
    if type(value) is not bool:
        raise ApiError(400, "Analysis options must be true or false.")
    return value


def _pull_request(body: dict[str, Any]) -> PullRequestRef:
    url = body.get("url")
    if isinstance(url, str) and url.strip():
        match = _PR_URL.match(url.strip())
        if not match:
            raise ApiError(400, "Enter a pull request URL like https://github.com/OWNER/REPO/pull/123.")
        return PullRequestRef(match[1], match[2], int(match[3]))
    repository = body.get("repository")
    number = body.get("pull_number")
    if not isinstance(repository, str) or not _REPOSITORY_NAME.fullmatch(repository):
        raise ApiError(400, "Enter a repository as OWNER/REPO.")
    if type(number) is not int or number < 1:
        raise ApiError(400, "Pull request number must be a positive integer.")
    owner, name = repository.split("/", 1)
    return PullRequestRef(owner, name, number)


def _local_path(body: dict[str, Any]) -> str:
    path = Path(_text(body, "path")).expanduser()
    if not str(path) or not path.is_absolute():
        raise ApiError(400, "Enter the full path of a local git repository (e.g. /home/you/projects/app).")
    if not path.is_dir():
        raise ApiError(400, f"{path} does not exist.")
    return str(path.resolve())


def _allowed_hosts() -> set[str]:
    """localhost names plus CHANGEGUARD_ALLOWED_HOSTS (comma-separated), e.g. for a reverse proxy."""
    extra = {item.strip().lower() for item in os.getenv("CHANGEGUARD_ALLOWED_HOSTS", "").split(",") if item.strip()}
    return _ALLOWED_HOSTS | extra


def _safe_provider() -> str:
    try:
        return default_llm_provider()
    except ValueError:
        return "ollama"


def main() -> None:
    load_local_env()
    in_container = os.environ.get("CHANGEGUARD_CONTAINER") == "1"
    host = os.environ.get("CHANGEGUARD_DEMO_HOST", "0.0.0.0" if in_container else "127.0.0.1")
    port = int(os.environ.get("CHANGEGUARD_DEMO_PORT", os.environ.get("CHANGEGUARD_PORT", "8000")))
    # Inside a container the app must listen on all interfaces so Docker can forward the port;
    # `docker run -p 127.0.0.1:8000:8000` keeps it private to the host machine.
    if host not in {"127.0.0.1", "localhost", "::1"} and not (in_container and host == "0.0.0.0"):
        raise SystemExit("The ChangeGuard web app only binds to localhost.")
    try:
        server = ThreadingHTTPServer((host, port), WebAppHandler)
    except OSError as error:
        raise SystemExit(f"Could not start on {host}:{port} ({error}). Set CHANGEGUARD_PORT to use another port.")
    shown = "127.0.0.1" if host == "0.0.0.0" else host
    if in_container:
        print("Open the port you published, e.g. http://127.0.0.1:8000 (container port 8000).", flush=True)
    print(f"ChangeGuard is running at http://{shown}:{port}  (LLM provider: {_safe_provider()}; Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
