"""CI plan job: decides which optional jobs run and in what order (D1, D2).

Two modes:
  optimizer  read the portable LightGBM duration/failure model in ci/optimizer/model.json;
             apply P2, conservatively skip optional jobs and order by failure/second.
  baseline   the DSO5 baseline: the fixed order, each job filtered by its standard path rules
             from ci/optional-jobs.json.

The mode comes from, in order: an explicit --mode (the manual-run input or the repo variable
FLOWGUARD_CI_MODE), then the branch name (scenario/baseline/* or scenario/optimizer/*), then
the default, optimizer. The branch rule lets scenario runs pick a mode without a manual run,
which GitHub only offers for workflows on the default branch.

Build, unit tests and scans are not optional jobs and cannot be planned away. If the model
file is missing, unreadable or of an unknown type, the plan falls back to running everything:
a planning failure must never skip a check.

    python ci/plan.py --branch "$GITHUB_REF_NAME" --changed-files changed.txt \
        --github-output "$GITHUB_OUTPUT"

Standard library only: it runs on the hosted runner's own Python without installing anything.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path

CI_DIR = Path(__file__).resolve().parent
JOBS_FILE = CI_DIR / "optional-jobs.json"
MODEL_FILE = CI_DIR / "optimizer" / "model.json"
MODES = ("optimizer", "baseline")
DEFAULT_MODE = "optimizer"


@dataclass
class Plan:
    mode: str
    model: str | None
    run: list[str]
    skipped: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    mode_source: str = "default"
    predictions: dict = field(default_factory=dict)


def resolve_mode(explicit: str | None, branch: str | None) -> tuple[str, str]:
    """(mode, where it came from): explicit setting, then branch name, then the default."""
    if explicit:
        if explicit not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {explicit!r}")
        return explicit, "input"
    for mode in MODES:
        if (branch or "").startswith(f"scenario/{mode}/"):
            return mode, "branch"
    return DEFAULT_MODE, "default"


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


def optimizer_plan(
    catalog: dict,
    model_path: Path,
    changed: list[str] | None = None,
    stats: dict | None = None,
    security_status: str = "unknown",
) -> Plan:
    """Read a portable LightGBM policy; every invalid model falls back to running all."""
    everything = list(catalog["order"])
    try:
        model = json.loads(model_path.read_text(encoding="utf-8"))
        policy = model["policy"]["type"]
        name = model.get("model", "unnamed")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return Plan("optimizer", None, everything, warnings=[f"model unreadable ({exc}); ran all"])
    if policy == "run-all":
        return Plan("optimizer", name, everything)
    if policy == "lightgbm-json-v1":
        from optimizer.policy import apply

        try:
            run, skipped, predictions, warnings = apply(
                model, catalog, changed, stats, security_status
            )
            return Plan(
                "optimizer", name, run, skipped=skipped, predictions=predictions, warnings=warnings
            )
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError) as exc:
            return Plan("optimizer", name, everything, warnings=[f"invalid model ({exc}); ran all"])
    return Plan("optimizer", name, everything, warnings=[f"unknown policy {policy!r}; ran all"])


def make_plan(
    mode: str,
    changed: list[str] | None,
    catalog: dict,
    model_path: Path,
    stats: dict | None = None,
    security_status: str = "unknown",
) -> Plan:
    if mode == "baseline":
        return baseline_plan(catalog, changed)
    if mode == "optimizer":
        return optimizer_plan(catalog, model_path, changed, stats, security_status)
    raise ValueError(f"mode must be one of {MODES}, got {mode!r}")


def read_changed(path: Path | None, all_changed: bool) -> list[str] | None:
    if all_changed or path is None:
        return None
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", help=f"explicit mode {MODES}; empty means not set")
    parser.add_argument("--branch", help="branch name, for the scenario/<mode>/* rule")
    parser.add_argument("--changed-files", type=Path, help="one changed path per line")
    parser.add_argument("--all-changed", action="store_true", help="changes unknown: all")
    parser.add_argument("--diff-stats", type=Path, help="git diff --numstat output")
    parser.add_argument(
        "--security-status", choices=("clean", "flagged", "unknown"), default="unknown"
    )
    parser.add_argument("--model", type=Path, default=MODEL_FILE)
    parser.add_argument("--jobs", type=Path, default=JOBS_FILE)
    parser.add_argument("--github-output", type=Path, help="append outputs for later jobs")
    args = parser.parse_args(argv)

    catalog = load_catalog(args.jobs)
    changed = read_changed(args.changed_files, args.all_changed)
    mode, source = resolve_mode(args.mode, args.branch)
    stats = None
    if args.diff_stats:
        from optimizer.features import parse_numstat

        with suppress(OSError):  # unknown size disables predictive skipping
            stats = parse_numstat(args.diff_stats.read_text(encoding="utf-8"))
    plan = make_plan(mode, changed, catalog, args.model, stats, args.security_status)
    plan.mode_source = source
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
