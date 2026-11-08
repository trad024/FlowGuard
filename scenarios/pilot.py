"""Dev pilot for the L0 baseline (Step 4b): runs the stratified pilot specs and a long window
of normal traffic, resumably.

    python scenarios/pilot.py scenarios          # every pilot spec without a valid run yet
    python scenarios/pilot.py normal --hours 4   # normal traffic, no fault, recorded
    python scenarios/pilot.py mode agents         # staging's canary steps: agents or l0 (D53)
    python scenarios/pilot.py scenarios --agents --dir scenarios/dev/node-stress
                                                  # release specs under agents-mode (D54)
    python scenarios/pilot.py heldout --confirm-step-11
                                                  # Step 11: the held-out set, in run order
    python scenarios/pilot.py heldout --confirm-step-11 --share A
                                                  # one laptop's share of it (D75)

Held-out driver (Step 11, D65): the specs of scenarios/heldout/ run in the order of its
run-order.txt, each family in its own mode:
    release   recorded under the agents-mode canary (D54); L0 is replayed offline
    ci        live at L5 with the live executor (D64); no cluster
    pipeline  on a branch; a run whose CI succeeds is deployed by the deployer, so staging's
              Rollout is put in L0 mode (its own analysis) first
    security  the same: commit delivery, deployed under the L0 Rollout
    runtime   under the L0 Rollout: a runtime fault deploys nothing, but the runner records it
              in L0 mode and refuses to start unless the Rollout's mode matches (healthy_stable)
It refuses without --confirm-step-11, the frozen state (pre-registration frozen, eval-freeze
and diagnose-freeze unchanged) and the committed seed. It is resumable: a spec with a valid
recorded run (in the family's mode, with its recording folder present) is skipped; a run
that failed, was interrupted or was recorded invalid is run again (never dropped), up to
QUIET_ATTEMPTS when alerts are not quiet.

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


# ---------------------------------------------------------------- held-out driver (J1)


HELDOUT = ROOT / "scenarios" / "heldout"
# Staging's canary mode per family; None: leave it as it is (see the module docstring)
CANARY_MODE = {"release": "agents", "pipeline": "l0", "security": "l0", "runtime": "l0", "ci": None}
RUNNER_MODE = {"release": "agents"}  # every other family records with the default runner mode


def require_heldout(confirmed: bool) -> None:
    """Step 11 only: --confirm-step-11, the frozen state and the committed seed."""
    if not confirmed:
        raise SystemExit("held-out scenarios run only at Step 11 (--confirm-step-11)")
    import heldout

    heldout.frozen_agents()  # eval-freeze, agents/ unchanged since
    heldout.diagnose_frozen()  # pre-registration frozen, Diagnose and artifacts unchanged
    heldout.checked_seed()


def run_order(folder: Path = HELDOUT) -> list[Path]:
    """The specs in run-order.txt's order; every listed id must have its spec."""
    ids = [line.strip() for line in (folder / "run-order.txt").read_text().splitlines()]
    paths = [folder / f"{i}.yaml" for i in ids if i]
    if missing := [p.name for p in paths if not p.is_file()]:
        raise SystemExit(f"run-order.txt names specs that are missing: {missing[:5]}")
    return paths


# D75: the run order is split across three laptops by a fixed rule, before any recording.
# Share A (this laptop: GitHub and the deployer) takes every commit-delivered family; release
# and runtime specs alternate between B (1st, 3rd, ...) and C (2nd, 4th, ...) in run order.
SHARES = ("A", "B", "C")
SHARE_A_FAMILIES = ("ci", "pipeline", "security")
SHARE_BC_FAMILIES = ("release", "runtime")


def share(paths: list[Path], name: str) -> list[Path]:
    """The specs of one share, in run-order sequence."""
    if name not in SHARES:
        raise SystemExit(f"unknown share {name!r}: one of {', '.join(SHARES)}")
    out, nth = [], 0
    for path in paths:
        family = runner.load_spec(path)["family"]
        if family in SHARE_A_FAMILIES:
            owner = "A"
        elif family in SHARE_BC_FAMILIES:
            owner = "B" if nth % 2 == 0 else "C"
            nth += 1
        else:
            raise SystemExit(f"{path.name}: family {family!r} belongs to no share")
        if owner == name:
            out.append(path)
    return out


def recorded(truth: groundtruth.GroundTruthDB, spec_id: str, family: str) -> bool:
    """A valid run of the spec in its family's mode, with its recording folder present."""
    with truth._connect() as db:
        rows = db.execute(
            "SELECT observed, recording FROM runs WHERE spec_id = ? AND valid = 1", (spec_id,)
        ).fetchall()
    for observed, folder in rows:
        mode = (json.loads(observed or "{}") or {}).get("mode")
        if (mode == "agents") != (RUNNER_MODE.get(family) == "agents"):
            continue
        if folder and Path(folder).is_dir():
            return True
    return False


def runner_for(spec: dict, recordings: Path | None, ci_agents: str = "live"):
    family = spec["family"]
    if family == "ci":
        return runner.Runner(spec, ci_agents=ci_agents, recordings=recordings)
    return runner.Runner(spec, mode=RUNNER_MODE.get(family, "l0"), recordings=recordings)


def run_heldout(
    paths: list[Path],
    recordings: Path | None = None,
    ci_agents: str = "live",
    truth: groundtruth.GroundTruthDB | None = None,
    set_mode=None,
    say=print,
) -> list[dict]:
    """Each spec in order: skipped if validly recorded, else run (alerts not quiet: retried)."""
    truth = truth or groundtruth.GroundTruthDB.open()
    set_mode = set_mode or (lambda mode: runner.set_canary_mode("staging", mode))
    out, current = [], None
    for path in paths:
        spec = runner.load_spec(path)
        family = spec["family"]
        if recorded(truth, spec["id"], family):
            out.append({"spec": spec["id"], "skipped": "valid run recorded"})
            continue
        if (mode := CANARY_MODE[family]) and mode != current:
            set_mode(mode)
            current = mode
        started = time.time()
        line = {"spec": spec["id"], "family": family, "canary_mode": current}
        try:
            for attempt in range(1, QUIET_ATTEMPTS + 1):
                run = runner_for(spec, recordings, ci_agents)
                folder = run.run()
                line.update(recording=Path(folder).name, attempt=attempt)
                if "alerts_not_quiet" not in run.observed:
                    break
        except Exception as exc:  # recorded as failed; run again on the next pass
            line["failed"] = str(exc)[:300]
        line["seconds"] = round(time.time() - started)
        say(json.dumps(line))
        out.append(line)
    return out


def run_normal(hours: float, env: str = "staging", spec_id: str = "normal-traffic") -> Path:
    """Load with no fault for `hours`, then record the window (held out: heldout's id)."""
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
    spec = {"id": spec_id, "family": "normal", "hours": hours, "environment": env}
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
    n.add_argument("--heldout", action="store_true", help="Step 11: the held-out window")
    n.add_argument("--confirm-step-11", action="store_true")
    h = sub.add_parser("heldout", help="Step 11: the held-out specs in run order")
    h.add_argument("--confirm-step-11", action="store_true")
    h.add_argument("--share", help="D75: run only share A, B or C of the run order")
    m = sub.add_parser("mode")
    m.add_argument("mode", choices=("agents", "l0"))
    m.add_argument("--env", default="staging")
    args = parser.parse_args(argv)
    if args.cmd == "heldout":
        require_heldout(args.confirm_step_11)
        paths = run_order()
        if args.share is not None:
            paths = share(paths, args.share)
            print(json.dumps({"share": args.share, "specs": len(paths)}), flush=True)
        run_heldout(paths)
        return 0
    if args.cmd == "mode":
        runner.set_canary_mode(args.env, args.mode)
        print(json.dumps({"mode": runner.canary_mode(deployer.Cluster(args.env).rollout())}))
    elif args.cmd == "scenarios":
        run_scenarios(sorted(args.dir.glob("*.yaml")), "agents" if args.agents else "l0")
    elif args.heldout:
        require_heldout(args.confirm_step_11)
        import heldout

        print(run_normal(args.hours, spec_id=heldout.NORMAL_TRAFFIC_ID))
    else:
        print(run_normal(args.hours))
    return 0


if __name__ == "__main__":
    sys.exit(main())
