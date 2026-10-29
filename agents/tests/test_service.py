"""Live service: one poll gives a Monitor tick per namespace and, for a canary held mid-rollout,
one look by Release per poll after the step's warm-up, until it proposes; the gate, the approver
and a dry-run executor do the rest. Rollout state comes from Prometheus series only; all are
fakes here."""

import json

import pytest

from flowguard import series
from flowguard.approval import SimulatedApprover
from flowguard.audit import AuditLog
from flowguard.contracts import AgentName
from flowguard.executor import Executor
from flowguard.incidents import IncidentRecord
from flowguard.orchestrator import Orchestrator, level
from flowguard.service import (
    RolloutReader,
    RolloutState,
    Service,
    abort_of,
    make_orchestrator,
    report,
    rollout_state,
)

T = 1_800_000_000.0


def requests(ns, pod_hash, pods, ok, errors):
    """Counters for `pods` pods of one hash: `ok` 200s and `errors` 5xx each over the minute."""
    out = []
    for i in range(pods):
        labels = {"namespace": ns, "pod": f"podinfo-{pod_hash}-{i}", "path": "root",
                  "app": "podinfo", "rollouts_pod_template_hash": pod_hash}  # fmt: skip
        out.append({"metric": {**labels, "status": "200"},
                    "values": [[T - 45, "0"], [T, str(ok)]]})  # fmt: skip
        out.append({"metric": {**labels, "status": "500"},
                    "values": [[T - 45, "0"], [T, str(errors)]]})  # fmt: skip
    return out


def rollout_series(states):
    """namespace -> (paused, canary hash, stable hash[, canary pods]) as Prometheus would show
    it: pods up per hash, ReplicaSet creation times, Argo Rollouts' phase."""
    raw = {"podinfo_up": [], "replicasets": [], "rollout_info": []}
    for ns, (paused, canary, stable, *rest) in states.items():
        n_canary = rest[0] if rest else 2
        pods = {stable: 10 - n_canary, canary: n_canary} if canary != stable else {stable: 10}
        for h, n in pods.items():
            for i in range(n):
                labels = {"namespace": ns, "pod": f"podinfo-{h}-{i}", "app": "podinfo",
                          "rollouts_pod_template_hash": h}  # fmt: skip
                raw["podinfo_up"].append({"metric": labels, "values": [[T - 10, "1"]]})
            created = T - 100 if h == canary and canary != stable else T - 86_400
            raw["replicasets"].append(
                {
                    "metric": {"namespace": ns, "replicaset": f"podinfo-{h}"},
                    "values": [[T - 10, str(created)]],
                }  # fmt: skip
            )
        phase = "Paused" if paused else ("Progressing" if canary != stable else "Healthy")
        raw["rollout_info"].append(
            {
                "metric": {"namespace": ns, "name": "podinfo", "phase": phase},
                "values": [[T - 10, "1"]],
            }  # fmt: skip
        )
    return raw


class Source:
    """The same cluster at every poll: sample times move with t (values stay)."""

    def __init__(self, raw):
        self.raw = raw

    def fetch(self, t):
        shifted = {
            name: [{**s, "values": [[ts + (t - T), v] for ts, v in s["values"]]} for s in result]
            for name, result in self.raw.items()
        }
        return series.index(shifted)


def make(tmp_path, raw, states, name="L1", approve=True, rollouts=None):
    db = tmp_path / "state.db"
    audit = AuditLog(db, level=name)
    orch = Orchestrator(
        cfg=level(name),
        record=IncidentRecord(db),
        executor=Executor(audit=audit, dry_run=True, kubectl=lambda a: "ok"),
        audit=audit,
        approver=SimulatedApprover(policy=lambda p: approve),
    )
    return Service(orch, Source({**raw, **rollout_series(states)}), rollouts=rollouts)


STEADY = {"staging": (False, "old", "old"), "prod": (False, "old", "old")}


def alert(ns, name="HighErrorRatio", at=T - 5):
    return [{"metric": {"alertname": name, "alertstate": "firing", "namespace": ns},
             "values": [[at, "1"]]}]  # fmt: skip


def test_a_quiet_poll_opens_nothing(tmp_path):
    svc = make(tmp_path, {"podinfo_requests": requests("staging", "old", 2, 60, 0)}, STEADY)
    outs = svc.tick(T)
    assert [o["event"].environment for o in outs] == ["staging", "prod"]  # one tick each
    assert all(o["output"].incident is False and "incident_id" not in o for o in outs)


def test_an_alert_opens_an_incident_followed_up_by_diagnose(tmp_path):
    raw = {"podinfo_requests": requests("staging", "old", 2, 60, 20), "alerts": alert("staging")}
    svc = make(tmp_path, raw, STEADY)
    out, prod = svc.tick(T)
    assert "incident_id" not in prod
    incident, _ = svc.orch.record.incident(out["incident_id"])
    assert incident.detector == "alert_rule" and incident.environment == "staging"
    assert out["agent"] == "diagnose"
    assert [p.action for p in out["output"].proposals] == ["escalate_to_human"]
    assert [d.outcome for d in out["decisions"]] == ["execute"]  # low tier: no human


def test_a_prod_fault_is_followed_up_as_prod(tmp_path):
    raw = {
        "podinfo_requests": requests("staging", "old", 2, 60, 0)
        + requests("prod", "old", 2, 60, 20),
        "alerts": alert("prod"),
    }
    svc = make(tmp_path, raw, STEADY)
    staging, prod = svc.tick(T)
    assert "incident_id" not in staging and not staging["event"].payload["alerts"]
    assert set(staging["event"].payload["metrics"]) == {
        "staging:error_ratio",
        "staging/podinfo-old-0:error_ratio",
        "staging/podinfo-old-1:error_ratio",
    }
    assert all(k.startswith("prod") for k in prod["event"].payload["metrics"])
    incident, _ = svc.orch.record.incident(prod["incident_id"])
    assert incident.environment == "prod" and prod["event"].environment == "prod"
    claims = [p.claim for p in svc.orch.record.evidence(prod["incident_id"])]
    assert "L0 alert HighErrorRatio firing (prod)" in claims
    assert [p.environment for p in prod["output"].proposals] == ["prod"]


def test_each_namespace_keeps_its_own_alert_episode(tmp_path):
    def poll(svc, t, *namespaces):
        svc.source.raw["alerts"] = [a for ns in namespaces for a in alert(ns)]  # 5 s before t
        return [o["event"].environment for o in svc.tick(t) if o.get("incident_id")]

    svc = make(tmp_path, {}, STEADY)
    assert poll(svc, T, "staging", "prod") == ["staging", "prod"]  # one incident each
    assert poll(svc, T + 30, "staging", "prod") == []  # same episodes
    assert poll(svc, T + 60, "prod") == []  # staging resolved, prod still under way
    assert poll(svc, T + 90, "staging", "prod") == ["staging"]  # a new staging episode only


def polls(svc, start, count):
    """The canary looks (events that are not Monitor ticks) of `count` polls 30 s apart."""
    return [
        [o for o in svc.tick(start + 30 * k) if o.get("agent") != "monitor"] for k in range(count)
    ]


def test_a_held_prod_canary_gets_a_look_per_poll_then_one_human_approved_promotion(tmp_path):
    raw = {
        "podinfo_requests": requests("prod", "old", 8, 60, 0) + requests("prod", "new", 2, 60, 0)
    }
    states = {"staging": (False, "old", "old"), "prod": (True, "new", "old")}
    svc = make(tmp_path, raw, states)
    looks = polls(svc, T, 6)
    assert looks[0] == []  # the step's 30 s warm-up: no look yet
    held = [k[0] for k in looks[1:4]]
    assert all("incident_id" not in o and o["output"].proposals == [] for o in held)  # holds
    assert [o["event"].payload["params"]["looks_in_step"] for o in held] == [1, 2, 3]
    assert held[0]["event"].environment == "prod" and held[0]["event"].type == "rollout_step"
    assert held[0]["event"].payload["params"]["weight"] == 20
    (step,) = looks[4]  # the 4th look: no evidence against the canary, promote
    (p,) = step["output"].proposals
    assert p.action == "rollout_promote" and p.environment == "prod"
    (d,) = step["decisions"]
    assert d.outcome == "ask_human" and d.human_verdict == "approved"  # medium in prod (D23)
    assert step["results"][0].success  # dry run
    assert looks[5] == []  # one proposal per step: a denial would not be asked again
    with svc.orch.record._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1  # holds: none


def test_a_worse_canary_is_rolled_back_at_its_first_look_and_each_step_restarts(tmp_path):
    raw = {
        "podinfo_requests": requests("staging", "old", 8, 60, 0)
        + requests("staging", "new", 2, 30, 30)
    }
    states = {"staging": (True, "new", "old"), "prod": (False, "old", "old")}
    svc = make(tmp_path, raw, states)
    looks = polls(svc, T, 3)
    assert looks[0] == []
    (out,) = looks[1]
    assert [p.action for p in out["output"].proposals] == ["rollout_abort"]
    assert looks[2] == []  # decided for this step
    # The same canary at its next step (5 pods) is looked at again from its own warm-up
    svc.source.raw.update(rollout_series({**states, "staging": (True, "new", "old", 5)}))
    looks = polls(svc, T + 90, 3)
    assert looks[0] == [] and looks[1][0]["event"].payload["params"] == {
        "ts": T + 120, "canary_hash": "new", "looks_in_step": 1, "weight": 50, "aborted": False
    }  # fmt: skip


def test_the_service_gates_release_with_its_calibrator(tmp_path):
    orch = make_orchestrator("L3", AuditLog(tmp_path / "a.db", level="L3"), approver=None)
    cal = orch.calibrators[AgentName.RELEASE]
    assert cal.calibrated  # agents/flowguard/models/release-calibration.json


def test_a_canary_that_is_not_held_is_left_to_its_steps(tmp_path):
    raw = {
        "podinfo_requests": requests("prod", "old", 8, 60, 0) + requests("prod", "new", 2, 60, 0)
    }
    states = {"staging": (False, "old", "old"), "prod": (False, "new", "old")}
    assert len(make(tmp_path, raw, states).tick(T)) == 2  # the two Monitor ticks only


def test_a_monitor_incident_mid_canary_goes_to_release_which_escalates_without_counts(tmp_path):
    raw = {
        "podinfo_requests": requests("staging", "old", 8, 60, 0)
        + requests("staging", "new", 2, 30, 30),
        "alerts": alert("staging"),
    }
    states = {"staging": (False, "new", "old"), "prod": (False, "old", "old")}
    svc = make(tmp_path, raw, states, approve=False)
    out, _ = svc.tick(T)  # not held: no look, but the tick is in_canary
    assert out["agent"] == "release"
    assert out["event"].payload["metrics"]["canary_5xx_ratio"] == pytest.approx(0.5)
    # The Monitor's tick carries ratios, not Release's count windows: a human decides
    assert [p.action for p in out["output"].proposals] == ["escalate_to_human"]
    assert [d.outcome for d in out["decisions"]] == ["execute"]


def test_rollout_state_from_prometheus():
    idx = series.index(rollout_series({"prod": (True, "new", "old")}))
    state = rollout_state(idx, T, "prod")
    assert state == RolloutState(True, "new", "old", T - 100, 20) and state.mid_canary
    steady = rollout_state(series.index(rollout_series({"prod": (False, "a", "a")})), T, "prod")
    assert steady == RolloutState(False, "a", "a") and not steady.mid_canary
    raw = rollout_series({"prod": (False, "new", "old")})
    raw["replicasets"] = []  # no creation times: the hash with fewer pods is the canary
    assert rollout_state(series.index(raw), T, "prod") == RolloutState(
        False, "new", "old", None, 20
    )
    assert rollout_state(series.index({}), T, "prod") == RolloutState(False, None, None)


def test_report_prints_one_line_per_incident(tmp_path, capsys):
    report({"output": None})
    assert capsys.readouterr().out == ""
    raw = {
        "podinfo_requests": requests("prod", "old", 2, 60, 0) + requests("prod", "new", 2, 60, 0)
    }
    svc = make(tmp_path, raw, {"staging": (False, "o", "o"), "prod": (True, "new", "old")})
    for k in range(4):  # warm-up, then three holds: nothing to report
        for out in svc.tick(T + 30 * k):
            report(out)
    assert capsys.readouterr().out == ""
    report(svc.tick(T + 120)[2])
    line = json.loads(capsys.readouterr().out)
    assert line["proposals"] == ["rollout_promote"] and line["gate"] == ["ask_human/approved"]


# ---------------------------------------------------------------- an aborted Rollout (D44)


def status(**fields):
    """A Rollout as the Kubernetes API returns it, reduced to its status."""
    return {"kind": "Rollout", "status": {"currentPodHash": "new", "stableRS": "old", **fields}}


@pytest.mark.parametrize(
    ("rollout", "expected"),
    [
        (status(abort=True, phase="Degraded"), ("new", "old")),
        (
            status(
                phase="Degraded", message="RolloutAborted: Rollout aborted update to revision 3"
            ),
            ("new", "old"),
        ),  # fmt: skip
        (status(abort=True), ("new", "old")),  # status.abort alone
        (
            status(phase="Degraded", message="ProgressDeadlineExceeded: ReplicaSet has timed out"),
            None,
        ),  # Degraded, but not aborted: not Release's to undo  # fmt: skip
        (status(phase="Paused", abort=False), None),
        (status(phase="Healthy", currentPodHash="old"), None),
        ({"status": {"abort": True}}, None),  # no revision to name
        (None, None),
    ],
)
def test_an_abort_is_read_from_the_rollout_status(rollout, expected):
    assert abort_of(rollout) == expected


def test_an_aborted_rollout_gets_one_undo_through_the_gate_and_a_human(tmp_path):
    current = {"prod": status(abort=True, phase="Degraded"), "staging": status(phase="Healthy")}
    svc = make(tmp_path, {}, STEADY, rollouts=lambda ns: current[ns])
    outs = [o for o in svc.tick(T) if o.get("agent") == "release"]
    (undo,) = outs
    assert undo["event"].environment == "prod" and undo["event"].payload["params"]["aborted"]
    (p,) = undo["output"].proposals
    assert (p.action, p.environment, p.params["canary_hash"]) == ("rollout_undo", "prod", "new")
    (d,) = undo["decisions"]
    assert (d.tier, d.outcome, d.human_verdict) == ("high", "ask_human", "approved")
    assert undo["results"][0].success  # dry run
    incident, _ = svc.orch.record.incident(undo["incident_id"])
    assert incident.source == "rollout" and incident.environment == "prod"
    # Not asked again while the same revision stays aborted (a denial would stand)
    assert not [o for o in svc.tick(T + 30) if o.get("agent") == "release"]
    # Back to healthy, then a new abort of that revision: asked again
    current["prod"] = status(phase="Healthy", currentPodHash="old")
    assert not [o for o in svc.tick(T + 60) if o.get("agent") == "release"]
    current["prod"] = status(abort=True, phase="Degraded")
    assert [
        o["output"].proposals[0].action for o in svc.tick(T + 90) if o.get("agent") == ("release")
    ] == ["rollout_undo"]


def test_without_a_status_reader_or_with_an_api_error_the_poll_goes_on(tmp_path, capsys):
    assert len(make(tmp_path / "a", {}, STEADY).tick(T)) == 2  # the two Monitor ticks only

    def broken(ns):
        raise OSError("connection refused")

    svc = make(tmp_path / "b", {}, STEADY, rollouts=broken)
    assert len(svc.tick(T)) == 2
    errors = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {e["rollout_status"] for e in errors} == {"staging", "prod"}


def test_the_reader_only_gets_the_rollout_with_its_token():
    seen = []

    def get(request, ca_file):
        seen.append((request.full_url, request.get_method(), request.headers, ca_file))
        return json.dumps(status(abort=True)).encode()

    reader = RolloutReader("https://127.0.0.1:6550/", "t0ken", "ca.crt", get=get)
    assert abort_of(reader("prod")) == ("new", "old")
    ((url, method, headers, ca),) = seen
    assert (
        url == "https://127.0.0.1:6550/apis/argoproj.io/v1alpha1/namespaces/prod/rollouts/podinfo"
    )
    assert method == "GET" and headers["Authorization"] == "Bearer t0ken" and ca == "ca.crt"
