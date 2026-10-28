from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from changeguard import gate
from changeguard.analysis import ChangeAnalysisService
from changeguard.github.models import PullRequestRef
from changeguard.project import write_github_action
from changeguard.risk import ChangeContext, ChangeMetrics, RiskEngine
from changeguard.risk.triggers import ReviewTrigger, detect_review_triggers
from changeguard.storage import SQLiteAnalysisStore
from tests.conftest import git

HEAD = "a" * 40
SEND_ROUTE = (
    "from fastapi import FastAPI\napp = FastAPI()\n\n"
    "@app.post('/drafts/{draft_id}/send')\n"
    "def send_draft(draft_id: int, ack: str = ''):\n"
    "    if ack != '1':\n"
    "        return redirect('/drafts', err='Tick the confirmation box')\n"
    "    deliver(draft_id)\n"
    "    return 'sent'\n"
)


def _diff(path: str, removed: list[str], added: list[str]) -> str:
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,{len(removed)} +1,{len(added)} @@\n{body}"


def _kinds(triggers) -> set[str]:
    return {item.kind for item in triggers}


# ----- review triggers ---------------------------------------------------------------------


def test_removed_check_is_a_trigger_but_a_reworded_one_is_not():
    removed = detect_review_triggers(_diff("app/routes.py", ["    if ack != '1':", "        raise Forbidden()"], []))
    reworded = detect_review_triggers(_diff("app/routes.py", ["    raise NotFound('a')"], ["    raise NotFound('b')"]))

    assert "guard_removed" in _kinds(removed)
    assert reworded == ()


def test_replacing_an_error_with_a_silent_return_is_a_trigger():
    triggers = detect_review_triggers(_diff("deps.py", ["        raise HTTPException(status_code=404)"], ["        return None"]))
    assert "guard_removed" in _kinds(triggers)


def test_removed_access_check_shell_and_destructive_calls():
    triggers = detect_review_triggers(
        _diff("api.py", ["@login_required"], ["subprocess.run(cmd, shell=True)", "shutil.rmtree(path)"])
    )
    assert {"access_check_removed", "shell_or_eval", "destructive_operation"} <= _kinds(triggers)


def test_hardcoded_secret_value_is_not_repeated_in_the_report():
    triggers = detect_review_triggers(_diff("config.py", [], ["API_KEY = 'sk-live-1234567890abcdef'"]))
    secret = next(item for item in triggers if item.kind == "hardcoded_secret")
    assert "sk-live" not in secret.reason


def test_review_process_dependency_sensitive_name_and_wide_reach():
    triggers = detect_review_triggers(
        "",
        [".github/workflows/changeguard.yml", "CODEOWNERS", "pyproject.toml", "README.md"],
        ["app.mail.send_via_gmail()", "app.ui.render_sender_name()", "tests.test_mail.test_send_retries()"],
        direct_callers=30,
    )
    by_kind = {item.kind: item for item in triggers}
    assert {item.location for item in triggers if item.kind == "review_process_changed"} == {
        ".github/workflows/changeguard.yml", "CODEOWNERS",
    }
    assert by_kind["dependencies_changed"].severity == "MEDIUM"
    assert "send_via_gmail" in by_kind["sensitive_code_changed"].reason
    assert "render_sender_name" not in by_kind["sensitive_code_changed"].reason
    assert "test_send_retries" not in by_kind["sensitive_code_changed"].reason
    assert by_kind["wide_reach"].severity == "HIGH"


def test_test_files_skip_shell_rules_but_still_flag_removed_assertions():
    shell = detect_review_triggers(_diff("tests/test_cli.py", [], ["    subprocess.run([sys.executable, '-m', 'app'])"]))
    removed = detect_review_triggers(_diff("tests/test_cli.py", ["    assert result.ok"], []))

    assert shell == ()
    assert removed[0].kind == "guard_removed" and removed[0].reason.startswith("Removes a test assertion")


def test_docs_only_change_has_no_triggers():
    assert detect_review_triggers(_diff("docs/guide.md", ["if you"], ["when you"]), ["docs/guide.md"]) == ()


# ----- risk level floor --------------------------------------------------------------------


def test_triggers_raise_the_level_but_not_the_score():
    small = ChangeMetrics(files_changed=1, lines_added=0, lines_deleted=2, symbols_changed=1, methods_changed=1)
    plain = RiskEngine().assess(ChangeContext(change=small))
    flagged = RiskEngine().assess(ChangeContext(
        change=small, review_triggers=(ReviewTrigger("guard_removed", "HIGH", "Removes a check"),),
    ))

    assert plain.overall_level == "LOW"
    assert flagged.overall_level == "HIGH"
    assert flagged.score == plain.score
    assert flagged.metrics["level_before_review_triggers"] == "LOW"
    assert flagged.recommendations[0].startswith("Have a senior developer review")


def test_medium_trigger_never_lowers_a_higher_level():
    engine = RiskEngine()
    assert engine._apply_trigger_floor("CRITICAL", (ReviewTrigger("x", "MEDIUM", "r"),)) == "CRITICAL"
    assert engine._apply_trigger_floor("UNKNOWN", (ReviewTrigger("x", "MEDIUM", "r"),)) == "MEDIUM"


# ----- end to end: removing a check from an endpoint ---------------------------------------


@pytest.fixture
def guard_removal_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    (repo / "app.py").write_text(SEND_ROUTE, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    base = git(repo, "rev-parse", "HEAD")
    without_check = SEND_ROUTE.replace(
        "    if ack != '1':\n        return redirect('/drafts', err='Tick the confirmation box')\n", ""
    )
    (repo / "app.py").write_text(without_check, encoding="utf-8")
    git(repo, "commit", "-am", "agent: simplify send flow")
    return repo, base, git(repo, "rev-parse", "HEAD")


def test_removing_a_check_marks_the_endpoint_changed_and_raises_risk(tmp_path, guard_removal_repo):
    repo, base, head = guard_removal_repo
    result = ChangeAnalysisService(SQLiteAnalysisStore(str(tmp_path / "db.sqlite3"))).analyze(
        repo_path=str(repo), base=base, head=head
    ).to_dict()

    api = result["api_impact"]
    changed = [f"{e['http_method']} {e['route']}" for e in api["endpoints"] if e["endpoint_id"] in api["changed_endpoint_ids"]]
    assert changed == ["POST /drafts/{draft_id}/send"]
    risk = result["risk_assessment"]
    assert risk["overall_level"] == "HIGH"
    assert {"guard_removed", "sensitive_code_changed"} <= {t["kind"] for t in risk["review_triggers"]}


def test_impact_fail_on_exits_4_and_explains(tmp_path, guard_removal_repo):
    repo, _base, _head = guard_removal_repo
    db = str(tmp_path / "db.sqlite3")
    command = [sys.executable, "-m", "changeguard.cli", "impact", "--repo", str(repo), "--database", db]

    blocked = subprocess.run([*command, "--fail-on", "high"], capture_output=True, text=True, check=False)
    passed = subprocess.run([*command, "--fail-on", "critical"], capture_output=True, text=True, check=False)
    ungated = subprocess.run(command, capture_output=True, text=True, check=False)

    assert blocked.returncode == gate.GATE_EXIT_CODE
    assert "waiting for senior review" in blocked.stdout
    assert "Removes a check" in blocked.stdout
    assert "gate BLOCKED" in blocked.stderr
    assert passed.returncode == 0 and "Review gate: ✅ passed" in passed.stdout
    assert ungated.returncode == 0 and "Review gate" not in ungated.stdout


# ----- gate decisions ----------------------------------------------------------------------


def _analysis(level: str, triggers=()) -> dict:
    return {"risk_assessment": {"overall_level": level, "review_triggers": list(triggers)}}


@pytest.mark.parametrize(
    ("level", "fail_on", "blocked"),
    [("LOW", "HIGH", False), ("MEDIUM", "HIGH", False), ("HIGH", "HIGH", True), ("CRITICAL", "HIGH", True),
     ("MEDIUM", "medium", True), ("UNKNOWN", "CRITICAL", True)],
)
def test_gate_thresholds_and_fail_closed(level, fail_on, blocked):
    assert gate.evaluate(_analysis(level), fail_on).blocked is blocked


def test_incomplete_analysis_blocks():
    assert gate.evaluate(None, "CRITICAL").blocked
    assert gate.evaluate(_analysis("LOW"), "CRITICAL", completed=False).blocked


def test_parse_approvers(monkeypatch):
    monkeypatch.setenv("CHANGEGUARD_APPROVERS", "@Alice, bob\n")
    assert gate.parse_approvers(None) == ["alice", "bob"]
    assert gate.parse_approvers("carol") == ["carol"]
    assert gate.parse_approvers("") == []


# ----- approvals from GitHub ---------------------------------------------------------------


class FakeGitHub:
    def __init__(self, reviews, author="agent-bot"):
        self.responses = {"pull": {"user": {"login": author}}, "reviews": reviews}

    def __call__(self, request, timeout=30):
        key = "reviews" if "/reviews" in request.full_url else "pull"
        return io.BytesIO(json.dumps(self.responses[key]).encode())


def _review(login, state, commit=HEAD, association="COLLABORATOR", kind="User"):
    return {"user": {"login": login, "type": kind}, "state": state, "commit_id": commit, "author_association": association}


REF = PullRequestRef("acme", "jobpilot", 7)


def _approved(reviews, approvers=(), author="agent-bot"):
    return gate.find_senior_approval(REF, HEAD, "token", list(approvers), opener=FakeGitHub(reviews, author))


def test_collaborator_approval_of_the_latest_commit_counts():
    assert _approved([_review("Senior", "APPROVED")]) == "Senior"


@pytest.mark.parametrize(
    "reviews",
    [
        [_review("senior", "APPROVED", commit="b" * 40)],                      # older commit
        [_review("agent-bot", "APPROVED")],                                    # PR author
        [_review("helper[bot]", "APPROVED", kind="Bot")],                      # bot account
        [_review("outsider", "APPROVED", association="CONTRIBUTOR")],          # not trusted
        [_review("senior", "APPROVED"), _review("senior", "CHANGES_REQUESTED")],
        [_review("senior", "APPROVED"), _review("senior", "DISMISSED")],
        [_review("senior", "COMMENTED")],
    ],
)
def test_approvals_that_do_not_count(reviews):
    assert _approved(reviews) is None


def test_approver_list_limits_who_can_clear_the_gate():
    reviews = [_review("someone", "APPROVED", association="OWNER")]
    assert _approved(reviews, approvers=["lead"]) is None
    assert _approved(reviews + [_review("Lead", "APPROVED")], approvers=["lead"]) == "Lead"


def test_pull_request_gate_is_cleared_by_approval_and_reports_who():
    payload = {"status": "COMPLETED", "head_sha": HEAD, "analysis": _analysis("HIGH")}
    result = gate.evaluate_pull_request(
        REF, payload, "HIGH", [], "token", opener=FakeGitHub([_review("senior", "APPROVED")])
    )
    assert not result.blocked and result.approved_by == "senior"
    assert "approved by @senior" in result.markdown()


def test_pull_request_gate_stays_blocked_without_token():
    payload = {"status": "COMPLETED", "head_sha": HEAD, "analysis": _analysis("HIGH")}
    result = gate.evaluate_pull_request(REF, payload, "HIGH", [], None)
    assert result.blocked and "Could not check approvals" in result.reason


# ----- generated workflow ------------------------------------------------------------------


def test_workflow_fails_the_check_and_reruns_on_reviews(tmp_path):
    path, written = write_github_action(tmp_path, fail_on="high")
    text = path.read_text(encoding="utf-8")

    assert written
    assert "|| true" not in text
    assert "--fail-on high" in text
    assert "pull_request_review:" in text
    assert "name: ChangeGuard review gate" in text
    assert 'exit $status' in text
    assert "${{ vars.CHANGEGUARD_APPROVERS }}" in text
