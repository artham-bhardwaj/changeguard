"""SQLite persistence for analyses and local engineering evidence."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
from changeguard.models.schemas import AnalysisState
from changeguard.evidence.models import EvidenceChunk, EvidenceDocument

class SQLiteAnalysisStore:
    def __init__(self,database_path: str)->None: self.database_path=database_path; self._initialize()
    def _connect(self)->sqlite3.Connection:
        connection=sqlite3.connect(self.database_path); connection.row_factory=sqlite3.Row; return connection
    def _initialize(self)->None:
        Path(self.database_path).parent.mkdir(parents=True,exist_ok=True)
        with self._connect() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS analyses (analysis_id TEXT PRIMARY KEY,repository TEXT NOT NULL,base_commit TEXT NOT NULL,head_commit TEXT NOT NULL,changed_files_json TEXT NOT NULL,diff_summary_json TEXT NOT NULL,recent_commits_json TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_traces (id INTEGER PRIMARY KEY,analysis_id TEXT NOT NULL,step INTEGER NOT NULL,tool_name TEXT NOT NULL,input_json TEXT NOT NULL,output_summary TEXT NOT NULL,timestamp TEXT NOT NULL,transport TEXT NOT NULL DEFAULT 'local');
CREATE TABLE IF NOT EXISTS evidence (id INTEGER PRIMARY KEY,analysis_id TEXT NOT NULL,kind TEXT NOT NULL,summary TEXT NOT NULL,details_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS change_analyses (analysis_id TEXT PRIMARY KEY,repository TEXT NOT NULL,analysis_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_documents (id INTEGER PRIMARY KEY,evidence_id TEXT NOT NULL UNIQUE,title TEXT NOT NULL,source_type TEXT NOT NULL,source TEXT NOT NULL,repository TEXT NOT NULL,metadata_json TEXT NOT NULL,content_hash TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_chunks (id INTEGER PRIMARY KEY,chunk_id TEXT NOT NULL UNIQUE,evidence_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,content TEXT NOT NULL,character_count INTEGER NOT NULL,UNIQUE(evidence_id,chunk_index));
CREATE TABLE IF NOT EXISTS evaluation_results (evaluation_run_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS evaluation_reports (evaluation_run_id TEXT PRIMARY KEY,report_json TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS pr_analyses (repository TEXT NOT NULL,pull_number INTEGER NOT NULL,head_sha TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(repository,pull_number,head_sha));""")
            columns={row["name"] for row in c.execute("PRAGMA table_info(agent_traces)")}
            if "transport" not in columns: c.execute("ALTER TABLE agent_traces ADD COLUMN transport TEXT NOT NULL DEFAULT 'local'")
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError(f"Unsupported ChangeGuard SQLite schema version: {version}")
            if version < 1:
                c.execute("PRAGMA user_version = 1")
    def save_analysis(self,state: AnalysisState)->None:
        with self._connect() as c:
            c.execute("INSERT INTO analyses VALUES (?, ?, ?, ?, ?, ?, ?, ?)",(state.analysis_id,state.repository,state.base_commit,state.head_commit,json.dumps(state.changed_files),json.dumps(state.diff_summary),json.dumps([x.__dict__ for x in state.recent_commits]),state.status))
            c.executemany("INSERT INTO agent_traces (analysis_id,step,tool_name,input_json,output_summary,timestamp,transport) VALUES (?, ?, ?, ?, ?, ?, ?)",[(state.analysis_id,x.step,x.tool_name,json.dumps(x.input),x.output_summary,x.timestamp,x.transport) for x in state.agent_trace])
            c.executemany("INSERT INTO evidence (analysis_id,kind,summary,details_json) VALUES (?, ?, ?, ?)",[(state.analysis_id,x.kind,x.summary,json.dumps(x.details)) for x in state.evidence])
    def get_analysis(self,analysis_id: str)->dict[str,object]|None:
        with self._connect() as c: row=c.execute("SELECT * FROM analyses WHERE analysis_id=?",(analysis_id,)).fetchone()
        return None if row is None else dict(row)
    def save_change_analysis(self, analysis_id: str, repository: str, value: dict[str, object]) -> None:
        with self._connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO change_analyses (analysis_id,repository,analysis_json) VALUES (?, ?, ?)",
                (analysis_id, repository, json.dumps(value, separators=(",", ":"))),
            )
    def get_change_analysis(self, analysis_id: str) -> dict[str, object] | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT analysis_json FROM change_analyses WHERE analysis_id=?",
                (analysis_id,),
            ).fetchone()
        return None if row is None else json.loads(row["analysis_json"])
    def find_evidence_by_hash(self,content_hash: str)->str|None:
        with self._connect() as c: row=c.execute("SELECT evidence_id FROM evidence_documents WHERE content_hash=?",(content_hash,)).fetchone()
        return None if row is None else str(row["evidence_id"])
    def save_evidence_document(self,document: EvidenceDocument,chunks: list[EvidenceChunk])->None:
        with self._connect() as c:
            c.execute("INSERT INTO evidence_documents (evidence_id,title,source_type,source,repository,metadata_json,content_hash,created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",(document.evidence_id,document.title,document.source_type,document.source,document.repository,json.dumps(document.metadata),document.content_hash,document.created_at))
            c.executemany("INSERT INTO evidence_chunks (chunk_id,evidence_id,chunk_index,content,character_count) VALUES (?, ?, ?, ?, ?)",[(x.chunk_id,x.evidence_id,x.chunk_index,x.content,x.character_count) for x in chunks])
    def list_evidence_chunks(self,source_type: str|None=None,repository: str|None=None)->list[dict[str,object]]:
        clauses=[]; parameters=[]
        if source_type: clauses.append("d.source_type=?"); parameters.append(source_type)
        if repository: clauses.append("d.repository=?"); parameters.append(repository)
        where=" WHERE "+" AND ".join(clauses) if clauses else ""
        sql="SELECT d.evidence_id,d.title,d.source,d.source_type,d.metadata_json,c.chunk_id,c.content FROM evidence_chunks c JOIN evidence_documents d ON d.evidence_id=c.evidence_id"+where
        with self._connect() as c: rows=c.execute(sql,parameters).fetchall()
        return [{"evidence_id":r["evidence_id"],"title":r["title"],"source":r["source"],"source_type":r["source_type"],"metadata":json.loads(r["metadata_json"]),"chunk_id":r["chunk_id"],"content":r["content"]} for r in rows]
    def save_evaluation_result(self, value: dict[str, object]) -> None:
        with self._connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO evaluation_results (evaluation_run_id,case_id,result_json) VALUES (?, ?, ?)",
                (
                    str(value["evaluation_run_id"]),
                    str(value["case_id"]),
                    json.dumps(value, separators=(",", ":"), sort_keys=True),
                ),
            )
    def get_evaluation_result(self, evaluation_run_id: str) -> dict[str, object] | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT result_json FROM evaluation_results WHERE evaluation_run_id=?",
                (evaluation_run_id,),
            ).fetchone()
        return None if row is None else json.loads(row["result_json"])
    def save_evaluation_report(self, value: dict[str, object]) -> None:
        with self._connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO evaluation_reports (evaluation_run_id,report_json) VALUES (?, ?)",
                (
                    str(value["evaluation_run_id"]),
                    json.dumps(value, separators=(",", ":"), sort_keys=True),
                ),
            )
    def get_evaluation_report(self, evaluation_run_id: str) -> dict[str, object] | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT report_json FROM evaluation_reports WHERE evaluation_run_id=?",
                (evaluation_run_id,),
            ).fetchone()
        return None if row is None else json.loads(row["report_json"])
    def get_evaluation_run(self, evaluation_run_id: str) -> dict[str, object] | None:
        return self.get_evaluation_report(evaluation_run_id) or self.get_evaluation_result(evaluation_run_id)
    def list_evaluation_runs(self, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as c:
            rows = c.execute(
                "SELECT evaluation_run_id,case_id,created_at,'case' AS run_type FROM evaluation_results "
                "UNION ALL SELECT evaluation_run_id,NULL AS case_id,created_at,'aggregate' AS run_type "
                "FROM evaluation_reports ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
    def save_pr_analysis(self, value: dict[str, object]) -> None:
        if value.get("status") != "COMPLETED":
            raise ValueError("Only completed pull request analyses can be cached.")
        head_sha = value.get("head_sha")
        if not isinstance(head_sha, str):
            raise ValueError("A completed pull request analysis must have a head SHA.")
        with self._connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO pr_analyses "
                "(repository,pull_number,head_sha,result_json) VALUES (?,?,?,?)",
                (
                    str(value["repository"]),
                    int(value["pull_number"]),
                    head_sha,
                    json.dumps(value, separators=(",", ":"), sort_keys=True),
                ),
            )
    def get_pr_analysis(
        self, repository: str, pull_number: int, head_sha: str
    ) -> dict[str, object] | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT result_json FROM pr_analyses "
                "WHERE repository=? AND pull_number=? AND head_sha=?",
                (repository, pull_number, head_sha),
            ).fetchone()
        return None if row is None else json.loads(row["result_json"])
