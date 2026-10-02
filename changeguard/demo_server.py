from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from changeguard.github import (
    GitHubAPIAdapter,
    GitHostingError,
    PullRequestAnalysisService,
    PullRequestRef,
)
from changeguard.storage.sqlite import SQLiteAnalysisStore

_ASSETS = Path(__file__).resolve().parent.parent / "demo"
_SAMPLE = _ASSETS / "demo-analysis.json"
_MAX_REQUEST_BYTES = 16 * 1024
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ASSET_TYPES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/docs/demo.md": ("../docs/demo.md", "text/markdown; charset=utf-8"),
}


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "ChangeGuardDemo/1.0"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/demo":
            self._send_json(200, json.loads(_SAMPLE.read_text(encoding="utf-8")))
            return
        asset = _ASSET_TYPES.get(path)
        if asset is None:
            self.send_error(404)
            return
        filename, content_type = asset
        body = (_ASSETS / filename).resolve().read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/analyze":
            self.send_error(404)
            return
        try:
            request = self._read_json()
            repository = request.get("repository")
            number = request.get("pull_number")
            verify = request.get("verify", False)
            llm = request.get("llm", False)
            if not isinstance(repository, str) or not _REPOSITORY_NAME.fullmatch(repository):
                raise ValueError("Enter a repository as OWNER/REPO.")
            if type(number) is not int or number < 1:
                raise ValueError("Pull request number must be a positive integer.")
            if type(verify) is not bool or type(llm) is not bool:
                raise ValueError("Analysis options must be true or false.")
            owner, name = repository.split("/", 1)
            result = PullRequestAnalysisService(
                GitHubAPIAdapter(token=os.environ.get("GITHUB_TOKEN")),
                SQLiteAnalysisStore(":memory:"),
                token=os.environ.get("GITHUB_TOKEN"),
            ).analyze(
                PullRequestRef(owner, name, number),
                verify=verify,
                llm=llm,
                persist=False,
                refresh_head=True,
            )
            self._send_json(200, result.to_dict())
        except (ValueError, GitHostingError, OSError, RuntimeError) as error:
            self._send_json(400, {"error": str(error)})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[changeguard-demo] {format % args}")

    def _read_json(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length", "")
        if not length_header.isdecimal():
            raise ValueError("A valid Content-Length header is required.")
        length = int(length_header)
        if length > _MAX_REQUEST_BYTES:
            raise ValueError("Request body is too large.")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Request body must be valid JSON.") from error
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object.")
        return value

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    host = os.environ.get("CHANGEGUARD_DEMO_HOST", "127.0.0.1")
    port = int(os.environ.get("CHANGEGUARD_DEMO_PORT", "8000"))
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("The demo server only binds to localhost.")
    print(f"ChangeGuard demo ready at http://{host}:{port}")
    ThreadingHTTPServer((host, port), DemoHandler).serve_forever()


if __name__ == "__main__":
    main()
