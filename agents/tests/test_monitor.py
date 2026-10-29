"""Monitor (Step 6): features from raw series, the runtime and CI detectors, evidence, P1, the
orchestrator's Monitor slot. Every series here is synthetic, built in the shape Prometheus
returns for `selector[window]`."""

import hashlib
import math
import random
import re
from datetime import UTC, datetime
from urllib.parse import quote_plus as quote

import pytest

from flowguard import monitor, series
from flowguard.agent_io import AgentInput
from flowguard.audit import AuditLog
from flowguard.catalog import AGENT_ACTIONS
from flowguard.contracts import AgentName
from flowguard.executor import Executor
from flowguard.incidents import IncidentRecord
from flowguard.monitor import (
    CIDetector,
    CIModel,
    Monitor,
    MonitorModel,
    RuntimeDetector,
    fit_ci,
    fit_runtime,
    poll,
    spread,
    tick_event,
)
from flowguard.orchestrator import Orchestrator, level

T0 = 1_800_000_000.0
STEP = 15.0
LE = ["0.005", "0.01", "0.025", "0.05", "0.1", "0.25", "0.5", "1", "2.5", "5", "10", "+Inf"]
PODS = ("podinfo-aaa-1", "podinfo-aaa-2", "podinfo-aaa-3", "podinfo-aaa-4")
MIB = 1024 * 1024
REF = re.compile(
    r"^prom:[A-Za-z_:][A-Za-z0-9_:]*\{([a-z_]+=\"[^\"]*\"(,[a-z_]+=\"[^\"]*\")*)?\}"
    r"\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ,\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\]$"
    r"|^ci:run/\d+(/job/\d+)?$"
)


def raw_cluster(seconds, *, start=T0, seed=1, rps=1.0, pods=PODS, fault=None):
    """Raw series for one namespace: request counters by status, latency buckets, working set,
    restarts. fault(pod, t) -> {error_rate, latency_s, slow_fraction, leak_bytes_per_s}."""
    rng = random.Random(seed)
    fault = fault or (lambda pod, t: {})
    counters, buckets, restarts, extra = {}, {}, dict.fromkeys(pods, 0), dict.fromkeys(pods, 0.0)
    out = {"podinfo_requests": {}, "podinfo_latency_buckets": {}, "container_memory": {}}
    out["pod_restarts"] = {}

    def sample(kind, key, labels, t, v):
        s = out[kind].setdefault(key, {"metric": labels, "values": []})
        s["values"].append([t, str(v)])

    for k in range(int(seconds // STEP) + 1):
        t = start + k * STEP
        for pod in pods:
            f = fault(pod, t)
            base = {"namespace": "staging", "pod": pod, "app": "podinfo"}
            for _ in range(int(rps * STEP)):
                status = "500" if rng.random() < f.get("error_rate", 0.0) else "200"
                latency = rng.lognormvariate(math.log(0.004), 0.5)
                if rng.random() < f.get("slow_fraction", 0.0):
                    latency += f.get("latency_s", 0.0)
                counters[(pod, status)] = counters.get((pod, status), 0) + 1
                for le in LE:
                    if latency <= float(le):
                        buckets[(pod, le)] = buckets.get((pod, le), 0) + 1
            for (p, status), n in counters.items():
                if p == pod:
                    labels = {**base, "path": "root", "status": status, "method": "GET"}
                    sample("podinfo_requests", (pod, status), labels, t, n)
            for le in LE:
                labels = {**base, "path": "root", "le": le}
                sample("podinfo_latency_buckets", (pod, le), labels, t, buckets.get((pod, le), 0))
            extra[pod] += f.get("leak_bytes_per_s", 0.0) * STEP
            working = 24 * MIB + rng.uniform(-0.2, 0.2) * MIB + extra[pod]
            if working > 64 * MIB:  # OOM kill: the container restarts empty
                restarts[pod] += 1
                extra[pod] = 0.0
                working = 24 * MIB
            labels = {**base, "container": "podinfo"}
            sample("container_memory", pod, labels, t, working)
            sample("pod_restarts", pod, labels, t, restarts[pod])
    return {name: list(v.values()) for name, v in out.items()}


def ticks(raw, seconds, start=T0):
    idx = series.index(raw)
    return [
        (t, series.snapshot(idx, t))
        for t in (start + k * STEP for k in range(4, int(seconds // STEP) + 1))
    ]


def flagged_times(model, raw, seconds, start=T0):
    det = RuntimeDetector(model)
    return [
        t for t, snap in ticks(raw, seconds, start) if any(s.flagged for s in det.step(snap, t))
    ]


@pytest.fixture(scope="module")
def model():
    normal = raw_cluster(7200, seed=11)
    return fit_runtime(ticks(normal, 7200), fitted_on={"normal": ["synthetic"]})


# ---------------------------------------------------------------- series


def test_counter_increase_counts_resets_and_quantile_matches_prometheus():
    s = series.Series({}, [0, 15, 30, 45], [10, 20, 5, 9])  # reset between 15 and 30
    assert s.increase(0, 45) == 10 + 5 + 4
    assert s.latest(50) == 9 and s.latest(100) is None
    buckets = {0.1: 50.0, 0.5: 90.0, 1.0: 100.0, math.inf: 100.0}
    assert series._quantile(0.5, buckets) == pytest.approx(0.1)
    assert series._quantile(0.99, buckets) == pytest.approx(0.5 + 0.5 * 9 / 10)
    assert series._quantile(0.99, {0.1: 0.0, math.inf: 0.0}) is None
    assert series._quantile(0.999, {0.1: 1.0, math.inf: 2.0}) == 0.1  # +Inf bucket: last bound


def test_snapshot_has_namespace_and_pod_features():
    raw = raw_cluster(600, fault=lambda p, t: {"error_rate": 0.5} if p == PODS[0] else {})
    snap = series.snapshot(series.index(raw), T0 + 600)
    assert 0.3 < snap[f"staging/{PODS[0]}:error_ratio"] < 0.7
    assert snap[f"staging/{PODS[1]}:error_ratio"] == 0.0
    assert 0.07 < snap["staging:error_ratio"] < 0.18  # one pod of four
    assert snap["staging:memory_ratio"] == pytest.approx(24 / 64, abs=0.01)
    assert snap["staging:restarts"] == 0 and snap[f"staging/{PODS[0]}:p99_s"] < 0.1
    assert not [k for k in snap if k.startswith("node")]  # no node series recorded


def test_memory_limit_series_and_node_series_are_used_when_present():
    raw = raw_cluster(120)
    raw["memory_limits"] = [
        {"metric": {"namespace": "staging", "pod": PODS[0]}, "values": [[T0 + 120, str(32 * MIB)]]}
    ]
    raw["node_cpu"] = [{"metric": {"id": "/"}, "values": [[T0 + 60, "100"], [T0 + 120, "154"]]}]
    raw["node_memory"] = [{"metric": {"id": "/"}, "values": [[T0 + 120, str(3 * 1024 * MIB)]]}]
    raw["node_allocatable"] = [
        {"metric": {"resource": "cpu"}, "values": [[T0 + 120, "1"]]},
        {"metric": {"resource": "memory"}, "values": [[T0 + 120, str(4 * 1024 * MIB)]]},
    ]
    snap = series.snapshot(series.index(raw), T0 + 120)
    assert snap[f"staging/{PODS[0]}:memory_ratio"] == pytest.approx(24 / 32, abs=0.02)
    assert snap[f"staging/{PODS[1]}:memory_ratio"] == pytest.approx(24 / 64, abs=0.01)
    assert snap["node:cpu_ratio"] == pytest.approx(0.9)
    assert snap["node:memory_ratio"] == pytest.approx(0.75)


def test_firing_alerts_and_refs_format():
    raw = {
        "alerts": [
            {
                "metric": {"alertname": "HighErrorRatio", "alertstate": "firing"},
                "values": [[T0 + 30, "1"]],
            },
            {"metric": {"alertname": "Other", "alertstate": "pending"}, "values": [[T0 + 30, "1"]]},
        ]
    }
    idx = series.index(raw)
    assert series.firing(idx, T0 + 30) == ["HighErrorRatio"] and series.firing(idx, T0 + 45) == []
    for feature in series.FEATURES:
        for r in series.refs(f"staging/{PODS[0]}", feature, T0):
            assert REF.match(r), r
    assert all(REF.match(r) for r in series.refs("node", "cpu_ratio", T0))


def test_live_poll_reads_the_recorded_selectors_and_builds_a_tick():
    raw = raw_cluster(600)
    urls = []

    def get(url):
        urls.append(url)
        name = next(n for n, sel in series.SELECTORS.items() if quote(f"{sel}[600s]") in url)
        return {"data": {"result": raw.get(name, [])}}

    event = poll(series.PrometheusSource(get=get), T0 + 600)
    assert len(urls) == len(series.SELECTORS) and all(f"time={T0 + 600}" in u for u in urls)
    assert event.type == "metrics_tick" and event.payload["params"]["ts"] == T0 + 600
    assert event.payload["metrics"]["staging:error_ratio"] == 0.0
    assert event.payload["alerts"] == []


# ---------------------------------------------------------------- runtime detector


def test_fit_is_reproducible_through_the_model_file(model, tmp_path):
    loaded = MonitorModel.load(MonitorModel(runtime=model).save(tmp_path / "m.json")).runtime
    assert loaded == model
    x = [[5.0, 0.0, 0.0, 0.0, 4.0, 0.0, 0.0, 0.0, 5.0, 4.0]]
    assert model.forest().score_samples(x)[0] == loaded.forest().score_samples(x)[0]
    assert model.if_threshold > 0 and len(model.train) > 100


def test_quiet_on_new_normal_traffic(model):
    assert flagged_times(model, raw_cluster(3600, seed=99), 3600) == []


@pytest.mark.parametrize(
    "fault",
    [
        {"error_rate": 0.2},  # one pod of four: 5% of the service, below L0's alert
        {"latency_s": 0.8, "slow_fraction": 0.5},  # p99 under L0's 1 s
        {"leak_bytes_per_s": 120 * 1024},
    ],
    ids=["error_burst", "latency_spike", "memory_leak"],
)
def test_detects_each_runtime_fault_after_it_starts(model, fault):
    start = T0 + 600

    def inject(pod, t):
        return fault if pod == PODS[0] and start <= t < start + 300 else {}

    times = flagged_times(model, raw_cluster(1200, seed=5, fault=inject), 1200)
    assert times and min(times) >= start
    assert min(times) - start <= 180  # within the incident's first three minutes


def test_baseline_is_frozen_during_an_anomaly(model):
    det = RuntimeDetector(model)
    entity = f"staging/{PODS[0]}"
    for k in range(12):
        det.step({f"{entity}:error_ratio": 0.0}, T0 + k * STEP)
    for k in range(12, 30):
        det.step({f"{entity}:error_ratio": 0.3}, T0 + k * STEP)
    learned = [v for _, v in det.history[(entity, "error_ratio")]]
    assert max(learned) == 0.0  # 4.5 minutes of 30% errors never entered the baseline


# ---------------------------------------------------------------- the agent


def inp(metrics=None, alerts=(), t=T0, environment="staging", **params):
    return AgentInput(
        incident_id="i-1",
        environment=environment,
        metrics=dict(metrics or {}),
        alerts=list(alerts),
        params={"ts": t, **params},
    )


def test_monitor_proposes_nothing_and_opens_one_incident_per_episode(model):
    assert AGENT_ACTIONS[AgentName.MONITOR] == frozenset()
    mon, l1 = Monitor(MonitorModel(runtime=model)), level("L1")
    start = T0 + 600
    raw = raw_cluster(
        1200,
        seed=5,
        fault=lambda p, t: {"error_rate": 0.3} if p == PODS[0] and start <= t < start + 300 else {},
    )
    outs = [(t, mon.run(inp(snap, t=t), l1)) for t, snap in ticks(raw, 1200)]
    assert all(o.proposals == [] for _, o in outs)
    opened = [(t, o) for t, o in outs if o.incident]
    assert len(opened) >= 1 and all(t >= start for t, _ in opened)
    post = opened[0][1].evidence[0]
    assert post.agent == "monitor" and f"staging/{PODS[0]}" in post.claim
    assert "isolation score" in post.claim and post.refs
    assert all(REF.match(r) for o in opened for p in o[1].evidence for r in p.refs)
    assert any("http_request_duration_seconds_count" in r for r in post.refs)


def test_l0_reads_only_alerts_and_l1_without_a_model_says_so():
    mon = Monitor(model=None)
    out = mon.run(inp({"staging:error_ratio": 0.9}), level("L0"))
    assert not out.incident  # L0: no alert, no incident, whatever the features say
    out = mon.run(inp(alerts=["HighErrorRatio"], t=T0 + 15), level("L0"))
    assert out.incident and out.evidence[0].claim == "L0 alert HighErrorRatio firing (staging)"
    assert REF.match(out.evidence[0].refs[0])
    assert not mon.run(inp(alerts=["HighErrorRatio"], t=T0 + 30), level("L0")).incident  # same
    out = Monitor(model=None).run(inp(alerts=["PodCrashLooping"]), level("L1"))
    assert [p.claim for p in out.evidence][-1] == "detectors off, no fitted model: runtime, ci"


def test_episodes_and_the_p1_view_are_kept_per_environment():
    mon, l0 = Monitor(model=None), level("L0")
    staging = mon.run(inp(alerts=["HighErrorRatio"]), l0)
    prod = mon.run(inp(alerts=["HighErrorRatio"], environment="prod"), l0)
    assert staging.incident and prod.incident  # the same rule in two namespaces: two incidents
    assert prod.evidence[0].claim == "L0 alert HighErrorRatio firing (prod)"
    assert not mon.run(inp(alerts=["HighErrorRatio"], t=T0 + 15), l0).incident  # same episode
    healthy = {"node:cpu_ratio": 0.3, "node:memory_ratio": 0.4}
    mon.run(inp({**healthy, "prod/podinfo-old-1:restarts": 3}, t=T0 + 30, environment="prod"), l0)
    mon.run(inp(healthy, t=T0 + 30), l0)
    assert "normal" in mon.answer(inp(t=T0 + 35)).claim  # staging's view
    assert "3 restarts" in mon.answer(inp(t=T0 + 35, environment="prod")).claim
    raw = {"alerts": [{"metric": {"alertname": "HighErrorRatio", "alertstate": "firing",
                                  "namespace": "prod"}, "values": [[T0, "1"]]}]}  # fmt: skip
    assert series.firing(series.index(raw), T0, namespace="staging") == []
    assert series.firing(series.index(raw), T0, namespace="prod") == ["HighErrorRatio"]


def test_orchestrator_slot_l0_vs_l1_on_the_same_ticks(model, tmp_path):
    start = T0 + 600
    raw = raw_cluster(
        1200,
        seed=5,
        fault=lambda p, t: {"error_rate": 0.3} if p == PODS[0] and start <= t < start + 300 else {},
    )
    events = [tick_event(snap, [], t) for t, snap in ticks(raw, 1200)]
    opened = {}
    for name in ("L0", "L1"):
        db = tmp_path / f"{name}.db"
        audit = AuditLog(db)
        orch = Orchestrator(
            cfg=level(name),
            record=IncidentRecord(db),
            executor=Executor(audit=audit),
            audit=audit,
        )
        assert type(orch.agents[AgentName.MONITOR]) is Monitor  # the real agent, via stubs
        orch.agents[AgentName.MONITOR] = Monitor(MonitorModel(runtime=model))
        outs = [orch.handle(e) for e in events]
        opened[name] = [o for o in outs if o["agent"] != "monitor"]  # followed up: an incident
        for o in opened[name]:
            claims = [e.claim for e in orch.record.evidence(o["incident_id"])]
            assert any(c.startswith(f"anomaly on staging/{PODS[0]}:") for c in claims)
    assert opened["L0"] == [] and len(opened["L1"]) == 1  # one fault, one incident


# ---------------------------------------------------------------- CI detector


def gh_time(t):
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def ci_run(run_id, unit_s, *, start=T0, branch="dev", conclusion="success", done=True):
    jobs = [
        {
            "id": run_id * 10 + 1,
            "name": "build",
            "conclusion": "success",
            "started_at": gh_time(start),
            "completed_at": gh_time(start + 60),
        },
        {
            "id": run_id * 10 + 2,
            "name": "unit",
            "conclusion": conclusion if done else None,
            "started_at": gh_time(start + 60),
            "completed_at": gh_time(start + 60 + unit_s) if done else None,
        },
    ]
    run = {"id": run_id, "head_branch": branch, "status": "completed" if done else "in_progress"}
    return run, jobs


def test_ci_fit_and_a_slow_job_is_flagged_while_it_still_runs():
    history = [ci_run(i, d) for i, d in enumerate([98, 102, 100, 97, 105, 101, 99], start=1)]
    m = fit_ci(history)
    assert m.k == 3.0 and sorted(m.durations["unit"])[0] == 97
    med, s = spread(m.durations["unit"])
    bound = med + m.k * s
    det = CIDetector(m)
    run, jobs = ci_run(50, 300, start=T0 + 1000, done=False)
    assert det.check(run, jobs, T0 + 1000 + 60 + bound - 1) == []
    (f,) = det.check(run, jobs, T0 + 1000 + 60 + bound + 1)
    assert f.kind == "slow" and f.job == "unit" and f.job_id == 502
    assert det.check(run, jobs, T0 + 1000 + 200) == []  # reported once
    run, jobs = ci_run(51, 104, start=T0 + 2000)
    assert det.check(run, jobs, T0 + 2200) == []  # a normal run stays quiet


def test_failure_streak_on_one_branch():
    det = CIDetector(CIModel(durations={}, streak=3))
    found = []
    for i in range(3):
        found += det.check(*ci_run(i, 30, branch="feature", conclusion="failure"), T0 + i)
    found += det.check(*ci_run(9, 30, branch="other", conclusion="failure"), T0 + 9)
    (f,) = found
    assert f.kind == "streak" and f.run_ids == [0, 1, 2]
    det.check(*ci_run(3, 30, branch="feature"), T0 + 10)  # green resets the streak
    assert det.check(*ci_run(4, 30, branch="feature", conclusion="failure"), T0 + 11) == []


def test_monitor_posts_ci_evidence():
    history = [ci_run(i, 100 + i % 3) for i in range(1, 8)]
    mon = Monitor(MonitorModel(ci=fit_ci(history)))
    run, jobs = ci_run(77, 400, start=T0)
    out = mon.run(inp(t=T0 + 460, ci_run={"run": run, "jobs": jobs}), level("L1"))
    claims = [p.claim for p in out.evidence]
    assert out.incident and claims[0].startswith("CI job unit at 400 s, above its")
    assert out.evidence[0].refs == ["ci:run/77/job/772"]
    assert claims[-1] == "detectors off, no fitted model: runtime"


# ---------------------------------------------------------------- P1


def test_p1_answer_claims_with_series_refs():
    mon = Monitor(model=None)
    stress = mon.answer(inp({"node:cpu_ratio": 0.93, "node:memory_ratio": 0.5}))
    assert stress.claim.startswith("node under stress: cpu 0.93 >= 0.51")
    assert any("container_cpu_usage_seconds_total" in r for r in stress.refs)
    assert all(REF.match(r) for r in stress.refs)
    normal = mon.answer(inp({"node:cpu_ratio": 0.4, "node:memory_ratio": 0.5}))
    assert normal.claim.startswith("node resources normal: cpu 0.40, memory 0.50")
    unknown = mon.answer(inp({"node:cpu_ratio": 0.4}))
    assert unknown.claim == "node stress unknown: no memory series"  # never "normal" unmeasured
    assert mon.answer(inp()).claim == "node stress unknown: no cpu or memory series"
    assert "stress" in mon.answer(inp({"node_cpu_ratio": 0.97})).claim  # Step 5 key


def test_p1_thresholds_are_frozen():
    """Set from the recordings-2 node series by the rule in monitor.py, then frozen: cpu from
    the separated medians (0.023 unstressed, 0.990 stressed), memory provisional (no recording
    is memory-stressed), any restart of a non-canary pod. Changing them needs a decision."""
    assert (monitor.P1_CPU, monitor.P1_MEMORY, monitor.P1_RESTARTS) == (0.507, 0.90, 1.0)


# The model fitted on the first half of r-24e489bc (dev normal traffic) with the settings the
# grid chose on the 18 dev incidents (in-sample): frozen from now on
MODEL_SHA256 = "3beb9c02fb3c441dda5d7fa86b5b5dad5c55763c46a1be1411975a801022fd16"


def test_the_monitor_model_is_frozen():
    body = monitor.MODEL_FILE.read_bytes()
    assert hashlib.sha256(body).hexdigest() == MODEL_SHA256
    runtime = MonitorModel.load().runtime
    assert (runtime.z_threshold, runtime.quantile, runtime.persist) == (5.0, 0.99, 1)
    assert Monitor().model is not None and Monitor().runtime is not None  # the default loads it


def test_p1_counts_restarts_on_non_canary_pods_only():
    mon = Monitor(model=None)
    healthy = {"node:cpu_ratio": 0.3, "node:memory_ratio": 0.4}
    stable, canary = "staging/podinfo-old-1", "staging/podinfo-new-1"
    mon.run(inp({**healthy, f"{stable}:restarts": 0}, t=T0), level("L0"))
    view = {**healthy, f"{stable}:restarts": 0, f"{canary}:restarts": 2}
    mon.run(inp(view, t=T0 + 15), level("L0"))
    assert mon.answer(inp(t=T0 + 20)).claim.startswith("node resources normal")  # canary inferred
    view[f"{stable}:restarts"] = 1
    mon.run(inp(view, t=T0 + 30), level("L0"))
    claim = mon.answer(inp(t=T0 + 35, canary_hash="new")).claim
    assert claim == f"node under stress: 1 restarts on {stable}"
    stale = mon.answer(inp(t=T0 + 35 + 120))  # the last poll is too old to be "right now"
    assert stale.claim.startswith("node stress unknown")
