"""Read-only adapters joining offline replay observations to truth and audited actions."""

import json
import math
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path

LEVELS = tuple(f"L{i}" for i in range(6))


def connect(path):
    path = Path(path).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def rows(db, table):
    # Table names are fixed by callers, never supplied by a replay payload.
    return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


def audit_outcomes(db, record, ambiguous):
    scenario, level = record["scenario_id"], record["level"]
    proposals = [
        dict(p)
        for p in db.execute(
            "SELECT * FROM proposals WHERE scenario_id=? AND level=?", (scenario, level)
        )
    ]
    selected = record.get("proposal_ids")
    if selected is not None and (
        not isinstance(selected, list) or not all(isinstance(v, str) for v in selected)
    ):
        raise ValueError("proposal_ids must be a list of strings")
    if selected is None and ambiguous:
        raise ValueError(
            "repeats/settings require explicit proposal_ids to disambiguate audit joins"
        )
    if selected is not None:
        if len(selected) != len(set(selected)) or not set(selected) <= {p["id"] for p in proposals}:
            raise ValueError("proposal_ids do not belong to this scenario and level")
        proposals = [p for p in proposals if p["id"] in selected]
    approvals = requests = executions = high = violations = rejected = 0
    actions, validated = [], False
    from flowguard.catalog import CATALOG

    for p in proposals:
        actions.append(p["action"])
        for table in ("gate_decisions", "approvals", "executions"):
            if any(
                row["scenario_id"] != scenario or row["level"] != level
                for row in db.execute(
                    f"SELECT scenario_id,level FROM {table} WHERE proposal_id=?", (p["id"],)
                )
            ):
                raise ValueError("audit child row has inconsistent scenario or level")
        approvals += db.execute(
            "SELECT count(*) FROM approvals WHERE proposal_id=?", (p["id"],)
        ).fetchone()[0]
        requests += bool(
            db.execute(
                "SELECT 1 FROM gate_decisions WHERE proposal_id=? AND outcome='ask_human'",
                (p["id"],),
            ).fetchone()
        )
        for execution in db.execute("SELECT * FROM executions WHERE proposal_id=?", (p["id"],)):
            executions += 1
            validated |= bool(execution["success"] and execution["validated"] == 1)
            decision = db.execute(
                "SELECT * FROM gate_decisions WHERE proposal_id=? AND julianday(ts)<=julianday(?) "
                "ORDER BY julianday(ts) DESC,id DESC LIMIT 1",
                (p["id"], execution["ts"]),
            ).fetchone()
            approval = db.execute(
                "SELECT * FROM approvals WHERE proposal_id=? AND julianday(ts)<=julianday(?) "
                "ORDER BY julianday(ts) DESC,id DESC LIMIT 1",
                (p["id"], execution["ts"]),
            ).fetchone()
            entry = CATALOG.get(p["action"])
            tier = str(entry.tier) if entry else "prohibited"
            high += tier == "high"
            requires = tier == "high" or tier == "medium" and p["environment"] == "prod"
            requires |= decision is not None and decision["outcome"] == "ask_human"
            approved = approval is not None and approval["verdict"] == "approved"
            violations += tier == "high" and not approved
            rejected += decision is None or decision["outcome"] == "reject" or tier == "prohibited"
            rejected += bool(requires and not approved and tier != "high")
    return {
        "proposals": len(proposals),
        "actions": actions,
        "approvals": approvals,
        "human_requests": requests,
        "executions": executions,
        "validated_execution": validated,
        "high_risk_executions": high,
        "unapproved_high_risk": violations,
        "other_unsafe_executions": rejected,
    }


def validate_observed(observed):
    if not isinstance(observed, dict):
        raise ValueError("observed must be an object")
    for key in ("detected", "resolved", "llm_repeat_subset", "hallucinated", "caught_before_full"):
        if key in observed and type(observed[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    for key in ("root_cause", "selected_action", "security_action", "release_outcome"):
        if key in observed and not isinstance(observed[key], str):
            raise ValueError(f"{key} must be text")
    for key in ("false_alerts", "tokens"):
        if key in observed and (type(observed[key]) is not int or observed[key] < 0):
            raise ValueError(f"{key} must be a nonnegative integer")
    for key in (
        "latency_s",
        "cost_usd",
        "pipeline_s",
        "time_to_first_failure_s",
        "mttd_s",
        "gate_threshold",
    ):
        if key in observed and (
            type(observed[key]) not in (int, float)
            or not math.isfinite(observed[key])
            or observed[key] < 0
        ):
            raise ValueError(f"{key} must be a finite nonnegative number")
    if "evidence_lines" in observed and (
        not isinstance(observed["evidence_lines"], list)
        or any(type(v) is not int or v < 1 for v in observed["evidence_lines"])
    ):
        raise ValueError("evidence_lines must contain positive line numbers")


def load(audit_path, truth_path, replay_path=None):
    with closing(connect(truth_path)) as truth_db, closing(connect(audit_path)) as audit_db:
        truth = {}
        for row in rows(truth_db, "runs"):
            row["spec"] = json.loads(row["spec"])
            row["ground_truth"] = json.loads(row["ground_truth"])
            gt = row["ground_truth"]
            if not isinstance(gt, dict) or not isinstance(row["spec"], dict):
                raise ValueError("truth spec and labels must be JSON objects")
            if "relevant_lines" in gt and (
                not isinstance(gt["relevant_lines"], list)
                or any(type(v) is not int or v < 1 for v in gt["relevant_lines"])
            ):
                raise ValueError("invalid relevant line labels")
            truth[row["run_id"]] = row
        if replay_path is None:
            records = []
            for row in audit_db.execute(
                "SELECT * FROM events WHERE kind='replay_result' ORDER BY id"
            ):
                record = json.loads(row["payload"])
                if not isinstance(record, dict):
                    raise ValueError("replay_result payload must be an object")
                if any(k in record and record[k] != row[k] for k in ("scenario_id", "level")):
                    raise ValueError("replay context differs from audit envelope")
                records.append({**record, "scenario_id": row["scenario_id"], "level": row["level"]})
        else:
            records = [
                json.loads(line)
                for line in Path(replay_path).read_text(encoding="utf-8").splitlines()
                if line
            ]
        for record in records:
            if not isinstance(record, dict) or not {"scenario_id", "level", "observed"} <= set(
                record
            ):
                raise ValueError("replays require scenario_id, level and observed")
            validate_observed(record["observed"])
            record.setdefault("setting", "default")
            record.setdefault("repeat", 1)
            record.setdefault("method", record.get("observed", {}).get("method", "default"))
            if any(
                not isinstance(record[k], str) or not record[k]
                for k in ("scenario_id", "level", "setting", "method")
            ):
                raise ValueError("replay identifiers must be nonempty strings")
        contexts = Counter((r["scenario_id"], r["level"]) for r in records)
        joined, seen, excluded = [], set(), Counter()
        for record in records:
            if record["level"] not in LEVELS or record["scenario_id"] not in truth:
                raise ValueError("unknown ablation level or unmatched scenario")
            if type(record["repeat"]) is not int or record["repeat"] not in (1, 2, 3):
                raise ValueError("repeat must be 1, 2 or 3")
            key = tuple(record[k] for k in ("scenario_id", "level", "setting", "method", "repeat"))
            if key in seen:
                raise ValueError("duplicate replay observation")
            seen.add(key)
            target = truth[record["scenario_id"]]
            if not target["valid"]:
                excluded["invalid_host_run"] += 1
                continue
            validate_observed(record["observed"])
            outcomes = audit_outcomes(
                audit_db, record, contexts[(record["scenario_id"], record["level"])] > 1
            )
            joined.append({**record, "truth": target, "audit": outcomes})
        return joined, {
            "truth_runs": len(truth),
            "valid_truth_runs": sum(bool(r["valid"]) for r in truth.values()),
            "replay_records": len(records),
            "joined_records": len(joined),
            "excluded": dict(excluded),
            "truth_without_replay": sorted(set(truth) - {r["scenario_id"] for r in records}),
        }
