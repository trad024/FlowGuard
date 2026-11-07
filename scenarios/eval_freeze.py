"""The checks before the eval-freeze tag (D39, D65), run by scripts/eval-freeze.ps1 and .sh.

    python scenarios/eval_freeze.py check     # refuses, with every reason, unless ready
    python scenarios/eval_freeze.py check --tag harness-diagnose-freeze     # D69 stage A
    python scenarios/eval_freeze.py lanes     # the LLM lanes in .env, with no LLM call

`check` refuses unless: the checkout is on dev with a clean working tree and in step with
origin/dev, no eval-freeze tag exists yet, and the Diagnose freeze holds (pre-registration
frozen, Diagnose code and artifacts unchanged since diagnose-freeze). The scripts then run the
full test suite and, only if it passes, create the tag locally and print the push command; they
never push. `lanes` counts the lanes the LLM runs would use (GROQ_KEY_n, then LLM_API_KEY;
identical values count once) and checks each key's format, printing names only: it cannot tell
whether two keys belong to one account, which needs a call (flowguard.lanes.independent).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scenarios"), str(ROOT / "ci"), str(ROOT / "agents")]

import heldout  # noqa: E402
from flowguard import lanes  # noqa: E402

TAG = heldout.FREEZE_TAG
KEY_FORMAT = re.compile(r"gsk_[A-Za-z0-9]{20,}")  # Groq API keys


def git(*args: str, root: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def problems(
    root: Path = ROOT, run=git, diagnose_frozen=heldout.diagnose_frozen, tag: str = TAG
) -> list[str]:
    """Every reason not to tag `tag` now (empty: ready for the test suite and the tag)."""
    out = []
    branch = run("rev-parse", "--abbrev-ref", "HEAD", root=root).stdout.strip()
    if branch != "dev":
        out.append(f"on {branch or 'no branch'}, not dev")
    if run("status", "--porcelain", root=root).stdout.strip():
        out.append("the working tree is not clean")
    head = run("rev-parse", "HEAD", root=root).stdout.strip()
    remote = run("rev-parse", "origin/dev", root=root).stdout.strip()
    if head != remote:
        out.append("dev is not in step with origin/dev (push or pull first)")
    if run("rev-parse", "-q", "--verify", f"refs/tags/{tag}", root=root).returncode == 0:
        out.append(f"{tag} already exists: it is created once")
    try:
        diagnose_frozen(root)
    except SystemExit as exc:
        out.append(f"Diagnose freeze: {exc}")
    return out


def lane_report(env: dict[str, str]) -> dict:
    """Lane names, their count and malformed keys; never a key value."""
    found, notes = lanes.candidates(env)
    bad = [lane.name for lane in found if not KEY_FORMAT.fullmatch(lane.key)]
    return {"lanes": [lane.name for lane in found], "count": len(found), "malformed": bad,
            "notes": notes}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check")
    check.add_argument("--tag", choices=(TAG, heldout.HARNESS_DIAGNOSE_TAG), default=TAG)
    lane = sub.add_parser("lanes")
    lane.add_argument("--expect", type=int, help="fail unless this many lanes are found")
    args = parser.parse_args(argv)
    if args.cmd == "check":
        if found := problems(tag=args.tag):
            for p in found:
                print(f"refused: {p}")
            return 1
        print(f"ready: dev is clean, in step with origin, {args.tag} absent, Diagnose freeze holds")
        return 0
    report = lane_report(lanes.read_env())
    print(f"lanes ({report['count']}): {', '.join(report['lanes'])}")
    for note in report["notes"]:
        print(f"note: {note}")
    if report["malformed"]:
        print(f"malformed keys: {', '.join(report['malformed'])}")
    print("not checked without a call: whether two keys share one account")
    ok = not report["malformed"] and (args.expect is None or report["count"] == args.expect)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
