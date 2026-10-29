"""Reproduce the expanded development replay, coverage checks and per-case report offline."""

import argparse
import json
from pathlib import Path

from development_replay import replay
from prepare_development import verify_plan


def summarize(plan, result):
    rows = []
    counts = dict.fromkeys(plan["requirements"], 0)
    for case in plan["cases"]:
        observed = result["per_case"][case["id"]]
        findings = [
            finding
            for finding in observed["findings"]
            if (
                finding["cve"] == case["cve"]
                and finding["module"] == case["module"]
                and finding["version"].removeprefix("v") == case["version"].removeprefix("v")
            )
        ]
        if not findings:
            raise ValueError(f"missing observed target finding: {case['id']}")
        reach = [finding["reachable"] for finding in findings]
        reachable = True if True in reach else None if None in reach else False
        fixed = [finding["fix_available"] for finding in findings]
        if all(value is True for value in fixed):
            fix_available, fix_source = True, "Trivy FixedVersion"
        elif all(
            finding["fix_available"] is False or finding["trivy_status"] == "affected"
            for finding in findings
        ):
            fix_available, fix_source = False, "Trivy affected/no fixed release; upstream advisory"
        else:
            fix_available, fix_source = None, "unknown"
        probabilities = {finding["epss"] for finding in findings}
        if probabilities != {case["epss"]}:
            raise ValueError("observed EPSS does not match the declared full snapshot")
        row = {
            "id": case["id"],
            "cve": case["cve"],
            "module": case["module"],
            "version": case["version"],
            "cvss": max(f["cvss"] for f in findings),
            "epss": case["epss"],
            "reachable": reachable,
            "fix_available": fix_available,
            "fix_evidence": fix_source,
            "policy_fix_available": fixed[0],
            "L0": observed["L0"]["action"],
            "L1": observed["L1"]["action"],
            "score_basis_points": observed["L1"]["score_basis_points"],
            **observed["counterfactuals"],
            "run_url": observed["source"]["run_url"],
            "commit": observed["source"]["sha"],
        }
        counts["reachable"] += reachable is True
        counts["unreachable"] += reachable is False
        counts["fixed"] += fix_available is True
        counts["no_fix"] += fix_available is False
        counts["low_epss"] += row["epss"] <= plan["epss"]["low_probability_at_most"]
        counts["high_epss"] += row["epss"] >= plan["epss"]["high_probability_at_least"]
        rows.append(row)
    met = {key: counts[key] >= minimum for key, minimum in plan["requirements"].items()}
    return {
        "scope": "deliberately selected development cases only; not held-out or confirmatory",
        "n": len(rows),
        "unique_target_cves": len({row["cve"] for row in rows}),
        "development_seed": plan["development_seed"],
        "epss": result["epss_provenance"],
        "coverage": counts,
        "required_minimum": plan["requirements"],
        "requirements_met": met,
        "epss_changed_decisions": sum(row["epss_changed_decision"] for row in rows),
        "reachability_changed_decisions": sum(row["reachability_changed_decision"] for row in rows),
        "hard_rule_blocks": sum(row["L1"] == "block_deployment" for row in rows),
        "cases": rows,
    }


def markdown(summary):
    epss, rows = summary["epss"], summary["cases"]

    def yesno(value):
        return "unknown" if value is None else "yes" if value else "no"

    lines = [
        "# Expanded development security cases",
        "",
        f"Eight real app-ci cases: {summary['epss_changed_decisions']}/8 decisions changed "
        f"due to EPSS and {summary['reachability_changed_decisions']}/8 due to reachability.",
        "",
        "Each decision uses every real scan finding in the image. EPSS effects compare actual "
        "L1 with the same case at zero EPSS; reachability effects compare actual L1 with measured "
        "reachability replaced by unknown. Missing EPSS remains missing; hard rules remain active.",
        "",
        "| Case / CI run | CVE | Module@version | CVSS | EPSS | Reachable | Fix available "
        "| L0 | L1 | EPSS changed L1 | Reachability changed L1 |",
        "| --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- | --- |",
    ]
    action = {
        "block_deployment": "block",
        "request_security_review": "review",
        "allow_with_findings": "allow with findings",
        "allow_deployment": "allow",
    }
    for row in rows:
        lines.append(
            f"| [{row['id']}]({row['run_url']}) | {row['cve']} | "
            f"`{row['module']}@{row['version']}` | {row['cvss']} | {row['epss']:.5f} | "
            f"{yesno(row['reachable'])} | {yesno(row['fix_available'])} | {action[row['L0']]} | "
            f"{action[row['L1']]} | {yesno(row['epss_changed_decision'])} | "
            f"{yesno(row['reachability_changed_decision'])} |"
        )
    coverage = ", ".join(f"{key}={value}" for key, value in summary["coverage"].items())
    lines += [
        "",
        f"Observed mix: {coverage}. Low EPSS means <=0.01; high means >=0.10, "
        "declared before the scenario pushes. All six required coverage minima must pass.",
        "",
        "Allow with findings still requires a human through the existing gate. Unreachable "
        "means the complete pinned govulncheck source/symbol run produced only module/package "
        "findings for that CVE, not an absence of scan output. Reachable is govulncheck's "
        "conservative call-graph result, not proof that the service can be exploited. Legacy "
        "JWT's advisory has no symbol-specific metadata, so its reachable result is coarse.",
        "",
        "The legacy JWT module has no known fixed version on that module path: Trivy reports "
        "Status=affected and omits FixedVersion; the Go advisory has no fixed event. The report "
        "records no known fix, while the policy preserves the absent field as unknown and "
        "conservatively applies the full no-fix weight. No missing field was filled in to "
        "change a decision. Migration to jwt/v4 or jwt/v5 is a different module path.",
        "",
        f"FIRST source: [{epss['date']}]({epss['source_url']}), {epss['rows']:,} CVEs, "
        f"model {epss['model_version']}; full source cached outside git in `state/epss/`.",
        "",
        f"- CSV SHA-256: `{epss['csv_sha256']}`",
        f"- Gzip SHA-256: `{epss['gzip_sha256']}`",
        "",
        "These eight cases contain only three distinct target CVEs, including paired versions "
        "of the same CVE. They test policy paths and are not eight independent vulnerability "
        "families or evidence of generalization. Both older x/net cases need a compatible "
        "gRPC pin; that pin has a real CRITICAL finding, so both cases block under the hard "
        "rule regardless of EPSS. Current fixed gRPC versions require HTTP/2 APIs absent "
        "from the vulnerable x/net versions. These two cases establish high-EPSS coverage "
        "but do not isolate an EPSS-only decision effect. EPSS changed no whole-case decision.",
        "",
        "The initial Rapid Reset candidate CVE-2023-44487 is MEDIUM in its current "
        "upstream advisory and cannot be a HIGH target. The same x/net v0.16.0 case instead "
        "uses its real HIGH CVE-2023-45288, with EPSS 0.91969; the correction is recorded in "
        "the plan. No scanner severity, CVSS, reachability or result was invented or filtered "
        "to make an action change. All observed findings remain in the replay JSON.",
        "",
        "The first local x/net build with gRPC 1.83.2 failed because Framer APIs were missing. "
        "It was rejected before pushing and is not counted as a security block. All eight "
        "reported scenario commits passed the real Linux build, unit tests, optional jobs, "
        "Trivy and pinned govulncheck in app-ci. Raw artifacts and local preparation logs "
        "remain outside git; committed sanitized observations include their SHA-256 values.",
        "",
        "## Reproduction",
        "",
        "From the repository root with the project environment and agent package installed:",
        "",
        "```powershell",
        "python ci/security/download_epss.py --date 2026-10-28 --cache state/epss "
        "--sha256 " + epss["gzip_sha256"],
        "python ci/security/report_development.py",
        "```",
        "",
        "The second command is offline: it validates the full snapshot, combines the 14 prior "
        "dev cases with the eight retained new cases, recomputes L0/L1 and counterfactuals, "
        "checks coverage, and writes `ci/security/dev-results.json`, "
        "`ci/security/development-results.json`, and this report. Tests use only synthetic "
        "scanner reports, CI metadata and FIRST fixtures; never the network or real held-out data.",
        "",
        "To reproduce the application fixtures, use "
        "`python ci/security/prepare_development.py --go <Go executable>`. It prepares and "
        "checks builds locally in ignored state, refusing to overwrite prior preparations. "
        "The immutable scenario commits linked above retain the actual go.mod/go.sum and "
        "benign symbol-call probes. Rerunning hosted scans may see updated vulnerability "
        "databases.",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epss", type=Path, default=Path("state/epss/2026-10-28.csv"))
    args = parser.parse_args()
    root = Path("ci/security")
    plan = json.loads((root / "development-suite.json").read_text(encoding="utf-8"))
    verify_plan(plan, args.epss)
    new = json.loads((root / "development-observations.json").read_text(encoding="utf-8"))
    if {case["id"] for case in new} != {case["id"] for case in plan["cases"]}:
        raise ValueError("observations must match the declared development cases exactly")
    if len(new) != len(plan["cases"]):
        raise ValueError("duplicate development observations")
    original = json.loads((root / "dev-cases.json").read_text(encoding="utf-8"))
    ids = {case["id"] for case in new}
    cases = [case for case in original if case["id"] not in ids] + new
    result = replay(cases, args.epss)
    summary = summarize(plan, result)
    if not all(summary["requirements_met"].values()):
        raise ValueError(f"required observed development mix not met: {summary['coverage']}")
    (root / "dev-cases.json").write_text(json.dumps(cases, indent=2) + "\n", encoding="utf-8")
    (root / "dev-results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (root / "development-results.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    Path("docs/security-dev-cases.md").write_text(markdown(summary), encoding="utf-8")
    print(
        json.dumps(
            {
                key: summary[key]
                for key in (
                    "n",
                    "coverage",
                    "requirements_met",
                    "epss_changed_decisions",
                    "reachability_changed_decisions",
                    "hard_rule_blocks",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
