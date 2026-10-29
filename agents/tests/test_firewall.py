"""Information firewall (D25): nothing under agents/ may reach what only the experiment knows.

- No import of a module from scenarios/ or evaluation/ (statement, __import__ or importlib).
- No path into scenarios/ or evaluation/ and no reference to the ground-truth DB.
- No catalog action an agent may execute reaches the fault admin endpoint, the release
  Secrets or podinfo's /env: the only way in, kubectl exec, is prohibited.
"""

import ast
from pathlib import Path

import pytest
import yaml

from flowguard import catalog
from flowguard.contracts import RiskTier

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "agents"
EVALUATION_SIDE = ("scenarios", "evaluation")
TRUTH_WORDS = ("groundtruth", "ground_truth", "ground truth", "flowguard_groundtruth_db")


def evaluation_modules() -> set[str]:
    names = set(EVALUATION_SIDE)
    for top in EVALUATION_SIDE:
        for path in (ROOT / top).rglob("*.py"):
            names.add(path.stem)
        names |= {p.name for p in (ROOT / top).iterdir() if p.is_dir()}
    return names - {"__init__", "tests", "conftest", "__pycache__"}


def violations(source: str, forbidden_modules: set[str]) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        modules = []
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules = [node.module]
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in ("import_module", "__import__") and isinstance(node.args[0].value, str):
                modules = [node.args[0].value]
        found += [f"imports {m}" for m in modules if m.split(".")[0] in forbidden_modules]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.lower()
            if any(w in text for w in TRUTH_WORDS):
                found.append(f"mentions the ground truth: {node.value[:60]!r}")
            for top in EVALUATION_SIDE:
                if text == top or f"{top}/" in text or f"{top}\\" in text:
                    found.append(f"path into {top}/: {node.value[:60]!r}")
    return found


def agent_sources():
    for path in sorted(AGENTS.rglob("*.py")):
        if "__pycache__" not in path.parts and path.resolve() != Path(__file__).resolve():
            yield path


@pytest.mark.parametrize(
    "source",
    [
        "import runner",
        "from recorder import record",
        "import groundtruth as g",
        "from evaluation import loadgen",
        "importlib.import_module('ci_failures')",
        "__import__('leakage')",
        "p = ROOT / 'evaluation' / 'runs'",
        "sys.path.insert(0, 'scenarios/dev')",
        "db = 'evaluation/runs/groundtruth.db'",
        "os.environ['FLOWGUARD_GROUNDTRUTH_DB']",
        "x = 'ground truth'",
    ],
)
def test_the_checker_catches_every_route(source):
    assert violations(source, evaluation_modules())


def test_the_checker_ignores_ordinary_code():
    ok = "import json\nfrom flowguard.audit import AuditLog\nlevel = 'L0'  # the evaluation level"
    assert violations(ok, evaluation_modules()) == []


def test_agents_code_never_reaches_the_evaluation_side():
    forbidden = evaluation_modules()
    assert {"runner", "recorder", "groundtruth", "ci_failures", "leakage"} <= forbidden
    problems = {
        str(p.relative_to(ROOT)): v
        for p in agent_sources()
        if (v := violations(p.read_text(encoding="utf-8"), forbidden))
    }
    assert problems == {}


def test_no_executable_action_reaches_faults_secrets_or_env():
    executable = {e.action for e in catalog.CATALOG.values() if e.tier != RiskTier.PROHIBITED}
    for word in ("exec", "fault", "secret", "env", "http", "curl", "shell"):
        assert not [a for a in executable if word in a], word
    assert catalog.CATALOG["kubectl_exec"].tier == RiskTier.PROHIBITED
    assert catalog.CATALOG["delete_secret"].tier == RiskTier.PROHIBITED


def test_no_agent_query_can_reach_flowguard_metrics():
    """D25: the Prometheus the agents read also scrapes the dashboard's exporters, and the
    scored results there come from the evaluation side. Every selector an agent uses names one
    metric literally, never a flowguard_* one; no PromQL in the modules that read Prometheus
    (the Monitor and its P1 answers, Release, the service) names one or matches on __name__."""
    import re

    from flowguard import series, service

    selectors = {**series.SELECTORS, **service.ROLLOUT_SELECTORS}
    assert selectors and all(series.checked_selector(s) == s for s in selectors.values())
    pkg = AGENTS / "flowguard"
    for name in ("series", "service", "monitor", "release"):
        tree = ast.parse((pkg / f"{name}.py").read_text(encoding="utf-8"))
        strings = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        assert not [s for s in strings if re.search(r"flowguard_[a-z]|__name__\s*=~", s)], name


@pytest.mark.parametrize(
    "selector",
    [
        "flowguard_result",
        'flowguard_result{objective="DSO1"}',
        "flowguard_proposals_total",  # the agents' own audit counts are not an input either
        '{__name__=~"flowguard_.*"}',
        'up{__name__=~"flowguard_result"}',
        "sum(flowguard_result)",
    ],
)
def test_prometheus_source_refuses_flowguard_metrics(selector):
    from flowguard import series

    with pytest.raises(ValueError):
        series.PrometheusSource(get=lambda url: {}, selectors={"x": selector})


def test_the_fault_admin_port_has_no_service():
    docs = yaml.safe_load_all((ROOT / "infra/apps/podinfo/base/podinfo.yaml").read_text())
    services = [d for d in docs if d and d.get("kind") == "Service"]
    ports = {p["port"] for s in services for p in s["spec"]["ports"]}
    assert services and 9797 not in ports
