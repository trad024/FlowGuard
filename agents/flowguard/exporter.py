"""Prometheus metrics from the audit log and the incident record, for the Grafana dashboards.

    python -m flowguard.exporter [--host 127.0.0.1] [--port 9464]     # GET /metrics

Counts only, per ablation level: proposals, gate outcomes, approvals, executions, deployments
and Monitor incidents, plus the DSO6 safety counts (executions without a required approval or
after a reject, both must stay 0) and approvals per incident. No rationale, log or parameter
text leaves the database. The file is opened read-only (FLOWGUARD_AUDIT_DB, else
state/audit.db); a missing file gives flowguard_audit_up 0. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
from collections import defaultdict
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from flowguard.audit import DEFAULT_PATH

PORT = 9464

# name, help, type, query: label columns then a `value` column
COUNTS = (
    (
        "flowguard_proposals_total",
        "Agent proposals by level, agent and action.",
        "SELECT level, agent, action, COUNT(*) AS value FROM proposals"
        " GROUP BY level, agent, action",
    ),
    (
        "flowguard_gate_decisions_total",
        "Gate decisions by level, outcome (execute, ask_human, reject) and tier.",
        "SELECT level, outcome, tier, COUNT(*) AS value FROM gate_decisions"
        " GROUP BY level, outcome, tier",
    ),
    (
        "flowguard_approvals_total",
        "Human (or simulated) answers by level and verdict.",
        "SELECT level, verdict, COUNT(*) AS value FROM approvals GROUP BY level, verdict",
    ),
    (
        "flowguard_executions_total",
        "Executor results by level and success.",
        "SELECT level, CASE success WHEN 1 THEN 'true' ELSE 'false' END AS success,"
        " COUNT(*) AS value FROM executions GROUP BY level, success",
    ),
    (
        "flowguard_deployments_total",
        "Finished canary deployments by level, environment and outcome.",
        "SELECT level, COALESCE(environment, '') AS environment,"
        " COALESCE(json_extract(payload, '$.outcome'), '') AS outcome, COUNT(*) AS value"
        " FROM events WHERE kind = 'deploy_finished' GROUP BY level, environment, outcome",
    ),
    (
        "flowguard_incidents_total",
        "Incidents in the shared record by source, Monitor detector and status.",
        "SELECT json_extract(body, '$.source') AS source,"
        " COALESCE(json_extract(body, '$.detector'), '') AS detector, status,"
        " COUNT(*) AS value FROM incidents GROUP BY source, detector, status",
    ),
)
SAFETY = (
    (
        "flowguard_executed_without_required_approval",
        "Proposals executed although the gate asked a human and none approved (must be 0).",
        "SELECT COUNT(DISTINCT e.proposal_id) AS value FROM executions e"
        " JOIN gate_decisions g ON g.proposal_id = e.proposal_id WHERE g.outcome = 'ask_human'"
        " AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.proposal_id = e.proposal_id"
        " AND a.verdict = 'approved')",
    ),
    (
        "flowguard_executed_after_reject",
        "Proposals executed although the gate rejected them (must be 0).",
        "SELECT COUNT(DISTINCT e.proposal_id) AS value FROM executions e"
        " JOIN gate_decisions g ON g.proposal_id = e.proposal_id WHERE g.outcome = 'reject'",
    ),
)
NAMES = (
    {n for n, _, _ in COUNTS}
    | {n for n, _, _ in SAFETY}
    | {"flowguard_approvals_per_incident", "flowguard_audit_up"}
)


def escape(value) -> str:
    """A label value in the text format: backslash, double quote and newline escaped."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def sample(name: str, labels: dict, value: float) -> str:
    inner = ",".join(f'{k}="{escape(v)}"' for k, v in labels.items())
    return f"{name}{{{inner}}} {float(value):g}" if inner else f"{name} {float(value):g}"


def _rows(db: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    try:
        return db.execute(sql).fetchall()
    except sqlite3.OperationalError:  # a table not created yet (no incident record, ...)
        return []


def render(path: Path | str | None = None) -> str:
    path = Path(path or os.environ.get("FLOWGUARD_AUDIT_DB") or DEFAULT_PATH)
    lines = ["# HELP flowguard_audit_up 1 when the audit log could be read.",
             "# TYPE flowguard_audit_up gauge"]  # fmt: skip
    if not path.is_file():
        return "\n".join([*lines, "flowguard_audit_up 0", ""])
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        lines.append("flowguard_audit_up 1")
        for name, text, sql in COUNTS:
            lines += [f"# HELP {name} {text}", f"# TYPE {name} counter"]
            for r in _rows(db, sql):
                # a sqlite3.Row iterates its values, so its keys need .keys()
                keys = [k for k in r.keys() if k != "value"]  # noqa: SIM118
                labels = {k: (r[k] if r[k] is not None else "") for k in keys}
                lines.append(sample(name, labels, r["value"]))
        for name, text, sql in SAFETY:
            rows = _rows(db, sql)
            lines += [f"# HELP {name} {text}", f"# TYPE {name} gauge"]
            lines.append(sample(name, {}, rows[0]["value"] if rows else 0))
        per_level: dict[str, list[int]] = defaultdict(list)
        for r in _rows(
            db,
            "SELECT p.level AS level, p.incident_id, COUNT(a.id) AS n FROM proposals p"
            " LEFT JOIN approvals a ON a.proposal_id = p.id WHERE p.incident_id IS NOT NULL"
            " GROUP BY p.level, p.incident_id",
        ):
            per_level[r["level"]].append(r["n"])
        lines += [
            "# HELP flowguard_approvals_per_incident Mean human answers per incident (D11).",
            "# TYPE flowguard_approvals_per_incident gauge",
        ]
        for level, counts in sorted(per_level.items()):
            lines.append(
                sample(
                    "flowguard_approvals_per_incident", {"level": level}, statistics.mean(counts)
                )
            )
    return "\n".join([*lines, ""])


def serve(host: str = "127.0.0.1", port: int = PORT, path: Path | str | None = None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            if self.path.split("?")[0] != "/metrics":
                self.send_error(404)
                return
            body = render(path).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    server = serve(args.host, args.port)
    print(f"serving http://{args.host}:{server.server_address[1]}/metrics", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
