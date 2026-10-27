"""Label leakage: no generated change, and no line a generated failure adds to a log, may name
the root-cause class or give the experiment away."""

import io
import json
import random
import tarfile
from pathlib import Path

import pytest

import ci_failures
import leakage
import runner

ROOT = Path(__file__).resolve().parents[2]
CHECKS = ROOT / "evaluation" / "runs" / "template-checks"


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


def extract(archive, dest):
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    return dest


def diff_files(root, changed, original):
    return {
        rel: (
            (original / rel).read_text(encoding="utf-8") if (original / rel).exists() else "",
            (root / rel).read_text(encoding="utf-8"),
        )
        for rel in changed
    }


# ---------------------------------------------------------------- the checker itself


@pytest.mark.parametrize(
    "text",
    [
        "TestFlakyCache",
        "flaky_test",
        "dependency-03",
        "Scenario run",
        "infra-structure",
        "simulated timing",
        "FlowGuard",
        "injected fault",
        "root cause",
        "ENVIRONMENT",
    ],
)
def test_forbidden_words_are_caught_in_any_spelling(text):
    assert leakage.hits(text) or leakage.hits(text.replace("-", ""))


@pytest.mark.parametrize(
    "text",
    [
        "TestCacheEntryExpiresAfterTTL",
        "Bump websocket",
        "mirror.registry.internal",
        "go.mod requires go >= 1.26",
        "w.WriteHeader(http.StatusOK)",
    ],
)
def test_ordinary_text_is_not_flagged(text):
    assert leakage.hits(text) == set()


def test_branch_names_must_be_opaque():
    ok = leakage.scan_change({}, "Bump websocket", "scenario/r-1a2b3c4d")
    bad = leakage.scan_change({}, "Bump websocket", "scenario/ci-dependency-20261026")
    assert ok == [] and bad


def test_log_scan_ignores_lines_a_normal_run_also_has():
    baseline = "Run actions/checkout\n/home/runner/work/FlowGuard/FlowGuard\nexit code 0\n"
    log = (
        "Run actions/checkout\n/home/runner/work/FlowGuard/FlowGuard\nexit code 1\n"
        "--- FAIL: TestFlakyThing (0.02s)\n"
    )
    problems = leakage.scan_log(log, baseline)
    assert len(problems) == 1 and "flaky" in problems[0]


# ---------------------------------------------------------------- generated changes


@pytest.mark.parametrize("template_id", sorted(ci_failures.TEMPLATES))
def test_ci_templates_do_not_leak(head_archive, tmp_path, template_id):
    original = extract(head_archive, tmp_path / "a")
    root = extract(head_archive, tmp_path / "b")
    template = ci_failures.TEMPLATES[template_id]
    changed = template.apply(root)
    problems = leakage.scan_change(
        diff_files(root, changed, original),
        template.commit_message,
        f"scenario/{runner.new_run_id()}",
    )
    assert problems == []


@pytest.mark.parametrize(
    ("family", "kind"),
    [
        ("security", "leaked_secret"),
        ("security", "vulnerable_dependency"),
        ("security", "low_severity_finding"),
        ("pipeline", "slow_step"),
        ("release", "http_errors"),
    ],
)
def test_other_commit_scenarios_do_not_leak(head_archive, tmp_path, family, kind):
    original = extract(head_archive, tmp_path / "a")
    root = extract(head_archive, tmp_path / "b")
    s = {
        "id": "x-1",
        "family": family,
        "description": "",
        "ground_truth": {},
        "fault": {"kind": kind, "delivery": "commit", "error_rate": 0.1},
    }
    change = runner.commit_change(root, s, random.Random("r-1"))
    problems = leakage.scan_change(
        diff_files(root, change["files"], original),
        change["message"],
        f"scenario/{runner.new_run_id()}",
    )
    assert problems == []


# ---------------------------------------------------------------- captured logs


def captured_logs():
    """Outputs from scenarios/verify_templates.py, when it has been run on this machine."""
    summary = CHECKS / "summary.json"
    if not summary.is_file():
        return []
    return [r["template"] for r in json.loads(summary.read_text(encoding="utf-8"))]


def baseline_for(template_id):
    job = ci_failures.TEMPLATES[template_id].failing_job
    name = job.replace("optional (", "").rstrip(")")
    return (CHECKS / f"baseline-{name}.log").read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize(
    "template_id",
    captured_logs()
    or [
        pytest.param(
            "none",
            marks=pytest.mark.skip(
                reason="run scenarios/verify_templates.py first to capture logs"
            ),
        )
    ],
)
def test_captured_logs_do_not_leak(template_id):
    log = (CHECKS / f"{template_id}.log").read_text(encoding="utf-8", errors="replace")
    assert leakage.scan_log(log, baseline_for(template_id)) == []


def test_tool_phrases_are_masked_but_class_words_still_count():
    assert leakage.scan_log("ERROR: exit code: 1\nwrong status code: got 200", "") == []
    assert leakage.scan_log("code generation failed", "")  # "code" on its own still counts
