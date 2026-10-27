"""L0 baselines that are code, not configuration.

DSO2 (D3): a regex root-cause classifier for failed CI logs and a fixed class -> action
playbook. The rules are written from the class definitions and common CI error messages, frozen
before the corpus is scored, and never tuned on it (fairness rule). A log matches the first
class, in the order below, that has a matching line; no match is "unclear" and goes to a human.

The order encodes precedence when a log shows several symptoms: an unreachable host or a killed
container explains everything after it, a wrong toolchain explains a failed module download,
and a failed test is code unless it shows a timing symptom.
"""

from __future__ import annotations

import re

RULES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (cls, re.compile(pattern, re.IGNORECASE))
    for cls, pattern in (
        (
            "infrastructure",
            r"no such host|dial tcp|i/o timeout|connection (refused|reset|timed out)"
            r"|tls handshake timeout|temporary failure in name resolution"
            r"|operation timed out|curl: \(28\)|503 service unavailable|no space left on device"
            r"|oomkilled|out of memory|cannot allocate memory|exit code 137",
        ),
        (
            "environment",
            r"go\.mod requires go >=|toolchain|gcc.*not found|c compiler"
            r"|inconsistent vendoring|-mod=vendor|vendor directory"
            r"|goproxy=off|module lookup disabled"
            r"|manifest unknown|: not found: manifest|failed to resolve source metadata"
            r"|command not found|unsupported (go|python|node) version",
        ),
        (
            "dependency",
            r"checksum mismatch|missing go\.sum entry|unknown revision|invalid version"
            r"|cannot find module|module .* found .* but does not contain|no matching versions"
            r"|reading .*(404|410)|version .* not found|has post-v1 module path"
            r"|could not resolve dependencies|npm err! 404|no matching distribution",
        ),
        (
            "flaky_test",
            r"--- fail:.*|\btimeout\b.*test|deadline exceeded|still waiting|within \d"
            r"|after \d+(\.\d+)?m?s",
        ),
        (
            "code",
            r"--- fail:|undefined:|too many arguments|not enough arguments|cannot use"
            r"|syntax error|declared and not used|result of .* not used|panic:"
            r"|assertion failed|expected .* got",
        ),
    )
)

# flaky_test needs a timing symptom on top of a failed test: "--- FAIL" alone is code
TIMING = re.compile(
    r"deadline exceeded|still waiting|timed out|\btimeout\b|within \d|after \d+(\.\d+)?m?s"
    r"|ttl|grace|budget|interval",
    re.IGNORECASE,
)

# D3's fixed playbook: the action L0 takes for each class (catalog names)
PLAYBOOK = {
    "code": "escalate_to_human",
    "dependency": "pin_dependency",
    "environment": "escalate_to_human",
    "flaky_test": "retry_job",
    "infrastructure": "rerun_workflow",
    "unclear": "escalate_to_human",
}


TAIL = 200  # a CI failure shows at the end of the failing job's log


def classify(log: str) -> tuple[str, list[int]]:
    """(class or "unclear", 1-based numbers of the lines that matched it), from the last TAIL
    lines only, so earlier successful steps cannot trigger a rule."""
    lines = log.splitlines()
    first = max(0, len(lines) - TAIL)
    window = list(enumerate(lines[first:], start=first + 1))
    failed = any("--- FAIL" in line.upper() for _, line in window)
    for cls, pattern in RULES:
        hits = [n for n, line in window if pattern.search(line)]
        if cls == "flaky_test":
            timing = [n for n, line in window if TIMING.search(line)]
            if failed and hits and timing:
                return cls, timing
            continue
        if hits:
            return cls, hits
    return "unclear", []


def playbook(cls: str) -> str:
    return PLAYBOOK[cls]
