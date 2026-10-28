"""Local settings shared by the CLI and demo server."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

_GITHUB_REMOTE = re.compile(
    r"github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def default_database_path() -> str:
    """Database for analyses and evidence: CHANGEGUARD_DATABASE, else the user data directory."""
    configured = os.getenv("CHANGEGUARD_DATABASE")
    if configured:
        return str(Path(configured).expanduser())
    data_home = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return str(Path(data_home) / "changeguard" / "changeguard.sqlite3")


def repository_key(repo_path: str) -> str:
    """Identify a repository for evidence: ``owner/repo`` (lowercase) for GitHub clones,
    otherwise the absolute path."""
    try:
        remote = subprocess.run(
            ["git", "-C", repo_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        remote = ""
    match = _GITHUB_REMOTE.search(remote)
    if match:
        return f"{match['owner']}/{match['name']}".lower()
    return str(Path(repo_path).resolve())


def github_token() -> str | None:
    """GITHUB_TOKEN/GH_TOKEN from the environment, else the logged-in GitHub CLI's token."""
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        return token
    if os.getenv("CHANGEGUARD_USE_GH_TOKEN", "1") == "0" or shutil.which("gh") is None:
        return None
    try:
        completed = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = completed.stdout.strip()
    return token if completed.returncode == 0 and token else None
