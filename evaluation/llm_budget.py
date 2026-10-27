"""LLM budget measurement (D27): dev CI failure logs through the pinned model with the planned
prompt budget, to measure tokens per call and project the free quota for the full evaluation.

    python evaluation/llm_budget.py measure [--n 20]
    python evaluation/llm_budget.py project --calls 5000 [--tokens-per-day 200000]

`measure` sends a seeded selection of dev logs (the real CI failing-job logs first, then local
template-check logs): each log reduced to about 1,200 tokens (error lines with context and the
tail), 3 retrieved cases from other dev logs (standing in for retrieval, which Step 7 builds),
and a short JSON answer. Results go to evaluation/runs/llm-budget.json; responses are cached by
the client, so a rerun sends nothing new. Only dev logs: never held-out data.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "ci")]

import deployer  # noqa: E402  (.env)
from flowguard import llm  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402

RUNS = ROOT / "evaluation" / "runs"
RECORDINGS = ROOT / "evaluation" / "recordings"
LOG_BUDGET_CHARS = 4800  # about 1,200 tokens at about 4 characters per token
SEED = 20261027
SIGNAL = re.compile(
    r"error|fail|panic|fatal|cannot|unable|timeout|timed out|denied|not found|missing|refused",
    re.IGNORECASE,
)
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ")
SYSTEM = (
    "You diagnose failed CI jobs. Classify the root cause as exactly one of: code, dependency, "
    "environment, flaky_test, infrastructure. Quote 1 to 3 log lines, with their line numbers, "
    "that show the cause. Answer only JSON: "
    '{"root_cause": "...", "evidence": [{"line": 0, "text": "..."}], "fix": "at most 20 words"}'
)


def reduce_log(text: str, budget: int = LOG_BUDGET_CHARS) -> str:
    """Numbered lines around error signals, plus the tail, within the budget (newest first kept)."""
    lines = [TIMESTAMP.sub("", line) for line in text.splitlines()]
    # GitHub's post-job cleanup (credentials, orphan processes) says nothing about the failure
    cut = next((i for i, line in enumerate(lines) if "Post job cleanup" in line), len(lines))
    lines = lines[:cut]
    keep = set(range(max(0, len(lines) - 40), len(lines)))
    for i, line in enumerate(lines):
        if SIGNAL.search(line):
            keep.update(range(max(0, i - 2), min(len(lines), i + 3)))
    out, size = [], 0
    for i in sorted(keep, reverse=True):
        row = f"{i + 1}: {lines[i][:300]}"
        if size + len(row) + 1 > budget:
            break
        out.append(row)
        size += len(row) + 1
    return "\n".join(reversed(out))


def dev_logs() -> list[tuple[str, str, str]]:
    """(name, root cause, failing job log) for dev failures: real CI first, then local checks."""
    found = []
    for gt_path in sorted(RECORDINGS.glob("r-*/ground_truth.json")):
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        cause = (gt.get("ground_truth") or {}).get("root_cause")
        job = (gt.get("ground_truth") or {}).get("failing_job")
        if not cause or not job:
            continue
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in job)
        for log in gt_path.parent.glob(f"ci/*/logs/{safe}.txt"):
            found.append((f"ci:{gt_path.parent.name}", cause, log.read_text(encoding="utf-8")))
    sys.path.insert(0, str(ROOT / "scenarios"))
    import ci_failures

    checks = RUNS / "template-checks"
    local = []
    for tid, template in sorted(ci_failures.TEMPLATES.items()):
        path = checks / f"{tid}.log"
        if path.is_file():
            local.append((f"local:{tid}", template.root_cause, path.read_text(encoding="utf-8")))
    random.Random(SEED).shuffle(local)
    return found + local


def retrieved_cases(logs, exclude: str, rng: random.Random) -> str:
    pool = [entry for entry in logs if entry[0] != exclude]
    cases = []
    for _name, cause, text in rng.sample(pool, 3):
        evidence = [line for line in reduce_log(text, 1200).splitlines() if SIGNAL.search(line)]
        cases.append(f"Similar past failure ({cause}):\n" + "\n".join(evidence[:3]))
    return "\n\n".join(cases)


def measure(n: int) -> dict:
    env = deployer.read_env_file(ROOT / ".env")
    client = llm.LLMClient(api_key=env["LLM_API_KEY"], audit=AuditLog.open())
    logs = dev_logs()[:n]
    rng = random.Random(SEED)
    calls = []
    for name, cause, text in logs:
        messages = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": retrieved_cases(dev_logs(), name, rng)
                + "\n\nFailed job log (reduced):\n"
                + reduce_log(text),
            },
        ]
        started = time.monotonic()
        out = client.complete(messages)
        seconds = round(time.monotonic() - started, 2)
        try:
            answer = json.loads(out["content"])
        except ValueError:
            answer = {}
        calls.append(
            {
                "log": name,
                "cached": out["cached"],
                "seconds": None if out["cached"] else seconds,
                **{
                    f"{k}_tokens": out["usage"].get(f"{k}_tokens")
                    for k in ("prompt", "completion", "total")
                },
                "valid_json": bool(answer),
                "root_cause_matches": answer.get("root_cause") == cause,
                "rate": out.get("rate"),
            }
        )
        print(json.dumps({k: v for k, v in calls[-1].items() if k != "rate"}), flush=True)
    totals = [c["total_tokens"] for c in calls]
    summary = {
        "model": client.settings.model,
        "settings": client.settings.__dict__,
        "calls": len(calls),
        "tokens_per_call": {
            "mean": round(statistics.mean(totals)),
            "median": statistics.median(totals),
            "max": max(totals),
            "prompt_mean": round(statistics.mean(c["prompt_tokens"] for c in calls)),
            "completion_mean": round(statistics.mean(c["completion_tokens"] for c in calls)),
        },
        "seconds_per_call_median": statistics.median(
            [c["seconds"] for c in calls if c["seconds"] is not None] or [0]
        ),
        "valid_json": sum(c["valid_json"] for c in calls),
        "rate_limits_seen": next((c["rate"] for c in reversed(calls) if c.get("rate")), None),
        "detail": calls,
    }
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / "llm-budget.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def project(
    calls: int,
    tokens_per_call: float,
    tokens_per_day: int,
    requests_per_day: int,
    usable: float = 0.9,
) -> dict:
    """Days of free quota for `calls` LLM calls, limited by tokens or requests per day."""
    by_tokens = calls * tokens_per_call / (usable * tokens_per_day)
    by_requests = calls / (usable * requests_per_day)
    return {
        "calls": calls,
        "days_by_tokens": round(by_tokens, 1),
        "days_by_requests": round(by_requests, 1),
        "days": math.ceil(max(by_tokens, by_requests)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure")
    m.add_argument("--n", type=int, default=20)
    p = sub.add_parser("project")
    p.add_argument("--calls", type=int, required=True)
    p.add_argument("--tokens-per-day", type=int, default=200_000)
    p.add_argument("--requests-per-day", type=int, default=1000)
    args = parser.parse_args(argv)
    if args.cmd == "measure":
        s = measure(args.n)
        print(json.dumps({k: v for k, v in s.items() if k != "detail"}, indent=1))
        return 0
    measured = json.loads((RUNS / "llm-budget.json").read_text(encoding="utf-8"))
    per_call = measured["tokens_per_call"]["mean"]
    print(json.dumps(project(args.calls, per_call, args.tokens_per_day, args.requests_per_day)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
