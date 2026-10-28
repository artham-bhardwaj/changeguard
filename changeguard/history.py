"""Registered projects and saved reports, shared by the CLI and the web app."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    path TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    name TEXT NOT NULL,
    languages_json TEXT NOT NULL,
    frameworks_json TEXT NOT NULL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    repository TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_level TEXT,
    risk_score REAL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_created ON reports(created_at DESC);
"""
MAX_REPORTS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HistoryStore:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    # ----- projects -----

    def save_project(self, path: str, repository: str, name: str, languages: dict[str, int], frameworks: list[str]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO projects (path, repository, name, languages_json, frameworks_json, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET repository=excluded.repository, "
                "name=excluded.name, languages_json=excluded.languages_json, frameworks_json=excluded.frameworks_json",
                (path, repository, name, json.dumps(languages), json.dumps(frameworks), _now()),
            )

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [
            {
                "path": row["path"], "repository": row["repository"], "name": row["name"],
                "languages": json.loads(row["languages_json"]), "frameworks": json.loads(row["frameworks_json"]),
                "added_at": row["added_at"],
            }
            for row in rows
        ]

    def remove_project(self, path: str) -> bool:
        with self._connect() as connection:
            return connection.execute("DELETE FROM projects WHERE path = ?", (path,)).rowcount > 0

    # ----- reports -----

    def save_report(self, *, kind: str, target: str, repository: str, payload: dict[str, Any]) -> str:
        analysis = payload.get("analysis") or {}
        risk = analysis.get("risk_assessment") or {}
        report_id = uuid.uuid4().hex[:12]
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO reports (id, created_at, kind, target, repository, status, risk_level, risk_score, summary, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id, _now(), kind, target, repository, str(payload.get("status", "COMPLETED")),
                    risk.get("overall_level"), risk.get("score"), str(analysis.get("change_summary", ""))[:500],
                    json.dumps(payload),
                ),
            )
            connection.execute(
                "DELETE FROM reports WHERE id NOT IN (SELECT id FROM reports ORDER BY created_at DESC LIMIT ?)",
                (MAX_REPORTS,),
            )
        return report_id

    def list_reports(self, limit: int = 50, repository: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, created_at, kind, target, repository, status, risk_level, risk_score, summary FROM reports"
        params: list[Any] = []
        if repository:
            sql += " WHERE repository = ?"
            params.append(repository)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            return None
        result = {key: row[key] for key in row.keys() if key != "payload_json"}
        result["payload"] = json.loads(row["payload_json"])
        return result

    def delete_report(self, report_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute("DELETE FROM reports WHERE id = ?", (report_id,)).rowcount > 0
