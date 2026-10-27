"""Scenario specs, the runner's pure parts, and the CI failure templates."""

import io
import json
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


@pytest.fixture
def stores(monkeypatch, tmp_path):
    """Audit log, ground-truth DB and deployer-event log in tmp_path, never the real ones."""
    monkeypatch.setenv("FLOWGUARD_AUDIT_DB", str(tmp_path / "audit.db"))
    monkeypatch.setenv("FLOWGUARD_GROUNDTRUTH_DB", str(tmp_path / "groundtruth.db"))
    monkeypatch.setattr(runner, "RUNS", tmp_path)
    return tmp_path


def test_release_settings_and_runtime_settings():
    assert runner.release_settings({"kind": "none"}) == {}
    fault = {"kind": "latency", "latency_ms": 300, "jitter_ms": 300, "fraction": 0.03}
    settings = runner.release_settings(fault)
    assert settings == {
        "PODINFO_FAULT_ERROR_RATE": "0.0",
        "PODINFO_FAULT_LATENCY_MS": "300",
        "PODINFO_FAULT_JITTER_MS": "300",
        "PODINFO_FAULT_FRACTION": "0.03",
    }
    assert set(settings) == set(runner.deployer.RELEASE_KEYS)
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


def test_template_ops_set_or_remove_the_release_secret():
    env = runner.env_from("podinfo-release-1a2b3c4d")
    ops = runner.template_ops({}, "img:1", ["./podinfo"], env)
    assert ops[-1] == {
        "op": "add",
        "path": "/spec/template/spec/containers/0/envFrom",
        "value": [{"secretRef": {"name": "podinfo-release-1a2b3c4d"}}],
    }
    # Back to a template without one: removed only if the current one has it
    assert runner.template_ops({"envFrom": env}, "img:1", ["./podinfo"], None)[-1]["op"] == "remove"
    assert len(runner.template_ops({}, "img:1", ["./podinfo"], None)) == 2


def test_wait_ci_retries_network_errors(stores):
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
    assert len(list(r.truth_db.events("r-0000abcd", "poll_retry"))) == 2
    assert list(r.audit.events()) == []  # experiment events never reach the audit log


def test_host_sleep_is_detected_in_the_load_log(tmp_path):
    log = tmp_path / "load.jsonl"
    rows = [
        '{"ts":"2026-10-27T12:52:00.000Z","status":200,"latency_ms":20}',
        '{"ts":"2026-10-27T12:52:01.000Z","status":200,"latency_ms":25}',
    ]
    log.write_text("\n".join(rows), encoding="utf-8")
    assert runner.suspend_gaps([log]) == []
    # Requests in flight over a one-hour sleep (lines carry the start time), then the next
    # request after the wake: one episode
    rows += [
        '{"ts":"2026-10-27T12:52:15.100Z","status":200,"latency_ms":3629570.5}',
        '{"ts":"2026-10-27T12:52:15.000Z","status":200,"latency_ms":3629606.7}',
        '{"ts":"2026-10-27T13:53:00.000Z","status":200,"latency_ms":20}',
        # later, the load generator stops for two minutes
        '{"ts":"2026-10-27T13:53:30.000Z","status":200,"latency_ms":20}',
        '{"ts":"2026-10-27T13:55:30.000Z","status":200,"latency_ms":20}',
    ]
    log.write_text("\n".join(rows), encoding="utf-8")
    gaps = runner.suspend_gaps([log, tmp_path / "missing.jsonl"])
    assert gaps == [
        {"from": "2026-10-27T12:52:15Z", "to": "2026-10-27T13:53:00Z"},
        {"from": "2026-10-27T13:53:30Z", "to": "2026-10-27T13:55:30Z"},
    ]


class FakeCluster:
    def __init__(self, rollout=None):
        self.state = rollout
        self.gc = 0

    def rollout(self):
        return self.state

    def gc_release_settings(self):
        self.gc += 1
        return ["podinfo-release-old"]


def test_an_aborted_restore_is_retried_once_with_live_load(monkeypatch, stores):
    r = runner.Runner(spec(), run_id="r-0000abcd")
    r.cluster = FakeCluster()
    outcomes = iter(["aborted", "promoted"])
    calls = []
    monkeypatch.setattr(runner, "patch_template", lambda *a: calls.append(("patch", a[3])))
    monkeypatch.setattr(runner, "retry_rollout", lambda ns: calls.append("retry"))
    monkeypatch.setattr(runner, "wait_outcome", lambda *a: next(outcomes))

    class DeadLoad:
        def alive(self):
            return False

        def start(self, label):
            calls.append(label)

    r.loadgen = DeadLoad()
    saved_env = runner.env_from("podinfo-release-saved")
    r.restore("img:1", ["./podinfo"], saved_env)
    assert calls == [
        ("patch", saved_env),  # the pre-scenario release Secret comes back with the template
        "r-0000abcd-restore-1",
        "retry",
        "r-0000abcd-restore-2",
    ]
    assert r.observed["restore"] == "promoted" and r.cluster.gc == 1
    assert [e["kind"] for e in r.truth_db.events("r-0000abcd")][-1] == "release_settings_deleted"


def test_config_release_keeps_the_injection_out_of_the_audit_log(monkeypatch, stores):
    """D25: the audit log sees a deployment like any other; what was injected is only in the
    ground-truth DB. Good and bad releases differ only in the Secret's (unseen) values."""
    image, command = "ghcr.io/x/podinfo:abc", list(runner.BASE_COMMAND)
    stable = {
        "spec": {"template": {"spec": {"containers": [{"image": image, "command": command}]}}},
        "status": {"phase": "Healthy", "stableRS": "h1", "currentPodHash": "h1"},
    }
    patches, secrets = [], []

    def create(namespace, values):
        secrets.append(values)
        return f"podinfo-release-{len(secrets):08x}"

    monkeypatch.setattr(runner.deployer, "create_release_settings", create)
    monkeypatch.setattr(runner, "patch_template", lambda *a: patches.append(a))
    monkeypatch.setattr(runner, "wait_outcome", lambda *a: "aborted")
    monkeypatch.setattr(runner, "retry_rollout", lambda ns: retries.append(ns))
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    retries = []

    class Load:
        def start(self, label):
            return stores / f"{label}.jsonl"

        def stop(self):
            pass

        def alive(self):
            return True

    fault = {"kind": "http_errors", "delivery": "config", "error_rate": 0.05}
    r = runner.Runner(spec(fault=fault), run_id="r-0000abcd")
    r.cluster, r.loadgen = FakeCluster(stable), Load()
    r.run_config()

    release, restore = patches
    assert release[1:3] == (image, command)  # same image and command as stable
    assert release[3] == runner.env_from("podinfo-release-00000001")
    assert restore[3] is None  # the stable template had no release Secret
    assert secrets == [runner.release_settings(fault)]
    assert retries == ["staging"]  # the aborted restore was retried once

    audit = list(r.audit.events())
    # release, then the restore (aborted, retried once)
    assert [e["kind"] for e in audit] == ["deploy_started", "deploy_finished"] * 3
    for e in audit:
        text = json.dumps(e).lower()
        assert "fault" not in text and "error_rate" not in text and "http_errors" not in text
    truth = {e["kind"]: e["payload"] for e in r.truth_db.events("r-0000abcd")}
    assert truth["fault_injected"]["settings"]["PODINFO_FAULT_ERROR_RATE"] == "0.05"
    assert truth["l0_outcome"]["outcome"] == "aborted"


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


@pytest.mark.parametrize("template_id", sorted(ci_failures.ALL_VARIANTS))
def test_every_template_applies_to_head(head_archive, tmp_path, template_id):
    with tarfile.open(fileobj=io.BytesIO(head_archive)) as tar:
        tar.extractall(tmp_path, filter="data")
    changed = ci_failures.ALL_VARIANTS[template_id].apply(tmp_path)
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


def test_a_release_through_ci_is_an_ordinary_defect_with_no_fault_setting(checkout):
    """D30: the diff is a plausible bug that unit tests miss; it names no fault setting."""
    before = (checkout / "app/podinfo/pkg/api/http/info.go").read_text(encoding="utf-8")
    s = spec(fault={"kind": "http_errors", "delivery": "commit", "error_rate": 0.1})
    change = runner.commit_change(checkout, s, random.Random("r-1"))
    assert change["files"] == ["app/podinfo/pkg/api/http/info.go"]
    after = (checkout / change["files"][0]).read_text(encoding="utf-8")
    added = "\n".join(line for line in after.splitlines() if line not in before.splitlines())
    assert "fault" not in added.lower() and "PODINFO_" not in added
    assert "refreshInterval = 10 * time.Second" in added and "fields[1]" in added
    assert change["truth"]["defect"]["expected_5xx_share"] == 0.1


@pytest.mark.parametrize(
    ("rate", "load", "expected"),
    [(0.1, 10, (10, 0.1)), (0.05, 10, (20, 0.05)), (0.5, 10, (5, 0.2)), (0.1, 20, (5, 0.1))],
)
def test_refresh_interval_gives_the_share_and_never_drops_below_the_minimum(rate, load, expected):
    assert runner.refresh_interval(rate, {"load": {"rate": load}}) == expected


@pytest.mark.parametrize(
    "fault",
    [
        {"kind": "latency", "delivery": "commit", "latency_ms": 300},
        {"kind": "http_errors", "delivery": "commit"},
    ],
)
def test_a_release_through_ci_must_be_an_error_defect(fault):
    assert any("through CI" in e for e in runner.validate_spec(spec(fault=fault)))


def test_corpus_split_is_by_template_seeded_and_covers_every_class():
    import corpus

    by_class = {c: [t.id for t in ci_failures.BY_CLASS[c]] for c in ci_failures.CLASSES}
    split = corpus.split(by_class)
    assert split == corpus.split(by_class) != corpus.split(by_class, seed="other")
    for cls, ids in by_class.items():
        parts = [split[t] for t in ids]
        assert {"train", "validation", "test"} <= set(parts), cls
        assert parts.count("train") >= len(ids) // 2
    # every variant follows its template into one split
    for vid in ci_failures.ALL_VARIANTS:
        assert ci_failures.template_of(vid) in split


def test_corpus_runs_the_jobs_in_app_ci_order():
    import corpus

    assert corpus.jobs_for(ci_failures.ALL_VARIANTS["code-05"]) == ["build", "unit"]
    lint = ci_failures.ALL_VARIANTS["code-06.v2"]
    assert corpus.jobs_for(lint) == ["build", "unit", "optional (lint)"]


@pytest.mark.parametrize(
    ("module", "version", "replaced"),
    [("golang.org/x/text", "v0.19.0", True), ("google.golang.org/grpc", "v1.67.1", False)],
)
def test_dso4_items_pin_the_drawn_module_version(checkout, monkeypatch, module, version, replaced):
    sums = [f"{module} {version} h1:aaa=", f"{module} {version}/go.mod h1:bbb="]
    monkeypatch.setattr(runner, "go_sum_lines", lambda m, v: sums)
    s = spec(
        family="security",
        fault={
            "kind": "vulnerable_dependency",
            "delivery": "commit",
            "module": module,
            "version": version,
        },
        ground_truth={"bad_release": True, "expected_l0": "block"},
    )
    change = runner.commit_change(checkout, s, random.Random("r-1"))
    gomod = (checkout / runner.GOMOD).read_text(encoding="utf-8")
    assert f"replace {module} => {module} {version}" in gomod
    assert gomod.count(f"replace {module} =>") == 1  # an existing directive is updated
    assert all(line in (checkout / runner.GOSUM).read_text(encoding="utf-8") for line in sums)
    assert change["message"] == f"Pin {module.rsplit('/', 1)[-1]}"
    assert change["truth"]["finding"] == f"{module} {version}"
    if replaced:
        assert "golang.org/x/text => golang.org/x/text v0.42.0" not in gomod
