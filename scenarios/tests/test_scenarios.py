"""Scenario specs, the runner's pure parts, and the CI failure generator applied to a copy of
the files it edits."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import ci_failures
import runner

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "scenarios" / "dev"


def spec(**over):
    base = {
        "id": "release-errors-05",
        "family": "release",
        "description": "x",
        "fault": {"kind": "http_errors", "delivery": "config", "error_rate": 0.05},
        "ground_truth": {"bad_release": True, "expected_l0": "abort"},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- specs


def test_every_dev_spec_is_valid_and_named_after_its_id():
    paths = sorted(DEV.glob("*.yaml"))
    assert len(paths) >= 12
    ids = []
    for path in paths:
        s = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert runner.validate_spec(s) == [], path.name
        assert path.stem == s["id"]
        ids.append(s["id"])
    assert len(ids) == len(set(ids))


def test_dev_specs_cover_every_ci_class_and_both_release_deliveries():
    specs = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in DEV.glob("*.yaml")]
    ci = {s["fault"]["kind"] for s in specs if s["family"] == "ci"}
    assert ci == set(ci_failures.CLASSES)
    deliveries = {s["fault"]["delivery"] for s in specs if s["family"] == "release"}
    assert deliveries == {"config", "commit"}
    assert any(s["ground_truth"]["expected_l0"] == "block" for s in specs)
    # Graded: at least one bad release that L0 is expected to miss, and a good control
    assert any(
        s["ground_truth"].get("bad_release") and s["ground_truth"]["expected_l0"] == "promote"
        for s in specs
    )
    assert any(not s["ground_truth"].get("bad_release", True) for s in specs)


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"family": "runtime"}, "not supported"),
        ({"family": "chaos"}, "family must be"),
        ({"fault": {"kind": "leaked_secret", "delivery": "config"}}, "fault.kind"),
        ({"fault": {"kind": "http_errors", "delivery": "teleport"}}, "fault.delivery"),
        ({"fault": {"kind": "http_errors", "delivery": "config", "error_rate": 2}}, "within 0..1"),
        ({"fault": {"kind": "latency", "delivery": "config", "latency_ms": -1}}, ">= 0"),
        ({"ground_truth": {"expected_l0": "maybe"}}, "expected_l0"),
        ({"environment": "prod"}, "only staging"),
        ({"id": "Bad ID"}, "id:"),
    ],
)
def test_invalid_specs_are_rejected_with_a_reason(over, message):
    errors = runner.validate_spec(spec(**over))
    assert any(message in e for e in errors), errors


def test_ci_specs_must_state_the_root_cause_as_ground_truth():
    s = spec(
        family="ci",
        fault={"kind": "dependency", "delivery": "commit"},
        ground_truth={"expected_l0": "ci_failure", "root_cause": "code"},
    )
    assert any("root_cause" in e for e in runner.validate_spec(s))


def test_missing_keys_are_all_reported():
    assert runner.validate_spec({"id": "x"}) == [
        "missing 'family'",
        "missing 'description'",
        "missing 'fault'",
        "missing 'ground_truth'",
    ]


def test_heldout_specs_are_gitignored():
    assert "scenarios/heldout/" in (ROOT / ".gitignore").read_text().splitlines()


# ---------------------------------------------------------------- runner helpers


def test_fault_flags_for_config_delivery():
    assert runner.fault_flags({"kind": "none"}) == []
    assert runner.fault_flags(
        {"kind": "latency", "latency_ms": 300, "jitter_ms": 300, "fraction": 0.03}
    ) == [
        "--fault-error-rate=0.0",
        "--fault-latency-ms=300",
        "--fault-jitter-ms=300",
        "--fault-fraction=0.03",
    ]


def test_run_id_and_l0_correctness():
    when = datetime(2026, 10, 26, 19, 5, 0, tzinfo=UTC)
    assert runner.run_id_for(spec(), when) == "release-errors-05-20261026-190500"
    assert runner.l0_correct("abort", "aborted")
    assert runner.l0_correct("block", "blocked")
    assert runner.l0_correct("ci_failure", "failure")
    assert not runner.l0_correct("promote", "aborted")
    assert not runner.l0_correct("abort", "timeout")


# ---------------------------------------------------------------- CI failure generator


@pytest.fixture
def repo_copy(tmp_path):
    for rel in (
        "app/podinfo/pkg/api/http/version.go",
        "app/podinfo/go.mod",
        "app/podinfo/Dockerfile",
        "ci/integration-test.sh",
    ):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    (tmp_path / "app/podinfo/pkg/api/http").mkdir(parents=True, exist_ok=True)
    return tmp_path


def read(root, rel):
    return (root / rel).read_text(encoding="utf-8")


def test_ci_classes_match_the_contract_root_causes():
    import typing

    from flowguard.contracts import RootCause

    assert set(ci_failures.CLASSES) == set(typing.get_args(RootCause))


def test_code_class_breaks_the_version_handler(repo_copy):
    change = ci_failures.apply(repo_copy, "code")
    assert '"commit":  version.VERSION,' in read(repo_copy, change.files[0])
    assert change.expected_failing_job == "unit"


def test_dependency_class_requires_a_missing_module(repo_copy):
    ci_failures.apply(repo_copy, "dependency")
    assert "github.com/flowguard-scenarios/missing-module v0.0.1" in read(
        repo_copy, "app/podinfo/go.mod"
    )


def test_environment_class_downgrades_the_build_image(repo_copy):
    ci_failures.apply(repo_copy, "environment")
    assert read(repo_copy, "app/podinfo/Dockerfile").startswith(
        "FROM golang:1.22-alpine AS builder"
    )


def test_flaky_class_adds_a_random_test(repo_copy):
    change = ci_failures.apply(repo_copy, "flaky_test")
    assert "rand.Float64() < 0.8" in read(repo_copy, change.files[0])


def test_infrastructure_class_pulls_from_an_unreachable_registry(repo_copy):
    ci_failures.apply(repo_copy, "infrastructure")
    script = read(repo_copy, "ci/integration-test.sh")
    assert "docker pull registry.flowguard-scenario.invalid" in script
    assert script.index("docker pull registry") < script.index("docker run -d")


def test_generator_refuses_unknown_classes_and_missing_patterns(repo_copy):
    with pytest.raises(ValueError):
        ci_failures.apply(repo_copy, "cosmic_ray")
    ci_failures.apply(repo_copy, "code")
    with pytest.raises(ValueError):  # already applied: the pattern is gone
        ci_failures.apply(repo_copy, "code")
