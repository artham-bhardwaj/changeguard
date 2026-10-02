from __future__ import annotations

import base64
import json
import os
import subprocess
from email.message import Message
from urllib.error import HTTPError

import pytest

from changeguard.github.models import (
    ChangedFile,
    GitHostingError,
    GitHubError,
    PullRequestRef,
    PullRequestSnapshot,
    RateLimit,
)
from changeguard.github.provider import GitHubAPIAdapter
from changeguard.github.service import PullRequestAnalysisService
from changeguard.storage.sqlite import SQLiteAnalysisStore


BASE = "a" * 40
HEAD = "b" * 40


class FakeProvider:
    def __init__(self, heads: tuple[str, ...] = (HEAD,)) -> None:
        self.heads = heads
        self.get_count = 0
        self.files_count = 0

    def get_pull_request(self, reference: PullRequestRef):
        head = self.heads[min(self.get_count, len(self.heads) - 1)]
        self.get_count += 1
        return (
            PullRequestSnapshot(
                reference,
                "https://github.com/acme/widget/pull/17",
                "A change",
                "open",
                "main",
                BASE,
                "feature",
                head,
                1,
                RateLimit(limit=5000, remaining=4998),
                "acme/widget",
            ),
            RateLimit(limit=5000, remaining=4998),
        )

    def list_pull_request_files(self, reference: PullRequestRef):
        self.files_count += 1
        return (
            (ChangedFile("src/app.py", "modified", 2, 1),),
            RateLimit(limit=5000, remaining=4997),
        )


class FakeAnalysis:
    def __init__(self) -> None:
        self.verify_calls = 0
        self.analyze_arguments = None

    def analyze(self, **kwargs):
        self.analyze_arguments = kwargs
        return FakeAnalysisResult()

    def verify(self, result):
        self.verify_calls += 1
        value = result.to_dict()
        value["verification_result"] = {"status": "PASSED"}
        return FakeAnalysisResult(value)


class FakeAnalysisResult:
    def __init__(self, value=None) -> None:
        self.value = value or {"change_summary": "One source file changed."}

    def to_dict(self):
        return self.value


def _service(provider, store, fake_analysis, monkeypatch):
    monkeypatch.setattr(
        "changeguard.github.service._initialize_snapshot",
        lambda *args: None,
    )
    return PullRequestAnalysisService(
        provider,
        store,
        analysis_factory=lambda *_args: fake_analysis,
    )


def test_pr_reference_validation_and_canonical_parsing():
    assert PullRequestRef.parse("acme/widget#17") == PullRequestRef("acme", "widget", 17)
    for invalid in ("https://github.com/acme/widget/pull/17", "../widget#17", "acme/widget#0"):
        with pytest.raises(ValueError):
            PullRequestRef.parse(invalid)
    with pytest.raises(ValueError):
        PullRequestRef("..", "widget", 17)


def test_provider_parses_pr_metadata_and_rate_limits():
    payload = {
        "html_url": "https://github.com/acme/widget/pull/17",
        "title": "A change",
        "state": "open",
        "changed_files": 1,
        "base": {"ref": "main", "sha": BASE},
        "head": {
            "ref": "feature",
            "sha": HEAD,
            "repo": {"full_name": "acme/widget"},
        },
    }

    class Response:
        headers = {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4999"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(payload).encode()

    adapter = GitHubAPIAdapter(opener=lambda *_args, **_kwargs: Response())
    pr, rate = adapter.get_pull_request(PullRequestRef("acme", "widget", 17))

    assert pr.head_sha == HEAD
    assert rate.remaining == 4999


def test_provider_uses_token_header_and_rejects_malformed_json(monkeypatch):
    requests = []
    token_parts = ("secret-", "value")
    token = "".join(token_parts)

    class CapturedRequest:
        def __init__(self, url, *, headers):
            self.full_url = url
            self.headers = headers

    monkeypatch.setattr("changeguard.github.provider.Request", CapturedRequest)

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"{"

    def opener(request, **_kwargs):
        requests.append(request)
        return Response()

    adapter = GitHubAPIAdapter(token, opener=opener)
    with pytest.raises(GitHostingError, match="invalid JSON") as raised:
        adapter.get_pull_request(PullRequestRef("acme", "widget", 17))
    assert requests[0].headers["Authorization"] == "Bearer " + token
    assert token not in str(raised.value)


def test_provider_reports_auth_and_rate_limit_errors_without_response_body():
    headers = Message()
    headers["X-RateLimit-Remaining"] = "0"
    headers["Retry-After"] = "30"

    def opener(*_args, **_kwargs):
        raise HTTPError("https://api.github.com/", 403, "forbidden", headers, None)

    adapter = GitHubAPIAdapter(opener=opener, max_retries=0)
    with pytest.raises(GitHostingError) as raised:
        adapter.get_pull_request(PullRequestRef("acme", "widget", 17))
    assert raised.value.error.code == "RATE_LIMITED"
    assert raised.value.error.rate_limit.remaining == 0
    assert raised.value.error.rate_limit.retry_after_seconds == 30


def test_provider_retries_timeout_with_bounded_attempts():
    attempts = []
    sleeps = []

    def opener(*_args, **_kwargs):
        attempts.append(1)
        raise TimeoutError("network timeout")

    adapter = GitHubAPIAdapter(opener=opener, sleep=sleeps.append)
    with pytest.raises(GitHostingError) as raised:
        adapter.get_pull_request(PullRequestRef("acme", "widget", 17))
    assert raised.value.error.code == "TIMEOUT"
    assert len(attempts) == 3
    assert sleeps == [0.1, 0.2]


def test_service_uses_existing_analysis_flow_and_marks_changed_head_stale(
    tmp_path, monkeypatch
):
    provider = FakeProvider((HEAD, "c" * 40))
    analysis = FakeAnalysis()
    service = _service(
        provider, SQLiteAnalysisStore(str(tmp_path / "state.sqlite")), analysis, monkeypatch
    )

    result = service.analyze(PullRequestRef("acme", "widget", 17), verify=True)

    assert result.status == "STALE"
    assert result.head_sha == HEAD
    assert result.current_head_sha == "c" * 40
    assert result.changed_files == ("src/app.py",)
    assert analysis.analyze_arguments["base"] == BASE
    assert analysis.analyze_arguments["head"] == HEAD
    assert analysis.verify_calls == 1


def test_mocked_github_workflow_evaluation_is_repeatable_and_offline():
    from evaluation.runner import EvaluationRunner

    results = EvaluationRunner().run_github_workflows()

    assert results["network_access"] is False
    assert results["scenario_count"] == 4
    assert results["passed"] == 4
    assert results["failed"] == 0


def test_pr_cache_is_exact_revision_and_only_skips_matching_completed_analysis(
    tmp_path, monkeypatch
):
    provider = FakeProvider()
    analysis = FakeAnalysis()
    store = SQLiteAnalysisStore(str(tmp_path / "state.sqlite"))
    service = _service(provider, store, analysis, monkeypatch)
    reference = PullRequestRef("acme", "widget", 17)

    first = service.analyze(reference, persist=True, refresh_head=False)
    second = service.analyze(reference, persist=True, refresh_head=False)

    assert first.status == "COMPLETED"
    assert second.cached is True
    assert second.head_refreshed is False
    assert second.current_head_sha is None
    assert provider.files_count == 1
    assert store.get_pr_analysis("acme/widget", 17, HEAD)["head_sha"] == HEAD


def test_verified_or_llm_pr_results_are_not_reused_as_deterministic_cache(
    tmp_path, monkeypatch
):
    provider = FakeProvider()
    analysis = FakeAnalysis()
    store = SQLiteAnalysisStore(str(tmp_path / "state.sqlite"))
    service = _service(provider, store, analysis, monkeypatch)

    service.analyze(PullRequestRef("acme", "widget", 17), verify=True, persist=True)

    assert store.get_pr_analysis("acme/widget", 17, HEAD) is None


def test_stale_analysis_is_not_cached(tmp_path, monkeypatch):
    provider = FakeProvider((HEAD, "c" * 40))
    analysis = FakeAnalysis()
    store = SQLiteAnalysisStore(str(tmp_path / "state.sqlite"))
    result = _service(provider, store, analysis, monkeypatch).analyze(
        PullRequestRef("acme", "widget", 17), persist=True
    )

    assert result.status == "STALE"
    assert store.get_pr_analysis("acme/widget", 17, HEAD) is None


def test_private_git_snapshot_credentials_are_not_command_arguments_or_git_config(
    tmp_path, monkeypatch
):
    calls = []
    revisions = iter((BASE, HEAD))

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        output = ""
        if arguments[:2] == ["git", "rev-parse"]:
            output = next(revisions) + "\n"
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr("changeguard.github.service.subprocess.run", run)
    from changeguard.github.service import _initialize_snapshot

    _initialize_snapshot(
        tmp_path, "acme/widget", "fork/widget", BASE, HEAD, "secret-value"
    )
    fetched_urls = []
    for arguments, kwargs in calls:
        assert "secret-value" not in " ".join(arguments)
        assert "GITHUB_TOKEN" not in kwargs["env"]
        assert kwargs["cwd"] == tmp_path
        if "fetch" in arguments:
            fetched_urls.append(arguments[arguments.index("fetch") + 5])
        if "GIT_CONFIG_VALUE_3" in kwargs["env"]:
            assert kwargs["env"]["GIT_CONFIG_VALUE_3"].startswith("AUTHORIZATION: basic ")
            assert "secret-value" not in kwargs["env"]["GIT_CONFIG_VALUE_3"]
        if "fetch" in arguments:
            assert "--depth=1" in arguments or "--depth=10" in arguments
    assert all("remote" not in arguments for arguments, _kwargs in calls)
    assert fetched_urls == [
        "https://github.com/acme/widget.git",
        "https://github.com/fork/widget.git",
    ]
    assert all(".git/config" not in str(path) for path in tmp_path.iterdir())


def test_analysis_errors_return_explicit_failure_without_leaking_token(tmp_path, monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(
        "changeguard.github.service._initialize_snapshot",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("failed secret-value")),
    )
    result = PullRequestAnalysisService(
        provider,
        SQLiteAnalysisStore(str(tmp_path / "state.sqlite")),
        token="secret-value",
    ).analyze(PullRequestRef("acme", "widget", 17))

    assert result.status == "FAILED"
    assert result.error.code == "ANALYSIS_FAILED"
    assert "secret-value" not in result.error.message


def test_llm_mcp_child_does_not_inherit_github_credentials(tmp_path, monkeypatch):
    from changeguard.github import service as github_service

    token = "private-" + "credential"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    captured = {}

    class FakeMCPPort:
        def __init__(self, **kwargs):
            assert kwargs["evidence_database"] == str(tmp_path / "analysis.sqlite")
            captured.update(kwargs["server_environment"])

    class FakeInvestigator:
        def __init__(self, *_args):
            pass

    monkeypatch.setattr(github_service, "MCPToolPort", FakeMCPPort)
    monkeypatch.setattr(github_service, "BoundedInvestigationAgent", FakeInvestigator)
    monkeypatch.setattr(github_service, "OllamaLLMAdapter", lambda: object())
    github_service._analysis_service(
        SQLiteAnalysisStore(str(tmp_path / "state.sqlite")),
        True,
        str(tmp_path / "analysis.sqlite"),
    )

    assert "GITHUB_TOKEN" not in captured
    assert os.environ["GITHUB_TOKEN"] == token


def test_result_round_trips_error_contract():
    error = GitHubError("NOT_FOUND", "Not found", 404, RateLimit(remaining=0))
    assert error.to_dict()["rate_limit"]["remaining"] == 0
