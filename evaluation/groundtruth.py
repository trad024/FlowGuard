"""Ground-truth store (D25, information firewall).

What the experiment knows and the system must not: each run's spec, ground truth, what L0
did, and every injection event (scenario started, fault injected or cleared, node stress,
L0 outcome). It lives in its own SQLite file (default evaluation/runs/groundtruth.db,
gitignored; FLOWGUARD_GROUNDTRUTH_DB overrides) that nothing under agents/ ever opens. The
shared audit log keeps only what a real system would see.

Standard library only, like the audit log.

    truth = GroundTruthDB.open()
    truth.event("r-1a2b3c4d", "fault_injected", delivery="config", settings={...})
    truth.record_run("r-1a2b3c4d", spec=spec, ground_truth=gt, observed=obs, l0_correct=True)
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

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "evaluation" / "runs" / "groundtruth.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,            -- scenario_started, fault_injected, l0_outcome, ...
    payload TEXT NOT NULL          -- JSON
);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id, ts);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    spec_id TEXT,
    family TEXT,
    recorded_at TEXT NOT NULL,
    recording TEXT,                -- the recording folder, if any
    spec TEXT NOT NULL,            -- JSON
    ground_truth TEXT NOT NULL,    -- JSON
    observed TEXT NOT NULL,        -- JSON
    l0_correct INTEGER,            -- 1, 0 or NULL (not judged at L0, or not valid)
    valid INTEGER NOT NULL         -- 0 when the host slept during the run
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GroundTruthDB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)

    @classmethod
    def open(cls) -> GroundTruthDB:
        return cls(os.environ.get("FLOWGUARD_GROUNDTRUTH_DB") or DEFAULT_PATH)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def event(self, run_id: str, kind: str, /, *, ts: str | None = None, **payload: Any) -> int:
        with closing(self._connect()) as db, db:
            return db.execute(
                "INSERT INTO events (ts, run_id, kind, payload) VALUES (?, ?, ?, ?)",
                (ts or utc_now(), run_id, kind, json.dumps(payload, default=str, sort_keys=True)),
            ).lastrowid

    def record_run(
        self,
        run_id: str,
        *,
        spec: dict,
        ground_truth: dict,
        observed: dict,
        l0_correct: bool | None,
        valid: bool = True,
        recording: str | None = None,
    ) -> None:
        """One row per run; recording the same run again replaces it."""
        row = (
            run_id,
            spec.get("id"),
            spec.get("family"),
            utc_now(),
            recording,
            json.dumps(spec, default=str, sort_keys=True),
            json.dumps(ground_truth, default=str, sort_keys=True),
            json.dumps(observed, default=str, sort_keys=True),
            None if l0_correct is None else int(l0_correct),
            int(valid),
        )
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)

    def events(self, run_id: str | None = None, kind: str | None = None) -> Iterator[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if run_id:
            sql, args = sql + " AND run_id = ?", [*args, run_id]
        if kind:
            sql, args = sql + " AND kind = ?", [*args, kind]
        with closing(self._connect()) as db:
            for row in db.execute(sql + " ORDER BY id", args):
                yield {**dict(row), "payload": json.loads(row["payload"])}

    def run(self, run_id: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        for key in ("spec", "ground_truth", "observed"):
            out[key] = json.loads(out[key])
        if out["l0_correct"] is not None:
            out["l0_correct"] = bool(out["l0_correct"])
        out["valid"] = bool(out["valid"])
        return out
