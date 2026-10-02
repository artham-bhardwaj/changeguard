from __future__ import annotations

import pytest

from changeguard.tools.filesystem_tools import FileTooLargeError, get_file
from changeguard.tools.git_tools import get_git_diff, get_recent_commits, get_repo_status


def test_git_diff_reports_files_and_line_counts(git_repo):
    repo, base, head = git_repo
    result = get_git_diff(str(repo), base, head)
    assert result.changed_files == ["app.txt", "new.txt"]
    assert result.additions == 2
    assert result.deletions == 0
    assert "+second line" in result.diff_text


def test_recent_commits_returns_metadata(git_repo):
    repo, _, head = git_repo
    commits = get_recent_commits(str(repo), 2)
    assert commits[0].commit == head
    assert commits[0].subject == "add a second line"
    assert len(commits) == 2


def test_repo_status(git_repo):
    repo, _, head = git_repo
    status = get_repo_status(str(repo))
    assert status.branch == "main"
    assert status.head_commit == head
    assert status.modified_files == []


def test_file_size_limit(git_repo):
    repo, _, _ = git_repo
    (repo / "large.txt").write_bytes(b"x" * 11)
    with pytest.raises(FileTooLargeError):
        get_file(str(repo), "large.txt", max_bytes=10)
    assert get_file(str(repo), "app.txt") == "first line\nsecond line\n"
