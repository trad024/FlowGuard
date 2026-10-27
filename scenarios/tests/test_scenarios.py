"""Scenario specs, the runner's pure parts, and the CI failure templates."""

import io
import random
import tarfile
import typing
from pathlib import Path

import pytest
import yaml

import ci_failures
import runner
from flowguard.contracts import RootCause

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


def dev_specs():
    return [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(DEV.glob("*.yaml"))]


@pytest.fixture(scope="module")
def head_archive(tmp_path_factory):
    """A tar of the working tree's app/ and ci/: what is about to be committed."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for top in ("app", "ci"):
            tar.add(
                ROOT / top, arcname=top, filter=lambda i: None if "__pycache__" in i.name else i
            )
    return buf.getvalue()


@pytest.fixture
def checkout(tmp_path, head_archive):
    with tarfile.open(fileobj=io.BytesIO(head_archive)) as tar:
        tar.extractall(tmp_path, filter="data")
    return tmp_path


# ---------------------------------------------------------------- specs


def test_every_dev_spec_is_valid_and_named_after_its_id():
    paths = sorted(DEV.glob("*.yaml"))
    assert len(paths) >= 19
    for path in paths:
        s = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert runner.validate_spec(s) == [], path.name
        assert path.stem == s["id"]


def test_dev_specs_cover_every_family_and_required_case():
    specs = dev_specs()
    kinds = {(s["family"], s["fault"]["kind"]) for s in specs}
    assert {k for f, k in kinds if f == "ci"} == set(ci_failures.CLASSES)
    assert {k for f, k in kinds if f == "runtime"} >= {
        "error_burst",
        "latency_spike",
        "memory_leak",
    }
    assert ("security", "vulnerable_dependency") in kinds
    assert ("security", "low_severity_finding") in kinds
    assert ("pipeline", "slow_step") in kinds
    stressed = [s for s in specs if s.get("node_stress")]
    assert stressed and all(s["ground_truth"]["bad_release"] is False for s in stressed)
    assert {s["fault"]["delivery"] for s in specs if s["family"] == "release"} == {
        "config",
        "commit",
    }
    assert any(
        s["ground_truth"].get("bad_release") and s["ground_truth"]["expected_l0"] == "promote"
        for s in specs
    )


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"family": "chaos"}, "family must be"),
        ({"fault": {"kind": "leaked_secret", "delivery": "config"}}, "fault.kind"),
        ({"fault": {"kind": "http_errors", "delivery": "teleport"}}, "fault.delivery"),
        ({"fault": {"kind": "http_errors", "delivery": "config", "error_rate": 2}}, "within 0..1"),
        ({"fault": {"kind": "latency", "delivery": "config", "latency_ms": -1}}, ">= 0"),
        ({"ground_truth": {"expected_l0": "maybe"}}, "expected_l0"),
        ({"environment": "prod"}, "only staging"),
        ({"id": "Bad ID"}, "id:"),
        (
            {
                "family": "security",
                "node_stress": {"cpu_workers": 2},
                "fault": {"kind": "leaked_secret", "delivery": "commit"},
            },
            "node_stress",
        ),
        (
            {"family": "runtime", "fault": {"kind": "error_burst", "delivery": "config"}},
            "fault.delivery",
        ),
    ],
)
def test_invalid_specs_are_rejected_with_a_reason(over, message):
    errors = runner.validate_spec(spec(**over))
    assert any(message in e for e in errors), errors


def test_ci_specs_name_the_root_cause_and_a_matching_template():
    truth = {"expected_l0": "ci_failure", "root_cause": "code"}
    s = spec(family="ci", fault={"kind": "dependency", "delivery": "commit"}, ground_truth=truth)
    assert any("root_cause" in e for e in runner.validate_spec(s))
    fault = {"kind": "code", "delivery": "commit", "template": "dependency-01"}
    s = spec(family="ci", fault=fault, ground_truth=truth)
    assert any("fault.template" in e for e in runner.validate_spec(s))


def test_heldout_specs_are_gitignored():
    assert "scenarios/heldout/" in (ROOT / ".gitignore").read_text().splitlines()


# ---------------------------------------------------------------- runner helpers


def test_fault_flags_and_runtime_settings():
    assert runner.fault_flags({"kind": "none"}) == []
    fault = {"kind": "latency", "latency_ms": 300, "jitter_ms": 300, "fraction": 0.03}
    assert runner.fault_flags(fault) == [
        "--fault-error-rate=0.0",
        "--fault-latency-ms=300",
        "--fault-jitter-ms=300",
        "--fault-fraction=0.03",
    ]
    assert runner.runtime_settings({"kind": "memory_leak", "leak_kb_per_s": 512}) == {
        "error_rate": 0.0,
        "latency_ms": 0,
        "jitter_ms": 0,
        "fraction": 1.0,
        "leak_kb_per_s": 512,
    }


def test_run_ids_are_opaque_and_l0_correctness():
    ids = {runner.new_run_id() for _ in range(50)}
    assert len(ids) == 50 and all(i.startswith("r-") and len(i) == 10 for i in ids)
    assert runner.l0_correct("abort", "aborted")
    assert runner.l0_correct("block", "blocked")
    assert runner.l0_correct("ci_failure", "failure")
    assert runner.l0_correct("promote", "aborted") is False
    assert runner.l0_correct("detect", "not_measured") is None  # judged in Step 4b


def test_wait_ci_retries_network_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWGUARD_AUDIT_DB", str(tmp_path / "audit.db"))
    r = runner.Runner(spec(), run_id="r-0000abcd")
    calls = []

    class FlakyGitHub:
        def run_for_commit(self, sha):
            calls.append(sha)
            if len(calls) < 3:
                raise OSError("The handshake operation timed out")
            return {"id": 1, "status": "completed", "conclusion": "failure"}

    run = r.wait_ci(FlakyGitHub(), "abc", sleep=lambda s: None)
    assert run["conclusion"] == "failure" and len(calls) == 3
    assert len(list(r.audit.events("poll_retry"))) == 2


def test_host_sleep_is_detected_in_the_load_log(tmp_path):
    log = tmp_path / "load.jsonl"
    rows = [
        '{"ts":"2026-10-27T12:52:00.000Z","status":200,"latency_ms":20}',
        '{"ts":"2026-10-27T12:52:01.000Z","status":200,"latency_ms":25}',
    ]
    log.write_text("\n".join(rows), encoding="utf-8")
    assert runner.suspend_gaps([log]) == []
    # A request in flight over a one-hour sleep, then a gap in the log
    rows += [
        '{"ts":"2026-10-27T12:52:15.000Z","status":200,"latency_ms":3629606.7}',
        '{"ts":"2026-10-27T13:53:00.000Z","status":200,"latency_ms":20}',
    ]
    log.write_text("\n".join(rows), encoding="utf-8")
    gaps = runner.suspend_gaps([log, tmp_path / "missing.jsonl"])
    assert [g["at"] for g in gaps] == ["2026-10-27T12:52:15.000Z", "2026-10-27T13:53:00.000Z"]


def test_an_aborted_restore_is_retried_once_with_live_load(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWGUARD_AUDIT_DB", str(tmp_path / "audit.db"))
    r = runner.Runner(spec(), run_id="r-0000abcd")
    outcomes = iter(["aborted", "promoted"])
    calls = []
    monkeypatch.setattr(runner, "patch_template", lambda *a: calls.append("patch"))
    monkeypatch.setattr(runner, "retry_rollout", lambda ns: calls.append("retry"))
    monkeypatch.setattr(runner, "wait_outcome", lambda *a: next(outcomes))

    class DeadLoad:
        def alive(self):
            return False

        def start(self, label):
            calls.append(label)

    r.loadgen = DeadLoad()
    r.restore("img:1", ["./podinfo"])
    assert calls == ["patch", "r-0000abcd-restore-1", "retry", "r-0000abcd-restore-2"]
    assert r.observed["restore"] == "promoted"


# ---------------------------------------------------------------- CI failure templates


def test_at_least_six_templates_per_class_matching_the_contract():
    assert set(ci_failures.CLASSES) == set(typing.get_args(RootCause))
    for cls in ci_failures.CLASSES:
        templates = ci_failures.BY_CLASS[cls]
        assert len(templates) >= 6, cls
        assert all(t.id.startswith(cls + "-") for t in templates)


def test_templates_differ_in_files_and_failing_jobs(head_archive, tmp_path):
    jobs = {t.failing_job for t in ci_failures.TEMPLATES.values()}
    assert {"build", "unit", "optional (lint)", "optional (integration)"} <= jobs
    files = set()
    for template in ci_failures.TEMPLATES.values():  # each on its own fresh copy
        dest = tmp_path / template.id
        with tarfile.open(fileobj=io.BytesIO(head_archive)) as tar:
            tar.extractall(dest, filter="data")
        files.update(template.apply(dest))
    assert len(files) >= 12
    # Every class fails in more than one way
    for cls in ci_failures.CLASSES:
        expects = {t.expect for t in ci_failures.BY_CLASS[cls]}
        assert len(expects) >= 5, cls


@pytest.mark.parametrize("template_id", sorted(ci_failures.TEMPLATES))
def test_every_template_applies_to_head(head_archive, tmp_path, template_id):
    with tarfile.open(fileobj=io.BytesIO(head_archive)) as tar:
        tar.extractall(tmp_path, filter="data")
    changed = ci_failures.TEMPLATES[template_id].apply(tmp_path)
    assert changed and all("\\" not in f for f in changed)


def test_pick_is_reproducible_and_respects_the_class():
    a = ci_failures.pick("code", random.Random("r-1234abcd"))
    b = ci_failures.pick("code", random.Random("r-1234abcd"))
    assert a.id == b.id and a.root_cause == "code"
    assert ci_failures.pick("code", random.Random(1), "code-03").id == "code-03"
    with pytest.raises(ValueError):
        ci_failures.pick("code", random.Random(1), "dependency-01")
    with pytest.raises(ValueError):
        ci_failures.pick("cosmic_ray", random.Random(1))


@pytest.mark.parametrize(
    ("family", "kind", "expected_truth"),
    [
        ("security", "vulnerable_dependency", "finding"),
        ("security", "low_severity_finding", "finding"),
        ("pipeline", "slow_step", "anomaly"),
        ("ci", "code", "template_id"),
    ],
)
def test_commit_changes_apply_and_record_their_ground_truth(checkout, family, kind, expected_truth):
    s = spec(
        family=family,
        fault={"kind": kind, "delivery": "commit"},
        ground_truth={"expected_l0": "block", "root_cause": kind},
    )
    change = runner.commit_change(checkout, s, random.Random("r-1"))
    assert change["files"] and change["message"]
    assert expected_truth in change["truth"]
