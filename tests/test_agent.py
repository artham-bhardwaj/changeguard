from __future__ import annotations

from changeguard.agent import DeterministicAgent
from changeguard.storage.sqlite import SQLiteAnalysisStore


def test_deterministic_agent_runs_and_persists_trace(git_repo, tmp_path):
    repo, base, head = git_repo
    store = SQLiteAnalysisStore(str(tmp_path / "analysis.sqlite3"))
    result = DeterministicAgent(store).investigate(repo_path=str(repo), base=base, head=head)
    assert result.status == "completed"
    assert result.changed_files == ["app.txt", "new.txt"]
    assert [item.tool_name for item in result.agent_trace] == [
        "get_repo_status", "get_git_diff", "get_recent_commits", "search_evidence"]
    assert len(result.agent_trace) == DeterministicAgent.max_tool_calls
    assert store.get_analysis(result.analysis_id) is not None
