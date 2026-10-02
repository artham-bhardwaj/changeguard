from __future__ import annotations

import sqlite3

from changeguard.models.schemas import AnalysisState, AgentTrace, Evidence
from changeguard.storage.sqlite import SQLiteAnalysisStore


def test_sqlite_persists_analysis_trace_and_evidence(tmp_path):
    store = SQLiteAnalysisStore(str(tmp_path / "state.sqlite3"))
    state = AnalysisState("/repo", "base", "head", ["a.py"], {"additions": 1, "deletions": 0}, [],
                          [Evidence("diff", "one file", {"files": 1})],
                          [AgentTrace(1, "get_git_diff", {}, "one file")], "completed")
    store.save_analysis(state)
    saved = store.get_analysis(state.analysis_id)
    assert saved is not None
    assert saved["repository"] == "/repo"
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_traces").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
