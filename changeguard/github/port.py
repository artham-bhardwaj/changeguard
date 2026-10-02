from __future__ import annotations

from typing import Protocol

from changeguard.github.models import (
    ChangedFile,
    PullRequestRef,
    PullRequestSnapshot,
    RateLimit,
)


class GitHostingPort(Protocol):
    def get_pull_request(
        self, reference: PullRequestRef
    ) -> tuple[PullRequestSnapshot, RateLimit]: ...

    def list_pull_request_files(
        self, reference: PullRequestRef
    ) -> tuple[tuple[ChangedFile, ...], RateLimit]: ...
