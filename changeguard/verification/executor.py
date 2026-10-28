from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Protocol

from changeguard.tools.git_tools import GitToolError, _git, _repository
from changeguard.verification.models import (
    VerificationCheck,
    VerificationCheckResult,
    VerificationEvidence,
    VerificationPlan,
    VerificationResult,
    VerificationStatus,
)

MAX_PLAN_CHECKS = 5
MAX_TIMEOUT_SECONDS = 300
MAX_OUTPUT_BYTES = 64 * 1024
OUTPUT_TAIL_BYTES = 8 * 1024
MAX_WORKSPACE_BYTES = 250 * 1024 * 1024
MAX_WORKSPACE_FILES = 10_000
MAX_TARGETS_PER_CHECK = 100
MAX_TARGET_PATH_LENGTH = 240
MAX_MEMORY_BYTES = 2 * 1024 * 1024 * 1024


class VerificationExecutorPort(Protocol):
    def execute(self, plan: VerificationPlan) -> VerificationResult: ...


class VerificationError(RuntimeError):
    pass


NODE_OPERATIONS = {"NODE_TYPECHECK", "NODE_TEST"}


# Upper limit for the sandbox's in-memory /tmp; memory is only used for files actually written.
# Large enough for apps whose health checks require some free disk space.
SANDBOX_TMP_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class _CheckSpec:
    argv: list[str]
    chdir: str = "/workspace"
    ro_mounts: tuple[str, ...] = ()
    binds: tuple[tuple[str, str], ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    writable_workspace: bool = False
    limit_memory: bool = True


class SafeVerificationExecutor:
    """Execute a narrow Python verification allowlist in bubblewrap, never via a shell."""

    def __init__(
        self,
        *,
        sandbox_executable: str | None = None,
        python_executable: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.sandbox_executable = sandbox_executable or shutil.which("bwrap")
        self.python_executable = python_executable or sys.executable
        self._now = now or (lambda: datetime.now(timezone.utc))

    def execute(self, plan: VerificationPlan) -> VerificationResult:
        started = time.monotonic()
        validation_error = self._validate_plan(plan)
        if validation_error:
            return self._blocked(plan, validation_error, started)
        if plan.status != "READY" or not plan.checks:
            return self._blocked(
                plan,
                "; ".join(plan.limitations) or "Verification plan has no runnable checks.",
                started,
            )
        if sys.platform != "linux" or not self.sandbox_executable:
            return self._blocked(
                plan,
                "BLOCKED: Linux bubblewrap (bwrap) is required; no unsandboxed fallback is permitted.",
                started,
            )

        try:
            revision = self._resolve_revision(plan.repository, plan.working_revision)
        except (GitToolError, OSError, ValueError) as error:
            return self._blocked(plan, f"Could not resolve verification revision: {error}", started)

        check_results: list[VerificationCheckResult] = []
        aggregate_stdout: list[str] = []
        aggregate_stderr: list[str] = []
        with tempfile.TemporaryDirectory(prefix="changeguard-verify-") as temp_root:
            workspace = Path(temp_root) / "workspace"
            workspace.mkdir()
            try:
                self._materialize_revision(
                    plan.repository, revision, workspace, plan.workspace_max_bytes
                )
            except (GitToolError, OSError, ValueError, tarfile.TarError) as error:
                return self._blocked(
                    plan, f"Could not create bounded revision snapshot: {error}", started
                )
            for check in plan.checks:
                for target in check.targets:
                    path = workspace.joinpath(*PurePosixPath(target).parts)
                    if not path.is_file() or path.is_symlink():
                        return self._blocked(
                            plan,
                            f"Verification target is not a regular file in the pinned revision: {target}",
                            started,
                        )
            for check in plan.checks:
                check_started_at = self._now().isoformat()
                check_started = time.monotonic()
                spec = self._check_spec(check, workspace, plan.repository)
                try:
                    if isinstance(spec, str):
                        status, stdout, stderr, return_code = "BLOCKED", b"", spec.encode(), None
                    else:
                        command = self._sandbox_argv(workspace, spec.argv, spec)
                        status, stdout, stderr, return_code = self._run_bounded(
                            command,
                            timeout_seconds=plan.timeout_seconds,
                            max_output_bytes=plan.max_output_bytes,
                            limit_memory=spec.limit_memory,
                        )
                except (OSError, subprocess.SubprocessError) as error:
                    status, stdout, stderr, return_code = (
                        "ERROR",
                        b"",
                        f"Sandbox execution failed: {error}".encode(),
                        None,
                    )
                duration = time.monotonic() - check_started
                stdout_summary = self._summarize(stdout, plan.max_output_bytes)
                stderr_summary = self._summarize(stderr, plan.max_output_bytes) if stderr else ""
                output_summary = "\n".join(
                    section
                    for section in (stdout_summary, f"stderr:\n{stderr_summary}" if stderr_summary else "")
                    if section
                )[:plan.max_output_bytes]
                failure = None
                if status == "PASSED" and return_code != 0:
                    status = "FAILED"
                if (
                    status == "FAILED"
                    and stderr.startswith(b"bwrap:")
                ):
                    status = "BLOCKED"
                if status in {"FAILED", "TIMED_OUT", "ERROR", "BLOCKED"}:
                    failure = self._failure_summary(status, output_summary, return_code)
                evidence = VerificationEvidence(
                    check_id=check.check_id,
                    check_type=check.operation,
                    repository=plan.repository,
                    revision=revision,
                    status=status,
                    started_at=check_started_at,
                    duration_seconds=round(duration, 3),
                    output_summary=output_summary,
                    failure_summary=failure,
                )
                check_results.append(
                    VerificationCheckResult(
                        check,
                        status,
                        round(duration, 3),
                        output_summary,
                        failure,
                        evidence,
                        return_code,
                    )
                )
                aggregate_stdout.append(f"[{check.check_id} {status}]\n{stdout_summary}")
                if stderr_summary:
                    aggregate_stderr.append(f"[{check.check_id}]\n{stderr_summary}")
                if status not in {"PASSED"}:
                    break

        statuses = [item.status for item in check_results]
        overall: VerificationStatus
        if not statuses:
            overall = "BLOCKED"
        elif "TIMED_OUT" in statuses:
            overall = "TIMED_OUT"
        elif "ERROR" in statuses:
            overall = "ERROR"
        elif "BLOCKED" in statuses:
            overall = "BLOCKED"
        elif "FAILED" in statuses:
            overall = "FAILED"
        elif len(check_results) < len(plan.checks):
            overall = statuses[-1]
        else:
            overall = "PASSED"
        failures = tuple(
            item.failure_summary
            for item in check_results
            if item.failure_summary is not None
        )
        return VerificationResult(
            status=overall,
            checks=tuple(check_results),
            duration_seconds=round(time.monotonic() - started, 3),
            stdout_summary="\n".join(aggregate_stdout)[:plan.max_output_bytes],
            stderr_summary="\n".join(aggregate_stderr)[:plan.max_output_bytes],
            failures=failures,
            evidence=tuple(item.evidence for item in check_results),
            limitations=tuple(plan.limitations) + self._setup_hints(check_results),
        )

    @staticmethod
    def _setup_hints(check_results) -> tuple[str, ...]:
        """Explain common environment problems so a failure isn't mistaken for a code bug."""
        hints = []
        for item in check_results:
            if item.check.operation == "PYTHON_PYTEST" and "No module named pytest" in item.output_summary:
                hints.append(
                    "pytest is not installed in the Python used for verification: install pytest in the "
                    "project's virtualenv (e.g. `.venv/bin/pip install pytest`)."
                )
        return tuple(dict.fromkeys(hints))

    def _validate_plan(self, plan: VerificationPlan) -> str | None:
        if not isinstance(plan, VerificationPlan):
            return "Verification input must be a validated VerificationPlan."
        if plan.status != "READY":
            return f"Plan status {plan.status} does not allow execution."
        if not 1 <= plan.max_checks <= MAX_PLAN_CHECKS:
            return f"Maximum checks must be between 1 and {MAX_PLAN_CHECKS}."
        if not 1 <= len(plan.checks) <= min(plan.max_checks, MAX_PLAN_CHECKS):
            return f"A plan must contain between 1 and {MAX_PLAN_CHECKS} checks."
        if type(plan.timeout_seconds) is not int or not 1 <= plan.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            return f"Timeout must be between 1 and {MAX_TIMEOUT_SECONDS} seconds."
        if type(plan.max_output_bytes) is not int or not 1 <= plan.max_output_bytes <= MAX_OUTPUT_BYTES:
            return f"Output limit must be between 1 and {MAX_OUTPUT_BYTES} bytes."
        if type(plan.workspace_max_bytes) is not int or not 1 <= plan.workspace_max_bytes <= MAX_WORKSPACE_BYTES:
            return f"Workspace limit must be between 1 and {MAX_WORKSPACE_BYTES} bytes."
        if not plan.analysis_id or not plan.repository or not self._valid_revision(
            plan.working_revision
        ):
            return "Plan must identify an analysis, repository, and full Git revision."
        allowed = {"PYTHON_COMPILE", "PYTHON_PYTEST", "NODE_TYPECHECK", "NODE_TEST"}
        seen: set[str] = set()
        for check in plan.checks:
            if check.operation not in allowed:
                return f"Unknown verification operation: {check.operation}"
            if check.check_id in seen or not check.check_id:
                return "Check IDs must be non-empty and unique."
            seen.add(check.check_id)
            if not check.targets or len(check.targets) > MAX_TARGETS_PER_CHECK:
                return f"Check {check.check_id} has an invalid number of targets."
            if check.operation in NODE_OPERATIONS:
                target = check.targets[0]
                if (
                    len(check.targets) != 1
                    or not isinstance(target, str)
                    or PurePosixPath(target).name != "package.json"
                    or target.startswith("-")
                    or "\\" in target
                    or PurePosixPath(target).is_absolute()
                    or ".." in PurePosixPath(target).parts
                ):
                    return f"Node checks need exactly one package.json target: {check.targets!r}"
                continue
            for target in check.targets:
                if (
                    not isinstance(target, str)
                    or len(target) > MAX_TARGET_PATH_LENGTH
                    or target.startswith("-")
                    or "\\" in target
                    or PurePosixPath(target).is_absolute()
                    or ".." in PurePosixPath(target).parts
                    or not target.endswith(".py")
                ):
                    return f"Invalid verification target path: {target!r}"
                if check.operation == "PYTHON_PYTEST":
                    basename = PurePosixPath(target).name
                    if not (
                        basename.startswith("test_") or basename.endswith("_test.py")
                    ):
                        return f"Pytest target is not a discovered test file: {target!r}"
        return None

    def _resolve_revision(self, repository: str, revision: str) -> str:
        root = _repository(repository)
        resolved = _git(root, "rev-parse", "--verify", f"{revision}^{{commit}}").strip()
        if resolved != revision:
            raise ValueError("Plan revision must be a full resolved commit hash.")
        return resolved

    def _materialize_revision(
        self, repository: str, revision: str, workspace: Path, max_bytes: int
    ) -> None:
        root = _repository(repository)
        process = subprocess.Popen(
            ["git", "archive", "--format=tar", revision],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None:
            process.kill()
            raise VerificationError("Could not read Git archive stream.")
        extracted_bytes = 0
        extracted_files = 0
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                for item in archive:
                    pure_path = PurePosixPath(item.name)
                    if (
                        pure_path.is_absolute()
                        or not pure_path.parts
                        or ".." in pure_path.parts
                    ):
                        raise VerificationError("Git archive contains an unsafe path.")
                    target = workspace.joinpath(*pure_path.parts)
                    if not target.resolve().is_relative_to(workspace.resolve()):
                        raise VerificationError("Git archive path escaped the isolated workspace.")
                    if item.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if not item.isfile():
                        raise VerificationError(
                            f"Unsupported non-regular archive entry: {item.name}"
                        )
                    extracted_files += 1
                    extracted_bytes += item.size
                    if extracted_files > MAX_WORKSPACE_FILES or extracted_bytes > max_bytes:
                        raise VerificationError("Revision exceeds the verification workspace bounds.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(item)
                    if source is None:
                        raise VerificationError(f"Could not read archive file {item.name}.")
                    with source, target.open("xb") as destination:
                        shutil.copyfileobj(source, destination, length=64 * 1024)
                    target.chmod(0o700 if item.mode & 0o111 else 0o600)
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
        stderr = process.stderr.read() if process.stderr is not None else b""
        return_code = process.wait(timeout=5)
        if return_code:
            raise GitToolError(
                stderr.decode("utf-8", errors="replace").strip()
                or "Git archive failed."
            )

    def _argv(self, check: VerificationCheck) -> list[str]:
        if check.operation == "PYTHON_COMPILE":
            return [self.python_executable, "-m", "py_compile", *check.targets]
        if check.operation == "PYTHON_PYTEST":
            return [
                self.python_executable,
                "-m",
                "pytest",
                # Not -q: combined with a project's own `addopts = -q` it hides the pass/fail totals.
                "--verbosity=0",
                "--no-header",
                "--tb=short",
                "-o",
                "cache_dir=/tmp/changeguard-pytest-cache",
                *check.targets,
            ]
        raise ValueError(f"Unknown verification operation: {check.operation}")

    def _check_spec(self, check: VerificationCheck, workspace: Path, repository: str) -> "_CheckSpec | str":
        """Sandbox settings for one check, or a reason the check cannot run."""
        if check.operation not in NODE_OPERATIONS:
            return self._python_spec(check, workspace, repository)
        package_dir = PurePosixPath(check.targets[0]).parent
        node = shutil.which("node")
        npm = shutil.which("npm")
        if not node or not npm:
            return "BLOCKED: Node.js and npm must be installed to verify JavaScript/TypeScript projects."
        node_prefix = Path(node).resolve().parent.parent
        npm_cli = Path(npm).resolve()
        repo_root = Path(repository).resolve()
        binds: list[tuple[str, str]] = []
        for directory in dict.fromkeys((package_dir, PurePosixPath("."))):
            host_modules = repo_root.joinpath(*directory.parts, "node_modules")
            if host_modules.is_dir():
                sandbox_modules = workspace.joinpath(*directory.parts, "node_modules")
                sandbox_modules.mkdir(parents=True, exist_ok=True)
                binds.append((str(host_modules), "/workspace/" + "/".join((*directory.parts, "node_modules"))))
        if not binds:
            return (
                f"BLOCKED: dependencies are not installed. Run `npm install` in "
                f"{repo_root.joinpath(*package_dir.parts)} and analyze that local checkout."
            )
        try:
            import json

            manifest = json.loads(workspace.joinpath(*package_dir.parts, "package.json").read_text(encoding="utf-8"))
            scripts = manifest.get("scripts") or {}
        except (OSError, ValueError, AttributeError):
            return "BLOCKED: package.json could not be read at the pinned revision."
        if check.operation == "NODE_TEST":
            argv = [node, str(npm_cli), "test", "--silent"]
        elif "typecheck" in scripts:
            argv = [node, str(npm_cli), "run", "typecheck", "--silent"]
        else:
            argv = [node, "node_modules/typescript/bin/tsc", "--noEmit", "--incremental", "false"]
        mounts = [str(node_prefix)] if not str(node_prefix).startswith(("/usr", "/lib")) else []
        if not str(npm_cli).startswith((*mounts, "/usr", "/lib")):
            mounts.append(str(npm_cli.parent.parent))
        return _CheckSpec(
            argv=argv,
            chdir="/workspace/" + "/".join(package_dir.parts) if package_dir.parts else "/workspace",
            ro_mounts=tuple(mounts),
            binds=tuple(binds),
            writable_workspace=True,
            limit_memory=False,
            env=(
                ("PATH", f"{node_prefix}/bin:/usr/bin:/bin"),
                ("CI", "true"),
                ("NODE_OPTIONS", "--max-old-space-size=2048"),
                ("npm_config_cache", "/tmp/npm-cache"),
                ("npm_config_update_notifier", "false"),
                ("npm_config_fund", "false"),
            ),
        )

    def _python_spec(self, check: VerificationCheck, workspace: Path, repository: str) -> "_CheckSpec":
        """Run Python checks with the project's own virtualenv when it has one.

        The venv is mounted read-only. The workspace copy goes first on PYTHONPATH, so the
        analyzed revision is tested even when the venv has an editable install of the checkout.
        """
        argv = self._argv(check)
        venv_python = project_python(repository)
        if venv_python is None:
            return _CheckSpec(argv)
        argv[0] = str(venv_python)
        mounts = []
        for prefix in (venv_python.parent.parent, venv_python.resolve().parent.parent):
            text = str(prefix)
            if not text.startswith(("/usr", "/lib")) and text not in mounts:
                mounts.append(text)
        python_path = ["/workspace"]
        if workspace.joinpath("src").is_dir():
            python_path.insert(0, "/workspace/src")
        return _CheckSpec(
            argv,
            ro_mounts=tuple(mounts),
            env=(
                ("PATH", f"{venv_python.parent}:/usr/bin:/bin"),
                ("PYTHONPATH", ":".join(python_path)),
                ("VIRTUAL_ENV", str(venv_python.parent.parent)),
            ),
        )

    def _sandbox_argv(self, workspace: Path, command: list[str], spec: "_CheckSpec | None" = None) -> list[str]:
        if not self.sandbox_executable:
            raise VerificationError("Bubblewrap executable is unavailable.")
        spec = spec or _CheckSpec(command)
        argv = [
            self.sandbox_executable,
            "--unshare-all",
            "--die-with-parent",
            "--ro-bind",
            "/usr",
            "/usr",
        ]
        for path in ("/lib", "/lib64"):
            if Path(path).exists():
                argv.extend(["--ro-bind", path, path])
        argv.extend(["--size", str(SANDBOX_TMP_BYTES), "--tmpfs", "/tmp"])
        python_prefix = Path(self.python_executable).parent.parent
        if python_prefix.exists() and not str(python_prefix).startswith(("/usr/", "/lib/")):
            mount_path = str(python_prefix)
            argv.extend(["--dir", mount_path, "--ro-bind", mount_path, mount_path])
        for mount_path in spec.ro_mounts:
            argv.extend(["--dir", mount_path, "--ro-bind", mount_path, mount_path])
        argv.extend([
            "--proc", "/proc", "--dev", "/dev",
            "--bind" if spec.writable_workspace else "--ro-bind", str(workspace), "/workspace",
        ])
        # Dependency folders are layered read-only over the disposable workspace copy.
        for source, destination in spec.binds:
            argv.extend(["--ro-bind", source, destination])
        environment = dict((("PATH", "/usr/bin:/bin"),) + spec.env)
        for name, value in environment.items():
            argv.extend(["--setenv", name, value])
        argv.extend(
            [
                "--chdir",
                spec.chdir,
                "--setenv",
                "HOME",
                "/tmp",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "PYTHONDONTWRITEBYTECODE",
                "1",
                "--setenv",
                "PYTHONPYCACHEPREFIX",
                "/tmp/pycache",
                "--setenv",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
                "1",
                "--",
                *command,
            ]
        )
        return argv

    def _run_bounded(
        self,
        command: list[str],
        *,
        timeout_seconds: int,
        max_output_bytes: int,
        limit_memory: bool = True,
    ) -> tuple[VerificationStatus, bytes, bytes, int | None]:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={},
            cwd="/",
            start_new_session=True,
            bufsize=0,
            preexec_fn=self._resource_limits(timeout_seconds, limit_memory),
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            return "ERROR", b"", b"Verification process did not expose output.", None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        tails = {"stdout": bytearray(), "stderr": bytearray()}
        captured = 0
        exceeded = False
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                events = selector.select(min(remaining, 0.1))
                for key, _ in events:
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = max_output_bytes - captured
                    if available > 0:
                        outputs[key.data].extend(chunk[:available])
                        captured += min(len(chunk), available)
                    if len(chunk) > available:
                        exceeded = True
                        # Keep the latest output too: test runners print their totals last.
                        tails[key.data].extend(chunk[max(available, 0):])
                        del tails[key.data][:-OUTPUT_TAIL_BYTES]
            if timed_out:
                os.killpg(process.pid, signal.SIGKILL)
            try:
                return_code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                return_code = process.wait(timeout=2)
        finally:
            selector.close()
            process.stdout.close()
        if exceeded:
            name = "stdout" if outputs["stdout"] else "stderr"
            selected, tail = outputs[name], bytes(tails[name])
            marker = b"\n[output truncated at configured limit]\n"
            keep_tail = tail[-max(0, min(len(tail), max_output_bytes // 4)):] if tail else b""
            del selected[max(0, max_output_bytes - len(marker) - len(keep_tail)) :]
            selected.extend((marker + keep_tail)[:max_output_bytes])
        if timed_out:
            return "TIMED_OUT", bytes(outputs["stdout"]), bytes(outputs["stderr"]), return_code
        return (
            "PASSED" if return_code == 0 else "FAILED",
            bytes(outputs["stdout"]),
            bytes(outputs["stderr"]),
            return_code,
        )

    def _resource_limits(self, timeout_seconds: int, limit_memory: bool = True):
        def apply_limits() -> None:
            import resource

            cpu_seconds = max(1, timeout_seconds)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (MAX_WORKSPACE_BYTES, MAX_WORKSPACE_BYTES),
            )
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if limit_memory:
                # Node reserves far more virtual memory than it uses; its heap is capped
                # with --max-old-space-size instead.
                resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))

        return apply_limits

    def _summarize(self, output: bytes, maximum: int) -> str:
        """The output, or its start and end when too long (test runners print totals last)."""
        if len(output) > maximum:
            tail = min(4096, maximum // 4)
            marker = b"\n...[output truncated]...\n"
            output = output[: max(0, maximum - tail - len(marker))] + marker + output[-tail:]
        return output.decode("utf-8", errors="replace").strip() or "(no output)"

    def _failure_summary(
        self, status: VerificationStatus, output: str, return_code: int | None
    ) -> str:
        if status == "TIMED_OUT":
            return f"Check exceeded its configured timeout. Output: {output[:1000]}"
        if status == "FAILED":
            return f"Check exited with status {return_code}. Output: {output[:1000]}"
        return f"Check status {status}. Output: {output[:1000]}"

    def _blocked(
        self, plan: VerificationPlan, reason: str, started: float
    ) -> VerificationResult:
        limitations = getattr(plan, "limitations", ())
        return VerificationResult(
            status="BLOCKED",
            checks=(),
            duration_seconds=round(time.monotonic() - started, 3),
            stdout_summary="",
            stderr_summary="",
            failures=(reason,),
            evidence=(),
            limitations=tuple((*limitations, reason)),
        )

    def _valid_revision(self, revision: str) -> bool:
        return len(revision) in {40, 64} and all(
            character in "0123456789abcdef" for character in revision
        )


VENV_NAMES = (".venv", "venv", "env")


def project_python(repository: str) -> Path | None:
    """The Python of the project's virtualenv (``.venv``, ``venv`` or ``env``), if any.

    For a git worktree without its own venv, the main checkout's venv is used.
    """
    roots = [Path(repository).resolve()]
    try:
        common = subprocess.run(
            ["git", "-C", repository, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        common = ""
    if common:
        main_root = Path(common).parent
        if main_root not in roots:
            roots.append(main_root)
    for root in roots:
        for name in VENV_NAMES:
            python = root / name / "bin" / "python"
            if (root / name / "pyvenv.cfg").is_file() and python.exists():
                return python
    return None
