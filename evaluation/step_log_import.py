"""Import the side logger's Rollout steps into recordings' step logs (D54).

    python evaluation/step_log_import.py state/rollout-steps.jsonl <recordings folder> ...
        [--force]

The side logger polled the Rollouts every 5 s and wrote one JSON object per change (rollout,
namespace, step index, setWeight, paused, abort, phase, the canary and stable hashes, ts in
UTC) to state/rollout-steps.jsonl. Recordings made while it ran have no rollout-events.jsonl
(recorder.RolloutStepLog came later); this writes one into each, in the step log's format,
from the rows inside the recording's window and namespace, so release_replay.py reads them
like any other step log.

Times are when a poll saw the change, so every row is marked time_source "poll" (up to 5 s
late; the recorder's own rows are "argo", Argo's startTime and abortedAt):
    a pause starts at the first row at its step index, and only where the row before it (same
    canary) was at a lower index: a logger started mid-pause gives no start, and the replay
    falls back to its estimate for that recording
    an abort at the first row that has it
The step kinds (setWeight, pause, analysis) come from the repo's canary steps: the agents-mode
component for a recording made in agents mode, else the L0 Rollout (both put the setWeights
and the 30 s pauses at the same indexes). A recording that already has a step log keeps it
(Argo's times win) unless --force.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "evaluation")]

import l0_baseline  # noqa: E402
import monitor_replay  # noqa: E402
import recorder  # noqa: E402

PODINFO = ROOT / "infra" / "apps" / "podinfo"
ROLLOUT = "podinfo"
# The side logger's field names, and the Rollout status names it may have kept instead
FIELDS = {
    "step_index": ("step_index", "currentStepIndex", "step"),
    "set_weight": ("set_weight", "setWeight", "weight"),
    "canary_hash": ("canary_hash", "currentPodHash", "current_hash"),
    "stable_hash": ("stable_hash", "stableRS", "stable_rs"),
}


def field(row: dict, name: str):
    return next((row[k] for k in FIELDS[name] if row.get(k) is not None), None)


def canary_steps(mode: str) -> list[dict]:
    """The canary steps the repo defines for a mode (agents-mode component or L0 Rollout)."""
    if mode == "agents":
        component = yaml.safe_load((PODINFO / "agents-mode" / "kustomization.yaml").read_text())
        (op,) = yaml.safe_load(component["patches"][0]["patch"])
        return op["value"]
    docs = yaml.safe_load_all((PODINFO / "base" / "podinfo.yaml").read_text())
    rollout = next(d for d in docs if d and d.get("kind") == "Rollout")
    return rollout["spec"]["strategy"]["canary"]["steps"]


def convert(rows: list[dict], steps: list[dict]) -> list[dict]:
    """Side-logger rows (one namespace, in time order) -> step-log rows, time_source "poll"."""
    out, last = [], {}
    for row in rows:
        index = field(row, "step_index")
        index = int(index) if isinstance(index, (int, float)) else None
        canary = field(row, "canary_hash")
        step = steps[index] if index is not None and 0 <= index < len(steps) else None
        kind = next(iter(step), None) if step else None
        weight = field(row, "set_weight")
        if weight is None and index is not None:  # the last setWeight reached so far
            for s in steps[: index + 1]:
                weight = s.get("setWeight", weight)
        same = canary == last.get("canary_hash")
        pause_start = None
        if kind == "pause":
            if same and last.get("step_index") == index:
                pause_start = last.get("pause_start")  # the same pause, still under way
            elif same and last.get("step_index") is not None and last["step_index"] < index:
                pause_start = row["ts"]  # the poll that saw the pause begin
        abort = bool(row.get("abort"))
        aborted_at = (last.get("aborted_at") if same else None) or (row["ts"] if abort else None)
        view = {
            "step_index": index,
            "step": kind,
            "set_weight": weight,
            "pause_start": pause_start,
            "abort": abort,
            "aborted_at": aborted_at if abort else None,
            "phase": row.get("phase"),
            "canary_hash": canary,
            "stable_hash": field(row, "stable_hash"),
        }
        changes = recorder.step_changes(view, last)
        last = view
        if changes:
            out.append(
                {
                    "ts": row["ts"],
                    "namespace": row.get("namespace"),
                    "changes": changes,
                    "time_source": "poll",
                    **view,
                }  # fmt: skip
            )
    return out


def import_into(run: dict, rows: list[dict], force: bool = False) -> str:
    """Write one recording's step log from the side logger's rows; what was done, in words."""
    target = Path(run["recording"]) / recorder.ROLLOUT_EVENTS
    if target.is_file() and target.stat().st_size and not force:
        return "kept its own step log"
    ns = run["spec"].get("environment", "staging")
    t0, t1 = run["window"]
    mine = sorted(
        (
            r
            for r in rows
            if r.get("namespace") == ns
            and r.get("rollout", ROLLOUT) == ROLLOUT
            and t0 <= l0_baseline.ts(r["ts"]) <= t1
        ),
        key=lambda r: l0_baseline.ts(r["ts"]),
    )
    if not mine:
        return "no logged rows in its window"
    truth = json.loads((Path(run["recording"]) / "ground_truth.json").read_text())
    mode = (truth.get("observed") or {}).get("mode", "l0")
    converted = convert(mine, canary_steps(mode))
    target.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in converted))
    return f"{len(converted)} rows ({mode} steps)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("steps_log", type=Path)
    parser.add_argument("recordings", type=Path, nargs="+")
    parser.add_argument("--force", action="store_true", help="replace an existing step log")
    args = parser.parse_args(argv)
    rows = [json.loads(line) for line in args.steps_log.read_text().splitlines() if line.strip()]
    for path in args.recordings:
        for run in monitor_replay.load_runs(monitor_replay.open_recordings(path)):
            if run["family"] == "release":
                print(
                    json.dumps(
                        {"recording": run["id"], "step_log": import_into(run, rows, args.force)}
                    )
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
