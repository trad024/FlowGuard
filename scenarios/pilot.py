"""Dev pilot for the L0 baseline (Step 4b): runs the stratified pilot specs and a long window
of normal traffic, resumably.

    python scenarios/pilot.py scenarios          # every pilot spec without a valid run yet
    python scenarios/pilot.py normal --hours 4   # normal traffic, no fault, recorded
    python scenarios/pilot.py mode agents         # staging's canary steps: agents or l0 (D53)
    python scenarios/pilot.py scenarios --agents --dir scenarios/dev/node-stress
                                                  # release specs under agents-mode (D54)

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
QUIET_ATTEMPTS = 3  # D50: a run started under firing alerts is invalid and retried


def done(truth: groundtruth.GroundTruthDB, mode: str = "l0") -> set[str]:
    """Specs with a valid run in this mode (agents-mode runs say so in observed, D54)."""
    agents = "json_extract(observed, '$.mode') = 'agents'"
    where = agents if mode == "agents" else f"NOT IFNULL({agents}, 0)"
    with truth._connect() as db:
        return {r[0] for r in db.execute(f"SELECT spec_id FROM runs WHERE valid = 1 AND {where}")}


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
        if runner.canary_mode(cluster.rollout()) == "agents":
            outcome = runner.pace_canary(cluster, image, 900, lambda: runner.resume(env), 0)
        else:
            outcome = runner.wait_outcome(cluster, image, 900)
    finally:
        load.stop()
    print(json.dumps({"heal": outcome}), flush=True)
    return outcome == "promoted"


def run_scenarios(specs: list[Path], mode: str = "l0") -> None:
    truth = groundtruth.GroundTruthDB.open()
    for path in specs:
        spec = runner.load_spec(path)
        if spec["id"] in done(truth, mode):
            continue
        if mode == "agents" and (spec["family"], spec["fault"]["delivery"]) != (
            "release",
            "config",
        ):
            continue  # D54: only config-delivered releases are recorded under agents-mode
        if not healthy():
            print(json.dumps({"skipped": spec["id"], "reason": "staging not healthy"}), flush=True)
            continue
        print(json.dumps({"start": spec["id"], "at": recorder.iso(datetime.now(UTC))}), flush=True)
        try:
            for attempt in range(1, QUIET_ATTEMPTS + 1):
                run = runner.Runner(spec, mode=mode)
                out = run.run()
                print(json.dumps({"done": spec["id"], "recording": out.name}), flush=True)
                if "alerts_not_quiet" not in run.observed:
                    break
                # D50: recorded as invalid; retried after another quiet wait
                print(json.dumps({"retry": spec["id"], "attempt": attempt}), flush=True)
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
    s = sub.add_parser("scenarios")
    s.add_argument("--dir", type=Path, default=PILOT, help="folder of specs (default: the pilot)")
    s.add_argument("--agents", action="store_true", help="record under agents-mode (D54)")
    n = sub.add_parser("normal")
    n.add_argument("--hours", type=float, default=4)
    m = sub.add_parser("mode")
    m.add_argument("mode", choices=("agents", "l0"))
    m.add_argument("--env", default="staging")
    args = parser.parse_args(argv)
    if args.cmd == "mode":
        runner.set_canary_mode(args.env, args.mode)
        print(json.dumps({"mode": runner.canary_mode(deployer.Cluster(args.env).rollout())}))
    elif args.cmd == "scenarios":
        run_scenarios(sorted(args.dir.glob("*.yaml")), "agents" if args.agents else "l0")
    else:
        print(run_normal(args.hours))
    return 0


if __name__ == "__main__":
    sys.exit(main())
