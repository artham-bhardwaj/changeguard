from __future__ import annotations

import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

from changeguard.webapp import WebAppHandler
from tests.conftest import git


@pytest.fixture
def app(tmp_path):
    handler = type("Handler", (WebAppHandler,), {"database_path": str(tmp_path / "app.sqlite3")})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=60)
        sent = {"Host": f"127.0.0.1:{server.server_port}", **({"X-ChangeGuard": "1"} if method == "POST" else {})}
        sent.update(headers or {})
        payload = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        if payload is not None:
            sent["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        is_json = response.getheader("Content-Type", "").startswith("application/json")
        return response.status, json.loads(raw) if is_json else raw.decode()

    yield call
    server.shutdown()
    server.server_close()


def make_repo(tmp_path):
    repo = tmp_path / "shop"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "remote", "add", "origin", "git@github.com:owner/shop.git")
    (repo / "prices.py").write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
    (repo / "cart.py").write_text("from prices import total\n\ndef checkout(items):\n    return total(items)\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    (repo / "prices.py").write_text("def total(items):\n    return round(sum(items), 2)\n", encoding="utf-8")
    git(repo, "commit", "-am", "round totals")
    return repo


def test_static_app_is_served_with_strict_csp(app):
    status, page = app("GET", "/")
    assert status == 200 and "ChangeGuard" in page and '<script src="/app.js">' in page
    assert app("GET", "/app.js")[0] == 200 and app("GET", "/styles.css")[0] == 200
    assert app("GET", "/../pyproject.toml")[0] == 404


def test_requests_from_other_sites_are_rejected(app):
    assert app("GET", "/api/status", headers={"Host": "evil.example:8000"})[0] == 403
    status, body = app("POST", "/api/settings", {"provider": "gemini"}, headers={"X-ChangeGuard": ""})
    assert status == 403 and "X-ChangeGuard" in body["error"]
    status, _ = app("POST", "/api/settings", {"provider": "gemini"}, headers={"Origin": "https://evil.example"})
    assert status == 403


def test_add_project_analyze_and_browse_reports(app, tmp_path):
    repo = make_repo(tmp_path)
    status, project = app("POST", "/api/projects", {"path": str(repo), "evidence_folders": True})
    assert status == 200, project
    assert project["repository"] == "owner/shop" and project["languages"] == {"Python": 2}
    assert (repo / ".changeguard" / "evidence" / "incidents").is_dir()
    assert [p["name"] for p in app("GET", "/api/projects")[1]] == ["shop"]

    revisions = app("GET", f"/api/revisions?path={repo}")[1]
    assert revisions["branches"] == ["main"] and revisions["commits"][0]["subject"] == "round totals"

    status, run = app("POST", "/api/analyze/local", {"path": str(repo)})
    assert status == 200, run
    reports = app("GET", "/api/reports")[1]
    assert reports[0]["id"] == run["report_id"] and reports[0]["repository"] == "owner/shop"
    report = app("GET", f"/api/reports/{run['report_id']}")[1]
    analysis = report["payload"]["analysis"]
    assert [s["qualified_name"] for s in analysis["changed_symbols"]] == ["prices.total()"]
    assert any("checkout" in str(item) for item in analysis["blast_radius"]["direct_dependents"])
    assert report["markdown"].startswith("#")

    assert app("POST", f"/api/reports/{run['report_id']}/delete")[1] == {"deleted": True}
    assert app("GET", f"/api/reports/{run['report_id']}")[0] == 404


def test_bad_input_gets_friendly_errors(app, tmp_path):
    assert "full path" in app("POST", "/api/analyze/local", {"path": "relative/dir"})[1]["error"]
    status, body = app("POST", "/api/analyze/local", {"path": str(make_repo(tmp_path)), "base": "nope"})
    assert status == 400 and "not a commit" in body["error"]
    assert "pull request URL" in app("POST", "/api/analyze/pr", {"url": "https://example.com/x"})[1]["error"]
    assert app("POST", "/api/analyze/pr", {"repository": "o/r", "pull_number": 1, "llm": "yes"})[0] == 400


def test_evidence_add_list_and_search(app):
    status, added = app("POST", "/api/evidence", {
        "title": "Checkout outage", "source_type": "incident", "repository": "Owner/Shop",
        "content": "prices.total returned floats with rounding errors during checkout.",
    })
    assert status == 200 and added["duplicate"] is False
    assert [d["title"] for d in app("GET", "/api/evidence?repository=owner/shop")[1]] == ["Checkout outage"]
    assert app("GET", "/api/evidence/search?q=rounding+checkout&repository=owner/shop")[1][0]["title"] == "Checkout outage"
    assert app("POST", "/api/evidence", {"title": "x", "source_type": "secret", "repository": "o/r", "content": "y"})[0] == 400


def test_settings_are_saved_without_exposing_secrets(app, monkeypatch, tmp_path):
    for name in ("CHANGEGUARD_LLM_PROVIDER", "GEMINI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    status, settings = app("POST", "/api/settings", {"provider": "gemini", "gemini_api_key": "secret-key-123"})
    assert status == 200 and settings["provider"] == "gemini" and settings["gemini_key_set"] is True
    assert "secret-key-123" not in json.dumps(settings)
    from changeguard.llm.config import user_env_file

    env_file = user_env_file()
    assert "GEMINI_API_KEY=secret-key-123" in env_file.read_text(encoding="utf-8")
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"

    assert app("POST", "/api/settings", {"gemini_api_key": None})[1]["gemini_key_set"] is False
    assert "GEMINI_API_KEY" not in env_file.read_text(encoding="utf-8")
    assert app("POST", "/api/settings", {"gemini_model": "a\nGITHUB_TOKEN=x"})[0] == 400


def test_extra_allowed_hosts_and_container_binding(app, monkeypatch):
    assert app("GET", "/api/status", headers={"Host": "changeguard.lan"})[0] == 403
    monkeypatch.setenv("CHANGEGUARD_ALLOWED_HOSTS", "changeguard.lan")
    assert app("GET", "/api/status", headers={"Host": "changeguard.lan:8000"})[0] == 200

    from changeguard import webapp

    monkeypatch.setenv("CHANGEGUARD_DEMO_HOST", "0.0.0.0")
    monkeypatch.delenv("CHANGEGUARD_CONTAINER", raising=False)
    with pytest.raises(SystemExit, match="only binds to localhost"):
        webapp.main()
