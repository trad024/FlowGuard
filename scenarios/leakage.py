"""Label-leakage checks for generated failures (DSO2 ground truth must stay hidden).

Generated changes (added lines, file names, commit message, branch name) must not contain a
root-cause class name or a word that gives the experiment away. Identifiers are split into
words, so `TestFlakyCache` or `flaky_test` are caught as well as plain text.

CI logs are compared with a baseline log of a normal run of the same pipeline: every log of
this repository contains the checkout path (".../FlowGuard/FlowGuard") and words like "code"
("exit code 1"), so a forbidden word is a leak only on a line that the baseline does not have.
"""

from __future__ import annotations

import re

CLASS_WORDS = {"code", "dependency", "environment", "flaky", "infrastructure"}
EXPERIMENT_WORDS = {
    "scenario",
    "flowguard",
    "simulated",
    "injected",
    "root",
    "cause",
    "ground",
    "truth",
    "label",
    "template",
}
FORBIDDEN = CLASS_WORDS | EXPERIMENT_WORDS
BRANCH = re.compile(r"scenario/r-[0-9a-f]{8,}")  # opaque run id; the prefix is the same for all


def words(text: str) -> set[str]:
    """Lower-case words, splitting camelCase, snake_case, kebab-case and paths."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    whole = re.findall(r"[A-Za-z]+", text)  # "FlowGuard" as one word, too
    return {w.lower() for w in re.findall(r"[A-Za-z]+", spaced) + whole}


def hits(text: str) -> set[str]:
    return words(text) & FORBIDDEN


def added_lines(before: str, after: str) -> list[str]:
    """Lines present in `after` that `before` does not have (order-insensitive, enough here)."""
    old = set(before.splitlines())
    return [line for line in after.splitlines() if line not in old]


def scan_change(files: dict[str, tuple[str, str]], commit_message: str, branch: str) -> list[str]:
    """Problems in a generated change. `files` maps path -> (text before, text after)."""
    problems = []
    if not BRANCH.fullmatch(branch):
        problems.append(f"branch {branch!r} is not an opaque scenario/r-<hex> id")
    if found := hits(commit_message):
        problems.append(f"commit message {commit_message!r}: {sorted(found)}")
    for path, (before, after) in files.items():
        if found := hits(path.rsplit("/", 1)[-1]):
            problems.append(f"file name {path}: {sorted(found)}")
        for line in added_lines(before, after):
            if found := hits(line):
                problems.append(f"{path}: {line.strip()!r}: {sorted(found)}")
    return problems


NOISE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?Z?|[0-9a-f]{7,64}|scenario/r-[0-9a-f]+|\d+"
)


def normalize(line: str) -> str:
    """A log line without timestamps, hashes, run ids and numbers."""
    return NOISE.sub("#", line).strip()


def scan_log(log: str, baseline: str) -> list[str]:
    """Lines of a CI log with a forbidden word that a normal run's log does not have."""
    known = {normalize(line) for line in baseline.splitlines()}
    problems = []
    for line in log.splitlines():
        if (found := hits(line)) and normalize(line) not in known:
            problems.append(f"{line.strip()[:160]!r}: {sorted(found)}")
    return problems
