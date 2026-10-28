from __future__ import annotations

import io
import json
import sys
from urllib.error import HTTPError

import pytest

from changeguard.cli import main as cli_main
from changeguard.github.comments import MARKER, CommentError, post_report_comment
from changeguard.github.models import PullRequestRef
from changeguard.history import HistoryStore
from changeguard.project import detect_project, discover_evidence
from tests.conftest import git


class FakeGitHub:
    def __init__(self, existing=None, fail=None):
        self.calls = []
        self.existing = existing or []
        self.fail = fail

    def __call__(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.get_method(), request.full_url, body))
        if self.fail:
            raise HTTPError(request.full_url, self.fail, "error", {}, io.BytesIO(b"{}"))
        if request.get_method() == "GET":
            payload = self.existing
        else:
            payload = {"html_url": "https://github.com/o/r/pull/1#issuecomment-9"}
        return _Response(payload)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


REF = PullRequestRef("o", "r", 1)


def test_comment_is_created_once_then_updated():
    github = FakeGitHub(existing=[{"id": 5, "body": "unrelated"}])
    url = post_report_comment(REF, "# Report", "token", opener=github)
    assert url.endswith("issuecomment-9")
    method, endpoint, body = github.calls[-1]
    assert (method, endpoint) == ("POST", "https://api.github.com/repos/o/r/issues/1/comments")
    assert body["body"].startswith(MARKER) and "# Report" in body["body"]

    github = FakeGitHub(existing=[{"id": 7, "body": f"{MARKER}\nold"}])
    post_report_comment(REF, "# New", "token", opener=github)
    assert github.calls[-1][:2] == ("PATCH", "https://api.github.com/repos/o/r/issues/comments/7")


def test_comment_errors_are_explained():
    with pytest.raises(CommentError, match="needs a GitHub token"):
        post_report_comment(REF, "x", None)
    with pytest.raises(CommentError, match="pull-requests: write"):
        post_report_comment(REF, "x", "token", opener=FakeGitHub(fail=403))


def make_repo(tmp_path):
    repo = tmp_path / "shop"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "remote", "add", "origin", "https://github.com/Owner/Shop.git")
    (repo / "api.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("fastapi\npytest\n", encoding="utf-8")
    (repo / "web").mkdir()
    (repo / "web" / "package.json").write_text('{"dependencies": {"next": "15", "react": "19"}}', encoding="utf-8")
    (repo / "web" / "page.tsx").write_text("export default function Page() { return null }\n", encoding="utf-8")
    (repo / "main.go").write_text("package main\n", encoding="utf-8")
    (repo / "docs" / "adr").mkdir(parents=True)
    (repo / "docs" / "adr" / "0001-use-postgres.md").write_text("# Use Postgres\n\nWe chose Postgres.\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def test_detect_project_reports_languages_frameworks_and_gaps(tmp_path):
    profile = detect_project(str(make_repo(tmp_path)))
    assert profile.languages == {"Python": 1, "TypeScript": 1, "Go": 1}
    assert {"FastAPI", "pytest", "Next.js", "React"} <= set(profile.frameworks)
    assert any(note.startswith("Go:") for note in profile.notes)
    assert any("npm install" in note for note in profile.notes)


def test_init_sets_up_evidence_workflow_and_registers_project(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    incident = repo / ".changeguard" / "evidence" / "incidents"
    database = str(tmp_path / "db.sqlite3")
    monkeypatch.setattr(sys, "argv", ["changeguard", "init", str(repo), "--github-action", "--database", database])
    cli_main()
    out = capsys.readouterr().out
    assert "owner/shop" in out and "evidence 1 document(s) imported" in out
    workflow = (repo / ".github" / "workflows" / "changeguard.yml").read_text(encoding="utf-8")
    assert "changeguard pr analyze" in workflow and "${{ github.event.pull_request.number }}" in workflow
    assert incident.is_dir() and (repo / ".changeguard" / "evidence" / "README.md").is_file()
    assert [p["repository"] for p in HistoryStore(database).list_projects()] == ["owner/shop"]

    (incident / "outage.md").write_text("# Checkout outage\n\nPostgres pool exhausted.\n", encoding="utf-8")
    assert [kind for _, kind in discover_evidence(repo)] == ["incident", "architecture_decision"]
    monkeypatch.setattr(sys, "argv", ["changeguard", "evidence", "sync", str(repo), "--database", database])
    cli_main()
    assert "1 added, 1 already stored" in capsys.readouterr().out


def test_history_keeps_reports(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite3"))
    report_id = store.save_report(kind="pr", target="o/r#1", repository="o/r", payload={
        "status": "COMPLETED", "analysis": {"change_summary": "1 file", "risk_assessment": {"overall_level": "LOW", "score": 3.0}},
    })
    listed = store.list_reports()
    assert listed[0]["id"] == report_id and listed[0]["risk_level"] == "LOW"
    assert store.get_report(report_id)["payload"]["analysis"]["change_summary"] == "1 file"
    assert store.delete_report(report_id) and store.get_report(report_id) is None
