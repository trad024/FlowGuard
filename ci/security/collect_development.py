"""Validate retained real development app-ci artifacts and keep sanitized scan evidence."""

import argparse
import hashlib
import json
from pathlib import Path

from map_govulncheck import messages, reachability
from prepare_development import verify_plan

VULNERABILITY_FIELDS = (
    "VulnerabilityID",
    "PkgName",
    "InstalledVersion",
    "Severity",
    "CVSS",
    "FixedVersion",
    "Status",
    "Reachable",
    "ReachabilitySource",
)


def collect_case(case, publication, run, jobs, artifacts):
    """Reject failed builds and mismatched runs; never substitute intended reachability."""
    if (
        run["head_sha"] != publication["sha"]
        or run["head_branch"] != publication["branch"]
        or not run["head_branch"].startswith("scenario/security-dev-31-")
        or run["name"] != "app-ci"
        or publication["development_seed"] != 31
    ):
        raise ValueError("run does not match the declared development commit")
    completed = {job["name"]: job["conclusion"] for job in jobs["jobs"]}
    if any(completed.get(name) != "success" for name in ("build", "unit", "scans")):
        raise ValueError("invalid case: successful real build, unit tests and scans are required")
    paths = {
        name: artifacts / name
        for name in (
            "trivy.json",
            "gitleaks.json",
            "govulncheck.json",
            "govulncheck-reachability.json",
        )
    }
    raw = {name: path.read_bytes() for name, path in paths.items()}
    trivy = json.loads(raw["trivy.json"])
    leaks = json.loads(raw["gitleaks.json"])
    mapped = json.loads(raw["govulncheck-reachability.json"])
    checked = reachability(raw["govulncheck.json"].decode("utf-8"), 0)
    if not mapped.get("complete") or mapped != checked:
        raise ValueError("complete pinned source/symbol govulncheck evidence is required")
    sanitized = {"Results": []}
    findings = []
    for target in trivy["Results"]:
        entries = []
        for vulnerability in target.get("Vulnerabilities") or []:
            item = {key: vulnerability[key] for key in VULNERABILITY_FIELDS if key in vulnerability}
            cve = item["VulnerabilityID"]
            observed = mapped["reachable_by_cve"].get(cve)
            if item.get("Reachable") is not observed:
                raise ValueError("Trivy enrichment disagrees with retained govulncheck evidence")
            entries.append(item)
        sanitized["Results"].append({"Vulnerabilities": entries})
        findings.extend(entries)
    target_findings = [
        finding
        for finding in findings
        if (
            finding["VulnerabilityID"] == case["cve"]
            and finding.get("PkgName") == case["module"]
            and finding.get("InstalledVersion", "").removeprefix("v")
            == case["version"].removeprefix("v")
            and finding["Severity"] == "HIGH"
        )
    ]
    if not target_findings:
        raise ValueError(
            f"invalid case: requested HIGH CVE/module/version not observed: {case['id']}"
        )
    return {
        "id": case["id"],
        "run_id": run["id"],
        "bad_release": True,
        "ground_truth_scope": "vulnerable development pin; runtime exploitation is not established",
        "counts": {
            "trivy_high": sum(f["Severity"] == "HIGH" for f in findings),
            "trivy_critical": sum(f["Severity"] == "CRITICAL" for f in findings),
            "gitleaks": len(leaks),
        },
        "reports": {
            "trivy": sanitized,
            "gitleaks": [{"RuleID": "redacted-secret-finding"} for _ in leaks],
        },
        "target": {key: case[key] for key in ("cve", "module", "version", "epss", "advisory")},
        "source": {
            **publication,
            "run_id": run["id"],
            "run_url": run["html_url"],
            "created_at": run["created_at"],
            "run_conclusion": run["conclusion"],
            "required_job_conclusions": {
                name: completed[name] for name in ("build", "unit", "scans")
            },
            "scope": "development seed only",
            "govulncheck": mapped,
            "govulncheck_config": messages(raw["govulncheck.json"].decode("utf-8"))[0]["config"],
            "artifact_sha256": {
                name: hashlib.sha256(data).hexdigest() for name, data in raw.items()
            },
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path("ci/security/development-suite.json"))
    parser.add_argument(
        "--publication", type=Path, default=Path("state/security-dev-cases/publication.json")
    )
    parser.add_argument("--retained", type=Path, default=Path("state/security-dev-cases/retained"))
    parser.add_argument("--epss", type=Path, default=Path("state/epss/2026-10-28.csv"))
    parser.add_argument(
        "--out", type=Path, default=Path("ci/security/development-observations.json")
    )
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    verify_plan(plan, args.epss)
    publication = {p["id"]: p for p in json.loads(args.publication.read_text(encoding="utf-8"))}
    records = []
    for case in plan["cases"]:
        retained = args.retained / case["id"]
        record = collect_case(
            case,
            publication[case["id"]],
            json.loads((retained / "run.json").read_text(encoding="utf-8")),
            json.loads((retained / "jobs.json").read_text(encoding="utf-8")),
            retained / "scan-reports",
        )
        artifact_metadata = json.loads((retained / "artifacts.json").read_text(encoding="utf-8"))
        artifact = next(a for a in artifact_metadata["artifacts"] if a["name"] == "scan-reports")
        record["source"]["artifact"] = {
            key: artifact.get(key) for key in ("id", "name", "created_at", "digest")
        }
        records.append(record)
    args.out.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    print(f"Validated {len(records)} real development scan cases: {args.out}")


if __name__ == "__main__":
    main()
