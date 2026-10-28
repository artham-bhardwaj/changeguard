from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path
from uuid import uuid4

from changeguard.analysis.models import ChangeGuardAnalysis
from changeguard.tools.git_tools import get_revision_file, get_revision_files
from changeguard.verification.models import VerificationCheck, VerificationPlan

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 300
MAX_OUTPUT_BYTES = 64 * 1024
MAX_CHECKS = 5
MAX_NODE_PACKAGES = 2
NODE_TIMEOUT_SECONDS = 300
MAX_SCANNED_TESTS = 500
MAX_SCANNED_TEST_BYTES = 256 * 1024
SCRIPT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
MAX_WORKSPACE_BYTES = 250 * 1024 * 1024
MAX_PLAN_FILES = 10_000


class VerificationPlanner:
    """Plan repository-native Python (pytest, py_compile) and JS/TS (typecheck, test) checks."""

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
        checks: list[VerificationCheck] = []
        if changed_python:
            selected_tests = self._related_tests(analysis, changed_python, paths, test_paths, revision)
            if selected_tests and pytest_configured:
                risk_level = analysis.risk_assessment.overall_level
                rationale = (
                    "Run test files matched to the changed code by module name, by import, "
                    "or by requesting a changed endpoint's URL."
                )
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
        node_checks = self._node_checks(analysis, revision, paths, limitations)
        checks.extend(node_checks)

        if not checks:
            if not changed_python and not self._changed_scripts(analysis, paths):
                limitations.insert(0,
                    "This verifier supports Python and JavaScript/TypeScript projects only; no changed tracked Python or JS/TS source is available at the analyzed revision."
                )
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
                limitations=tuple(limitations),
            )
        return VerificationPlan(
            plan_id=plan_id,
            analysis_id=analysis.analysis_id,
            repository=analysis.repository,
            working_revision=revision,
            checks=tuple(checks[:MAX_CHECKS]),
            constraints=self._constraints(),
            timeout_seconds=NODE_TIMEOUT_SECONDS if node_checks else DEFAULT_TIMEOUT_SECONDS,
            max_output_bytes=MAX_OUTPUT_BYTES,
            max_checks=MAX_CHECKS,
            workspace_max_bytes=MAX_WORKSPACE_BYTES,
            limitations=tuple(limitations),
        )

    @staticmethod
    def _changed_scripts(analysis: ChangeGuardAnalysis, paths: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            path for path in analysis.changed_files
            if path.endswith(SCRIPT_SUFFIXES) and not path.endswith(".d.ts") and path in paths
        )

    def _node_checks(
        self,
        analysis: ChangeGuardAnalysis,
        revision: str,
        paths: tuple[str, ...],
        limitations: list[str],
    ) -> list[VerificationCheck]:
        """Typecheck and test each package (nearest package.json) containing changed JS/TS files."""
        manifests = {path.rsplit("/", 1)[0] if "/" in path else "" for path in paths if path.rsplit("/", 1)[-1] == "package.json"}
        packages: dict[str, None] = {}
        for path in self._changed_scripts(analysis, paths):
            directory = path.rsplit("/", 1)[0] if "/" in path else ""
            while True:
                if directory in manifests:
                    packages[directory] = None
                    break
                if not directory:
                    break
                directory = directory.rsplit("/", 1)[0] if "/" in directory else ""
        checks: list[VerificationCheck] = []
        repo_root = Path(analysis.repository)
        for package in list(packages)[:MAX_NODE_PACKAGES]:
            manifest_path = f"{package}/package.json" if package else "package.json"
            label = package or "repository root"
            try:
                manifest = json.loads(get_revision_file(analysis.repository, revision, manifest_path, max_bytes=512 * 1024))
            except (OSError, RuntimeError, ValueError) as error:
                limitations.append(f"Could not read {manifest_path}: {error}")
                continue
            if not isinstance(manifest, dict):
                continue
            scripts = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
            dependencies = {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}
            installed = (repo_root / package / "node_modules").is_dir() or (repo_root / "node_modules").is_dir()
            if not installed:
                limitations.append(
                    f"JS/TS checks for {label} were skipped: dependencies are not installed. "
                    f"Run `npm install` in {repo_root / package} and analyze that local checkout with `changeguard impact --verify`."
                )
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", package.lower()).strip("-") or "root"
            tsconfig = (f"{package}/tsconfig.json" if package else "tsconfig.json") in paths
            if "typecheck" in scripts or (tsconfig and "typescript" in dependencies):
                checks.append(VerificationCheck(
                    check_id=f"node-typecheck-{slug}",
                    operation="NODE_TYPECHECK",
                    targets=(manifest_path,),
                    rationale=(
                        f"Type-check {label} with its `typecheck` script."
                        if "typecheck" in scripts else f"Type-check {label} with `tsc --noEmit`."
                    ),
                ))
            test_script = scripts.get("test")
            if isinstance(test_script, str) and "no test specified" not in test_script:
                checks.append(VerificationCheck(
                    check_id=f"node-test-{slug}",
                    operation="NODE_TEST",
                    targets=(manifest_path,),
                    rationale=f"Run the `test` script of {label} (`{test_script[:80]}`) with CI=true.",
                ))
            elif "typecheck" not in scripts:
                limitations.append(f"{label} has no `test` script; JS/TS tests were not run.")
        if checks:
            limitations.append(
                "JS/TS checks use the dependencies currently installed in the local checkout, "
                "which may differ from those declared at the analyzed revision."
            )
        return checks

    def _related_tests(
        self,
        analysis: ChangeGuardAnalysis,
        changed_python: tuple[str, ...],
        tracked_paths: tuple[str, ...],
        test_paths: tuple[str, ...],
        revision: str | None = None,
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
        matched = {
            path
            for path in test_paths
            if path in changed_python
            or path.rsplit("/", 1)[-1]
            .removeprefix("test_")
            .removesuffix("_test.py")
            .removesuffix(".py")
            in changed_stems
        }
        if revision is not None:
            matched.update(
                self._tests_by_content(analysis, related_sources, tracked_paths, test_paths, revision)
            )
        return tuple(sorted(matched))

    def _tests_by_content(
        self,
        analysis: ChangeGuardAnalysis,
        related_sources: set[str],
        tracked_paths: tuple[str, ...],
        test_paths: tuple[str, ...],
        revision: str,
    ) -> set[str]:
        """Tests that import a changed module or request a changed endpoint's URL."""
        modules = _module_names(tracked_paths)
        changed_modules = {name for name, path in modules.items() if path in related_sources}
        route_patterns = [_route_pattern(route) for route in _changed_routes(analysis)]
        if not changed_modules and not route_patterns:
            return set()
        matched: set[str] = set()
        for path in test_paths[:MAX_SCANNED_TESTS]:
            try:
                source = get_revision_file(analysis.repository, revision, path, max_bytes=MAX_SCANNED_TEST_BYTES)
            except (OSError, RuntimeError, ValueError):
                continue
            if _imported_modules(source) & changed_modules:
                matched.add(path)
            elif any(pattern.search(source) for pattern in route_patterns):
                matched.add(path)
        return matched

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
            "Only Python pytest/py_compile and the project's own JS/TS typecheck/test scripts are allowlisted.",
            "No shell, network, model-supplied command text, or repository write is permitted.",
            "Execution uses a disposable snapshot of the pinned Git revision.",
        )


def _module_names(paths: tuple[str, ...]) -> dict[str, str]:
    """Dotted module name -> path, also without a leading ``src.`` for src layouts."""
    names: dict[str, str] = {}
    for path in paths:
        if not path.endswith(".py"):
            continue
        dotted = path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
        names.setdefault(dotted, path)
        if dotted.startswith("src."):
            names.setdefault(dotted[4:], path)
    return names


def _imported_modules(source: str) -> set[str]:
    """Absolute modules a file imports; ``from a import b`` yields both ``a`` and ``a.b``."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _changed_routes(analysis: ChangeGuardAnalysis) -> list[str]:
    api = analysis.api_impact
    if api is None:
        return []
    changed = set(api.changed_endpoint_ids)
    return sorted({
        endpoint.route for endpoint in api.endpoints
        if endpoint.endpoint_id in changed and endpoint.route.strip("/")
    })


def _route_pattern(route: str) -> re.Pattern[str]:
    """``/drafts/{id}/send`` matches ``"/drafts/5/send"`` or ``f"/drafts/{d.id}/send"`` in test code."""
    parts = re.split(r"\{[^}]*\}|:[A-Za-z_]\w*|<[^>]*>", route)
    body = r"(?:\{[^}]*\}|[^/\s\"'?{}]+)".join(re.escape(part) for part in parts)
    return re.compile(rf"[\"']{body}(?:[\"'?#]|$)")
