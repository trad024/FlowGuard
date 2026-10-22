"""CI plan job: decides which optional jobs run and in what order (D1, D2).

Two modes:
  optimizer  read the model file committed in the repo (ci/optimizer/model.json). Until the
             Step 10 model exists it is a stub that runs every optional job in the fixed order.
  baseline   the DSO5 baseline: the fixed order, each job filtered by its standard path rules
             from ci/optional-jobs.json.

Build, unit tests and scans are not optional jobs and cannot be planned away. If the model
file is missing, unreadable or of an unknown type, the plan falls back to running everything:
a planning failure must never skip a check.

    python ci/plan.py --mode optimizer --changed-files changed.txt --github-output "$GITHUB_OUTPUT"

Standard library only: it runs on the hosted runner's own Python without installing anything.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

CI_DIR = Path(__file__).resolve().parent
JOBS_FILE = CI_DIR / "optional-jobs.json"
MODEL_FILE = CI_DIR / "optimizer" / "model.json"
MODES = ("optimizer", "baseline")


@dataclass
class Plan:
    mode: str
    model: str | None
    run: list[str]
    skipped: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def load_catalog(path: Path = JOBS_FILE) -> dict:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    order, jobs = catalog["order"], catalog["jobs"]
    if sorted(order) != sorted(jobs) or len(set(order)) != len(order):
        raise ValueError("optional-jobs.json: 'order' must list every job exactly once")
    return catalog


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def baseline_plan(catalog: dict, changed: list[str] | None) -> Plan:
    """Fixed order; a job runs when a changed file matches its paths (all, if unknown)."""
    plan = Plan(mode="baseline", model=None, run=[])
    for job in catalog["order"]:
        if changed is None or any(matches(f, catalog["jobs"][job]["paths"]) for f in changed):
            plan.run.append(job)
        else:
            plan.skipped[job] = "no changed file matches its paths"
    return plan


def optimizer_plan(catalog: dict, model_path: Path) -> Plan:
    """Apply the committed model. Only the run-all stub exists until Step 10."""
    everything = list(catalog["order"])
    try:
        model = json.loads(model_path.read_text(encoding="utf-8"))
        policy = model["policy"]["type"]
        name = model.get("model", "unnamed")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return Plan("optimizer", None, everything, warnings=[f"model unreadable ({exc}); ran all"])
    if policy == "run-all":
        return Plan("optimizer", name, everything)
    return Plan("optimizer", name, everything, warnings=[f"unknown policy {policy!r}; ran all"])


def make_plan(mode: str, changed: list[str] | None, catalog: dict, model_path: Path) -> Plan:
    if mode == "baseline":
        return baseline_plan(catalog, changed)
    if mode == "optimizer":
        return optimizer_plan(catalog, model_path)
    raise ValueError(f"mode must be one of {MODES}, got {mode!r}")


def read_changed(path: Path | None, all_changed: bool) -> list[str] | None:
    if all_changed or path is None:
        return None
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", default="optimizer", choices=MODES)
    parser.add_argument("--changed-files", type=Path, help="one changed path per line")
    parser.add_argument("--all-changed", action="store_true", help="changes unknown: all")
    parser.add_argument("--model", type=Path, default=MODEL_FILE)
    parser.add_argument("--jobs", type=Path, default=JOBS_FILE)
    parser.add_argument("--github-output", type=Path, help="append outputs for later jobs")
    args = parser.parse_args(argv)

    catalog = load_catalog(args.jobs)
    changed = read_changed(args.changed_files, args.all_changed)
    plan = make_plan(args.mode, changed, catalog, args.model)
    print(json.dumps(asdict(plan), indent=2))
    for warning in plan.warnings:
        print(f"::warning::plan: {warning}", file=sys.stderr)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as out:
            out.write(f"optional={json.dumps(plan.run)}\n")
            out.write(f"plan={json.dumps(asdict(plan), separators=(',', ':'))}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
