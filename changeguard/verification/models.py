from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

VerificationStatus = Literal[
    "NOT_RUN", "RUNNING", "PASSED", "FAILED", "TIMED_OUT", "BLOCKED", "ERROR"
]
VerificationOperation = Literal["PYTHON_COMPILE", "PYTHON_PYTEST"]
PlanStatus = Literal["READY", "UNSUPPORTED_PROJECT", "NO_CHECKS"]


@dataclass(frozen=True)
class VerificationCheck:
    check_id: str
    operation: VerificationOperation
    targets: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class VerificationPlan:
    plan_id: str
    analysis_id: str
    repository: str
    working_revision: str
    checks: tuple[VerificationCheck, ...]
    constraints: tuple[str, ...]
    timeout_seconds: int
    max_output_bytes: int
    max_checks: int
    workspace_max_bytes: int
    status: PlanStatus = "READY"
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationEvidence:
    check_id: str
    check_type: VerificationOperation
    repository: str
    revision: str
    status: VerificationStatus
    started_at: str
    duration_seconds: float
    output_summary: str
    failure_summary: str | None = None


@dataclass(frozen=True)
class VerificationCheckResult:
    check: VerificationCheck
    status: VerificationStatus
    duration_seconds: float
    output_summary: str
    failure_summary: str | None
    evidence: VerificationEvidence
    return_code: int | None = None


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    checks: tuple[VerificationCheckResult, ...]
    duration_seconds: float
    stdout_summary: str
    stderr_summary: str
    failures: tuple[str, ...]
    evidence: tuple[VerificationEvidence, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def verification_plan_from_dict(value: dict[str, object]) -> VerificationPlan:
    checks = tuple(
        VerificationCheck(
            check_id=str(item["check_id"]),
            operation=item["operation"],  # type: ignore[arg-type]
            targets=tuple(item["targets"]),  # type: ignore[arg-type]
            rationale=str(item["rationale"]),
        )
        for item in value["checks"]  # type: ignore[union-attr]
    )
    return VerificationPlan(
        plan_id=str(value["plan_id"]),
        analysis_id=str(value["analysis_id"]),
        repository=str(value["repository"]),
        working_revision=str(value["working_revision"]),
        checks=checks,
        constraints=tuple(value["constraints"]),  # type: ignore[arg-type]
        timeout_seconds=int(value["timeout_seconds"]),
        max_output_bytes=int(value["max_output_bytes"]),
        max_checks=int(value["max_checks"]),
        workspace_max_bytes=int(value["workspace_max_bytes"]),
        status=value["status"],  # type: ignore[arg-type]
        limitations=tuple(value["limitations"]),  # type: ignore[arg-type]
    )


def verification_result_from_dict(value: dict[str, object]) -> VerificationResult:
    from changeguard.verification.models import VerificationCheckResult

    results = []
    for item in value["checks"]:  # type: ignore[union-attr]
        check_value = item["check"]
        evidence_value = item["evidence"]
        results.append(
            VerificationCheckResult(
                check=VerificationCheck(
                    check_id=check_value["check_id"],
                    operation=check_value["operation"],
                    targets=tuple(check_value["targets"]),
                    rationale=check_value["rationale"],
                ),
                status=item["status"],
                duration_seconds=float(item["duration_seconds"]),
                output_summary=str(item["output_summary"]),
                failure_summary=item["failure_summary"],
                evidence=VerificationEvidence(**evidence_value),
                return_code=item["return_code"],
            )
        )
    return VerificationResult(
        status=value["status"],  # type: ignore[arg-type]
        checks=tuple(results),
        duration_seconds=float(value["duration_seconds"]),
        stdout_summary=str(value["stdout_summary"]),
        stderr_summary=str(value["stderr_summary"]),
        failures=tuple(value["failures"]),  # type: ignore[arg-type]
        evidence=tuple(VerificationEvidence(**item) for item in value["evidence"]),  # type: ignore[union-attr]
        limitations=tuple(value["limitations"]),  # type: ignore[arg-type]
    )
