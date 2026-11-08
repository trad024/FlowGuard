"""Merge the held-out outputs into the report's one audit log, truth DB and replay file (R5).

    python evaluation/merge_heldout.py --confirm-step-11 --out evaluation/runs/report-inputs \
        --source diagnose:evaluation/runs/diagnose-heldout \
        --source dso6:evaluation/runs/dso6-heldout \
        --source "scenarios:audit=state/audit.db,truth=evaluation/runs/groundtruth.db,
replays=dso1-records.jsonl;dso3-records.jsonl"   (one argument)

A source is a folder (its audit.db, groundtruth.db and every replay file in it: <L>.jsonl,
L3-r2.jsonl, records.jsonl; never the -raw files) or explicit audit=, truth= and replays=
(semicolon-separated) paths. The held-out Diagnose outputs, the DSO6 outputs and the scenario
recordings' audit log, truth DB and replay records (monitor_replay and release_replay --records)
are the three kinds; the Diagnose calls' llm_call rows are in the scenarios' audit log already.

Refuses, before writing anything, on:
  - an id collision: a truth run id or spec id in two sources, an audit proposal or incident id
    in two sources, a scenario id with audit rows in two sources;
  - mixed development and held-out inputs: a scored spec id that is a dev spec (scenarios/dev/),
    a dev Diagnose variant (training or validation templates), "normal-traffic" (the dev
    window; the held-out one is heldout.NORMAL_TRAFFIC_ID) or a synthetic id;
  - a replay record whose scenario has no truth run, or two records with the same key.
Then: report.py --audit <out>/audit.db --truth <out>/groundtruth.db --replays <out>/replay.jsonl.
Gated like the other stage B tools (heldout_guard.require_stage_b).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation"), str(ROOT / "scenarios")]

import groundtruth  # noqa: E402
import heldout_guard  # noqa: E402
import report_scope  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402
from flowguard.incidents import IncidentRecord  # noqa: E402

AUDIT_TABLES = (
    "events", "proposals", "gate_decisions", "approvals", "executions", "refusals",
    "incidents", "evidence", "agent_requests",
)  # fmt: skip
TEXT_KEYS = {"proposals": "id", "incidents": "id"}  # kept as they are: they must not collide
SCENARIO_TABLES = ("proposals", "gate_decisions", "approvals", "executions", "refusals")


class Refused(SystemExit):
    """The inputs cannot be merged into one confirmatory report."""


def parse_source(text: str) -> dict:
    name, _, spec = text.partition(":")
    if not name or not spec:
        raise SystemExit(f"--source {text!r}: name:folder or name:audit=..,truth=..,replays=..")
    if "=" not in spec:
        folder = Path(spec)
        replays = sorted(
            p
            for p in folder.glob("*.jsonl")
            if not p.stem.endswith("-raw") and p.name != "items.jsonl"
        )
        audit = folder / "audit.db"
        return {"name": name, "audit": audit if audit.is_file() else None,
                "truth": folder / "groundtruth.db", "replays": replays}  # fmt: skip
    parts = dict(p.split("=", 1) for p in spec.split(","))
    return {"name": name, "audit": Path(parts["audit"]) if parts.get("audit") else None,
            "truth": Path(parts["truth"]) if parts.get("truth") else None,
            "replays": [Path(p) for p in parts.get("replays", "").split(";") if p]}  # fmt: skip


def rows(path: Path, sql: str) -> list[tuple]:
    if path is None or not Path(path).is_file():
        return []
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        try:
            return db.execute(sql).fetchall()
        except sqlite3.OperationalError:  # a table this source never created
            return []


def dev_ids(root: Path = ROOT) -> set[str]:
    return report_scope.dev_spec_ids(root) | report_scope.dev_diagnose_ids(root)


def check(sources: list[dict], root: Path = ROOT) -> dict:
    """Every refusal reason, before anything is written."""
    problems, runs, specs, keys, scenarios = [], {}, {}, {}, {}
    dev = dev_ids(root)
    for s in sources:
        for run_id, spec_id in rows(s["truth"], "SELECT run_id, spec_id FROM runs"):
            if run_id in runs:
                problems.append(f"truth run {run_id} in {runs[run_id]} and {s['name']}")
            runs[run_id] = s["name"]
            if spec_id in specs and specs[spec_id] != s["name"]:
                problems.append(f"spec {spec_id} in {specs[spec_id]} and {s['name']}")
            specs[spec_id] = s["name"]
            if spec_id in dev or str(spec_id).startswith("synthetic-"):
                problems.append(f"development or synthetic id among held-out inputs: {spec_id}")
        for table, col in TEXT_KEYS.items():
            for (key,) in rows(s["audit"], f"SELECT {col} FROM {table}"):
                if (table, key) in keys:
                    problems.append(f"{table} id {key} in {keys[(table, key)]} and {s['name']}")
                keys[(table, key)] = s["name"]
        seen = set()
        for table in SCENARIO_TABLES:
            seen |= {
                r[0] for r in rows(s["audit"], f"SELECT DISTINCT scenario_id FROM {table}") if r[0]
            }
        for sid in seen:
            if sid in scenarios:
                problems.append(
                    f"scenario {sid} has audit rows in {scenarios[sid]} and {s['name']}"
                )
            scenarios[sid] = s["name"]
    records, seen_keys = [], set()
    for s in sources:
        for path in s["replays"]:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                r = json.loads(line)
                key = (r.get("scenario_id"), r.get("level"), r.get("setting", "default"),
                       r.get("method", "default"), r.get("repeat", 1))  # fmt: skip
                if r.get("scenario_id") not in runs:
                    problems.append(f"replay record without a truth run: {r.get('scenario_id')}")
                if key in seen_keys:
                    problems.append(f"duplicate replay record: {key}")
                seen_keys.add(key)
                records.append(r)
    if problems:
        raise Refused("refused: " + "; ".join(dict.fromkeys(problems)))
    return {"runs": len(runs), "records": records}


def copy_table(main: sqlite3.Connection, alias: str, table: str) -> int:
    cols = [r[1] for r in main.execute(f"PRAGMA {alias}.table_info({table})")]
    if not cols:
        return 0
    if table not in TEXT_KEYS and cols[0] == "id":
        cols = cols[1:]  # integer keys are renumbered
    names = ", ".join(cols)
    cur = main.execute(f"INSERT INTO main.{table} ({names}) SELECT {names} FROM {alias}.{table}")
    return cur.rowcount


def merge(sources: list[dict], out: Path, root: Path = ROOT) -> dict:
    found = check(sources, root)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("audit.db", "groundtruth.db", "replay.jsonl"):
        if (out / name).exists():
            raise Refused(f"{out / name} exists: merge into an empty folder")
    AuditLog(out / "audit.db")
    IncidentRecord(out / "audit.db")
    groundtruth.GroundTruthDB(out / "groundtruth.db")
    counts = {"audit_rows": 0, "truth_runs": 0}
    with closing(sqlite3.connect(out / "audit.db")) as main:
        for s in sources:
            if s["audit"] is None:
                continue
            main.execute("ATTACH DATABASE ? AS src", (str(s["audit"]),))
            for table in AUDIT_TABLES:
                counts["audit_rows"] += copy_table(main, "src", table)
            main.commit()
            main.execute("DETACH DATABASE src")
    with closing(sqlite3.connect(out / "groundtruth.db")) as main:
        for s in sources:
            if s["truth"] is None or not Path(s["truth"]).is_file():
                continue
            main.execute("ATTACH DATABASE ? AS src", (str(s["truth"]),))
            counts["truth_runs"] += copy_table(main, "src", "runs")
            copy_table(main, "src", "events")
            main.commit()
            main.execute("DETACH DATABASE src")
    (out / "replay.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in found["records"]), encoding="utf-8"
    )
    return {**counts, "replay_records": len(found["records"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm-step-11", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", action="append", required=True)
    args = parser.parse_args(argv)
    heldout_guard.require_stage_b(args.confirm_step_11, "merge_heldout")
    print(json.dumps(merge([parse_source(s) for s in args.source], args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
