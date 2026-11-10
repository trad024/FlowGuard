"""D77: held-out-only copies of one laptop's truth DB and audit log, the inputs block 11 merges.

    python evaluation/heldout_inputs.py --confirm-step-11 --truth evaluation/runs/groundtruth.db \
        --audit state/audit.db --list state/heldout-recordings-A.txt --out evaluation/runs/heldout-A
    (add --check to count only and write nothing)

A laptop's truth DB and audit log also hold development rows (dev specs, the dev window, early
live runs), which merge_heldout.py refuses and report_scope.py would call unknown. This script
opens both read-only, copies them whole (SQLite backup) into an empty folder and deletes from
the copies every row that is not held-out. The originals are never written.

Kept, and nothing else:
  - truth runs whose spec_id is a run-order id or heldout.NORMAL_TRAFFIC_ID and whose
    recorded_at is on or after CUT (valid and invalid alike: the report counts re-runs from the
    invalid ones), and the truth events of those run ids. The recording folder is used only to
    check --list; ground_truth is never read;
  - audit rows whose scenario_id is a kept run id; gate decisions, approvals, executions and
    refusals that name a kept proposal (an approval may carry no scenario id); llm_call events
    without a scenario_id whose cache_key is in a held-out Diagnose raw file (the Diagnose calls
    carry no scenario id); incidents named by a kept proposal, and their evidence and agent
    requests.
Refuses, before writing anything, unless every folder of --list maps to exactly one kept valid
run, every kept valid run's folder is listed, every held-out run and kept audit row has an ISO
date, every kept audit row is dated on or after CUT, and no row linking a kept proposal names
another scenario. Dropped rows are counted, not dropped silently: held-out runs dated before
CUT, audit rows dated on or after CUT by table (events also by kind), undated audit rows.
Also writes recordings-<family>.txt (the listed folders by truth family) for the replays.
Prints counts only.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation"), str(ROOT / "scenarios")]

import heldout_guard  # noqa: E402

CUT = "2026-11-08"  # Step 11's first held-out output (D77)
RUN_ORDER = ROOT / "scenarios" / "heldout" / "run-order.txt"
RAW = ROOT / "evaluation" / "runs" / "diagnose-heldout"
SCENARIO_TABLES = ("proposals", "gate_decisions", "approvals", "executions", "refusals")
ISO_DATE = re.compile(r"\d{4}-\d\d-\d\d([T ]|$)")


class Refused(SystemExit):
    """The copies would not be held-out-only inputs."""


def folder_name(recording) -> str | None:
    return PurePosixPath(str(recording).replace("\\", "/")).name if recording else None


def heldout_ids(run_order: Path = RUN_ORDER) -> set[str]:
    import heldout

    ids = {line.strip() for line in run_order.read_text(encoding="utf-8").splitlines()}
    return (ids - {""}) | {heldout.NORMAL_TRAFFIC_ID}


def diagnose_cache_keys(raw_dir: Path = RAW) -> set[str]:
    """Cache keys of the held-out Diagnose calls (every *-raw.jsonl record's cache_key)."""
    keys = set()
    for path in sorted(raw_dir.glob("*-raw.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and (key := json.loads(line).get("cache_key")):
                keys.add(key)
    return keys


def _iso(value) -> bool:
    """A date string this rule can compare with CUT (YYYY-MM-DD first), not NULL."""
    return isinstance(value, str) and ISO_DATE.match(value) is not None


def read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def plan(
    truth: Path, audit: Path, listed: list[str], ids: set[str], keys: set[str], cut: str = CUT
) -> dict:
    """Which rows stay, every refusal reason, and the counts; reads ids, dates and links only."""
    problems = []
    with closing(read_only(truth)) as db:
        runs = db.execute("SELECT run_id, spec_id, family, recording, recorded_at, valid FROM runs")
        runs = runs.fetchall()
        has_events = "events" in tables(db)
        truth_events = db.execute("SELECT id, run_id FROM events").fetchall() if has_events else []
    heldout_runs = [r for r in runs if r[1] in ids]
    if any(not _iso(r[4]) for r in heldout_runs):
        problems.append("a held-out run has no ISO recorded_at")
    kept = {r[0] for r in heldout_runs if _iso(r[4]) and r[4] >= cut}
    by_folder = defaultdict(list)
    for run_id, _, family, rec, _, valid in runs:
        by_folder[folder_name(rec)].append((run_id, family, valid))
    names = [folder_name(p) for p in listed]
    families = {}
    for path, name in zip(listed, names, strict=True):
        found = by_folder.get(name, [])
        if len(found) != 1 or found[0][0] not in kept or not found[0][2]:
            problems.append("a listed folder has no single kept valid run")
        else:
            families[path] = found[0][1] or "unknown"
    if len(set(names)) != len(names):
        problems.append("a folder is listed twice")
    listed_runs = {by_folder[n][0][0] for n in names if len(by_folder.get(n, [])) == 1}
    if {r[0] for r in runs if r[0] in kept and r[5]} - listed_runs:
        problems.append("a kept valid run's folder is not listed")
    keep: dict[str, set] = {"runs": kept, "truth_events": {i for i, r in truth_events if r in kept}}
    counts = {
        "truth_runs": [len(runs), len(kept)],
        "truth_events": [len(truth_events), len(keep["truth_events"])],
        "heldout_runs_before_cut_dropped": sum(
            1 for r in heldout_runs if _iso(r[4]) and r[4] < cut
        ),
    }
    dropped: dict = {}  # rows dated on or after CUT that are dropped, and undated dropped rows

    def check_dates(table: str, rows: list, ok: set) -> None:
        for r in rows:
            ts = r[1]
            if r[0] in ok:
                if not _iso(ts):
                    problems.append("a kept audit row has no ISO date")
                elif ts < cut:
                    problems.append(f"a kept audit row is dated before {cut}")
            elif not _iso(ts):
                dropped.setdefault("undated", Counter())[table] += 1
            elif ts >= cut:
                dropped.setdefault("on_or_after_cut", Counter())[table] += 1

    with closing(read_only(audit)) as db:
        present = tables(db)
        if "events" in present:
            rows = db.execute("SELECT id, ts, kind, scenario_id, payload FROM events").fetchall()
            ok = set()
            for i, _, kind, sid, payload in rows:
                if sid in kept or (
                    sid is None
                    and kind == "llm_call"
                    and json.loads(payload or "{}").get("cache_key") in keys
                ):
                    ok.add(i)
            check_dates("events", rows, ok)
            late = Counter(r[2] for r in rows if r[0] not in ok and _iso(r[1]) and r[1] >= cut)
            if late:
                counts["events_dropped_on_or_after_cut_by_kind"] = dict(sorted(late.items()))
            keep["events"], counts["events"] = ok, [len(rows), len(ok)]
        incident_ids, kept_proposals = set(), set()
        for table in SCENARIO_TABLES:
            if table not in present:
                continue
            link = "incident_id" if table == "proposals" else "proposal_id"
            rows = db.execute(f"SELECT id, ts, scenario_id, {link} FROM {table}").fetchall()
            if table == "proposals":
                ok = {r[0] for r in rows if r[2] in kept}
                kept_proposals = ok
                incident_ids = {r[3] for r in rows if r[0] in ok and r[3]}
            else:
                # by scenario id, or by a kept proposal (an approval may carry no scenario id)
                ok = {r[0] for r in rows if r[2] in kept or r[3] in kept_proposals}
                if any(r[2] is not None and r[2] not in kept for r in rows if r[0] in ok):
                    problems.append("an audit row links a kept proposal under another scenario")
            check_dates(table, rows, ok)
            keep[table], counts[table] = ok, [len(rows), len(ok)]
        for table, key in (
            ("incidents", "id"),
            ("evidence", "incident_id"),
            ("agent_requests", "incident_id"),
        ):
            if table not in present:
                continue
            rows = db.execute(f"SELECT id, {key} FROM {table}").fetchall()
            ok = {r[0] for r in rows if r[1] in incident_ids}
            keep[table], counts[table] = ok, [len(rows), len(ok)]
    for name, by_table in dropped.items():
        counts[f"audit_rows_dropped_{name}"] = dict(sorted(by_table.items()))
    return {
        "keep": keep,
        "counts": counts,
        "problems": list(dict.fromkeys(problems)),
        "families": families,
    }


def _prune(db: sqlite3.Connection, table: str, key: str, ids: set) -> None:
    db.execute("CREATE TEMP TABLE keep_ids (id PRIMARY KEY)")
    db.executemany("INSERT INTO keep_ids VALUES (?)", [(i,) for i in ids])
    db.execute(f"DELETE FROM {table} WHERE {key} NOT IN (SELECT id FROM keep_ids)")
    db.execute("DROP TABLE keep_ids")


def _copy(source: Path, target: Path) -> None:
    with closing(read_only(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def write(truth: Path, audit: Path, out: Path, found: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name in ("groundtruth.db", "audit.db"):
        if (out / name).exists():
            raise Refused(f"{out / name} exists: write into an empty folder")
    _copy(truth, out / "groundtruth.db")
    _copy(audit, out / "audit.db")
    keep = found["keep"]
    with closing(sqlite3.connect(out / "groundtruth.db")) as db:
        _prune(db, "runs", "run_id", keep["runs"])
        if "events" in tables(db):
            _prune(db, "events", "id", keep["truth_events"])
        db.commit()
        db.execute("VACUUM")
    with closing(sqlite3.connect(out / "audit.db")) as db:
        for table in ("events", *SCENARIO_TABLES, "incidents", "evidence", "agent_requests"):
            if table in keep:
                _prune(db, table, "id", keep[table])
        db.commit()
        db.execute("VACUUM")
    lists = defaultdict(list)
    for path, family in found["families"].items():
        lists[family].append(path)
    for family, paths in sorted(lists.items()):
        (out / f"recordings-{family}.txt").write_bytes(("\n".join(sorted(paths)) + "\n").encode())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm-step-11", action="store_true")
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--list", type=Path, required=True, help="the valid held-out folders")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", action="store_true", help="count only, write nothing")
    args = parser.parse_args(argv)
    heldout_guard.require_stage_b(args.confirm_step_11, "heldout_inputs")
    listed = [x for x in args.list.read_text(encoding="utf-8").splitlines() if x.strip()]
    found = plan(args.truth, args.audit, listed, heldout_ids(), diagnose_cache_keys())
    summary = {
        "listed": len(listed),
        "kept_of_total": found["counts"],
        "families": dict(Counter(found["families"].values())),
    }
    if found["problems"]:
        raise Refused("refused: " + "; ".join(found["problems"]))
    if not args.check:
        if args.out is None:
            raise SystemExit("--out is required unless --check")
        write(args.truth, args.audit, args.out, found)
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
