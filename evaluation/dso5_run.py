"""DSO5 on the held-out commit sequence (Step 11; D69 stage B): the same 30 commits through
app-ci in both plan modes.

    python evaluation/dso5_run.py plan --confirm-step-11          # dry run: prints, pushes nothing
    python evaluation/dso5_run.py run  --confirm-step-11          # live: pushes and waits for CI

Reads scenarios/heldout/dso5-sequence.yaml (written by `heldout.py generate`; its schema is
heldout.dso5_sequence: index, change_type and the file, module or template the change needs).
Each plan mode gets its own branch, scenario/baseline/dso5-<tag> and
scenario/optimizer/dso5-<tag>: ci/plan.py takes the mode from the branch name, and the deployer
never deploys these branches (it releases only dev, or a scenario run's own branch, which this
runner never hands it). Both branches start from one base commit (--base, default origin/dev,
resolved once and recorded in every row; both modes share it) and get the sequence's commits in
order, one push per commit, in a separate git worktree (the main checkout is never switched).

D76: every commit is applied to the base tree. After its app-ci run is recorded, a restore
commit whose tree is the base tree (`git commit-tree <base>^{tree} -p HEAD`, message with
[skip ci], so no workflow runs) is pushed before the next commit. A new branch is first
created by an empty [skip ci] commit on the base (`git commit-tree <base>^{tree} -p <base>`).
Each commit's CI result is then its own change's, and app-ci still sees one push per commit on
one branch (the changed files are that commit's, the first one's included; the previous scan
status is the previous measured commit's, unknown for the first, which plans as the baseline).

Per commit: docs_only, app_code, app_test and ci_config append one comment line to the drawn
file; failing applies the drawn test-split template (scenarios/ci_failures.py);
dependency_bump pins the drawn module to its pre-registered version (R4,
dso5_commit_sequence.dependency_pool) with its go.sum lines (runner.pin_module,
runner.go_sum_lines). After each push the app-ci run of the commit is awaited and recorded:
conclusion, run duration, time to the first failed job, the jobs and the plan's
skipped jobs. A docs-only commit starts no run (app-ci's paths filters), recorded as such.

Refuses unless --confirm-step-11, eval-freeze, the Diagnose freeze and the committed seed hold.
Resumable: commits already recorded for a mode are not made again; the branch continues, and a
restore left pending (the tip's tree is not the base tree) is pushed first. A commit pushed but
not recorded before a stop is made again. Refuses rows of another branch name or another base.
Output: evaluation/runs/dso5-heldout-d76/runs.jsonl (tag heldout-d76), one line per (mode,
commit). The pre-D76 run (tag heldout, evaluation/runs/dso5-heldout) is kept apart (D76).
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
TAG = "heldout-d76"  # D76: new branches; the pre-D76 tag "heldout" and its rows stay apart
OUT = ROOT / "evaluation" / "runs" / "dso5-heldout-d76"
BASE = "origin/dev"
MODES = ("baseline", "optimizer")
RESTORE = "Restore the DSO5 base tree after commit {index} [skip ci]"
START = "Start the DSO5 branch at its base [skip ci]"
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
    if item.get("version"):
        target = f"{target}@{item['version']}"
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
        if not item.get("version"):
            raise Unresolved(
                f"commit {item['index']}: dependency_bump of {item['module']} has no target "
                "version (the pre-registered pool, R4)"
            )
        import runner

        module, version = item["module"], item["version"]
        files = runner.pin_module(root, module, version, runner.go_sum_lines(module, version))
        return files, f"Bump {module.rsplit('/', 1)[-1]} to {version}"
    raise ValueError(f"unknown change type {kind!r}")


def plan(sequence: list[dict], tag: str) -> list[dict]:
    return [
        {"mode": mode, "branch": branch(mode, tag), **describe(item)}
        for mode in MODES
        for item in sorted(sequence, key=lambda i: i["index"])
    ]


def recorded(out: Path, tag: str) -> tuple[dict[str, set[int]], str | None]:
    """(indices recorded per mode, the base commit they share). Refuses a row of another
    branch name or without a base, and rows of more than one base (D76)."""
    path = out / "runs.jsonl"
    done: dict[str, set[int]] = {m: set() for m in MODES}
    bases: set[str] = set()
    if path.is_file():
        for line in path.read_text().splitlines():
            if line:
                row = json.loads(line)
                if row.get("branch") != branch(row["mode"], tag):
                    raise SystemExit(
                        f"{path}: a row of branch {row.get('branch')!r}, not of tag {tag!r}; "
                        "use another --out"
                    )
                if not row.get("base"):
                    raise SystemExit(f"{path}: a row without a base commit (pre-D76)")
                bases.add(row["base"])
                done[row["mode"]].add(row["index"])
    if len(bases) > 1:
        raise SystemExit(f"{path}: rows of {len(bases)} base commits; both modes share one")
    return done, next(iter(bases), None)


def restore(work: Path, name: str, base_tree: str, index, git_run) -> bool:
    """Push a [skip ci] commit whose tree is the base tree, unless the tip already has it."""
    if git_run(work, "rev-parse", "HEAD^{tree}") == base_tree:
        return False
    sha = git_run(work, "commit-tree", base_tree, "-p", "HEAD", "-m", RESTORE.format(index=index))
    git_run(work, "reset", "-q", "--hard", sha)
    git_run(work, "push", "-q", "origin", f"HEAD:refs/heads/{name}")
    return True


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
    base_ref: str | None = None,
) -> int:
    """Both modes, commit by commit, in a worktree; each commit pushed and its CI recorded,
    then the base tree restored (D76)."""
    out.mkdir(parents=True, exist_ok=True)
    done, base = recorded(out, tag)
    n = 0
    git_run(root, "fetch", "-q", "origin")
    wanted = git_run(root, "rev-parse", f"{base_ref or BASE}^{{commit}}")
    if base is None:
        base = wanted
    elif base_ref and wanted != base:
        raise SystemExit(f"--base {base_ref} is {wanted[:12]}; the recorded rows use {base[:12]}")
    base_tree = git_run(root, "rev-parse", f"{base}^{{tree}}")
    for mode in MODES:
        todo = [
            i for i in sorted(sequence, key=lambda i: i["index"]) if i["index"] not in done[mode]
        ]
        if not todo:
            continue
        name = branch(mode, tag)
        with tempfile.TemporaryDirectory(prefix="dso5-") as tmp:
            work = Path(tmp) / "wt"
            exists = git_run(root, "ls-remote", "origin", f"refs/heads/{name}")
            start = base
            if exists:
                git_run(root, "fetch", "-q", "origin", name)
                start = f"origin/{name}"
            git_run(root, "worktree", "add", "-q", "-B", f"dso5-{mode}-{tag}", str(work), start)
            try:
                if exists:  # a stop between a commit's CI and its restore
                    restore(work, name, base_tree, "pending (resume)", git_run)
                else:  # the branch's first push: an empty [skip ci] commit on the base, so the
                    # first measured push has a previous commit and its own changed files only
                    sha = git_run(work, "commit-tree", base_tree, "-p", base, "-m", START)
                    git_run(work, "reset", "-q", "--hard", sha)
                    git_run(work, "push", "-q", "origin", f"HEAD:refs/heads/{name}")
                for item in todo:
                    files, message = apply_change(work, item)
                    git_run(work, "add", *files)
                    git_run(work, "commit", "-q", "-m", message)
                    sha = git_run(work, "rev-parse", "HEAD")
                    git_run(work, "push", "-q", "origin", f"HEAD:refs/heads/{name}")
                    ci_run, jobs = wait_ci(sha)
                    row = {"mode": mode, "branch": name, "base": base, **describe(item),
                           "sha": sha, **summarize(ci_run, jobs)}  # fmt: skip
                    with (out / "runs.jsonl").open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row) + "\n")
                    n += 1
                    restore(work, name, base_tree, item["index"], git_run)
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
    parser.add_argument("--tag", default=TAG)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--base", help=f"base commit of both modes (default {BASE}, D76)")
    args = parser.parse_args(argv)
    heldout_guard.require_stage_b(args.confirm_step_11, "dso5_run")
    sequence = yaml.safe_load(args.sequence.read_text(encoding="utf-8"))
    if args.cmd == "plan":
        for step in plan(sequence, args.tag):
            print(json.dumps(step))
        return 0
    unresolved = [
        i["index"]
        for i in sequence
        if i["change_type"] == "dependency_bump" and not i.get("version")
    ]
    if unresolved:
        raise SystemExit(f"dependency_bump commits {unresolved} have no target version")
    n = run_live(sequence, args.tag, args.out, github_wait(), base_ref=args.base)
    print(json.dumps({"commits_run": n}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
