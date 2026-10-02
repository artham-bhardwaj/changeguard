from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal


_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repository: str
    number: int

    def __post_init__(self) -> None:
        if (
            not _REPOSITORY_PART.fullmatch(self.owner)
            or not _REPOSITORY_PART.fullmatch(self.repository)
        ):
            raise ValueError("Repository must contain a valid owner and repository name.")
        if type(self.number) is not int or not 1 <= self.number <= 2**31 - 1:
            raise ValueError("Pull request number must be a positive integer.")

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"

    @classmethod
    def parse(cls, value: str) -> PullRequestRef:
        match = re.fullmatch(
            r"([A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?)/"
            r"([A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?)#([1-9][0-9]*)",
            value,
        )
        if match is None:
            raise ValueError("Expected a pull request in OWNER/REPO#NUMBER format.")
        try:
            number = int(match.group(3))
        except ValueError as error:
            raise ValueError("Pull request number must be a positive integer.") from error
        return cls(match.group(1), match.group(2), number)


@dataclass(frozen=True)
class RateLimit:
    limit: int | None = None
    remaining: int | None = None
    reset_epoch: int | None = None
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    additions: int
    deletions: int


@dataclass(frozen=True)
class PullRequestSnapshot:
    reference: PullRequestRef
    url: str
    title: str
    state: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    changed_files_count: int
    rate_limit: RateLimit
    head_repository: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.base_sha, str)
            or not isinstance(self.head_sha, str)
            or not _SHA.fullmatch(self.base_sha)
            or not _SHA.fullmatch(self.head_sha)
        ):
            raise ValueError("GitHub returned an invalid immutable commit SHA.")
        if type(self.changed_files_count) is not int or self.changed_files_count < 0:
            raise ValueError("GitHub returned an invalid changed-file count.")
        if self.head_repository is not None:
            PullRequestRef.parse(f"{self.head_repository}#1")


@dataclass(frozen=True)
class GitHubError:
    code: str
    message: str
    http_status: int | None = None
    rate_limit: RateLimit = RateLimit()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GitHostingError(RuntimeError):
    def __init__(self, error: GitHubError) -> None:
        super().__init__(error.message)
        self.error = error


PRStatus = Literal["COMPLETED", "STALE", "FAILED"]


@dataclass(frozen=True)
class PRAnalysisResult:
    status: PRStatus
    repository: str
    pull_number: int
    base_sha: str | None
    head_sha: str | None
    current_head_sha: str | None
    changed_files: tuple[str, ...]
    analysis: dict[str, Any] | None
    error: GitHubError | None = None
    cached: bool = False
    head_refreshed: bool = False
    rate_limit: RateLimit = RateLimit()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["changed_files"] = list(self.changed_files)
        return value
