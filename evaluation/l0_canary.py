"""Offline L0 canary analysis (D54): the frozen analysis-l0.yaml replayed on recorded series.

Release scenarios are recorded under the agents-mode Rollout (D53), so every canary runs its
full observation window; L0's decision is computed here, on the same recording every level
sees. The interval, count, failureLimit and thresholds are read from the frozen template
itself; its two queries are evaluated as Prometheus 3 evaluates them:

    error-ratio-5xx      (sum(rate(5xx[1m])) or vector(0)) / sum(rate(all[1m]))
    latency-p99-seconds  histogram_quantile(0.99, sum by (le) (rate(bucket[1m])))

on the canary's pods (rollouts_pod_template_hash) and path="root" only, with `rate` over the
left-open range (t - 1m, t], counter resets and Prometheus' extrapolation to the range edges,
and `histogram_quantile` with linear interpolation inside the bucket. As in Argo Rollouts, a
measurement is taken when the analysis starts and then every interval, it succeeds only when
the condition holds (`len(result) == 1 && result[0] <= threshold`: no data and NaN fail), and the
analysis fails at the measurement where a metric's failures exceed its failureLimit.

The analysis of a step starts after the step's 30 s pause; `release_replay.py` finds the steps
in the recording. The test on the dev recordings compares this with L0's live outcome.
"""

from __future__ import annotations

import math
import re
import sys
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents")]

from flowguard import series  # noqa: E402

TEMPLATE = ROOT / "infra" / "apps" / "podinfo" / "base" / "analysis-l0.yaml"
RANGE_S = 60.0  # the queries' [1m]
QUANTILE = 0.99
CONDITION = re.compile(r"^len\(result\) == 1 && result\[0\] <= ([0-9.]+)$")
KINDS = {"error-ratio-5xx": "ratio", "latency-p99-seconds": "p99"}


@dataclass(frozen=True)
class Metric:
    name: str
    kind: str
    interval_s: float
    count: int
    failure_limit: int
    threshold: float


def duration(value: str) -> float:
    units = {"s": 1, "m": 60}
    return float(value[:-1]) * units[value[-1]]


def load_template(path: Path = TEMPLATE) -> list[Metric]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))["spec"]
    out = []
    for m in spec["metrics"]:
        match = CONDITION.match(m["successCondition"].strip())
        if m["name"] not in KINDS or not match:
            raise ValueError(f"{m['name']}: not a metric this evaluator reproduces")
        out.append(
            Metric(
                m["name"],
                KINDS[m["name"]],
                duration(m["interval"]),
                int(m["count"]),
                int(m["failureLimit"]),
                float(match.group(1)),
            )
        )
    return out


# ---------------------------------------------------------------- PromQL, as Prometheus 3


def rate(s: series.Series, t: float, range_s: float = RANGE_S) -> float | None:
    """Prometheus' extrapolated `rate(counter[range])` at t, over samples in (t - range, t]."""
    i, j = bisect_right(s.times, t - range_s), bisect_right(s.times, t)  # left-open range
    times, values = s.times[i:j], s.values[i:j]
    if len(times) < 2:
        return None
    increase = values[-1] - values[0]
    for a, b in zip(values, values[1:], strict=False):
        if b < a:  # counter reset
            increase += a
    start, end = t - range_s, t
    sampled = times[-1] - times[0]
    average = sampled / (len(times) - 1)
    to_start, to_end = times[0] - start, end - times[-1]
    threshold = average * 1.1  # extrapolate to an edge only if a sample could be missing there
    if to_start >= threshold:
        to_start = average / 2
    if increase > 0 and values[0] >= 0:  # a counter does not extrapolate below zero
        to_start = min(to_start, sampled * (values[0] / increase))
    if to_end >= threshold:
        to_end = average / 2
    return increase * ((sampled + to_start + to_end) / sampled) / range_s


def _selected(idx: series.Index, name: str, namespace: str, canary: str):
    for s in idx.get(name, []):
        m = s.labels
        if (
            m.get("namespace") == namespace
            and m.get("app", "podinfo") == "podinfo"
            and m.get("path") == "root"
            and m.get("rollouts_pod_template_hash") == canary
        ):
            yield s


def error_ratio(idx: series.Index, t: float, namespace: str, canary: str) -> list[float]:
    """The error-ratio-5xx query's result vector (empty, or one value that may be NaN)."""
    all_rates, error_rates = [], []
    for s in _selected(idx, "podinfo_requests", namespace, canary):
        r = rate(s, t)
        if r is None:
            continue
        all_rates.append(r)
        if re.fullmatch(r"5..", str(s.labels.get("status", ""))):
            error_rates.append(r)
    if not all_rates:
        return []  # the denominator's sum() is empty, so the division is empty
    numerator = sum(error_rates) if error_rates else 0.0  # or vector(0)
    denominator = sum(all_rates)
    return [numerator / denominator if denominator else math.nan]


def histogram_quantile(q: float, buckets: dict[float, float]) -> float:
    """Prometheus' histogram_quantile over cumulative bucket counts by upper bound."""
    bounds = sorted(buckets)
    if not bounds or not math.isinf(bounds[-1]):
        return math.nan
    counts, top = [], 0.0
    for b in bounds:  # ensure monotonic, as Prometheus does
        top = max(top, buckets[b])
        counts.append(top)
    if len(bounds) < 2 or counts[-1] == 0:
        return math.nan
    rank = q * counts[-1]
    b = next((i for i in range(len(bounds) - 1) if counts[i] >= rank), len(bounds) - 1)
    if b == len(bounds) - 1:
        return bounds[-2]
    if b == 0 and bounds[0] <= 0:
        return bounds[0]
    start, count = 0.0, counts[b]
    if b > 0:
        start, count, rank = bounds[b - 1], counts[b] - counts[b - 1], rank - counts[b - 1]
    return start + (bounds[b] - start) * (rank / count)


def p99(idx: series.Index, t: float, namespace: str, canary: str) -> list[float]:
    """The latency-p99-seconds query's result vector."""
    by_le: dict[float, float] = {}
    for s in _selected(idx, "podinfo_latency_buckets", namespace, canary):
        r = rate(s, t)
        if r is not None:
            le = float(s.labels["le"])
            by_le[le] = by_le.get(le, 0.0) + r
    if not by_le:
        return []
    return [histogram_quantile(QUANTILE, by_le)]


QUERIES = {"ratio": error_ratio, "p99": p99}


def measure(metric: Metric, idx: series.Index, t: float, namespace: str, canary: str) -> bool:
    result = QUERIES[metric.kind](idx, t, namespace, canary)
    return len(result) == 1 and result[0] <= metric.threshold  # NaN fails


def analysis(
    idx: series.Index,
    start: float,
    namespace: str,
    canary: str,
    end: float | None = None,
    metrics: list[Metric] | None = None,
) -> dict:
    """One step's AnalysisRun from `start`: successful, failed (at t) or truncated (the
    recording ends before its last measurement)."""
    metrics = load_template() if metrics is None else metrics
    failures = {m.name: 0 for m in metrics}
    taken = []
    for k in range(max(m.count for m in metrics)):
        t = start + k * metrics[0].interval_s
        if end is not None and t > end:
            return {"outcome": "truncated", "measurements": taken}
        ok = {m.name: measure(m, idx, t, namespace, canary) for m in metrics if k < m.count}
        taken.append({"t": t, **ok})
        for m in metrics:
            if not ok.get(m.name, True):
                failures[m.name] += 1
                if failures[m.name] > m.failure_limit:
                    return {"outcome": "failed", "t": t, "metric": m.name, "measurements": taken}
    return {"outcome": "successful", "t": taken[-1]["t"], "measurements": taken}
