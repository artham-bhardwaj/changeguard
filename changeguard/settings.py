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


# Settings the web app may change; values are single-line strings.
EDITABLE_SETTINGS = {
    "CHANGEGUARD_LLM_PROVIDER",
    "GEMINI_API_KEY",
    "CHANGEGUARD_GEMINI_MODEL",
    "CHANGEGUARD_MODEL",
    "CHANGEGUARD_OLLAMA_BASE_URL",
    "GITHUB_TOKEN",
}


def update_user_settings(values: dict[str, str | None]) -> str:
    """Set (or remove, with None/"") keys in ~/.config/changeguard/.env and the current process.

    Other lines, including comments, are kept. Returns the file path.
    """
    from changeguard.llm.config import user_env_file

    unknown = set(values) - EDITABLE_SETTINGS
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    for name, value in values.items():
        if value is not None and ("\n" in value or "\r" in value or len(value) > 500):
            raise ValueError(f"{name} must be a single line")
    path = user_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(values)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        name = stripped.removeprefix("export ").split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if name in remaining:
            value = remaining.pop(name)
            if value:
                output.append(f"{name}={value}")
            continue
        output.append(line)
    output.extend(f"{name}={value}" for name, value in remaining.items() if value)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    for name, value in values.items():
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)
    return str(path)
