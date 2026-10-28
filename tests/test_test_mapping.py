from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

import changeguard.analysis  # noqa: F401  (import order: analysis before planner)
from changeguard.analysis import ChangeAnalysisService
from changeguard.storage.sqlite import SQLiteAnalysisStore
from changeguard.verification.executor import SafeVerificationExecutor, project_python
from changeguard.verification.models import (
    VerificationCheck,
    VerificationCheckResult,
    VerificationEvidence,
    VerificationResult,
)
from changeguard.verification.planner import (
    VerificationPlanner,
    _imported_modules,
    _module_names,
    _route_pattern,
)
from tests.conftest import git


def test_route_pattern_matches_literal_and_fstring_urls_only_for_that_route():
    pattern = _route_pattern("/gmail/drafts/{draft_id}/{action}")
    assert pattern.search('client.post("/gmail/drafts/5/send", data=form)')
    assert pattern.search("client.post(f'/gmail/drafts/{draft.id}/create')")
    assert pattern.search('client.post("/gmail/drafts/5/send?next=1")')
    assert not pattern.search('client.get("/gmail/drafts/5")')
    assert not pattern.search('client.post("/gmail/drafts/5/send/extra")')
    assert _route_pattern("/users/:id").search("request('/users/42')")


def test_imports_and_src_layout_module_names():
    assert _imported_modules("from app.services import drafts as ds\nimport app.web\nfrom . import local") == {
        "app.services", "app.services.drafts", "app.web",
    }
    assert _imported_modules("not python (") == set()
    names = _module_names(("src/app/services/drafts.py", "src/app/__init__.py", "README.md"))
    assert names["app.services.drafts"] == "src/app/services/drafts.py"
    assert names["app"] == "src/app/__init__.py"


@pytest.fixture
def mapped_repo(tmp_path):
    repo = tmp_path / "repo"
    files = {
        "pyproject.toml": "[tool.pytest.ini_options]\naddopts = \"-q\"\n",
        "src/shop/__init__.py": "",
        "src/shop/billing.py": "def total(items):\n    return sum(items)\n",
        "src/shop/web.py": (
            "from fastapi import FastAPI\napp = FastAPI()\n\n"
            "@app.post('/orders/{order_id}/pay')\ndef pay(order_id: int):\n    return {'ok': True}\n"
        ),
        "tests/test_milestone_1.py": "from shop import billing\n\ndef test_total():\n    assert billing.total([1, 2]) == 3\n",
        "tests/test_milestone_2.py": "def test_pay(client):\n    client.post('/orders/7/pay')\n",
        "tests/test_unrelated.py": "def test_nothing():\n    assert True\n",
    }
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(tmp_path, "init", "-b", "main", str(repo))
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "src/shop/billing.py").write_text("def total(items):\n    return sum(items) + 0\n", encoding="utf-8")
    (repo / "src/shop/web.py").write_text(files["src/shop/web.py"].replace("True", "1"), encoding="utf-8")
    git(repo, "commit", "-am", "change billing and pay")
    return repo, base, git(repo, "rev-parse", "HEAD")


def test_planner_selects_tests_by_import_and_endpoint_url(tmp_path, mapped_repo):
    repo, base, head = mapped_repo
    result = ChangeAnalysisService(SQLiteAnalysisStore(str(tmp_path / "db.sqlite3"))).analyze(
        repo_path=str(repo), base=base, head=head
    )
    plan = VerificationPlanner().plan(result)

    pytest_check = next(check for check in plan.checks if check.operation == "PYTHON_PYTEST")
    assert pytest_check.targets == ("tests/test_milestone_1.py", "tests/test_milestone_2.py")
    assert plan.working_revision == head


def test_project_python_prefers_own_venv_then_main_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-b", "main")
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "T")
    (main / "a.txt").write_text("a", encoding="utf-8")
    git(main, "add", ".")
    git(main, "commit", "-m", "initial")
    assert project_python(str(main)) is None

    venv = main / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (venv / "bin" / "python").symlink_to(sys.executable)
    worktree = tmp_path / "worktree"
    git(main, "worktree", "add", "-q", str(worktree), "-b", "other")

    assert project_python(str(main)) == venv / "bin" / "python"
    assert project_python(str(worktree)) == venv / "bin" / "python"


def test_pytest_uses_explicit_verbosity_so_project_quiet_option_keeps_totals():
    argv = SafeVerificationExecutor(sandbox_executable="/bin/false")._argv(
        VerificationCheck("t", "PYTHON_PYTEST", ("tests/test_a.py",), "r")
    )
    assert "--verbosity=0" in argv and "-q" not in argv


def test_capped_output_keeps_the_final_totals():
    executor = SafeVerificationExecutor(sandbox_executable="/bin/false")
    script = "import sys\nsys.stdout.write('x' * 200000)\nprint('\\n1 failed, 45 passed in 2.0s')\n"
    _status, stdout, _stderr, _code = executor._run_bounded(
        [sys.executable, "-c", script], timeout_seconds=30, max_output_bytes=8192, limit_memory=False,
    )
    text = stdout.decode()
    assert len(stdout) <= 8192
    assert "[output truncated at configured limit]" in text
    assert text.rstrip().endswith("1 failed, 45 passed in 2.0s")


def _failed_result(output: str) -> VerificationResult:
    check = VerificationCheck("pytest-targeted", "PYTHON_PYTEST", ("tests/test_a.py",), "r")
    evidence = VerificationEvidence("pytest-targeted", "PYTHON_PYTEST", "/repo", "a" * 40, "FAILED", "now", 1.0, output, "f")
    return VerificationResult(
        "FAILED", (VerificationCheckResult(check, "FAILED", 1.0, output, "f", evidence, 1),),
        1.0, output, "", ("f",), (evidence,), (),
    )


def test_failed_checks_become_a_review_trigger_naming_the_tests():
    output = (
        "F.\n=== FAILURES ===\n"
        "___________ test_send_requires_confirmation ___________\nE assert 'Tick' in url\n"
        "_ test_routes_never_500[POST /a/1] _\nE assert 500 == 404\n"
    )
    trigger = ChangeAnalysisService._failed_tests_trigger(_failed_result(output))
    assert trigger.kind == "tests_failed" and trigger.severity == "HIGH"
    assert "`test_send_requires_confirmation`" in trigger.reason
    assert "`test_routes_never_500`" in trigger.reason


def test_no_trigger_when_checks_pass():
    result = _failed_result("ok")
    passed = VerificationResult("PASSED", (), 1.0, "", "", (), (), ())
    assert ChangeAnalysisService._failed_tests_trigger(passed) is None
    assert ChangeAnalysisService._failed_tests_trigger(result) is not None


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap not installed")
def test_sandboxed_pytest_runs_with_the_project_venv(tmp_path, mapped_repo):
    repo, base, head = mapped_repo
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(repo / ".venv")], check=True)
    service = ChangeAnalysisService(SQLiteAnalysisStore(str(tmp_path / "db.sqlite3")))
    result = service.verify(service.analyze(repo_path=str(repo), base=base, head=head))
    pytest_result = next(c for c in result.verification_result.checks if c.check.operation == "PYTHON_PYTEST")
    if pytest_result.status == "BLOCKED":
        pytest.skip("sandbox unavailable here")
    # The project's venv was used; it has no pytest, which the report explains.
    assert ".venv/bin/python: No module named pytest" in pytest_result.output_summary
    assert any("install pytest in the" in item for item in result.verification_result.limitations)
