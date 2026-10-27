"""Dev pilot for the L0 baseline (Step 4b): runs the stratified pilot specs and a long window
of normal traffic, resumably.

    python scenarios/pilot.py scenarios          # every pilot spec without a valid run yet
    python scenarios/pilot.py normal --hours 4   # normal traffic, no fault, recorded

A spec is skipped when the ground-truth DB already holds a valid run of it, so an interrupted
pilot continues where it stopped. The normal-traffic window is recorded like a scenario
(Prometheus series including the L0 alerts, load log), with ground truth {"normal_traffic":
true}, for DSO1's false alerts per hour.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scenarios"), str(ROOT / "ci"), str(ROOT / "evaluation")]

import deployer  # noqa: E402
import groundtruth  # noqa: E402
import recorder  # noqa: E402
import runner  # noqa: E402

PILOT = ROOT / "scenarios" / "dev" / "pilot"


def done(truth: groundtruth.GroundTruthDB) -> set[str]:
    with truth._connect() as db:
        return {r[0] for r in db.execute("SELECT spec_id FROM runs WHERE valid = 1")}


def healthy(env: str = "staging") -> bool:
    """Staging Healthy and stable; if not, one retry of its canary with load."""
    cluster = deployer.Cluster(env)
    status = cluster.rollout().get("status", {})
    if status.get("phase") == "Healthy" and status.get("stableRS") == status.get("currentPodHash"):
        return True
    image = deployer.container(cluster.rollout())["image"]
    load = deployer.LoadGenerator(env)
    load.start(f"heal-{runner.new_run_id()}")
    try:
        runner.retry_rollout(env)
        outcome = runner.wait_outcome(cluster, image, 900)
    finally:
        load.stop()
    print(json.dumps({"heal": outcome}), flush=True)
    return outcome == "promoted"


def run_scenarios(specs: list[Path]) -> None:
    truth = groundtruth.GroundTruthDB.open()
    for path in specs:
        spec = runner.load_spec(path)
        if spec["id"] in done(truth):
            continue
        if not healthy():
            print(json.dumps({"skipped": spec["id"], "reason": "staging not healthy"}), flush=True)
            continue
        print(json.dumps({"start": spec["id"], "at": recorder.iso(datetime.now(UTC))}), flush=True)
        try:
            out = runner.Runner(spec).run()
            print(json.dumps({"done": spec["id"], "recording": out.name}), flush=True)
        except Exception as exc:  # one failed scenario must not stop the pilot
            print(json.dumps({"failed": spec["id"], "error": str(exc)[:300]}), flush=True)


def run_normal(hours: float, env: str = "staging") -> Path:
    """Load with no fault for `hours`, then record the window."""
    run_id = runner.new_run_id()
    load = deployer.LoadGenerator(env)
    start = datetime.now(UTC)
    log = load.start(run_id)
    try:
        time.sleep(hours * 3600)
    finally:
        load.stop()
    end = datetime.now(UTC)
    truth = groundtruth.GroundTruthDB.open()
    gaps = runner.suspend_gaps([log])
    spec = {"id": "normal-traffic", "family": "normal", "hours": hours, "environment": env}
    return recorder.record(
        run_id,
        start,
        end,
        loadgen_files=[log],
        ground_truth={"normal_traffic": True},
        evaluation={
            "spec": spec,
            "observed": {"host_suspended": gaps} if gaps else {},
            "l0_correct": None,
            "valid": not gaps,
        },
        truth_db=truth,
        recording_id=run_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scenarios")
    n = sub.add_parser("normal")
    n.add_argument("--hours", type=float, default=4)
    args = parser.parse_args(argv)
    if args.cmd == "scenarios":
        run_scenarios(sorted(PILOT.glob("*.yaml")))
    else:
        print(run_normal(args.hours))
    return 0


if __name__ == "__main__":
    sys.exit(main())
