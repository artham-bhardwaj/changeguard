"""Review gate: block a change at or above a risk level until a senior developer approves it.

Used by ``changeguard impact --fail-on`` (local commits, e.g. before an AI agent pushes) and
``changeguard pr analyze --fail-on`` (pull requests, e.g. as a required GitHub check). The gate
fails closed: an incomplete analysis or an unknown risk level blocks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from urllib.request import urlopen

from changeguard.github.comments import CommentError, _call
from changeguard.github.models import PullRequestRef

LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
GATE_EXIT_CODE = 4
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass
class GateResult:
    blocked: bool
    level: str
    fail_on: str
    reason: str
    approved_by: str | None = None
    triggers: list[dict[str, Any]] = field(default_factory=list)

    def markdown(self) -> str:
        if self.approved_by:
            head = f"## Review gate: ✅ approved by @{self.approved_by}"
        elif self.blocked:
            head = "## Review gate: ⛔ waiting for senior review"
        else:
            head = "## Review gate: ✅ passed"
        return f"{head}\n\n{self.reason}\n"

    def summary(self) -> str:
        state = "APPROVED" if self.approved_by else ("BLOCKED" if self.blocked else "PASSED")
        return f"ChangeGuard gate {state}: {self.reason}"


def parse_approvers(value: str | None) -> list[str]:
    raw = value if value is not None else os.environ.get("CHANGEGUARD_APPROVERS", "")
    return [item.strip().lstrip("@").lower() for item in raw.replace("\n", ",").split(",") if item.strip()]


def evaluate(analysis: dict[str, Any] | None, fail_on: str, *, completed: bool = True) -> GateResult:
    """Decide from the analysis alone (no approvals considered)."""
    fail_on = fail_on.upper()
    if not completed or not analysis:
        return GateResult(True, "UNKNOWN", fail_on, "The analysis did not complete, so the change cannot be cleared automatically.")
    risk = analysis.get("risk_assessment") or {}
    level = str(risk.get("overall_level") or "UNKNOWN").upper()
    triggers = list(risk.get("review_triggers") or [])
    if level not in LEVELS:
        return GateResult(True, level, fail_on, "The risk level is unknown, so the change cannot be cleared automatically.", triggers=triggers)
    if LEVELS.index(level) < LEVELS.index(fail_on):
        return GateResult(False, level, fail_on, f"Risk is {level}, below the {fail_on} review threshold.", triggers=triggers)
    reasons = "; ".join(str(item.get("reason", "")) for item in triggers[:3])
    because = f" Review triggers: {reasons}" if reasons else ""
    return GateResult(
        True, level, fail_on,
        f"Risk is {level} (threshold {fail_on}). A senior developer must review and approve this change.{because}",
        triggers=triggers,
    )


def find_senior_approval(
    reference: PullRequestRef,
    head_sha: str,
    token: str | None,
    approvers: list[str],
    *,
    opener=urlopen,
) -> str | None:
    """Login of a reviewer who approved ``head_sha``, or None.

    With ``approvers`` set, only those logins count. Otherwise any owner, member or collaborator
    counts. The PR author and bot accounts never count, and a later "changes requested" or a
    dismissal replaces that reviewer's earlier approval. Approvals of older commits don't count,
    so every new push needs a fresh approval.
    """
    if not token:
        raise CommentError("Checking approvals needs a GitHub token (GITHUB_TOKEN or `gh auth login`).")
    base = f"/repos/{reference.owner}/{reference.repository}/pulls/{reference.number}"
    pull = _call("GET", base, token, opener)
    author = str(((pull or {}).get("user") or {}).get("login", "")).lower()
    latest: dict[str, dict[str, Any]] = {}
    for page in range(1, 6):
        reviews = _call("GET", f"{base}/reviews?per_page=100&page={page}", token, opener)
        if not isinstance(reviews, list):
            break
        for review in reviews:
            user = review.get("user") or {}
            login = str(user.get("login", "")).lower()
            if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"} and login:
                latest[login] = {**review, "_bot": user.get("type") == "Bot"}
        if len(reviews) < 100:
            break
    for login, review in latest.items():
        if login == author or review["_bot"] or review.get("state") != "APPROVED":
            continue
        if review.get("commit_id") != head_sha:
            continue
        if approvers and login not in approvers:
            continue
        if not approvers and review.get("author_association") not in TRUSTED_ASSOCIATIONS:
            continue
        return str((review.get("user") or {}).get("login", login))
    return None


def evaluate_pull_request(
    reference: PullRequestRef,
    payload: dict[str, Any],
    fail_on: str,
    approvers: list[str],
    token: str | None,
    *,
    opener=urlopen,
) -> GateResult:
    result = evaluate(payload.get("analysis"), fail_on, completed=payload.get("status") == "COMPLETED")
    head_sha = payload.get("head_sha")
    if not result.blocked or not head_sha or payload.get("status") != "COMPLETED":
        return result
    who = ", ".join(f"@{name}" for name in approvers) if approvers else "a repository owner, member or collaborator"
    try:
        approved_by = find_senior_approval(reference, head_sha, token, approvers, opener=opener)
    except CommentError as error:
        result.reason += f" Could not check approvals: {error}"
        return result
    if approved_by:
        result.blocked = False
        result.approved_by = approved_by
        result.reason = f"Risk is {result.level}; @{approved_by} approved commit `{head_sha[:12]}`."
    else:
        result.reason += f" Waiting for an approving review of commit `{head_sha[:12]}` from {who}."
    return result
