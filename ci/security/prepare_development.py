"""Prepare deliberately selected development-only application fixtures, without pushing CI."""

import argparse
import hashlib
import io
import json
import os
import random
import subprocess
import zipfile
from pathlib import Path

from flowguard.epss import load

ROOT = Path(__file__).resolve().parents[2]
PROBES = {
    "precis-called": """package main

import "golang.org/x/text/secure/precis"

func init() {
	_, _ = precis.Nickname.String("FlowGuard development case")
}
""",
    "precis-uncalled": """package main

import _ "golang.org/x/text/secure/precis"
""",
    "jwt5-uncalled": """package main

import jwtdev "github.com/golang-jwt/jwt/v5"

var developmentToken = jwtdev.New(jwtdev.SigningMethodHS256)
""",
    "legacy-jwt": """package main

import jwtdev "github.com/golang-jwt/jwt"

var developmentToken = jwtdev.New(jwtdev.SigningMethodHS256)
""",
}


def verify_plan(plan, epss):
    snapshot = load(epss)
    metadata = json.loads(epss.with_suffix(".json").read_text(encoding="utf-8"))
    expected = plan["epss"]
    if (
        metadata.get("sample") is not False
        or snapshot.date != expected["date"]
        or snapshot.sha256 != expected["csv_sha256"]
        or metadata.get("gzip_sha256") != expected["gzip_sha256"]
        or len(snapshot.scores) != expected["rows"]
    ):
        raise ValueError("requires the pinned full dated FIRST snapshot, not a sample")
    compressed = epss.with_suffix(".csv.gz")
    if hashlib.sha256(compressed.read_bytes()).hexdigest() != expected["gzip_sha256"]:
        raise ValueError("compressed FIRST snapshot checksum mismatch")
    for case in plan["cases"]:
        if snapshot.scores.get(case["cve"]) != case["epss"]:
            raise ValueError(f"CVE absent or EPSS differs from declared value: {case['id']}")
    return snapshot


def run(args, cwd, env, log):
    completed = subprocess.run(
        [str(arg) for arg in args], cwd=cwd, env=env, stdout=log, stderr=log, check=False
    )
    if completed.returncode:
        raise RuntimeError(f"command failed ({completed.returncode}): {args[1:]}")


def prepare(plan, case, out, go):
    destination = out / case["id"]
    if destination.exists():
        raise ValueError(f"refusing to overwrite a prepared case: {destination}")
    archive = subprocess.check_output(
        ["git", "archive", "--format=zip", plan["base"], "app/podinfo"], cwd=ROOT
    )
    destination.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        zipped.extractall(destination)
    app = destination / "app/podinfo"
    probe = PROBES.get(case["probe"])
    if probe:
        (app / "cmd/podinfo/security_dev_case.go").write_text(probe, encoding="utf-8")
    env = {**os.environ, "GOMAXPROCS": "2", "GOMEMLIMIT": "1800MiB", "CGO_ENABLED": "0"}
    with (destination / "prepare.log").open("w", encoding="utf-8") as log:
        pins = {case["module"]: case["version"], **case.get("compatibility_pins", {})}
        for module, version in pins.items():
            run([go, "mod", "edit", f"-require={module}@{version}"], app, env, log)
            # Enforce pins even when minimum version selection would raise the version.
            run([go, "mod", "edit", f"-replace={module}={module}@{version}"], app, env, log)
        run([go, "mod", "tidy"], app, env, log)
        if probe:
            run([go, "fmt", "./cmd/podinfo"], app, env, log)
        run([go, "list", "-m", "-json", case["module"]], app, env, log)
        run(
            [go, "build", "./cmd/podinfo", "./cmd/podcli"],
            app,
            {**env, "GOOS": "linux", "GOARCH": "amd64"},
            log,
        )
    record = {
        "id": case["id"],
        "base": plan["base"],
        "development_seed": plan["development_seed"],
        "local_linux_build": "passed",
        "local_only": True,
        "files": {
            str(p.relative_to(app)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (app / "go.mod", app / "go.sum")
        },
    }
    (destination / "prepared.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=ROOT / "ci/security/development-suite.json")
    parser.add_argument("--epss", type=Path, default=ROOT / "state/epss/2026-10-28.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "state/security-dev-cases/prepared")
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument(
        "--case", help="prepare just this case; otherwise use public dev-seed order"
    )
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    verify_plan(plan, args.epss)
    cases = list(plan["cases"])
    random.Random(plan["development_seed"]).shuffle(cases)
    if args.case:
        cases = [case for case in cases if case["id"] == args.case]
        if not cases:
            parser.error("case is not in the declared development plan")
    for case in cases:
        print(f"Preparing {case['id']} ({case['module']}@{case['version']})", flush=True)
        print(json.dumps(prepare(plan, case, args.out, args.go.resolve())), flush=True)


if __name__ == "__main__":
    main()
