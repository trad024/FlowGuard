"""SQLite audit log: every event, proposal, gate decision, approval and execution result.

One file per laptop (default evaluation/runs/audit.db, gitignored; FLOWGUARD_AUDIT_DB
overrides). Every row carries the scenario id and the ablation level it belongs to, so the
evaluation can filter by both. Rows are only ever inserted, never updated or deleted.

Standard library only, so the stdlib-only laptop scripts (deployer, scenario runner) can write
to it. Contract objects (ActionProposal, GateDecision, ExecutionResult) are stored through
their own model_dump, so this module does not import pydantic.

    audit = AuditLog.open()
    audit.event("deployer", "deploy_finished", environment="staging", commit_sha=sha,
                outcome="promoted", seconds=285)
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "evaluation" / "runs" / "audit.db"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,          -- deployer, security_gate, scenario_runner, monitor, ...
    kind TEXT NOT NULL,            -- deploy_finished, security_decision, scenario_started, ...
    scenario_id TEXT,
    level TEXT NOT NULL,           -- ablation level: L0 .. L5
    environment TEXT,
    commit_sha TEXT,
    payload TEXT NOT NULL          -- JSON object
);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, ts);
CREATE INDEX IF NOT EXISTS events_scenario ON events (scenario_id, ts);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    scenario_id TEXT,
    level TEXT NOT NULL,
    incident_id TEXT,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    environment TEXT NOT NULL,
    confidence REAL NOT NULL,
    rationale TEXT NOT NULL,
    params TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_decisions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    proposal_id TEXT NOT NULL REFERENCES proposals (id),
    scenario_id TEXT,
    level TEXT NOT NULL,
    tier TEXT NOT NULL,
    risk_score REAL NOT NULL,
    outcome TEXT NOT NULL,         -- execute, ask_human, reject
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    proposal_id TEXT NOT NULL REFERENCES proposals (id),
    scenario_id TEXT,
    level TEXT NOT NULL,
    verdict TEXT NOT NULL,         -- approved, denied, timeout
    approver TEXT,                 -- team member, or "simulated" (D17)
    wait_seconds REAL
);
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    proposal_id TEXT NOT NULL REFERENCES proposals (id),
    scenario_id TEXT,
    level TEXT NOT NULL,
    success INTEGER NOT NULL,
    validated INTEGER,             -- NULL = not applicable
    output TEXT NOT NULL
);
"""

VERDICTS = ("approved", "denied", "timeout")
OUTCOMES = ("execute", "ask_human", "reject")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return str(value) if value else utc_now()


def _dump(obj: Any) -> dict:
    """A contract object (pydantic) or a plain dict, as a JSON-ready dict."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return dict(obj)


class AuditLog:
    """Append-only writer and reader. `scenario_id` and `level` are stamped on every row."""

    def __init__(self, path: Path | str, scenario_id: str | None = None, level: str = "L0"):
        self.path = Path(path)
        self.scenario_id = scenario_id
        self.level = level
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @classmethod
    def open(cls, scenario_id: str | None = None, level: str | None = None) -> AuditLog:
        """The laptop's audit log; FLOWGUARD_AUDIT_DB, _SCENARIO and _LEVEL override."""
        path = os.environ.get("FLOWGUARD_AUDIT_DB") or DEFAULT_PATH
        scenario = scenario_id or os.environ.get("FLOWGUARD_SCENARIO") or None
        return cls(path, scenario, level or os.environ.get("FLOWGUARD_LEVEL") or "L0")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _insert(self, table: str, row: dict) -> int:
        row = {"scenario_id": self.scenario_id, "level": self.level, **row}
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with closing(self._connect()) as db, db:
            cur = db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
            return cur.lastrowid

    # ------------------------------------------------------------------ writers

    def event(
        self,
        source: str,
        kind: str,
        *,
        environment: str | None = None,
        commit_sha: str | None = None,
        ts: str | None = None,
        **payload: Any,
    ) -> int:
        return self._insert(
            "events",
            {
                "ts": ts or utc_now(),
                "source": source,
                "kind": kind,
                "environment": environment,
                "commit_sha": commit_sha,
                "payload": json.dumps(payload, default=str, sort_keys=True),
            },
        )

    def proposal(self, proposal: Any) -> str:
        p = _dump(proposal)
        self._insert(
            "proposals",
            {
                "id": p["id"],
                "ts": _iso(p.get("proposed_at")),
                "incident_id": p.get("incident_id"),
                "agent": p["agent"],
                "action": p["action"],
                "target": p["target"],
                "environment": p["environment"],
                "confidence": p["confidence"],
                "rationale": p["rationale"],
                "params": json.dumps(p.get("params") or {}, sort_keys=True),
            },
        )
        return p["id"]

    def gate_decision(self, decision: Any) -> int:
        d = _dump(decision)
        if d["outcome"] not in OUTCOMES:
            raise ValueError(f"gate outcome must be one of {OUTCOMES}")
        row_id = self._insert(
            "gate_decisions",
            {
                "ts": _iso(d.get("decided_at")),
                "proposal_id": d["proposal_id"],
                "tier": d["tier"],
                "risk_score": d["risk_score"],
                "outcome": d["outcome"],
                "reason": d["reason"],
            },
        )
        if d.get("human_verdict"):
            self.approval(d["proposal_id"], d["human_verdict"], d.get("approved_by"))
        return row_id

    def approval(
        self,
        proposal_id: str,
        verdict: str,
        approver: str | None,
        wait_seconds: float | None = None,
    ) -> int:
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")
        return self._insert(
            "approvals",
            {
                "ts": utc_now(),
                "proposal_id": proposal_id,
                "verdict": verdict,
                "approver": approver,
                "wait_seconds": wait_seconds,
            },
        )

    def execution(self, result: Any) -> int:
        r = _dump(result)
        validated = r.get("validated")
        return self._insert(
            "executions",
            {
                "ts": _iso(r.get("finished_at")),
                "proposal_id": r["proposal_id"],
                "success": int(bool(r["success"])),
                "validated": None if validated is None else int(bool(validated)),
                "output": r.get("output", ""),
            },
        )

    # ------------------------------------------------------------------ readers

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(sql, params)]

    def events(self, kind: str | None = None, scenario_id: str | None = None) -> Iterator[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if kind:
            sql, args = sql + " AND kind = ?", [*args, kind]
        if scenario_id:
            sql, args = sql + " AND scenario_id = ?", [*args, scenario_id]
        for row in self.query(sql + " ORDER BY ts, id", args):
            row["payload"] = json.loads(row["payload"])
            yield row
