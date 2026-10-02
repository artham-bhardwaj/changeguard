from __future__ import annotations

import tomllib
from uuid import uuid4

from changeguard.analysis.models import ChangeGuardAnalysis
from changeguard.tools.git_tools import get_revision_file, get_revision_files
from changeguard.verification.models import VerificationCheck, VerificationPlan

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 300
MAX_OUTPUT_BYTES = 64 * 1024
MAX_CHECKS = 3
MAX_WORKSPACE_BYTES = 250 * 1024 * 1024
MAX_PLAN_FILES = 10_000


class VerificationPlanner:
    """Plan only repository-native Python compile and pytest operations."""

    def plan(self, analysis: ChangeGuardAnalysis) -> VerificationPlan:
        plan_id = str(uuid4())
        limitations: list[str] = []
        revision = analysis.change_reference.rsplit("..", 1)[-1]
        try:
            paths = get_revision_files(
                analysis.repository,
                revision,
                max_files=MAX_PLAN_FILES,
            )
        except (OSError, RuntimeError, ValueError) as error:
            return self._blocked_plan(analysis, plan_id, f"Could not inspect revision files: {error}")

        pyproject = None
        if "pyproject.toml" in paths:
            try:
                pyproject = get_revision_file(
                    analysis.repository,
                    revision,
                    "pyproject.toml",
                    max_bytes=512 * 1024,
                )
            except (OSError, RuntimeError, ValueError) as error:
                limitations.append(f"Could not read pyproject.toml: {error}")

        pytest_configured = False
        if pyproject is not None:
            try:
                config = tomllib.loads(pyproject)
                pytest_configured = isinstance(
                    config.get("tool", {}).get("pytest", {}).get("ini_options"), dict
                )
            except (tomllib.TOMLDecodeError, AttributeError):
                limitations.append("pyproject.toml has invalid TOML; pytest configuration was not inferred.")
        if "pytest.ini" in paths:
            pytest_configured = True

        test_paths = tuple(
            path
            for path in paths
            if path.endswith(".py")
            and (path.rsplit("/", 1)[-1].startswith("test_") or path.rsplit("/", 1)[-1].endswith("_test.py"))
        )
        changed_python = tuple(
            path for path in analysis.changed_files if path.endswith(".py") and path in paths
        )
        if not changed_python:
            return VerificationPlan(
                plan_id=plan_id,
                analysis_id=analysis.analysis_id,
                repository=analysis.repository,
                working_revision=revision,
                checks=(),
                constraints=self._constraints(),
                timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                max_output_bytes=MAX_OUTPUT_BYTES,
                max_checks=MAX_CHECKS,
                workspace_max_bytes=MAX_WORKSPACE_BYTES,
                status="UNSUPPORTED_PROJECT",
                limitations=(
                    "This verifier currently supports Python projects only; no changed tracked Python source is available at the analyzed revision.",
                ),
            )

        checks: list[VerificationCheck] = []
        selected_tests = self._related_tests(analysis, changed_python, paths, test_paths)
        if selected_tests and pytest_configured:
            risk_level = analysis.risk_assessment.overall_level
            rationale = "Run test files deterministically matched by changed or affected module names."
            if risk_level in {"HIGH", "CRITICAL"}:
                rationale += f" Prioritized because deterministic initial risk is {risk_level}."
            checks.append(
                VerificationCheck(
                    check_id="pytest-targeted",
                    operation="PYTHON_PYTEST",
                    targets=selected_tests,
                    rationale=rationale,
                )
            )
        elif test_paths and not pytest_configured:
            limitations.append("Pytest files exist but the project does not declare a supported pytest configuration.")
        else:
            limitations.append(
                "UNKNOWN: no test file could be confidently mapped to the changed Python modules; the full suite will not be run automatically."
            )

        checks.append(
            VerificationCheck(
                check_id="python-compile",
                operation="PYTHON_COMPILE",
                targets=changed_python,
                rationale="Compile changed tracked Python source without executing project code.",
            )
        )
        return VerificationPlan(
            plan_id=plan_id,
            analysis_id=analysis.analysis_id,
            repository=analysis.repository,
            working_revision=analysis.repository_status.head_commit,
            checks=tuple(checks[:MAX_CHECKS]),
            constraints=self._constraints(),
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            max_output_bytes=MAX_OUTPUT_BYTES,
            max_checks=MAX_CHECKS,
            workspace_max_bytes=MAX_WORKSPACE_BYTES,
            limitations=tuple(limitations),
        )

    def _related_tests(
        self,
        analysis: ChangeGuardAnalysis,
        changed_python: tuple[str, ...],
        tracked_paths: tuple[str, ...],
        test_paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        related_sources = set(changed_python)
        if analysis.blast_radius is not None:
            related_sources.update(
                path
                for path in analysis.blast_radius.affected_files
                if path.endswith(".py") and path in tracked_paths
            )
        changed_stems = set()
        for path in related_sources:
            stem = path.rsplit("/", 1)[-1].removesuffix(".py")
            if stem != "__init__":
                changed_stems.add(stem)
        matched = [
            path
            for path in test_paths
            if path in changed_python
            or path.rsplit("/", 1)[-1]
            .removeprefix("test_")
            .removesuffix("_test.py")
            .removesuffix(".py")
            in changed_stems
        ]
        return tuple(sorted(matched))

    def _blocked_plan(
        self, analysis: ChangeGuardAnalysis, plan_id: str, reason: str
    ) -> VerificationPlan:
        revision = analysis.change_reference.rsplit("..", 1)[-1]
        return VerificationPlan(
            plan_id,
            analysis.analysis_id,
            analysis.repository,
            revision,
            (),
            self._constraints(),
            DEFAULT_TIMEOUT_SECONDS,
            MAX_OUTPUT_BYTES,
            MAX_CHECKS,
            MAX_WORKSPACE_BYTES,
            "UNSUPPORTED_PROJECT",
            (reason,),
        )

    def _constraints(self) -> tuple[str, ...]:
        return (
            "Only deterministic Python pytest and py_compile operations are allowlisted.",
            "No shell, network, model-supplied command text, or repository write is permitted.",
            "Execution uses a disposable snapshot of the pinned Git revision.",
        )
