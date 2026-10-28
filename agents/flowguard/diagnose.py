"""Diagnose agent core (Step 7): prompt, structured answer, evidence grounding (D8).

L2 is zero-shot: the reduced log only. L3 adds up to 3 retrieved training cases and applies
grounding. Grounding is always measured (the hallucination rate is logged at L2 too) and
applied from L3 on: a diagnosis is rejected (class "unclear", action escalate_to_human) if it
quotes no line or any quoted line is not in the real log.

The answer is JSON with one of the 5 classes, a score per class (used by the conformal
calibration, D9), one fix action from Diagnose's allowed actions, and quoted evidence lines.
An action outside those is replaced by escalate_to_human and flagged; the gate still decides.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from flowguard import logreduce
from flowguard.catalog import AGENT_ACTIONS
from flowguard.contracts import AgentName

CLASSES = ("code", "dependency", "environment", "flaky_test", "infrastructure")
ALLOWED = tuple(sorted(AGENT_ACTIONS[AgentName.DIAGNOSE]))
MAX_EXAMPLES = 3
EXAMPLE_CHARS = 2_000  # each example's log, its last part (request size)
MIN_QUOTE = 8  # a shorter quote counts only as a whole line, not as part of one

DEFINITIONS = {
    "code": "a defect in the project's own source or tests (compile error, failing assertion, "
    "lint or vet finding)",
    "dependency": "a third-party module or package problem (missing, wrong or unresolvable "
    "version, checksum mismatch, incompatible API)",
    "environment": "the build environment is misconfigured (environment variables, tool or "
    "language versions, file permissions, configuration files)",
    "flaky_test": "a test that fails nondeterministically (timing, ordering, randomness, "
    "shared state), not a deterministic defect",
    "infrastructure": "the runner or a service it needs failed, not the project (network, "
    "registry, disk, out of memory, cancelled or timed-out job)",
}
ACTIONS = {
    "retry_job": "rerun only the failed job",
    "rerun_workflow": "rerun the whole workflow",
    "restart_deployment": "restart the deployed service",
    "pin_dependency": "pin a dependency to a known-good version",
    "open_fix_pr": "open a pull request that fixes the code or configuration",
    "escalate_to_human": "hand the failure to a person",
}

SYSTEM = (
    "You diagnose failed CI pipeline jobs. Classify the root cause into exactly one class:\n"
    + "".join(f"- {c}: {DEFINITIONS[c]}\n" for c in CLASSES)
    + "Propose one fix action from this list:\n"
    + "".join(f"- {a}: {ACTIONS[a]}\n" for a in ALLOWED)
    + "Each log line is prefixed with its line number as 'L<n>: '. Quote up to 5 lines that "
    "show the root cause, copied exactly from the log without the 'L<n>: ' prefix.\n"
    "Answer with one JSON object only:\n"
    '{"root_cause": "<class>", "scores": {"<class>": <probability>, ...}, '
    '"action": "<action>", "fix": "<one sentence>", "evidence": ["<line>", ...]}\n'
    "The scores cover all five classes and sum to 1."
)


@dataclass
class Example:
    """A retrieved training case (L3): its reduced log, class and fix action."""

    log: str
    root_cause: str
    action: str


def messages(log: str, examples: list[Example] = ()) -> list[dict]:
    """The chat messages for one log; `examples` only at L3 (at most 3)."""
    parts = []
    for i, ex in enumerate(list(examples)[:MAX_EXAMPLES], start=1):
        answer = json.dumps({"root_cause": ex.root_cause, "action": ex.action})
        text = ex.log[-EXAMPLE_CHARS:]
        parts.append(f"Similar past failure {i}:\n{text}\nIts diagnosis: {answer}\n")
    reduced = logreduce.render(logreduce.reduce(log))
    parts.append(f"Failed job log:\n{reduced}")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(parts)}]


@dataclass
class Diagnosis:
    root_cause: str
    scores: dict[str, float]
    action: str
    fix: str
    quotes: list[str]
    grounded: list[int] = field(default_factory=list)  # original line numbers found
    ungrounded: int = 0
    rejected: bool = False
    flags: list[str] = field(default_factory=list)

    @property
    def hallucination_rate(self) -> float | None:
        return self.ungrounded / len(self.quotes) if self.quotes else None


def _norm(text: str) -> str:
    return " ".join(text.split())


def ground(quotes: list[str], log: str) -> tuple[list[int], int]:
    """Line numbers of the quotes found in the real log, and how many were not found. A quote
    is found if it is a whole log line, or part of one when it has at least MIN_QUOTE
    characters (whitespace collapsed)."""
    lines = [_norm(x) for x in logreduce.original_lines(log)]
    found, missing = [], 0
    for q in quotes:
        q = _norm(q)
        hit = next(
            (
                n
                for n, line in enumerate(lines, 1)
                if q and (q == line or (len(q) >= MIN_QUOTE and q in line))
            ),
            0,
        )
        if hit:
            found.append(hit)
        else:
            missing += 1
    return found, missing


PREFIX = re.compile(r"^L\d+:\s?")


def parse(content: str, log: str, apply_grounding: bool) -> Diagnosis:
    flags = []
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        data, flags = {}, ["unparseable answer"]
    cls = data.get("root_cause")
    if cls not in CLASSES:
        flags.append(f"invalid class {cls!r}" if cls else "no class")
        cls = "unclear"
    raw = data.get("scores") if isinstance(data.get("scores"), dict) else {}
    scores = {}
    for c in CLASSES:
        try:
            scores[c] = max(0.0, float(raw.get(c, 0.0)))
        except (TypeError, ValueError):
            scores[c] = 0.0
    total = sum(scores.values())
    if total > 0:
        scores = {c: s / total for c, s in scores.items()}
    else:
        scores = {c: (1.0 if c == cls else 0.0) for c in CLASSES}
    action = data.get("action")
    if action not in ALLOWED:
        flags.append(f"action {action!r} not allowed for Diagnose")
        action = "escalate_to_human"
    quotes = [PREFIX.sub("", str(q)).strip() for q in (data.get("evidence") or [])][:5]
    quotes = [q for q in quotes if q]
    found, missing = ground(quotes, log)
    d = Diagnosis(cls, scores, action, str(data.get("fix") or ""), quotes, found, missing)
    d.flags = flags
    if apply_grounding and (missing or not quotes):
        d.rejected = True
        d.flags.append("rejected: evidence not in the log" if missing else "rejected: no evidence")
        d.root_cause, d.action = "unclear", "escalate_to_human"
    return d
