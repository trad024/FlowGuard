"""Static checks on the infra manifests: they keep the cluster inside the laptop budget, consistent
with the k3d port map, and keep the step 2 canary and its frozen L0 analysis as approved. They run
without a cluster."""

import json
import math
import re
from pathlib import Path

import yaml

INFRA = Path(__file__).resolve().parents[2] / "infra"

# Memory limits of every workload container, summed at peak pod count (a canary in progress),
# must leave room for k3s, containerd and the Docker VM (about 1 GB) plus a 0.5 GB margin inside
# the 4 GB budget (blueprint section 8). Limits are ceilings: check-resources measures real use.
WORKLOAD_LIMIT_BUDGET_MIB = 2560
ENVIRONMENTS = ("staging", "prod")
ARGO_ROLLOUTS = INFRA / "argo-rollouts"


def manifests():
    for path in INFRA.rglob("*.yaml"):
        # k3d is not a Kubernetes manifest; argo-rollouts is upstream plus a partial patch
        if path.name == "kustomization.yaml" or {"k3d", "argo-rollouts"} & set(path.parts):
            continue
        for doc in yaml.safe_load_all(path.read_text()):
            if doc:
                yield path, doc


def containers():
    for path, doc in manifests():
        if doc["kind"] in ("Deployment", "Rollout"):
            for c in doc["spec"]["template"]["spec"]["containers"]:
                yield path, doc, c


def to_mib(quantity: str) -> float:
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024}
    for suffix, factor in units.items():
        if quantity.endswith(suffix):
            return float(quantity[: -len(suffix)]) * factor
    raise ValueError(f"unexpected memory quantity {quantity}")


def test_every_image_is_pinned_to_a_version():
    for path, _, c in containers():
        image = c["image"]
        assert ":" in image.split("/")[-1], f"{path}: {image} has no tag"
        assert not image.endswith(":latest"), f"{path}: {image} uses latest"


def test_every_container_has_a_memory_limit():
    for path, _, c in containers():
        assert "memory" in c.get("resources", {}).get("limits", {}), f"{path}: {c['name']}"


def peak_pods(doc) -> int:
    """Pods a workload can run at once: replicas plus the canary surge, in every environment."""
    replicas = doc["spec"].get("replicas", 1)
    if doc["kind"] != "Rollout":
        return replicas
    surge = str(doc["spec"]["strategy"]["canary"]["maxSurge"])
    extra = math.ceil(replicas * int(surge[:-1]) / 100) if surge.endswith("%") else int(surge)
    return (replicas + extra) * len(ENVIRONMENTS)


def controller_patch():
    return yaml.safe_load((ARGO_ROLLOUTS / "controller-resources.yaml").read_text())


def test_workload_memory_fits_the_laptop_budget():
    total = sum(
        peak_pods(doc) * to_mib(c["resources"]["limits"]["memory"]) for _, doc, c in containers()
    )
    controller = controller_patch()["spec"]["template"]["spec"]["containers"][0]
    total += to_mib(controller["resources"]["limits"]["memory"])
    assert total <= WORKLOAD_LIMIT_BUDGET_MIB, f"{total:.0f} MiB of limits"


def test_prometheus_keeps_one_day():
    args = next(c["args"] for _, _, c in containers() if c["name"] == "prometheus")
    assert "--storage.tsdb.retention.time=1d" in args


def test_node_ports_match_the_k3d_port_map():
    cluster = yaml.safe_load((INFRA / "k3d" / "cluster.yaml").read_text())
    mapped = {int(p["port"].split(":")[1]) for p in cluster["ports"]}
    used = {
        port["nodePort"]
        for _, doc in manifests()
        if doc["kind"] == "Service"
        for port in doc["spec"]["ports"]
        if "nodePort" in port
    }
    # podinfo prod's 30899 is set by a kustomize patch, not in the base file
    assert used | {30899} == mapped


def test_both_environments_deploy_podinfo():
    for env in ("staging", "prod"):
        kust = yaml.safe_load((INFRA / "apps" / "podinfo" / env / "kustomization.yaml").read_text())
        assert kust["namespace"] == env


def test_dashboard_is_valid_json_with_the_six_signals():
    dash = json.loads((INFRA / "monitoring" / "grafana" / "podinfo-dashboard.json").read_text())
    titles = {p["title"] for p in dash["panels"]}
    assert titles == {
        "Request rate",
        "Error rate (5xx share)",
        "Latency p95",
        "CPU",
        "Memory (working set)",
        "Container restarts",
    }


def podinfo_rollout():
    return next(doc for _, doc in manifests() if doc["kind"] == "Rollout")


def test_argo_rollouts_is_pinned_and_capped():
    kust = yaml.safe_load((ARGO_ROLLOUTS / "kustomization.yaml").read_text())
    remote = [r for r in kust["resources"] if r.startswith("https://")]
    assert remote == [
        "https://github.com/argoproj/argo-rollouts/releases/download/v1.10.1/install.yaml"
    ]
    limits = controller_patch()["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert "memory" in limits["limits"]


def test_podinfo_is_a_canary_rollout_20_50_100_with_l0_analysis():
    spec = podinfo_rollout()["spec"]
    assert spec["replicas"] == 10  # option A: 20% and 50% are whole pods
    steps = spec["strategy"]["canary"]["steps"]
    kinds = [next(iter(step)) for step in steps]
    assert kinds == ["setWeight", "pause", "analysis"] * 2 + ["setWeight"]
    assert [s["setWeight"] for s in steps if "setWeight" in s] == [20, 50, 100]
    for step in steps:
        if "analysis" in step:
            assert step["analysis"]["templates"] == [{"templateName": "podinfo-l0"}]


def test_agents_mode_canary_keeps_the_steps_and_waits_for_release_instead_of_l0():
    podinfo = INFRA / "apps" / "podinfo"
    component = yaml.safe_load((podinfo / "agents-mode" / "kustomization.yaml").read_text())
    assert component["kind"] == "Component"
    (patch,) = component["patches"]
    assert patch["target"] == {"kind": "Rollout", "name": "podinfo"}
    (op,) = yaml.safe_load(patch["patch"])
    assert (op["op"], op["path"]) == ("replace", "/spec/strategy/canary/steps")
    # The L0 steps with each inline analysis replaced by a pause until Release decides
    l0 = podinfo_rollout()["spec"]["strategy"]["canary"]["steps"]
    assert op["value"] == [{"pause": {}} if "analysis" in s else s for s in l0]
    for env in ENVIRONMENTS:
        overlay = yaml.safe_load((podinfo / f"{env}-agents" / "kustomization.yaml").read_text())
        assert overlay["resources"] == [f"../{env}"]
        assert overlay["components"] == ["../agents-mode"]
    # The deployed default stays L0
    deployed = yaml.safe_load((INFRA / "kustomization.yaml").read_text())["resources"]
    assert not any("agents" in r for r in deployed)


def test_the_agent_service_reader_can_only_read_rollouts():
    docs = list(yaml.safe_load_all((INFRA / "base" / "flowguard-reader.yaml").read_text()))
    roles = [d for d in docs if d["kind"] in ("Role", "ClusterRole")]
    assert {r["kind"] for r in roles} == {"Role"}  # namespaced, no cluster-wide rights
    assert {r["metadata"]["namespace"] for r in roles} == set(ENVIRONMENTS)
    for role in roles:
        assert role["rules"] == [
            {"apiGroups": ["argoproj.io"], "resources": ["rollouts"],
             "verbs": ["get", "list", "watch"]}
        ]  # fmt: skip
    bindings = [d for d in docs if d["kind"] == "RoleBinding"]
    assert {b["metadata"]["namespace"] for b in bindings} == set(ENVIRONMENTS)
    assert all(b["subjects"][0]["name"] == "flowguard-reader" for b in bindings)
    base = yaml.safe_load((INFRA / "base" / "kustomization.yaml").read_text())
    assert "flowguard-reader.yaml" in base["resources"]


def test_l0_thresholds_are_frozen():
    # Changing these values changes the DSO3 baseline: update the blueprint first.
    template = next(doc for _, doc in manifests() if doc["kind"] == "AnalysisTemplate")
    metrics = {m["name"]: m for m in template["spec"]["metrics"]}
    assert metrics["error-ratio-5xx"]["successCondition"].endswith("result[0] <= 0.01")
    assert metrics["latency-p99-seconds"]["successCondition"].endswith("result[0] <= 0.5")
    for m in metrics.values():
        assert (m["interval"], m["count"], m["failureLimit"]) == ("30s", 4, 1)
        query = m["provider"]["prometheus"]["query"]
        # Canary pods only, user traffic only
        assert 'rollouts_pod_template_hash="{{args.canary-hash}}"' in query
        assert 'path="root"' in query


def test_prometheus_keeps_the_canary_hash_label():
    config = (INFRA / "monitoring" / "prometheus" / "prometheus.yaml").read_text()
    assert re.search(
        r"__meta_kubernetes_pod_label_rollouts_pod_template_hash\]\s+"
        r"target_label: rollouts_pod_template_hash",
        config,
    )


def test_podinfo_metrics_and_liveness_avoid_the_fault_middleware():
    # podinfo's fault flags act on its main port; its metrics port has none of that middleware.
    # A bad version must neither hide its own metrics nor be restarted by its liveness probe.
    template = podinfo_rollout()["spec"]["template"]
    container = template["spec"]["containers"][0]
    assert template["metadata"]["annotations"]["prometheus.io/port"] == "9797"
    assert "--port-metrics=9797" in container["command"]
    assert container["livenessProbe"]["httpGet"]["port"] == "metrics"
    # deploy-version replaces the whole command, so it must keep the flag
    for script in ("deploy-version.ps1", "deploy-version.sh"):
        assert "--port-metrics=9797" in (INFRA / "scripts" / script).read_text()


def test_runtime_fault_admin_is_enabled_in_staging_only():
    def env_names(overlay):
        kust = yaml.safe_load(
            (INFRA / "apps" / "podinfo" / overlay / "kustomization.yaml").read_text()
        )
        return "PODINFO_FAULT_ADMIN" in json.dumps(kust.get("patches", []))

    assert env_names("staging")
    assert not env_names("prod")


def test_every_release_gets_a_settings_secret_and_no_fault_flags():
    # D25: fault settings never appear in the pod spec; each release mounts its own Secret
    for script in ("deploy-version.ps1", "deploy-version.sh"):
        text = (INFRA / "scripts" / script).read_text()
        assert "--fault-" not in text
        assert "podinfo-release-" in text and "envFrom" in text and "secretRef" in text
        assert "release-settings" in text  # the label the deployer's clean-up selects on
        for key in ("ERROR_RATE", "LATENCY_MS", "JITTER_MS", "FRACTION"):
            assert f"PODINFO_FAULT_{key}" in text


def test_dso1_l0_alert_rules_are_frozen():
    # Changing these values changes the DSO1 baseline: update the blueprint first.
    rules_file = INFRA / "monitoring" / "prometheus" / "l0-alerts.yml"
    (group,) = yaml.safe_load(rules_file.read_text())["groups"]
    rules = {r["alert"]: r for r in group["rules"]}
    expected = {
        "HighErrorRatio": (") > 0.05", "1m"),
        "HighLatencyP99": ("> 1", "1m"),
        "ContainerOOMKilled": ('reason="OOMKilled"}[10m]) == 1', None),
        "PodCrashLooping": ('reason="CrashLoopBackOff"}[5m]) >= 1', "2m"),
        "ContainerHighMemory": ("> 0.8", "2m"),
    }
    assert set(rules) == set(expected)
    for name, (threshold, for_) in expected.items():
        assert threshold in rules[name]["expr"], name
        assert rules[name].get("for") == for_, name
    # Built into the rules ConfigMap that Prometheus loads
    kust = yaml.safe_load((INFRA / "monitoring" / "kustomization.yaml").read_text())
    generated = {g["name"]: g["files"] for g in kust["configMapGenerator"]}
    assert generated["prometheus-rules"] == ["prometheus/l0-alerts.yml"]
