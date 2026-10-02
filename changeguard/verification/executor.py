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
MAX_WORKSPACE_BYTES = 250 * 1024 * 1024
MAX_WORKSPACE_FILES = 10_000
MAX_TARGETS_PER_CHECK = 100
MAX_TARGET_PATH_LENGTH = 240
MAX_MEMORY_BYTES = 2 * 1024 * 1024 * 1024


class VerificationExecutorPort(Protocol):
    def execute(self, plan: VerificationPlan) -> VerificationResult: ...


class VerificationError(RuntimeError):
    pass


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
                argv = self._argv(check)
                command = self._sandbox_argv(workspace, argv)
                try:
                    status, stdout, stderr, return_code = self._run_bounded(
                        command,
                        timeout_seconds=plan.timeout_seconds,
                        max_output_bytes=plan.max_output_bytes,
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
            limitations=tuple(plan.limitations),
        )

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
        allowed = {"PYTHON_COMPILE", "PYTHON_PYTEST"}
        seen: set[str] = set()
        for check in plan.checks:
            if check.operation not in allowed:
                return f"Unknown verification operation: {check.operation}"
            if check.check_id in seen or not check.check_id:
                return "Check IDs must be non-empty and unique."
            seen.add(check.check_id)
            if not check.targets or len(check.targets) > MAX_TARGETS_PER_CHECK:
                return f"Check {check.check_id} has an invalid number of targets."
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
                "-q",
                "--no-header",
                "--tb=short",
                "-o",
                "cache_dir=/tmp/changeguard-pytest-cache",
                *check.targets,
            ]
        raise ValueError(f"Unknown verification operation: {check.operation}")

    def _sandbox_argv(self, workspace: Path, command: list[str]) -> list[str]:
        if not self.sandbox_executable:
            raise VerificationError("Bubblewrap executable is unavailable.")
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
        argv.extend(["--size", str(256 * 1024 * 1024), "--tmpfs", "/tmp"])
        python_prefix = Path(self.python_executable).parent.parent
        if python_prefix.exists() and not str(python_prefix).startswith(("/usr/", "/lib/")):
            mount_path = str(python_prefix)
            argv.extend(["--dir", mount_path, "--ro-bind", mount_path, mount_path])
        argv.extend(
            [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--ro-bind",
                str(workspace),
                "/workspace",
                "--chdir",
                "/workspace",
                "--setenv",
                "HOME",
                "/tmp",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "PATH",
                "/usr/bin:/bin",
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
    ) -> tuple[VerificationStatus, bytes, bytes, int | None]:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={},
            cwd="/",
            start_new_session=True,
            bufsize=0,
            preexec_fn=self._resource_limits(timeout_seconds),
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            return "ERROR", b"", b"Verification process did not expose output.", None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
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
            suffix = b"\n[output truncated at configured limit]"
            selected = outputs["stdout"] if outputs["stdout"] else outputs["stderr"]
            del selected[max(0, max_output_bytes - len(suffix)) :]
            selected.extend(suffix[:max_output_bytes])
        if timed_out:
            return "TIMED_OUT", bytes(outputs["stdout"]), bytes(outputs["stderr"]), return_code
        return (
            "PASSED" if return_code == 0 else "FAILED",
            bytes(outputs["stdout"]),
            bytes(outputs["stderr"]),
            return_code,
        )

    def _resource_limits(self, timeout_seconds: int):
        def apply_limits() -> None:
            import resource

            cpu_seconds = max(1, timeout_seconds)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (MAX_WORKSPACE_BYTES, MAX_WORKSPACE_BYTES),
            )
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))

        return apply_limits

    def _summarize(self, output: bytes, maximum: int) -> str:
        return output[:maximum].decode("utf-8", errors="replace").strip() or "(no output)"

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
