"""Shared incident record (D5): incidents, evidence posts and agent requests in SQLite.

Agents read and post here; collaboration exchanges evidence, never actions. The tables live in
the system's own database (the audit log's file, state/audit.db by default), next to the
audit tables. Rows are only inserted, except an incident's status.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from flowguard.contracts import AgentRequest, EvidencePost, Incident

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    opened_at TEXT NOT NULL,
    status TEXT NOT NULL,            -- open, resolved, escalated
    body TEXT NOT NULL               -- the Incident contract, JSON
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    agent TEXT NOT NULL,
    claim TEXT NOT NULL,
    refs TEXT NOT NULL,              -- JSON list
    posted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_requests (
    id INTEGER PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    question TEXT NOT NULL,
    asked_at TEXT NOT NULL
);
"""
STATUSES = ("open", "resolved", "escalated")


class IncidentRecord:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def open(self, incident: Incident) -> str:
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO incidents (id, opened_at, status, body) VALUES (?, ?, ?, ?)",
                (
                    incident.id,
                    incident.detected_at.isoformat(),
                    "open",
                    incident.model_dump_json(),
                ),
            )
        return incident.id

    def incident(self, incident_id: str) -> tuple[Incident, str] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if row is None:
            return None
        return Incident.model_validate_json(row["body"]), row["status"]

    def set_status(self, incident_id: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        with closing(self._connect()) as db, db:
            db.execute("UPDATE incidents SET status = ? WHERE id = ?", (status, incident_id))

    def post(self, evidence: EvidencePost) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO evidence (incident_id, agent, claim, refs, posted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    evidence.incident_id,
                    str(evidence.agent),
                    evidence.claim,
                    json.dumps(evidence.refs),
                    evidence.posted_at.isoformat(),
                ),
            )

    def evidence(self, incident_id: str) -> list[EvidencePost]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM evidence WHERE incident_id = ? ORDER BY id", (incident_id,)
            ).fetchall()
        return [
            EvidencePost(
                incident_id=r["incident_id"],
                agent=r["agent"],
                claim=r["claim"],
                refs=json.loads(r["refs"]),
                posted_at=r["posted_at"],
            )
            for r in rows
        ]

    def ask(self, request: AgentRequest) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO agent_requests (incident_id, from_agent, to_agent, question, asked_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    request.incident_id,
                    str(request.from_agent),
                    str(request.to_agent),
                    request.question,
                    request.asked_at.isoformat(),
                ),
            )

    def requests(self, incident_id: str) -> list[AgentRequest]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM agent_requests WHERE incident_id = ? ORDER BY id", (incident_id,)
            ).fetchall()
        return [
            AgentRequest(
                incident_id=r["incident_id"],
                from_agent=r["from_agent"],
                to_agent=r["to_agent"],
                question=r["question"],
                asked_at=r["asked_at"],
            )
            for r in rows
        ]
