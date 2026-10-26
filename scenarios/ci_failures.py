"""CI failure generator (DSO2): one commit-sized change per root-cause class, applied to a
checkout of the repo on a scenario/* branch. Each change makes app-ci fail in the way that
class fails in real pipelines; the class is the ground truth for Diagnose (Step 7).

The five classes are contracts.RootCause:
  code            a bug in application code that a unit test catches
  dependency      a requirement that cannot be resolved (module missing from the proxy)
  environment     the build environment does not match the code (Go toolchain too old)
  flaky_test      a test that fails at random, independent of the change
  infrastructure  the CI infrastructure fails (a registry the job needs is unreachable)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

CLASSES = ("code", "dependency", "environment", "flaky_test", "infrastructure")
APP = Path("app/podinfo")


@dataclass(frozen=True)
class Change:
    root_cause: str
    files: tuple[str, ...]
    summary: str
    expected_failing_job: str


def _edit(root: Path, rel: Path, pattern: str, replacement: str) -> None:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if n != 1:
        raise ValueError(f"{rel}: pattern not found: {pattern}")
    path.write_text(new, encoding="utf-8", newline="\n")


def code(root: Path) -> Change:
    rel = APP / "pkg/api/http/version.go"
    _edit(root, rel, r'"commit":\s+version\.REVISION,', '"commit":  version.VERSION,')
    return Change(
        "code", (rel.as_posix(),), "version handler reports the version as the commit", "unit"
    )


def dependency(root: Path) -> Change:
    rel = APP / "go.mod"
    _edit(
        root,
        rel,
        r"^require \(\n",
        "require (\n\tgithub.com/flowguard-scenarios/missing-module v0.0.1\n",
    )
    return Change(
        "dependency", (rel.as_posix(),), "requires a module version that does not exist", "build"
    )


def environment(root: Path) -> Change:
    rel = APP / "Dockerfile"
    _edit(
        root, rel, r"^FROM golang:[\d.]+-alpine AS builder$", "FROM golang:1.22-alpine AS builder"
    )
    return Change(
        "environment",
        (rel.as_posix(),),
        "build image's Go 1.22 is older than go.mod requires",
        "build",
    )


FLAKY_TEST = """package http

// Scenario file (FlowGuard CI failure generator, class flaky_test): fails at random, about
// four runs in five, whatever the code does.

import (
	"math/rand/v2"
	"testing"
)

func TestScenarioFlakyNetworkTiming(t *testing.T) {
	if rand.Float64() < 0.8 {
		t.Fatal("timed out waiting for the upstream echo (simulated flaky timing)")
	}
}
"""


def flaky_test(root: Path) -> Change:
    rel = APP / "pkg/api/http/scenario_flaky_test.go"
    (root / rel).write_text(FLAKY_TEST, encoding="utf-8", newline="\n")
    return Change("flaky_test", (rel.as_posix(),), "a test that fails at random (p=0.8)", "unit")


def infrastructure(root: Path) -> Change:
    rel = Path("ci/integration-test.sh")
    _edit(
        root,
        rel,
        r'^IMAGE="\$\{1:\?usage: \$0 IMAGE\}"$',
        'IMAGE="${1:?usage: $0 IMAGE}"\n'
        "# Scenario (CI failure generator, infrastructure): a registry the job needs is down\n"
        "docker pull registry.flowguard-scenario.invalid/fixtures/echo:1",
    )
    return Change(
        "infrastructure",
        (rel.as_posix(),),
        "integration job pulls from an unreachable registry",
        "optional (integration)",
    )


GENERATORS = {
    "code": code,
    "dependency": dependency,
    "environment": environment,
    "flaky_test": flaky_test,
    "infrastructure": infrastructure,
}


def apply(root: Path, root_cause: str) -> Change:
    if root_cause not in GENERATORS:
        raise ValueError(f"root cause must be one of {CLASSES}, got {root_cause!r}")
    return GENERATORS[root_cause](root)
