"""Agents-vs-L0 dashboard: results flattened to gauges with the labels the panels select on,
the dashboard provisioned, on the Prometheus data source, and every query naming a metric that
one of the two exporters (or Prometheus itself) produces."""

import json
import re
from pathlib import Path

import pytest
import yaml

import results_exporter as rx
from flowguard import exporter

ROOT = Path(__file__).resolve().parents[2]
GRAFANA = ROOT / "infra" / "monitoring" / "grafana"
DASHBOARD = GRAFANA / "flowguard-agents-vs-l0.json"

REPLAY = {
    "recordings": 3,
    "runtime": {
        "label": "dev, in-sample",
        "L0": {
            "recall": {"k": 7, "n": 15, "rate": 0.467, "ci95": [0.248, 0.699]},
            "mttd_s_median": 88.0,
            "by_band": {
                "near": {
                    "recall": {"k": 2, "n": 5, "rate": 0.4, "ci95": [0.1, 0.8]},
                    "mttd_s_median": 113.0,
                },
            },  # fmt: skip
            "false_alerts_per_hour": {
                "episodes": 1,
                "hours": 2.0,
                "rate": 0.5,
                "ci95": [0.013, 2.786],
            },  # fmt: skip
        },
        "L1": {
            "recall": {"k": 12, "n": 15, "rate": 0.8, "ci95": [0.548, 0.93]},
            "mttd_s_median": None,
        },  # fmt: skip
        "L1_vs_L0": {"mcnemar_exact_p": 0.0625, "mttd_gain_s_median": 40.0},
        "without_carry_over": {
            "L0": {"recall": {"k": 5, "n": 12, "rate": 0.417, "ci95": [0.193, 0.68]}}
        },
    },
    "ci": {"L0": {"recall": {"k": 1, "n": 1, "rate": 1.0, "ci95": [0.207, 1.0]}}},
}


def series(runs: Path) -> dict[tuple, float]:
    out = {}
    for line in rx.render(runs).splitlines():
        if line.startswith("flowguard_result{"):
            labels = dict(re.findall(r'(\w+)="([^"]*)"', line))
            out[tuple(sorted(labels.items()))] = float(line.rsplit(" ", 1)[1])
    return out


def find(found, **want):
    return [v for k, v in found.items() if want.items() <= dict(k).items()]


def test_results_are_flattened_with_level_band_variant_and_stat(tmp_path):
    (tmp_path / "dso1-replay.json").write_text(json.dumps(REPLAY))
    (tmp_path / "l0-baseline.json").write_text(
        json.dumps({"DSO3": {"bad_caught": {"k": 9, "n": 10, "rate": 0.9, "ci95": [0.6, 0.98]},
                             "bad_caught_by_band": {"above": {"k": 3, "n": 3, "rate": 1.0,
                                                              "ci95": [0.4, 1.0]}}}})
    )  # fmt: skip
    got = series(tmp_path)
    base = {"source": "dso1-replay", "objective": "DSO1 runtime", "variant": "primary"}
    assert find(got, **base, level="L1", band="", metric="recall", stat="value") == [0.8]
    assert find(got, **base, level="L0", band="near", metric="recall", stat="ci_high") == [0.8]
    assert find(got, **base, level="L0", band="near", metric="mttd_s_median") == [113.0]
    fa = dict(level="L0", metric="false_alerts_per_hour")
    assert sorted(find(got, **base, **fa)) == [0.013, 0.5, 1.0, 2.0, 2.786]  # k=1, n=2 hours
    assert find(got, variant="without_carry_over", level="L0", stat="k") == [5.0]
    assert find(got, objective="DSO1 CI", level="L0", stat="value") == [1.0]
    assert find(got, metric="mcnemar_exact_p") == [0.0625]
    assert not find(got, level="L1", metric="mttd_s_median")  # null is not exported
    assert find(got, objective="DSO3", level="L0", band="", metric="bad_caught", stat="n") == [10]
    assert find(got, objective="DSO3", band="above", metric="bad_caught", stat="value") == [1.0]
    assert "flowguard_result_files 2" in rx.render(tmp_path)
    assert "flowguard_result_files 0" in rx.render(tmp_path / "missing")


def test_the_dashboard_is_provisioned_and_queries_only_exported_metrics():
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    podinfo = json.loads((GRAFANA / "podinfo-dashboard.json").read_text(encoding="utf-8"))
    assert dash["uid"] != podinfo["uid"]
    kust = yaml.safe_load((ROOT / "infra" / "monitoring" / "kustomization.yaml").read_text())
    files = {g["name"]: g["files"] for g in kust["configMapGenerator"]}["grafana-dashboards"]
    assert "grafana/flowguard-agents-vs-l0.json" in files
    known = exporter.NAMES | {"flowguard_result", "flowguard_result_files", "ALERTS"}
    rows = [p["title"] for p in dash["panels"] if p["type"] == "row"]
    assert [r.split(":")[0] for r in rows] == ["DSO1 detection", "DSO3 canary decisions",
                                              "DSO6 gate"]  # fmt: skip
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids))
    for p in dash["panels"]:
        if p["type"] == "row":
            continue
        assert p["datasource"] == {"type": "prometheus", "uid": "prometheus"}, p["title"]
        for t in p["targets"]:
            names = set(re.findall(r"\b(flowguard_[a-z_]+|ALERTS)\b", t["expr"]))
            assert names and names <= known, (p["title"], names - known)
    selects = " ".join(t["expr"] for p in dash["panels"] for t in p.get("targets", []))
    for label in ('level=~"L0|L1"', 'objective="DSO1 runtime"', "outcome", "verdict"):
        assert label in selects


def test_every_scored_result_is_a_flowguard_result_series(tmp_path):
    """The runner's start check counts flowguard_result.* series: it must see all of them."""
    (tmp_path / "dso1-replay.json").write_text(json.dumps(REPLAY))
    names = {
        line.split("{")[0].split(" ")[0]
        for line in rx.render(tmp_path).splitlines()
        if line and not line.startswith("#")
    }
    assert names and all(n.startswith("flowguard_result") for n in names), names


def test_the_results_exporter_refuses_to_start_during_a_run(tmp_path, monkeypatch, capsys):
    lock = tmp_path / "state" / "run.lock"
    monkeypatch.setattr(rx.recorder, "RUN_LOCK", lock)
    lock.parent.mkdir()
    lock.write_text('{"run_id": "r-0000abcd"}')
    with pytest.raises(SystemExit) as exc:
        rx.main(["--port", "0"])
    assert exc.value.code == 2 and "run.lock" in capsys.readouterr().err
    lock.unlink()
    with pytest.raises(SystemExit):  # and never on every interface
        rx.main(["--host", "0.0.0.0", "--port", "0"])
    assert "all interfaces" in capsys.readouterr().err


def test_the_demo_never_binds_the_exporters_to_every_interface():
    demo = (ROOT / "docs" / "demo.md").read_text(encoding="utf-8")
    assert not re.search(r"--host\s+(0\.0\.0\.0|::)", demo)
    hosts = re.findall(r"(?:flowguard\.exporter|results_exporter\.py) --host (\S+)", demo)
    assert len(hosts) == 4 and all(h in ("$gw", '"$gw"') for h in hosts), hosts


def test_prometheus_scrapes_both_exporters():
    config = (ROOT / "infra" / "monitoring" / "prometheus" / "prometheus.yaml").read_text()
    prom = yaml.safe_load(next(d for d in yaml.safe_load_all(config) if d["kind"] == "ConfigMap")
                          ["data"]["prometheus.yml"])  # fmt: skip
    (job,) = [j for j in prom["scrape_configs"] if j["job_name"] == "flowguard"]
    targets = job["static_configs"][0]["targets"]
    assert targets == [f"host.k3d.internal:{exporter.PORT}", f"host.k3d.internal:{rx.PORT}"]
