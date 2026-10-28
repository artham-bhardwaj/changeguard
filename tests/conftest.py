from __future__ import annotations

import subprocess

import pytest


def git(repo, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """Keep tests independent of the developer's ~/.config and ~/.local settings."""
    home = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.delenv("CHANGEGUARD_DATABASE", raising=False)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    (repo / "app.txt").write_text("first line\n", encoding="utf-8")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-m", "initial commit")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "app.txt").write_text("first line\nsecond line\n", encoding="utf-8")
    (repo / "new.txt").write_text("new file\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add a second line")
    return repo, base, git(repo, "rev-parse", "HEAD")
