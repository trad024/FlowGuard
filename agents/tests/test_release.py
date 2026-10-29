"""Release (Step 8): the always-valid sequential test, the absolute floor, the decision mapping,
P1 holding a rollback under node stress, and the per-hash windows built from Prometheus series.
Canaries are simulated on L0's schedule: 4 looks at 20% then 4 at 50% of 10 requests/s."""

import random

import pytest

from flowguard import release, series
from flowguard.agent_io import AgentInput
from flowguard.audit import AuditLog
from flowguard.catalog import AGENT_ACTIONS
from flowguard.contracts import AgentName
from flowguard.executor import Executor
from flowguard.incidents import IncidentRecord
from flowguard.orchestrator import Orchestrator, level

SCHEDULE = [(60, 240)] * 4 + [(150, 150)] * 4  # canary, stable requests per 30 s window
T = 1_800_000_000.0


def canary(rng, p_canary, p_stable, slow_canary=0.0, slow_stable=0.0):
    """Look after every window; the look (1 to 8) at which Release would roll back, or None."""
    draw = lambda n, p: sum(rng.random() < p for _ in range(n))  # noqa: E731
    ws = []
    for i, (nc, ns) in enumerate(SCHEDULE):
        ws.append([nc, draw(nc, p_canary), draw(nc, slow_canary),
                   ns, draw(ns, p_stable), draw(ns, slow_stable)])  # fmt: skip
        if release.judge(ws, i % 4 + 1).outcome == "rollback":
            return i + 1
    return None


@pytest.mark.parametrize("p", [0.0, 0.002, 0.02, 0.1])
def test_false_rollbacks_stay_under_alpha_when_canary_and_stable_are_alike(p):
    rng = random.Random(f"h0-{p}")
    runs = 1000
    false = sum(canary(rng, p, p, p, p) is not None for _ in range(runs))
    assert false / runs <= release.ALPHA  # 8 looks, both metrics: still under alpha


@pytest.mark.parametrize(
    ("p_canary", "p_stable", "slow", "power", "by_look"),
    [
        (0.05, 0.0, False, 1.0, 1),  # 5% 5xx, clean stable: caught at the first look
        (0.03, 0.005, False, 0.85, 8),  # 3% vs 0.5%: about 90% within the 8 looks
        (0.05, 0.0, True, 1.0, 1),  # 5% of requests over 500 ms
    ],
)
def test_power_on_clear_differences(p_canary, p_stable, slow, power, by_look):
    rng = random.Random(f"h1-{p_canary}-{slow}")
    runs = 200
    looks = [
        canary(rng, 0, 0, p_canary, p_stable) if slow else canary(rng, p_canary, p_stable)
        for _ in range(runs)
    ]
    caught = [k for k in looks if k is not None]
    assert len(caught) / runs >= power
    assert sorted(caught)[len(caught) // 2] <= by_look  # the median look


def test_e_value_reads_the_canary_share_of_events_not_their_rate():
    assert release.e_value([[60, 0, 0, 240, 0, 0]], "5xx") == 1.0  # no event: no evidence
    stable_only = release.e_value([[60, 0, 0, 240, 3, 0]], "5xx")
    assert stable_only < 1.0  # the stable's errors are evidence for the canary
    one, three = ([[60, k, 0, 240, 0, 0]] for k in (1, 3))
    assert 1.0 < release.e_value(one, "5xx") < release.e_value(three, "5xx")
    # One canary error at 20% is not enough to roll back (L0 aborts on it: noise)
    assert release.judge(one, 4).outcome == "continue"


def test_the_absolute_floor_blocks_a_significant_but_negligible_excess():
    tiny = [[1_000_000, 1500, 0, 10_000_000, 9000, 0]]  # 0.15% vs 0.09%: significant, < 0.1 pt
    v = release.judge(tiny, 1)
    assert v.e_values["5xx"] >= 2 / release.ALPHA and v.excess["5xx"] < release.FLOOR
    assert v.outcome == "hold"
    clear = [[1_000_000, 3000, 0, 10_000_000, 10_000, 0]]  # 0.3% vs 0.1%
    assert release.judge(clear, 1).outcome == "rollback"


def inp(windows=None, looks=release.LOOKS_PER_STEP, **params):
    metrics = {"windows": windows} if windows is not None else {}
    return AgentInput(
        incident_id="i-1",
        metrics=metrics,
        params={"ts": T, "canary_hash": "new", "looks_in_step": looks, **params},
    )


WORSE = [[60, 6, 0, 240, 0, 0], [60, 6, 0, 240, 0, 0]]
ALIKE = [[60, 0, 0, 240, 0, 0], [60, 0, 0, 240, 1, 0]]


@pytest.mark.parametrize(
    ("given", "action"),
    [
        (inp(WORSE), "rollout_abort"),  # roll back before 100%
        (inp(ALIKE), "rollout_promote"),  # continue after the step's looks
        (inp(ALIKE, looks=2), None),  # hold: no proposal, the Rollout stays paused
        (inp([[0, 0, 0, 240, 0, 0]]), "escalate_to_human"),  # an unmeasurable canary
        (inp(), "escalate_to_human"),  # no counts: nothing to test
        (inp(ALIKE, aborted=True), "rollout_undo"),  # aborted: back to the previous revision
    ],
)
def test_decision_mapping(given, action):
    out = release.Release().run(given, level("L3"))
    assert [p.action for p in out.proposals] == ([action] if action else [])
    assert {p.action for p in out.proposals} <= AGENT_ACTIONS[AgentName.RELEASE]
    if action in ("rollout_abort", "rollout_promote"):
        (p,) = out.proposals
        scores = out.label_scores[p.id]
        assert set(scores) == {"rollback", "continue"} and p.confidence == 0.0  # D9
        assert (scores["rollback"] > 0.5) == (action == "rollout_abort")
    if action is None:
        assert out.evidence and "hold" in out.evidence[0].claim


def test_l0_judges_the_canary_alone_on_the_frozen_thresholds():
    noisy = [[60, 0, 0, 240, 0, 0], [60, 1, 0, 240, 0, 0]]  # 1 error in 120: 0.8%
    assert release.judge_l0(noisy, 4).outcome == "continue"
    assert release.judge_l0([[60, 1, 0, 240, 0, 0], [50, 1, 0, 240, 0, 0]], 1).outcome == (
        "rollback"
    )  # 2 in 110: over 1%
    out = release.Release().run(inp(noisy), level("L0"))
    assert [p.action for p in out.proposals] == ["rollout_promote"]


def make(tmp_path, name):
    db = tmp_path / f"{name}.db"
    audit = AuditLog(db, level=name)
    return Orchestrator(
        cfg=level(name),
        record=IncidentRecord(db),
        executor=Executor(audit=audit, dry_run=True),
        audit=audit,
    )


def rollout_step(**metrics):
    from flowguard.contracts import Event

    return Event(
        id="e-1",
        type="rollout_step",
        service="podinfo",
        environment="staging",
        payload={"metrics": {"windows": WORSE, **metrics}, "in_canary": True,
                 "params": {"ts": T, "canary_hash": "new", "looks_in_step": 1}},
    )  # fmt: skip


def test_p1_holds_a_rollback_when_the_node_is_under_stress(tmp_path):
    stressed = rollout_step(**{"node:cpu_ratio": 0.97, "node:memory_ratio": 0.5})
    l4 = make(tmp_path, "L4")
    out = l4.handle(stressed)
    assert [p.action for p in out["output"].proposals] == ["escalate_to_human"]
    asked = {r.to_agent for r in l4.record.requests(out["incident_id"])}
    assert asked == {AgentName.MONITOR, AgentName.DIAGNOSE}
    claims = [e.claim for e in l4.record.evidence(out["incident_id"])]
    assert any(c.startswith("node under stress: cpu 0.97") for c in claims)
    l3 = make(tmp_path, "L3")  # Release alone: no question asked, it rolls back
    out = l3.handle(stressed)
    assert [p.action for p in out["output"].proposals] == ["rollout_abort"]
    assert l3.record.requests(out["incident_id"]) == []


@pytest.mark.parametrize(
    "node",
    [
        {"node:cpu_ratio": 0.3, "node:memory_ratio": 0.4},  # normal
        {},  # no node series: "stress unknown" is not infrastructure evidence
    ],
)
def test_p1_does_not_hold_a_rollback_without_infrastructure_evidence(tmp_path, node):
    out = make(tmp_path, "L4").handle(rollout_step(**node))
    assert [p.action for p in out["output"].proposals] == ["rollout_abort"]


# ---------------------------------------------------------------- windows from Prometheus


def counter(labels, points):
    return {"metric": labels, "values": [[T + t, str(v)] for t, v in points]}


def raw_canary():
    base = {"namespace": "staging", "path": "root", "app": "podinfo"}
    new, old = (
        {**base, "rollouts_pod_template_hash": h, "pod": f"podinfo-{h}-0"} for h in ("new", "old")
    )
    return {
        "podinfo_requests": [
            # a reset at 45 s (pod restart): 0..40 then 0..20 counts 60
            counter({**new, "status": "200"}, [(0, 0), (15, 20), (30, 40), (45, 10), (60, 20)]),
            counter({**new, "status": "500"}, [(0, 0), (15, 1), (30, 2), (45, 1), (60, 3)]),
            counter({**old, "status": "200"}, [(0, 0), (15, 80), (30, 160), (45, 240), (60, 320)]),
            counter({**new, "status": "200", "path": "readyz"}, [(0, 0), (60, 999)]),  # not /
        ],
        "podinfo_latency_buckets": [
            counter({**new, "le": "+Inf"}, [(0, 0), (30, 42), (60, 66)]),
            counter({**new, "le": "0.5"}, [(0, 0), (30, 40), (60, 60)]),
            counter({**old, "le": "+Inf"}, [(0, 0), (30, 160), (60, 320)]),
            counter({**old, "le": "0.5"}, [(0, 0), (30, 160), (60, 320)]),
        ],
    }


def test_windows_add_up_across_boundaries_and_resets():
    idx = series.index(raw_canary())
    ws = release.windows(idx, T, T + 60, "staging", "new", "old")
    assert len(ws) == 2
    assert ws[0] == [42.0, 2.0, 2.0, 160.0, 0.0, 0.0]
    # 200s: 20 + 20 + 10 (after the reset) + 10; 500s: 1 + 1 + 1 + 2; slow: 66 - 60
    assert [sum(w[i] for w in ws) for i in range(6)] == [65.0, 5.0, 6.0, 320.0, 0.0, 0.0]


def test_step_event_carries_the_windows_and_the_p1_rows():
    raw = raw_canary()
    raw["node_cpu"] = [counter({"id": "/"}, [(0, 0), (30, 120), (60, 240)])]
    raw["node_allocatable"] = [counter({"resource": "cpu"}, [(0, 8), (60, 8)])]
    event = release.step_event(series.index(raw), T + 60, "staging", "new", "old", T, 2, weight=20)
    assert event.type == "rollout_step" and event.environment == "staging"
    m, params = event.payload["metrics"], event.payload["params"]
    assert m["canary_requests"] == 65.0 and len(m["windows"]) == 2
    assert m["node:cpu_ratio"] == pytest.approx(0.5)
    assert params == {"ts": T + 60, "canary_hash": "new", "looks_in_step": 2, "weight": 20,
                      "aborted": False}  # fmt: skip
    assert event.payload["in_canary"] is True
