from __future__ import annotations

import shutil
import subprocess

import pytest

from changeguard.analysis import ChangeAnalysisService
from changeguard.storage import SQLiteAnalysisStore
from changeguard.verification.executor import SafeVerificationExecutor
from changeguard.verification.planner import VerificationPlanner
from tests.conftest import git


def _bwrap_works() -> bool:
    if not shutil.which("bwrap") or not shutil.which("node"):
        return False
    probe = subprocess.run(
        ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "true"], capture_output=True, check=False
    )
    return probe.returncode == 0


def js_repo(tmp_path, *, install=True, test_body="if (add(1, 2) !== 3) process.exit(1)"):
    repo = tmp_path / "repo"
    (repo / "web" / "src").mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    (repo / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    (repo / "web" / "package.json").write_text(
        '{"name": "web", "scripts": {"typecheck": "node check.js", "test": "node test.js"}}', encoding="utf-8"
    )
    (repo / "web" / "check.js").write_text(
        "require('fs').writeFileSync('.probe', 'ok'); console.log('typecheck ok')\n", encoding="utf-8"
    )
    (repo / "web" / "test.js").write_text(
        f"const helper = require('helper'); const {{ add }} = require('./src/math.js');\n{test_body}\nconsole.log('ok', helper)\n",
        encoding="utf-8",
    )
    (repo / "web" / "src" / "math.js").write_text("exports.add = (a, b) => a + b\n", encoding="utf-8")
    if install:
        (repo / "web" / "node_modules" / "helper").mkdir(parents=True)
        (repo / "web" / "node_modules" / "helper" / "index.js").write_text("module.exports = 'helper'\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "web" / "src" / "math.js").write_text("exports.add = (a, b) => a + b // v2\n", encoding="utf-8")
    git(repo, "commit", "-am", "change")
    return repo, base, git(repo, "rev-parse", "HEAD")


def plan_for(repo, base, head, tmp_path):
    analysis = ChangeAnalysisService(SQLiteAnalysisStore(str(tmp_path / "db.sqlite3"))).analyze(
        repo_path=str(repo), base=base, head=head
    )
    return VerificationPlanner().plan(analysis)


def test_planner_adds_typecheck_and_test_for_the_changed_package(tmp_path):
    plan = plan_for(*js_repo(tmp_path), tmp_path)
    assert plan.status == "READY"
    assert [(c.check_id, c.operation, c.targets) for c in plan.checks] == [
        ("node-typecheck-web", "NODE_TYPECHECK", ("web/package.json",)),
        ("node-test-web", "NODE_TEST", ("web/package.json",)),
    ]
    assert plan.timeout_seconds == 300


def test_planner_explains_missing_dependencies(tmp_path):
    plan = plan_for(*js_repo(tmp_path, install=False), tmp_path)
    assert plan.status == "UNSUPPORTED_PROJECT"
    assert any("dependencies are not installed" in item and "npm install" in item for item in plan.limitations)


def test_node_sandbox_command_layers_dependencies_over_a_writable_copy(tmp_path):
    if not shutil.which("node") or not shutil.which("npm"):
        pytest.skip("node/npm not installed")
    repo, base, head = js_repo(tmp_path)
    plan = plan_for(repo, base, head, tmp_path)
    executor = SafeVerificationExecutor(sandbox_executable="/usr/bin/bwrap")
    workspace = tmp_path / "workspace"
    (workspace / "web").mkdir(parents=True)
    (workspace / "web" / "package.json").write_text((repo / "web" / "package.json").read_text(), encoding="utf-8")

    spec = executor._check_spec(plan.checks[1], workspace, str(repo))
    argv = executor._sandbox_argv(workspace, spec.argv, spec)

    assert spec.argv[-2:] == ["test", "--silent"]
    assert spec.limit_memory is False
    assert argv[argv.index(str(workspace)) - 1] == "--bind"
    modules = str(repo / "web" / "node_modules")
    assert argv[argv.index(modules) - 1] == "--ro-bind"
    assert argv[argv.index(modules) + 1] == "/workspace/web/node_modules"
    assert argv.index(modules) > argv.index(str(workspace))  # layered on top of the workspace
    assert argv[argv.index("--chdir") + 1] == "/workspace/web"
    assert "--unshare-all" in argv
    assert ["--setenv", "CI", "true"] == argv[argv.index("CI") - 1: argv.index("CI") + 2]


def test_node_checks_must_target_one_package_json(tmp_path):
    from dataclasses import replace

    plan = plan_for(*js_repo(tmp_path), tmp_path)
    bad = replace(plan, checks=(replace(plan.checks[0], targets=("web/../package.json",)),))
    result = SafeVerificationExecutor(sandbox_executable="/usr/bin/bwrap").execute(bad)
    assert result.status == "BLOCKED"
    assert "package.json target" in result.limitations[-1] or "package.json target" in result.stderr_summary


@pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap sandbox is not usable here")
def test_node_checks_run_in_the_sandbox(tmp_path):
    repo, base, head = js_repo(tmp_path)
    result = SafeVerificationExecutor().execute(plan_for(repo, base, head, tmp_path))
    assert result.status == "PASSED", result.stderr_summary
    assert not (repo / "web" / ".probe").exists()  # writes stay in the disposable copy

    failing_repo, base, head = js_repo(tmp_path / "failing", test_body="process.exit(1)")
    failed = SafeVerificationExecutor().execute(plan_for(failing_repo, base, head, tmp_path / "failing"))
    assert failed.status == "FAILED"
