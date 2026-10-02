from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from changeguard.github.models import (
    ChangedFile,
    GitHostingError,
    GitHubError,
    PullRequestRef,
    PullRequestSnapshot,
    RateLimit,
)
from changeguard.github.service import PullRequestAnalysisService
from changeguard.storage.sqlite import SQLiteAnalysisStore


_BASE = "a" * 40
_HEAD = "b" * 40
_NEXT_HEAD = "c" * 40


@dataclass(frozen=True)
class GitHubWorkflowEvaluation:
    case_id: str
    expected_status: str
    observed_status: str
    passed: bool
    cache_hit: bool
    head_refreshed: bool
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _FakeProvider:
    def __init__(self, heads: tuple[str, ...], *, error: GitHubError | None = None) -> None:
        self.heads = heads
        self.error = error
        self.reads = 0

    def get_pull_request(self, reference: PullRequestRef):
        if self.error:
            raise GitHostingError(self.error)
        head = self.heads[min(self.reads, len(self.heads) - 1)]
        self.reads += 1
        return (
            PullRequestSnapshot(
                reference,
                "https://github.com/evaluation/changeguard/pull/1",
                "Evaluation change",
                "open",
                "main",
                _BASE,
                "feature",
                head,
                1,
                RateLimit(5000, 4999),
                "evaluation/changeguard",
            ),
            RateLimit(5000, 4999),
        )

    def list_pull_request_files(self, _reference: PullRequestRef):
        return ((ChangedFile("src/app.py", "modified", 1, 0),), RateLimit(5000, 4998))


class _FakeAnalysisResult:
    def to_dict(self) -> dict[str, Any]:
        return {"change_summary": "One authored evaluation file changed."}


class _FakeAnalysisService:
    def analyze(self, **_kwargs) -> _FakeAnalysisResult:
        return _FakeAnalysisResult()


def run_github_workflows() -> dict[str, Any]:
    """Evaluate read-only PR behavior with authored metadata and no network access."""
    scenarios = (
        ("github-stable-head", (_HEAD, _HEAD), None, False, "COMPLETED"),
        ("github-stale-head", (_HEAD, _NEXT_HEAD), None, False, "STALE"),
        ("github-auth-failure", (), GitHubError("AUTHENTICATION_FAILED", "GitHub authentication failed.", 401), False, "FAILED"),
    )
    results: list[GitHubWorkflowEvaluation] = []
    for case_id, heads, error, cache, expected in scenarios:
        with tempfile.TemporaryDirectory(prefix="changeguard-pr-eval-") as directory:
            store = SQLiteAnalysisStore(str(Path(directory) / "evaluation.sqlite"))
            provider = _FakeProvider(heads, error=error)
            service = PullRequestAnalysisService(
                provider,
                store,
                analysis_factory=lambda *_args: _FakeAnalysisService(),  # type: ignore[arg-type]
                snapshot_materializer=lambda *_args: None,
            )
            result = service.analyze(
                PullRequestRef("evaluation", "changeguard", 1), persist=cache
            )
            results.append(
                GitHubWorkflowEvaluation(
                    case_id,
                    expected,
                    result.status,
                    result.status == expected,
                    result.cached,
                    result.head_refreshed,
                    result.error.code if result.error else None,
                )
            )

    with tempfile.TemporaryDirectory(prefix="changeguard-pr-cache-eval-") as directory:
        store = SQLiteAnalysisStore(str(Path(directory) / "evaluation.sqlite"))
        provider = _FakeProvider((_HEAD,))
        service = PullRequestAnalysisService(
            provider,
            store,
            analysis_factory=lambda *_args: _FakeAnalysisService(),  # type: ignore[arg-type]
            snapshot_materializer=lambda *_args: None,
        )
        reference = PullRequestRef("evaluation", "changeguard", 1)
        service.analyze(reference, persist=True, refresh_head=False)
        cached = service.analyze(reference, persist=True, refresh_head=False)
        results.append(
            GitHubWorkflowEvaluation(
                "github-exact-revision-cache",
                "COMPLETED",
                cached.status,
                cached.status == "COMPLETED" and cached.cached,
                cached.cached,
                cached.head_refreshed,
            )
        )

    return {
        "scenario_count": len(results),
        "passed": sum(item.passed for item in results),
        "failed": sum(not item.passed for item in results),
        "results": [item.to_dict() for item in results],
        "network_access": False,
    }
