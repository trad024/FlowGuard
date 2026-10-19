"""Static checks on the step 1 manifests: they keep the cluster inside the laptop budget
and consistent with the k3d port map. They run without a cluster."""

import json
from pathlib import Path

import yaml

INFRA = Path(__file__).resolve().parents[2] / "infra"

# Memory limits of every workload container, summed, must leave room for k3s itself
# (about 0.5 GB) inside the 4 GB budget (blueprint section 10).
WORKLOAD_LIMIT_BUDGET_MIB = 1536


def manifests():
    for path in INFRA.rglob("*.yaml"):
        if path.name == "kustomization.yaml" or "k3d" in path.parts:
            continue
        for doc in yaml.safe_load_all(path.read_text()):
            if doc:
                yield path, doc


def containers():
    for path, doc in manifests():
        if doc["kind"] == "Deployment":
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


def test_workload_memory_fits_the_laptop_budget():
    # podinfo runs once per environment, so it counts twice.
    total = 0.0
    for _, doc, c in containers():
        copies = 2 if doc["metadata"]["name"] == "podinfo" else 1
        total += copies * to_mib(c["resources"]["limits"]["memory"])
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
