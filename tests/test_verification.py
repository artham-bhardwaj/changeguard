from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import replace

import pytest

from changeguard.analysis import ChangeAnalysisService, ChangeGuardAnalysis
from changeguard.storage.sqlite import SQLiteAnalysisStore
from changeguard.verification.executor import SafeVerificationExecutor
from changeguard.verification.models import VerificationCheck, VerificationPlan
from changeguard.verification.planner import VerificationPlanner


def git(repo, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def python_repo(tmp_path):
    repo = tmp_path / "python-project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_module.py").write_text(
        "from pathlib import Path\nfrom module import VALUE\n\n"
        "def test_revision_contents():\n"
        "    assert VALUE == 2\n"
        "    assert Path('version.txt').read_text().strip() == 'new'\n\n"
        "def test_workspace_is_read_only():\n"
        "    try:\n"
        "        Path('module.py').write_text('VALUE = 999\\n')\n"
        "    except OSError:\n"
        "        return\n"
        "    raise AssertionError('sandbox workspace was writable')\n",
        encoding="utf-8",
    )
    (repo / "version.txt").write_text("old\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline Python project")
    base = git(repo, "rev-parse", "HEAD")

    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "version.txt").write_text("new\n", encoding="utf-8")
    (repo / "tests" / "test_module.py").write_text(
        "from pathlib import Path\nfrom module import VALUE\n\n"
        "def test_revision_contents():\n"
        "    assert VALUE == 2\n"
        "    assert Path('version.txt').read_text().strip() == 'new'\n\n"
        "def test_workspace_is_read_only():\n"
        "    try:\n"
        "        Path('module.py').write_text('VALUE = 999\\n')\n"
        "    except OSError:\n"
        "        return\n"
        "    raise AssertionError('sandbox workspace was writable')\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change module and its test")
    head = git(repo, "rev-parse", "HEAD")
    return repo, base, head


def analyze(repo, base, head, database):
    store = SQLiteAnalysisStore(str(database))
    result = ChangeAnalysisService(store).analyze(
        repo_path=str(repo), base=base, head=head
    )
    return result, store


def plan_for(result, **changes) -> VerificationPlan:
    plan = VerificationPlanner().plan(result)
    assert plan.status == "READY"
    return replace(plan, **changes)


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is not installed")
def test_planner_runs_only_mapped_pytest_and_compile_in_revision_pinned_sandbox(
    tmp_path,
):
    repo, base, head = python_repo(tmp_path)
    result, store = analyze(repo, base, head, tmp_path / "analysis.sqlite3")
    plan = plan_for(result)
    before_status = git(repo, "status", "--porcelain")
    before_source = (repo / "module.py").read_text(encoding="utf-8")
    (repo / "version.txt").write_text("dirty working tree\n", encoding="utf-8")
    dirty_status = git(repo, "status", "--porcelain")

    verified = ChangeAnalysisService(store).verify(result)

    assert [check.operation for check in plan.checks] == [
        "PYTHON_PYTEST",
        "PYTHON_COMPILE",
    ]
    assert plan.checks[0].targets == ("tests/test_module.py",)
    assert verified.verification_result is not None
    assert (
        verified.verification_result.status == "PASSED"
    ), (
        verified.verification_result.stdout_summary,
        verified.verification_result.stderr_summary,
        verified.verification_result.failures,
    )
    assert all(item.status == "PASSED" for item in verified.verification_result.checks)
    assert all(item.evidence.revision == head for item in verified.verification_result.checks)
    assert verified.test_signals.state == "known"
    assert verified.test_signals.tests_passed == 2
    assert verified.initial_risk_assessment is not None
    assert verified.initial_risk_assessment.factors[3].state == "unknown"
    assert verified.risk_assessment.factors[3].state == "known"
    assert git(repo, "status", "--porcelain") == dirty_status
    assert (repo / "module.py").read_text(encoding="utf-8") == before_source
    assert before_status == ""

    stored = store.get_change_analysis(result.analysis_id)
    restored = ChangeGuardAnalysis.from_dict(stored or {})
    assert restored.verification_result is not None
    assert restored.verification_result.status == "PASSED"
    assert restored.initial_risk_assessment == verified.initial_risk_assessment
    assert restored.risk_assessment.score == verified.risk_assessment.score


def test_planner_marks_unmapped_tests_unknown_and_compiles_changed_source(tmp_path):
    repo, base, head = python_repo(tmp_path)
    (repo / "other.py").write_text("OTHER = True\n", encoding="utf-8")
    git(repo, "add", "other.py")
    git(repo, "commit", "-m", "add unrelated Python module")
    head = git(repo, "rev-parse", "HEAD")
    (repo / "other.py").write_text("OTHER = False\n", encoding="utf-8")
    git(repo, "add", "other.py")
    git(repo, "commit", "-m", "change unmapped module")
    changed_head = git(repo, "rev-parse", "HEAD")
    result, _ = analyze(repo, head, changed_head, tmp_path / "analysis.sqlite3")

    plan = VerificationPlanner().plan(result)

    assert plan.status == "READY"
    assert [check.operation for check in plan.checks] == ["PYTHON_COMPILE"]
    assert plan.checks[0].targets == ("other.py",)
    assert any("UNKNOWN" in item for item in plan.limitations)


def test_planner_returns_blocked_plan_when_revision_cannot_be_inspected(
    tmp_path, monkeypatch
):
    from changeguard.tools.git_tools import GitToolError

    repo, base, head = python_repo(tmp_path)
    result, _ = analyze(repo, base, head, tmp_path / "analysis.sqlite3")

    def unavailable(*args, **kwargs):
        raise GitToolError("unavailable")

    monkeypatch.setattr(
        "changeguard.verification.planner.get_revision_files",
        unavailable,
    )

    plan = VerificationPlanner().plan(result)

    assert plan.status == "UNSUPPORTED_PROJECT"
    assert plan.working_revision == head
    assert "Could not inspect revision files" in plan.limitations[0]


def test_java_only_change_is_reported_as_unsupported_project(git_repo, tmp_path):
    repo, base, head = git_repo
    result, _ = analyze(repo, base, head, tmp_path / "analysis.sqlite3")

    plan = VerificationPlanner().plan(result)

    assert plan.status == "UNSUPPORTED_PROJECT"
    blocked = SafeVerificationExecutor().execute(plan)
    assert blocked.status == "BLOCKED"
    assert any("Python projects only" in item for item in blocked.limitations)


@pytest.mark.parametrize(
    ("check", "reason"),
    [
        (
            VerificationCheck("bad", "SHELL", ("test_module.py",), "arbitrary"),
            "Unknown verification operation",
        ),
        (
            VerificationCheck(
                "bad", "PYTHON_PYTEST", ("../outside/test_bad.py",), "traversal"
            ),
            "Invalid verification target path",
        ),
        (
            VerificationCheck(
                "bad", "PYTHON_PYTEST", ("module.py",), "non-test target"
            ),
            "not a discovered test file",
        ),
    ],
)
def test_invalid_operations_and_arguments_are_blocked_without_execution(
    git_repo, check, reason
):
    repo, _, head = git_repo
    plan = VerificationPlan(
        "plan",
        "analysis",
        str(repo),
        head,
        (check,),
        (),
        10,
        1024,
        3,
        1024 * 1024,
    )

    result = SafeVerificationExecutor().execute(plan)

    assert result.status == "BLOCKED"
    assert reason in result.limitations[-1]
    assert result.checks == ()


def test_check_count_timeout_and_output_limits_are_validated(git_repo):
    repo, _, head = git_repo
    executor = SafeVerificationExecutor()
    check = VerificationCheck(
        "test", "PYTHON_COMPILE", ("app.txt",), "invalid for test fixture"
    )
    plan = VerificationPlan(
        "plan",
        "analysis",
        str(repo),
        head,
        tuple(replace(check, check_id=f"check-{index}") for index in range(6)),
        (),
        10,
        1024,
        3,
        1024 * 1024,
    )

    result = executor.execute(plan)

    assert result.status == "BLOCKED"
    assert "between 1 and 5 checks" in result.limitations[-1]

    invalid_timeout = replace(plan, checks=(check,), timeout_seconds=301)
    assert executor.execute(invalid_timeout).status == "BLOCKED"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is not installed")
def test_timeout_is_reported_and_output_is_capped(tmp_path):
    repo, _, head = python_repo(tmp_path)
    (repo / "tests" / "test_module.py").write_text(
        "import time\n\ndef test_timeout():\n    time.sleep(10)\n",
        encoding="utf-8",
    )
    git(repo, "add", "tests/test_module.py")
    git(repo, "commit", "-m", "add timeout test")
    head = git(repo, "rev-parse", "HEAD")
    timeout_plan = VerificationPlan(
        "timeout-plan",
        "analysis",
        str(repo),
        head,
        (
            VerificationCheck(
                "timeout",
                "PYTHON_PYTEST",
                ("tests/test_module.py",),
                "exercise timeout",
            ),
        ),
        (),
        1,
        512,
        1,
        1024 * 1024,
    )
    timeout_result = SafeVerificationExecutor().execute(timeout_plan)
    assert timeout_result.status == "TIMED_OUT"
    assert timeout_result.evidence[0].status == "TIMED_OUT"

    (repo / "tests" / "test_module.py").write_text(
        "def test_large_output():\n    print('x' * 5000)\n    assert False\n",
        encoding="utf-8",
    )
    git(repo, "add", "tests/test_module.py")
    git(repo, "commit", "-m", "add output limit test")
    output_head = git(repo, "rev-parse", "HEAD")
    output_plan = replace(
        timeout_plan,
        plan_id="output-plan",
        working_revision=output_head,
        timeout_seconds=10,
        max_output_bytes=512,
    )
    output_result = SafeVerificationExecutor().execute(output_plan)
    assert output_result.status == "FAILED"
    assert len(output_result.checks[0].output_summary.encode()) <= 512
    assert len(output_result.stdout_summary.encode()) <= 512


@pytest.mark.parametrize(
    ("platform", "bwrap"),
    [("linux", None), ("darwin", "/usr/bin/bwrap")],
)
def test_non_linux_or_missing_bwrap_never_falls_back_to_host_execution(
    tmp_path, monkeypatch, platform, bwrap
):
    repo, _, head = python_repo(tmp_path)
    monkeypatch.setattr(
        "changeguard.verification.executor.shutil.which", lambda _: bwrap
    )
    monkeypatch.setattr("changeguard.verification.executor.sys.platform", platform)
    plan = VerificationPlan(
        "plan",
        "analysis",
        str(repo),
        head,
        (VerificationCheck("compile", "PYTHON_COMPILE", ("app.py",), "test"),),
        (),
        10,
        1024,
        1,
        1024 * 1024,
    )

    result = SafeVerificationExecutor(sandbox_executable=None).execute(plan)

    assert result.status == "BLOCKED"
    assert "no unsandboxed fallback" in result.limitations[-1]


def test_verified_risk_is_recalculated_by_risk_engine_and_report_stays_truthful(
    tmp_path,
):
    from changeguard.llm.decision import InvestigationState
    from changeguard.llm.report import InvestigationReport
    from changeguard.verification.models import (
        VerificationCheckResult,
        VerificationEvidence,
        VerificationResult,
    )

    class PassingExecutor:
        def execute(self, plan):
            evidence = VerificationEvidence(
                "pytest-targeted",
                "PYTHON_PYTEST",
                plan.repository,
                plan.working_revision,
                "PASSED",
                "2026-10-01T00:00:00+00:00",
                0.1,
                "2 passed",
            )
            check = plan.checks[0]
            result = VerificationCheckResult(
                check, "PASSED", 0.1, "2 passed", None, evidence, 0
            )
            return VerificationResult(
                "PASSED", (result,), 0.1, "2 passed", "", (), (evidence,), ()
            )

    class MockInvestigator:
        def investigate_and_report(self, task, **kwargs):
            report = InvestigationReport(
                "Insufficient evidence.",
                [],
                [],
                [],
                ["No investigation tool result."],
                [],
                [],
            )
            return type(
                "Generated",
                (),
                {"report": report, "investigation": InvestigationState(task), "error": None},
            )()

    repo, base, head = python_repo(tmp_path)
    result, store = analyze(repo, base, head, tmp_path / "analysis.sqlite3")
    report = InvestigationReport(
        "Insufficient evidence.",
        [],
        [],
        [],
        ["No investigation tool result."],
        [],
        [],
    )
    result = replace(
        result,
        investigation_report=report,
    )
    verified = ChangeAnalysisService(
        store,
        investigator=MockInvestigator(),  # type: ignore[arg-type]
        verification_executor=PassingExecutor(),  # type: ignore[arg-type]
    ).verify(result)

    assert verified.verification_result is not None
    assert verified.verification_result.status == "PASSED"
    assert verified.test_signals.tests_passed == 2
    assert verified.investigation_report is not None
    assert verified.investigation_report.verification["status"] == "PASSED"
    assert verified.investigation_report.verification["initial_risk"][
        "score"
    ] == result.risk_assessment.score
    assert verified.investigation_report.verification["verified_risk"][
        "score"
    ] == verified.risk_assessment.score
