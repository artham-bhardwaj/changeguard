"""Run analyses and save them to history. Shared by the CLI and the web app."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from changeguard.agent.agent import LocalTools
from changeguard.analysis import ChangeAnalysisService
from changeguard.github import GitHubAPIAdapter, PullRequestAnalysisService, PullRequestRef
from changeguard.history import HistoryStore
from changeguard.llm import create_llm_adapter, max_tool_calls_for
from changeguard.llm.agent import BoundedInvestigationAgent
from changeguard.mcp import MCPToolPort
from changeguard.report import render_markdown
from changeguard.settings import github_token, repository_key
from changeguard.storage import SQLiteAnalysisStore


@dataclass
class RunResult:
    payload: dict[str, Any]
    markdown: str | None
    report_id: str | None
    token_available: bool = True


def resolve_commit(repo: str, revision: str) -> str:
    """Resolve HEAD~1, a branch or a tag to a full commit SHA; ValueError if it is not a commit."""
    completed = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"{revision!r} is not a commit in {repo}")
    return completed.stdout.strip()


def analyze_pull_request(
    reference: PullRequestRef,
    *,
    database: str,
    llm: bool = False,
    verify: bool = False,
    llm_provider: str = "ollama",
    persist: bool = False,
    refresh_head: bool = True,
    save: bool = True,
) -> RunResult:
    token = github_token()
    result = PullRequestAnalysisService(
        GitHubAPIAdapter(token=token), SQLiteAnalysisStore(database), token=token, llm_provider=llm_provider
    ).analyze(reference, verify=verify, llm=llm, persist=persist, refresh_head=refresh_head)
    payload = result.to_dict()
    markdown = render_markdown(result.analysis, pr=payload) if result.analysis else None
    report_id = None
    if save and result.analysis:
        report_id = HistoryStore(database).save_report(
            kind="pr", target=f"{reference.full_name}#{reference.number}",
            repository=reference.full_name.lower(), payload=payload,
        )
    return RunResult(payload, markdown, report_id, token is not None)


def analyze_local(
    repo_path: str,
    base: str = "HEAD~1",
    head: str = "HEAD",
    *,
    database: str,
    llm: bool = False,
    verify: bool = False,
    llm_provider: str = "ollama",
    max_tool_calls: int = 3,
    save: bool = True,
) -> RunResult:
    """Analyze ``base..head`` of a local repository. Raises ValueError for bad input."""
    repo = str(Path(repo_path).expanduser().resolve())
    if not Path(repo).is_dir():
        raise ValueError(f"{repo_path} is not a directory")
    base_sha, head_sha = resolve_commit(repo, base), resolve_commit(repo, head)
    store = SQLiteAnalysisStore(database)
    investigator = None
    if llm:
        investigator = BoundedInvestigationAgent(
            create_llm_adapter(llm_provider),
            MCPToolPort(evidence_database=database),
            max_tool_calls=max_tool_calls_for(llm_provider, max_tool_calls),
        )
    # Read the repository directly; the MCP server is only used by the AI investigation.
    service = ChangeAnalysisService(store, LocalTools(store), investigator=investigator)
    result = service.analyze(
        repo_path=repo, base=base_sha, head=head_sha, evidence_repository=repository_key(repo)
    )
    if verify:
        result = service.verify(result)
    analysis = result.to_dict()
    payload = {"status": "COMPLETED", "repo_path": repo, "base": base, "head": head, "analysis": analysis}
    report_id = None
    if save:
        report_id = HistoryStore(database).save_report(
            kind="local", target=f"{Path(repo).name} {base}..{head}",
            repository=repository_key(repo), payload=payload,
        )
    return RunResult(payload, render_markdown(analysis), report_id)


def report_markdown(payload: dict[str, Any]) -> str | None:
    """Markdown for a saved report payload (PR or local)."""
    analysis = payload.get("analysis")
    if not analysis:
        return None
    return render_markdown(analysis, pr=payload if "pull_number" in payload else None)
