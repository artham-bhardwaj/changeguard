from __future__ import annotations

import json
import math
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from changeguard.github.models import (
    ChangedFile,
    GitHostingError,
    GitHubError,
    PullRequestRef,
    PullRequestSnapshot,
    RateLimit,
)


class GitHubAPIAdapter:
    """Small, read-only adapter for the fixed GitHub pull-request API endpoints."""

    def __init__(
        self,
        token: str | None = None,
        *,
        timeout_seconds: float = 10,
        max_retries: int = 2,
        opener: Any = urlopen,
        sleep: Any = time.sleep,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("GitHub timeout must be positive.")
        if type(max_retries) is not int or not 0 <= max_retries <= 2:
            raise ValueError("GitHub retries must be between zero and two.")
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._opener = opener
        self._sleep = sleep

    def get_pull_request(
        self, reference: PullRequestRef
    ) -> tuple[PullRequestSnapshot, RateLimit]:
        payload, headers = self._request(
            f"/repos/{reference.owner}/{reference.repository}/pulls/{reference.number}"
        )
        try:
            base = payload["base"]
            head = payload["head"]
            repo = PullRequestSnapshot(
                reference=reference,
                url=_bounded_text(payload["html_url"], "pull request URL"),
                title=_bounded_text(payload["title"], "pull request title"),
                state=_bounded_text(payload["state"], "pull request state"),
                base_ref=_bounded_text(base["ref"], "base branch"),
                base_sha=_bounded_text(base["sha"], "base SHA"),
                head_ref=_bounded_text(head["ref"], "head branch"),
                head_sha=_bounded_text(head["sha"], "head SHA"),
                changed_files_count=_nonnegative_integer(payload["changed_files"]),
                rate_limit=_rate_limit(headers),
                head_repository=(
                    _bounded_text(head["repo"]["full_name"], "head repository")
                    if isinstance(head.get("repo"), dict)
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GitHostingError(
                GitHubError("MALFORMED_RESPONSE", "GitHub returned malformed pull request metadata.")
            ) from error
        return repo, repo.rate_limit

    def list_pull_request_files(
        self, reference: PullRequestRef
    ) -> tuple[tuple[ChangedFile, ...], RateLimit]:
        results: list[ChangedFile] = []
        rate = RateLimit()
        for page in range(1, 11):
            payload, headers = self._request(
                f"/repos/{reference.owner}/{reference.repository}/pulls/{reference.number}/files"
                f"?per_page=100&page={page}"
            )
            rate = _rate_limit(headers)
            if not isinstance(payload, list):
                raise GitHostingError(
                    GitHubError("MALFORMED_RESPONSE", "GitHub returned malformed pull request files.")
                )
            try:
                results.extend(
                    ChangedFile(
                        path=_bounded_text(item["filename"], "changed file path"),
                        status=_bounded_text(item["status"], "changed file status"),
                        additions=_nonnegative_integer(item["additions"]),
                        deletions=_nonnegative_integer(item["deletions"]),
                    )
                    for item in payload
                )
            except (KeyError, TypeError, ValueError) as error:
                raise GitHostingError(
                    GitHubError("MALFORMED_RESPONSE", "GitHub returned malformed changed-file metadata.")
                ) from error
            if len(payload) < 100 or len(results) >= 1000:
                break
        return tuple(results[:1000]), rate

    def _request(self, path: str) -> tuple[Any, dict[str, str]]:
        request = Request(
            "https://api.github.com" + path,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ChangeGuard",
                **({"Authorization": f"Bearer {self._token}"} if self._token else {}),
            },
        )
        for attempt in range(self._max_retries + 1):
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    try:
                        body = response.read(5 * 1024 * 1024 + 1)
                        if len(body) > 5 * 1024 * 1024:
                            raise GitHostingError(
                                GitHubError("RESPONSE_TOO_LARGE", "GitHub API response exceeded the size limit.")
                            )
                        return json.loads(body), headers
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise GitHostingError(
                            GitHubError("MALFORMED_RESPONSE", "GitHub returned invalid JSON.")
                        ) from error
            except HTTPError as error:
                rate = _rate_limit({key.lower(): value for key, value in error.headers.items()})
                status = error.code
                if status in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                    delay = min(rate.retry_after_seconds or 0, 1)
                    self._sleep(delay)
                    continue
                codes = {
                    401: ("AUTHENTICATION_FAILED", "GitHub authentication failed."),
                    403: ("ACCESS_DENIED", "GitHub denied access or the API rate limit was reached."),
                    404: ("NOT_FOUND", "The pull request or repository was not found."),
                    429: ("RATE_LIMITED", "GitHub API rate limit was reached."),
                }
                code, message = codes.get(status, ("HTTP_ERROR", f"GitHub API request failed with HTTP {status}."))
                if status == 403 and (
                    rate.remaining == 0 or rate.retry_after_seconds is not None
                ):
                    code, message = "RATE_LIMITED", "GitHub API rate limit was reached."
                raise GitHostingError(GitHubError(code, message, status, rate)) from None
            except (URLError, TimeoutError, OSError) as error:
                if attempt < self._max_retries:
                    self._sleep(min(0.1 * (2**attempt), 0.5))
                    continue
                is_timeout = isinstance(error, TimeoutError) or (
                    isinstance(error, URLError) and isinstance(error.reason, TimeoutError)
                )
                code, message = (
                    ("TIMEOUT", "GitHub API request timed out.")
                    if is_timeout
                    else ("UNAVAILABLE", "GitHub API is unavailable.")
                )
                raise GitHostingError(GitHubError(code, message)) from None
        raise AssertionError("Retry loop must return or raise.")


def _bounded_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError(f"Invalid {label}")
    return value


def _nonnegative_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Expected a nonnegative integer")
    return value


def _rate_limit(headers: dict[str, str]) -> RateLimit:
    def integer(name: str) -> int | None:
        try:
            return int(headers[name]) if name in headers else None
        except ValueError:
            return None

    return RateLimit(
        limit=integer("x-ratelimit-limit"),
        remaining=integer("x-ratelimit-remaining"),
        reset_epoch=integer("x-ratelimit-reset"),
        retry_after_seconds=integer("retry-after"),
    )
