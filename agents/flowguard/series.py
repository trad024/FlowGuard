"""Prometheus series -> the Monitor's features (DSO1). The same code runs live and in replay (D15).

A series set maps a name to a Prometheus matrix result, as a `selector[window]` query returns it
and as recordings store it: [{"metric": {labels}, "values": [[unix time, "value"], ...]}, ...].
`snapshot(index, t)` turns it into one flat dict of features at time t:

    <namespace>:<feature>          the namespace as a whole, like the L0 alert rules
    <namespace>/<pod>:<feature>    one pod
    node:cpu_ratio, node:memory_ratio    cluster CPU and memory against allocatable, when the
                                         node series exist (P1)

Features: error_ratio (5xx share of requests to /, 1-minute window), p99_s (from the latency
histogram, 1-minute window), restarts (container restarts in the last 5 minutes), memory_ratio
(working set over the memory limit; the limit series when present, else the manifest's 64 Mi).
Every feature has the raw series behind it, given by `refs()` for evidence posts.
"""

from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

FEATURES = ("error_ratio", "p99_s", "memory_ratio", "restarts")
RATE_WINDOW_S = 60  # like the L0 rules' 1-minute rates
RESTART_WINDOW_S = 300
LOOKBACK_S = 30  # a gauge sample older than two scrapes is stale
DEFAULT_MEMORY_LIMIT = 64 * 1024 * 1024  # infra/apps/podinfo/base/podinfo.yaml
HISTORY_S = 600  # how far back a live poll reads

# Live queries, recorded under the same names since D49 (by the recorder); recordings
# made before D49 have no node data, so their memory ratio uses DEFAULT_MEMORY_LIMIT.
SELECTORS = {
    "podinfo_requests": 'http_request_duration_seconds_count{app="podinfo"}',
    "podinfo_latency_buckets": 'http_request_duration_seconds_bucket{app="podinfo",path="root"}',
    "container_memory": (
        'container_memory_working_set_bytes{namespace=~"staging|prod",container="podinfo"}'
    ),
    "pod_restarts": 'kube_pod_container_status_restarts_total{namespace=~"staging|prod"}',
    "alerts": 'ALERTS{alertname=~"HighErrorRatio|HighLatencyP99|ContainerOOMKilled'
    '|PodCrashLooping|ContainerHighMemory"}',
    "memory_limits": (
        'kube_pod_container_resource_limits{namespace=~"staging|prod",container="podinfo",'
        'resource="memory"}'
    ),
    "node_cpu": 'container_cpu_usage_seconds_total{id="/"}',
    "node_memory": 'container_memory_working_set_bytes{id="/"}',
    "node_allocatable": 'kube_node_status_allocatable{resource=~"cpu|memory"}',
}

# feature -> (metric, extra labels, window in seconds) behind it, for evidence references
FEATURE_SERIES = {
    "error_ratio": [("http_request_duration_seconds_count", {"path": "root"}, RATE_WINDOW_S)],
    "p99_s": [("http_request_duration_seconds_bucket", {"path": "root"}, RATE_WINDOW_S)],
    "memory_ratio": [
        ("container_memory_working_set_bytes", {"container": "podinfo"}, LOOKBACK_S),
        ("kube_pod_container_resource_limits", {"resource": "memory"}, LOOKBACK_S),
    ],
    "restarts": [("kube_pod_container_status_restarts_total", {}, RESTART_WINDOW_S)],
    "cpu_ratio": [
        ("container_cpu_usage_seconds_total", {"id": "/"}, RATE_WINDOW_S),
        ("kube_node_status_allocatable", {"resource": "cpu"}, LOOKBACK_S),
    ],
    "node_memory_ratio": [
        ("container_memory_working_set_bytes", {"id": "/"}, LOOKBACK_S),
        ("kube_node_status_allocatable", {"resource": "memory"}, LOOKBACK_S),
    ],
}


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def series_ref(metric: str, labels: dict, start: float, end: float) -> str:
    """prom:<metric>{<labels>}[<start>,<end>]: a query a human can rerun over that window."""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return f"prom:{metric}{{{inner}}}[{iso(start)},{iso(end)}]"


def refs(entity: str, feature: str, t: float) -> list[str]:
    """The raw series behind one feature of one entity at time t."""
    if entity == "node":
        key = "node_memory_ratio" if feature == "memory_ratio" else feature
        labels = {}
    else:
        namespace, _, pod = entity.partition("/")
        key, labels = feature, {"namespace": namespace, **({"pod": pod} if pod else {})}
    return [
        series_ref(metric, {**labels, **extra}, t - window, t)
        for metric, extra, window in FEATURE_SERIES[key]
    ]


def split_key(key: str) -> tuple[str, str] | None:
    """'staging/podinfo-a-b:p99_s' -> ('staging/podinfo-a-b', 'p99_s'); None if not a feature."""
    entity, sep, feature = key.rpartition(":")
    return (entity, feature) if sep and entity else None


def pod_hash(pod: str) -> str | None:
    """The pod-template hash in an Argo Rollouts pod name (podinfo-<hash>-<suffix>)."""
    parts = pod.rsplit("-", 2)
    return parts[1] if len(parts) == 3 else None


# ---------------------------------------------------------------- indexed series


@dataclass
class Series:
    labels: dict
    times: list[float]
    values: list[float]

    def between(self, lo: float, hi: float) -> tuple[list[float], list[float]]:
        i, j = bisect_left(self.times, lo), bisect_right(self.times, hi)
        return self.times[i:j], self.values[i:j]

    def latest(self, t: float, lookback: float = LOOKBACK_S) -> float | None:
        j = bisect_right(self.times, t)
        if j and self.times[j - 1] >= t - lookback:
            return self.values[j - 1]
        return None

    def increase(self, lo: float, hi: float) -> float:
        """Counter increase between the samples inside [lo, hi], counting resets as Prometheus
        does (a drop means the counter restarted from zero)."""
        _, values = self.between(lo, hi)
        return sum(b - a if b >= a else b for a, b in zip(values, values[1:], strict=False))

    def rate(self, lo: float, hi: float) -> float | None:
        times, _ = self.between(lo, hi)
        if len(times) < 2 or times[-1] <= times[0]:
            return None
        return self.increase(lo, hi) / (times[-1] - times[0])


Index = dict[str, list[Series]]


def index(raw: dict[str, list[dict] | dict]) -> Index:
    """Name -> matrix result (or a recording's {"result": [...]} file) -> sorted series."""
    out: Index = {}
    for name, data in raw.items():
        result = data.get("result", []) if isinstance(data, dict) else data
        out[name] = []
        for s in result:
            pairs = sorted((float(t), float(v)) for t, v in s.get("values", []))
            out[name].append(
                Series(dict(s.get("metric", {})), [p[0] for p in pairs], [p[1] for p in pairs])
            )
    return out


def _quantile(q: float, buckets: dict[float, float]) -> float | None:
    """histogram_quantile over cumulative bucket counts {le: count}, as Prometheus computes it."""
    bounds = sorted(buckets)
    if not bounds or bounds[-1] != math.inf:
        return None
    counts, running = [], 0.0
    for le in bounds:  # increases may be slightly non-monotonic between scrapes
        running = max(running, buckets[le])
        counts.append(running)
    total = counts[-1]
    if total <= 0:
        return None
    rank = q * total
    i = next(k for k, c in enumerate(counts) if c >= rank)
    if bounds[i] == math.inf:
        return bounds[i - 1] if i else 0.0
    lo_le, lo_c = (bounds[i - 1], counts[i - 1]) if i else (0.0, 0.0)
    if counts[i] == lo_c:
        return bounds[i]
    return lo_le + (bounds[i] - lo_le) * (rank - lo_c) / (counts[i] - lo_c)


def _where(labels: dict, namespace: str = "") -> bool:
    return not namespace or labels.get("namespace") == namespace


def snapshot(idx: Index, t: float, memory_limit: float = DEFAULT_MEMORY_LIMIT) -> dict[str, float]:
    """Every feature at time t, flat (see the module docstring)."""
    lo = t - RATE_WINDOW_S
    requests: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0])  # (ns, pod) -> all, 5xx
    for s in idx.get("podinfo_requests", []):
        m = s.labels
        if m.get("path") != "root" or "namespace" not in m or "pod" not in m:
            continue
        inc = s.increase(lo, t)
        c = requests[(m["namespace"], m["pod"])]
        c[0] += inc
        c[1] += inc if str(m.get("status", "")).startswith("5") else 0.0
    buckets: dict[tuple, dict[float, float]] = defaultdict(lambda: defaultdict(float))
    for s in idx.get("podinfo_latency_buckets", []):
        m = s.labels
        if "namespace" not in m or "pod" not in m or "le" not in m:
            continue
        buckets[(m["namespace"], m["pod"])][float(m["le"])] += s.increase(lo, t)
    memory: dict[tuple, float] = defaultdict(float)
    for s in idx.get("container_memory", []):
        m = s.labels
        if (v := s.latest(t)) is not None and "namespace" in m and "pod" in m:
            memory[(m["namespace"], m["pod"])] += v
    limits: dict[tuple, float] = defaultdict(float)
    for s in idx.get("memory_limits", []):
        m = s.labels
        if (v := s.latest(t, RESTART_WINDOW_S)) is not None and "pod" in m:
            limits[(m.get("namespace", ""), m["pod"])] += v
    restarts: dict[tuple, float] = defaultdict(float)
    for s in idx.get("pod_restarts", []):
        m = s.labels
        if m.get("container", "podinfo") != "podinfo" or "namespace" not in m or "pod" not in m:
            continue
        times, _ = s.between(t - RESTART_WINDOW_S, t)
        if times:
            restarts[(m["namespace"], m["pod"])] += s.increase(t - RESTART_WINDOW_S, t)

    out: dict[str, float] = {}
    pods = set(requests) | set(memory) | set(restarts)
    for ns in sorted({ns for ns, _ in pods}):
        mine = [p for p in pods if p[0] == ns]
        for key in mine:
            entity = f"{ns}/{key[1]}"
            if key in requests and requests[key][0] > 0:
                out[f"{entity}:error_ratio"] = requests[key][1] / requests[key][0]
            if key in buckets and (q := _quantile(0.99, buckets[key])) is not None:
                out[f"{entity}:p99_s"] = q
            if key in memory:
                out[f"{entity}:memory_ratio"] = memory[key] / (limits.get(key) or memory_limit)
            if key in restarts:
                out[f"{entity}:restarts"] = restarts[key]
        total = sum(requests[k][0] for k in mine if k in requests)
        if total > 0:
            out[f"{ns}:error_ratio"] = sum(requests[k][1] for k in mine if k in requests) / total
        merged: dict[float, float] = defaultdict(float)
        for k in mine:
            for le, c in buckets.get(k, {}).items():
                merged[le] += c
        if (q := _quantile(0.99, merged)) is not None:
            out[f"{ns}:p99_s"] = q
        ratios = [out[f"{ns}/{k[1]}:memory_ratio"] for k in mine if k in memory]
        if ratios:
            out[f"{ns}:memory_ratio"] = max(ratios)
        if any(k in restarts for k in mine):
            out[f"{ns}:restarts"] = sum(restarts[k] for k in mine if k in restarts)
    out.update(node(idx, t))
    return out


def node(idx: Index, t: float) -> dict[str, float]:
    """Cluster CPU and memory against allocatable (P1), when those series exist."""
    alloc: dict[str, float] = defaultdict(float)
    for s in idx.get("node_allocatable", []):
        if (v := s.latest(t, RESTART_WINDOW_S)) is not None:
            alloc[s.labels.get("resource", "")] += v
    out = {}
    cpu = [r for s in idx.get("node_cpu", []) if (r := s.rate(t - RATE_WINDOW_S, t)) is not None]
    if cpu and alloc.get("cpu"):
        out["node:cpu_ratio"] = sum(cpu) / alloc["cpu"]
    mem = [v for s in idx.get("node_memory", []) if (v := s.latest(t)) is not None]
    if mem and alloc.get("memory"):
        out["node:memory_ratio"] = sum(mem) / alloc["memory"]
    return out


def by_hash(idx: Index, t: float, namespace: str) -> dict[str, tuple[float, float]]:
    """Pod-template hash -> (requests, 5xx) to / over the last minute in one namespace: per-pod
    counter increases summed per hash, how DSO3 compares canary and stable (D21)."""
    out: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for s in idx.get("podinfo_requests", []):
        m = s.labels
        h = m.get("rollouts_pod_template_hash")
        if m.get("path") != "root" or m.get("namespace") != namespace or not h:
            continue
        inc = s.increase(t - RATE_WINDOW_S, t)
        out[h][0] += inc
        out[h][1] += inc if str(m.get("status", "")).startswith("5") else 0.0
    return {h: (c[0], c[1]) for h, c in out.items()}


def firing(idx: Index, t: float, step: float = 15.0, namespace: str = "") -> list[str]:
    """The L0 alerts Prometheus had firing in (t - step, t], from the recorded ALERTS series;
    only one namespace's when given (every L0 rule keeps the namespace label)."""
    names = set()
    for s in idx.get("alerts", []):
        if s.labels.get("alertstate") == "firing" and _where(s.labels, namespace):
            times, _ = s.between(t - step + 1e-6, t)
            if times:
                names.add(s.labels.get("alertname", ""))
    return sorted(names)


# ---------------------------------------------------------------- live source


class PrometheusSource:
    """Live polling: the same raw samples a recording holds, read with `selector[window]`."""

    def __init__(
        self,
        base: str = "http://localhost:9090",
        get: Callable[[str], dict] | None = None,
        selectors: dict[str, str] = SELECTORS,
    ):
        self.base, self.selectors = base.rstrip("/"), selectors
        self.get = get or self._get

    @staticmethod
    def _get(url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "flowguard-monitor"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def fetch(self, t: float, window_s: int = HISTORY_S) -> Index:
        raw = {}
        for name, selector in self.selectors.items():
            query = urllib.parse.urlencode({"query": f"{selector}[{window_s}s]", "time": t})
            raw[name] = self.get(f"{self.base}/api/v1/query?{query}").get("data", {})
        return index(raw)
