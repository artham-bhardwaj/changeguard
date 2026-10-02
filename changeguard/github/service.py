from __future__ import annotations

import os
import re
import subprocess
import tempfile
import base64
from pathlib import Path
from typing import Callable

from changeguard.analysis.service import ChangeAnalysisService
from changeguard.github.models import (
    GitHostingError,
    GitHubError,
    PRAnalysisResult,
    PullRequestRef,
    RateLimit,
)
from changeguard.github.port import GitHostingPort
from changeguard.llm.agent import BoundedInvestigationAgent
from changeguard.llm.ollama import OllamaLLMAdapter
from changeguard.mcp.adapter import MCPToolPort
from changeguard.storage.sqlite import SQLiteAnalysisStore


class PullRequestAnalysisService:
    """Analyze an immutable GitHub PR revision using the existing analysis pipeline."""

    def __init__(
        self,
        provider: GitHostingPort,
        store: SQLiteAnalysisStore,
        *,
        token: str | None = None,
        analysis_factory: Callable[[SQLiteAnalysisStore, bool, str], ChangeAnalysisService]
        | None = None,
        snapshot_materializer: Callable[
            [Path, str, str, str, str, str | None], None
        ]
        | None = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self._token = token
        self._analysis_factory = analysis_factory or _analysis_service
        self._snapshot_materializer = snapshot_materializer or _initialize_snapshot

    def analyze(
        self,
        reference: PullRequestRef,
        *,
        verify: bool = False,
        llm: bool = False,
        persist: bool = False,
        refresh_head: bool = True,
    ) -> PRAnalysisResult:
        try:
            pull, rate_limit = self.provider.get_pull_request(reference)
            if persist and not verify and not llm:
                cached = self.store.get_pr_analysis(
                    reference.full_name, reference.number, pull.head_sha
                )
                if cached and cached.get("base_sha") == pull.base_sha:
                    return _result_from_dict(cached, cached=True)

            files, files_rate_limit = self.provider.list_pull_request_files(reference)
            if len(files) != pull.changed_files_count:
                raise GitHostingError(
                    GitHubError(
                        "INCOMPLETE_FILE_LIST",
                        "GitHub changed-file metadata exceeded the bounded list returned by the API.",
                    )
                )
            changed_files = tuple(item.path for item in files)
            rate_limit = files_rate_limit
            with tempfile.TemporaryDirectory(prefix="changeguard-pr-") as temp_dir:
                repo = Path(temp_dir) / "repository"
                repo.mkdir()
                if pull.head_repository is None:
                    raise GitHostingError(
                        GitHubError(
                            "HEAD_REPOSITORY_UNAVAILABLE",
                            "The pull request head repository is unavailable.",
                        )
                    )
                self._snapshot_materializer(
                    repo,
                    reference.full_name,
                    pull.head_repository,
                    pull.base_sha,
                    pull.head_sha,
                    self._token,
                )
                local_store = SQLiteAnalysisStore(str(Path(temp_dir) / "analysis.sqlite3"))
                analysis_service = self._analysis_factory(local_store, llm, str(local_store.database_path))
                analysis = analysis_service.analyze(
                    repo_path=str(repo), base=pull.base_sha, head=pull.head_sha
                )
                if verify:
                    analysis = analysis_service.verify(analysis)
                analysis_dict = analysis.to_dict()
                current_head: str | None = None
                status = "COMPLETED"
                if refresh_head:
                    refreshed, rate_limit = self.provider.get_pull_request(reference)
                    current_head = refreshed.head_sha
                    if current_head != pull.head_sha:
                        status = "STALE"

            result = PRAnalysisResult(
                status=status,
                repository=reference.full_name,
                pull_number=reference.number,
                base_sha=pull.base_sha,
                head_sha=pull.head_sha,
                current_head_sha=current_head,
                changed_files=changed_files,
                analysis=analysis_dict,
                head_refreshed=refresh_head,
                rate_limit=rate_limit,
            )
            if persist and status == "COMPLETED" and not verify and not llm:
                self.store.save_pr_analysis(result.to_dict())
            return result
        except GitHostingError as error:
            return PRAnalysisResult(
                status="FAILED",
                repository=reference.full_name,
                pull_number=reference.number,
                base_sha=None,
                head_sha=None,
                current_head_sha=None,
                changed_files=(),
                analysis=None,
                error=error.error,
                head_refreshed=False,
            )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            # Tool exceptions do not contain request credentials; keep only a bounded message.
            return PRAnalysisResult(
                status="FAILED",
                repository=reference.full_name,
                pull_number=reference.number,
                base_sha=None,
                head_sha=None,
                current_head_sha=None,
                changed_files=(),
                analysis=None,
                error=GitHubError("ANALYSIS_FAILED", _safe_error(str(error), self._token)),
                head_refreshed=False,
            )


def _initialize_snapshot(
    repository: Path,
    base_repository: str,
    head_repository: str,
    base_sha: str,
    head_sha: str,
    token: str | None,
) -> None:
    base_reference = PullRequestRef.parse(f"{base_repository}#1")
    head_reference = PullRequestRef.parse(f"{head_repository}#1")
    sources = (
        (f"https://github.com/{base_reference.full_name}.git", base_sha, 1),
        (f"https://github.com/{head_reference.full_name}.git", head_sha, 10),
    )
    environment = os.environ.copy()
    for key in tuple(environment):
        if (
            key == "GIT_CONFIG_COUNT"
            or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
            or key in {
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_ASKPASS",
                "GIT_CURL_VERBOSE",
            }
            or key.startswith("GIT_TRACE")
        ):
            environment.pop(key, None)
    environment.pop("GITHUB_TOKEN", None)
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "protocol.file.allow",
            "GIT_CONFIG_VALUE_1": "never",
            "GIT_CONFIG_KEY_2": "core.hooksPath",
            "GIT_CONFIG_VALUE_2": os.devnull,
        }
    )
    if token:
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        environment["GIT_CONFIG_COUNT"] = "4"
        environment["GIT_CONFIG_KEY_3"] = "http.https://github.com/.extraheader"
        environment["GIT_CONFIG_VALUE_3"] = f"AUTHORIZATION: basic {auth}"

    _git(repository, environment, "init", "--quiet")
    for url, revision, depth in sources:
        _git(
            repository,
            environment,
            "-c",
            "protocol.file.allow=never",
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            f"--depth={depth}",
            url,
            revision,
        )
        resolved = _git(repository, environment, "rev-parse", "--verify", "FETCH_HEAD^{commit}").strip()
        if resolved.lower() != revision.lower():
            raise RuntimeError("GitHub did not provide the requested immutable commit.")
        _git(repository, environment, "checkout", "--quiet", "--detach", resolved)


def _git(repository: Path, environment: dict[str, str], *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        message = result.stderr.strip() or "Git snapshot operation failed."
        raise RuntimeError(_sanitize_git_error(message, environment))
    return result.stdout


def _safe_error(message: str, secret: str | None) -> str:
    sanitized = message.replace(secret, "[redacted]") if secret else message
    return sanitized[:1000] or "Pull request analysis failed."


def _sanitize_git_error(message: str, environment: dict[str, str]) -> str:
    header = environment.get("GIT_CONFIG_VALUE_3", "")
    sanitized = message
    prefix, separator, encoded = header.partition("basic ")
    if separator:
        try:
            credential = base64.b64decode(encoded).decode("utf-8")
            token = credential.partition(":")[2]
            if token:
                sanitized = sanitized.replace(token, "[redacted]")
            sanitized = re.sub(re.escape(encoded), "[redacted]", sanitized, flags=re.IGNORECASE)
            sanitized = re.sub(
                re.escape(f"{prefix}basic {encoded}"),
                "[redacted]",
                sanitized,
                flags=re.IGNORECASE,
            )
        except (ValueError, UnicodeDecodeError):
            sanitized = sanitized.replace(encoded, "[redacted]")
    elif header:
        sanitized = re.sub(re.escape(header), "[redacted]", sanitized, flags=re.IGNORECASE)
    return sanitized[:1000] or "Git snapshot operation failed."


def _analysis_service(
    store: SQLiteAnalysisStore, llm: bool, database: str
) -> ChangeAnalysisService:
    investigator = None
    if llm:
        server_environment = os.environ.copy()
        server_environment.pop("GITHUB_TOKEN", None)
        investigator = BoundedInvestigationAgent(
            OllamaLLMAdapter(),
            MCPToolPort(
                evidence_database=database,
                server_environment=server_environment,
            ),
        )
    return ChangeAnalysisService(store, investigator=investigator)


def _result_from_dict(value: dict[str, object], *, cached: bool) -> PRAnalysisResult:
    error = value.get("error")
    if isinstance(error, dict):
        error = GitHubError(**error)
    return PRAnalysisResult(
        status=value["status"],  # type: ignore[arg-type]
        repository=str(value["repository"]),
        pull_number=int(value["pull_number"]),
        base_sha=value.get("base_sha"),
        head_sha=value.get("head_sha"),
        current_head_sha=value.get("current_head_sha"),
        changed_files=tuple(value["changed_files"]),
        analysis=value.get("analysis"),
        error=error if isinstance(error, GitHubError) else None,
        cached=cached,
        head_refreshed=bool(value.get("head_refreshed", False)),
        rate_limit=RateLimit(**value.get("rate_limit", {})),
    )
