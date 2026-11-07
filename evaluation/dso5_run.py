"""DSO5 on the held-out commit sequence (Step 11; D69 stage B): the same 30 commits through
app-ci in both plan modes.

    python evaluation/dso5_run.py plan --confirm-step-11          # dry run: prints, pushes nothing
    python evaluation/dso5_run.py run  --confirm-step-11          # live: pushes and waits for CI

Reads scenarios/heldout/dso5-sequence.yaml (written by `heldout.py generate`; its schema is
heldout.dso5_sequence: index, change_type and the file, module or template the change needs).
Each plan mode gets its own branch, scenario/baseline/dso5-<tag> and
scenario/optimizer/dso5-<tag>: ci/plan.py takes the mode from the branch name, and the deployer
never deploys these branches (it releases only dev, or a scenario run's own branch, which this
runner never hands it). Both branches start from origin/dev and get the sequence's commits in
order, one push per commit, in a separate git worktree (the main checkout is never switched).

Per commit: docs_only, app_code, app_test and ci_config append one comment line to the drawn
file; failing applies the drawn test-split template (scenarios/ci_failures.py); dependency_bump
needs a target version the generator does not record, so the live run refuses it until a rule
is decided (the dry run shows it). After each push the app-ci run of the commit is awaited and
recorded: conclusion, run duration, time to the first failed job, the jobs and the plan's
skipped jobs. A docs-only commit starts no run (app-ci's paths filters), recorded as such.

Refuses unless --confirm-step-11, eval-freeze, the Diagnose freeze and the committed seed hold.
Resumable: commits already recorded for a mode are not made again; the branch continues.
Output: evaluation/runs/dso5-heldout/runs.jsonl, one line per (mode, commit).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation"), str(ROOT / "scenarios"),
                str(ROOT / "ci")]  # fmt: skip

import heldout_guard  # noqa: E402

SEQUENCE = ROOT / "scenarios" / "heldout" / "dso5-sequence.yaml"
OUT = ROOT / "evaluation" / "runs" / "dso5-heldout"
MODES = ("baseline", "optimizer")
COMMENT = {".go": "// ", ".md": "<!-- ", ".yml": "# ", ".yaml": "# "}
CLOSE = {".md": " -->"}


class Unresolved(RuntimeError):
    """A change the live run cannot make yet (dependency_bump has no target version)."""


def branch(mode: str, tag: str) -> str:
    if mode not in MODES:
        raise ValueError(f"unknown plan mode {mode!r}")
    return f"scenario/{mode}/dso5-{tag}"


def describe(item: dict) -> dict:
    """What a commit changes, for the dry run and the record."""
    kind = item["change_type"]
    target = item.get("file") or item.get("module") or item.get("template")
    return {"index": item["index"], "change_type": kind, "target": target}


def apply_change(root: Path, item: dict, rng=None) -> tuple[list[str], str]:
    """Write one commit's change into a checkout: (files changed, commit message)."""
    kind = item["change_type"]
    if kind in ("docs_only", "app_code", "app_test", "ci_config"):
        rel = item["file"]
        path = root / rel
        suffix = path.suffix
        line = f"{COMMENT.get(suffix, '# ')}note {item['index']}{CLOSE.get(suffix, '')}\n"
        text = path.read_text(encoding="utf-8")
        path.write_text(text + ("" if text.endswith("\n") else "\n") + line, encoding="utf-8",
                        newline="\n")  # fmt: skip
        return [rel], f"Update {Path(rel).name}"
    if kind == "failing":
        import random

        import ci_failures

        kind_name = item["template"].split("-")[0]
        template = ci_failures.pick(kind_name, rng or random.Random(item["index"]),
                                    item["template"])  # fmt: skip
        return template.apply(root), template.commit_message
    if kind == "dependency_bump":
        raise Unresolved(
            f"commit {item['index']}: dependency_bump of {item['module']} needs a target version "
            "the generator does not record; decide the rule before the live run"
        )
    raise ValueError(f"unknown change type {kind!r}")


def plan(sequence: list[dict], tag: str) -> list[dict]:
    return [
        {"mode": mode, "branch": branch(mode, tag), **describe(item)}
        for mode in MODES
        for item in sorted(sequence, key=lambda i: i["index"])
    ]


def recorded(out: Path) -> dict[str, set[int]]:
    path = out / "runs.jsonl"
    done: dict[str, set[int]] = {m: set() for m in MODES}
    if path.is_file():
        for line in path.read_text().splitlines():
            if line:
                row = json.loads(line)
                done[row["mode"]].add(row["index"])
    return done


def ts(value: str | None) -> float | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def summarize(run: dict | None, jobs: list[dict]) -> dict:
    """Pipeline duration and time to the first failed job, from GitHub's run and jobs."""
    if run is None:
        return {"ci": "no run"}
    start = ts(run.get("run_started_at") or run.get("created_at"))
    end = ts(run.get("updated_at"))
    failed = [ts(j.get("completed_at")) for j in jobs if j.get("conclusion") == "failure"]
    failed = [t for t in failed if t is not None]
    return {
        "ci": run.get("conclusion"),
        "run_id": run.get("id"),
        "pipeline_s": round(end - start, 1) if start and end else None,
        "time_to_first_failure_s": round(min(failed) - start, 1) if failed and start else None,
        "jobs": {j["name"]: j.get("conclusion") for j in jobs},
    }


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()  # fmt: skip


def run_live(
    sequence: list[dict],
    tag: str,
    out: Path,
    wait_ci: Callable[[str], tuple[dict | None, list[dict]]],
    git_run=git,
    root: Path = ROOT,
) -> int:
    """Both modes, commit by commit, in a worktree; each commit pushed and its CI recorded."""
    out.mkdir(parents=True, exist_ok=True)
    done, n = recorded(out), 0
    for mode in MODES:
        todo = [
            i for i in sorted(sequence, key=lambda i: i["index"]) if i["index"] not in done[mode]
        ]
        if not todo:
            continue
        name = branch(mode, tag)
        with tempfile.TemporaryDirectory(prefix="dso5-") as tmp:
            work = Path(tmp) / "wt"
            git_run(root, "fetch", "-q", "origin")
            exists = git_run(root, "ls-remote", "origin", f"refs/heads/{name}")
            start = f"origin/{name}" if exists else "origin/dev"
            if exists:
                git_run(root, "fetch", "-q", "origin", name)
            git_run(root, "worktree", "add", "-q", "-B", f"dso5-{mode}-{tag}", str(work), start)
            try:
                for item in todo:
                    files, message = apply_change(work, item)
                    git_run(work, "add", *files)
                    git_run(work, "commit", "-q", "-m", message)
                    sha = git_run(work, "rev-parse", "HEAD")
                    git_run(work, "push", "-q", "origin", f"HEAD:refs/heads/{name}")
                    ci_run, jobs = wait_ci(sha)
                    row = {"mode": mode, "branch": name, **describe(item), "sha": sha,
                           **summarize(ci_run, jobs)}  # fmt: skip
                    with (out / "runs.jsonl").open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row) + "\n")
                    n += 1
            finally:
                git_run(root, "worktree", "remove", "--force", str(work))
    return n


def github_wait(timeout_s: int = 1800, no_run_s: int = 180, sleep=None, clock=None):
    """The app-ci run of a commit and its jobs once it completes. No run within `no_run_s`
    (a docs-only commit: app-ci's paths filters skip it) is recorded as no run."""
    import time

    import deployer

    sleep, clock = sleep or time.sleep, clock or time.monotonic
    cfg = deployer.load_config(ROOT / ".env")
    gh = deployer.GitHub(cfg)

    def wait(sha: str):
        start = clock()
        while clock() - start < timeout_s:
            try:
                run = gh.run_for_commit(sha)
            except OSError:  # a network hiccup: try again
                run = None
            if run is None and clock() - start >= no_run_s:
                return None, []
            if run is not None and run.get("status") == "completed":
                jobs = gh.get(f"/repos/{cfg.repo}/actions/runs/{run['id']}/jobs?per_page=50")
                return run, jobs.get("jobs", [])
            sleep(15)
        raise TimeoutError(f"app-ci on {sha[:12]} did not finish within {timeout_s} s")

    return wait


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cmd", choices=("plan", "run"))
    parser.add_argument("--confirm-step-11", action="store_true")
    parser.add_argument("--sequence", type=Path, default=SEQUENCE)
    parser.add_argument("--tag", default="heldout")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    heldout_guard.require_stage_b(args.confirm_step_11, "dso5_run")
    sequence = yaml.safe_load(args.sequence.read_text(encoding="utf-8"))
    if args.cmd == "plan":
        for step in plan(sequence, args.tag):
            print(json.dumps(step))
        return 0
    unresolved = [i["index"] for i in sequence if i["change_type"] == "dependency_bump"]
    if unresolved:
        raise SystemExit(f"dependency_bump commits {unresolved} have no target version rule yet")
    print(json.dumps({"commits_run": run_live(sequence, args.tag, args.out, github_wait())}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
