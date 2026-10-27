"""Local check of the CI failure templates: each one is applied to a clean export of HEAD and
the check behind its failing job is run with Docker, the way app-ci runs it. A template passes
when that check fails and its output shows the expected error. Outputs are kept for the log
leakage scan, next to baseline outputs of the unmodified code.

    python scenarios/verify_templates.py                 # every template
    python scenarios/verify_templates.py code-01 dependency-02

Writes evaluation/runs/template-checks/<id>.log and summary.json (gitignored).
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scenarios"))
import ci_failures  # noqa: E402

OUT = ROOT / "evaluation" / "runs" / "template-checks"
GO_IMAGE = "golang:1.26.10-alpine"


def export_head(dest: Path) -> None:
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "HEAD"], capture_output=True, check=True
    ).stdout
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as f:
        f.write(archive)
    with tarfile.open(f.name) as tar:
        tar.extractall(dest, filter="data")
    Path(f.name).unlink()


def win(path: Path) -> str:
    """A path Docker on Windows accepts in -v."""
    return str(path.resolve())


def run(cmd: list[str], timeout: int = 1800) -> tuple[int, str]:
    env = {**__import__("os").environ, "MSYS_NO_PATHCONV": "1"}
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    return p.returncode, p.stdout + p.stderr


def go(src: Path, script: str, toolchain: str = "local") -> tuple[int, str]:
    return run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{win(src / 'app/podinfo')}:/src",
            "-w",
            "/src",
            "-e",
            f"GOTOOLCHAIN={toolchain}",
            GO_IMAGE,
            "sh",
            "-c",
            script,
        ]
    )


def build(src: Path, tag: str) -> tuple[int, str]:
    return run(
        ["docker", "build", "--no-cache", "--progress=plain", "-t", tag, str(src / "app/podinfo")]
    )


def check(src: Path, job: str, tag: str, toolchain: str = "local") -> tuple[int, str]:
    if job == "build":
        return build(src, tag)
    if job == "unit":
        return go(src, "go test ./... 2>&1", toolchain)
    if job == "optional (lint)":
        return go(src, 'go vet ./... 2>&1 && test -z "$(gofmt -l .)"')
    if job == "optional (integration)":
        code, out = build(src, tag)
        if code:
            return code, out
        # Git Bash explicitly: on Windows a bare "bash" can resolve to WSL, which cannot read
        # the Windows path of the script
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if git_bash.exists() else "bash"
        script = (src / "ci/integration-test.sh").resolve().as_posix()
        return run([bash, script, tag], timeout=600)
    raise ValueError(job)


def earlier_jobs(job: str) -> list[str]:
    """The app-ci jobs that run before `job` and must pass for CI to reach it (the optional
    jobs run side by side after unit, so none of them comes before another)."""
    order = ["build", "unit"]
    return order[: order.index(job)] if job in order else order


def verify(template: ci_failures.Template) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp)
        export_head(src)
        template.apply(src)
        # An opaque tag: docker build prints it, and the log must not name the class
        tag = f"podinfo-check:{secrets.token_hex(4)}"
        # The toolchain template fails where setup-go downloads Go: GOTOOLCHAIN=auto does that
        toolchain = "auto" if template.id == "environment-05" else "local"
        # In CI a failure in an earlier job hides the intended one: those jobs must pass
        earlier = {
            job: check(src, job, tag, toolchain)[0] for job in earlier_jobs(template.failing_job)
        }
        attempts = 3 if template.root_cause == "flaky_test" else 1
        results = [check(src, template.failing_job, tag, toolchain) for _ in range(attempts)]
        run(["docker", "rmi", "-f", tag])
    failed = [(code, out) for code, out in results if code != 0]
    output = failed[0][1] if failed else results[-1][1]
    (OUT / f"{template.id}.log").write_text(output, encoding="utf-8")
    earlier_pass = all(code == 0 for code in earlier.values())
    return {
        "template": template.id,
        "job": template.failing_job,
        "earlier_jobs": {job: "pass" if code == 0 else "fail" for job, code in earlier.items()},
        "failed_runs": f"{len(failed)}/{attempts}",
        "expected_text_seen": bool(failed) and template.expect in output,
        "ok": earlier_pass and bool(failed) and template.expect in output,
    }


def baselines() -> None:
    """Outputs of the unmodified code, for the leakage scan's comparison."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp)
        export_head(src)
        for job in ("build", "unit", "optional (lint)", "optional (integration)"):
            code, out = check(src, job, "podinfo-check:baseline")
            name = job.replace("optional (", "").rstrip(")")
            (OUT / f"baseline-{name}.log").write_text(out, encoding="utf-8")
            print(f"baseline {job}: exit {code}", flush=True)
        run(["docker", "rmi", "-f", "podinfo-check:baseline"])


def main(argv: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ids = argv or list(ci_failures.TEMPLATES)
    if not argv:
        baselines()
    # Results are merged into summary.json after each template, so a partial run is kept
    path = OUT / "summary.json"
    results = {r["template"]: r for r in json.loads(path.read_text())} if path.is_file() else {}
    for tid in ids:
        result = verify(ci_failures.TEMPLATES[tid])
        results[tid] = result
        path.write_text(
            json.dumps(sorted(results.values(), key=lambda r: r["template"]), indent=2),
            encoding="utf-8",
        )
        print(json.dumps(result), flush=True)
    summary = list(results.values())
    bad = [r["template"] for r in summary if not r["ok"]]
    print(f"{len(summary) - len(bad)}/{len(summary)} templates behave as expected; failing: {bad}")
    shutil.rmtree(Path(tempfile.gettempdir()) / "podinfo-check", ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
